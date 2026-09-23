"""The k3s sidecar of one workspace: a cluster in the host's rootless Podman.

The box stays unprivileged; the cluster runs next to it in its own
container, network and volume, all named ``agent-box-k3s-<workspace id>``.
The recipe below is what a rootless k3s needs and nothing more: privileges
that stay inside the sidecar's user namespace, a private cgroup namespace,
the kubelet's user-namespace feature gate, and kube-proxy told not to touch
host conntrack limits it cannot write.

A dedicated network rather than Podman's default pasta: pasta hands the node
the host's LAN address, which moves with the host's network and strands the
node. The API is published on host loopback only; the box reaches it through
pasta's loopback forward (``runtime`` wires that), and nothing is exposed to
the LAN.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

from .atomicio import atomic_write_text
from .config import K3sConfig
from .errors import AgentBoxError

IMAGE = "docker.io/rancher/k3s"
#: The stable channel's release when last updated; pinned by its multi-arch
#: index digest so a moved tag cannot change the cluster under a user.
PINNED_TAG = "v1.36.4-k3s1"
PINNED_DIGEST = "sha256:edad48e12bf81c3a09ac1c05c0c0ffaaa22145980b989d6fae84543a76b83657"
IMAGE_SIZE_HINT = "about 250 MB"

LABEL = "io.agent-box.k3s"
SPEC_LABEL = "io.agent-box.k3s.spec"
API_PORT = "6443/tcp"
DATA_DIR = "/var/lib/rancher/k3s"
KUBECONFIG_SOURCE = "/etc/rancher/k3s/k3s.yaml"
KUBECONFIG_FILE = "kubeconfig"
CONTEXT = "agent-box"
READY_TIMEOUT = 180
#: Bounds one readiness probe, so a stalled ``podman exec`` cannot outlive
#: the overall deadline.
PROBE_TIMEOUT = 15
REQUIRED_CONTROLLERS = ("cpu", "cpuset", "io", "memory", "pids")

#: cgroup v2 forbids enabling controllers for children of a cgroup that holds
#: processes, and a container starts inside its (private) cgroup root. Moving
#: every process into a leaf first lets the kubelet create ``kubepods`` --
#: the same step k3d and kind take. Passed inline so no host file is needed.
ENTRYPOINT = (
    "set -e\n"
    "mkdir -p /sys/fs/cgroup/init\n"
    "xargs -rn1 < /sys/fs/cgroup/cgroup.procs > /sys/fs/cgroup/init/cgroup.procs 2>/dev/null || :\n"
    "sed -e 's/ / +/g' -e 's/^/+/' < /sys/fs/cgroup/cgroup.controllers"
    " > /sys/fs/cgroup/cgroup.subtree_control\n"
    'exec /bin/k3s "$@"\n'
)
USERNS_GATE = "feature-gates=KubeletInUserNamespace=true"


def name(workspace_id: str) -> str:
    """The container, network and volume name of one workspace's cluster."""
    return f"agent-box-k3s-{workspace_id}"


def image_ref(k3s: K3sConfig) -> str:
    if k3s.version:
        return f"{IMAGE}:{k3s.version}"
    return f"{IMAGE}:{PINNED_TAG}@{PINNED_DIGEST}"


def server_args(k3s: K3sConfig) -> list[str]:
    disabled = [component for component, on in (("traefik", k3s.traefik), ("metrics-server", k3s.metrics_server))
                if not on]
    return [
        "server", "--write-kubeconfig-mode=600",
        *([f"--disable={','.join(disabled)}"] if disabled else []),
        f"--kubelet-arg={USERNS_GATE}",
        f"--kube-apiserver-arg={USERNS_GATE}",
        f"--kube-controller-manager-arg={USERNS_GATE}",
        "--kube-proxy-arg=conntrack-max-per-core=0",
    ]


def container_args(workspace_id: str, k3s: K3sConfig) -> list[str]:
    """Everything that defines the sidecar; its hash decides recreation."""
    resource = name(workspace_id)
    return [
        f"--hostname={resource}", f"--network={resource}",
        "--privileged", "--cgroupns=private", "--tmpfs=/run", "--tmpfs=/var/run",
        f"--mount=type=volume,source={resource},target={DATA_DIR}",
        f"--publish=127.0.0.1::{API_PORT}",
        "--entrypoint=/bin/sh", image_ref(k3s), "-c", ENTRYPOINT, "k3s", *server_args(k3s),
    ]


def spec_hash(workspace_id: str, k3s: K3sConfig) -> str:
    text = json.dumps(container_args(workspace_id, k3s), separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _podman(*args: str, timeout: float | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["podman", *args], text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(["podman", *args], 124, "", f"timed out after {timeout}s")
    except OSError as exc:
        raise AgentBoxError(f"k3s: podman {args[0]} failed: {exc}") from exc


def _checked(*args: str) -> str:
    result = _podman(*args)
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise AgentBoxError(f"k3s: podman {' '.join(args[:2])} failed: {detail}")
    return result.stdout


def _say(message: str) -> None:
    print(f"agent-box: {message}", file=sys.stderr)


def _exists(kind: str, resource: str) -> bool:
    """Podman's own answer; a failing Podman is an error, never "absent"."""
    result = _podman(kind, "exists", resource)
    if result.returncode not in (0, 1):
        detail = result.stderr.strip() or f"exit {result.returncode}"
        raise AgentBoxError(f"k3s: podman {kind} exists failed: {detail}")
    return result.returncode == 0


def _require_owned(kind: str, workspace_id: str, owner: str) -> None:
    """Refuse a same-named object agent-box did not create for this workspace."""
    if owner != workspace_id:
        raise AgentBoxError(f"k3s: a {kind} named {name(workspace_id)} exists but was not created by "
                            "agent-box for this workspace; rename or remove it yourself")


def inspect(workspace_id: str) -> dict | None:
    """The sidecar container, or ``None`` when there is none."""
    if not _exists("container", name(workspace_id)):
        return None
    try:
        payload = json.loads(_checked("container", "inspect", name(workspace_id)))
    except json.JSONDecodeError:
        raise AgentBoxError("k3s: podman container inspect returned invalid JSON") from None
    if not (isinstance(payload, list) and payload and isinstance(payload[0], dict)):
        raise AgentBoxError("k3s: podman container inspect returned an unexpected shape")
    labels = payload[0].get("Config", {}).get("Labels") or {}
    _require_owned("container", workspace_id, labels.get(LABEL, ""))
    return payload[0]


def _running(container: dict | None) -> bool:
    return bool(container) and container.get("State", {}).get("Status") == "running"


def check_host() -> None:
    """Refuse before creating anything on a host whose cgroups cannot hold a kubelet."""
    info = json.loads(_checked("info", "--format=json") or "{}").get("host", {})
    if info.get("cgroupVersion") != "v2":
        raise AgentBoxError("k3s needs cgroup v2; this host's Podman reports "
                            f"{info.get('cgroupVersion') or 'an unknown version'}")
    missing = [c for c in REQUIRED_CONTROLLERS if c not in (info.get("cgroupControllers") or [])]
    if missing:
        raise AgentBoxError(
            f"k3s needs the cgroup controllers {', '.join(REQUIRED_CONTROLLERS)} delegated to your user; "
            f"missing: {', '.join(missing)}. Delegate them with a systemd user@.service drop-in "
            "(Delegate=cpu cpuset io memory pids) and log in again")


def pull(k3s: K3sConfig) -> None:
    ref = image_ref(k3s)
    if _podman("image", "exists", ref).returncode == 0:
        return
    _say(f"pulling the k3s image {ref} ({IMAGE_SIZE_HINT})")
    _checked("pull", "--quiet", ref)


def _owned_exists(kind: str, workspace_id: str) -> bool:
    resource = name(workspace_id)
    if not _exists(kind, resource):
        return False
    owner = _checked(kind, "inspect", f'--format={{{{index .Labels "{LABEL}"}}}}', resource).strip()
    _require_owned(kind, workspace_id, owner)
    return True


def _ensure_labeled(kind: str, workspace_id: str) -> None:
    if not _owned_exists(kind, workspace_id):
        _checked(kind, "create", f"--label={LABEL}={workspace_id}", name(workspace_id))


def _log_tail(workspace_id: str) -> str:
    result = _podman("logs", "--tail=15", name(workspace_id), timeout=PROBE_TIMEOUT)
    lines = (result.stdout + result.stderr).strip().splitlines()
    return "\n".join(line[:300] for line in lines[-15:]) or "(no log output)"


def ensure(workspace_id: str, k3s: K3sConfig, state_dir: Path, *, timeout: float = READY_TIMEOUT) -> int:
    """Bring the workspace's cluster up and ready; return its host API port.

    A container whose definition changed (version, components, recipe) is
    recreated on the same volume, so the cluster's data survives. The
    kubeconfig is rendered anew every time: a recreated sidecar publishes a
    new port.
    """
    resource = name(workspace_id)
    wanted = spec_hash(workspace_id, k3s)
    container = inspect(workspace_id)
    labels = (container or {}).get("Config", {}).get("Labels") or {}
    if container is not None and labels.get(SPEC_LABEL) != wanted:
        _checked("rm", "--force", resource)
        container = None
    if container is None:
        check_host()
        pull(k3s)
        _ensure_labeled("network", workspace_id)
        _ensure_labeled("volume", workspace_id)
        _checked("create", f"--name={resource}", f"--label={LABEL}={workspace_id}",
                 f"--label={SPEC_LABEL}={wanted}", *container_args(workspace_id, k3s))
    if not _running(container):
        _say(f"starting the k3s cluster {resource}")
        _checked("start", resource)
    wait_ready(workspace_id, timeout)
    port = api_port(workspace_id)
    render_kubeconfig(workspace_id, port, state_dir)
    return port


def wait_ready(workspace_id: str, timeout: float) -> None:
    """Wait for a Ready node, failing early if the sidecar dies."""
    resource = name(workspace_id)
    deadline = time.monotonic() + timeout
    probe = ["exec", resource, "kubectl", "get", "nodes",
             "-o", "jsonpath={.items[*].status.conditions[?(@.type==\"Ready\")].status}"]
    while True:
        if _podman(*probe, timeout=PROBE_TIMEOUT).stdout.strip() == "True":
            return
        if not _running(inspect(workspace_id)):
            raise AgentBoxError(f"k3s: the cluster {resource} stopped while starting; its log ends with:\n"
                                + _log_tail(workspace_id))
        if time.monotonic() >= deadline:
            raise AgentBoxError(f"k3s: the cluster {resource} was not ready within {int(timeout)}s; "
                                "its log ends with:\n" + _log_tail(workspace_id))
        time.sleep(1)


def api_port(workspace_id: str) -> int:
    published = _checked("port", name(workspace_id), API_PORT).strip().splitlines()
    match = re.fullmatch(r"127\.0\.0\.1:(\d+)", published[0].strip()) if published else None
    if not match:
        raise AgentBoxError(f"k3s: unexpected published API address {published!r}")
    return int(match.group(1))


def kubeconfig_path(state_dir: Path) -> Path:
    return state_dir / KUBECONFIG_FILE


def render_kubeconfig(workspace_id: str, port: int, state_dir: Path) -> Path:
    """The cluster-admin credential, pointed at the host-loopback API port."""
    text = _checked("exec", name(workspace_id), "cat", KUBECONFIG_SOURCE)
    text, servers = re.subn(r"(?m)^(\s*server: https://)127\.0\.0\.1:6443\s*$", rf"\g<1>127.0.0.1:{port}", text)
    if servers != 1:
        raise AgentBoxError("k3s: the cluster's kubeconfig has an unexpected server entry")
    text = re.sub(r"(?m)^(\s*(?:- )?(?:name|cluster|user|current-context): )default\s*$", rf"\g<1>{CONTEXT}", text)
    path = kubeconfig_path(state_dir)
    atomic_write_text(path, text, 0o600)
    return path


def stop(workspace_id: str, k3s: K3sConfig, state_dir: Path) -> bool:
    """Stop the sidecar; without ``persistent_data`` also delete its data.

    Returns whether anything was running.
    """
    container = inspect(workspace_id)
    was_running = _running(container)
    if was_running:
        _checked("stop", "--time=10", name(workspace_id))
    if not k3s.persistent_data:
        remove(workspace_id, state_dir, data=True)
    return was_running


def remove(workspace_id: str, state_dir: Path | None, *, data: bool) -> None:
    """Delete the sidecar and its network; with ``data`` its volume and kubeconfig too.

    Anything already absent is not an error.
    """
    resource = name(workspace_id)
    if inspect(workspace_id) is not None:
        _checked("rm", "--force", resource)
    if _owned_exists("network", workspace_id):
        _checked("network", "rm", resource)
    if data:
        if _owned_exists("volume", workspace_id):
            _checked("volume", "rm", resource)
        if state_dir is not None:
            kubeconfig_path(state_dir).unlink(missing_ok=True)


def all_workspaces() -> list[str]:
    """Workspace IDs with any k3s container, network or volume, for uninstall."""
    found: set[str] = set()
    for command in (("ps", "--all", "--format={{.Names}}"), ("network", "ls", "--format={{.Name}}"),
                    ("volume", "ls", "--format={{.Name}}")):
        output = _checked(*command, f"--filter=label={LABEL}")
        found.update(match.group(1) for line in output.splitlines()
                     if (match := re.fullmatch(r"agent-box-k3s-([0-9a-f]{16})", line.strip())))
    return sorted(found)


def status(workspace_id: str) -> str:
    """``absent``, ``stopped``, ``running`` (API not answering) or ``ready``."""
    container = inspect(workspace_id)
    if container is None:
        return "absent"
    if not _running(container):
        return "stopped"
    result = _podman("exec", name(workspace_id), "kubectl", "get", "--raw=/readyz", timeout=PROBE_TIMEOUT)
    return "ready" if result.returncode == 0 and result.stdout.strip() == "ok" else "running"
