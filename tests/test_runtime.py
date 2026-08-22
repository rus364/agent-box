import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_box import runtime
from agent_box.config import Config, NotificationsConfig
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
            mock.patch.object(runtime, "NOTIFY_STATE_ROOT", state / "notify"),
            mock.patch.object(runtime, "NOTIFY_CREDENTIALS", state / "runtime/notify.json"),
        ]
        for patch in self._patches:
            patch.start()
            self.addCleanup(patch.stop)


class BuildMountsEnsuresStateDirsTests(IsolatedRuntimeTest):
    """podman fails a bind mount outright when its source is missing, so
    build_mounts must create the host-side state directories itself rather
    than trusting a caller to have done it."""

    def test_build_mounts_creates_missing_state_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            with mock.patch.object(runtime, "STATE_ROOT", state_root), \
                 mock.patch.object(runtime, "BUILD_STATE", state_root / "build"), \
                 mock.patch.object(runtime, "NOTIFY_STATE_ROOT", state_root / "notify"):
                self.assertFalse(state_root.exists())
                with tempfile.TemporaryDirectory() as project:
                    workspace = Workspace(Path(project), Path(project))
                    runtime.build_mounts(workspace, [], Path(tmp))
                self.assertTrue((state_root / "cache").is_dir())
                self.assertTrue((state_root / "uv").is_dir())
                self.assertTrue((state_root / "mise").is_dir())
                self.assertTrue((state_root / "notify").is_dir())


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
            self.assertNotIn("/workspace", " ".join(argv))

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


class CodexRuntimeOverridesTests(IsolatedRuntimeTest):
    def _overrides_for(self, config_text: str | None) -> tuple[list[str], tuple[bytes, bytes]]:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex-home"
            codex_home.mkdir()
            config_path = codex_home / "config.toml"
            if config_text is not None:
                config_path.write_text(config_text)
            before = config_path.read_bytes() if config_path.exists() else b""
            with mock.patch.object(runtime, "CODEX_HOME", codex_home):
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
        self.assertEqual(no_mcp_overrides, [])

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

        class ExecCalled(Exception):
            pass

        for agent in ("claude", "shell", "doctor"):
            with self.subTest(agent=agent), \
                 mock.patch.object(runtime.shutil, "which", return_value="/usr/bin/podman"), \
                 mock.patch.object(runtime, "resolve_workspace", return_value=workspace), \
                 mock.patch.object(runtime, "ensure_state_dirs"), \
                 mock.patch.object(runtime, "ensure_refresh_token"), \
                 mock.patch.object(runtime, "load_config", return_value=Config()), \
                 mock.patch.object(runtime, "image_exists", return_value=True), \
                 mock.patch.object(runtime, "sync_claude_agent_skills"), \
                 mock.patch.object(runtime, "claude_channel_args", return_value=([], [])), \
                 mock.patch.object(runtime, "container_argv", return_value=["podman"]), \
                 mock.patch.object(runtime, "codex_runtime_overrides") as overrides, \
                 mock.patch.object(runtime.os, "execvp", side_effect=ExecCalled):
                with self.assertRaises(ExecCalled):
                    runtime.runtime_main([agent], Path.cwd())
                overrides.assert_not_called()


if __name__ == "__main__":
    unittest.main()
