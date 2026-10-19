import json
import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from agent_box import claude_settings, external_launch, notify, runtime
from agent_box.config import Config, HostConfig, NotificationsConfig, TrackerConnection
from agent_box.errors import AgentBoxError
from agent_box.mounts import MountSpec
from agent_box.workspace import Workspace


class IsolatedRuntimeTest(unittest.TestCase):
    def setUp(self):
        self._state_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._state_tmp.cleanup)
        state = Path(self._state_tmp.name) / "state"
        self._patches = [
            mock.patch.object(runtime, "STATE_ROOT", state),
            mock.patch.object(runtime, "BUILD_STATE", state / "build"),
            mock.patch.object(runtime, "TOOLS_REFRESH_FILE", state / "build/tools-refresh"),
            mock.patch.object(runtime, "NOTIFY_STATE_ROOT", state / "notify"),
            mock.patch.object(runtime, "NOTIFY_CREDENTIALS", state / "runtime/notify.json"),
            mock.patch.object(runtime, "TRACKER_RUNTIME_DIR", state / "runtime/tracker"),
            mock.patch.object(runtime, "POLICY_RUNTIME_DIR", state / "runtime/policy"),
            mock.patch.object(runtime, "WORKFLOW_STATE", state / "workflow"),
            mock.patch.object(runtime, "load_config", return_value=Config()),
            mock.patch.object(runtime, "existing_workspace_spec", return_value=None),
            mock.patch.object(runtime, "CLAUDE_HOME", state / "claude"),
            mock.patch.object(runtime, "CODEX_HOME", state / "codex"),
            mock.patch.object(runtime, "XDG_CACHE_HOME", state / "cache"),
            mock.patch.object(runtime, "AGENT_SKILLS_DIR", state / "agents/skills"),
            mock.patch.object(runtime, "MANAGED_SKILLS_SOURCE", state / "shipped-skills"),
            # Host launches unless a test says otherwise; the suite itself may run in a box.
            mock.patch.object(runtime, "inside_box", return_value=False),
        ]
        for patch in self._patches:
            patch.start()
            self.addCleanup(patch.stop)


class IsolatedRuntimeFixtureTests(unittest.TestCase):
    def test_refresh_token_stays_in_fixture_state_and_cleanup_restores_paths(self):
        """A doctor-style refresh must not create or change a host build marker."""
        with tempfile.TemporaryDirectory() as tmp:
            for has_refresh in (False, True):
                with self.subTest(has_refresh=has_refresh):
                    fake_host_state = Path(tmp) / f"host-state-{has_refresh}"
                    fake_host_build = fake_host_state / "build"
                    fake_host_refresh = fake_host_build / "tools-refresh"
                    fake_host_cache = Path(tmp) / f"host-cache-{has_refresh}"
                    if has_refresh:
                        fake_host_build.mkdir(parents=True)
                        fake_host_refresh.write_text("host marker\n")

                    with mock.patch.object(runtime, "STATE_ROOT", fake_host_state), \
                         mock.patch.object(runtime, "BUILD_STATE", fake_host_build), \
                         mock.patch.object(runtime, "TOOLS_REFRESH_FILE", fake_host_refresh), \
                         mock.patch.object(runtime, "XDG_CACHE_HOME", fake_host_cache):
                        case = IsolatedRuntimeTest()
                        case.setUp()
                        try:
                            isolated_refresh = runtime.TOOLS_REFRESH_FILE
                            runtime.ensure_state_dirs()
                            runtime.ensure_refresh_token()
                            self.assertTrue(isolated_refresh.is_file())
                            self.assertFalse(fake_host_cache.exists())
                            if has_refresh:
                                self.assertEqual(fake_host_refresh.read_text(), "host marker\n")
                            else:
                                self.assertFalse(fake_host_state.exists())
                        finally:
                            case.doCleanups()

                        self.assertIs(runtime.STATE_ROOT, fake_host_state)
                        self.assertIs(runtime.BUILD_STATE, fake_host_build)
                        self.assertIs(runtime.TOOLS_REFRESH_FILE, fake_host_refresh)
                        self.assertIs(runtime.XDG_CACHE_HOME, fake_host_cache)


class BuildCacheTests(IsolatedRuntimeTest):
    def test_build_update_rebuild_have_distinct_refresh_and_base_policies(self):
        """An ordinary update must not invalidate stable layers via a new base."""
        from agent_box.identity import HostIdentity

        identity = HostIdentity(name="tester", uid=1234, gid=1234, home="/home/tester")
        with mock.patch.object(runtime.image_build, "build", return_value=(0, "image-id")) as call:
            tokens = []
            for refresh, no_cache in ((False, False), (True, False), (False, False), (True, True)):
                self.assertEqual(runtime.build_image(runtime.PACKAGE_ROOT, refresh, no_cache,
                                                    identity=identity, config=Config()), 0)
                argv = call.call_args.args[4]
                self.assertEqual(call.call_args.args[5], no_cache)
                tokens.append(runtime.TOOLS_REFRESH_FILE.read_text().strip())
                self.assertIn(f"TOOLS_REFRESH={tokens[-1]}", argv)
            self.assertNotEqual(tokens[0], tokens[1])
            self.assertEqual(tokens[1], tokens[2])
            self.assertNotEqual(tokens[2], tokens[3])


class ContainerStatePreparationTests(IsolatedRuntimeTest):
    """A new box prepares bind sources before mount argv rendering."""

    def test_preparation_creates_missing_state_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            with mock.patch.object(runtime, "STATE_ROOT", state_root), \
                 mock.patch.object(runtime, "BUILD_STATE", state_root / "build"), \
                 mock.patch.object(runtime, "NOTIFY_STATE_ROOT", state_root / "notify"):
                self.assertFalse(state_root.exists())
                runtime.prepare_container_lifetime_state(Config(), mount_notifications=False)
                self.assertTrue((state_root / "cache").is_dir())
                self.assertTrue((state_root / "uv").is_dir())
                self.assertTrue((state_root / "mise").is_dir())
                self.assertTrue((state_root / "mise-config").is_dir())
                self.assertTrue((state_root / "cargo").is_dir())
                self.assertTrue((state_root / "rustup").is_dir())
                self.assertTrue((state_root / "notify").is_dir())


class MiseStateAcrossBoxesTests(IsolatedRuntimeTest):
    def test_global_tool_choice_outlives_the_box_and_is_shared(self):
        """`mise use -g` writes ~/.config/mise; in the box's own home it died with the box,
        leaving installed tools behind a shim with no version. It must come from agent-box
        state shared by every workspace, as the installed tools do, and never from the host."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seen = []
            for name in ("first", "second"):
                (root / name).mkdir()
                argv = runtime.container_argv(Workspace(root / name, root / name), [], root, ["codex"])
                seen.append([flag for flag in argv if flag.endswith(f"dst={runtime.HOST_HOME}/.config/mise")])
            self.assertEqual(seen[0], [f"type=bind,src={runtime.STATE_ROOT}/mise-config,dst={runtime.HOST_HOME}/.config/mise"])
            self.assertEqual(seen[0], seen[1])

    def test_trusted_project_configs_outlive_the_box(self):
        """`mise trust` writes to mise's state directory; in the box's own home it died with
        the box and every project tool shim then failed as untrusted. It must sit inside the
        persistent, shared mise data mount."""
        with tempfile.TemporaryDirectory() as tmp:
            argv = runtime.container_argv(Workspace(Path(tmp), Path(tmp)), [], Path(tmp), ["codex"])
        data = f"type=bind,src={runtime.STATE_ROOT}/mise,dst={runtime.HOST_HOME}/.local/share/mise"
        self.assertIn(data, argv)
        self.assertIn(f"--env=MISE_STATE_DIR={runtime.HOST_HOME}/.local/share/mise/state", argv)

class ImageExistenceTests(unittest.TestCase):
    def test_missing_image_is_distinct_from_podman_storage_failure(self):
        with mock.patch.object(
            runtime.subprocess, "run", return_value=mock.Mock(returncode=1, stdout="", stderr=""),
        ):
            self.assertFalse(runtime.image_exists())

        with mock.patch.object(
            runtime.subprocess, "run", return_value=mock.Mock(returncode=125, stdout="", stderr="storage failure"),
        ):
            with self.assertRaisesRegex(AgentBoxError, "could not determine image state"):
                runtime.image_exists()


class MountOverlapWithProjectRootTests(IsolatedRuntimeTest):
    """A --mount that's a parent of (or identical to) the project root must
    collapse to one podman --mount flag, not two overlapping ones."""

    def test_parent_extra_mount_collapses_with_project_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            project = parent / "project"
            project.mkdir()
            workspace = Workspace(project.resolve(), project.resolve())
            extra = MountSpec(path=parent.resolve(), read_only=False, label="--mount")
            argv = runtime.container_argv(workspace, [extra], project, ["codex"])
            mount_flags = [a for a in argv if a.startswith("type=bind")]
            matching_project = [f for f in mount_flags if f"src={project.resolve()},dst={project.resolve()}" in f]
            matching_parent = [f for f in mount_flags if f"src={parent.resolve()},dst={parent.resolve()}" in f]
            self.assertEqual(matching_project, [], "the narrower project mount must not survive alongside the parent")
            self.assertEqual(len(matching_parent), 1)

    def test_conflicting_ro_rw_between_extra_mount_and_project_root_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            project = parent / "project"
            project.mkdir()
            workspace = Workspace(project.resolve(), project.resolve())
            extra = MountSpec(path=parent.resolve(), read_only=True, label="--mount")
            with self.assertRaises(Exception):
                runtime.container_argv(workspace, [extra], project, ["codex"])


class ClaudeHomeMountTests(IsolatedRuntimeTest):
    def setUp(self):
        super().setUp()
        self.root = Path(self._state_tmp.name)
        self.home = self.root / "home"
        self.default = self.home / ".claude"
        self.sidecar = self.home / ".claude.json"
        self.default.mkdir(parents=True)
        self.project = self.root / "project"
        self.project.mkdir()

    def mounts(self, claude_home):
        with mock.patch.multiple(runtime, HOST_HOME=self.home, CLAUDE_HOME=claude_home,
                                 DEFAULT_CLAUDE_HOME=self.default, DEFAULT_CLAUDE_SIDECAR=self.sidecar), \
                mock.patch.object(runtime, "agent_tree_mounts", return_value=[]):
            return runtime.build_mounts(Workspace(self.project, self.project), [], self.project, Config())

    def test_default_home_keeps_its_existing_writable_sidecar(self):
        """Default Claude preferences live outside its directory and need a separate bind."""
        self.sidecar.write_text("default preferences")
        self.assertIn(f"type=bind,src={self.sidecar},dst={self.sidecar}", self.mounts(self.default))
        self.assertEqual(self.sidecar.read_text(), "default preferences")

    def test_missing_default_sidecar_is_neither_mounted_nor_created(self):
        self.assertFalse(any(f"dst={self.sidecar}" in arg for arg in self.mounts(self.default)))
        self.assertFalse(self.sidecar.exists())

    def test_relocated_home_exposes_its_tree_without_unrelated_sidecars(self):
        """Relocation must not retain writable access to default or sibling preferences."""
        relocated = self.root / "private/claude"
        relocated.mkdir(parents=True)
        own = relocated / ".claude.json"
        sibling = relocated.parent / ".claude.json"
        for path in (self.sidecar, sibling, own):
            path.write_text(str(path))
        mounts = self.mounts(relocated)
        self.assertIn(f"type=bind,src={relocated},dst={relocated}", mounts)
        for path in (self.sidecar, sibling):
            self.assertFalse(any(f"dst={path}" in arg for arg in mounts), path)
        for path in (self.sidecar, sibling, own):
            self.assertEqual(path.read_text(), str(path))


class CredentialSnapshotMountTests(IsolatedRuntimeTest):
    def test_workspace_mounts_exclude_other_workspace_and_host_snapshots(self):
        """Shared keep-id containers must not receive another workspace's session account."""
        with tempfile.TemporaryDirectory() as project:
            root = Path(project)
            workspaces = [Workspace(root / name, root / name) for name in ('first', 'second')]
            for workspace in workspaces:
                workspace.host_root.mkdir()
            config = Config()
            config.tracker.enabled = True
            config.tracker.connections['vikunja'] = TrackerConnection('https://tracker.example', 'host-secret')
            with mock.patch.object(runtime, 'container_tracker_url', side_effect=lambda url: url):
                host = runtime.session_runtime(config, container=False)
                snapshots, specs, disposable = [], [], []
                for index, workspace in enumerate(workspaces):
                    config.tracker.connections['vikunja'].token = f'workspace-{index}-secret'
                    spec = runtime.workspace_container_spec(workspace, [], root, config)
                    specs.append(spec)
                    # Both launch routes must create precisely the scoped mount source.
                    disposable.append(runtime.container_argv(workspace, [], root, ['codex'], config))
                    source = runtime.tracker_mounts(config, spec.runtime_state)[0][0]
                    self.assertTrue(source.is_dir())
                    snapshot = runtime.session_runtime(config, runtime_state=spec.runtime_state)
                    snapshots.append(snapshot)
                first_contents = snapshots[0].read_bytes()
                config.tracker.connections['vikunja'].token = 'later-secret'
                later = runtime.session_runtime(config, runtime_state=specs[0].runtime_state)

            def sources(argv):
                return [Path(arg.split('src=', 1)[1].split(',', 1)[0])
                        for arg in argv if arg.startswith('type=bind,src=')]

            def visible(path, mounts):
                return any(path == source or source in path.parents for source in mounts)

            for index, spec in enumerate(specs):
                for argv in (spec.create_argv, disposable[index]):
                    mounts = sources(argv)
                    self.assertTrue(visible(snapshots[index], mounts))
                    self.assertFalse(visible(snapshots[1 - index], mounts))
                    self.assertFalse(visible(host, mounts))
                disabled = runtime.workspace_container_spec(workspaces[index], [], root, Config())
                self.assertFalse(visible(snapshots[index], sources(disabled.create_argv)))
                self.assertFalse(visible(host, sources(disabled.create_argv)))
            self.assertNotEqual(later, snapshots[0])
            self.assertEqual(snapshots[0].read_bytes(), first_contents)
            self.assertIn('workspace-0-secret', first_contents.decode())
            self.assertIn('workspace-1-secret', snapshots[1].read_text())
            self.assertIn('host-secret', host.read_text())


class ContainerArgvTests(IsolatedRuntimeTest):
    def test_same_absolute_path_mounted_no_workspace_rewrite(self):
        with tempfile.TemporaryDirectory() as project:
            workspace = Workspace(Path(project), Path(project))
            argv = runtime.container_argv(workspace, [], Path(project), ["codex", "app-server"])
            self.assertIn("podman", argv)
            self.assertIn(f"--workdir={project}", argv)
            self.assertTrue(any(f"src={project},dst={project}" in a for a in argv))
            self.assertNotIn("dst=/workspace", " ".join(argv))

    def test_legacy_disposable_argv_does_not_mount_all_runtime_state(self):
        with tempfile.TemporaryDirectory() as project:
            workspace = Workspace(Path(project), Path(project))
            argv = runtime.container_argv(workspace, [], Path(project), ["codex"])
        self.assertNotIn(f"src={runtime.STATE_ROOT},dst={runtime.STATE_ROOT}", argv)

    def test_rust_tool_state_uses_standard_container_paths(self):
        with tempfile.TemporaryDirectory() as project:
            workspace = Workspace(Path(project), Path(project))
            argv = runtime.container_argv(workspace, [], Path(project), ["codex"])
        self.assertIn(
            f"type=bind,src={runtime.STATE_ROOT / 'cargo'},dst={runtime.HOST_HOME}/.cargo", argv,
        )
        self.assertIn(
            f"type=bind,src={runtime.STATE_ROOT / 'rustup'},dst={runtime.HOST_HOME}/.rustup", argv,
        )

    def test_runtime_path_prefers_mise_shims_then_cargo_bin(self):
        expected_prefix = (
            f"{runtime.HOST_HOME}/.local/share/mise/shims:{runtime.HOST_HOME}/.cargo/bin:"
        )
        self.assertTrue(runtime.CONTAINER_RUNTIME_PATH.startswith(expected_prefix))

    def test_extra_env_is_passed_before_the_image(self):
        with tempfile.TemporaryDirectory() as project:
            workspace = Workspace(Path(project), Path(project))
            argv = runtime.container_argv(
                workspace, [], Path(project), ["claude"], extra_env=["FOO=1"])
            self.assertIn("--env=FOO=1", argv)
            self.assertLess(argv.index("--env=FOO=1"), argv.index("claude"))

    def test_extra_mounts_included(self):
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as extra:
            workspace = Workspace(Path(project), Path(project))
            spec = MountSpec(path=Path(extra).resolve(), read_only=True)
            argv = runtime.container_argv(workspace, [spec], Path(project), ["codex"])
            self.assertTrue(any(f"src={spec.path},dst={spec.path}" in a and "ro=true" in a for a in argv))

    def test_hardening_flags_present(self):
        with tempfile.TemporaryDirectory() as project:
            workspace = Workspace(Path(project), Path(project))
            argv = runtime.container_argv(workspace, [], Path(project), ["codex"])
            self.assertIn("--cap-drop=all", argv)
            self.assertIn("--security-opt=no-new-privileges", argv)


class InvocationEnvironmentTests(unittest.TestCase):
    def test_github_token_discovered_for_this_exec_is_inherited_by_name(self):
        config = Config()
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(runtime, "github_env", side_effect=lambda _config: (
                 os.environ.__setitem__("GH_TOKEN", "derived-token") or ["--env=GH_TOKEN"]
             )), \
             mock.patch.object(runtime, "provider_env", return_value=[]):
            flags, env = runtime.invocation_env(config, "codex")
        self.assertIn("GH_TOKEN", flags)
        self.assertEqual(env["GH_TOKEN"], "derived-token")

    def test_every_launch_keeps_background_managed_runs_through_pressure_spikes(self):
        """Claude Code's PSI reaper killed a background managed review, CLI and all.
        A box's tmux server keeps its first launch's environment, so a Codex or
        shell launch must carry the opt-out for a later Claude pane. It is per
        exec, so enabling it never recreates a box, and env.set still wins."""
        name = runtime.PRESSURE_REAP_OPT_OUT
        for agent in ("claude", "codex", "shell"):
            with self.subTest(agent=agent), \
                 mock.patch.object(runtime, "github_env", return_value=[]), \
                 mock.patch.object(runtime, "provider_env", return_value=[]), \
                 mock.patch.object(runtime, "user_env", return_value=[f"--env={name}=0"]):
                pairs, _ = runtime.invocation_env(Config(), agent)
                self.assertLess(pairs.index(f"{name}=1"), pairs.index(f"{name}=0"))
        with tempfile.TemporaryDirectory() as project:
            workspace = Workspace(Path(project), Path(project))
            self.assertNotIn(name, " ".join(runtime.container_argv(workspace, [], Path(project), ["claude"])))


class HostReaperOptOutTests(IsolatedRuntimeTest):
    def test_host_sessions_carry_the_pressure_reaper_opt_out_unless_env_set_overrides(self):
        """A host coordinator runs the same background managed reviews as one in a box."""
        name = runtime.PRESSURE_REAP_OPT_OUT
        for configured, expected in (({}, "1"), ({name: "0"}, "0")):
            config = Config()
            config.box.env.set = configured
            with self.subTest(configured=configured), \
                 mock.patch.object(runtime.shutil, "which", return_value="/usr/bin/claude"), \
                 mock.patch.object(runtime, "resolve_workspace", return_value=Workspace(Path.cwd(), Path.cwd())), \
                 mock.patch.object(runtime, "launch_config", return_value=config), \
                 mock.patch.object(runtime, "tmux_enabled", return_value=False), \
                 mock.patch.object(runtime, "remote_control_invocation", return_value=False), \
                 mock.patch.object(runtime, "ensure_state_dirs"), \
                 mock.patch.object(runtime, "ensure_refresh_token"), \
                 mock.patch.object(runtime.subprocess, "call", return_value=0) as call:
                runtime.host_main("claude", [])
            self.assertEqual(call.call_args.kwargs["env"][name], expected)


class CodexRuntimeOverridesTests(IsolatedRuntimeTest):
    def _overrides_for(self, config_text: str | None) -> tuple[list[str], tuple[bytes, bytes]]:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex-home"
            codex_home.mkdir()
            config_path = codex_home / "config.toml"
            if config_text is not None:
                config_path.write_text(config_text)
            before = config_path.read_bytes() if config_path.exists() else b""
            with mock.patch.object(runtime, "CODEX_HOME", codex_home), \
                 mock.patch.object(runtime, "CODEX_PLUGIN_CACHE", codex_home / "plugins/cache"):
                overrides = runtime.codex_runtime_overrides()
            after = config_path.read_bytes() if config_path.exists() else b""
            return overrides, (before, after)

    def test_suppresses_chatgpt_desktop_mcp_without_mutating_config(self):
        overrides, contents = self._overrides_for(
            "[mcp_servers.node_repl]\n"
            'command = "/usr/lib/chatgpt/resources/cua_node/bin/node_repl"\n'
            "args = []\n"
        )
        self.assertEqual(overrides, ["mcp_servers.node_repl.enabled=false"])
        self.assertEqual(contents[0], contents[1])

    def test_expected_desktop_mcp_filtering_is_silent(self):
        """An enforced container boundary is not a warning on every launch."""
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex-home"
            codex_home.mkdir()
            (codex_home / "config.toml").write_text(
                "[mcp_servers.node_repl]\n"
                'command = "/usr/lib/chatgpt/resources/cua_node/bin/node_repl"\n'
            )
            with mock.patch.object(runtime, "CODEX_HOME", codex_home), \
                 mock.patch.object(runtime, "warn") as warn:
                overrides = runtime.codex_runtime_overrides()

        self.assertEqual(overrides, ["mcp_servers.node_repl.enabled=false"])
        warn.assert_not_called()

    def test_suppresses_chatgpt_desktop_mcp_loaded_by_enabled_plugin(self):
        """Plugin MCPs need their plugin-scoped override or Codex treats the
        override as a new, invalid top-level MCP configuration.

        A plugin id carries an ``@``, which TOML forbids in a bare key and
        Codex does not unquote, so the path is an inline table: as a dotted
        path it was rejected by the external launch's own value check, and
        every external Codex launch on such a host died before Codex started.
        """
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex-home"
            plugin = codex_home / "plugins/cache/openai-bundled/computer-use/1.0.0"
            (plugin / ".codex-plugin").mkdir(parents=True)
            (codex_home / "config.toml").write_text(
                '[plugins."computer-use@openai-bundled"]\n'
                "enabled = true\n"
            )
            (plugin / ".codex-plugin/plugin.json").write_text(json.dumps({
                "name": "computer-use",
                "version": "1.0.0",
                "mcpServers": "./.mcp.json",
            }))
            (plugin / ".mcp.json").write_text(json.dumps({
                "mcpServers": {
                    "cua_repl": {
                        "command": "/usr/lib/chatgpt/resources/cua_node/bin/node",
                        "args": ["scripts/launch.mjs"],
                    },
                },
            }))

            with mock.patch.object(runtime, "CODEX_HOME", codex_home), \
                 mock.patch.object(runtime, "CODEX_PLUGIN_CACHE", codex_home / "plugins/cache"):
                overrides = runtime.codex_runtime_overrides()

        self.assertEqual(overrides, [
            'plugins={"computer-use@openai-bundled"={mcp_servers={cua_repl={enabled=false}}}}',
        ])

    def test_suppresses_desktop_mcp_whose_name_codex_cannot_read_unquoted(self):
        """A quoted segment would name a server that does not exist.

        Codex splits a ``-c`` key on dots and never unquotes a segment, so
        quoting the name -- valid TOML, and what this used to emit -- aborts
        the launch with "invalid transport" instead of disabling the server.
        """
        overrides, _contents = self._overrides_for(
            '[mcp_servers."desktop repl"]\n'
            'command = "/usr/lib/chatgpt/resources/something/bin/server"\n'
        )
        self.assertEqual(overrides, ['mcp_servers={"desktop repl"={enabled=false}}'])

    def test_generated_overrides_are_values_the_launch_path_can_parse(self):
        """agent-box must be able to read the ``-c`` values it writes itself.

        Codex needs a plugin id verbatim, the launch's value check needs TOML;
        when the two disagreed, the check rejected agent-box's own MCP
        overrides and the launch failed before Codex was reached.
        """
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex-home"
            plugin = codex_home / "plugins/cache/openai-bundled/computer-use/1.0.0"
            (plugin / ".codex-plugin").mkdir(parents=True)
            (codex_home / "config.toml").write_text(
                "[mcp_servers.node_repl]\n"
                'command = "/usr/lib/chatgpt/resources/cua_node/bin/node_repl"\n'
                '[mcp_servers."🚀 node"]\n'
                'command = "/usr/lib/chatgpt/resources/cua_node/bin/node_repl"\n'
                "[mcp_servers.remote_dev]\n"
                'command = "/usr/lib/chatgpt/resources/cua_node/bin/node_repl"\n'
                '[plugins."computer-use@openai-bundled"]\n'
                "enabled = true\n"
            )
            (plugin / ".codex-plugin/plugin.json").write_text(json.dumps({
                "name": "computer-use", "version": "1.0.0", "mcpServers": "./.mcp.json",
            }))
            (plugin / ".mcp.json").write_text(json.dumps({"mcpServers": {
                "cua_repl": {"command": "/usr/lib/chatgpt/resources/cua_node/bin/node"},
                "🚀 repl": {"command": "/usr/lib/chatgpt/resources/cua_node/bin/node"},
            }}))

            with mock.patch.object(runtime, "CODEX_HOME", codex_home), \
                 mock.patch.object(runtime, "CODEX_PLUGIN_CACHE", codex_home / "plugins/cache"):
                command = runtime.codex_command(["exec", "hello"], Config())

        values = [command[index + 1] for index, arg in enumerate(command) if arg == "-c"]
        self.assertEqual(len(values), 5)
        for value in values:
            with self.subTest(value=value):
                tomllib.loads(value)
        external_launch.invocation_settings("codex", command[1:])

    def test_quoted_names_keep_the_control_characters_and_astral_planes_parseable(self):
        """An escaper for TOML is not an escaper for JSON.

        ``json.dumps`` writes a non-BMP character as a surrogate pair and TOML
        rejects a surrogate escape, while its non-ASCII mode leaves DEL
        literal, which a TOML basic string forbids. Either way the generated
        ``-c`` stops being a value Codex -- or agent-box itself -- can parse.
        """
        for spelling, name in (
            ('"🚀 node"', "🚀 node"),
            ('"del\\u007Fhere"', "del\x7fhere"),
            ('"quo\\"te"', 'quo"te'),
            ('"back\\\\slash"', "back\\slash"),
        ):
            with self.subTest(name=name):
                overrides, _contents = self._overrides_for(
                    f"[mcp_servers.{spelling}]\n"
                    'command = "/usr/lib/chatgpt/resources/something/bin/server"\n'
                )
                self.assertEqual(len(overrides), 1)
                self.assertEqual(
                    tomllib.loads(overrides[0]),
                    {"mcp_servers": {name: {"enabled": False}}},
                )

    def test_a_name_without_a_toml_spelling_is_reported_instead_of_emitted(self):
        """A lone surrogate has no TOML string, and no argv either.

        Only a malformed plugin manifest can carry one; writing it would put
        invalid UTF-8 in the command Codex parses and fail the whole launch,
        which is worse than leaving that one desktop MCP enabled.
        """
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex-home"
            plugin = codex_home / "plugins/cache/openai-bundled/computer-use/1.0.0"
            (plugin / ".codex-plugin").mkdir(parents=True)
            (codex_home / "config.toml").write_text(
                '[plugins."computer-use@openai-bundled"]\nenabled = true\n'
            )
            (plugin / ".codex-plugin/plugin.json").write_text(json.dumps({
                "name": "computer-use", "version": "1.0.0", "mcpServers": "./.mcp.json",
            }))
            (plugin / ".mcp.json").write_text(json.dumps({"mcpServers": {
                "\ud83d": {"command": "/usr/lib/chatgpt/resources/cua_node/bin/node"},
            }}))

            with mock.patch.object(runtime, "CODEX_HOME", codex_home), \
                 mock.patch.object(runtime, "CODEX_PLUGIN_CACHE", codex_home / "plugins/cache"), \
                 mock.patch.object(runtime, "warn") as warn:
                overrides = runtime.codex_runtime_overrides()

        self.assertEqual(overrides, [])
        self.assertIn("no TOML spelling", warn.call_args[0][0])

    def test_leaves_command_name_and_normal_absolute_path_mcps_enabled(self):
        overrides, _contents = self._overrides_for(
            "[mcp_servers.example]\ncommand = \"npx\"\nargs = [\"example-mcp\"]\n"
            "[mcp_servers.custom]\ncommand = \"/usr/local/bin/custom-mcp\"\n"
        )
        self.assertEqual(overrides, [])

    def test_suppresses_only_chatgpt_desktop_mcps_when_multiple_are_configured(self):
        overrides, _contents = self._overrides_for(
            "[mcp_servers.node_repl]\n"
            'command = "/usr/lib/chatgpt/resources/cua_node/bin/node_repl"\n'
            "[mcp_servers.custom]\ncommand = \"uvx\"\nargs = [\"foo\"]\n"
            "[mcp_servers.desktop_other]\n"
            'command = "/usr/lib/chatgpt/resources/something/bin/server"\n'
        )
        self.assertEqual(overrides, [
            "mcp_servers.desktop_other.enabled=false",
            "mcp_servers.node_repl.enabled=false",
        ])

    def test_missing_or_unrelated_config_has_no_mcp_overrides(self):
        missing_overrides, _contents = self._overrides_for(None)
        no_mcp_overrides, _contents = self._overrides_for(
            "model = \"gpt-5\"\n[mcp_servers.no_command]\nargs = []\n"
        )
        self.assertEqual(missing_overrides, [])

    def test_malformed_config_warns_and_launch_overrides_remain_empty(self):
        with mock.patch.object(runtime, "warn") as warn:
            overrides, _contents = self._overrides_for("[mcp_servers.node_repl\n")
        self.assertEqual(overrides, [])
        warn.assert_called_once()
        self.assertIn("could not parse Codex config", warn.call_args.args[0])

    def test_notifications_use_global_config_without_a_launch_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "alternative-codex-home"
            codex_home.mkdir()
            (codex_home / "config.toml").write_text(
                "[mcp_servers.node_repl]\n"
                'command = "/usr/lib/chatgpt/resources/cua_node/bin/node_repl"\n'
            )
            config = Config(host=HostConfig(notifications=NotificationsConfig(
                enabled=True, telegram_token="token", telegram_chat_id="chat",
            )))
            with mock.patch.object(runtime, "CODEX_HOME", codex_home):
                command = runtime.codex_command(["exec", "hello"], config)
        self.assertEqual(command, [
            "codex",
            "-c", "mcp_servers.node_repl.enabled=false",
            "--dangerously-bypass-approvals-and-sandbox",
            "exec", "hello",
        ])

    def test_native_wait_override_is_only_for_interactive_workflow_sessions(self):
        """Batch commands and either disabled switch must retain ordinary Codex argv."""
        config = Config()
        setting = "features.multi_agent_v2.default_wait_timeout_ms=1680000"
        self.assertNotIn(setting, runtime.codex_command([], config))

        config.workflow.enabled = True
        self.assertIn(setting, runtime.codex_command([], config))
        self.assertNotIn(setting, runtime.codex_command(["exec", "hello"], config))
        config.agents.codex.native_agent_wait.enabled = False
        self.assertNotIn(setting, runtime.codex_command([], config))

    def test_native_wait_override_uses_the_configured_bounded_interval(self):
        """The cache-aware cadence is independent of managed-agent runtime."""
        config = Config()
        config.workflow.enabled = True
        config.workflow.timeout_seconds = 45
        config.agents.codex.native_agent_wait.interval_seconds = 1200
        self.assertIn("features.multi_agent_v2.default_wait_timeout_ms=1200000",
                      runtime.codex_workflow_overrides([], config))
        config.agents.codex.native_agent_wait.interval_seconds = 3600
        self.assertIn("features.multi_agent_v2.default_wait_timeout_ms=3600000",
                      runtime.codex_workflow_overrides([], config))
        config.agents.codex.native_agent_wait.interval_seconds = 3601
        self.assertFalse(any("default_wait_timeout_ms" in setting
                             for setting in runtime.codex_workflow_overrides([], config)))

    def test_native_scout_capacity_tracks_project_limit_independently_of_wait(self):
        """Native scouts must fit the effective limit even with the wait policy disabled."""
        from agent_box import project_config
        config = Config()
        config.workflow.enabled = True
        config.workflow.scout.limit = 6
        config.agents.codex.native_agent_wait.enabled = False
        for limit in (1, 6, 16):
            with self.subTest(limit=limit):
                effective = project_config.apply(config, {"workflow": {"scout": {"limit": limit}}})
                command = runtime.codex_command([], effective)
                self.assertIn(f"agents.max_concurrent_threads_per_session={limit}", command)
                self.assertIn(f"features.multi_agent_v2.max_concurrent_threads_per_session={limit + 1}",
                              command)
                self.assertFalse(any("default_wait_timeout_ms" in arg for arg in command))
        self.assertEqual(config.workflow.scout.limit, 6)

    def test_native_capacity_leaves_non_workflow_and_service_commands_alone(self):
        """Managed workers and service processes must not acquire coordinator policy."""
        config = Config()
        self.assertEqual(runtime.codex_workflow_overrides([], config), [])
        config.workflow.enabled = True
        for rest in (["exec", "hello"], ["review"], ["app-server"], ["--help"], ["--version"]):
            with self.subTest(rest=rest):
                self.assertEqual(runtime.codex_workflow_overrides(rest, config), [])

    def test_native_wait_override_never_writes_the_user_config(self):
        """A launch-time policy is an argv override, not integration application."""
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex-home"
            codex_home.mkdir()
            config_path = codex_home / "config.toml"
            config_path.write_text('model = "user-choice"\n')
            before = config_path.read_bytes()
            config = Config()
            config.workflow.enabled = True
            with mock.patch.object(runtime, "CODEX_HOME", codex_home):
                runtime.codex_command([], config)
            self.assertEqual(config_path.read_bytes(), before)



    def test_non_codex_launches_do_not_inspect_codex_mcp_configuration(self):
        workspace = Workspace(Path.cwd(), Path.cwd())

        for agent in ("claude", "shell", "doctor"):
            with self.subTest(agent=agent), \
                 mock.patch.object(runtime.host_podman, "require"), \
                 mock.patch.object(runtime, "resolve_workspace", return_value=workspace), \
                 mock.patch.object(runtime, "ensure_state_dirs"), \
                 mock.patch.object(runtime, "ensure_refresh_token"), \
                 mock.patch.object(runtime, "load_config", return_value=Config()), \
                 mock.patch.object(runtime, "ensure_workspace_image", return_value="sha256:test-image"), \
                 mock.patch.object(runtime, "sync_managed_skills"), \
                 mock.patch.object(runtime, "claude_skill_links"), \
                 mock.patch.object(runtime, "claude_channel_args", return_value=([], [])), \
                 mock.patch.object(runtime, "launch_in_workspace_container", return_value=0), \
                 mock.patch.object(runtime, "codex_runtime_overrides") as overrides, \
                 self.assertRaises(SystemExit):
                runtime.runtime_main([agent], runtime.PACKAGE_ROOT)
            overrides.assert_not_called()

    def test_bare_launch_starts_the_configured_agent_unless_named(self):
        """An explicit claude/codex wins over default_agent."""
        workspace = Workspace(Path.cwd(), Path.cwd())
        config = Config()
        config.agents.default = "codex"
        for args, expected in (([], "codex"), (["claude"], "claude"), (["codex"], "codex")):
            with self.subTest(args=args), \
                 mock.patch.object(runtime.host_podman, "require"), \
                 mock.patch.object(runtime, "resolve_workspace", return_value=workspace), \
                 mock.patch.object(runtime, "ensure_state_dirs"), \
                 mock.patch.object(runtime, "ensure_refresh_token"), \
                 mock.patch.object(runtime, "load_config", return_value=config), \
                 mock.patch.object(runtime, "ensure_workspace_image", return_value="sha256:test-image"), \
                 mock.patch.object(runtime, "tmux_enabled", return_value=False), \
                 mock.patch.object(runtime, "claude_channel_args", return_value=([], [])), \
                 mock.patch.object(runtime, "codex_command", return_value=["codex"]), \
                 mock.patch.object(runtime, "launch_in_workspace_container", return_value=0) as launch, \
                 self.assertRaises(SystemExit):
                runtime.runtime_main(args, runtime.PACKAGE_ROOT)
            self.assertEqual(launch.call_args.args[2][0], expected)

    def test_external_launch_selects_the_model_for_the_default_agent(self):
        """`ext NAME` without a CLI must pick the model of the agent that will actually start."""
        config = Config()
        config.agents.default = "codex"
        with mock.patch.object(runtime.host_podman, "require"), \
             mock.patch.object(runtime, "resolve_workspace", return_value=Workspace(Path.cwd(), Path.cwd())), \
             mock.patch.object(runtime, "ensure_state_dirs"), \
             mock.patch.object(runtime, "ensure_refresh_token"), \
             mock.patch.object(runtime, "load_config", return_value=config), \
             mock.patch.object(external_launch, "selected_model", side_effect=AgentBoxError("stop")) as selected, \
             self.assertRaisesRegex(AgentBoxError, "stop"):
            runtime.runtime_main(["--model", "m"], runtime.PACKAGE_ROOT, api_provider="deepseek")
        self.assertEqual(selected.call_args.args[1:], ("deepseek", "codex", ["--model", "m"]))




class ClaudeNotifyHookLifecycleTests(IsolatedRuntimeTest):
    """Notification hooks are applied, never installed on the way past.

    ``settings.json`` is Claude's file and the user's; agent-box owns a few
    handlers inside it and nothing else. Applying a configuration installs
    exactly those, leaves every other entry byte-for-byte, and rewrites the
    file only when it actually differs. Starting an agent does none of it.
    """

    def _configured_settings(self, root: Path) -> Path:
        claude_home = root / "claude"
        claude_home.mkdir()
        settings_path = claude_home / "settings.json"
        settings_path.write_text(json.dumps({
            "model": "opus",
            "hooks": {
                "PreToolUse": [{"matcher": "Bash", "hooks": [
                    {"type": "command", "command": "my-user-hook"},
                ]}],
                "Notification": [
                    {"matcher": "other", "hooks": [
                        {"type": "command", "command": "my-other-hook"},
                    ]},
                ],
                "Stop": [{"hooks": [
                    {"type": "command", "command": "my-stop-hook"},
                ]}],
            },
        }))
        return settings_path

    def _notifying(self) -> Config:
        return Config(host=HostConfig(notifications=NotificationsConfig(
            enabled=True, telegram_token="token", telegram_chat_id="chat",
        )))

    def test_apply_and_disable_reconcile_codex_alongside_claude(self):
        """Both providers must gain and lose notifications at the apply boundary."""
        import tomllib
        runtime.apply_config(self._notifying())
        path = runtime.CODEX_HOME / "config.toml"
        self.assertEqual(tomllib.loads(path.read_text())["notify"], ["agent-box", "notify", "codex"])
        runtime.apply_config(Config())
        self.assertNotIn("notify", tomllib.loads(path.read_text()))

    def test_apply_installs_the_managed_hooks_and_disturbs_nothing_else(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings_path = self._configured_settings(root)
            with mock.patch.object(runtime, "CLAUDE_HOME", root / "claude"), \
                 mock.patch.object(runtime, "sync_managed_skills"), \
                 mock.patch.object(runtime, "claude_skill_links"), \
                 mock.patch.object(runtime, "sync_tracker_runtime"), \
                 mock.patch.object(runtime, "ensure_state_dirs"), \
                 mock.patch.object(runtime.notify_module, "sync_credentials"):
                runtime.apply_config(self._notifying())

            settings = json.loads(settings_path.read_text())
            self.assertEqual(settings["model"], "opus")
            self.assertEqual(
                settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"], "my-user-hook")
            other = next(
                entry for entry in settings["hooks"]["Notification"]
                if entry.get("matcher") == "other")
            self.assertEqual(other["hooks"][0]["command"], "my-other-hook")
            self.assertIn(
                "my-stop-hook",
                [h["command"] for e in settings["hooks"]["Stop"] for h in e["hooks"]])

            managed = {event: 0 for event in ("Notification", "Stop", "StopFailure")}
            for event, entries in settings["hooks"].items():
                for entry in entries:
                    for handler in entry.get("hooks", []):
                        if handler.get("command") == notify.NOTIFY_COMMAND:
                            managed[event] = managed.get(event, 0) + 1
            self.assertEqual(managed, {"Notification": 2, "Stop": 1, "StopFailure": 1})

    def test_applying_an_unchanged_configuration_rewrites_nothing(self):
        """Idempotence, asserted at the write rather than at the content."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._configured_settings(root)
            with mock.patch.object(runtime, "CLAUDE_HOME", root / "claude"), \
                 mock.patch.object(runtime, "sync_managed_skills"), \
                 mock.patch.object(runtime, "claude_skill_links"), \
                 mock.patch.object(runtime, "sync_tracker_runtime"), \
                 mock.patch.object(runtime, "ensure_state_dirs"), \
                 mock.patch.object(runtime.notify_module, "sync_credentials"), \
                 mock.patch.object(
                     claude_settings, "atomic_write_json",
                     wraps=claude_settings.atomic_write_json) as write_json:
                runtime.apply_config(self._notifying())
                self.assertEqual(write_json.call_count, 1)
                write_json.reset_mock()
                runtime.apply_config(self._notifying())
                write_json.assert_not_called()

    def test_a_container_launch_leaves_the_hooks_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings_path = self._configured_settings(root)
            before = settings_path.read_text()
            with (
                mock.patch.object(runtime.host_podman, "require"),
                mock.patch.object(runtime, "resolve_workspace", return_value=Workspace(root, root)),
                mock.patch.object(runtime, "ensure_state_dirs"),
                mock.patch.object(runtime, "ensure_refresh_token"),
                mock.patch.object(runtime, "load_config", return_value=self._notifying()),
                mock.patch.object(runtime, "ensure_workspace_image", return_value="sha256:test-image"),
                mock.patch.object(runtime, "claude_channel_args", return_value=([], [])),
                mock.patch.object(runtime, "launch_in_workspace_container", return_value=0),
                mock.patch.object(runtime, "CLAUDE_HOME", root / "claude"),
                self.assertRaises(SystemExit),
            ):
                runtime.runtime_main(["claude"], runtime.PACKAGE_ROOT)
            self.assertEqual(settings_path.read_text(), before)

    def test_a_host_launch_leaves_the_hooks_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings_path = self._configured_settings(root)
            before = settings_path.read_text()
            with (
                mock.patch.object(runtime.shutil, "which", return_value="/usr/bin/claude"),
                mock.patch.object(runtime, "resolve_workspace", return_value=Workspace(root, root)),
                mock.patch.object(runtime, "load_config", return_value=self._notifying()),
                mock.patch.object(runtime, "CLAUDE_HOME", root / "claude"),
                mock.patch.object(runtime.subprocess, "call", return_value=0),
            ):
                self.assertEqual(runtime.host_main("claude", []), 0)
            self.assertEqual(settings_path.read_text(), before)


class HostModeTests(IsolatedRuntimeTest):

    def test_native_host_ignores_conflicting_container_network_settings(self):
        """A host process creates no namespace or mappings and cannot clear them with container-only flags."""
        config = Config()
        config.box.network.mode = "host"
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(runtime.shutil, "which", return_value="/usr/bin/codex"), \
             mock.patch.object(runtime, "resolve_workspace", return_value=Workspace(Path(tmp), Path(tmp))), \
             mock.patch.object(runtime, "runtime_config_base", return_value=config), \
             mock.patch("agent_box.project_config.load", return_value={"box": {"network": {"ports": ["8080:3000"]}}}), \
             mock.patch.object(runtime.subprocess, "call", return_value=0) as call:
            self.assertEqual(runtime.host_main("codex", ["exec", "test"]), 0)
            self.assertEqual(call.call_args.args[0], ["/usr/bin/codex", "exec", "test"])

    def test_bare_host_launch_uses_the_default_agent_like_a_box_launch(self):
        """--host must not answer 'which agent' differently from a box launch."""
        config = Config()
        config.agents.default = "codex"
        for agent, expected in ((None, "codex"), ("claude", "claude")):
            with self.subTest(agent=agent), tempfile.TemporaryDirectory() as tmp, \
                 mock.patch.object(runtime.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}"), \
                 mock.patch.object(runtime, "resolve_workspace", return_value=Workspace(Path(tmp), Path(tmp))), \
                 mock.patch.object(runtime, "launch_config", return_value=config), \
                 mock.patch.object(runtime, "tmux_enabled", return_value=False), \
                 mock.patch.object(runtime.subprocess, "call", return_value=0) as call:
                self.assertEqual(runtime.host_main(agent, ["--model", "x"]), 0)
            self.assertEqual(call.call_args.args[0], [f"/usr/bin/{expected}", "--model", "x"])

    def test_host_codex_gets_the_same_interactive_workflow_policy(self):
        """Host coordinators share workflow behavior without affecting host batch commands."""
        config = Config()
        config.workflow.enabled = True
        config.workflow.scout.limit = 6
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(runtime.shutil, "which", return_value="/usr/bin/codex"), \
             mock.patch.object(runtime, "resolve_workspace", return_value=Workspace(Path(tmp), Path(tmp))), \
             mock.patch.object(runtime, "launch_config", return_value=config), \
             mock.patch.object(runtime, "tmux_enabled", return_value=False), \
             mock.patch.object(runtime.subprocess, "call", return_value=0) as call:
            self.assertEqual(runtime.host_main("codex", []), 0)
            self.assertEqual(call.call_args.args[0], [
                "/usr/bin/codex", "-c",
                "agents.max_concurrent_threads_per_session=6", "-c",
                "features.multi_agent_v2.max_concurrent_threads_per_session=7", "-c",
                "features.multi_agent_v2.default_wait_timeout_ms=1680000",
            ])

    def test_interactive_host_launch_captures_remote_mode_for_the_new_session(self):
        for agent in ("claude", "codex"):
            for enabled in (False, True):
                config = Config()
                getattr(config.agents, agent).remote_control = enabled
                with self.subTest(agent=agent, enabled=enabled), tempfile.TemporaryDirectory() as tmp, \
                     mock.patch.object(runtime.shutil, "which", return_value=f"/usr/bin/{agent}"), \
                     mock.patch.object(runtime, "resolve_workspace", return_value=Workspace(Path(tmp), Path(tmp))), \
                     mock.patch.object(runtime, "launch_config", return_value=config), \
                     mock.patch.object(runtime, "tmux_enabled", return_value=False), \
                     mock.patch.object(runtime.sys.stdin, "isatty", return_value=True), \
                     mock.patch.object(runtime.sys.stdout, "isatty", return_value=True), \
                     mock.patch.object(runtime.subprocess, "call", return_value=0) as call:
                    runtime.host_main(agent, ["--model", "example"])
                    command = [f"/usr/bin/{agent}", "--model", "example"]
                    if enabled:
                        self.assertEqual(call.call_args.args[0][2:], ["_session", agent, *command])
                    else:
                        self.assertEqual(call.call_args.args[0], command)

    def test_host_session_metadata_wins_over_user_assignments_without_dropping_custom_env(self):
        """The child must read the generated session snapshot, not an env.set substitute."""
        from agent_box.config import effective_config
        config = Config()
        config.workflow.review.max_lines = 47
        config.box.env.set = {'AGENT_BOX_RUNTIME_CONFIG': '/missing/wrong.json',
                          'CUSTOM_ENV': 'retained'}
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(runtime.shutil, 'which', return_value='/usr/bin/codex'), \
             mock.patch.object(runtime, 'resolve_workspace', return_value=Workspace(Path(tmp), Path(tmp))), \
             mock.patch.object(runtime, 'launch_config', return_value=config), \
             mock.patch.object(runtime.subprocess, 'call', return_value=0) as call:
            self.assertEqual(runtime.host_main('codex', []), 0)
            env = call.call_args.kwargs['env']
            self.assertEqual(env['CUSTOM_ENV'], 'retained')
            self.assertTrue(Path(env['AGENT_BOX_RUNTIME_CONFIG']).is_file())
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(effective_config(include_project=False).workflow.review.max_lines, 47)


    def test_host_claude_selects_host_claude_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp), Path(tmp))
            with mock.patch.object(runtime.shutil, "which", return_value="/usr/local/bin/claude"), \
                 mock.patch.object(runtime, "resolve_workspace", return_value=workspace), \
                 mock.patch.object(runtime, "load_config", return_value=Config()), \
                 mock.patch.object(runtime.subprocess, "call", return_value=0) as call:
                rc = runtime.host_main("claude", ["--model", "opus"])
            self.assertEqual(rc, 0)
            call.assert_called_once()
            argv = call.call_args.args[0]
            self.assertEqual(argv, ["/usr/local/bin/claude", "--model", "opus"])

    def test_host_claude_never_adds_the_container_permission_bypass(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp), Path(tmp))
            with mock.patch.object(runtime.shutil, "which", return_value="/usr/local/bin/claude"), \
                 mock.patch.object(runtime, "resolve_workspace", return_value=workspace), \
                 mock.patch.object(runtime, "load_config", return_value=Config()), \
                 mock.patch.object(runtime.subprocess, "call", return_value=0) as call:
                runtime.host_main("claude", ["import", "codex"])
            self.assertEqual(
                call.call_args.args[0], ["/usr/local/bin/claude", "import", "codex"]
            )

    def test_host_codex_selects_host_codex_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp), Path(tmp))
            with mock.patch.object(runtime.shutil, "which", return_value="/usr/local/bin/codex"), \
                 mock.patch.object(runtime, "resolve_workspace", return_value=workspace), \
                 mock.patch.object(runtime, "load_config", return_value=Config()), \
                 mock.patch.object(runtime.subprocess, "call", return_value=0) as call:
                runtime.host_main("codex", ["exec", "hello"])
            argv = call.call_args.args[0]
            self.assertEqual(argv, ["/usr/local/bin/codex", "exec", "hello"])

    def test_passthrough_args_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp), Path(tmp))
            with mock.patch.object(runtime.shutil, "which", return_value="/usr/local/bin/claude"), \
                 mock.patch.object(runtime, "resolve_workspace", return_value=workspace), \
                 mock.patch.object(runtime, "load_config", return_value=Config()), \
                 mock.patch.object(runtime.subprocess, "call", return_value=0) as call:
                runtime.host_main("claude", ["-p", "do a thing", "--model", "opus"])
            self.assertEqual(
                call.call_args.args[0],
                ["/usr/local/bin/claude", "-p", "do a thing", "--model", "opus"],
            )

    def test_podman_never_invoked_in_host_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp), Path(tmp))
            with mock.patch.object(runtime.shutil, "which", return_value="/usr/local/bin/claude"), \
                 mock.patch.object(runtime, "resolve_workspace", return_value=workspace), \
                 mock.patch.object(runtime, "load_config", return_value=Config()), \
                 mock.patch.object(runtime.subprocess, "call", return_value=0) as call, \
                 mock.patch.object(runtime.os, "execvp") as execvp:
                runtime.host_main("claude", [])
            execvp.assert_not_called()
            for arg in call.call_args.args[0]:
                self.assertNotEqual(arg, "podman")








    def test_real_exit_status_propagated(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp), Path(tmp))
            with mock.patch.object(runtime.shutil, "which", return_value="/usr/local/bin/claude"), \
                 mock.patch.object(runtime, "resolve_workspace", return_value=workspace), \
                 mock.patch.object(runtime, "load_config", return_value=Config()), \
                 mock.patch.object(runtime.subprocess, "call", return_value=7):
                rc = runtime.host_main("claude", [])
            self.assertEqual(rc, 7)

    def test_missing_host_executable_raises(self):
        from agent_box.errors import AgentBoxError
        with (
            mock.patch.object(runtime.shutil, "which", return_value=None),
            self.assertRaises(AgentBoxError),
        ):
            runtime.host_main("claude", [])


class ContainerHostParityTests(IsolatedRuntimeTest):
    """Container and host mode must resolve the same Flow-Next policy through
    the same shared helper, so they cannot drift."""




if __name__ == "__main__":
    unittest.main()
