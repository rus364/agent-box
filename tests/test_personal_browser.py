"""Personal desktop access must require its own host choice and browser consent."""
import json
from contextlib import ExitStack
import io
from itertools import product
import os
from pathlib import Path
import tempfile
import socket
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from agent_box import browser, paths, personal_browser, project_config, runtime
from agent_box.config import Config, PersonalBrowserConfig
from agent_box.errors import AgentBoxError
from agent_box.launch_options import apply as apply_overrides
from agent_box.launch_options import LaunchOptions, parse
from agent_box.workspace import Workspace
from test_runtime import IsolatedRuntimeTest


def consent_url(**extra):
    query = {"mcpRelayUrl": "ws://127.0.0.1:41234/extension/12345678-1234-1234-1234-123456789abc",
             "client": '{"name":"test"}', "protocolVersion": "2", **extra}
    return f"chrome-extension://{personal_browser.EXTENSION}/connect.html?{urlencode(query)}"


class PersonalBrowserTests(unittest.TestCase):
    def test_old_configuration_stays_off_and_roundtrip_preserves_selection(self):
        """An upgrade cannot convert isolated browsing into access to signed-in sites."""
        old = Config.from_dict({"box": {"browser": {"enabled": True}}})
        self.assertFalse(old.host.personal_browser.enabled)
        old.host.personal_browser = PersonalBrowserConfig(True, "chromium", "Profile 2")
        self.assertEqual(Config.from_dict(old.to_dict()).host.personal_browser, old.host.personal_browser)

    def test_launch_overrides_are_independent_from_isolated_browser(self):
        """Either session override must work without granting the other capability."""
        config = Config()
        apply_overrides(config, {"personal-browser": True})
        self.assertTrue(config.host.personal_browser.enabled)
        self.assertFalse(config.box.browser.active())
        config.box.browser.enabled = True
        apply_overrides(config, {"personal-browser": False})
        self.assertFalse(config.host.personal_browser.enabled)
        self.assertTrue(config.box.browser.active())

    def test_project_allowlist_excludes_personal_browser(self):
        """A repository cannot turn on the owner's authenticated desktop capability."""
        with self.assertRaises(AgentBoxError):
            project_config.validate({"host": {"personal_browser": {"enabled": True}}}, Path("/project/.agent-box.json"))

    def test_opener_accepts_only_official_consent_without_bypass(self):
        """The private opener must not become an arbitrary host URL/command endpoint."""
        self.assertEqual(personal_browser.connection_url(consent_url()), consent_url())
        for url in (consent_url(token="secret"), consent_url(mcpRelayUrl="ws://example.com:80/extension/" + "a"*36),
                    consent_url(mcpRelayUrl="ws://127.0.0.1:42/cdp/" + "a"*36),
                    consent_url().replace("/connect.html", "/status.html"),
                    "file:///etc/passwd", consent_url() + "&client=duplicate"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                personal_browser.connection_url(url)

    def test_remote_and_headless_fail_before_browser_discovery(self):
        """A remote session must never silently connect to a browser on its server."""
        for env in ({}, {"DISPLAY": ":0", "SSH_CONNECTION": "remote"},
                    {"DISPLAY": ":0", "CODER_AGENT_URL": "remote"}):
            with patch.dict(os.environ, env, clear=True), patch.object(runtime, "inside_box", return_value=False), \
                    patch.object(personal_browser.shutil, "which") as which, self.assertRaises(AgentBoxError):
                personal_browser.command(PersonalBrowserConfig(True))
            which.assert_not_called()

    def test_selected_profile_is_one_argument_without_profile_reading(self):
        """Selection is delegated to Chrome; profile contents never enter the container."""
        with patch.dict(os.environ, {"DISPLAY": ":0"}, clear=True), \
                patch.object(runtime, "inside_box", return_value=False), \
                patch.object(personal_browser.shutil, "which", return_value="/usr/bin/test-chrome"):
            command = personal_browser.command(PersonalBrowserConfig(True, "chrome", "Profile 2"))
            self.assertEqual(command, ["/usr/bin/test-chrome", "--profile-directory=Profile 2"])
            with self.assertRaises(AgentBoxError):
                personal_browser.command(PersonalBrowserConfig(True, "chrome", "../Default"))

    def test_missing_or_sandboxed_browser_has_no_fallback(self):
        """A selected browser's failure cannot silently move consent to another profile."""
        with patch.dict(os.environ, {"DISPLAY": ":0"}, clear=True), \
                patch.object(runtime, "inside_box", return_value=False):
            with patch.object(personal_browser.shutil, "which", return_value=None), \
                    self.assertRaisesRegex(AgentBoxError, "not found"):
                personal_browser.command(PersonalBrowserConfig(True, "chrome"))
            with patch.object(personal_browser.shutil, "which", return_value="/snap/chromium/browser"), \
                    self.assertRaisesRegex(AgentBoxError, "Snap/Flatpak"):
                personal_browser.command(PersonalBrowserConfig(True, "chromium"))

    def test_extension_container_uses_host_loopback_without_profile_mounts_or_tokens(self):
        """The real desktop lives on the host, even when its MCP client is in a workspace."""
        argv = browser.container_command("/session", "/project", "/project", [],
                                         container="workspace", personal=True)
        self.assertIn("--network=host", argv)
        self.assertIn("--extension", argv)
        self.assertNotIn("--isolated", argv)
        self.assertFalse(any("TOKEN" in arg or "user-data-dir" in arg for arg in argv))
        self.assertEqual([argv[i+1] for i,a in enumerate(argv) if a == "--mount"],
                         ["type=bind,src=/project,dst=/project,ro=false",
                          "type=bind,src=/session/output,dst=/session/output,ro=false"])

    def test_provider_registrations_are_additive_and_distinct(self):
        """Personal access cannot replace isolated browsing or user-owned MCP settings."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            argv = runtime.browser_codex_overrides(root, ["bridge"], server_name=personal_browser.SERVER)
            self.assertIn("mcp_servers.agent_box_personal_browser=", argv[0])
            args = runtime.browser_claude_args(root, ["bridge"], server_name=personal_browser.SERVER)
            self.assertEqual(args, ["--mcp-config", str(root / "mcp.json")])
            self.assertEqual(json.loads((root / "mcp.json").read_text())["mcpServers"],
                             {"agent_box_personal_browser": {"command": "bridge", "args": []}})
            self.assertNotIn("--strict-mcp-config", args)

    def test_opener_is_private_per_process_and_removed_after_start_failure(self):
        """Concurrent clients must not share a host opener or leave it after failed startup."""
        with tempfile.TemporaryDirectory() as temporary, patch.object(personal_browser, "STATE_ROOT", Path(temporary)):
            root = Path(temporary) / "runtime/browser-openers"
            with self.assertRaisesRegex(OSError, "start failed"):
                with personal_browser.opener(["podman", "run", "image"], ["/browser"]) as first, \
                        personal_browser.opener(["podman", "run", "image"], ["/browser"]) as second:
                    self.assertNotEqual(first[3], second[3])
                    self.assertEqual(len(list(root.iterdir())), 2)
                    for directory in root.iterdir():
                        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
                        self.assertEqual((directory / "open.sock").stat().st_mode & 0o777, 0o600)
                    raise OSError("start failed")
            self.assertEqual(list(root.iterdir()), [])

    def test_socket_uses_only_the_host_selected_browser_and_profile(self):
        """Workspace-supplied command/profile fields cannot replace trusted opener policy."""
        with tempfile.TemporaryDirectory() as temporary, patch.object(personal_browser, "STATE_ROOT", Path(temporary)):
            opened = threading.Event()
            with patch.object(personal_browser.subprocess, "Popen", side_effect=lambda *a, **kw: opened.set()) as popen, \
                    personal_browser.opener(["podman", "run", "image"], ["/browser", "--profile-directory=Profile 2"]):
                directory = next((Path(temporary) / "runtime/browser-openers").iterdir())
                with socket.socket(socket.AF_UNIX) as connection:
                    connection.connect(str(directory / "open.sock"))
                    connection.sendall(json.dumps({"url": consent_url(), "command": "/malicious"}).encode())
                    connection.shutdown(socket.SHUT_WR)
                    self.assertTrue(opened.wait(2))
                self.assertEqual(popen.call_args.args[0],
                                 ["/browser", "--profile-directory=Profile 2", consent_url()])


class ExtensionPreflightTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.profile = self.root / "Default"
        self.profile.mkdir()

    def prefs(self, entries, filename="Preferences"):
        (self.profile / filename).write_text(json.dumps({"extensions": {"settings": entries}}))

    def test_split_registration_and_disabled_extensions_are_not_missing(self):
        """The secure preference store and unpacked installs must not trigger reinstallation."""
        self.prefs({})
        self.assertEqual(personal_browser.extension_status(self.profile), "missing")
        for entry, expected in (({"state": 1, "path": "/unpacked"}, "registered"),
                                ({"state": 0}, "disabled"),
                                ({"state": 1, "disable_reasons": [1]}, "disabled")):
            with self.subTest(entry=entry):
                self.prefs({personal_browser.EXTENSION: entry}, "Secure Preferences")
                self.assertEqual(personal_browser.extension_status(self.profile), expected)
        self.prefs({personal_browser.EXTENSION: {"state": 1}})
        self.assertEqual(personal_browser.extension_status(self.profile), "unknown")

    def test_missing_unreadable_and_malformed_metadata_do_not_prove_absence(self):
        """Unknown metadata must not open an installation page or masquerade as detection."""
        self.assertEqual(personal_browser.extension_status(self.profile), "unknown")
        for value in ("{}", "[]", "{", '{"extensions":{"settings":[]}}'):
            with self.subTest(value=value):
                (self.profile / "Preferences").write_text(value)
                self.assertEqual(personal_browser.extension_status(self.profile), "unknown")
        self.prefs({})
        with patch.object(Path, "read_text", side_effect=PermissionError):
            self.assertEqual(personal_browser.extension_status(self.profile), "unknown")
        (self.profile / "Secure Preferences").write_text("{")
        self.assertEqual(personal_browser.extension_status(self.profile), "unknown")

    def test_missing_registration_opens_store_in_selected_profile_without_mutation(self):
        """Installation must stay in the user's selected browser and require their interaction."""
        self.prefs({})
        before = (self.profile / "Preferences").read_bytes()
        desktop = ["/usr/bin/chromium", "--profile-directory=Default"]
        with patch.object(personal_browser, "command", return_value=desktop), \
                patch.object(personal_browser, "browser_user_data_dir", return_value=self.root), \
                patch.object(personal_browser.subprocess, "Popen") as opened, \
                self.assertRaisesRegex(AgentBoxError, "Install it there, then start a new"):
            personal_browser.prepare(PersonalBrowserConfig(True, "chromium", "Default"))
        self.assertEqual(opened.call_args.args[0], [*desktop, personal_browser.STORE])
        self.assertEqual((self.profile / "Preferences").read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["Default"])

    def test_last_used_profile_is_resolved_and_pinned_for_this_launch_only(self):
        """A profile switch between detection and opening must not target another profile."""
        (self.root / "Local State").write_text(json.dumps({"profile": {"last_used": "Default"}}))
        self.prefs({personal_browser.EXTENSION: {"state": 1}})
        config = PersonalBrowserConfig(True, "chrome")
        with patch.object(personal_browser, "command", return_value=["/usr/bin/google-chrome"]), \
                patch.object(personal_browser, "browser_user_data_dir", return_value=self.root), \
                patch.object(personal_browser.subprocess, "Popen") as opened:
            desktop, notice = personal_browser.prepare(config)
        opened.assert_not_called()
        self.assertEqual(desktop, ["/usr/bin/google-chrome", "--profile-directory=Default"])
        self.assertIn("approve", notice)
        self.assertEqual(config.profile, "")

    def test_unknown_registration_guides_installation_without_claiming_absence(self):
        """Custom or unreadable profiles still get the normal consent flow and a visible link."""
        with patch.object(personal_browser, "command", return_value=["/custom-browser"]), \
                patch.object(personal_browser, "browser_user_data_dir", return_value=None), \
                patch.object(personal_browser.subprocess, "Popen") as opened:
            desktop, notice = personal_browser.prepare(PersonalBrowserConfig(True))
        opened.assert_not_called()
        self.assertEqual(desktop, ["/custom-browser"])
        self.assertIn("could not verify", notice)
        self.assertIn(personal_browser.STORE, notice)

    def test_disabled_integration_performs_no_discovery_or_opening(self):
        """Default-off and a negative session override cannot inspect personal profile metadata."""
        with patch.object(personal_browser, "command") as command, \
                patch.object(personal_browser, "browser_user_data_dir") as directory, \
                patch.object(personal_browser.subprocess, "Popen") as opened:
            self.assertEqual(personal_browser.prepare(PersonalBrowserConfig()), ([], ""))
        command.assert_not_called()
        directory.assert_not_called()
        opened.assert_not_called()

    def test_disabled_extension_guides_enablement_without_opening_store(self):
        """Registration with a disabled state requires enablement, not another install."""
        self.prefs({personal_browser.EXTENSION: {"state": 0}})
        with patch.object(personal_browser, "command", return_value=["/usr/bin/chromium"]), \
                patch.object(personal_browser, "browser_user_data_dir", return_value=self.root), \
                patch.object(personal_browser.subprocess, "Popen") as opened, \
                self.assertRaisesRegex(AgentBoxError, "enable it at chrome://extensions"):
            personal_browser.prepare(PersonalBrowserConfig(True, profile="Default"))
        opened.assert_not_called()

    def test_browser_paths_match_native_environment_precedence_without_trusting_home(self):
        """Inspection must follow the same selected data directory, never another browser's."""
        with patch.dict(os.environ, {}, clear=True), patch.object(paths, "HOST_HOME", self.root):
            self.assertEqual(paths.browser_user_data_dir("/opt/google/chrome/google-chrome"),
                             self.root / ".config/google-chrome")
            self.assertEqual(paths.browser_user_data_dir("/usr/bin/chromium"),
                             self.root / ".config/chromium")
            os.environ["XDG_CONFIG_HOME"] = "/xdg"
            self.assertEqual(paths.browser_user_data_dir("chromium"), Path("/xdg/chromium"))
            os.environ["CHROME_CONFIG_HOME"] = "/chrome-config"
            self.assertEqual(paths.browser_user_data_dir("chromium"), Path("/chrome-config/chromium"))
            os.environ["CHROME_USER_DATA_DIR"] = "/custom"
            self.assertEqual(paths.browser_user_data_dir("chromium"), Path("/custom"))
            os.environ["CHROME_USER_DATA_DIR"] = "relative"
            self.assertIsNone(paths.browser_user_data_dir("chromium"))
        with patch.dict(os.environ, {"HOME": "/poisoned"}, clear=True):
            self.assertIsNone(paths.browser_user_data_dir("chromium"))
            self.assertIsNone(paths.browser_user_data_dir("unknown-wrapper"))


class PersonalHostLaunchTests(IsolatedRuntimeTest):
    def test_profile_override_reaches_host_preparation_in_both_launch_modes(self):
        """Both launch modes must use the override for extension checks and the consent opener."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile = root / "Profile 1"
            profile.mkdir()
            for host, request in product((False, True), ("missing", "registered", "disabled", "version")):
                config = Config()
                config.host.personal_browser = PersonalBrowserConfig(True, "chromium", "Default")
                before = config.to_dict()
                entries = {personal_browser.EXTENSION: {"state": 1}} if request == "registered" else {}
                (profile / "Preferences").write_text(json.dumps({"extensions": {"settings": entries}}))
                options, _ = parse(["--personal-browser-profile", "Profile 1",
                                    *(["--no-personal-browser"] if request == "disabled" else []), "--no-tmux"])
                rest = ["--version"] if request == "version" else ["exec", "hello"]
                with self.subTest(host=host, request=request), ExitStack() as stack:
                    for name, value in (
                        ("load_config", config), ("resolved_workspace", Workspace(root, root)),
                        ("session_runtime", root / "snapshot.json"),
                        ("tracker_mounts", [(root, root, False)]),
                        ("ensure_workspace_image", "sha256:test"), ("tmux_enabled", False),
                        ("launch_in_workspace_container", 0),
                    ):
                        stack.enter_context(patch.object(runtime, name, return_value=value))
                    for name in ("sync_managed_skills", "claude_skill_links", "prepare_network",
                                 "prepare_k3s", "workspace_container_spec"):
                        stack.enter_context(patch.object(runtime, name))
                    stack.enter_context(patch.object(runtime.host_podman, "require"))
                    stack.enter_context(patch.object(runtime.shutil, "which", return_value="/test/native-browser"))
                    stack.enter_context(patch.dict(os.environ, {"DISPLAY": ":0"}, clear=True))
                    stack.enter_context(patch.object(project_config, "load", return_value={}))
                    selected = stack.enter_context(patch.object(personal_browser, "command", wraps=personal_browser.command))
                    stack.enter_context(patch.object(personal_browser, "browser_user_data_dir", return_value=root))
                    opened = stack.enter_context(patch.object(personal_browser.subprocess, "Popen"))
                    stack.enter_context(patch.object(runtime.subprocess, "call", return_value=0))
                    prepared = stack.enter_context(patch.object(browser, "prepare", return_value=["/test/bridge"]))
                    stderr = stack.enter_context(patch("sys.stderr", new_callable=io.StringIO))
                    if host:
                        runtime.host_main("codex", rest, options)
                    else:
                        with self.assertRaises(SystemExit) as exited:
                            runtime.runtime_main(["codex", *rest], runtime.PACKAGE_ROOT, options)
                        self.assertEqual(exited.exception.code, 0)
                    self.assertEqual(config.to_dict(), before)
                    desktop = ["/test/native-browser", "--profile-directory=Profile 1"]
                    if request == "registered":
                        prepared.assert_called_once()
                        self.assertEqual(prepared.call_args.kwargs["desktop"], desktop)
                        opened.assert_not_called()
                    elif request == "missing":
                        prepared.assert_not_called()
                        self.assertEqual(opened.call_args.args[0], [*desktop, personal_browser.STORE])
                        self.assertIn("Install it there, then start a new", stderr.getvalue())
                    else:
                        prepared.assert_not_called()
                        selected.assert_not_called()
                        opened.assert_not_called()

    def test_real_launch_wiring_adds_both_servers_without_touching_saved_settings(self):
        """Testing the helpers alone would miss the second server being lost at dispatch."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = Config()
            config.box.browser.enabled = True
            config.host.personal_browser.enabled = True
            before = config.to_dict()

            def prepare(directory, *args, **kwargs):
                directory.mkdir(parents=True, exist_ok=True)
                return ["python3", "/test/bridge.py"]

            for agent in ("claude", "codex"):
                with self.subTest(agent=agent), \
                        patch.object(runtime, "launch_config", return_value=config), \
                        patch.object(runtime, "resolved_workspace", return_value=Workspace(root, root)), \
                        patch.object(runtime, "session_runtime", return_value=root / "snapshot.json"), \
                        patch.object(runtime, "HOST_SESSIONS_ROOT", root / "sessions"), \
                        patch.object(runtime, "record_workspace_path"), \
                        patch.object(runtime.shutil, "which", return_value=f"/usr/bin/{agent}"), \
                        patch.object(personal_browser, "command", return_value=["/usr/bin/test-browser"]), \
                        patch.object(browser, "prepare", side_effect=prepare) as prepared, \
                        patch.object(runtime.subprocess, "call", return_value=0) as called:
                    runtime.host_main(agent, ["--print", "hello"] if agent == "claude" else ["exec", "hello"],
                                      LaunchOptions(tmux=False))
                argv = called.call_args.args[0]
                self.assertEqual(prepared.call_count, 2)
                if agent == "codex":
                    self.assertIn("mcp_servers.agent_box_browser=", " ".join(argv))
                    self.assertIn("mcp_servers.agent_box_personal_browser=", " ".join(argv))
                else:
                    files = [argv[i+1] for i, arg in enumerate(argv) if arg == "--mcp-config"]
                    servers = {name for f in files for name in json.loads(Path(f).read_text())["mcpServers"]}
                    self.assertEqual(servers, {"agent_box_browser", "agent_box_personal_browser"})
                self.assertEqual(config.to_dict(), before)


if __name__ == "__main__":
    unittest.main()
