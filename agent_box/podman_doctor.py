"""Offline Podman diagnostics, runnable from the mounted library without imports."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["podman", *args], capture_output=True, text=True, timeout=20)


def diagnose() -> list[str]:
    """Check the engine without pulling images or running an image's default command."""
    if "podman" not in os.environ.get("AGENT_BOX_TOOLS", "").split(","):
        return ["podman:              disabled (image tool group is off)"]
    if not shutil.which("podman"):
        return ["podman:              MISSING (run 'agent-box build')"]
    lines = []
    stage = "version"
    try:
        result = _run("--version")
        if result.returncode:
            raise ValueError(result.stderr.strip() or f"exit {result.returncode}")
        lines.append(f"podman:              {result.stdout.strip()}")
        stage = "info"
        result = _run("info", "--format=json")
        if result.returncode:
            raise ValueError(result.stderr.strip() or f"exit {result.returncode}")
        info = json.loads(result.stdout)
        host, storage = info["host"], info["store"]
        lines.extend([
            f"podman rootless:     {host['security']['rootless']}",
            f"podman storage:      {storage['graphDriverName']} ({storage['graphRoot']})",
            f"podman runtime:      {host['ociRuntime']['name']}",
            f"podman cgroups:      {host['cgroupManager']} ({host['cgroupVersion']})",
            f"podman network:      {host['networkBackend']}",
        ])
        if host["security"]["rootless"] is not True:
            raise ValueError("rootless engine required")
        stage = "namespace"
        result = _run("unshare", "true")
        if result.returncode:
            raise ValueError(result.stderr.strip() or f"exit {result.returncode}")
        lines.append("podman namespace:    OK")
        # Known shell-bearing images only; arbitrary cached images may have
        # incompatible filesystems or untrusted default entrypoints.
        stage = "local image"
        image = None
        for candidate in (os.environ.get("AGENT_BOX_IMAGE", "localhost/agent-box:latest"), "docker.io/library/alpine:3.23",
                          "docker.io/library/busybox:latest"):
            result = _run("image", "exists", candidate)
            if result.returncode == 0:
                image = candidate
                break
            if result.returncode != 1:
                raise ValueError(result.stderr.strip() or f"exit {result.returncode}")
        if image is None:
            lines.append("podman run/storage:  SKIPPED (no local agent-box, alpine:3.23 or busybox:latest image; no pull)")
            return lines
        stage = "run/storage"
        name = "agent-box-doctor-" + uuid.uuid4().hex
        try:
            result = _run("run", "--pull=never", "--rm", "--name", name,
                          "--network=none", "--user", "0", "--entrypoint", "/bin/sh",
                          image, "-ec", "echo doctor-ok > /tmp/agent-box-doctor; test "
                          '"$(cat /tmp/agent-box-doctor)" = doctor-ok')
            if result.returncode:
                raise ValueError(result.stderr.strip() or f"exit {result.returncode}")
            lines.append(f"podman run/storage:  OK ({image}; network disabled)")
        finally:
            # A timed-out client can leave the created container running.
            cleanup = _run("rm", "--force", "--ignore", "--time", "1", name)
            if cleanup.returncode:
                lines.append(f"podman cleanup:      FAILED ({name}; {cleanup.stderr.strip()})")
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError) as exc:
        lines.append(f"podman {stage}: FAILED ({exc})")
    return lines


if __name__ == "__main__":
    print("\n".join(diagnose()))
