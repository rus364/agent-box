"""Synthetic clipboard and real PTY checks must never read the desktop selection."""
import base64
import fcntl
import json
import os
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest import mock

from agent_box import clipboard, config, config_menu, project_config, runtime
from agent_box.errors import AgentBoxError
from agent_box.launch_options import LaunchOptions
from agent_box.workspace import Workspace
from test_external_config import external_config
from test_runtime import IsolatedRuntimeTest

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1sAAAAASUVORK5CYII=")


class ClipboardInputTests(unittest.TestCase):
    def test_keys_and_split_enhanced_keys_deliver_an_attachment(self):
        """Terminals encode the same gesture differently; each requests one image."""
        for key in (b"\x16", b"\x1b[118;5u", b"\x1b[118;5:1u", b"\x1b[118:86;5u", b"\x1b[27;5;118~"):
            for split in range(len(key) + 1):
                with self.subTest(key=key, split=split):
                    paste = mock.Mock(return_value=b"ATTACHMENT")
                    parser = clipboard.InputFilter(paste, mock.Mock())
                    self.assertEqual(parser.feed(key[:split]) + parser.feed(key[split:]), b"ATTACHMENT")
                    paste.assert_called_once_with()

    def test_bracketed_text_never_reads_clipboard_even_across_idle_flushes(self):
        """A pasted Ctrl+V is untrusted text, including split framing delimiters."""
        text = b"\x1b[200~hello\x16\x1b[118;5u\x1b[200~world\x1b[201~"
        for split in range(len(text) + 1):
            with self.subTest(split=split):
                paste = mock.Mock()
                parser = clipboard.InputFilter(paste, mock.Mock())
                result = parser.feed(text[:split]) + parser.flush() + parser.feed(text[split:]) + parser.flush()
                self.assertEqual(result, text)
                paste.assert_not_called()
                self.assertFalse(parser.pasting)

    def test_unrelated_keys_and_escapes_remain_byte_exact(self):
        """Clipboard support must not change navigation, releases or text paste."""
        text = b"abc\x03\x1b[A\x1b[118;6u\x1b[118;5:3u\x1bxyz\x1b"
        parser = clipboard.InputFilter(mock.Mock(side_effect=AssertionError), mock.Mock())
        result = b"".join(parser.feed(bytes([value])) for value in text) + parser.flush()
        self.assertEqual(result, text)

    def test_failure_reports_and_forwards_original_key_without_ending_input(self):
        """Text-only or unavailable clipboards must retain native non-image behavior."""
        diagnostic = mock.Mock()
        parser = clipboard.InputFilter(mock.Mock(side_effect=clipboard.ClipboardError("no PNG")), diagnostic)
        self.assertEqual(parser.feed(b"a\x16b"), b"a\x16b")
        self.assertIn("no PNG", diagnostic.call_args.args[0])


class ClipboardTransferTests(unittest.TestCase):
    def test_wayland_requests_only_png_and_never_falls_back_to_x11(self):
        """An XWayland selection may differ from the user's active Wayland clipboard."""
        with mock.patch.dict(os.environ, {"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":1"}, clear=True), \
             mock.patch.object(clipboard.shutil, "which", return_value="/synthetic/wl-paste"), \
             mock.patch.object(clipboard, "exchange", return_value=PNG) as exchange:
            self.assertEqual(clipboard.read_image(), PNG)
            self.assertEqual(exchange.call_args.args[0], ["wl-paste", "--no-newline", "--type", "image/png"])
            exchange.side_effect = clipboard.ClipboardError("no image")
            with self.assertRaises(clipboard.ClipboardError):
                clipboard.read_image()
            self.assertEqual(exchange.call_count, 2)

    def test_x11_requests_clipboard_png_without_primary_or_text_access(self):
        with mock.patch.dict(os.environ, {"DISPLAY": ":1"}, clear=True), \
             mock.patch.object(clipboard.shutil, "which", return_value="/synthetic/xclip"), \
             mock.patch.object(clipboard, "exchange", return_value=PNG) as exchange:
            self.assertEqual(clipboard.read_image(), PNG)
        self.assertEqual(exchange.call_args.args[0], ["xclip", "-selection", "clipboard", "-out", "-target", "image/png"])

    def test_ssh_headless_and_missing_tools_do_not_read_clipboard(self):
        """Unavailable desktops report only on Ctrl+V and preserve the original key."""
        for environment, message in (({}, "needs a Wayland or X11 desktop"),
                ({"DISPLAY": ":1", "SSH_CONNECTION": "remote"}, "SSH clipboard access is disabled"),
                ({"WAYLAND_DISPLAY": "wayland-0", "SSH_TTY": "/dev/pts/2"}, "SSH clipboard access is disabled"),
                ({"DISPLAY": ":1"}, "needs xclip installed on the host"),
                ({"WAYLAND_DISPLAY": "wayland-0"}, "needs wl-paste installed on the host")):
            with self.subTest(environment=environment), mock.patch.dict(os.environ, environment, clear=True), \
                 mock.patch.object(clipboard.shutil, "which", return_value=None) as which, \
                 mock.patch.object(clipboard, "exchange") as exchange:
                diagnostic = mock.Mock()
                parser = clipboard.InputFilter(clipboard.read_image, diagnostic)
                self.assertEqual(parser.feed(b"ordinary input"), b"ordinary input")
                which.assert_not_called()
                diagnostic.assert_not_called()
                self.assertEqual(parser.feed(b"\x16"), b"\x16")
                diagnostic.assert_called_once()
                self.assertIn(message, diagnostic.call_args.args[0])
                exchange.assert_not_called()

    def test_non_png_and_excessive_dimensions_are_not_transferred(self):
        oversized = PNG[:16] + struct.pack(">II", 65536, 65536) + PNG[24:]
        for image in (b"private clipboard text", b"", oversized):
            with self.subTest(image=image[:8]), mock.patch.dict(os.environ, {"DISPLAY": ":1"}, clear=True), \
                 mock.patch.object(clipboard.shutil, "which", return_value="xclip"), \
                 mock.patch.object(clipboard, "exchange", return_value=image):
                with self.assertRaises(clipboard.ClipboardError):
                    clipboard.read_image()

    def test_exchange_bounds_bytes_and_timeout_and_reaps_its_child(self):
        for code, limit, timeout, expected in (
            ("import os; os.write(1, b'x'*1000000)", 32, 2, "size limit"),
            ("import time; time.sleep(10)", 32, .1, "timed out"),
            ("raise SystemExit(1)", 32, 2, "failed"),
        ):
            with self.subTest(expected=expected):
                real_popen = subprocess.Popen
                children = []
                def start(*args, **kwargs):
                    child = real_popen(*args, **kwargs)
                    children.append(child)
                    return child
                with mock.patch.object(clipboard.subprocess, "Popen", side_effect=start):
                    with self.assertRaisesRegex(clipboard.ClipboardError, expected):
                        clipboard.exchange([sys.executable, "-c", code], timeout=timeout, limit=limit)
                self.assertIsNotNone(children[0].poll())
                self.assertTrue(children[0].stdin.closed)
                self.assertTrue(children[0].stdout.closed)

    def test_exchange_handles_large_binary_stdin_and_backpressure(self):
        """A bounded duplex pump must not deadlock while the receiver writes first."""
        body = PNG * 20000
        code = "import os,sys; os.write(1,b'x'*65536); data=sys.stdin.buffer.read(); os.write(1,str(len(data)).encode())"
        self.assertEqual(clipboard.exchange([sys.executable, "-c", code], timeout=3, limit=70000, data=body),
                         b"x" * 65536 + str(len(body)).encode())

    def test_transfer_keeps_bytes_out_of_argv_and_validates_returned_path(self):
        def transfer(command, **kwargs):
            self.assertEqual(command[:5], ["podman", "exec", "-i", "box", "python3"])
            self.assertEqual(kwargs["data"], PNG)
            self.assertNotIn(PNG.decode("latin1"), " ".join(command))
            return b"/tmp/agent-box-clipboard-1000/" + command[-1].encode() + b"\n"
        with mock.patch.object(clipboard, "read_image", return_value=PNG), \
             mock.patch.object(clipboard, "exchange", side_effect=transfer):
            result = clipboard.paste_image("box")
        self.assertTrue(result.startswith(clipboard.PASTE_START + b"/tmp/agent-box-clipboard-1000/"))
        self.assertTrue(result.endswith(b".png" + clipboard.PASTE_END))
        with mock.patch.object(clipboard, "read_image", return_value=PNG), \
             mock.patch.object(clipboard, "exchange", return_value=b"\x1b[200~injected"):
            with self.assertRaisesRegex(clipboard.ClipboardError, "invalid path"):
                clipboard.paste_image("box")


class ClipboardStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "images"
        self.script = clipboard.STORE_SCRIPT.replace('"/tmp/agent-box-clipboard-" + str(os.getuid())', repr(str(self.root)))

    def store(self, name, data=PNG, *, count=32, quota=1024):
        return subprocess.run([sys.executable, "-c", self.script, "512", str(quota), str(count), name],
                              input=data, capture_output=True, timeout=6)

    def test_store_is_private_and_preserves_earlier_images_for_reattach(self):
        self.assertEqual(self.store("one.png").returncode, 0)
        self.assertEqual(self.store("two.png").returncode, 0)
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
        for name in ("one.png", "two.png"):
            self.assertEqual((self.root / name).read_bytes(), PNG)
            self.assertEqual((self.root / name).stat().st_mode & 0o777, 0o600)

    def test_quota_and_file_count_refuse_without_evicting_pending_images(self):
        self.assertEqual(self.store("one.png").returncode, 0)
        for kwargs in ({"count": 1}, {"quota": len(PNG)}, {}):
            with self.subTest(kwargs=kwargs):
                data = b"x" * 513 if not kwargs else PNG
                self.assertNotEqual(self.store("two.png", data=data, **kwargs).returncode, 0)
                self.assertEqual(list(self.root.iterdir()), [self.root / "one.png"])
                self.assertEqual((self.root / "one.png").read_bytes(), PNG)

    def test_symlink_directory_and_foreign_entries_cannot_redirect_writes(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        self.root.symlink_to(outside, target_is_directory=True)
        self.assertNotEqual(self.store("one.png").returncode, 0)
        self.assertEqual(list(outside.iterdir()), [])
        self.root.unlink()
        self.root.mkdir(mode=0o700)
        (self.root / "foreign").symlink_to(outside)
        self.assertNotEqual(self.store("one.png").returncode, 0)
        self.assertEqual(list(outside.iterdir()), [])

    def test_lock_contention_is_bounded_and_never_deletes_existing_files(self):
        self.root.mkdir(mode=0o700)
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self.assertNotEqual(self.store("one.png").returncode, 0)
            self.assertEqual(list(self.root.iterdir()), [])
        finally:
            os.close(fd)

    def test_receiver_timeout_removes_its_incomplete_image(self):
        """A killed Podman sender must not leave an unbounded receiving process."""
        script = self.script.replace("signal.ITIMER_REAL, 4)", "signal.ITIMER_REAL, 0.1)")
        child = subprocess.Popen([sys.executable, "-c", script, "512", "1024", "32", "one.png"],
                                 stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            child.stdin.write(PNG[:8])
            child.stdin.flush()
            self.assertNotEqual(child.wait(timeout=2), 0)
            self.assertEqual(list(self.root.iterdir()), [])
        finally:
            child.stdin.close()
            if child.poll() is None:
                child.kill()
                child.wait()


class ClipboardConfigurationTests(unittest.TestCase):
    def test_clipboard_has_no_saved_setting_menu_or_session_capability(self):
        """Explicit terminal input needs neither a global choice nor a project grant."""
        settings = config.Config()
        self.assertFalse(hasattr(settings.host, "clipboard_images"))
        self.assertNotIn("clipboard_images", settings.host.to_dict())
        self.assertNotIn("clipboard_images", config_menu.menu_keys(settings.host.to_dict(), ("host",)))
        self.assertNotIn("host.clipboard_images", config_menu.HELP)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            config.save_config(settings, path)
            self.assertNotIn("clipboard_images", path.read_text())
            self.assertFalse(hasattr(config.load_config(path).host, "clipboard_images"))
            with self.assertRaises(AgentBoxError):
                project_config.validate({"host": {"clipboard_images": True}}, path)
            runtime.sync_tracker_runtime(settings, Path(temp) / "snapshot", policy=True)
            snapshot = json.loads((Path(temp) / "snapshot/config.json").read_text())
            self.assertNotIn("host", snapshot)
            self.assertNotIn("clipboard_images", json.dumps(snapshot))

    def test_ineligible_launcher_is_the_original_popen_and_opens_no_pty(self):
        """Shells and noninteractive commands retain their direct process behavior."""
        with mock.patch.object(clipboard, "TerminalProxy") as proxy:
            with clipboard.launcher() as start:
                self.assertIs(start, subprocess.Popen)
            proxy.assert_not_called()

    def test_eligible_launcher_does_not_probe_the_desktop_on_start(self):
        """Automatic routing must not require a clipboard tool until the user pastes."""
        with mock.patch.object(clipboard, "TerminalProxy") as proxy, \
             mock.patch.object(clipboard, "read_image") as read, \
             mock.patch.object(clipboard.shutil, "which") as which:
            with clipboard.launcher("box") as start:
                start(["podman", "exec", "box", "codex"])
            proxy.assert_called_once_with("box")
            proxy.return_value.close.assert_called_once_with()
            read.assert_not_called()
            which.assert_not_called()


class ClipboardRoutingTests(IsolatedRuntimeTest):
    def test_launch_automatically_bridges_interactive_agents_including_no_tmux(self):
        """The bridge is invocation policy, independent of the detachable wrapper."""
        root = Path(self._state_tmp.name) / "project"
        root.mkdir()
        workspace = Workspace(root, root)
        cases = [(["codex"], True, ""), (["claude"], True, ""),
                 (["codex", "exec", "hi"], False, ""), (["claude", "-p", "hi"], False, ""),
                 (["claude", "--background"], False, ""), (["shell"], False, ""),
                 (["codex", "--model", "external-small"], True, "deepseek"),
                 (["claude", "--model", "external-small"], True, "deepseek")]
        for args, expected, api in cases:
            for tmux, stdin, stdout, nested in ((False, True, True, False), (True, True, True, False),
                    (False, False, True, False), (False, True, False, False), (True, True, True, True)):
                with self.subTest(args=args, api=api, tmux=tmux, stdin=stdin, stdout=stdout, nested=nested), ExitStack() as stack:
                    settings = external_config() if api else config.Config()
                    for name, value in (("resolve_workspace", workspace), ("load_config", settings),
                                        ("ensure_workspace_image", "sha256:test"), ("claude_channel_args", ([], [])),
                                        ("codex_command", ["codex"]), ("tmux_enabled", tmux), ("inside_box", nested),
                                        ("nested_launch_options", (LaunchOptions(tmux=tmux), [])),
                                        ("withhold_from_nested_box", [])):
                        stack.enter_context(mock.patch.object(runtime, name, return_value=value))
                    for name in ("ensure_state_dirs", "ensure_refresh_token", "announce_existing_sessions"):
                        stack.enter_context(mock.patch.object(runtime, name))
                    stack.enter_context(mock.patch.object(runtime.host_podman, "require"))
                    stack.enter_context(mock.patch.object(runtime.sys.stdin, "isatty", return_value=stdin))
                    stack.enter_context(mock.patch.object(runtime.sys.stdout, "isatty", return_value=stdout))
                    stack.enter_context(mock.patch.object(runtime.TmuxSession, "finish", return_value=0))
                    launch = stack.enter_context(mock.patch.object(runtime, "launch_in_workspace_container", return_value=0))
                    with self.assertRaises(SystemExit):
                        runtime.runtime_main(args, runtime.PACKAGE_ROOT, LaunchOptions(tmux=tmux), api_provider=api)
                    self.assertEqual(launch.call_args.kwargs.get("clipboard_input", False),
                                     expected and stdin and stdout and not nested)


class ClipboardPtyTests(unittest.TestCase):
    @contextmanager
    def proxy(self, child, *, terminal_closes=False, input_eof=False, terminal_stderr=False,
              stderr=subprocess.PIPE):
        """Two real PTYs exercise termios and process cleanup without Podman or a desktop."""
        master, slave = os.openpty()
        attributes = termios.tcgetattr(slave)
        flags = fcntl.fcntl(slave, fcntl.F_GETFL)
        script = '''
import os, sys
from agent_box import clipboard
clipboard.paste_image = lambda _: clipboard.PASTE_START + b"/tmp/synthetic.png" + clipboard.PASTE_END
if sys.argv[2] == "True":
    read = os.read
    clipboard.os.read = lambda fd, size: b"" if fd == 0 else read(fd, size)
before = len(os.listdir("/proc/self/fd"))
with clipboard.launcher("synthetic") as start:
    process = start([sys.executable, "-c", sys.argv[1]])
    result = process.wait()
assert len(os.listdir("/proc/self/fd")) == before
raise SystemExit(result)
'''
        process = subprocess.Popen([sys.executable, "-c", script, child, str(input_eof)], stdin=slave, stdout=slave,
                                   stderr=slave if terminal_stderr else stderr,
                                   preexec_fn=clipboard._child_terminal)
        try:
            yield process, master, slave
        finally:
            clipboard._stop(process)
            if not terminal_closes:
                self.assertEqual(termios.tcgetattr(slave), attributes)
                self.assertEqual(fcntl.fcntl(slave, fcntl.F_GETFL), flags)
            if process.stderr is not None:
                process.stderr.close()
            os.close(slave)
            if not terminal_closes:
                os.close(master)

    def receive(self, master, marker):
        result = bytearray()
        deadline = time.monotonic() + 5
        while marker not in result and time.monotonic() < deadline:
            if select.select([master], [], [], .1)[0]:
                result.extend(os.read(master, 65536))
        self.assertIn(marker, result)
        return bytes(result)

    def test_redirected_stderr_stays_separate_from_interactive_stdout(self):
        """Automatic proxying must preserve stderr redirection and its non-TTY identity."""
        child = '''
import os
os.write(2, b"STDERR:" + str(os.isatty(2)).encode())
os.write(1, b"STDOUT:" + str(os.isatty(0) and os.isatty(1)).encode() + b":DONE")
'''
        with self.proxy(child) as (process, master, _slave):
            output = self.receive(master, b":DONE")
            self.assertEqual(process.wait(timeout=3), 0)
            self.assertEqual(process.stderr.read(), b"STDERR:False")
            self.assertEqual(output, b"STDOUT:True:DONE")

    def test_terminal_stderr_shares_the_child_pty(self):
        """Interactive diagnostics must retain the same terminal as child stdout."""
        child = '''
import os
os.write(1, b"STDOUT:")
os.write(2, b"STDERR:" + str(os.isatty(2) and os.ttyname(2) == os.ttyname(1)).encode() + b":DONE")
'''
        with self.proxy(child, terminal_stderr=True) as (process, master, _slave):
            self.assertEqual(self.receive(master, b":DONE"), b"STDOUT:STDERR:True:DONE")
            self.assertEqual(process.wait(timeout=3), 0)

    def test_stderr_redirected_to_another_terminal_retains_its_destination(self):
        """A distinct terminal is a deliberate stderr destination, not proxy stdout."""
        error_master, error_slave = os.openpty()
        identity = os.fstat(error_slave)
        child = '''
import os
identity = os.fstat(2)
os.write(2, b"STDERR:" + str(os.isatty(2)).encode() + b":" +
         repr((identity.st_dev, identity.st_ino)).encode() + b":DONE")
os.write(1, b"STDOUT:" + str(os.isatty(0) and os.isatty(1)).encode() + b":DONE")
'''
        try:
            with self.proxy(child, stderr=error_slave) as (process, master, _slave):
                self.assertEqual(self.receive(master, b"STDOUT:True:DONE"), b"STDOUT:True:DONE")
                self.assertEqual(process.wait(timeout=3), 0)
                expected = b"STDERR:True:" + repr((identity.st_dev, identity.st_ino)).encode() + b":DONE"
                self.assertEqual(self.receive(error_master, b":DONE"), expected)
        finally:
            os.close(error_slave)
            os.close(error_master)

    def test_native_pty_paste_resize_exit_and_terminal_restoration(self):
        """Image framing, ordinary bytes and resize must survive both terminal layers."""
        child = '''
import fcntl, os, struct, termios, tty
tty.setraw(0)
os.write(1, b"READY")
data = b""
while not data.endswith(b"!"):
    data += os.read(0, 4096)
size = struct.unpack("HHHH", fcntl.ioctl(0, termios.TIOCGWINSZ, b"\\0"*8))[:2]
os.write(1, b"RESULT:" + data.hex().encode() + b":" + repr(size).encode() + b":DONE")
raise SystemExit(7)
'''
        with self.proxy(child) as (process, master, slave):
            self.receive(master, b"READY")
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 35, 111, 0, 0))
            # Signal delivery precedes input processing; no real clipboard is reachable.
            os.kill(process.pid, signal.SIGWINCH)
            os.write(master, b"abc\x16!")
            result = self.receive(master, b":DONE")
            expected = b"abc" + clipboard.PASTE_START + b"/tmp/synthetic.png" + clipboard.PASTE_END + b"!"
            self.assertIn(expected.hex().encode(), result)
            self.assertIn(b"(35, 111)", result)
            self.assertEqual(process.wait(timeout=3), 7, process.stderr.read().decode())

    def test_external_termination_reaps_only_its_child_and_restores_terminal(self):
        child = "import os,time; os.write(1, ('READY:'+str(os.getpid())+':END').encode()); time.sleep(30)"
        with self.proxy(child) as (process, master, _slave):
            output = self.receive(master, b":END")
            pid = int(output.split(b"READY:")[1].split(b":END")[0])
            os.kill(process.pid, signal.SIGTERM)
            self.assertEqual(process.wait(timeout=3), 143, process.stderr.read().decode())
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_closed_terminal_reaps_child_without_termios_traceback(self):
        """A disappearing outer PTY cannot prevent cleanup of our exec client."""
        child = "import os,time; os.write(1, ('READY:'+str(os.getpid())+':END').encode()); time.sleep(30)"
        with self.proxy(child, terminal_closes=True) as (process, master, _slave):
            output = self.receive(master, b":END")
            pid = int(output.split(b"READY:")[1].split(b":END")[0])
            os.close(master)
            self.assertEqual(process.wait(timeout=3), 129, process.stderr.read().decode())
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_input_eof_closes_exec_instead_of_spinning_on_readability(self):
        """A readable fd returning zero bytes is terminal loss, not more input to poll."""
        child = "import os,time; os.write(1, b'READY'); time.sleep(30)"
        with self.proxy(child, input_eof=True) as (process, master, _slave):
            self.receive(master, b"READY")
            os.write(master, b"x")
            self.assertEqual(process.wait(timeout=3), 129, process.stderr.read().decode())


if __name__ == "__main__":
    unittest.main()
