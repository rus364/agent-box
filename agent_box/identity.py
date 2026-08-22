"""Host Unix identity, used to build a container whose user mirrors the host.

Rather than inventing a second, container-only user (as an earlier revision
of this tool did with a hardcoded ``agentbox`` account), the image is built
for *this* host's actual user: same username, UID, GID, and home directory.
That keeps absolute paths meaningful across the host/container boundary (see
``workspace.py``) and keeps existing host-side symlinks — notably the shared
``.agents/skills`` links written by ``sync_claude_agent_skills`` — valid
inside the container without any path rewriting.

Nothing here is a literal username: it is always derived from
``pwd.getpwuid`` (with narrow, validated fallbacks), so the source tree
contains no developer-specific identity. The home directory comes from the
same passwd entry (``paths.passwd_home``) and is never read from ``$HOME``.
"""
from __future__ import annotations

import os
import pwd
import re
from dataclasses import dataclass
from pathlib import Path

from .paths import passwd_home

# Linux useradd's allowed pattern is roughly ^[a-z_][a-z0-9_-]*[$]?$, capped
# at 32 characters. We stay inside that without the trailing '$'.
_VALID_USERNAME = re.compile(r"^[a-z_][a-z0-9_-]{0,30}$")


@dataclass(frozen=True)
class HostIdentity:
    name: str
    uid: int
    gid: int
    home: Path


def _sanitize_username(candidate: str, uid: int) -> str:
    lowered = candidate.strip().lower()
    # Common real-world usernames already fit (e.g. "alex", "vasya"); this
    # only rewrites names that would be invalid for `useradd --login`.
    sanitized = re.sub(r"[^a-z0-9_-]", "-", lowered).strip("-")
    if sanitized and not sanitized[0].isalpha() and sanitized[0] != "_":
        sanitized = f"u{sanitized}"
    sanitized = sanitized[:31]
    if _VALID_USERNAME.match(sanitized):
        return sanitized
    return f"user{uid}"


def detect_host_identity() -> HostIdentity:
    """Read the real Unix identity for the current process from the OS.

    Uses the passwd database (``pwd.getpwuid``) rather than trusting
    ``$USER``/``$HOME``, which a shell environment can set to anything.
    Falls back conservatively if the current UID has no passwd entry (e.g.
    some minimal/containerized hosts) or reports a non-absolute home.
    """
    uid = os.getuid()
    gid = os.getgid()
    try:
        raw_name = pwd.getpwuid(uid).pw_name
    except KeyError:
        raw_name = os.environ.get("USER") or os.environ.get("LOGNAME") or f"user{uid}"
    # Always the passwd home (paths.passwd_home), never $HOME: the build
    # identity, the container's HOME, and the host paths build_mounts binds
    # from must all agree, and a shell-settable variable must not be able to
    # move any of them.
    home = passwd_home(uid)
    name = _sanitize_username(raw_name, uid)
    return HostIdentity(name=name, uid=uid, gid=gid, home=home)
