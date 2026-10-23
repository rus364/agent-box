"""Host-owned Playwright containers; workspace clients forward only MCP bytes."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid

from . import host_podman, runtime_bundle, personal_browser
from .errors import AgentBoxError
from .mounts import MountSpec, host_tmp_mounts, plan_mounts
from .paths import PACKAGE_ROOT, STATE_ROOT


IMAGE = "mcr.microsoft.com/playwright/mcp@sha256:690763cbb437ee7233f538a953bb5390bf03791eb5c965b471e59d17a6132fcb"
LABEL = "io.agent-box.browser"


def ensure_image():
    host_podman.require()
    result = subprocess.run(["podman", "image", "exists", IMAGE], capture_output=True, text=True)
    if result.returncode == 0:
        return
    if result.returncode != 1:
        raise AgentBoxError("cannot inspect the browser image: " + result.stderr.strip())
    print("agent-box: downloading the Playwright browser image (first use)...", file=sys.stderr)
    result = subprocess.run(["podman", "pull", IMAGE], stdout=sys.stderr)
    if result.returncode:
        raise AgentBoxError("browser image download failed; retry online or disable browsing with --no-browser / --no-personal-browser")


def container_command(directory, project, cwd, mounts, *, container="", workspace="", host_tmp=False, personal=False):
    """Host /tmp crosses this boundary only through the independent opt-in."""
    name = "agent-box-browser-" + uuid.uuid4().hex[:20]
    argv = ["podman", "run", "--rm", "-i", "--pull=never", "--name", name,
            "--label", f"{LABEL}=true", "--label", f"io.agent-box.browser.workspace={workspace}",
            "--label", f"io.agent-box.browser.mode={'workspace' if container else 'host'}",
            "--network=" + (f"container:{container}" if container and not personal else "host"),
            "--userns=keep-id:uid=1000,gid=1000", "--user=1000:1000",
            "--cap-drop=all", "--security-opt=no-new-privileges", "--shm-size=256m",
            "--workdir", str(cwd)]
    selected = plan_mounts([MountSpec(Path(project), False, "project"), *mounts, *host_tmp_mounts(host_tmp),
                            MountSpec(Path(directory) / "output", False, "browser output")])
    for mount in selected:
        if "," in str(mount.path):
            raise AgentBoxError("browser bind paths containing commas are unsupported")
        argv += ["--mount", f"type=bind,src={mount.path},dst={mount.path},ro={'true' if mount.read_only else 'false'}"]
    # MCP roots only describe the client's cwd; the container's bind set is
    # the boundary that also admits explicitly selected upload directories.
    mode = (["--extension", "--browser", "chromium", "--executable-path", "/agent-box-opener/open"]
            if personal else ["--isolated"])
    argv += [IMAGE, *mode, "--allow-unrestricted-file-access",
             "--output-dir", str(Path(directory) / "output")]
    return argv


def remove_container(argv):
    name = argv[argv.index("--name") + 1]
    subprocess.run(["podman", "rm", "--force", "--ignore", name],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)


def stop(workspace, mode):
    """Explicit workspace stop also recovers containers from a killed supervisor."""
    result = subprocess.run(["podman", "ps", "-aq", "--filter", f"label={LABEL}=true",
                             "--filter", f"label=io.agent-box.browser.workspace={workspace}",
                             "--filter", f"label=io.agent-box.browser.mode={mode}"],
                            capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise AgentBoxError("could not inspect browser containers: " + result.stderr.strip())
    ids = result.stdout.split()
    if ids:
        result = subprocess.run(["podman", "rm", "--force", "--ignore", *ids],
                                capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise AgentBoxError("could not stop browser containers: " + result.stderr.strip())


def process_identity(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return ""


def owner_alive(owner):
    if owner["identity"] and process_identity(owner["pid"]) == owner["identity"]:
        return True
    if owner["session"]:
        from .runtime import tmux_session_exists
        return tmux_session_exists(owner["container"], owner["session"])
    return False


def serve(directory, argv, owner, desktop=()):
    """Fixed launch policy is received at spawn, never from a workspace-writable file."""
    directory = Path(directory)
    os.chdir(directory)
    process = None

    def stop(_signum, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    try:
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind("mcp.sock")
            os.chmod("mcp.sock", 0o600)
            listener.listen(1)
            listener.settimeout(1)
            while owner_alive(owner):
                try:
                    connection, _ = listener.accept()
                except socket.timeout:
                    continue
                with connection, personal_browser.opener(argv, desktop) as actual:
                    process = subprocess.Popen(actual, stdin=connection, stdout=connection)
                    while process.poll() is None and owner_alive(owner):
                        time.sleep(.5)
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                    remove_container(argv)
                    process = None
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
        remove_container(argv)
        (directory / "mcp.sock").unlink(missing_ok=True)


def prepare(directory, project, cwd, mounts=(), *, container="", workspace="", session="", host_tmp=False, desktop=()):
    """Prepare one invocation; host stdio naturally follows the agent/tmux lifetime."""
    ensure_image()
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    (directory / "output").mkdir(mode=0o700, exist_ok=True)
    argv = container_command(directory, project, cwd, list(mounts), container=container, workspace=workspace, host_tmp=host_tmp, personal=bool(desktop))
    bundle = runtime_bundle.prepare(PACKAGE_ROOT, STATE_ROOT / "runtime/launcher", managed=True)
    # Keep the immutable package visible in argv after the broker changes cwd.
    launcher = [str(Path(sys.executable).resolve()), str(bundle / "__main__.py"), "_browser"]
    if not container:
        # The absolute package runner survives an installer replacing its old tree.
        command = [str(Path(sys.executable).resolve()), str(bundle / "__main__.py"), "_browser", "run", json.dumps(argv), json.dumps(desktop)]
    else:
        client = directory / "client.py"
        shutil.copyfile(PACKAGE_ROOT / "browser_client.py", client)
        command = ["python3", str(client), str(directory / "mcp.sock")]
        owner = {"pid": os.getpid(), "identity": process_identity(os.getpid()),
                 "container": container, "session": session}
        with (directory / "broker.log").open("ab") as log:
            child = subprocess.Popen([*launcher, "serve", str(directory), json.dumps(argv), json.dumps(owner), json.dumps(desktop)],
                                     stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                     start_new_session=True, cwd=bundle.parent)
        deadline = time.monotonic() + 5
        while not (directory / "mcp.sock").exists():
            if child.poll() is not None or time.monotonic() >= deadline:
                child.terminate()
                raise AgentBoxError(f"browser broker failed; see {directory / 'broker.log'}")
            time.sleep(.05)
    return command


def run(argv, desktop=()):
    # Codex may reuse one MCP command across overlapping resumed threads.
    # Identity and cleanup must belong to this process, not the prepared command.
    argv = list(argv)
    argv[argv.index("--name") + 1] = "agent-box-browser-" + uuid.uuid4().hex[:20]

    def stop(_signum, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    try:
        with personal_browser.opener(argv, desktop) as actual:
            return subprocess.call(actual)
    finally:
        remove_container(argv)


def diagnose():
    """An offline real Chromium launch; diagnostics never download an image."""
    script = """const {chromium}=require(require.resolve('playwright',{paths:['/app']}));
const timer=setTimeout(()=>process.exit(2),20000);
(async()=>{const b=await chromium.launch({headless:true,executablePath:chromium.executablePath(),args:['--no-sandbox']});
try {const p=await b.newPage();await p.goto('data:text/html,<h1>agent-box</h1>');
if(await p.textContent('h1')!=='agent-box')throw Error('unexpected page');
console.log(b.version());}finally {await b.close();clearTimeout(timer);}})()
.catch(e=>{console.error(e.message);process.exit(1)});"""
    name = "agent-box-browser-check-" + uuid.uuid4().hex[:12]
    argv = ["podman", "run", "--rm", "--pull=never", "--name", name,
            "--label", f"{LABEL}=true", "--network=none",
            "--userns=keep-id:uid=1000,gid=1000", "--cap-drop=all",
            "--security-opt=no-new-privileges", "--entrypoint=node", IMAGE, "-e", script]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        if result.returncode:
            return "browser: FAILED (" + result.stderr.strip()[-700:] + "; enable browser and launch once to fetch its image)"
        return "browser: OK (isolated container, Chromium " + result.stdout.strip() + ")"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "browser: FAILED (" + str(exc) + ")"
    finally:
        try:
            remove_container(argv)
        except (OSError, subprocess.SubprocessError):
            pass


def main(args=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    direct = sub.add_parser("run")
    direct.add_argument("command")
    direct.add_argument("desktop", nargs="?", default="[]")
    broker = sub.add_parser("serve")
    broker.add_argument("directory")
    broker.add_argument("command")
    broker.add_argument("owner")
    broker.add_argument("desktop", nargs="?", default="[]")
    options = parser.parse_args(args)
    if options.action == "run":
        return run(json.loads(options.command), json.loads(options.desktop))
    serve(options.directory, json.loads(options.command), json.loads(options.owner), json.loads(options.desktop))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
