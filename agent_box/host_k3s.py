"""Shared cluster policy and native consumers, serialized by the workspace lock.

The tmux client is not an owner: its pane survives detach. A reservation is
handed to the pane runner, which records both itself and its actual child so
an abruptly killed launcher cannot make a surviving agent lose its cluster.
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

from . import channel, k3s, migrations, runtime_bundle
from .atomicio import atomic_write_json
from .config import Config
from .errors import AgentBoxError
from .paths import PACKAGE_ROOT, host_admission_lock
from .workspace import Workspace

FILE = "host-cluster.json"
ENV = "AGENT_BOX_HOST_CLUSTER"
CHILD_ENV = "AGENT_BOX_HOST_CLUSTER_CHILD"
SCHEMA = migrations.Schema("host cluster ownership")


def identity(pid=None):
    pid = os.getpid() if pid is None else pid
    return {"pid": pid, "pid_start": channel._pid_start_ticks(pid),
            "boot_id": channel._boot_id()}


def alive(process):
    if not (process.get("boot_id") == channel._boot_id() and channel._launcher_alive(process)):
        return False
    try:
        # An orphan can remain as a zombie until its reaper runs; it uses no API.
        return Path(f"/proc/{process['pid']}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (FileNotFoundError, ProcessLookupError):
        # The process can disappear before open or while reading proc stat.
        return False


def pane_alive(owner):
    if owner.get("pending") and owner.get("session"):
        from .sessions import tmux_sessions
        return owner["session"] in tmux_sessions(None, host_root=Path(owner["host_root"]))
    return False


def children(owner):
    return [owner.get("child", {}), *owner.get("descendants", [])]


def children_alive(owner):
    return any(alive(process) for process in children(owner))


def load(root):
    path = root / FILE
    if not path.exists():
        return None
    try:
        data = SCHEMA.prepare(json.loads(path.read_bytes()))
        settings = Config.from_dict({"box": data["box"]})
        if (settings.box.k3s.validate() or not isinstance(data["owners"], dict)
                or type(data["box"]["k3s"]["enabled"]) is not bool
                or not isinstance(data["runtime"], str)
                or not isinstance(data["project"], str) or not Path(data["project"]).is_absolute()):
            raise ValueError()
        for owner in data["owners"].values():
            if not isinstance(owner, dict) or not isinstance(owner["process"], dict):
                raise ValueError()
            if "child" in owner and not isinstance(owner["child"], dict):
                raise ValueError()
            if (not isinstance(owner.get("descendants", []), list)
                    or any(not isinstance(child, dict) for child in owner.get("descendants", []))):
                raise ValueError()
        data["owners"] = {key: owner for key, owner in data["owners"].items()
                          if alive(owner["process"]) or children_alive(owner) or pane_alive(owner)}
        return data
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise AgentBoxError(f"cannot read host cluster ownership at {path}; preserve it and repair before retrying") from exc


def save(root, data):
    root.mkdir(parents=True, exist_ok=True)
    atomic_write_json(root / FILE, {**data, "format_version": 1}, 0o600)


def has_owners(root):
    data = load(root)
    return bool(data and data["owners"])


def refuse_owners(root):
    """Guard destructive workspace commands before their first mutation."""
    data = load(root)
    if data and data["owners"]:
        raise AgentBoxError("host sessions still use this project's environment; "
                            "end them with agent-box --host stop before stopping or removing the workspace")
    if data and data["box"]["k3s"]["enabled"]:
        local_podman()


def box_policy(root, workspace):
    """Avoid requiring Podman for a host project that never created a box."""
    from . import runtime
    if not any(root.glob("box-*.json")):
        return None
    box = runtime.inspect_workspace_container(runtime.workspace_container_name(workspace))
    if (box or {}).get("State", {}).get("Status") != "running":
        return None
    labels = box.get("Config", {}).get("Labels") or {}
    if (labels.get(runtime.WORKSPACE_RUNTIME_LABEL) != runtime_bundle.identity(PACKAGE_ROOT)
            or labels.get(runtime.WORKSPACE_CONTAINER_WORKSPACE_LABEL) != root.name
            or labels.get(runtime.WORKSPACE_CONTAINER_LABEL) != "true"):
        raise AgentBoxError("running workspace uses an incompatible runtime; stop and relaunch before joining from --host")
    policy_id = labels.get(runtime.WORKSPACE_CONTAINER_POLICY_LABEL, "")
    import re
    if not re.fullmatch("[0-9a-f]{32}", policy_id):
        raise AgentBoxError("running workspace has no readable creation policy; stop and relaunch")
    return migrations.read(root / f"box-{policy_id}.json", migrations.BOX_POLICY)["box"]


def apply(config, options, policy):
    """Only shared sidecar choices are inherited; host credentials stay native."""
    shared = Config.from_dict({"box": policy})
    if "k3s" in options.overrides and options.overrides["k3s"] != shared.box.k3s.enabled:
        raise AgentBoxError("explicit k3s choice conflicts with the running project environment; "
                            "stop all project sessions and relaunch with that option")
    config.box.k3s = copy.deepcopy(shared.box.k3s)
    config._cluster_policy = shared.box.k3s.to_dict()


def inherit(config, options, root):
    """Called under the workspace lock before a container's creation."""
    data = load(root)
    if data and data.get("stopping"):
        raise AgentBoxError("host sessions are stopping; retry after agent-box --host stop finishes")
    if data and data["owners"]:
        if data["runtime"] != runtime_bundle.identity(PACKAGE_ROOT):
            raise AgentBoxError("host sessions use an incompatible runtime; end them and relaunch")
        apply(config, options, data["box"])
    return data


def verify(config, root):
    data = load(root)
    if not data or not data["owners"]:
        return
    expected = Config.from_dict({"box": data["box"]})
    if (data["runtime"] != runtime_bundle.identity(PACKAGE_ROOT)
            or config.box.k3s.to_dict() != expected.box.k3s.to_dict()):
        raise AgentBoxError("project cluster policy changed during startup; retry the launch")
    if config.box.k3s.enabled:
        sidecar = k3s.inspect(root.name)
        wanted = k3s.spec_hash(root.name, config.box.k3s, root / "k3s",
                               Path(data["project"]))
        if (sidecar or {}).get("State", {}).get("Status") != "running" or (
                sidecar.get("Config", {}).get("Labels", {}).get(k3s.SPEC_LABEL) != wanted):
            raise AgentBoxError("shared k3s is unavailable or incompatible; stop project sessions and relaunch")


def local_podman():
    from . import runtime
    if runtime.inside_box():
        raise AgentBoxError("host k3s is unavailable inside a workspace box; launch on the Linux Podman host")
    # Podman also enables remote mode through containers.conf and connections.
    if os.environ.get("CONTAINER_HOST") or os.environ.get("CONTAINER_CONNECTION"):
        raise AgentBoxError("host k3s requires local rootless Podman, not a remote connection")
    runtime.host_podman.require()
    result = subprocess.run(["podman", "info", "--format=json"], capture_output=True, text=True)
    if result.returncode:
        raise AgentBoxError("cannot inspect local Podman: " + result.stderr.strip())
    info = json.loads(result.stdout)
    host = info.get("host", {})
    if host.get("serviceIsRemote") or not host.get("security", {}).get("rootless"):
        raise AgentBoxError("host k3s requires local rootless Podman")


def reserve(config, options, workspace):
    with admission_lock():
        return _reserve(config, options, workspace)


@contextmanager
def admission_lock(*, exclusive=False):
    """Uninstall excludes new owners; unrelated workspace starts can overlap."""
    from . import runtime
    with runtime.transition_lock_at(host_admission_lock(runtime.STATE_ROOT), shared=not exclusive):
        yield


def _reserve(config, options, workspace):
    from . import runtime
    state = runtime.workspace_runtime_state(workspace)
    root = state.root
    with runtime.workspace_transition_lock(workspace):
        data = inherit(config, options, root)
        policy = box_policy(root, workspace)
        if policy is not None:
            apply(config, options, policy)
        needs_cleanup = policy is None and (not data or not data["owners"])
        # Both starting and cleaning up a previous cluster use this endpoint.
        # Reject unsupported topology before changing its resources or policy.
        if (config.box.k3s.enabled or needs_cleanup and
                (data and data["box"]["k3s"]["enabled"] or (root / "k3s").is_dir())):
            local_podman()
        if config.box.k3s.enabled and not runtime.shutil.which("kubectl"):
            raise AgentBoxError("host k3s requires kubectl on the host; install it before launching")
        if policy is not None:
            verify(config, root)
        elif needs_cleanup:
            # A detached box may have ended without a launcher to clean up.
            # Both modes must apply its recorded policy before creating a new one.
            runtime.release_k3s(workspace, config, state)
        if not data or not data["owners"]:
            if not data:
                host_root = runtime.HOST_SESSIONS_ROOT / root.name
                if host_root.is_dir() and runtime.shutil.which("tmux") and runtime.tmux_sessions(None, host_root=host_root):
                    raise AgentBoxError("existing host sessions have no shared cluster policy; end them and relaunch")
            data = {"box": {"k3s": config.box.k3s.to_dict()},
                    "runtime": runtime_bundle.identity(PACKAGE_ROOT), "owners": {},
                    "project": str(workspace.host_root)}
            # A failed readiness check must retain the creation-time cleanup policy.
            save(root, data)
        if config.box.k3s.enabled:
            sidecar = k3s.inspect(root.name)
            if policy is not None or data and data["owners"]:
                wanted = k3s.spec_hash(root.name, config.box.k3s, root / "k3s",
                                       workspace.host_root)
                if (sidecar or {}).get("State", {}).get("Status") != "running" or (
                        sidecar.get("Config", {}).get("Labels", {}).get(k3s.SPEC_LABEL) != wanted):
                    raise AgentBoxError("shared k3s is unavailable or incompatible; stop project sessions and relaunch")
            k3s.ensure(root.name, config.box.k3s, root / "k3s", workspace.host_root)
            # Probe through the same host kubeconfig the agent will receive.
            result = subprocess.run(
                ["kubectl", "--kubeconfig", str(k3s.kubeconfig_path(root / "k3s")),
                 "--request-timeout=10s", "get", "--raw=/readyz"],
                capture_output=True, text=True, timeout=15)
            if result.returncode or result.stdout.strip() != "ok":
                raise AgentBoxError("host cannot reach the managed k3s API: " + result.stderr.strip())
        token = uuid.uuid4().hex
        config._cluster_policy = config.box.k3s.to_dict()
        data["owners"][token] = {"process": identity(), "pending": True}
        save(root, data)
        runtime.record_workspace_path(root.name, str(workspace.host_root))
    return root, token


def release(root, token, *, pending=False):
    from . import runtime
    with runtime.transition_lock_at(root / runtime.WORKSPACE_LOCK_FILE):
        data = load(root)
        if not data:
            return
        owner = data["owners"].get(token)
        if owner and (not pending or owner.get("pending")):
            # A killed supervisor may leave its child alive; never drop that owner.
            if not children_alive(owner) and not pane_alive(owner):
                data["owners"].pop(token, None)
        save(root, data)
        if not data["owners"]:
            cleanup(root, data)


def register_tmux(reservation, session, host_root):
    from . import runtime
    root_text, token = json.loads(reservation)
    root = Path(root_text)
    with runtime.transition_lock_at(root / runtime.WORKSPACE_LOCK_FILE):
        data = load(root)
        if not data or token not in data["owners"] or data.get("stopping"):
            raise AgentBoxError("host session reservation expired; relaunch")
        data["owners"][token].update(session=session, host_root=str(host_root))
        save(root, data)


def cleanup(root, data, *, explicit=False):
    from . import runtime
    config = Config.from_dict({"box": data["box"]})
    if not config.box.k3s.enabled or not (root / "k3s").is_dir():
        return
    local_podman()
    workspace = Workspace(Path(data["project"]), Path(data["project"]))
    box = runtime.inspect_workspace_container(runtime.workspace_container_name(workspace))
    if (box or {}).get("State", {}).get("Status") == "running":
        return
    if explicit or config.box.k3s.lifetime == "box":
        k3s.stop(root.name, config.box.k3s, root / "k3s")


def record_box(config, root):
    """Keep a former host environment's cleanup policy current after box creation."""
    data = load(root)
    if data and not data["owners"]:
        data["box"] = {"k3s": config.box.k3s.to_dict()}
        data["runtime"] = runtime_bundle.identity(PACKAGE_ROOT)
        save(root, data)


def _spawn(root, token, data, command, environment, *, descendant=False, **kwargs):
    """Under the workspace lock, make ownership durable before allowing exec."""
    from . import runtime
    process = None
    read_fd, write_fd = os.pipe()
    try:
        process = subprocess.Popen(
            [sys.executable, str(runtime.HOST_SESSION_RUNNER), "--gate", str(read_fd), *command],
            env=environment, pass_fds=(*kwargs.pop("pass_fds", ()), read_fd), **kwargs)
        os.close(read_fd)
        read_fd = None
        owner = data["owners"][token]
        if descendant:
            owner.setdefault("descendants", []).append(identity(process.pid))
        else:
            owner["child"] = identity(process.pid)
        save(root, data)
        os.write(write_fd, b"1")
        return process
    except BaseException:
        if process is not None:
            process.kill()
            process.wait()
        raise
    finally:
        if read_fd is not None:
            os.close(read_fd)
        os.close(write_fd)


def popen(command, *, env=None, **kwargs):
    """Internal supervisors register their actual agents before starting them."""
    from . import runtime
    environment = dict(os.environ if env is None else env)
    reservation = environment.pop(CHILD_ENV, None)
    if reservation is None:
        return subprocess.Popen(command, **({"env": env} if env is not None else {}), **kwargs)
    root_text, token = json.loads(reservation)
    root = Path(root_text)
    with runtime.transition_lock_at(root / runtime.WORKSPACE_LOCK_FILE):
        data = load(root)
        if not data or token not in data["owners"] or data.get("stopping"):
            raise AgentBoxError("host session reservation expired; relaunch")
        return _spawn(root, token, data, command, environment, descendant=True, **kwargs)


def run(command, environment):
    """The actual pane/direct runner owns the child, not the attaching client."""
    from . import runtime
    root_text, token = json.loads(environment.pop(ENV))
    root = Path(root_text)
    process = None
    # Only agent-box's own supervisors receive the child-registration token.
    # Ordinary agents and their tools must not adopt that internal capability.
    environment.pop(CHILD_ENV, None)
    if (len(command) > 2 and command[1] == str(runtime.HOST_LAUNCHER)
            and command[2] in {"_external", "_session"}):
        environment[CHILD_ENV] = json.dumps([root_text, token])

    def forward(signum, frame):
        if process is not None and process.poll() is None:
            process.send_signal(signum)

    previous = {sig: signal.signal(sig, forward) for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)}
    try:
        with runtime.transition_lock_at(root / runtime.WORKSPACE_LOCK_FILE):
            data = load(root)
            if not data or token not in data["owners"] or data.get("stopping"):
                raise AgentBoxError("host session reservation expired; relaunch")
            data["owners"][token] = {"process": identity()}
            save(root, data)
            process = _spawn(root, token, data, command, environment)
        return process.wait()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        release(root, token)


def stop(root):
    """Signal only verified owners, then prove they ended before cluster cleanup."""
    from . import runtime
    with runtime.transition_lock_at(root / runtime.WORKSPACE_LOCK_FILE):
        data = load(root)
        if not data:
            return []
        if data["box"]["k3s"]["enabled"]:
            local_podman()
        tokens = list(data["owners"])
        data["stopping"] = True
        save(root, data)
        for owner in data["owners"].values():
            if pane_alive(owner):
                from .sessions import tmux_argv
                subprocess.run(tmux_argv("kill-session", "-t", "=" + owner["session"],
                                        host_root=Path(owner["host_root"])),
                               capture_output=True)
            for process in [*children(owner), owner["process"]]:
                if not alive(process):
                    continue
                # pidfd pins identity between checking /proc and delivering a signal.
                try:
                    fd = os.pidfd_open(process["pid"])
                except ProcessLookupError:
                    continue
                try:
                    if alive(process):
                        signal.pidfd_send_signal(fd, signal.SIGTERM)
                finally:
                    os.close(fd)
    deadline = time.monotonic() + 10
    while True:
        with runtime.transition_lock_at(root / runtime.WORKSPACE_LOCK_FILE):
            data = load(root)
            if not data or not data["owners"]:
                if data:
                    cleanup(root, data, explicit=True)
                    data.pop("stopping", None)
                    save(root, data)
                return tokens
        if time.monotonic() >= deadline:
            raise AgentBoxError("host sessions have not exited; cluster and data retained; end them and retry")
        time.sleep(0.05)
