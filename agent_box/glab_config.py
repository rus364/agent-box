"""Materialize glab keyring credentials without sharing the host keyring or OAuth renewal."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from .atomicio import atomic_write_text
from .errors import AgentBoxError


_PAIR = re.compile(r'''^( *)("(?:[^"\\]|\\.)*"|'(?:[^']|'')*'|[^\s][^\n]*?):(?: +(.*))?$''')
_REFRESH = {"oauth2_refresh_token", "refresh_token"}
_AUTH = _REFRESH | {"token", "job_token", "use_keyring", "is_oauth2", "oauth2_expiry_date"}


@dataclass
class Field:
    key: str
    value: str
    start: int
    end: int
    indent: int


def _unsupported(line: int) -> AgentBoxError:
    # Input and parser exceptions may contain credentials, even in malformed keys.
    return AgentBoxError(f"Unsupported glab config.yml structure at line {line + 1}; "
                         "use block mappings and plain or quoted authentication values (no YAML aliases or merges)")


def _scalar(text: str, line: int) -> str:
    text = text.strip()
    if text.startswith('"'):
        try:
            value, end = json.JSONDecoder().raw_decode(text)
        except ValueError:
            raise _unsupported(line) from None
        if not isinstance(value, str) or text[end:].strip()[:1] not in ("", "#"):
            raise _unsupported(line)
        return value
    if text.startswith("'"):
        match = re.fullmatch(r"'((?:[^']|'')*)' *(?:#.*)?", text)
        if not match:
            raise _unsupported(line)
        return match[1].replace("''", "'")
    text = re.split(r"\s#", text, maxsplit=1)[0].rstrip()
    if text.startswith("#"):
        return ""
    if text[:1] in tuple("|>&*!{[?%@`") or re.search(r":\s|[\x00-\x1f]", text):
        raise _unsupported(line)
    return text


def _fields(lines: list[str], start: int, end: int, indent: int) -> list[Field]:
    """Index one block mapping; opaque non-authentication values keep their original lines.

    Unlike the Helm/yc readers, credential rewriting must reject ambiguous keys
    and merges rather than skip them. Nested custom headers need no interpretation.
    """
    fields, keys = [], set()
    for index in range(start, end):
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        column = len(line) - len(line.lstrip())
        if "\t" in line[:column] or column < indent:
            raise _unsupported(index)
        if column > indent:
            if not fields:
                raise _unsupported(index)
            continue
        pair = _PAIR.fullmatch(line)
        if not pair:
            raise _unsupported(index)
        key = _scalar(pair[2], index)
        if not key or key == "<<" or key in keys:
            raise _unsupported(index)
        keys.add(key)
        if fields:
            fields[-1].end = index
        fields.append(Field(key, pair[3] or "", index, end, indent))
    return fields


def _children(lines: list[str], field: Field) -> list[Field]:
    value = field.value.split("#", 1)[0].strip()
    if value not in ("", "{}"):
        raise _unsupported(field.start)
    for index in range(field.start + 1, field.end):
        line = lines[index]
        if line.strip() and not line.lstrip().startswith("#"):
            indent = len(line) - len(line.lstrip())
            if value or indent <= field.indent:
                raise _unsupported(index)
            return _fields(lines, index, field.end, indent)
    return []


def _auth_value(lines: list[str], field: Field) -> str:
    if any(line.strip() and not line.lstrip().startswith("#")
           for line in lines[field.start + 1:field.end]):
        raise _unsupported(field.start)
    return _scalar(field.value, field.start)


def _keyring_credentials(host: str, number: int) -> dict[str, str]:
    """A minimal disposable config prevents glab's global/file/local token fallback.

    `config get` itself does not refresh tokens, but glab startup can rewrite
    config and send telemetry. Both side effects stay out of the host config.
    """
    failure = f"Could not copy glab keyring credentials for configured host #{number}"
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("GLAB_", "GITLAB_", "GIT_", "CI_"))
           and key not in {"OAUTH_TOKEN", "USE_KEYRING", "DEBUG", "DEBUG_HTTP"}}
    with tempfile.TemporaryDirectory(prefix="agent-box-glab-") as directory:
        root = Path(directory)
        atomic_write_text(root / "config.yml", json.dumps({"hosts": {host: {"use_keyring": "true"}}}), 0o600)
        env.update(GLAB_CONFIG_DIR=directory, GLAB_CHECK_UPDATE="false", GLAB_SEND_TELEMETRY="false",
                   GIT_CEILING_DIRECTORIES=directory, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        credentials = {}
        for key in ("token", "job_token"):
            try:
                result = subprocess.run(["glab", "config", "get", key, "--host", host],
                                        cwd=root, env=env, capture_output=True, text=True, timeout=15)
            except FileNotFoundError:
                raise AgentBoxError(f"{failure}: install glab on the host and retry") from None
            except (OSError, UnicodeError, subprocess.TimeoutExpired):
                raise AgentBoxError(f"{failure}: lookup failed or timed out; unlock the host keyring and retry") from None
            if result.returncode:
                raise AgentBoxError(f"{failure}: glab could not read the host keyring; unlock it and retry")
            value = result.stdout.removesuffix("\n")
            if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
                raise AgentBoxError(f"{failure}: invalid credential returned by glab")
            credentials[key] = value
        if not any(credentials.values()):
            raise AgentBoxError(f"{failure}: no access or job token was found; sign in with glab on the host")
        return credentials


def snapshot_config(path: Path) -> str | None:
    """Return a sanitized creation-time config; resolve every host before publishing any copy."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError):
        raise AgentBoxError("Could not read the selected glab config.yml") from None
    lines = text.splitlines()
    root = _fields(lines, 0, len(lines), 0)
    edits: dict[int, tuple[int, list[str]]] = {}
    for field in root:
        if field.key in _AUTH:
            _auth_value(lines, field)
        if field.key in _REFRESH:
            edits[field.start] = (field.end, [])
        if field.key != "hosts":
            continue
        for number, host in enumerate(_children(lines, field), 1):
            settings = _children(lines, host)
            auth = {item.key: _auth_value(lines, item) for item in settings if item.key in _AUTH}
            use_keyring = auth.get("use_keyring", "")
            if use_keyring not in ("", "true", "false"):
                raise _unsupported(host.start)
            for item in settings:
                if item.key in _REFRESH:
                    edits[item.start] = (item.end, [])
            if use_keyring != "true":
                continue
            if not re.fullmatch(r"(?:[A-Za-z0-9][A-Za-z0-9.-]*|\[[A-Fa-f0-9:]+\])(?::[0-9]+)?", host.key):
                raise _unsupported(host.start)
            credentials = _keyring_credentials(host.key, number)
            # Rebuild only this host's auth fields, dropping stale file fallbacks.
            replacement = [lines[host.start]]
            remove = _REFRESH | {"token", "job_token", "use_keyring"}
            for item in settings:
                if item.key not in remove:
                    replacement.extend(lines[item.start:item.end])
                edits.pop(item.start, None)
            indent = " " * settings[0].indent
            for key, value in {"use_keyring": "false", **credentials}.items():
                replacement.append(f"{indent}{key}: {json.dumps(value)}")
            edits[host.start] = (host.end, replacement)
    if not edits:
        return text
    for start, (end, replacement) in sorted(edits.items(), reverse=True):
        lines[start:end] = replacement
    return "\n".join(lines) + "\n"
