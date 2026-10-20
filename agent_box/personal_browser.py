"""Open the official extension's consent page on the local desktop."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
import sys
import threading
from urllib.parse import parse_qs, urlsplit

from .errors import AgentBoxError
from .paths import STATE_ROOT

SERVER = "agent_box_personal_browser"
EXTENSION = "mmlmfjhmonkocbjadbfplnigmagldckm"
STORE = f"https://chromewebstore.google.com/detail/playwright-extension/{EXTENSION}"

# The pinned MCP runtime invokes this as its browser executable. Only the
# connection URL crosses the socket; the host alone selects browser and profile.
OPENER = """#!/usr/bin/env node
const net = require('node:net');
const url = process.argv.at(-1);
const s = net.createConnection('/agent-box-opener/open.sock');
s.setTimeout(5000, () => s.destroy());
s.on('connect', () => s.end(JSON.stringify({url})));
s.on('error', () => process.exitCode = 1);
"""


def command(config):
    """Fail before image work when the selected desktop cannot grant consent."""
    from .runtime import inside_box
    if (inside_box() or any(os.environ.get(k) for k in
            ("SSH_CONNECTION", "SSH_TTY", "CODER_AGENT_URL", "CODER_WORKSPACE_NAME", "CODESPACES", "FLATPAK_ID", "SNAP", "AGENT_BOX_SYNC_EXECUTOR"))
            or not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))):
        raise AgentBoxError("personal browser needs a local graphical host session; "
                            "remote, nested and headless sessions are unsupported")
    choices = {"auto": ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser"),
               "chrome": ("google-chrome-stable", "google-chrome"),
               "chromium": ("chromium", "chromium-browser")}
    if config.browser not in choices:
        raise AgentBoxError("choose auto, chrome or chromium in Host > Personal browser")
    executable = next((p for name in choices[config.browser] if (p := shutil.which(name))), None)
    if not executable:
        raise AgentBoxError("desktop Chrome/Chromium not found; install it and its official "
                            f"Playwright extension: {STORE}")
    path = Path(executable).resolve()
    if any(part in {"snap", "flatpak"} for part in path.parts):
        raise AgentBoxError("Snap/Flatpak browsers are unsupported; select a native Chrome/Chromium installation")
    if config.profile and (config.profile in {".", ".."} or
                           any(c in config.profile for c in "/\\\0\n\r")):
        raise AgentBoxError("personal browser profile must be a directory name such as Default or Profile 2")
    return [str(path), *([f"--profile-directory={config.profile}"] if config.profile else [])]


def connection_url(value):
    """A fixed extension page and loopback relay cannot become a general opener."""
    if not isinstance(value, str) or len(value) > 8192 or any(ord(c) < 32 for c in value):
        raise ValueError("invalid extension connection URL")
    parsed = urlsplit(value)
    if (parsed.scheme != "chrome-extension" or parsed.netloc != EXTENSION
            or parsed.path != "/connect.html" or parsed.fragment):
        raise ValueError("not the official extension consent page")
    query = parse_qs(parsed.query, strict_parsing=True)
    if set(query) != {"mcpRelayUrl", "client", "protocolVersion"} or any(len(v) != 1 for v in query.values()):
        raise ValueError("unexpected connection parameters; approval bypass is disabled")
    relay = urlsplit(query["mcpRelayUrl"][0])
    if (relay.scheme != "ws" or relay.hostname not in {"localhost", "127.0.0.1", "::1"}
            or not relay.port or relay.username or relay.password or relay.query or relay.fragment
            or not re.fullmatch(r"/extension/[0-9a-f-]{36}", relay.path)):
        raise ValueError("extension relay must be a private loopback endpoint")
    if query["protocolVersion"][0] != "2":
        raise ValueError("unsupported extension protocol")
    return value


@contextmanager
def opener(argv, desktop):
    """Keep host-opening policy outside the workspace and live only with MCP."""
    if not desktop:
        yield argv
        return
    root = STATE_ROOT / "runtime/browser-openers"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix="session-", dir=root) as temporary:
        directory = Path(temporary)
        script = directory / "open"
        script.write_text(OPENER)
        script.chmod(0o700)
        listener = socket.socket(socket.AF_UNIX)
        # /proc/self/fd avoids AF_UNIX's pathname limit without process-wide chdir.
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            listener.bind(f"/proc/self/fd/{fd}/open.sock")
        finally:
            os.close(fd)
        (directory / "open.sock").chmod(0o600)
        listener.listen(1)
        listener.settimeout(.25)
        stopped = threading.Event()

        def serve():
            while not stopped.is_set():
                try:
                    connection, _ = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    return
                try:
                    with connection:
                        connection.settimeout(2)
                        data = b""
                        while chunk := connection.recv(4096):
                            data += chunk
                            if len(data) > 16384:
                                raise ValueError("oversized opener request")
                        url = connection_url(json.loads(data)["url"])
                        subprocess.Popen([*desktop, url], stdin=subprocess.DEVNULL,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                         start_new_session=True)
                except (OSError, ValueError, KeyError, TypeError):
                    # URLs contain transient capabilities and must not reach logs.
                    print("agent-box: personal browser could not open its consent page; "
                          "check the selected browser and installed extension", file=sys.stderr)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        # This directory is never mounted into the agent workspace.
        index = argv.index("run") + 1
        actual = [*argv[:index], "--mount",
                  f"type=bind,src={directory},dst=/agent-box-opener,ro=true", *argv[index:]]
        try:
            yield actual
        finally:
            stopped.set()
            listener.close()
            thread.join(timeout=3)
