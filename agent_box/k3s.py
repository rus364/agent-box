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

import contextlib
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
from .host_podman import WORKSPACE_PATH_LABEL
from . import networking

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
REGISTRIES_FILE = "registries.yaml"
REGISTRIES_TARGET = "/etc/rancher/k3s/registries.yaml"
#: k3s applies every manifest under its manifests directory, subdirectories
#: included; a subdirectory of its own keeps k3s's packaged manifests visible.
MANIFESTS_TARGET = f"{DATA_DIR}/server/manifests/agent-box"
CONTEXT = "agent-box"
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
    components = (("traefik", k3s.traefik), ("servicelb", k3s.servicelb),
                  ("metrics-server", k3s.metrics_server), ("local-storage", k3s.local_storage))
    disabled = [component for component, on in components if not on]
    return [
        "server", "--write-kubeconfig-mode=600",
        *([f"--disable={','.join(disabled)}"] if disabled else []),
        *([] if k3s.network_policy else ["--disable-network-policy"]),
        f"--kubelet-arg={USERNS_GATE}",
        f"--kube-apiserver-arg={USERNS_GATE}",
        f"--kube-controller-manager-arg={USERNS_GATE}",
        "--kube-proxy-arg=conntrack-max-per-core=0",
        # Last, so a user's flag can refine one of agent-box's.
        *k3s.extra_args,
    ]


def registries_yaml(k3s: K3sConfig) -> str:
    """k3s's registries.yaml for the configured mirrors; JSON is valid YAML."""
    if not k3s.registry_mirrors:
        return ""
    mirrors = {registry: {"endpoint": list(endpoints)}
               for registry, endpoints in sorted(k3s.registry_mirrors.items())}
    return json.dumps({"mirrors": mirrors}, indent=2) + "\n"


def manifests_dir(k3s: K3sConfig, project_root: Path) -> Path | None:
    """The project's manifests directory, refused if it resolves outside the project.

    It is mounted into the sidecar, so a symlink must not turn "a directory
    of this project" into any directory of the host.
    """
    if not k3s.manifests:
        return None
    root = project_root.resolve()
    path = (root / k3s.manifests).resolve()
    if path != root and root not in path.parents:
        raise AgentBoxError(f"k3s: box.k3s.manifests resolves outside the project: {path}")
    if not path.is_dir():
        raise AgentBoxError(f"k3s: box.k3s.manifests is not a directory: {path}")
    if "," in str(path):
        raise AgentBoxError(f"k3s: box.k3s.manifests cannot contain a comma: {path}")
    return path


def container_args(workspace_id: str, k3s: K3sConfig, state_dir: Path, project_root: Path) -> list[str]:
    """Everything that defines the sidecar; its hash decides recreation."""
    resource = name(workspace_id)
    manifests = manifests_dir(k3s, project_root)
    if k3s.registry_mirrors and "," in str(state_dir):
        # --mount is comma-delimited; a comma would split the path into options.
        raise AgentBoxError(f"k3s: registry mirrors need a state directory without a comma: {state_dir}")
    return [
        f"--hostname={resource}", f"--network={resource}",
        "--privileged", "--cgroupns=private", "--tmpfs=/run", "--tmpfs=/var/run",
        *([f"--cpus={k3s.cpus}"] if k3s.cpus else []),
        *([f"--memory={k3s.memory}"] if k3s.memory else []),
        f"--mount=type=volume,source={resource},target={DATA_DIR}",
        *([f"--mount=type=bind,src={manifests},dst={MANIFESTS_TARGET},ro=true"] if manifests else []),
        *([f"--mount=type=bind,src={state_dir / REGISTRIES_FILE},dst={REGISTRIES_TARGET},ro=true"]
          if k3s.registry_mirrors else []),
        f"--publish=127.0.0.1::{API_PORT}",
        # Loopback only: an ingress for the user's browser, never for the LAN.
        *[f"--publish={networking.parse_port(port).publish()}" for port in k3s.ports],
        "--entrypoint=/bin/sh", image_ref(k3s), "-c", ENTRYPOINT, "k3s", *server_args(k3s),
    ]


def spec_hash(workspace_id: str, k3s: K3sConfig, state_dir: Path, project_root: Path) -> str:
    """Covers the mounted registries.yaml's content, not only its path."""
    text = json.dumps([container_args(workspace_id, k3s, state_dir, project_root), registries_yaml(k3s)],
                      separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _podman(*args: str, timeout: float | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["podman", *args], text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(["podman", *args], 124, "", f"timed out after {timeout}s")
    except OSError as exc:
        raise AgentBoxError(f"k3s: podman {args[0]} failed: {exc}") from exc


def _checked(*args: str, timeout: float | None = None) -> str:
    result = _podman(*args, timeout=timeout)
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise AgentBoxError(f"k3s: podman {' '.join(args[:2])} failed: {detail}")
    return result.stdout


def _say(message: str) -> None:
    print(f"agent-box: {message}", file=sys.stderr)


def _exists(kind: str, resource: str, timeout: float | None = None) -> bool:
    """Podman's own answer; a failing Podman is an error, never "absent"."""
    result = _podman(kind, "exists", resource, timeout=timeout)
    if result.returncode not in (0, 1):
        detail = result.stderr.strip() or f"exit {result.returncode}"
        raise AgentBoxError(f"k3s: podman {kind} exists failed: {detail}")
    return result.returncode == 0


def _require_owned(kind: str, workspace_id: str, owner: str) -> None:
    """Refuse a same-named object agent-box did not create for this workspace."""
    if owner != workspace_id:
        raise AgentBoxError(f"k3s: a {kind} named {name(workspace_id)} exists but was not created by "
                            "agent-box for this workspace; rename or remove it yourself")


def inspect(workspace_id: str, timeout: float | None = None) -> dict | None:
    """The sidecar container, or ``None`` when there is none."""
    if not _exists("container", name(workspace_id), timeout):
        return None
    try:
        payload = json.loads(_checked("container", "inspect", name(workspace_id), timeout=timeout))
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
            f"missing: {', '.join(missing)}. Delegate them to your user's cgroup (on systemd hosts: "
            "a user@.service drop-in with Delegate=cpu cpuset io memory pids) and log in again")


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


def _remove_container(resource: str) -> None:
    """Remove the sidecar with the anonymous volumes of the image's ``VOLUME`` paths.

    ``rancher/k3s`` declares ``/var/log``, ``/var/lib/cni`` and
    ``/var/lib/kubelet`` as volumes; a new container never reuses them, so
    without ``--volumes`` every recreation leaves three behind. The named data
    volume is not anonymous and stays.
    """
    _checked("rm", "--force", "--volumes", resource)


def _ensure_labeled(kind: str, workspace_id: str, *labels: str) -> None:
    if not _owned_exists(kind, workspace_id):
        _checked(kind, "create", f"--label={LABEL}={workspace_id}", *labels, name(workspace_id))


def _log_tail(workspace_id: str) -> str:
    # Read after the deadline has passed, so it gets only a short grace.
    result = _podman("logs", "--tail=15", name(workspace_id), timeout=5)
    lines = (result.stdout + result.stderr).strip().splitlines()
    return "\n".join(line[:300] for line in lines[-15:]) or "(no log output)"


def is_outdated(workspace_id: str, k3s: K3sConfig, state_dir: Path, project_root: Path) -> bool:
    """Whether an existing sidecar was created from different settings."""
    container = inspect(workspace_id)
    labels = (container or {}).get("Config", {}).get("Labels") or {}
    return container is not None and labels.get(SPEC_LABEL) != spec_hash(workspace_id, k3s, state_dir, project_root)


def ensure(workspace_id: str, k3s: K3sConfig, state_dir: Path, project_root: Path) -> int:
    """Bring the workspace's cluster up and ready; return its host API port.

    A container whose definition changed (version, components, limits,
    ports, mounts, recipe) is recreated on the same volume, so the cluster's
    data survives. The kubeconfig is rendered anew every time: a recreated
    sidecar publishes a new port.
    """
    problems = k3s.validate()
    if problems:
        raise AgentBoxError("; ".join(problems))
    resource = name(workspace_id)
    wanted = spec_hash(workspace_id, k3s, state_dir, project_root)
    container = inspect(workspace_id)
    labels = (container or {}).get("Config", {}).get("Labels") or {}
    if container is not None and labels.get(SPEC_LABEL) != wanted:
        _remove_container(resource)
        container = None
    if container is None:
        check_host()
        pull(k3s)
        # The workspace's record that a sidecar may exist: release_k3s asks
        # Podman nothing without it, and a start that fails later still
        # leaves a sidecar to stop.
        state_dir.mkdir(parents=True, exist_ok=True)
        _ensure_labeled("network", workspace_id)
        path_label = f"--label={WORKSPACE_PATH_LABEL}={project_root.resolve()}"
        _ensure_labeled("volume", workspace_id, path_label)
        registries = registries_yaml(k3s)
        if registries:
            atomic_write_text(state_dir / REGISTRIES_FILE, registries, 0o644)
        _checked("create", f"--name={resource}", f"--label={LABEL}={workspace_id}", path_label,
                 f"--label={SPEC_LABEL}={wanted}", *container_args(workspace_id, k3s, state_dir, project_root))
    if not _running(container):
        _say(f"starting the k3s cluster {resource}")
        _checked("start", resource)
    wait_ready(workspace_id, k3s.ready_timeout)
    port = api_port(workspace_id)
    render_kubeconfig(workspace_id, port, state_dir)
    return port


def wait_ready(workspace_id: str, timeout: float) -> None:
    """Wait for a Ready node, failing early if the sidecar dies."""
    resource = name(workspace_id)
    deadline = time.monotonic() + timeout
    probe = ["exec", resource, "kubectl", "get", "nodes",
             "-o", "jsonpath={.items[*].status.conditions[?(@.type==\"Ready\")].status}"]
    def left() -> float:
        # Every call in the loop gets only what is left, so neither a stalled
        # kubectl nor a stalled Podman can hold the wait past the deadline.
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AgentBoxError(f"k3s: the cluster {resource} was not ready within {int(timeout)}s; "
                                "its log ends with:\n" + _log_tail(workspace_id))
        return min(PROBE_TIMEOUT, remaining)

    while True:
        if _podman(*probe, timeout=left()).stdout.strip() == "True":
            return
        if not _running(inspect(workspace_id, left())):
            raise AgentBoxError(f"k3s: the cluster {resource} stopped while starting; its log ends with:\n"
                                + _log_tail(workspace_id))
        time.sleep(min(1.0, left()))


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
        _remove_container(resource)
    if _owned_exists("network", workspace_id):
        _checked("network", "rm", resource)
    if data:
        if _owned_exists("volume", workspace_id):
            _checked("volume", "rm", resource)
        if state_dir is not None:
            kubeconfig_path(state_dir).unlink(missing_ok=True)
            (state_dir / REGISTRIES_FILE).unlink(missing_ok=True)
            with contextlib.suppress(OSError):
                state_dir.rmdir()


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
