"""Forward the container-registry and Helm logins the user chose into a box.

The host keeps three independent login files: containers' ``auth.json``
(podman, skopeo, buildah), Helm's OCI registry ``config.json`` and Helm's
``repositories.yaml``. Their contents differ -- Helm often holds registries
podman does not -- so each is filtered into its own rendered copy rather than
merged into one. The copies are re-rendered at every launch into a directory
the box mounts read-only, so a login renewed on the host (Yandex IAM tokens
last 12 hours) reaches a running box at its next launch through the atomic
replace; a mounted file would keep the old inode.
"""
from __future__ import annotations

import json
import re
import shutil
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

from .atomicio import atomic_write_json
from .paths import (
    CONTAINER_REGISTRY_RUNTIME_DIR,
    CONTAINERS_AUTH_FILES,
    HELM_REGISTRY_CONFIG,
    HELM_REPOSITORY_CONFIG,
    REGISTRY_RUNTIME_DIR,
)

#: Rendered file -> the variable that points its tools at it inside the box.
AUTH_FILE, HELM_REGISTRY_FILE, HELM_REPOSITORIES_FILE = "auth.json", "helm-registry.json", "helm-repositories.yaml"


def registry_host(key: str) -> str:
    """``cr.yandex`` for ``cr.yandex``, ``cr.yandex/folder/repo`` or ``https://cr.yandex/v2/``."""
    return re.sub(r"^[a-z]+://", "", key.strip().lower()).split("/", 1)[0]


def _stored(entry) -> bool:
    """An entry carrying the credential itself; an empty one defers to a helper."""
    return isinstance(entry, dict) and bool(entry.get("auth") or entry.get("identitytoken"))


def _read_auth(path: Path):
    """The parsed auth file, None when absent; ValueError when it cannot be read as one."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path}: not a readable JSON auth file ({exc.__class__.__name__})") from None
    if not isinstance(data, dict) or not isinstance(data.get("auths", {}), dict):
        raise ValueError(f"{path}: not an auth file with an auths object")
    return data


def _helper(data, host) -> bool:
    """Whether the file hands this host to a credential helper instead of storing its login."""
    helpers = data.get("credHelpers")
    if isinstance(helpers, dict) and host in map(registry_host, helpers):
        return True
    # A credsStore keeps an empty entry per host it holds.
    return bool(data.get("credsStore")) and host in map(registry_host, data.get("auths", {}))


_ESCAPE = re.compile(r"\\x([0-9A-Fa-f]{2})")


def _scalar(text: str, number: int):
    """One YAML scalar of the forms Helm writes: plain, single- or double-quoted."""
    text = text.strip()
    if text.startswith('"'):
        end = re.match(r'"(?:[^"\\]|\\.)*"', text)
        if not end or text[end.end():].strip()[:1] not in ("", "#"):
            raise ValueError(f"line {number}: unterminated double-quoted value")
        try:
            return json.loads(_ESCAPE.sub(r"\\u00\1", end.group()))
        except json.JSONDecodeError:
            raise ValueError(f"line {number}: unsupported escape in a double-quoted value") from None
    if text.startswith("'"):
        end = re.match(r"'(?:[^']|'')*'", text)
        if not end or text[end.end():].strip()[:1] not in ("", "#"):
            raise ValueError(f"line {number}: unterminated single-quoted value")
        return end.group()[1:-1].replace("''", "'")
    text = re.split(r"\s#", text, maxsplit=1)[0].rstrip()
    if text[:1] in tuple("|>&*!{[%@`"):
        raise ValueError(f"line {number}: unsupported YAML value")
    return {"true": True, "false": False}.get(text, text)


def parse_repositories(text: str) -> list[dict]:
    """The entries of a Helm ``repositories.yaml``.

    The standard library has no YAML parser and agent-box has no
    dependencies, so this reads the subset Helm itself writes: top-level keys
    and a ``repositories`` list of flat mappings. Anything else raises
    ValueError with the line, so a hand-written file is reported instead of
    being half-read.
    """
    entries, current, section, indent = [], None, None, None
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        item = re.match(r"^(\s*)-\s+([A-Za-z_][\w-]*):(?:\s+(.*))?$", line)
        pair = re.match(r"^(\s*)([A-Za-z_][\w-]*):(?:\s+(.*))?$", line)
        if item and section == "repositories":
            current, indent = {item.group(2): _scalar(item.group(3) or "", number)}, len(item.group(1))
            entries.append(current)
        elif pair and not pair.group(1):
            section, current = pair.group(2), None
            value = (pair.group(3) or "").strip()
            if section == "repositories" and value not in ("", "[]"):
                raise ValueError(f"line {number}: unsupported repositories value")
        elif pair and current is not None and len(pair.group(1)) > indent:
            current[pair.group(2)] = _scalar(pair.group(3) or "", number)
        else:
            raise ValueError(f"line {number}: unsupported YAML structure")
    return entries


def _repositories(problems: list[str]) -> dict[str, dict]:
    try:
        text = HELM_REPOSITORY_CONFIG.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as exc:
        problems.append(f"{HELM_REPOSITORY_CONFIG}: unreadable ({exc.__class__.__name__}); no Helm repository is forwarded")
        return {}
    try:
        entries = parse_repositories(text)
    except ValueError as exc:
        problems.append(f"{HELM_REPOSITORY_CONFIG}: {exc}; no Helm repository is forwarded")
        return {}
    return {entry["name"]: entry for entry in entries if isinstance(entry.get("name"), str)}


def _auth_sources(problems: list[str]) -> dict[str, list[tuple[Path, dict]]]:
    """Each OCI login kind with its readable files, in lookup order."""
    sources = {}
    for kind, paths in (("containers", CONTAINERS_AUTH_FILES), ("helm", (HELM_REGISTRY_CONFIG,))):
        sources[kind] = []
        for path in paths:
            try:
                data = _read_auth(path)
            except ValueError as exc:
                problems.append(f"{exc}; its registry logins are not forwarded")
                continue
            if data is not None:
                sources[kind].append((path, data))
    return sources


@dataclass
class Found:
    """What the host holds, for the menu; never a credential value."""

    #: Host -> where its login is stored, in words.
    hosts: dict[str, list[str]] = field(default_factory=dict)
    #: Repository name -> its URL without user information.
    repositories: dict[str, str] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)


def discover() -> Found:
    found = Found()
    tools = {"containers": "podman, skopeo", "helm": "helm"}
    for kind, files in _auth_sources(found.problems).items():
        for path, data in files:
            keys = [registry_host(key) for key, entry in data.get("auths", {}).items() if _stored(entry)]
            helpers = data.get("credHelpers") if isinstance(data.get("credHelpers"), dict) else {}
            for host in dict.fromkeys(keys):
                found.hosts.setdefault(host, []).append(f"{path} ({tools[kind]})")
            for host in dict.fromkeys(map(registry_host, helpers)):
                if host not in keys:
                    found.hosts.setdefault(host, []).append(f"{path} ({tools[kind]}; credential helper, not forwarded)")
    for name, entry in _repositories(found.problems).items():
        url = urllib.parse.urlsplit(str(entry.get("url", "")))
        found.repositories[name] = url._replace(netloc=url.netloc.rpartition("@")[2]).geturl()
    return found


def choices(field_name: str, chosen: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Menu options for ``hosts`` or ``helm_repositories``, what each one is, and unreadable sources.

    Found entries come first; an earlier choice the host no longer has stays
    listed, so it can be seen and unticked rather than silently kept.
    """
    found = discover()
    if field_name == "hosts":
        known = {host: "Stored login: " + "; ".join(where) for host, where in found.hosts.items()}
    else:
        known = {name: f"Helm repository {url or '(no URL)'} in {HELM_REPOSITORY_CONFIG}"
                 for name, url in found.repositories.items()}
    options = sorted(known) + [item for item in chosen if item not in known]
    gone = "Selected earlier but no longer found on the host; untick it to stop forwarding it."
    return options, [known.get(option, gone) for option in options], found.problems


def runtime_dir(workspace_id: str) -> Path:
    return REGISTRY_RUNTIME_DIR / "workspaces" / workspace_id


def mounts(config, workspace_id: str) -> list[tuple[Path, Path, bool]]:
    """The directory, never single files: an atomic replace must reach a running box."""
    if not config.credentials.registries.active:
        return []
    return [(runtime_dir(workspace_id), CONTAINER_REGISTRY_RUNTIME_DIR, True)]


def container_env(config) -> list[str]:
    """Paths only; the credentials stay in the mounted files, out of Podman's argv."""
    registries = config.credentials.registries
    if not registries.active:
        return []
    files = []
    if registries.hosts:
        files += [("REGISTRY_AUTH_FILE", AUTH_FILE), ("HELM_REGISTRY_CONFIG", HELM_REGISTRY_FILE)]
    if registries.helm_repositories:
        files.append(("HELM_REPOSITORY_CONFIG", HELM_REPOSITORIES_FILE))
    return [f"--env={name}={CONTAINER_REGISTRY_RUNTIME_DIR / file}" for name, file in files]


def sync(config, workspace_id: str) -> list[str]:
    """Render the chosen logins for one workspace, or remove them; return what could not be forwarded."""
    registries, directory = config.credentials.registries, runtime_dir(workspace_id)
    if not registries.active:
        shutil.rmtree(directory, ignore_errors=True)
        return []
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    problems: list[str] = []
    sources = _auth_sources(problems)
    rendered = {"containers": {}, "helm": {}}
    for host in registries.hosts:
        wanted, forwarded, helper = host.lower(), False, False
        for kind, files in sources.items():
            for _, data in files:
                entries = {key: entry for key, entry in data.get("auths", {}).items()
                           if registry_host(key) == wanted and _stored(entry)}
                if entries:
                    # The first file holding the host wins, as in podman's own lookup.
                    rendered[kind].update(entries)
                    forwarded = True
                    break
                helper = helper or _helper(data, wanted)
        if not forwarded:
            problems.append(
                f"Registry {host} is logged in through a credential helper on the host; only stored logins are forwarded"
                if helper else
                f"Registry {host} has no stored login on the host; run podman login or helm registry login there, "
                "or untick it in agent-box config")
    for kind, name in (("containers", AUTH_FILE), ("helm", HELM_REGISTRY_FILE)):
        if registries.hosts:
            atomic_write_json(directory / name, {"auths": rendered[kind]}, 0o600)
        else:
            (directory / name).unlink(missing_ok=True)
    if registries.helm_repositories:
        before = len(problems)
        available = _repositories(problems)
        readable = len(problems) == before
        for name in registries.helm_repositories:
            if readable and name not in available:
                problems.append(f"Helm repository {name} is not in {HELM_REPOSITORY_CONFIG}; "
                                "run helm repo add there, or untick it in agent-box config")
        # JSON is YAML, so Helm reads it without agent-box writing YAML.
        atomic_write_json(directory / HELM_REPOSITORIES_FILE, {"apiVersion": "", "repositories": [
            available[name] for name in registries.helm_repositories if name in available]}, 0o600)
    else:
        (directory / HELM_REPOSITORIES_FILE).unlink(missing_ok=True)
    return problems
