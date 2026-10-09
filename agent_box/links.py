"""Web links leave a box through an opt-in host broker, never a desktop mount.

The image's xdg-open also works without the broker: displaying a URL is
enough for CLIs that check the opener before reading cached credentials.
Callback ports are temporary loopback listeners; raw streams travel through
podman exec, so neither rootless networking nor a published container port
decides whether the workstation can reach the CLI's own loopback.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import select
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from contextlib import suppress
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .atomicio import atomic_write_json, read_json
from .paths import LINKS_RUNTIME_DIR

SOCKET_ENV = "AGENT_BOX_LINKS_SOCKET"
CALLBACK_TTL = 300
MAX_REQUEST = 16384
MAX_RELAYS = 16
MAX_CONNECTIONS = 16
_children: dict[str, subprocess.Popen] = {}


def directory(workspace_id: str) -> Path:
    return LINKS_RUNTIME_DIR / workspace_id


def endpoint(workspace_id: str) -> Path:
    return directory(workspace_id) / "open.sock"


def context_file(workspace_id: str) -> Path:
    # Only the socket subdirectory is mounted; the box cannot change the
    # host-authored opening policy stored beside it.
    return LINKS_RUNTIME_DIR / f"{workspace_id}.json"


def web_url(url: str):
    if not isinstance(url, str) or len(url.encode()) > MAX_REQUEST // 2 or any(
            ord(c) < 33 or ord(c) == 127 for c in url):
        raise ValueError("expected one web URL without whitespace or control characters")
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.port == 0:
        raise ValueError("only HTTP(S) URLs are supported")
    if parts.username is not None or parts.password is not None:
        raise ValueError("URLs with embedded credentials are unsupported")
    return parts


def loopback_target(url: str) -> tuple[str, int] | None:
    parts = web_url(url)
    host = parts.hostname.lower()
    if host == "localhost":
        return host, parts.port or (443 if parts.scheme == "https" else 80)
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    if not address.is_loopback:
        return None
    # Use the literal origin: an IPv6 callback must reach the CLI's IPv6 socket.
    return str(address), parts.port or (443 if parts.scheme == "https" else 80)


def loopback_hosts(host: str) -> tuple[str, ...]:
    return ("127.0.0.1", "::1") if host == "localhost" else (str(ipaddress.ip_address(host)),)


def origin(host: str) -> str:
    return f"[{host}]" if ":" in host else host


def callback_target(url: str) -> tuple[str, int] | None:
    parts = web_url(url)
    redirects = parse_qs(parts.query).get("redirect_uri", [])
    if len(redirects) > 1:
        raise ValueError("multiple redirect_uri values are ambiguous")
    if redirects:
        return loopback_target(redirects[0])
    return loopback_target(url)


def _receive(connection: socket.socket) -> dict:
    data = bytearray()
    while b"\n" not in data:
        chunk = connection.recv(min(4096, MAX_REQUEST + 1 - len(data)))
        if not chunk:
            raise ValueError("incomplete link request")
        data.extend(chunk)
        if len(data) > MAX_REQUEST:
            raise ValueError("link request is too large")
    result = json.loads(data.split(b"\n", 1)[0])
    if not isinstance(result, dict):
        raise ValueError("expected a link request object")
    return result


def remote_context(config) -> dict:
    """Host-authored connection hints; the box never supplies a host command."""
    ssh = config.ssh_destination
    ssh_port = 22
    connection = os.environ.get("SSH_CONNECTION", "").split()
    if not ssh and len(connection) == 4:
        from .identity import detect_host_identity
        server, port = connection[2:]
        if port.isdigit():
            ssh = f"{detect_host_identity().name}@{server}"
            # Keep the port separate: an SSH destination is one argv item.
            ssh_port = int(port)
    coder = config.coder_workspace or os.environ.get("CODER_WORKSPACE_NAME", "")
    remote = bool(connection or ssh or coder or os.environ.get("SSH_TTY"))
    desktop = not remote and not Path("/run/.containerenv").exists() and bool(
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    return {"ssh": ssh, "ssh_port": ssh_port, "coder": coder, "desktop": desktop}


def instructions(context: dict, target: tuple[str, int] | None) -> list[str]:
    if not target:
        return []
    host, port = target
    addresses = [origin(address) for address in loopback_hosts(host)]
    lines = [f"Server loopback port {port} forwards to the box for {CALLBACK_TTL} seconds."]
    if context.get("coder"):
        # Coder's implicit IPv6 bind is best-effort. An explicit callback
        # address must fail on collision instead of reporting IPv4 readiness.
        command = ["coder", "port-forward", context["coder"]]
        for address in addresses:
            command += ["--tcp", f"{address}:{port}:{port}"]
        lines += ["On your workstation, run before opening the link:", shlex.join(command)]
    elif context.get("ssh"):
        command = ["ssh", "-N", "-o", "ExitOnForwardFailure=yes"]
        if context.get("ssh_port", 22) != 22:
            command += ["-p", str(context["ssh_port"])]
        for address in addresses:
            command += ["-L", f"{address}:{port}:{address}:{port}"]
        command.append(context["ssh"])
        lines += ["On your workstation, run before opening the link:", shlex.join(command)]
    elif not context.get("desktop"):
        command = ["ssh", "-N", "-o", "ExitOnForwardFailure=yes"]
        for address in addresses:
            command += ["-L", f"{address}:{port}:{address}:{port}"]
        lines += ["If this launcher is remote, on your workstation run:",
                  shlex.join([*command, "USER@SERVER"]),
                  "Set box.links.ssh_destination to your SSH alias for an exact command."]
    return lines


def open_link(url: str) -> int:
    target = callback_target(url)
    path = os.environ.get(SOCKET_ENV, "")
    lines = []
    if path:
        try:
            with socket.socket(socket.AF_UNIX) as connection:
                connection.settimeout(15)
                connection.connect(path)
                connection.sendall(json.dumps({"url": url}).encode() + b"\n")
                result = _receive(connection)
            if not result.get("ok"):
                raise ValueError(result.get("error", "host link broker refused the request"))
            lines = result.get("lines", [])
        except (OSError, ValueError) as exc:
            print(f"xdg-open: host link connection failed ({exc}); no callback route prepared", file=sys.stderr)
            return 1
    else:
        if target:
            lines += [f"This link needs workstation loopback port {target[1]} to reach the box.",
                      "Start agent-box --links to prepare the host route; for a remote host, "
                      "also use the displayed SSH/Coder tunnel."]
        lines.append("Host link integration is off; no browser or forwarding was started.")
    print("\n".join([*lines, "Open in your browser:", url]), file=sys.stderr)
    return 0


def connect_stream(host: str, port: int) -> int:
    """Container-side pipe endpoint; no shell or general host command protocol."""
    if loopback_target(f"http://{origin(host)}:{port}") is None:
        raise ValueError("relay destination must be loopback")
    with socket.create_connection((host, port), timeout=10) as connection:
        connection.settimeout(CALLBACK_TTL)
        incoming = sys.stdin.buffer
        while True:
            readers, _, _ = select.select([connection, incoming], [], [], CALLBACK_TTL)
            if not readers:
                return 1
            if connection in readers:
                chunk = connection.recv(65536)
                if not chunk:
                    return 0
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
            if incoming in readers:
                chunk = os.read(incoming.fileno(), 65536)
                if not chunk:
                    connection.shutdown(socket.SHUT_WR)
                    incoming = None
                    # Once stdin ends only the server's reply remains.
                    while True:
                        chunk = connection.recv(65536)
                        if not chunk:
                            return 0
                        sys.stdout.buffer.write(chunk)
                        sys.stdout.buffer.flush()
                connection.sendall(chunk)


class Relay:
    def __init__(self, container: str, target: tuple[str, int], listen_host: str | None = None):
        self.container, self.target = container, target
        self.deadline = time.monotonic() + CALLBACK_TTL
        self.stop = threading.Event()
        self.slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self.workers = set()
        self.worker_lock = threading.Lock()
        self.listeners = []
        try:
            for address in (loopback_hosts(listen_host) if listen_host else loopback_hosts(target[0])):
                family = socket.AF_INET6 if ipaddress.ip_address(address).version == 6 else socket.AF_INET
                listener = socket.socket(family)
                self.listeners.append(listener)
                # TIME_WAIT must not block a fresh login; active listeners
                # still refuse the bind. Both localhost families are atomic.
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if family == socket.AF_INET6:
                    listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                listener.bind((address, target[1]))
                listener.listen(MAX_CONNECTIONS)
                listener.setblocking(False)
        except BaseException:
            for listener in self.listeners:
                listener.close()
            raise
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        try:
            while not self.stop.is_set() and time.monotonic() < self.deadline:
                try:
                    ready, _, _ = select.select(self.listeners, [], [], 0.2)
                except (OSError, ValueError):
                    break
                for listener in ready:
                    try:
                        client, _ = listener.accept()
                    except OSError:
                        continue
                    if self.slots.acquire(blocking=False):
                        worker = threading.Thread(target=self.exchange, args=(client,), daemon=True)
                        with self.worker_lock:
                            self.workers.add(worker)
                        worker.start()
                    else:
                        client.close()
        finally:
            for listener in self.listeners:
                listener.close()

    def exchange(self, client):
        process = None
        try:
            host, port = self.target
            process = subprocess.Popen(
                ["podman", "exec", "-i", self.container, "agent-box", "links", "connect", host, str(port)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
            client.setblocking(False)
            os.set_blocking(process.stdin.fileno(), False)
            os.set_blocking(process.stdout.fileno(), False)
            to_box, to_client = bytearray(), bytearray()
            client_eof = box_eof = False
            while not self.stop.is_set() and time.monotonic() < self.deadline:
                readers = ([client] if not client_eof and len(to_box) < 65536 else [])
                if not box_eof and len(to_client) < 65536:
                    readers.append(process.stdout)
                writers = ([client] if to_client else [])
                if to_box:
                    writers.append(process.stdin)
                readable, writable, _ = select.select(readers, writers, [], 0.2)
                if client in readable:
                    chunk = client.recv(65536 - len(to_box))
                    if not chunk:
                        client_eof = True
                    else:
                        to_box.extend(chunk)
                if process.stdout in readable:
                    chunk = os.read(process.stdout.fileno(), 65536 - len(to_client))
                    if not chunk:
                        box_eof = True
                    else:
                        to_client.extend(chunk)
                if process.stdin in writable:
                    sent = os.write(process.stdin.fileno(), to_box)
                    del to_box[:sent]
                if client in writable:
                    sent = client.send(to_client)
                    del to_client[:sent]
                if client_eof and not to_box and not process.stdin.closed:
                    process.stdin.close()
                if box_eof and not to_client:
                    break
        except (OSError, subprocess.SubprocessError):
            pass
        finally:
            client.close()
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                for stream in (process.stdin, process.stdout):
                    with suppress(OSError):
                        stream.close()
            self.slots.release()
            with self.worker_lock:
                self.workers.discard(threading.current_thread())

    def close(self):
        self.stop.set()
        for listener in self.listeners:
            listener.close()
        self.thread.join(timeout=1)
        deadline = time.monotonic() + 3
        with self.worker_lock:
            workers = tuple(self.workers)
        for worker in workers:
            worker.join(timeout=max(0, deadline - time.monotonic()))


class Broker:
    def __init__(self, container: str, context: dict, open_browser: bool, context_path: Path | None = None):
        self.container, self.context, self.open_browser = container, context, open_browser
        self.context_path = context_path
        self.relays = {}

    def handle(self, request: dict) -> dict:
        if set(request) == {"ping"} and request["ping"] is True:
            return {"ok": True, "container": self.container}
        if set(request) != {"url"}:
            raise ValueError("expected only a URL")
        if self.context_path is not None:
            self.context = read_json(self.context_path)
        url = request["url"]
        target = callback_target(url)
        for key, relay in list(self.relays.items()):
            if time.monotonic() >= relay.deadline:
                relay.close()
                del self.relays[key]
        if target and target not in self.relays:
            if len(self.relays) >= MAX_RELAYS:
                raise ValueError("too many pending callback ports; wait for their expiry")
            try:
                listen_host = "127.0.0.1" if self.context.get("coder") and target[0] != "localhost" else None
                self.relays[target] = Relay(self.container, target, listen_host)
            except OSError:
                raise ValueError(f"host loopback port {target[1]} is unavailable; "
                                 "close its listener or retry the CLI with another callback port") from None
        elif target:
            self.relays[target].deadline = time.monotonic() + CALLBACK_TTL
        lines = instructions(self.context, target)
        if self.open_browser and self.context.get("desktop"):
            opener = shutil.which("xdg-open")
            if opener:
                subprocess.Popen([opener, url], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
                lines.append("Requested opening in the host browser.")
            else:
                lines.append("Host xdg-open is unavailable; open the link manually.")
        return {"ok": True, "lines": lines}

    def close(self):
        for relay in self.relays.values():
            relay.stop.set()
        for relay in self.relays.values():
            relay.close()


def _alive(container: str) -> bool:
    result = subprocess.run(["podman", "inspect", "--format", "{{.State.Running}}", container],
                            capture_output=True, text=True, timeout=10)
    return result.returncode == 0 and result.stdout.strip() == "true"


def serve(path: Path, container: str, context_path: Path, open_browser: bool) -> None:
    broker = Broker(container, read_json(context_path), open_browser, context_path)
    stopped = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopped.set())
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(path))
        os.chmod(path, 0o600)
        listener.listen(8)
        listener.settimeout(1)
        check = time.monotonic() + 2
        try:
            while not stopped.is_set():
                if time.monotonic() >= check:
                    if not _alive(container):
                        break
                    check = time.monotonic() + 2
                try:
                    connection, _ = listener.accept()
                except socket.timeout:
                    continue
                with connection:
                    connection.settimeout(5)
                    try:
                        request = _receive(connection)
                        if request == {"stop": True}:
                            stopped.set()
                            result = {"ok": True}
                        else:
                            result = broker.handle(request)
                    except (OSError, ValueError, TypeError) as exc:
                        result = {"ok": False, "error": str(exc)}
                    with suppress(OSError):
                        connection.sendall(json.dumps(result).encode() + b"\n")
        finally:
            broker.close()
            context_path.unlink(missing_ok=True)
            path.unlink(missing_ok=True)


def ping(path: Path, container: str) -> bool:
    try:
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(0.5)
            connection.connect(str(path))
            connection.sendall(b'{"ping":true}\n')
            result = _receive(connection)
        return result.get("ok") is True and result.get("container") == container
    except (OSError, ValueError):
        return False


def private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, exist_ok=True)
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if os.fstat(descriptor).st_uid != os.getuid():
            raise ValueError(f"host link directory belongs to another user: {path}")
        os.fchmod(descriptor, 0o700)
    finally:
        os.close(descriptor)


def prepare(workspace_id: str) -> None:
    private_directory(LINKS_RUNTIME_DIR)
    private_directory(directory(workspace_id))
    # A recreated box must not inherit an old broker's host-opening policy.
    stop(workspace_id)


def stop(workspace_id: str) -> None:
    path = endpoint(workspace_id)
    try:
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(1)
            connection.connect(str(path))
            connection.sendall(b'{"stop":true}\n')
            _receive(connection)
    except (ConnectionRefusedError, FileNotFoundError):
        path.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass
    deadline = time.monotonic() + 6
    while path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    if path.exists():
        raise ValueError("host link broker did not stop; retry after its pending requests finish")
    context_file(workspace_id).unlink(missing_ok=True)
    process = _children.pop(workspace_id, None)
    if process is not None:
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=2)


def ensure(workspace_id: str, container: str, config) -> None:
    private_directory(LINKS_RUNTIME_DIR)
    path = endpoint(workspace_id)
    current = remote_context(config)
    context_path = context_file(workspace_id)
    try:
        previous = read_json(context_path)
    except FileNotFoundError:
        previous = None
    # The socket is shared by all sessions. Once a remote/headless session
    # joins, no later local launch may reopen the server's browser for it.
    # The caller holds the workspace lock, so concurrent launches retain this
    # conservative policy until the box is stopped/recreated.
    if previous and not previous["desktop"]:
        if current["desktop"]:
            current = previous
        else:
            if not current["ssh"]:
                current["ssh"], current["ssh_port"] = previous["ssh"], previous["ssh_port"]
            current["coder"] = current["coder"] or previous["coder"]
    current["policy"] = config.to_dict()
    atomic_write_json(context_path, current, 0o600)
    if ping(path, container):
        return
    # A busy broker may not answer a ping immediately. Replacing its socket
    # would strand existing callback routes and launch a competing broker.
    if path.exists():
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(0.5)
            try:
                connection.connect(str(path))
            except (ConnectionRefusedError, FileNotFoundError):
                pass
            else:
                raise ValueError("host link broker is busy or belongs to another box; retry or run agent-box stop")
    path.unlink(missing_ok=True)
    process = subprocess.Popen(
        [sys.executable, "-m", "agent_box.links", "serve", str(path), container,
         str(context_path), "1" if config.open_browser else "0"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, cwd=str(Path(__file__).resolve().parent.parent),
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if ping(path, container):
            _children[workspace_id] = process
            return
        if process.poll() is not None:
            break
        time.sleep(0.05)
    process.terminate()
    process.wait(timeout=3)
    raise ValueError("host link broker could not start; launch without --links to display URLs only")


def reattach(workspace_id: str, container: str) -> None:
    """An attachment retains box policy while refreshing its host connection."""
    try:
        context = read_json(context_file(workspace_id))
    except FileNotFoundError:
        if endpoint(workspace_id).exists():
            raise ValueError("host link policy is missing; restart the workspace box") from None
        return
    if "policy" not in context:
        raise ValueError("host link policy is missing; restart the workspace box")
    from .config import LinksConfig
    ensure(workspace_id, container, LinksConfig.from_dict(context["policy"]))


def main(args: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Open web links from a workspace box")
    sub = parser.add_subparsers(dest="action", required=True)
    opened = sub.add_parser("open", help="Display a web URL; use the enabled host broker")
    opened.add_argument("url")
    connected = sub.add_parser("connect", help=argparse.SUPPRESS)
    connected.add_argument("host")
    connected.add_argument("port", type=int)
    server = sub.add_parser("serve", help=argparse.SUPPRESS)
    server.add_argument("path", type=Path)
    server.add_argument("container")
    server.add_argument("context", type=Path)
    server.add_argument("open_browser", choices=("0", "1"))
    options = parser.parse_args(args)
    try:
        if options.action == "open":
            return open_link(options.url)
        if options.action == "connect":
            return connect_stream(options.host, options.port)
        serve(options.path, options.container, options.context, options.open_browser == "1")
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"agent-box links: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
