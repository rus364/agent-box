"""Personal desktop access must require its own host choice and browser consent."""
import json
import os
from pathlib import Path
import tempfile
import socket
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from agent_box import browser, personal_browser, project_config, runtime
from agent_box.config import Config, PersonalBrowserConfig
from agent_box.errors import AgentBoxError
from agent_box.launch_options import apply as apply_overrides
from agent_box.launch_options import LaunchOptions
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


class PersonalHostLaunchTests(IsolatedRuntimeTest):
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
