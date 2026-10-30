"""Nested rootless Podman setup; also runs standalone in the image build."""
from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
from pathlib import Path


# These operations need capabilities in the *inner* namespace. The outer
# seccomp filter cannot observe those capabilities after it has been installed.
NAMESPACE_SYSCALLS = frozenset({"chroot", "sethostname"})

# Rootless Podman reads its storage driver only from the user's file; the
# system storage.conf is ignored for rootless use.
USER_STORAGE_CONF = '[storage]\ndriver = "overlay"\n'

CONTAINERS_CONF = '''[containers]
cgroups = "disabled"
default_sysctls = []
netns = "slirp4netns"

[network]
default_rootless_network_cmd = "slirp4netns"

[engine]
cgroup_manager = "cgroupfs"
events_logger = "file"
runtime = "crun"
'''


def host_info() -> dict:
    """Read effective Podman settings, including custom seccomp and ID mappings."""
    result = subprocess.run(
        ["podman", "info", "--format=json"], text=True, capture_output=True,
    )
    if result.returncode:
        raise ValueError(f"Nested Podman: podman info failed: {result.stderr.strip()}")
    try:
        host = json.loads(result.stdout)["host"]
        if host["security"]["rootless"] is not True:
            raise ValueError("Nested Podman requires a rootless outer Podman")
        return host
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Nested Podman: podman info returned invalid host information") from exc


def subid_ranges(mappings: list[dict], user_id: int) -> list[tuple[int, int]]:
    """Preserve every parent mapping extent through keep-id's UID permutation.

    The kernel rejects a new mapping spanning two parent extents, even when
    both endpoints are mapped. A count alone therefore breaks deeper nesting.
    Outer root and the workspace identity are not delegated.
    """
    count = 0
    ordered = sorted(mappings, key=lambda row: row["container_id"])
    for mapping in ordered:
        start, size = mapping["container_id"], mapping["size"]
        if type(start) is not int or type(size) is not int or start != count or size < 1:
            raise ValueError("Nested Podman requires contiguous container ID mappings")
        count += size
    if count < 3 or user_id < 1:
        raise ValueError("Nested Podman requires subordinate UID and GID ranges")
    ranges = []
    # (engine start, engine stop, child offset): engine UID 0 is the user,
    # placed separately at user_id by keep-id, and is never a subordinate ID.
    for start, stop, offset in ((1, min(user_id + 1, count), -1), (user_id + 1, count, 0)):
        for mapping in ordered:
            low = max(start, mapping["container_id"], 1 - offset)
            high = min(stop, mapping["container_id"] + mapping["size"])
            if high > low:
                ranges.append((low + offset, high - low))
    return ranges


def seccomp_profile(profile: dict) -> dict:
    """Extend the effective allowlist without discarding unrelated restrictions."""
    if profile.get("defaultAction") != "SCMP_ACT_ERRNO" or not isinstance(profile.get("syscalls"), list):
        raise ValueError("Nested Podman requires an errno-default seccomp allowlist")
    result = copy.deepcopy(profile)
    rules = []
    for rule in result["syscalls"]:
        # Remove conditional allows and explicit denies before the unconditional
        # allow; libseccomp gives a conflicting errno rule precedence.
        rule["names"] = [name for name in rule["names"] if name not in NAMESPACE_SYSCALLS]
        if rule["names"]:
            rules.append(rule)
    rules.append({"names": sorted(NAMESPACE_SYSCALLS), "action": "SCMP_ACT_ALLOW", "args": []})
    result["syscalls"] = rules
    return result


def configure_image(root: Path, user: str, uid: int, gid: int, home: Path,
                    uid_ranges: list, gid_ranges: list) -> None:
    """Write only image-owned settings; never invoked by a workspace launch."""
    for name, identity, ranges in (("subuid", uid, uid_ranges), ("subgid", gid, gid_ranges)):
        if not ranges or any(type(start) is not int or type(size) is not int or start < 1 or size < 1
                             or start <= identity < start + size for start, size in ranges):
            raise ValueError(f"Invalid nested Podman {name} ranges")
        (root / "etc" / name).write_text(
            "".join(f"{user}:{start}:{size}\n" for start, size in ranges), encoding="utf-8")
    config = root / "etc/containers"
    config.mkdir(parents=True, exist_ok=True)
    (config / "containers.conf").write_text(CONTAINERS_CONF, encoding="utf-8")
    # The launcher mounts a dedicated volume here, so native overlay never uses
    # the outer container's overlay as its upper filesystem.
    user_config = root / home.relative_to("/") / ".config/containers"
    user_config.mkdir(parents=True, exist_ok=True)
    storage = user_config / "storage.conf"
    storage.write_text(USER_STORAGE_CONF, encoding="utf-8")
    for path in (user_config.parent, user_config, storage):
        os.chown(path, uid, gid)
    data = root / home.relative_to("/") / ".local/share/containers"
    data.mkdir(parents=True, exist_ok=True)
    # Podman copies the image mountpoint's owner into a newly created volume.
    os.chown(data, uid, gid)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Configure nested Podman in the image")
    parser.add_argument("--user", required=True)
    parser.add_argument("--uid", type=int, required=True)
    parser.add_argument("--gid", type=int, required=True)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--uid-ranges", type=json.loads, required=True)
    parser.add_argument("--gid-ranges", type=json.loads, required=True)
    args = parser.parse_args()
    configure_image(Path("/"), args.user, args.uid, args.gid, args.home, args.uid_ranges, args.gid_ranges)
