import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_box import claude_settings, notify, runtime
from agent_box.config import Config, NotificationsConfig
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
            mock.patch.object(runtime, "CLAUDE_HOME", state / "claude"),
            mock.patch.object(runtime, "CODEX_HOME", state / "codex"),
            mock.patch.object(runtime, "XDG_CACHE_HOME", state / "cache"),
            mock.patch.object(runtime, "AGENT_SKILLS_DIR", state / "agents/skills"),
            mock.patch.object(runtime, "MANAGED_SKILLS_SOURCE", state / "shipped-skills"),
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
                self.assertTrue((state_root / "cargo").is_dir())
                self.assertTrue((state_root / "rustup").is_dir())
                self.assertTrue((state_root / "notify").is_dir())


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
            "plugins.computer-use@openai-bundled.mcp_servers.cua_repl.enabled=false",
        ])

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

    def test_notification_and_mcp_overrides_compose(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "alternative-codex-home"
            codex_home.mkdir()
            (codex_home / "config.toml").write_text(
                "[mcp_servers.node_repl]\n"
                'command = "/usr/lib/chatgpt/resources/cua_node/bin/node_repl"\n'
            )
            config = Config(notifications=NotificationsConfig(
                enabled=True, telegram_token="token", telegram_chat_id="chat",
            ))
            with mock.patch.object(runtime, "CODEX_HOME", codex_home):
                command = runtime.codex_command(["exec", "hello"], config)
        self.assertEqual(command, [
            "codex",
            "-c", 'notify=["agent-box","notify","codex"]',
            "-c", "mcp_servers.node_repl.enabled=false",
            "--dangerously-bypass-approvals-and-sandbox",
            "exec", "hello",
        ])



    def test_non_codex_launches_do_not_inspect_codex_mcp_configuration(self):
        workspace = Workspace(Path.cwd(), Path.cwd())

        for agent in ("claude", "shell", "doctor"):
            with self.subTest(agent=agent), \
                 mock.patch.object(runtime.shutil, "which", return_value="/usr/bin/podman"), \
                 mock.patch.object(runtime, "resolve_workspace", return_value=workspace), \
                 mock.patch.object(runtime, "ensure_state_dirs"), \
                 mock.patch.object(runtime, "ensure_refresh_token"), \
                 mock.patch.object(runtime, "load_config", return_value=Config()), \
                 mock.patch.object(runtime, "image_exists", return_value=True), \
                 mock.patch.object(runtime, "sync_managed_skills"), \
                 mock.patch.object(runtime, "claude_skill_links"), \
                 mock.patch.object(runtime, "claude_channel_args", return_value=([], [])), \
                 mock.patch.object(runtime, "launch_in_workspace_container", return_value=0), \
                 mock.patch.object(runtime, "codex_runtime_overrides") as overrides, \
                 self.assertRaises(SystemExit):
                runtime.runtime_main([agent], Path.cwd())
            overrides.assert_not_called()




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
        return Config(notifications=NotificationsConfig(
            enabled=True, telegram_token="token", telegram_chat_id="chat",
        ))

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
                mock.patch.object(runtime.shutil, "which", return_value="/usr/bin/podman"),
                mock.patch.object(runtime, "resolve_workspace", return_value=Workspace(root, root)),
                mock.patch.object(runtime, "ensure_state_dirs"),
                mock.patch.object(runtime, "ensure_refresh_token"),
                mock.patch.object(runtime, "load_config", return_value=self._notifying()),
                mock.patch.object(runtime, "image_exists", return_value=True),
                mock.patch.object(runtime, "claude_channel_args", return_value=([], [])),
                mock.patch.object(runtime, "launch_in_workspace_container", return_value=0),
                mock.patch.object(runtime, "CLAUDE_HOME", root / "claude"),
                self.assertRaises(SystemExit),
            ):
                runtime.runtime_main(["claude"], Path(__file__).resolve().parent.parent)
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
