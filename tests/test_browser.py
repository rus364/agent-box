"""The opt-in container-native browser: Playwright MCP + headless Chromium.

Three things these tests exist to pin down, in order of how much they would
hurt to get wrong:

* **Off is really off.** A default install must add no MCP server, no argv and
  no state directory for either agent.
* **Enabled is invocation-scoped.** Neither the user's ``~/.codex/config.toml``
  nor their Claude configuration may be rewritten to carry a browser server,
  and the box's structural identity must not change -- flipping the switch
  cannot be allowed to destroy an otherwise compatible running container.
* **Sessions do not share writable browser state.** Two concurrent
  Claude/Codex launches in one workspace get separate profiles and separate
  output directories, and neither writes into the mounted project.
"""
import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_box import cli as cli_module
from agent_box import runtime
from agent_box.config import BrowserConfig, Config, NotificationsConfig
from agent_box.workspace import Workspace

from .test_runtime import IsolatedRuntimeTest


def browser_config(enabled: bool = True) -> Config:
    return Config(browser=BrowserConfig(enabled=enabled))


class BrowserSessionStateTests(IsolatedRuntimeTest):
    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "project"
        self.root.mkdir()
        self.workspace = Workspace(self.root, self.root)
        self.runtime_state = runtime.workspace_runtime_state(self.workspace)

    def _session(self, config: Config, agent: str = "claude", container=None):
        with mock.patch.object(runtime, "inspect_workspace_container", return_value=container):
            return runtime.browser_session(config, self.workspace, self.runtime_state, agent)

    def test_disabled_creates_no_state_at_all(self):
        self.assertIsNone(self._session(browser_config(False)))
        self.assertFalse(runtime.browser_session_root(self.runtime_state).exists())

    def test_enabled_creates_a_session_private_output_directory(self):
        session_dir = self._session(browser_config())
        self.assertIsNotNone(session_dir)
        self.assertTrue((session_dir / "output").is_dir())
        # Under the workspace's own runtime state, never in the project.
        self.assertTrue(session_dir.is_relative_to(self.runtime_state.root))
        self.assertFalse(session_dir.is_relative_to(self.root))

    def test_shell_and_doctor_get_no_browser_session(self):
        for agent in ("shell", "doctor"):
            with self.subTest(agent=agent):
                self.assertIsNone(self._session(browser_config(), agent))

    def test_concurrent_sessions_get_independent_directories(self):
        """Two launches in one workspace must not share a writable profile."""
        with mock.patch.object(runtime.os, "getpid", return_value=101):
            first = self._session(browser_config(), "claude")
        # The second launch finds the box the first one brought up.
        running = {"State": {"Status": "running"}}
        with mock.patch.object(runtime.os, "getpid", return_value=202):
            second = self._session(browser_config(), "codex", container=running)
        self.assertNotEqual(first, second)
        self.assertTrue((first / "output").is_dir())
        self.assertTrue((second / "output").is_dir())

    def test_a_launch_racing_another_startup_keeps_its_own_directory(self):
        """Both launches can legitimately see "no box running" while the first
        is still starting up; neither may delete the other's fresh state."""
        with mock.patch.object(runtime.os, "getpid", return_value=101):
            first = self._session(browser_config(), "claude", container=None)
        with mock.patch.object(runtime.os, "getpid", return_value=202):
            second = self._session(browser_config(), "codex", container=None)
        self.assertTrue((first / "output").is_dir())
        self.assertTrue((second / "output").is_dir())

    def test_state_is_pruned_only_when_no_box_is_running(self):
        stale = runtime.browser_session_root(self.runtime_state) / "claude-1"
        (stale / "output").mkdir(parents=True)
        aged = time.time() - runtime.BROWSER_SESSION_GRACE_SECONDS - 60
        os.utime(stale, (aged, aged))

        # A running box may hold a detached session using that state.
        self._session(browser_config(), container={"State": {"Status": "running"}})
        self.assertTrue(stale.is_dir())

        # No box means no session of this workspace can still be alive.
        self._session(browser_config(), container=None)
        self.assertFalse(stale.exists())

    def test_stop_removes_every_browser_session(self):
        for name in ("claude-1", "codex-2"):
            (runtime.browser_session_root(self.runtime_state) / name / "output").mkdir(parents=True)
        runtime.remove_browser_sessions(self.runtime_state)
        self.assertFalse(runtime.browser_session_root(self.runtime_state).exists())

    def test_unusable_state_warns_and_degrades_to_no_browser(self):
        with mock.patch.object(runtime, "inspect_workspace_container", return_value=None), \
             mock.patch.object(Path, "mkdir", side_effect=OSError("read-only")), \
             mock.patch.object(runtime, "warn") as warn:
            session_dir = runtime.browser_session(
                browser_config(), self.workspace, self.runtime_state, "claude")
        self.assertIsNone(session_dir)
        warn.assert_called_once()
        self.assertIn("browser integration is on", warn.call_args.args[0])


class BrowserMcpArgvTests(unittest.TestCase):
    def test_headless_isolated_and_redirected_output(self):
        args = runtime.browser_mcp_args(Path("/state/browser/claude-1"))
        self.assertEqual(args[0], "playwright-mcp")
        self.assertIn("--headless", args)
        # --isolated is what makes cookies/localStorage non-persistent.
        self.assertIn("--isolated", args)
        self.assertEqual(
            args[args.index("--output-dir") + 1], "/state/browser/claude-1/output")

    def test_bundled_chromium_is_selected_explicitly(self):
        """Playwright MCP defaults to the branded Chrome channel, which the
        image does not ship; the bundled Chromium must be named."""
        args = runtime.browser_mcp_args(Path("/state/browser/claude-1"))
        self.assertEqual(args[args.index("--browser") + 1], "chromium")

    def test_no_sandbox_flag_is_forced_either_way(self):
        """Playwright picks its own default for its bundled Chromium; agent-box
        neither weakens it nor forces on a sandbox that cannot work under
        --cap-drop=all."""
        args = runtime.browser_mcp_args(Path("/state/b"))
        self.assertNotIn("--no-sandbox", args)
        self.assertNotIn("--sandbox", args)

    def test_no_persistent_profile_directory_is_configured(self):
        args = runtime.browser_mcp_args(Path("/state/b"))
        self.assertNotIn("--user-data-dir", args)
        self.assertNotIn("--storage-state", args)


class CodexBrowserInjectionTests(IsolatedRuntimeTest):
    def setUp(self):
        super().setUp()
        # An empty Codex home, so a real ~/.codex/config.toml on the machine
        # running the tests cannot add overrides of its own.
        self._codex_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._codex_tmp.cleanup)
        patch = mock.patch.object(runtime, "CODEX_HOME", Path(self._codex_tmp.name))
        patch.start()
        self.addCleanup(patch.stop)

    def test_disabled_adds_no_mcp_configuration(self):
        command = runtime.codex_command(["exec", "hi"], browser_config(False), False, None)
        self.assertEqual(command, [
            "codex", "--dangerously-bypass-approvals-and-sandbox", "exec", "hi"])

    def test_enabled_registers_the_agent_box_browser_server(self):
        command = runtime.codex_command(
            ["exec", "hi"], browser_config(), False, Path("/state/browser/codex-7"))
        override = command[command.index("-c") + 1]
        self.assertTrue(override.startswith("mcp_servers.agent_box_browser={"))
        self.assertIn('command="playwright-mcp"', override)
        self.assertIn('"--headless"', override)
        self.assertIn('"--isolated"', override)
        self.assertIn('"/state/browser/codex-7/output"', override)

    def test_override_is_valid_toml_naming_a_stdio_server(self):
        import tomllib

        override = runtime.browser_codex_overrides(Path("/state/browser/codex-7"))[0]
        key, _, value = override.partition("=")
        parsed = tomllib.loads(f"x = {value}")["x"]
        self.assertEqual(key, "mcp_servers.agent_box_browser")
        self.assertEqual(parsed["command"], "playwright-mcp")
        self.assertEqual(parsed["args"], runtime.browser_mcp_args(Path("/state/browser/codex-7"))[1:])

    def test_composes_with_chatgpt_desktop_mcp_filtering(self):
        """Both kinds of override reach Codex through the same ephemeral -c
        mechanism; enabling the browser must not stop the desktop-only servers
        from being disabled."""
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex"
            codex_home.mkdir()
            config_path = codex_home / "config.toml"
            config_path.write_text(
                "[mcp_servers.node_repl]\n"
                'command = "/usr/lib/chatgpt/resources/cua_node/bin/node_repl"\n'
            )
            before = config_path.read_bytes()
            with mock.patch.object(runtime, "CODEX_HOME", codex_home):
                command = runtime.codex_command(
                    ["exec", "hi"], browser_config(), False, Path("/state/browser/codex-7"))
            self.assertEqual(before, config_path.read_bytes(),
                             "the host Codex config must never be rewritten")
        overrides = [command[i + 1] for i, arg in enumerate(command) if arg == "-c"]
        self.assertIn("mcp_servers.node_repl.enabled=false", overrides)
        self.assertTrue(any(o.startswith("mcp_servers.agent_box_browser=") for o in overrides))

    def test_composes_with_notifications(self):
        config = Config(
            notifications=NotificationsConfig(
                enabled=True, telegram_token="t", telegram_chat_id="c"),
            browser=BrowserConfig(enabled=True),
        )
        command = runtime.codex_command(["exec", "hi"], config, False, Path("/state/b/codex-1"))
        overrides = [command[i + 1] for i, arg in enumerate(command) if arg == "-c"]
        self.assertIn('notify=["agent-box","notify","codex"]', overrides)
        self.assertTrue(any(o.startswith("mcp_servers.agent_box_browser=") for o in overrides))


class ClaudeBrowserInjectionTests(unittest.TestCase):
    def test_disabled_adds_no_arguments(self):
        self.assertEqual(runtime.browser_claude_args(None), [])
        self.assertEqual(runtime.claude_session_args(["hi"], [], []), ["hi"])

    def test_enabled_writes_an_invocation_scoped_mcp_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_dir = Path(tmp)
            args = runtime.browser_claude_args(session_dir)
            self.assertEqual(args[0], "--mcp-config")
            written = json.loads(Path(args[1]).read_text())
        server = written["mcpServers"]["agent_box_browser"]
        self.assertEqual(server["command"], "playwright-mcp")
        self.assertIn("--headless", server["args"])
        self.assertIn("--isolated", server["args"])

    def test_config_file_lives_in_the_workspace_runtime_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_dir = Path(tmp) / "browser" / "claude-1"
            session_dir.mkdir(parents=True)
            path = Path(runtime.browser_claude_args(session_dir)[1])
        self.assertEqual(path.parent, session_dir)

    def test_strict_mcp_config_is_never_passed(self):
        """Without it Claude merges these servers with the user's own; with it
        the user's whole MCP configuration would be ignored."""
        with tempfile.TemporaryDirectory() as tmp:
            self.assertNotIn("--strict-mcp-config", runtime.browser_claude_args(Path(tmp)))

    def test_mcp_config_lands_after_a_positional_prompt(self):
        """--mcp-config is variadic: placed before a prompt it would swallow
        it and try to load the prompt as a second config file."""
        argv = runtime.claude_session_args(
            ["--print", "explain this"], [], ["--mcp-config", "/state/mcp.json"])
        self.assertEqual(argv, ["--print", "explain this", "--mcp-config", "/state/mcp.json"])

    def test_channel_and_browser_options_compose(self):
        argv = runtime.claude_session_args(
            ["hi"], ["--channels", "plugin:telegram@x"], ["--mcp-config", "/state/mcp.json"])
        self.assertEqual(argv, [
            "hi", "--channels", "plugin:telegram@x", "--mcp-config", "/state/mcp.json"])
        # The channel option is variadic too, and stops at the next option.
        self.assertLess(argv.index("--channels"), argv.index("--mcp-config"))

    def test_both_are_inserted_before_an_explicit_separator(self):
        argv = runtime.claude_session_args(
            ["--model", "opus", "--", "a prompt --with-dashes"],
            ["--channels", "plugin:telegram@x"], ["--mcp-config", "/state/mcp.json"])
        self.assertEqual(argv, [
            "--model", "opus", "--channels", "plugin:telegram@x",
            "--mcp-config", "/state/mcp.json", "--", "a prompt --with-dashes"])


class BrowserDoesNotChangeBoxIdentityTests(IsolatedRuntimeTest):
    """Enablement is invocation-level, so an otherwise compatible running box
    must not be destroyed and recreated just because the switch flipped."""

    def test_structural_identity_is_unchanged_by_the_browser_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            root.mkdir()
            workspace = Workspace(root, root)
            off = runtime.workspace_container_spec(workspace, [], Path.cwd(), browser_config(False))
            on = runtime.workspace_container_spec(workspace, [], Path.cwd(), browser_config(True))
        self.assertEqual(off.base_config_hash, on.base_config_hash)
        self.assertEqual(off.name, on.name)
        self.assertEqual(off.create_argv, on.create_argv)

    def test_no_browser_specific_mount_or_env_is_added(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            root.mkdir()
            workspace = Workspace(root, root)
            args = runtime.container_lifetime_args(
                Workspace(root, root), [], Path.cwd(), browser_config(True))
        joined = " ".join(args)
        self.assertNotIn("playwright", joined.lower())
        self.assertNotIn("chromium", joined.lower())
        # No X11/Wayland/socket passthrough, and no extra capability.
        self.assertNotIn("/tmp/.X11-unix", joined)
        self.assertNotIn("--cap-add", joined)
        self.assertIn("--cap-drop=all", args)
        self.assertIn("--security-opt=no-new-privileges", args)


class HostModeTests(IsolatedRuntimeTest):
    """`--host` runs the user's own installed CLI. The browser lives in the
    image, so there is nothing to give it -- and host agent configuration is
    never rewritten to fake one."""

    def test_host_launch_gets_no_browser_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            root.mkdir()
            captured = {}

            def fake_call(argv, **kwargs):
                captured["argv"] = argv
                return 0

            for agent in ("claude", "codex"):
                with self.subTest(agent=agent), \
                     mock.patch.object(runtime.shutil, "which", return_value=f"/usr/bin/{agent}"), \
                     mock.patch.object(
                         runtime, "resolve_workspace", return_value=Workspace(root, root)), \
                     mock.patch.object(runtime, "load_config", return_value=browser_config(True)), \
                     mock.patch.object(runtime.claude_settings, "reconcile_claude_settings"), \
                     mock.patch.object(runtime.subprocess, "call", side_effect=fake_call):
                    runtime.host_main(agent, ["--version"])
                joined = " ".join(captured["argv"])
                self.assertNotIn("--mcp-config", joined)
                self.assertNotIn("agent_box_browser", joined)
                self.assertNotIn("playwright", joined)

    def test_host_mode_writes_no_browser_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            root.mkdir()
            workspace = Workspace(root, root)
            with mock.patch.object(runtime.shutil, "which", return_value="/usr/bin/codex"), \
                 mock.patch.object(runtime, "resolve_workspace", return_value=workspace), \
                 mock.patch.object(runtime, "load_config", return_value=browser_config(True)), \
                 mock.patch.object(runtime.subprocess, "call", return_value=0):
                runtime.host_main("codex", ["--version"])
            state = runtime.browser_session_root(runtime.workspace_runtime_state(workspace))
            self.assertFalse(state.exists())


class DoctorBrowserDiagnosticsTests(unittest.TestCase):
    def test_disabled_is_reported_without_being_an_error(self):
        script = runtime.DOCTOR_SCRIPT
        self.assertIn("browser integration: disabled", script)
        self.assertIn("browser integration: enabled", script)

    def test_reports_the_playwright_mcp_version_either_way(self):
        self.assertIn("playwright-mcp --version", runtime.DOCTOR_SCRIPT)

    def test_smoke_test_is_bounded_and_offline(self):
        script = runtime.DOCTOR_SCRIPT
        self.assertIn("timeout 30 agent-box-browser-smoke", script)
        # Nothing in the browser section may reach for a website.
        section = script[script.index("playwright-mcp --version"):]
        self.assertNotIn("http://", section)
        self.assertNotIn("https://", section)

    def test_an_image_without_browser_support_says_so_actionably(self):
        self.assertIn("agent-box build", runtime.DOCTOR_SCRIPT)

    def test_enablement_reaches_the_container_as_one_env_flag(self):
        self.assertEqual(runtime.BROWSER_ENABLED_ENV, "AGENT_BOX_BROWSER")
        self.assertIn("${AGENT_BOX_BROWSER:-0}", runtime.DOCTOR_SCRIPT)


class DoctorLaunchTests(IsolatedRuntimeTest):
    def _doctor_env(self, config: Config) -> list[str]:
        captured = {}

        def fake_launch(spec, workspace, command, extra_env, process_env=None):
            captured["env"] = extra_env
            return 0

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            root.mkdir()
            with mock.patch.object(runtime.shutil, "which", return_value="/usr/bin/podman"), \
                 mock.patch.object(runtime, "resolve_workspace", return_value=Workspace(root, root)), \
                 mock.patch.object(runtime, "load_config", return_value=config), \
                 mock.patch.object(runtime, "image_exists", return_value=True), \
                 mock.patch.object(runtime, "sync_claude_agent_skills"), \
                 mock.patch.object(runtime, "workspace_container_spec"), \
                 mock.patch.object(runtime, "launch_in_workspace_container", side_effect=fake_launch), \
                 self.assertRaises(SystemExit):
                runtime.runtime_main(["doctor"], Path.cwd())
        return captured["env"]

    def test_doctor_is_told_the_browser_is_off(self):
        self.assertIn("AGENT_BOX_BROWSER=0", self._doctor_env(browser_config(False)))

    def test_doctor_is_told_the_browser_is_on(self):
        self.assertIn("AGENT_BOX_BROWSER=1", self._doctor_env(browser_config(True)))


class ConfigSurfaceTests(unittest.TestCase):
    """`agent-box config` has to show the switch and say what it means: this
    is the only place a user learns that the browser lives inside the same
    trust boundary as their project."""

    def _show(self, config: Config) -> str:
        buffer = io.StringIO()
        with mock.patch.object(cli_module, "load_config", return_value=config), \
             contextlib.redirect_stdout(buffer):
            cli_module.config_show()
        return buffer.getvalue()

    def test_config_show_reports_the_switch_off(self):
        line = next(l for l in self._show(browser_config(False)).splitlines() if "browser" in l.lower())
        self.assertRegex(line, r"Agent browser \(containers\)\s+off")

    def test_config_show_reports_the_switch_on_and_where_it_runs(self):
        line = next(l for l in self._show(browser_config(True)).splitlines() if "browser" in l.lower())
        self.assertRegex(line, r"Agent browser \(containers\)\s+on")
        self.assertIn("headless Chromium in the box", line)
        self.assertIn("--host", line)

    @contextlib.contextmanager
    def _isolated_config_ui(self, config: Config, answer=None):
        """Run the interactive UI without touching anything the user owns.

        Every derived artifact it writes -- the credential file, Claude's
        settings.json, the Flow-Next hooks -- belongs to the real host, so all
        of them are stubbed; only the Config handed to save_config matters
        here.
        """
        from agent_box import flow_push

        answered = (lambda _label, current: current) if answer is None else (
            lambda _label, _current: answer)
        with mock.patch.object(cli_module, "load_config", return_value=config), \
             mock.patch.object(cli_module, "save_config") as save, \
             mock.patch.object(cli_module, "_prompt_bool", side_effect=answered), \
             mock.patch.object(cli_module, "_prompt_secret", side_effect=lambda _l, current: current), \
             mock.patch.object(cli_module, "_prompt_text", side_effect=lambda _l, current: current), \
             mock.patch.object(cli_module.channel_module, "missing_requirements", return_value=[]), \
             mock.patch.object(cli_module.notify_module, "sync_credentials"), \
             mock.patch.object(cli_module.claude_settings, "reconcile_claude_settings"), \
             mock.patch.object(flow_push, "install_flow_push_hooks"), \
             mock.patch.object(flow_push, "remove_flow_push_hooks"):
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                cli_module.config_interactive()
            yield save, buffer.getvalue()

    def _interactive(self, config: Config, answer: bool) -> Config:
        with self._isolated_config_ui(config, answer) as (save, _output):
            return save.call_args.args[0]

    def test_interactive_config_can_turn_the_browser_on(self):
        self.assertTrue(self._interactive(browser_config(False), True).browser.enabled)

    def test_interactive_config_can_turn_the_browser_off(self):
        self.assertFalse(self._interactive(browser_config(True), False).browser.enabled)

    def test_pressing_enter_keeps_the_stored_answer(self):
        self.assertFalse(self._interactive(browser_config(False), None).browser.enabled)
        self.assertTrue(self._interactive(browser_config(True), None).browser.enabled)

    def test_interactive_prompt_states_the_trust_boundary(self):
        with self._isolated_config_ui(browser_config(False)) as (_save, text):
            pass
        self.assertIn("headless Chromium", text)
        self.assertIn("isolated, in-memory", text)
        self.assertIn("same side of the", text)
        self.assertIn("--host", text)


if __name__ == "__main__":
    unittest.main()
