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
    host = re.sub(r"^[a-z]+://", "", key.strip().lower()).split("/", 1)[0]
    # Podman's own alias for Docker Hub's legacy credential key.
    return "docker.io" if host == "index.docker.io" else host


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


def _scope(key: str) -> str:
    """A key as podman matches it: a URL-form legacy key covers its whole host, any other is host[/path]."""
    if re.match(r"^[a-z]+://", key.strip().lower()):
        return registry_host(key)
    host, slash, path = key.strip().lower().rstrip("/").partition("/")
    return registry_host(host) + slash + path


def _entries(data, host) -> dict:
    """The stored logins a file holds for this host, under their own keys."""
    return {key: entry for key, entry in data.get("auths", {}).items() if registry_host(key) == host and _stored(entry)}


def _helper(data, host) -> bool:
    """Whether the file hands this host to a credential helper instead of storing its login."""
    helpers = data.get("credHelpers")
    if isinstance(helpers, dict) and host in map(registry_host, helpers):
        return True
    # A credsStore keeps an empty entry per host it holds.
    return bool(data.get("credsStore")) and host in map(registry_host, data.get("auths", {}))


#: Read left to right, so an escaped backslash before "x41" stays a backslash.
_ESCAPE = re.compile(r"\\(\\|x[0-9A-Fa-f]{2})")


def _scalar(text: str, number: int):
    """One YAML scalar of the forms Helm writes: plain, single- or double-quoted."""
    text = text.strip()
    if text.startswith('"'):
        end = re.match(r'"(?:[^"\\]|\\.)*"', text)
        if not end or text[end.end():].strip()[:1] not in ("", "#"):
            raise ValueError(f"line {number}: unterminated double-quoted value")
        try:
            return json.loads(_ESCAPE.sub(lambda m: "\\\\" if m.group(1) == "\\" else "\\u00" + m.group(1)[1:],
                                          end.group()))
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


_BLOCK = re.compile(r"^\|([1-9]?)([+-]?)\s*(?:#.*)?$")
_KEY = re.compile(r"^(\s*)([A-Za-z_][\w-]*):(?:\s+(.*))?$")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _folded(first: str, rest: list[str], quoted: bool) -> str:
    """Join a flow scalar's lines as YAML folds them: a break is a space, an empty line a newline.

    In a double-quoted value a trailing backslash escapes the break, and a
    newline is written as its escape so the JSON decoder in ``_scalar`` reads it.
    """
    text, breaks = first.rstrip(), 0
    for part in rest:
        if not part:
            breaks += 1
            continue
        escaped = quoted and (len(text) - len(text.rstrip("\\"))) % 2 == 1
        if escaped:
            text = text[:-1] + part
        else:
            text += ("\\n" if quoted else "\n") * breaks if breaks else " "
            text += part
        breaks = 0
    return text


def _value(text: str, lines: list[str], index: int, column: int):
    """The value starting on line ``index`` and the index after it.

    Helm's YAML writer puts a multi-line string, such as a JSON key used as a
    password, in a literal block, and folds a long one containing spaces onto
    lines indented past the key. Either way its lines are those indented past
    the key's column.
    """
    stripped = text.strip()
    end = index + 1
    while end < len(lines) and (not lines[end].strip() or _indent(lines[end]) > column):
        end += 1
    block = _BLOCK.match(stripped)
    if block:
        body = lines[index + 1:end]
        content = [line for line in body if line.strip()]
        indent = column + int(block.group(1)) if block.group(1) else min(map(_indent, content), default=0)
        value = "\n".join(line[indent:] for line in body)
        chomp = block.group(2)
        if chomp != "+":
            value = value.rstrip("\n") + ("" if chomp == "-" or not content else "\n")
        elif body:
            value += "\n"
        return value, end
    while end > index + 1 and not lines[end - 1].strip():
        end -= 1
    rest = [line.strip() for line in lines[index + 1:end]]
    closed = re.fullmatch(r'"(?:[^"\\]|\\.)*"(?:\s+#.*)?|\'(?:[^\']|\'\')*\'(?:\s+#.*)?', stripped)
    if not rest or not stripped or closed:
        # Deeper lines after an empty or complete value are nested structure,
        # which the caller refuses.
        return _scalar(stripped, index + 1), index + 1
    # A comment ends a plain value, so nothing may follow one.
    if stripped[0] not in "\"'" and (re.search(r":\s|\s#", stripped)
                                     or any(re.search(r":\s|\s#|^#|^- ", part) for part in rest)):
        raise ValueError(f"line {index + 2}: unsupported YAML structure")
    return _scalar(_folded(stripped, rest, stripped[0] == '"'), index + 1), end


def _entry(lines: list[str], start: int, dashes: int) -> dict:
    """One repository entry: a flat mapping whose keys all sit in its first key's column."""
    first = re.match(rf"^ {{{dashes}}}-(\s+)([A-Za-z_][\w-]*):(?:\s+(.*))?$", lines[start])
    if not first:
        raise ValueError(f"line {start + 1}: unsupported YAML structure")
    column = first.start(2)
    entry = {}
    entry[first.group(2)], index = _value(first.group(3) or "", lines, start, column)
    while index < len(lines):
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            index += 1
            continue
        pair = _KEY.match(line)
        if not pair or len(pair.group(1)) != column or pair.group(2) in entry:
            # Deeper is a nested mapping or list, whose fields must not pass
            # for the entry's own; a repeated key is ambiguous.
            raise ValueError(f"line {index + 1}: unsupported YAML structure")
        entry[pair.group(2)], index = _value(pair.group(3) or "", lines, index, column)
    return entry


def _entry_name(lines: list[str], dashes: int):
    """The name an unreadable entry gives, if a line in its key column states it plainly."""
    first = re.match(rf"^ {{{dashes}}}-(\s+)", lines[0])
    column = first.end() if first else dashes + 2
    for number, line in enumerate(lines):
        named = re.match(r"^(\s*)(?:-\s+)?name:\s+(.*)$", line)
        if named and (number == 0 or len(named.group(1)) == column):
            try:
                name = _scalar(named.group(2), 0)
            except ValueError:
                return None
            return name if isinstance(name, str) else None
    return None


def parse_repositories(text: str) -> tuple[list[dict], list[tuple[str | None, str]]]:
    """The readable entries of a Helm ``repositories.yaml``, and the unreadable ones.

    The standard library has no YAML parser and agent-box has no
    dependencies, so this reads what Helm itself writes: top-level keys and a
    ``repositories`` list of flat mappings whose values are plain, quoted,
    folded onto further lines or literal blocks. An entry beyond that is
    returned as ``(name or None, problem)`` and left out instead of being
    half-read; the others still count. Raises ValueError when the file's own
    structure is not understood.
    """
    if text.lstrip().startswith("{"):
        # agent-box's own JSON copy, which a launch nested in a box reads.
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"line {exc.lineno}: not valid JSON") from None
        entries = data.get("repositories") if isinstance(data, dict) else None
        if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
            raise ValueError("line 1: JSON without a repositories list")
        return entries, []
    lines, entries, failures, index, seen = text.splitlines(), [], [], 0, set()
    while index < len(lines):
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            index += 1
            continue
        top = _KEY.match(line)
        if not top or top.group(1) or top.group(2) in seen:
            raise ValueError(f"line {index + 1}: unsupported YAML structure")
        seen.add(top.group(2))
        index += 1
        value = re.split(r"\s#", top.group(3) or "", maxsplit=1)[0].strip()
        if top.group(2) != "repositories":
            # A value nobody here reads, apiVersion or generated: skip its lines.
            while index < len(lines) and (not lines[index].strip() or lines[index][0].isspace()
                                          or lines[index].startswith("#")):
                index += 1
            continue
        if value not in ("", "[]"):
            raise ValueError(f"line {index}: unsupported repositories value")
        dashes = None
        while index < len(lines):
            line = lines[index]
            if not line.strip() or line.lstrip().startswith("#"):
                index += 1
                continue
            if not re.match(r"^\s*- ", line) or dashes not in (None, _indent(line)):
                break
            dashes, start = _indent(line), index
            index += 1
            # An entry runs to the next line at or left of its dash; a value's
            # lines are always indented past its keys.
            while index < len(lines) and (not lines[index].strip() or _indent(lines[index]) > dashes
                                          or lines[index].lstrip().startswith("#")):
                index += 1
            chunk = lines[start:index]
            try:
                entries.append(_entry(chunk, 0, dashes))
            except ValueError as exc:
                where = str(exc).split(":", 1)
                number = start + int(where[0].split()[1])
                failures.append((_entry_name(chunk, dashes), f"line {number}:{where[1]}"))
    return entries, failures


def _repositories(problems: list[str], chosen=None) -> tuple[dict[str, dict], set | None]:
    """Readable entries by name, the first of each name as in Helm's own lookup.

    Also returns the names already reported, None when the whole file was.
    An entry that cannot be read is reported when it is chosen, or nameless,
    or for the menu (``chosen`` None); a name that one of them gives is left
    out altogether, so a later entry with the same name cannot stand in for it.
    """
    try:
        text = HELM_REPOSITORY_CONFIG.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, set()
    except (OSError, UnicodeDecodeError) as exc:
        problems.append(f"{HELM_REPOSITORY_CONFIG}: unreadable ({exc.__class__.__name__}); no Helm repository is forwarded")
        return {}, None
    try:
        entries, failures = parse_repositories(text)
    except ValueError as exc:
        problems.append(f"{HELM_REPOSITORY_CONFIG}: {exc}; no Helm repository is forwarded")
        return {}, None
    for name, problem in failures:
        if chosen is None or name is None or name in chosen:
            label = f"the entry for {name}" if name is not None else "an entry"
            problems.append(f"{HELM_REPOSITORY_CONFIG}: {problem}; {label} cannot be read and is not forwarded")
    unreadable = {name for name, _ in failures}
    available = {}
    for entry in entries:
        name = entry.get("name")
        if isinstance(name, str) and name not in unreadable:
            available.setdefault(name, entry)
    return available, unreadable


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
    #: Repository name -> the origin of its URL.
    repositories: dict[str, str] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)


def discover() -> Found:
    found = Found()
    tools = {"containers": "podman, skopeo", "helm": "helm"}
    for kind, files in _auth_sources(found.problems).items():
        for path, data in files:
            helpers = data.get("credHelpers") if isinstance(data.get("credHelpers"), dict) else {}
            for host in dict.fromkeys(map(registry_host, [*data.get("auths", {}), *helpers])):
                if _entries(data, host):
                    found.hosts.setdefault(host, []).append(f"{path} ({tools[kind]})")
                elif _helper(data, host):
                    found.hosts.setdefault(host, []).append(f"{path} ({tools[kind]}; credential helper, not forwarded)")
    for name, entry in _repositories(found.problems)[0].items():
        # The origin only: user information, the path and the query may all carry a token.
        url = urllib.parse.urlsplit(str(entry.get("url", "")))
        found.repositories[name] = urllib.parse.urlunsplit((url.scheme, url.netloc.rpartition("@")[2], "", "", ""))
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
        wanted, forwarded, helpers = registry_host(host), False, []
        for kind, files in sources.items():
            # Podman tries the files in order for each image, so a later file's key
            # counts only where no earlier key covers the same or a wider scope.
            stored = {}
            for _, data in files:
                earlier = [_scope(key) for key in stored]
                stored.update({key: entry for key, entry in _entries(data, wanted).items()
                               if not any(_scope(key) == scope or _scope(key).startswith(scope + "/")
                                          for scope in earlier)})
            if stored:
                rendered[kind].update(stored)
                forwarded = True
            else:
                # Reported per source: a login stored for Helm does not make podman's helper usable.
                helpers += [path for path, data in files if _helper(data, wanted)]
        problems += [f"Registry {host}: the login in {path} is kept by a credential helper; "
                     "only stored logins are forwarded" for path in helpers]
        if not forwarded and not helpers:
            problems.append(f"Registry {host} has no stored login on the host; run podman login or helm registry "
                            "login there, or untick it in agent-box config")
    for kind, name in (("containers", AUTH_FILE), ("helm", HELM_REGISTRY_FILE)):
        if registries.hosts:
            atomic_write_json(directory / name, {"auths": rendered[kind]}, 0o600)
        else:
            (directory / name).unlink(missing_ok=True)
    if registries.helm_repositories:
        available, reported = _repositories(problems, registries.helm_repositories)
        for name in registries.helm_repositories:
            if reported is not None and name not in available and name not in reported:
                problems.append(f"Helm repository {name} is not in {HELM_REPOSITORY_CONFIG}; "
                                "run helm repo add there, or untick it in agent-box config")
        # JSON is YAML, so Helm reads it without agent-box writing YAML.
        atomic_write_json(directory / HELM_REPOSITORIES_FILE, {"apiVersion": "", "repositories": [
            available[name] for name in registries.helm_repositories if name in available]}, 0o600)
    else:
        (directory / HELM_REPOSITORIES_FILE).unlink(missing_ok=True)
    return problems
