"""Build-input identity and serialized publication of workspace images."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from .errors import AgentBoxError


LABEL = "io.agent-box.build-inputs"
# The staged context is also the fingerprint boundary. Adding COPY sources
# requires adding them here, otherwise the build fails rather than missing drift.
CONTEXT_FILES = ("Containerfile", ".containerignore", "nested_podman.py")


@contextmanager
def lock(state):
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(state / "image.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


@contextmanager
def context(source, state, arguments):
    """Hash the same immutable bytes Podman reads, even during a package update."""
    with tempfile.TemporaryDirectory(prefix="image-", dir=state) as temporary:
        root = Path(temporary)
        build = root / "context"
        build.mkdir()
        digest = hashlib.sha256(json.dumps(arguments, separators=(",", ":")).encode())
        for name in CONTEXT_FILES:
            data = (source / name).read_bytes()
            digest.update(name.encode() + b"\0" + len(data).to_bytes(8, "big") + data)
            (build / name).write_bytes(data)
        yield build, digest.hexdigest(), root / "image-id"


def inspect(image):
    result = subprocess.run(["podman", "image", "inspect", image], capture_output=True, text=True)
    if result.returncode:
        raise AgentBoxError("cannot inspect workspace image: " + result.stderr.strip())
    try:
        item = json.loads(result.stdout)[0]
        identity = item["Id"]
        if not re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", identity):
            raise ValueError()
        return identity, (item.get("Config", {}).get("Labels") or {}).get(LABEL)
    except (ValueError, KeyError, IndexError, TypeError):
        raise AgentBoxError("cannot read workspace image identity; inspect the local Podman store") from None


def current(image):
    result = subprocess.run(["podman", "image", "exists", image], capture_output=True, text=True)
    if result.returncode == 1:
        return None
    if result.returncode:
        raise AgentBoxError("cannot inspect workspace image state: " + result.stderr.strip())
    return inspect(image)


def build(image, directory, fingerprint, iidfile, arguments, no_cache):
    """Only a complete, verified build may replace the last usable tag."""
    argv = ["podman", "build", "--format=oci", "--layers",
            "--pull=always" if no_cache else "--pull=missing",
            *arguments, "--label", f"{LABEL}={fingerprint}",
            "--iidfile", str(iidfile), "--file", str(directory / "Containerfile")]
    if no_cache:
        argv.append("--no-cache")
    rc = subprocess.call([*argv, str(directory)])
    if rc:
        return rc, ""
    try:
        identity = iidfile.read_text().strip()
    except OSError as exc:
        raise AgentBoxError("workspace build returned no image ID; retry agent-box build") from exc
    if not re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", identity):
        raise AgentBoxError("workspace build returned an invalid image ID; retry agent-box build")
    identity, actual = inspect(identity)
    if actual != fingerprint:
        raise AgentBoxError("workspace build input label differs; retry agent-box build")
    tagged = subprocess.run(["podman", "tag", identity, image], capture_output=True, text=True)
    if tagged.returncode:
        raise AgentBoxError("cannot publish the built workspace image: " + tagged.stderr.strip())
    return 0, identity
