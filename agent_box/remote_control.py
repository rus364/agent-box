"""Opt-in session supervision; unsupported launches retain the native CLI."""
from __future__ import annotations

import base64
import contextlib
import hashlib
import fcntl
import json
import os
import select
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .errors import AgentBoxError
from .paths import CODEX_REMOTE_LOCK, SESSION_TEMP_ROOT, expand_user

START_TIMEOUT = 10
MAX_REPLY = 8 * 1024 * 1024


class Unavailable(Exception):
    """A public, static reason to keep the original local launch."""


class Interrupted(BaseException):
    def __init__(self, signum: int):
        self.signum = signum


def notice(reason: str) -> None:
    print(f"agent-box: automatic Remote Control skipped: {reason}; using the ordinary launch", file=sys.stderr)


def options(command: list[str]) -> tuple[list[str], list[str]]:
    args = command[1:]
    boundary = args.index("--") if "--" in args else len(args)
    return args[:boundary], args[boundary:]


def claude_command(command: list[str]) -> list[str]:
    """Leave settings and variadic integration arguments in the native parser."""
    args, tail = options(command)
    if any(arg.split("=", 1)[0] in {"--remote-control", "--rc"} for arg in args):
        return command
    if any(arg.split("=", 1)[0] in {"--resume", "-r", "--continue", "-c", "--fork-session"} for arg in args):
        raise Unavailable("resumed Claude sessions retain native Remote Control behavior")
    if any(arg.split("=", 1)[0] == "--settings" for arg in args):
        raise Unavailable("explicit Claude settings retain native launch behavior")
    if (os.environ.get("ANTHROPIC_BASE_URL", "").rstrip("/") not in {"", "https://api.anthropic.com"}
            or any(os.environ.get(key) for key in (
                "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
                "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"))):
        raise Unavailable("Claude endpoint/authentication is not eligible")
    # The trailing optional-name flag cannot consume the user's prompt, and it
    # terminates --channels/--mcp-config values without reordering their contents.
    return [command[0], *args, "--remote-control", *tail]


def claude_preflight(executable: str) -> None:
    help_result = subprocess.run([executable, "--help"], stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, timeout=5)
    if help_result.returncode or b"--remote-control" not in help_result.stdout:
        raise Unavailable("installed Claude does not advertise --remote-control")
    result = subprocess.run([executable, "auth", "status", "--json"], stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, timeout=5)
    account = json.loads(result.stdout)
    if (result.returncode or not isinstance(account, dict) or not account.get("loggedIn")
            or account.get("authMethod") != "claude.ai" or account.get("apiProvider") != "firstParty"):
        raise Unavailable("Claude Remote Control requires first-party claude.ai login")


def codex_commands(command: list[str]) -> tuple[list[str], list[str], Path]:
    """Give server-owned integrations every -c; preserve native flag precedence."""
    args, tail = options(command)
    client, config, typed = [], [], []
    cwd = Path.cwd()
    takes_value = {"-c", "--config", "-m", "--model", "-C", "--cd", "-s", "--sandbox",
                   "-a", "--ask-for-approval", "-i", "--image", "--enable", "--disable"}
    switches = {"--search", "--full-auto", "--dangerously-bypass-approvals-and-sandbox",
                "--no-alt-screen"}
    incompatible = {"-p", "--profile", "--remote", "--remote-auth-token-env", "--oss",
                    "--local-provider", "--worktree", "-w", "--add-dir"}
    index = 0
    while index < len(args):
        arg = args[index]
        key, equal, value = arg.partition("=")
        if key in incompatible or any(arg.startswith(short) and arg != short for short in ("-p", "-w")):
            raise Unavailable("this Codex profile/provider/remote/workspace option needs the native launcher")
        if not arg.startswith("-"):
            if arg in {"resume", "fork"}:
                raise Unavailable("resumed/forked Codex sessions retain native launch behavior")
            client.append(arg)
            index += 1
            continue
        if key not in takes_value and key not in switches:
            raise Unavailable("a Codex option is not supported by the Remote Control launcher")
        consumed = [arg]
        if key in takes_value and not equal:
            index += 1
            if index == len(args):
                raise Unavailable("incomplete Codex option")
            value = args[index]
            consumed.append(value)
        if key in {"-C", "--cd"}:
            cwd = expand_user(value)
            if not cwd.is_absolute():
                cwd = Path.cwd() / cwd
        else:
            client.extend(consumed)
        if key in {"-c", "--config"}:
            config += ["-c", value]
        elif key in {"--enable", "--disable"}:
            config += ["-c", f"features.{json.dumps(value)}={'true' if key == '--enable' else 'false'}"]
        elif key in {"-m", "--model", "-s", "--sandbox", "-a", "--ask-for-approval"}:
            setting = {"-m": "model", "--model": "model", "-s": "sandbox_mode", "--sandbox": "sandbox_mode",
                       "-a": "approval_policy", "--ask-for-approval": "approval_policy"}[key]
            typed += ["-c", f"{setting}={json.dumps(value)}"]
        elif key == "--dangerously-bypass-approvals-and-sandbox":
            typed += ["-c", 'sandbox_mode="danger-full-access"', "-c", 'approval_policy="never"']
        elif key == "--full-auto":
            typed += ["-c", 'sandbox_mode="workspace-write"', "-c", 'approval_policy="on-request"']
        elif key == "--search":
            typed += ["-c", 'web_search="live"']
        index += 1
    if not cwd.is_dir():
        raise Unavailable("Codex working directory is unavailable")
    return [command[0], *config, *typed, "app-server"], [command[0], *client, "--cd", str(cwd), *tail], cwd


@contextlib.contextmanager
def codex_slot():
    """Never unlink a flock inode: another box may already have it open."""
    fd = os.open(CODEX_REMOTE_LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise Unavailable("Codex Remote Control lock is not an owned regular file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Unavailable("another agent-box Codex Remote Control session is active") from None
        yield fd
    finally:
        os.close(fd)


def stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


class Rpc:
    """Bounded JSON-line requests over the provider's local transport."""
    def __init__(self, reader, write):
        self.reader, self.write = reader, write
        self.buffer = b""
        self.sequence = 0

    def call(self, method: str, params: dict) -> dict:
        self.sequence += 1
        self.write((json.dumps({"id": self.sequence, "method": method, "params": params}) + "\n").encode())
        deadline = time.monotonic() + START_TIMEOUT
        while True:
            reply = self.read_message(deadline)
            if not isinstance(reply, dict):
                raise Unavailable("Codex app-server returned an invalid message")
            if reply.get("id") != self.sequence:
                if time.monotonic() >= deadline:
                    raise Unavailable("Codex app-server did not answer in time")
                continue
            if "error" in reply or not isinstance(reply.get("result"), dict):
                raise Unavailable("installed Codex does not support the required app-server request")
            return reply["result"]

    def read_message(self, deadline: float) -> dict:
        while b"\n" not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.reader], [], [], remaining)[0]:
                raise Unavailable("Codex app-server did not answer in time")
            chunk = os.read(self.reader.fileno(), 65536)
            if not chunk:
                raise Unavailable("Codex app-server closed its connection")
            self.buffer += chunk
            if len(self.buffer) > MAX_REPLY:
                raise Unavailable("Codex app-server reply exceeded the local limit")
        line, self.buffer = self.buffer.split(b"\n", 1)
        return json.loads(line)

    def initialize(self) -> None:
        self.call("initialize", {"clientInfo": {"name": "agent_box", "version": "1"},
                                 "capabilities": {"experimentalApi": True}})
        self.write(b'{"method":"initialized","params":{}}\n')


class UnixRpc(Rpc):
    """Codex Unix sockets carry WebSocket frames, unlike its stdio transport."""
    def __init__(self, connection: socket.socket):
        super().__init__(connection, self.send_text)
        key = base64.b64encode(os.urandom(16)).decode()
        connection.sendall((f"GET /rpc HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                            f"Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
                            f"Sec-WebSocket-Key: {key}\r\n\r\n").encode())
        deadline = time.monotonic() + START_TIMEOUT
        header = b""
        while b"\r\n\r\n" not in header:
            header += self.read_exact(1, deadline)
            if len(header) > 8192:
                raise Unavailable("Codex WebSocket handshake exceeded the local limit")
        lines = header.decode("ascii").split("\r\n")
        headers = dict(line.lower().split(":", 1) for line in lines[1:] if ":" in line)
        expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        # Header names are case insensitive; the accept value is not.
        accept = next((line.split(":", 1)[1].strip() for line in lines[1:]
                       if line.lower().startswith("sec-websocket-accept:")), "")
        if (not lines[0].startswith("HTTP/1.1 101 ") or accept != expected
                or headers.get("upgrade", "").strip() != "websocket"):
            raise Unavailable("Codex app-server WebSocket handshake failed")

    def read_exact(self, size: int, deadline: float) -> bytes:
        data = b""
        while len(data) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Unavailable("Codex app-server did not answer in time")
            self.reader.settimeout(remaining)
            chunk = self.reader.recv(size - len(data))
            if not chunk:
                raise Unavailable("Codex app-server closed its connection")
            data += chunk
        return data

    def send_frame(self, data: bytes, opcode: int) -> None:
        mask = os.urandom(4)
        size = len(data)
        length = bytes([size | 128]) if size < 126 else (
            bytes([126 | 128]) + size.to_bytes(2, "big") if size < 65536 else
            bytes([127 | 128]) + size.to_bytes(8, "big"))
        self.reader.sendall(bytes([128 | opcode]) + length + mask
                            + bytes(byte ^ mask[index % 4] for index, byte in enumerate(data)))

    def send_text(self, data: bytes) -> None:
        self.reader.settimeout(START_TIMEOUT)
        self.send_frame(data, 1)

    def read_message(self, deadline: float) -> dict:
        message = bytearray()
        started = False
        while True:
            first, second = self.read_exact(2, deadline)
            opcode, size = first & 15, second & 127
            if first & 112 or second & 128:
                raise Unavailable("unsupported Codex WebSocket frame")
            if size in (126, 127):
                size = int.from_bytes(self.read_exact(2 if size == 126 else 8, deadline), "big")
            if size + len(message) > MAX_REPLY:
                raise Unavailable("Codex app-server reply exceeded the local limit")
            payload = self.read_exact(size, deadline)
            if opcode in (9, 10):
                if size > 125 or not first & 128:
                    raise Unavailable("invalid Codex WebSocket control frame")
                if opcode == 9:
                    self.send_frame(payload, 10)
                continue
            if opcode not in (0, 1) or (opcode == 0) != started:
                raise Unavailable("Codex app-server closed or changed its WebSocket protocol")
            started = True
            message.extend(payload)
            if first & 128:
                return json.loads(message)


def preflight(server: list[str], cwd: Path, log) -> None:
    """Ask Codex for effective layered config/auth without enrolling in RC."""
    env = dict(os.environ, CODEX_INTERNAL_APP_SERVER_REMOTE_CONTROL_DISABLED="1")
    process = subprocess.Popen([*server, "--stdio"], cwd=cwd, env=env,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log)
    try:
        def write(data):
            process.stdin.write(data)
            process.stdin.flush()
        rpc = Rpc(process.stdout, write)
        rpc.initialize()
        config = rpc.call("config/read", {"includeLayers": False})["config"]
        if not isinstance(config, dict):
            raise Unavailable("Codex app-server returned invalid configuration")
        if config.get("model_provider", "openai") not in (None, "openai"):
            raise Unavailable("custom Codex providers run without automatic Remote Control")
        providers = config.get("model_providers") or {}
        if not isinstance(providers, dict):
            raise Unavailable("Codex app-server returned invalid provider configuration")
        openai = providers.get("openai") or {}
        if not isinstance(openai, dict):
            raise Unavailable("Codex app-server returned invalid provider configuration")
        if openai.get("base_url") or os.environ.get("OPENAI_BASE_URL"):
            raise Unavailable("custom Codex endpoints run without automatic Remote Control")
        account = rpc.call("account/read", {"refreshToken": False}).get("account")
        if not isinstance(account, dict) or account.get("type") != "chatgpt":
            raise Unavailable("Codex Remote Control requires ChatGPT login")
    finally:
        stop(process)
        process.stdin.close()
        process.stdout.close()


def wait_for_client(command: list[str], server: subprocess.Popen | None = None) -> int:
    process = subprocess.Popen(command)
    try:
        if server is None:
            return process.wait()
        while process.poll() is None:
            if server is not None and server.poll() is not None:
                print("agent-box: Codex app-server stopped; ending its terminal session", file=sys.stderr)
                return 1
            try:
                return process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                pass
        return process.returncode
    finally:
        stop(process)


def run_codex(command: list[str]) -> int:
    client_started = False
    try:
        server, client, cwd = codex_commands(command)
        with codex_slot() as lock, tempfile.TemporaryDirectory(prefix="ab-rc-", dir=SESSION_TEMP_ROOT) as temp:
            directory = Path(temp)
            # Never expose provider stderr/config replies: they can contain URLs,
            # user configuration or credentials. The file is private and transient.
            with (directory / "server.log").open("w+b") as log:
                preflight(server, cwd, log)
                endpoint = directory / "server.sock"
                process = subprocess.Popen([*server, "--listen", f"unix://{endpoint}", "--remote-control"],
                                           cwd=cwd, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                           start_new_session=True, pass_fds=(lock,))
                try:
                    deadline = time.monotonic() + START_TIMEOUT
                    while not endpoint.exists():
                        if process.poll() is not None or time.monotonic() >= deadline:
                            raise Unavailable("Codex Remote Control server could not start")
                        time.sleep(0.05)
                    with socket.socket(socket.AF_UNIX) as connection:
                        connection.settimeout(START_TIMEOUT)
                        connection.connect(str(endpoint))
                        rpc = UnixRpc(connection)
                        rpc.initialize()
                        status = rpc.call("remoteControl/status/read", {})
                        if status.get("status") not in {"connecting", "connected"}:
                            raise Unavailable("Codex Remote Control is unavailable for this account/configuration")
                        pairing = rpc.call("remoteControl/pairing/start", {"manualCode": True})
                        code = pairing.get("manualPairingCode")
                        if not isinstance(code, str) or not code or len(code) > 256 or not code.isprintable():
                            raise Unavailable("Codex did not provide a manual pairing code")
                        print(f"agent-box: Codex Remote Control pairing code: {code}", file=sys.stderr)
                    args, tail = options(client)
                    client = [client[0], *args, "--remote", f"unix://{endpoint}", *tail]
                    print("agent-box: Remote Control requested; pair in the Codex app while this session is running", file=sys.stderr)
                    client_started = True
                    return wait_for_client(client, process)
                finally:
                    stop(process)
    except (Unavailable, OSError, ValueError, KeyError) as exc:
        if client_started:
            raise
        notice(str(exc) if isinstance(exc, Unavailable) else "Codex Remote Control startup failed")
    return wait_for_client(command)


def main(arguments: list[str]) -> int:
    if len(arguments) < 2 or arguments[0] not in {"claude", "codex"}:
        raise AgentBoxError("Internal session launcher requires PROVIDER COMMAND [ARGS]")
    provider, command = arguments[0], arguments[1:]
    previous = {}
    try:
        # The child shares the foreground group and receives terminal Ctrl-C.
        previous[signal.SIGINT] = signal.signal(signal.SIGINT, lambda *_: None)
        def interrupted(number, frame):
            raise Interrupted(number)
        for signum in (signal.SIGTERM, signal.SIGHUP):
            previous[signum] = signal.signal(signum, interrupted)
        if provider == "codex":
            return run_codex(command)
        try:
            candidate = claude_command(command)
            if candidate != command:
                claude_preflight(command[0])
            command = candidate
        except (Unavailable, OSError, ValueError, subprocess.TimeoutExpired) as exc:
            notice(str(exc) if isinstance(exc, Unavailable) else "Claude capability check failed")
        return wait_for_client(command)
    except Interrupted as exc:
        return 128 + exc.signum
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
