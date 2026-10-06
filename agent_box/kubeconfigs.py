"""Forward the kubeconfig files the user chose from ``~/.kube`` into a box.

Whole files are copied byte for byte and never merged: the same context,
cluster and user names recur across a user's kubeconfigs, and a merged
``KUBECONFIG`` would silently send kubectl to whichever file came first.
Each copy keeps its file name in a directory the box mounts read-only as
its own ``~/.kube``, so paths and commands match the host's. ``KUBECONFIG``
is left to k3s: with it on, its cluster stays the default and a forwarded
``~/.kube/config`` is used only when named. The copies are re-rendered at every launch, so a file changed on the host
reaches a running box through the atomic replace inside the mounted
directory; a mounted file would keep the old inode.

The files are read only to label them in the menu and to warn about what
cannot work inside the box, with a line scanner rather than a YAML parser
the standard library does not have: a misread label costs a hint, never
the copy, which stays exact.
"""
from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .atomicio import atomic_write_bytes
from .paths import KUBECONFIG_DIR, KUBECONFIG_RUNTIME_DIR, XDG_CACHE_HOME, YC_INSTALLER_BIN

#: Larger files in ~/.kube are not offered: no kubeconfig is this big, a stray archive may be.
MAX_SCANNED = 1 << 20
_LINE = re.compile(r"^(\s*)(-\s+)?([A-Za-z][\w-]*):(?:\s+(.*?))?\s*$")
_ITEM = re.compile(r"^\s*-\s+(.*?)\s*$")
#: The yc commands the image's yc group answers to; any other path to a yc is the host's alone.
BOX_YC_COMMANDS = ("yc", "/usr/local/bin/yc", str(YC_INSTALLER_BIN))
#: Keys whose value is a path to another file the kubeconfig needs.
_FILE_KEYS = ("certificate-authority", "client-certificate", "client-key", "tokenFile")


def _scalar(value: str | None) -> str:
    """A plain or quoted YAML scalar as written on one line, without a trailing comment."""
    value = (value or "").strip()
    if value[:1] in ("'", '"'):
        end = value.find(value[0], 1)
        return value[1:end] if end > 0 else value[1:]
    return value.split(" #", 1)[0].strip()


def _block(lines: list[str], start: int, column: int) -> list[str]:
    """The lines nested under the key at ``start``, which sits at ``column``."""
    nested = []
    for line in lines[start + 1:]:
        if line.strip() and not line.lstrip().startswith("#"):
            if len(line) - len(line.lstrip()) <= column:
                break
            nested.append(line)
    return nested


def _exec(block: list[str]) -> tuple[str, str]:
    """The exec command as written and its ``--profile``, never its other arguments."""
    command, args, in_args = "", [], False
    for line in block:
        key = _LINE.match(line)
        if key and not key.group(2):
            in_args = key.group(3) == "args"
            if key.group(3) == "command":
                command = _scalar(key.group(4))
            continue
        item = _ITEM.match(line)
        if in_args and item:
            args.append(_scalar(item.group(1)))
    profile = next((arg.split("=", 1)[1] for arg in args if arg.startswith("--profile=")), "")
    if not profile and "--profile" in args[:-1]:
        profile = args[args.index("--profile") + 1]
    return command, profile


def _exec_label(command: str, profile: str) -> str:
    """``exec: yc --profile=dixy``: the command's name only, as its path may be long."""
    return " ".join(["exec:", Path(command).name or "(no command)", *([f"--profile={profile}"] if profile else [])])


@dataclass
class Summary:
    """What a kubeconfig is, in words; never a credential value."""

    kubeconfig: bool = False
    contexts: list[str] = field(default_factory=list)
    #: Short labels, such as ``token`` or ``exec: yc --profile=dixy``.
    auth: list[str] = field(default_factory=list)
    #: Why the file may not work inside the box.
    warnings: list[str] = field(default_factory=list)
    #: The yc profiles its users sign in with, "" for yc's current one. yc runs in the box when
    #: its profile is forwarded, so whether that is a problem depends on the launch.
    yc_profiles: list[str] = field(default_factory=list)


def describe(text: str) -> Summary:
    summary, lines, section, dash = Summary(), text.splitlines(), "", None
    users, unread = False, False
    for index, line in enumerate(lines):
        match = _LINE.match(line)
        if not match:
            continue
        indent, item, key, value = len(match.group(1)), match.group(2), match.group(3), _scalar(match.group(4))
        if indent == 0 and not item:
            section, dash = key, None
            if key == "users" and value not in ("", "[]", "null", "~"):
                users = unread = True
            if (key, value) == ("kind", "Config") or key == "contexts":
                summary.kubeconfig = True
            continue
        if section == "contexts":
            dash = indent if item and dash is None else dash
            # A context's own name sits in its list item, not in the nested context mapping.
            if key == "name" and dash is not None and indent == (dash if item else dash + 2):
                summary.contexts.append(value)
        elif section == "users":
            users, label = True, None
            # Flow style ({exec: ...}) hides keys from a line scanner; unread must not pass for safe.
            unread = unread or value[:1] in ("{", "[")
            if key == "exec":
                command, profile = _exec(_block(lines, index, indent))
                label = _exec_label(command, profile)
                if command in BOX_YC_COMMANDS:
                    summary.yc_profiles.append(profile)
                elif Path(command).name == "yc":
                    summary.warnings.append(f"{label} runs {command}, which the box does not have: its yc is "
                                            f"yc on PATH or {YC_INSTALLER_BIN}")
                else:
                    summary.warnings.append(f"{label} does not run inside the box, so this user cannot authenticate there")
            elif key == "auth-provider":
                provider = next((_scalar(m.group(4)) for m in map(_LINE.match, _block(lines, index, indent))
                                 if m and m.group(3) == "name"), "")
                label = f"auth-provider: {provider}" if provider else "auth-provider"
                summary.warnings.append(f"{label} relies on host tooling and may not authenticate inside the box")
            elif key in ("client-certificate-data", "client-certificate"):
                label = "client certificate"
            elif key in ("token", "tokenFile"):
                label = "token"
            elif key in ("username", "password"):
                label = "basic auth"
            if label and label not in summary.auth:
                summary.auth.append(label)
        if section in ("clusters", "users") and key in _FILE_KEYS and value:
            summary.warnings.append(f"{key} refers to the file {value}; the box has it only where that path is mounted")
    if users and (unread or not summary.auth):
        summary.auth.append("unrecognised")
        summary.warnings.append("its authentication could not be read, so it may not work inside the box")
    summary.warnings = list(dict.fromkeys(summary.warnings))
    return summary


@dataclass
class Found:
    files: dict[str, Summary] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)


def discover() -> Found:
    """The kubeconfigs at the top of ``~/.kube``; its cache directories and other files are skipped."""
    found = Found()
    try:
        entries = sorted(KUBECONFIG_DIR.iterdir())
    except FileNotFoundError:
        return found
    except OSError as exc:
        found.problems.append(f"{KUBECONFIG_DIR}: unreadable ({exc.__class__.__name__})")
        return found
    for path in entries:
        try:
            if path.name.startswith(".") or not path.is_file() or path.stat().st_size > MAX_SCANNED:
                continue
            summary = describe(path.read_text(encoding="utf-8", errors="replace"))
        except OSError as exc:
            found.problems.append(f"{path}: unreadable ({exc.__class__.__name__}); it is not offered")
            continue
        if summary.kubeconfig:
            found.files[path.name] = summary
    return found


def choices(chosen: list[str]) -> tuple[list[str], list[str], list[str], list[str]]:
    """Menu options, what each one is, its authentication at a glance, and unreadable files.

    Found files come first; an earlier choice the host no longer has stays
    listed, so it can be seen and unticked rather than silently kept.
    """
    found = discover()
    options = list(found.files) + [name for name in chosen if name not in found.files]
    details, marks = [], []
    for name in options:
        summary = found.files.get(name)
        if summary is None:
            details.append("Selected earlier but no longer found on the host; untick it to stop forwarding it.")
            marks.append("missing")
            continue
        auth = ", ".join(summary.auth) or "no credentials"
        details.append("\n".join([f"{KUBECONFIG_DIR / name}",
                                  f"Contexts: {', '.join(summary.contexts) or 'none'}",
                                  f"Authentication: {auth}", *summary.warnings,
                                  *[f"{_exec_label('yc', profile)} works in the box once yc profile "
                                    f"{profile or '(the current one)'} is forwarded in Cloud CLI profiles"
                                    for profile in dict.fromkeys(summary.yc_profiles)]]))
        marks.append(auth)
    return options, details, marks, found.problems


def _yc_warnings(config, summary: Summary) -> list[str]:
    """yc exec users whose profile this launch does not forward; a missing yc itself is the tool warning's."""
    cloud = config.credentials.cloud
    forwarded = cloud.yc if cloud.active else []
    return [f"{_exec_label('yc', profile)} cannot sign in: yc profile {profile} is not forwarded "
            "(Cloud CLI profiles in agent-box config)" if profile else
            f"{_exec_label('yc', profile)} cannot sign in: no yc profile is forwarded "
            "(Cloud CLI profiles in agent-box config)"
            for profile in dict.fromkeys(summary.yc_profiles)
            if (profile not in forwarded if profile else not forwarded)]


def runtime_dir(workspace_id: str) -> Path:
    return KUBECONFIG_RUNTIME_DIR / "workspaces" / workspace_id


def mounts(config, workspace_id: str) -> list[tuple[Path, Path, bool]]:
    """The directory, never single files: an atomic replace must reach a running box."""
    if not config.credentials.kubernetes.active:
        return []
    return [(runtime_dir(workspace_id), KUBECONFIG_DIR, True)]


def container_env(config) -> list[str]:
    """kubectl's cache, out of the read-only ``~/.kube`` into the box's persistent cache."""
    if not config.credentials.kubernetes.active:
        return []
    return [f"--env=KUBECACHEDIR={XDG_CACHE_HOME / 'kube'}"]


def sync(config, workspace_id: str) -> list[str]:
    """Copy the chosen kubeconfigs for one workspace, or remove them; return what may not work."""
    chosen, directory = config.credentials.kubernetes, runtime_dir(workspace_id)
    if not chosen.active:
        shutil.rmtree(directory, ignore_errors=True)
        return []
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    problems: list[str] = []
    for name in chosen.files:
        source = KUBECONFIG_DIR / name
        try:
            # A symlink is followed: the box gets its target's content as a plain file.
            data = source.read_bytes()
        except OSError as exc:
            # A file gone from the host must not live on in the box as a stale copy.
            (directory / name).unlink(missing_ok=True)
            if not isinstance(exc, FileNotFoundError):
                problems.append(f"Kubeconfig {source}: unreadable ({exc.__class__.__name__}); not forwarded")
            elif source.is_symlink():
                problems.append(f"Kubeconfig {name} is a symlink to the missing {source.readlink()}; not forwarded")
            else:
                problems.append(f"Kubeconfig {name} is not in {KUBECONFIG_DIR}; untick it in agent-box config")
            continue
        atomic_write_bytes(directory / name, data, 0o600)
        summary = describe(data.decode("utf-8", "replace"))
        problems += [f"Kubeconfig {name}: {warning}" for warning in [*summary.warnings, *_yc_warnings(config, summary)]]
    for path in directory.iterdir():
        if path.name not in chosen.files:
            path.unlink(missing_ok=True)
    return problems
