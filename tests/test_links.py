"""CLI links preserve stdout; host access and callback relays need explicit consent."""
import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import quote

from agent_box import links, launch_options, runtime
from agent_box.config import Config, LinksConfig
from agent_box.workspace import Workspace


def oauth(host="127.0.0.1", port=42121):
    origin = f"[{host}]" if ":" in host else host
    return "https://example.invalid/login?redirect_uri=" + quote(f"http://{origin}:{port}/callback", safe="")


class LinkRequestTests(unittest.TestCase):
    def test_no_host_capability_is_needed_to_surface_a_url_without_a_terminal(self):
        """CLI JSON must remain on stdout; an absent /dev/tty must not lose the link."""
        url = oauth()
        with mock.patch.dict(os.environ, {links.SOCKET_ENV: ""}), \
                contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()) as err, \
                mock.patch.object(links.socket, "socket") as connect:
            self.assertEqual(links.open_link(url), 0)
        connect.assert_not_called()
        self.assertEqual(out.getvalue(), "")
        self.assertIn(url, err.getvalue())
        self.assertIn("--links", err.getvalue())

    def test_only_web_urls_and_unambiguous_loopback_callbacks_are_accepted(self):
        """Opening a URL must not become file access, a command, or an ambiguous relay."""
        for url in ("file:///etc/passwd", "javascript:alert(1)", "https://user:password@host/",
                    "https://host/\nforged output", "http://host:0/", "http://host:not-a-port/",
                    "https://host/?redirect_uri=http://localhost:1&redirect_uri=http://localhost:2"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                links.callback_target(url)
        self.assertEqual(links.callback_target(oauth()), ("127.0.0.1", 42121))
        self.assertEqual(links.callback_target(oauth("::1")), ("::1", 42121))
        self.assertEqual(links.callback_target("http://localhost:8080/"), ("localhost", 8080))
        self.assertIsNone(links.callback_target("https://example.invalid/docs"))

    def test_failed_broker_does_not_claim_to_prepare_the_callback(self):
        """An unavailable server route cannot be reported as a successful opener."""
        with mock.patch.dict(os.environ, {links.SOCKET_ENV: "/missing/link.sock"}), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(links.open_link(oauth()), 1)
        self.assertIn("no callback route prepared", err.getvalue())

    def test_remote_commands_keep_aliases_ports_and_ipv6_destinations(self):
        """The workstation and server loopbacks are distinct; SSH config owns jump hosts."""
        text = "\n".join(links.instructions({"ssh": "dev-alias", "ssh_port": 2222}, ("::1", 42121)))
        self.assertIn("-p 2222", text)
        self.assertIn("[::1]:42121:[::1]:42121", text)
        self.assertIn("dev-alias", text)
        self.assertIn("ExitOnForwardFailure=yes", text)
        text = "\n".join(links.instructions({"coder": "alice/workspace.agent"}, ("::1", 42121)))
        self.assertIn("coder port-forward alice/workspace.agent --tcp '[::1]:42121:42121'", text)
        text = "\n".join(links.instructions({"ssh": "dev-alias"}, ("127.0.0.2", 42121)))
        self.assertIn("127.0.0.2:42121:127.0.0.2:42121", text)
        text = "\n".join(links.instructions({"ssh": "dev-alias"}, ("localhost", 42121)))
        self.assertIn("-L 127.0.0.1:42121:127.0.0.1:42121", text)
        self.assertIn("-L '[::1]:42121:[::1]:42121'", text)
        text = "\n".join(links.instructions({"coder": "remote"}, ("localhost", 42121)))
        self.assertIn("--tcp 127.0.0.1:42121:42121", text)
        self.assertIn("--tcp '[::1]:42121:42121'", text)

    def test_remote_context_never_opens_a_graphical_server_browser(self):
        """SSH and Coder are remote even if the server happens to have DISPLAY set."""
        for environment in ({"SSH_CONNECTION": "192.0.2.1 55555 192.0.2.2 2222", "DISPLAY": ":0"},
                            {"CODER_WORKSPACE_NAME": "remote", "DISPLAY": ":0"}):
            with self.subTest(environment=environment), mock.patch.dict(os.environ, environment, clear=True):
                context = links.remote_context(LinksConfig())
            self.assertFalse(context["desktop"])


class BrokerTests(unittest.TestCase):
    def test_localhost_ipv6_conflict_refuses_opening_and_rolls_back_ipv4(self):
        """A browser choosing ::1 must not deliver its login to a different host service."""
        with socket.socket(socket.AF_INET6) as occupied:
            occupied.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            occupied.bind(("::1", 0))
            occupied.listen(1)
            port = occupied.getsockname()[1]
            broker = links.Broker("box", {"desktop": True}, True)
            try:
                with mock.patch.object(links.subprocess, "Popen") as opened, \
                        self.assertRaisesRegex(ValueError, "unavailable"):
                    broker.handle({"url": oauth("localhost", port)})
                opened.assert_not_called()
                with socket.socket() as replacement:
                    replacement.bind(("127.0.0.1", port))
            finally:
                broker.close()

    def test_expanded_ipv6_and_canonical_callbacks_use_the_same_ipv6_route(self):
        """Equivalent IPv6 spellings must work and cannot create competing listeners."""
        with socket.socket(socket.AF_INET6) as probe:
            probe.bind(("::1", 0))
            port = probe.getsockname()[1]
        broker = links.Broker("box", {"desktop": False}, False)
        try:
            broker.handle({"url": oauth("0:0:0:0:0:0:0:1", port)})
            broker.handle({"url": oauth("::1", port)})
            self.assertEqual(len(broker.relays), 1)
            with socket.socket(socket.AF_INET6) as collision, self.assertRaises(OSError):
                collision.bind(("::1", port))
        finally:
            broker.close()

    def test_host_opening_is_separate_and_happens_after_the_callback_listener(self):
        """No browser may receive a localhost callback URL before its route is prepared."""
        events = []
        relay = mock.Mock(deadline=time.monotonic() + 300)
        with mock.patch.object(links, "Relay", side_effect=lambda *a: (events.append("relay"), relay)[1]), \
                mock.patch.object(links.shutil, "which", return_value="/usr/bin/xdg-open"), \
                mock.patch.object(links.subprocess, "Popen", side_effect=lambda *a, **k: events.append("open")):
            broker = links.Broker("box", {"desktop": True}, True)
            self.assertTrue(broker.handle({"url": oauth()})["ok"])
            broker.close()
        self.assertEqual(events, ["relay", "open"])

    def test_port_conflicts_refuse_before_opening_and_duplicate_requests_share_a_route(self):
        """A port owned by another listener must never silently receive login callbacks."""
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            port = occupied.getsockname()[1]
            broker = links.Broker("box", {"desktop": True}, True)
            with mock.patch.object(links.subprocess, "Popen") as opened, \
                    self.assertRaisesRegex(ValueError, "unavailable"):
                broker.handle({"url": oauth(port=port)})
            opened.assert_not_called()
        relay = mock.Mock(deadline=time.monotonic() + 300)
        with mock.patch.object(links, "Relay", return_value=relay) as create:
            broker = links.Broker("box", {"desktop": False}, False)
            broker.handle({"url": oauth()})
            broker.handle({"url": oauth()})
            self.assertEqual(create.call_count, 1)
            broker.close()

    def test_a_relay_carries_http_and_half_close_without_shell_or_tty(self):
        """The same bytes must cross both directions, including a request ending with EOF."""
        with socket.socket() as upstream:
            upstream.bind(("127.0.0.2", 0))
            upstream.listen(1)
            upstream.settimeout(3)
            port = upstream.getsockname()[1]
            def response():
                for _ in range(2):
                    client, _ = upstream.accept()
                    with client:
                        client.settimeout(3)
                        request = bytearray()
                        while chunk := client.recv(4096):
                            request.extend(chunk)
                        client.sendall(b"HTTP/1.0 200 OK\r\n\r\n" + request)
            thread = threading.Thread(target=response)
            thread.start()
            real_popen = subprocess.Popen
            def connector(argv, **kwargs):
                self.assertEqual(argv[:5], ["podman", "exec", "-i", "box", "agent-box"])
                return real_popen([sys.executable, "-m", "agent_box.links", *argv[6:]], **kwargs)
            with mock.patch.object(links.subprocess, "Popen", side_effect=connector):
                relay = links.Relay("box", ("127.0.0.2", port), "localhost")
                try:
                    for host in ("127.0.0.1", "::1"):
                        with socket.create_connection((host, port), timeout=3) as client:
                            client.sendall(b"GET /callback?code=fake HTTP/1.0\r\n\r\n")
                            client.shutdown(socket.SHUT_WR)
                            received = bytearray()
                            while chunk := client.recv(4096):
                                received.extend(chunk)
                        self.assertIn(b"200 OK", received)
                        self.assertIn(b"code=fake", received)
                finally:
                    relay.close()
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())

    def test_expiry_and_close_release_the_loopback_port(self):
        """A finished login must not leave an indefinitely exposed server port."""
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        with mock.patch.object(links, "CALLBACK_TTL", 0.05):
            relay = links.Relay("box", ("127.0.0.1", port))
            relay.thread.join(timeout=1)
            relay.close()
        with socket.socket() as replacement:
            replacement.bind(("127.0.0.1", port))


class LinkPolicyTests(unittest.TestCase):
    def test_headless_join_keeps_detected_ssh_destination_and_port_together(self):
        """A later launch without SSH_CONNECTION must retain the usable nondefault tunnel port."""
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(links, "LINKS_RUNTIME_DIR", Path(tmp)), \
                mock.patch.object(links, "ping", return_value=True):
            links.prepare("ws")
            previous = {"desktop": False, "ssh": "alice@server", "ssh_port": 2222, "coder": ""}
            links.context_file("ws").write_text(json.dumps(previous))
            headless = {"desktop": False, "ssh": "", "ssh_port": 22, "coder": ""}
            with mock.patch.object(links, "remote_context", return_value=headless):
                links.ensure("ws", "box", LinksConfig(enabled=True))
            context = json.loads(links.context_file("ws").read_text())
            command = "\n".join(links.instructions(context, ("127.0.0.1", 42121)))
            self.assertIn("alice@server", command)
            self.assertIn("-p 2222", command)

    def test_runtime_root_is_private_and_cannot_be_redirected_by_symlinks(self):
        """A shared tmp parent must not let another user substitute the mounted socket directory."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(links, "LINKS_RUNTIME_DIR", root / "runtime"):
                links.prepare("ws")
                self.assertEqual(links.LINKS_RUNTIME_DIR.stat().st_mode & 0o777, 0o700)
                self.assertEqual(links.directory("ws").stat().st_mode & 0o777, 0o700)
            (root / "redirect").symlink_to(root / "runtime", target_is_directory=True)
            with mock.patch.object(links, "LINKS_RUNTIME_DIR", root / "redirect"), self.assertRaises(OSError):
                links.prepare("ws")
            with mock.patch.object(links, "LINKS_RUNTIME_DIR", root / "runtime"), \
                    mock.patch.object(links.os, "getuid", return_value=os.getuid() + 1), \
                    self.assertRaisesRegex(ValueError, "another user"):
                links.prepare("ws")

    def test_reused_broker_refreshes_remote_hints_without_reenabling_the_server_browser(self):
        """A local/SSH shared box must protect remote sessions even after another local join."""
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(links, "LINKS_RUNTIME_DIR", Path(tmp)), \
                mock.patch.object(links, "ping", return_value=True):
            links.prepare("ws")
            config = LinksConfig(enabled=True, open_browser=True)
            local = {"desktop": True, "ssh": "", "ssh_port": 22, "coder": ""}
            remote = {"desktop": False, "ssh": "server-alias", "ssh_port": 2222, "coder": ""}
            headless = {"desktop": False, "ssh": "", "ssh_port": 22, "coder": ""}
            with mock.patch.object(links, "remote_context", return_value=local):
                links.ensure("ws", "box", config)
            broker = links.Broker("box", local, True, links.context_file("ws"))
            self.assertNotEqual(links.context_file("ws").parent, links.directory("ws"))
            with mock.patch.object(links, "remote_context", return_value=remote):
                links.reattach("ws", "box")
            with mock.patch.object(links.subprocess, "Popen") as opened, \
                    mock.patch.object(links.shutil, "which", return_value="/usr/bin/xdg-open"), \
                    mock.patch.object(links, "Relay", return_value=mock.Mock(deadline=time.monotonic() + 300)):
                result = broker.handle({"url": oauth()})
                self.assertIn("server-alias", "\n".join(result["lines"]))
                self.assertFalse(broker.context["desktop"])
                with mock.patch.object(links, "remote_context", return_value=headless):
                    links.reattach("ws", "box")
                result = broker.handle({"url": oauth()})
                self.assertIn("-p 2222", "\n".join(result["lines"]))
                with mock.patch.object(links, "remote_context", return_value=local):
                    links.ensure("ws", "box", config)
                broker.handle({"url": "https://example.invalid/docs"})
                opened.assert_not_called()
            links.stop("ws")
            self.assertFalse(links.context_file("ws").exists())

    def test_host_access_is_off_and_structural_while_flags_and_snapshots_agree(self):
        """A per-exec switch cannot hide a socket already shared with the whole box."""
        config = Config()
        self.assertFalse(config.box.links.enabled or config.box.links.open_browser)
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(links, "LINKS_RUNTIME_DIR", Path(tmp)):
            workspace = Workspace(Path("/project"), Path("/project"))
            state = runtime.workspace_runtime_state(workspace)
            off = runtime.container_lifetime_args(workspace, [], Path("/checkout"), config, runtime_state=state)
            launch_options.apply(config, {"links": True})
            on = runtime.container_lifetime_args(workspace, [], Path("/checkout"), config, runtime_state=state)
            self.assertNotEqual(off, on)
            mount = next(a for a in on if f"src={links.directory(state.root.name)}" in a)
            self.assertTrue(mount.endswith(",ro=true"))
            self.assertFalse(any(links.SOCKET_ENV in a for a in off))
            self.assertTrue(any(links.SOCKET_ENV in a for a in on))
            restored = Config.from_dict(config.to_dict())
            self.assertTrue(restored.box.links.enabled)
            runtime.sync_tracker_runtime(config, Path(tmp) / "snapshot", policy=True)
            snapshot = json.loads((Path(tmp) / "snapshot/config.json").read_text())
            self.assertTrue(Config.from_dict(snapshot).box.links.enabled)
        for destination in ("--config=evil", "a b", "a\ncommand"):
            self.assertTrue(LinksConfig(ssh_destination=destination).validate())

    def test_an_unresponsive_live_socket_is_never_replaced_by_another_broker(self):
        """Concurrent opening must not lose active callback routes when ping times out."""
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(links, "LINKS_RUNTIME_DIR", Path(tmp)), \
                mock.patch.object(links, "ping", return_value=False), \
                mock.patch.object(links.subprocess, "Popen") as spawn:
            links.directory("ws").mkdir()
            with socket.socket(socket.AF_UNIX) as listener:
                listener.bind(str(links.endpoint("ws")))
                listener.listen(1)
                with self.assertRaisesRegex(ValueError, "busy"):
                    links.ensure("ws", "box", LinksConfig(enabled=True))
            spawn.assert_not_called()
            self.assertTrue(links.endpoint("ws").exists())

    def test_detached_broker_is_reused_and_stop_releases_its_socket(self):
        """A launcher exiting must not strand tmux login requests or duplicate the broker."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = root / "podman"
            binary.write_text('#!/bin/sh\nprintf "true\\n"\n')
            binary.chmod(0o700)
            with mock.patch.object(links, "LINKS_RUNTIME_DIR", root / "runtime"), \
                    mock.patch.dict(os.environ, {"PATH": f"{root}:{os.environ['PATH']}"}):
                config = LinksConfig(enabled=True, ssh_destination="server-alias")
                links.prepare("ws")
                try:
                    links.ensure("ws", "box", config)
                    self.assertTrue(links.ping(links.endpoint("ws"), "box"))
                    with mock.patch.object(links.subprocess, "Popen") as spawn:
                        links.ensure("ws", "box", config)
                    spawn.assert_not_called()
                    with socket.socket(socket.AF_UNIX) as connection:
                        connection.connect(str(links.endpoint("ws")))
                        connection.sendall(json.dumps({"url": "https://example.invalid/docs"}).encode() + b"\n")
                        self.assertTrue(links._receive(connection)["ok"])
                finally:
                    links.stop("ws")
                self.assertFalse(links.endpoint("ws").exists())

