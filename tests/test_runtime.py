import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_box import claude_settings, notify, runtime
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
            mock.patch.object(runtime, "CLAUDE_HOME", state / "claude"),
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
                 mock.patch.object(runtime, "launch_in_workspace_container", return_value=0), \
                 mock.patch.object(runtime, "codex_runtime_overrides") as overrides, \
                 self.assertRaises(SystemExit):
                runtime.runtime_main([agent], Path.cwd())
            overrides.assert_not_called()


class ContainerFlowNextEnvTests(IsolatedRuntimeTest):
    def test_flow_next_workspace_injects_review_backend_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".flow").mkdir()
            (root / ".flow" / "config.json").write_text("{}")
            workspace = Workspace(root, root)

            captured = {}

            def fake_launch(spec, workspace, command, extra_env, process_env):
                captured["extra_env"] = extra_env
                return 0

            with (
                mock.patch.object(runtime.shutil, "which", return_value="/usr/bin/podman"),
                mock.patch.object(runtime, "resolve_workspace", return_value=workspace),
                mock.patch.object(runtime, "ensure_state_dirs"),
                mock.patch.object(runtime, "ensure_refresh_token"),
                mock.patch.object(runtime, "load_config", return_value=Config()),
                mock.patch.object(runtime, "image_exists", return_value=True),
                mock.patch.object(runtime, "sync_claude_agent_skills"),
                mock.patch.object(runtime, "claude_channel_args", return_value=([], [])),
                mock.patch.object(runtime, "launch_in_workspace_container", side_effect=fake_launch),
                self.assertRaises(SystemExit),
            ):
                runtime.runtime_main(["claude"], Path.cwd())
            self.assertIn("FLOW_REVIEW_BACKEND=codex:gpt-5.6-sol:medium", captured["extra_env"])

    def test_non_flow_next_workspace_injects_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp), Path(tmp))

            captured = {}

            def fake_launch(spec, workspace, command, extra_env, process_env):
                captured["extra_env"] = extra_env
                return 0

            with (
                mock.patch.object(runtime.shutil, "which", return_value="/usr/bin/podman"),
                mock.patch.object(runtime, "resolve_workspace", return_value=workspace),
                mock.patch.object(runtime, "ensure_state_dirs"),
                mock.patch.object(runtime, "ensure_refresh_token"),
                mock.patch.object(runtime, "load_config", return_value=Config()),
                mock.patch.object(runtime, "image_exists", return_value=True),
                mock.patch.object(runtime, "sync_claude_agent_skills"),
                mock.patch.object(runtime, "claude_channel_args", return_value=([], [])),
                mock.patch.object(runtime, "launch_in_workspace_container", side_effect=fake_launch),
                self.assertRaises(SystemExit),
            ):
                runtime.runtime_main(["claude"], Path.cwd())
            self.assertFalse(any(pair.startswith("FLOW_") for pair in captured["extra_env"]))


class ClaudeNotifyHookLifecycleTests(IsolatedRuntimeTest):
    def test_claude_launch_migrates_legacy_notify_hooks_without_duplicates(self):
        """A user who already configured agent-box must not need to rerun config.

        This fails if the normal Claude-launch lifecycle stops reconciling the
        managed hook entries, if it leaves the old executable path behind, or
        if a repeat launch appends another current hook.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
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
                        {"matcher": "permission_prompt", "hooks": [
                            {"type": "command", "command": notify.STALE_NOTIFY_COMMAND},
                        ]},
                        {"matcher": "other", "hooks": [
                            {"type": "command", "command": "my-other-hook"},
                        ]},
                    ],
                    "Stop": [{"hooks": [
                        {"type": "command", "command": notify.STALE_NOTIFY_COMMAND},
                        {"type": "command", "command": "my-stop-hook"},
                    ]}],
                    "StopFailure": [{"hooks": [
                        {"type": "command", "command": notify.STALE_NOTIFY_COMMAND},
                    ]}],
                },
            }))
            config = Config(notifications=NotificationsConfig(
                enabled=True, telegram_token="token", telegram_chat_id="chat",
            ))

            with (
                mock.patch.object(runtime.shutil, "which", return_value="/usr/bin/podman"),
                mock.patch.object(runtime, "resolve_workspace", return_value=Workspace(root, root)),
                mock.patch.object(runtime, "ensure_state_dirs"),
                mock.patch.object(runtime, "ensure_refresh_token"),
                mock.patch.object(runtime, "load_config", return_value=config),
                mock.patch.object(runtime, "image_exists", return_value=True),
                mock.patch.object(runtime, "sync_claude_agent_skills"),
                mock.patch.object(runtime, "claude_channel_args", return_value=([], [])),
                mock.patch.object(runtime, "launch_in_workspace_container", return_value=0),
                mock.patch.object(runtime, "CLAUDE_HOME", claude_home),
            ):
                with mock.patch.object(
                    claude_settings, "atomic_write_json", wraps=claude_settings.atomic_write_json,
                ) as write_json:
                    with self.assertRaises(SystemExit):
                        runtime.runtime_main(["claude"], Path(__file__).resolve().parent.parent)
                    self.assertEqual(write_json.call_count, 1, "migration must write settings once")
                    write_json.reset_mock()
                    with self.assertRaises(SystemExit):
                        runtime.runtime_main(["claude"], Path(__file__).resolve().parent.parent)
                    write_json.assert_not_called()

            settings = json.loads(settings_path.read_text())
            self.assertEqual(settings["model"], "opus")
            self.assertEqual(
                settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"], "my-user-hook")
            other_notification = next(
                entry for entry in settings["hooks"]["Notification"]
                if entry.get("matcher") == "other")
            self.assertEqual(
                other_notification["hooks"][0]["command"], "my-other-hook")
            self.assertEqual(
                settings["hooks"]["Stop"][0]["hooks"][0]["command"], "my-stop-hook")

            managed_counts = {event: 0 for event in ("Notification", "Stop", "StopFailure")}
            stale_count = 0
            for event, entries in settings["hooks"].items():
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    for handler in entry.get("hooks", []):
                        if handler.get("command") == notify.NOTIFY_COMMAND:
                            managed_counts[event] = managed_counts.get(event, 0) + 1
                        if handler.get("command") == notify.STALE_NOTIFY_COMMAND:
                            stale_count += 1
            self.assertEqual(stale_count, 0)
            self.assertEqual(managed_counts, {"Notification": 2, "Stop": 1, "StopFailure": 1})

    def test_host_claude_launch_migrates_legacy_notify_hook(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude_home = root / "claude"
            claude_home.mkdir()
            settings_path = claude_home / "settings.json"
            settings_path.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [
                {"type": "command", "command": notify.STALE_NOTIFY_COMMAND},
                {"type": "command", "command": "my-stop-hook"},
            ]}]}}))
            config = Config(notifications=NotificationsConfig(
                enabled=True, telegram_token="token", telegram_chat_id="chat",
            ))
            with (
                mock.patch.object(runtime.shutil, "which", return_value="/usr/bin/claude"),
                mock.patch.object(runtime, "resolve_workspace", return_value=Workspace(root, root)),
                mock.patch.object(runtime, "load_config", return_value=config),
                mock.patch.object(runtime, "CLAUDE_HOME", claude_home),
                mock.patch.object(runtime.subprocess, "call", return_value=0),
            ):
                self.assertEqual(runtime.host_main("claude", []), 0)

            settings = json.loads(settings_path.read_text())
            commands = [
                handler["command"]
                for entry in settings["hooks"]["Stop"]
                for handler in entry["hooks"]
            ]
            self.assertNotIn(notify.STALE_NOTIFY_COMMAND, commands)
            self.assertIn(notify.NOTIFY_COMMAND, commands)
            self.assertIn("my-stop-hook", commands)


class HostModeTests(IsolatedRuntimeTest):
    def _flow_next_workspace(self, tmp: str) -> Path:
        root = Path(tmp) / "project"
        root.mkdir()
        (root / ".flow").mkdir()
        (root / ".flow" / "config.json").write_text("{}")
        return root

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

    def test_flow_next_env_injected_in_host_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._flow_next_workspace(tmp)
            workspace = Workspace(root, root)
            with mock.patch.object(runtime.shutil, "which", return_value="/usr/local/bin/claude"), \
                 mock.patch.object(runtime, "resolve_workspace", return_value=workspace), \
                 mock.patch.object(runtime, "load_config", return_value=Config()), \
                 mock.patch.object(runtime.subprocess, "call", return_value=0) as call:
                runtime.host_main("claude", [])
            env = call.call_args.kwargs["env"]
            self.assertEqual(env.get("FLOW_REVIEW_BACKEND"), "codex:gpt-5.6-sol:medium")

    def test_no_flow_next_env_for_non_flow_next_host_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp), Path(tmp))
            with mock.patch.object(runtime.shutil, "which", return_value="/usr/local/bin/claude"), \
                 mock.patch.object(runtime, "resolve_workspace", return_value=workspace), \
                 mock.patch.object(runtime, "load_config", return_value=Config()), \
                 mock.patch.object(runtime.subprocess, "call", return_value=0) as call:
                runtime.host_main("claude", [])
            env = call.call_args.kwargs["env"]
            self.assertNotIn("FLOW_REVIEW_BACKEND", env)

    def test_flow_next_workspace_overrides_stale_host_env(self):
        """A pre-existing (wrong) host FLOW_REVIEW_BACKEND must not survive:
        agent-box's configured policy is authoritative for this launch."""
        with tempfile.TemporaryDirectory() as tmp:
            root = self._flow_next_workspace(tmp)
            workspace = Workspace(root, root)
            with mock.patch.dict(os.environ, {"FLOW_REVIEW_BACKEND": "stale-wrong-value"}), \
                 mock.patch.object(runtime.shutil, "which", return_value="/usr/local/bin/claude"), \
                 mock.patch.object(runtime, "resolve_workspace", return_value=workspace), \
                 mock.patch.object(runtime, "load_config", return_value=Config()), \
                 mock.patch.object(runtime.subprocess, "call", return_value=0) as call:
                runtime.host_main("claude", [])
            env = call.call_args.kwargs["env"]
            self.assertEqual(env.get("FLOW_REVIEW_BACKEND"), "codex:gpt-5.6-sol:medium")

    def test_non_flow_next_workspace_strips_stale_host_env(self):
        """A non-Flow-Next launch must not merely fail to *add* the policy
        env -- it must actively strip whatever the parent shell exported,
        or a stale value would silently leak through untouched."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Workspace(Path(tmp), Path(tmp))
            with mock.patch.dict(os.environ, {
                "FLOW_REVIEW_BACKEND": "stale-wrong-value",
                "AGENT_BOX_FLOW_NEXT_AUTO_PUSH": "1",
            }), \
                 mock.patch.object(runtime.shutil, "which", return_value="/usr/local/bin/claude"), \
                 mock.patch.object(runtime, "resolve_workspace", return_value=workspace), \
                 mock.patch.object(runtime, "load_config", return_value=Config()), \
                 mock.patch.object(runtime.subprocess, "call", return_value=0) as call:
                runtime.host_main("claude", [])
            env = call.call_args.kwargs["env"]
            self.assertNotIn("FLOW_REVIEW_BACKEND", env)
            self.assertNotIn("AGENT_BOX_FLOW_NEXT_AUTO_PUSH", env)

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

    def test_container_and_host_inject_same_flow_next_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".flow").mkdir()
            (root / ".flow" / "config.json").write_text("{}")
            config = Config()
            from agent_box.flow_next import flow_next_launch_env
            container_env = flow_next_launch_env(root, "claude", config)
            with mock.patch.object(runtime.shutil, "which", return_value="/usr/local/bin/claude"), \
                 mock.patch.object(runtime, "resolve_workspace", return_value=Workspace(root, root)), \
                 mock.patch.object(runtime, "load_config", return_value=config), \
                 mock.patch.object(runtime.subprocess, "call", return_value=0) as call:
                runtime.host_main("claude", [])
            host_env = call.call_args.kwargs["env"]
            for pair in container_env:
                name, _, value = pair.partition("=")
                self.assertEqual(host_env.get(name), value)


if __name__ == "__main__":
    unittest.main()
