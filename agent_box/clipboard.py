"""Explicit terminal gestures bridge PNG bytes, never a clipboard capability."""
from __future__ import annotations

import errno
import fcntl
import os
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import termios
import time
import tty
import uuid
from contextlib import contextmanager

MAX_IMAGE = 16 * 1024 * 1024
MAX_STORED = 128 * 1024 * 1024
MAX_FILES = 32
READ_TIMEOUT = 3.0
TRANSFER_TIMEOUT = 5.0
PNG = b"\x89PNG\r\n\x1a\n"
PASTE_START, PASTE_END = b"\x1b[200~", b"\x1b[201~"


class ClipboardError(Exception):
    """A failed gesture is a diagnostic, not a failed agent session."""


def _stop(process):
    # Every process here has its own session; never signal Podman's server or a tmux pane.
    # A reader may exit after forking a child that still holds our pipe open.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        pass
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def exchange(command, *, timeout, limit, data=b""):
    """Bound both pipes while keeping image bytes out of argv and host files."""
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, start_new_session=True)
    output = bytearray()
    pending = memoryview(data)
    deadline = time.monotonic() + timeout
    try:
        os.set_blocking(process.stdout.fileno(), False)
        os.set_blocking(process.stdin.fileno(), False)
        readable = [process.stdout]
        while readable or pending or process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ClipboardError("clipboard operation timed out")
            if not pending and not process.stdin.closed:
                process.stdin.close()
            readers, writers, _ = select.select(readable, [process.stdin] if pending else [], [], min(remaining, 0.05))
            if readers:
                chunk = os.read(process.stdout.fileno(), min(65536, limit + 1 - len(output)))
                if not chunk:
                    readable = []
                output.extend(chunk)
                if len(output) > limit:
                    raise ClipboardError("clipboard data exceeds the size limit")
            if writers:
                try:
                    pending = pending[os.write(process.stdin.fileno(), pending[:65536]):]
                except BrokenPipeError:
                    raise ClipboardError("image transfer was refused") from None
        if process.wait():
            raise ClipboardError("clipboard reader or image transfer failed (PNG unavailable or store full)")
        return bytes(output)
    finally:
        _stop(process)
        process.stdin.close()
        process.stdout.close()


def read_image():
    """Wayland and X11 selections are separate: failure must not switch sources."""
    if any(os.environ.get(key) for key in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY")):
        raise ClipboardError("image paste needs a local desktop terminal; SSH clipboard access is disabled")
    if os.environ.get("WAYLAND_DISPLAY"):
        tool, command = "wl-paste", ["wl-paste", "--no-newline", "--type", "image/png"]
    elif os.environ.get("DISPLAY"):
        tool, command = "xclip", ["xclip", "-selection", "clipboard", "-out", "-target", "image/png"]
    else:
        raise ClipboardError("image paste needs a Wayland or X11 desktop")
    if not shutil.which(tool):
        raise ClipboardError(f"image paste needs {tool} installed on the host")
    image = exchange(command, timeout=READ_TIMEOUT, limit=MAX_IMAGE)
    if len(image) < 33 or not image.startswith(PNG) or image[8:16] != b"\x00\x00\x00\x0dIHDR":
        raise ClipboardError("the clipboard does not contain a PNG image")
    width, height = struct.unpack(">II", image[16:24])
    if not width or not height or max(width, height) > 16384 or width * height > 64 * 1024 * 1024:
        raise ClipboardError("clipboard image dimensions exceed the limit")
    return image


# Sent as code to the box so this host feature also works with an already-running
# runtime. Limits apply across all clients and detach/attach, not just this proxy.
STORE_SCRIPT = r'''
import fcntl, os, signal, stat, sys
limit, quota, count = map(int, sys.argv[1:4])
name = sys.argv[4]
root = "/tmp/agent-box-clipboard-" + str(os.getuid())
signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(TimeoutError()))
signal.setitimer(signal.ITIMER_REAL, 4)
try:
    os.mkdir(root, 0o700)
except FileExistsError:
    pass
directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
partial = name + ".part"
created = False
try:
    info = os.fstat(directory)
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("unsafe image store")
    fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
    entries = os.listdir(directory)
    used = 0
    if len(entries) >= count:
        raise ValueError("image store full")
    for entry in entries:
        info = os.stat(entry, dir_fd=directory, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("unsafe image store entry")
        used += info.st_size
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    created = True
    with os.fdopen(fd, "wb") as target:
        size = 0
        while True:
            chunk = sys.stdin.buffer.read(min(65536, limit + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            if size > limit or used + size > quota:
                raise ValueError("image store full")
            target.write(chunk)
        if not size:
            raise ValueError("empty image")
    os.rename(partial, name, src_dir_fd=directory, dst_dir_fd=directory)
    created = False
    print(root + "/" + name, flush=True)
finally:
    if created:
        os.unlink(partial, dir_fd=directory)
    os.close(directory)
'''


def paste_image(container):
    image = read_image()
    name = uuid.uuid4().hex + ".png"
    result = exchange(["podman", "exec", "-i", container, "python3", "-c", STORE_SCRIPT,
                       str(MAX_IMAGE), str(MAX_STORED), str(MAX_FILES), name],
                      data=image, timeout=TRANSFER_TIMEOUT, limit=256)
    # Do not trust terminal control bytes from a modified container executable.
    if not re.fullmatch(rb"/tmp/agent-box-clipboard-[0-9]+/" + name.encode() + rb"\n", result):
        raise ClipboardError("image transfer returned an invalid path")
    return PASTE_START + result.rstrip(b"\n") + PASTE_END


class InputFilter:
    """Track paste framing across reads; pasted control bytes are never gestures."""

    def __init__(self, paste, diagnostic):
        self.paste, self.diagnostic = paste, diagnostic
        self.sequence = bytearray()
        self.sent = 0
        self.pasting = False
        self.tail = b""

    def flush(self):
        result = bytes(self.sequence[self.sent:])
        self.sent = len(self.sequence)
        return result

    def _gesture(self, original):
        try:
            return self.paste()
        except (ClipboardError, OSError) as exc:
            self.diagnostic(f"agent-box: {exc}; Ctrl+V forwarded")
            return original

    def feed(self, data):
        output = bytearray()
        for value in data:
            if self.pasting:
                output.append(value)
                self.tail = (self.tail + bytes([value]))[-len(PASTE_END):]
                if self.tail == PASTE_END:
                    self.pasting = False
                    self.tail = b""
            elif self.sequence:
                self.sequence.append(value)
                complete = (len(self.sequence) == 2 and value != ord("[")) or (
                    len(self.sequence) > 2 and (0x40 <= value <= 0x7e or len(self.sequence) >= 128))
                if complete:
                    sequence = bytes(self.sequence)
                    if sequence == PASTE_START:
                        self.pasting = True
                    gesture = re.fullmatch(rb"\x1b\[(?:118(?::[0-9:]+)?;5(?::[12])?u|27;5;118~)", sequence)
                    output.extend(self._gesture(sequence) if gesture and not self.sent else self.flush())
                    self.sequence.clear()
                    self.sent = 0
            elif value == 27:
                self.sequence.append(value)
            elif value == 22:
                output.extend(self._gesture(bytes([value])))
            else:
                output.append(value)
        return bytes(output)


def _child_terminal():
    os.setsid()
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


class TerminalProxy:
    """Own only the exec client and its PTY; a detached tmux server stays alive."""

    def __init__(self, container):
        self.container = container
        self.master = None
        self.process = None
        self.restore = []
        self.handlers = {}
        self.stopped = 0

    def start(self, command, *, env=None):
        self.input, self.output = sys.stdin.fileno(), sys.stdout.fileno()
        master, slave = os.openpty()
        self.master = master
        try:
            termios.tcsetattr(slave, termios.TCSANOW, termios.tcgetattr(self.input))
            fcntl.ioctl(slave, termios.TIOCSWINSZ, fcntl.ioctl(self.input, termios.TIOCGWINSZ, b"\0" * 8))
            self.process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave,
                                            env=env, preexec_fn=_child_terminal)
        finally:
            os.close(slave)
        return self

    def poll(self):
        return self.process.poll()

    def _resize(self, *_):
        try:
            fcntl.ioctl(self.master, termios.TIOCSWINSZ,
                        fcntl.ioctl(self.input, termios.TIOCGWINSZ, b"\0" * 8))
        except OSError:
            self.stopped = signal.SIGHUP

    def _signal(self, number, _frame):
        if number == signal.SIGINT:
            try:
                os.killpg(self.process.pid, number)
            except ProcessLookupError:
                pass
        else:
            self.stopped = number

    def wait(self):
        try:
            attributes = termios.tcgetattr(self.input)
            self.restore.append(lambda: termios.tcsetattr(self.input, termios.TCSANOW, attributes))
            tty.setraw(self.input)
        except (OSError, termios.error):
            return 128 + signal.SIGHUP
        for fd in (self.input, self.output, self.master):
            flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            self.restore.append(lambda fd=fd, flags=flags: fcntl.fcntl(fd, fcntl.F_SETFL, flags))
            os.set_blocking(fd, False)
        for number in (signal.SIGWINCH, signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
            self.handlers[number] = signal.signal(number, self._resize if number == signal.SIGWINCH else self._signal)
        outgoing, incoming = bytearray(), bytearray()
        parser = InputFilter(lambda: paste_image(self.container),
                             lambda message: outgoing.extend(("\r\n" + message + "\r\n").encode()))
        ended = None
        last_input = time.monotonic()
        try:
            while not self.stopped:
                if self.process.poll() is not None:
                    ended = ended or time.monotonic()
                    if time.monotonic() - ended > 0.5:
                        break
                readers = ([self.input] if len(incoming) < 65536 and ended is None else [])
                if len(outgoing) < 65536:
                    readers.append(self.master)
                writers = ([self.master] if incoming else []) + ([self.output] if outgoing else [])
                readable, writable, _ = select.select(readers, writers, [], 0.05)
                if time.monotonic() - last_input >= 0.05:
                    incoming.extend(parser.flush())
                for fd in readable:
                    try:
                        chunk = os.read(fd, 16384)
                    except OSError as exc:
                        if exc.errno != errno.EIO:
                            raise
                        chunk = b""
                    if not chunk:
                        if fd == self.master and outgoing:
                            # Drain the last screen before leaving the proxy.
                            self._drain(outgoing)
                        if fd == self.master:
                            try:
                                return self.process.wait(timeout=0.5)
                            except subprocess.TimeoutExpired:
                                pass
                        return 128 + signal.SIGHUP
                    if fd == self.input:
                        last_input = time.monotonic()
                        incoming.extend(parser.feed(chunk))
                    else:
                        outgoing.extend(chunk)
                for fd in writable:
                    buffer = incoming if fd == self.master else outgoing
                    try:
                        del buffer[:os.write(fd, buffer[:16384])]
                    except BlockingIOError:
                        pass
            return 128 + self.stopped if self.stopped else self.process.wait()
        except OSError:
            return 128 + signal.SIGHUP

    def _drain(self, buffer):
        deadline = time.monotonic() + 0.5
        while buffer and time.monotonic() < deadline:
            if select.select([], [self.output], [], 0.05)[1]:
                try:
                    del buffer[:os.write(self.output, buffer[:16384])]
                except BlockingIOError:
                    pass

    def close(self):
        if self.process is not None:
            _stop(self.process)
        for restore in reversed(self.restore):
            try:
                restore()
            except (OSError, termios.error):
                pass
        for number, handler in self.handlers.items():
            signal.signal(number, handler)
        if self.master is not None:
            os.close(self.master)


@contextmanager
def launcher(container=""):
    """The disabled route is exactly the existing subprocess launch."""
    if not container:
        yield subprocess.Popen
        return
    proxy = TerminalProxy(container)
    try:
        yield proxy.start
    finally:
        proxy.close()
