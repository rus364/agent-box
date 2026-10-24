"""Container convenience must not silently disable either provider's permissions."""
import os
import tempfile
from pathlib import Path
from unittest import mock

from agent_box import config as config_module, remote_control, runtime
from agent_box.config import Config, effective_config, load_config, save_config
from agent_box.workspace import Workspace
from test_runtime import IsolatedRuntimeTest


FLAGS = {
    "codex": "--dangerously-bypass-approvals-and-sandbox",
    "claude": "--dangerously-skip-permissions",
}


class PermissionModesTests(IsolatedRuntimeTest):
    def test_missing_settings_never_grant_bypass_and_opt_ins_round_trip(self):
        """Each provider needs an independent opt-in, including in minimal current config."""
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            path.write_text('{"version":' + str(config_module.VERSION) + '}')
            original = path.read_bytes()
            saved = load_config(path)
            for agent in FLAGS:
                self.assertFalse(getattr(saved.agents, agent).bypass_permissions)
            self.assertEqual(path.read_bytes(), original)
            for selected in FLAGS:
                config = Config()
                getattr(config.agents, selected).bypass_permissions = True
                save_config(config, path)
                loaded = load_config(path)
                for agent in FLAGS:
                    self.assertEqual(getattr(loaded.agents, agent).bypass_permissions, agent == selected)
                self.assertEqual(loaded.to_dict(), config.to_dict())

    def test_session_snapshot_keeps_both_permission_choices(self):
        """Nested launches must inherit their session choice instead of saved host settings."""
        with tempfile.TemporaryDirectory() as temp:
            for enabled in (False, True):
                config = Config()
                for agent in FLAGS:
                    getattr(config.agents, agent).bypass_permissions = enabled
                runtime.sync_tracker_runtime(config, Path(temp), policy=True)
                snapshot = Path(temp) / "config.json"
                with mock.patch.dict(os.environ, {"AGENT_BOX_RUNTIME_CONFIG": str(snapshot)}), \
                     mock.patch.object(config_module, "load_config", side_effect=AssertionError("host read")):
                    loaded = effective_config(include_project=False)
                for agent in FLAGS:
                    self.assertEqual(getattr(loaded.agents, agent).bypass_permissions, enabled)

    def container_command(self, agent, config, args=(), *, external=False):
        workspace = Workspace(Path.cwd(), Path.cwd())
        with mock.patch.object(runtime.host_podman, "require"), \
             mock.patch.object(runtime, "resolve_workspace", return_value=workspace), \
             mock.patch.object(runtime, "launch_config", return_value=config), \
             mock.patch.object(runtime, "ensure_workspace_image", return_value="sha256:fixture"), \
             mock.patch.object(runtime, "tmux_enabled", return_value=False), \
             mock.patch.object(runtime, "claude_channel_args", return_value=([], [])), \
             mock.patch.object(runtime, "codex_runtime_overrides", return_value=[]), \
             mock.patch.object(runtime.sys.stdin, "isatty", return_value=True), \
             mock.patch.object(runtime.sys.stdout, "isatty", return_value=True), \
             mock.patch("agent_box.external_launch.selected_model"), \
             mock.patch.object(runtime, "external_command", side_effect=lambda command, *a, **kw: ["external", *command]), \
             mock.patch.object(runtime, "launch_in_workspace_container", return_value=0) as launch, \
             self.assertRaises(SystemExit) as result:
            runtime.runtime_main([agent, *args], runtime.PACKAGE_ROOT, api_provider="fixture" if external else "")
        self.assertEqual(result.exception.code, 0)
        return launch.call_args.args[2]

    def test_container_permission_mode_applies_before_ordinary_remote_and_external_routing(self):
        """RC and external providers must not silently restore the unconditional bypass."""
        for agent, flag in FLAGS.items():
            for enabled in (False, True):
                for route in ("ordinary", "remote", "external"):
                    with self.subTest(agent=agent, enabled=enabled, route=route):
                        config = Config()
                        getattr(config.agents, agent).bypass_permissions = enabled
                        getattr(config.agents, agent).remote_control = route == "remote"
                        command = self.container_command(agent, config, external=route == "external")
                        self.assertEqual(flag in command, enabled)
                        self.assertEqual("_session" in command, route == "remote")
                        self.assertEqual(command[0] == "external", route == "external")

    def test_disabled_mode_preserves_explicit_cli_permissions_and_resume_arguments(self):
        """Off removes only agent-box's override, never the user's own permission request."""
        for agent, flag in FLAGS.items():
            args = [flag, *(('resume', 'fixture') if agent == 'codex' else ('--resume', 'fixture'))]
            with self.subTest(agent=agent):
                command = self.container_command(agent, Config(), args)
                self.assertEqual(command, [agent, *args])

    def test_codex_remote_new_threads_have_no_injected_permission_override_when_off(self):
        """The default RC TUI must be eligible for upstream resume's no-override path."""
        for enabled in (False, True):
            config = Config()
            config.agents.codex.bypass_permissions = enabled
            with mock.patch.object(runtime, "codex_runtime_overrides", return_value=[]):
                server, client, _ = remote_control.codex_commands(runtime.codex_command([], config))
            self.assertEqual(FLAGS['codex'] in client, enabled)
            self.assertEqual('approval_policy="never"' in server, enabled)
            self.assertEqual('sandbox_mode="danger-full-access"' in server, enabled)
            if not enabled:
                self.assertEqual(server, ['codex', 'app-server'])

    def test_host_does_not_apply_container_opt_ins_or_drop_explicit_user_flags(self):
        """Enabling container convenience must not grant the same rights on the host."""
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(Path(temp), Path(temp))
            for agent, flag in FLAGS.items():
                config = Config()
                getattr(config.agents, agent).bypass_permissions = True
                for args in ([], [flag]):
                    with self.subTest(agent=agent, args=args), \
                         mock.patch.object(runtime.shutil, "which", return_value=f"/usr/bin/{agent}"), \
                         mock.patch.object(runtime, "resolve_workspace", return_value=workspace), \
                         mock.patch.object(runtime, "launch_config", return_value=config), \
                         mock.patch.object(runtime, "tmux_enabled", return_value=False), \
                         mock.patch.object(runtime.subprocess, "call", return_value=0) as call:
                        runtime.host_main(agent, args)
                        command = call.call_args.args[0]
                        if command[1:2] == [str(runtime.HOST_SESSION_RUNNER)]:
                            command = command[3:]
                        self.assertEqual(command, [f"/usr/bin/{agent}", *args])
