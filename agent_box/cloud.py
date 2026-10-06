"""Forward the cloud CLI profiles the user chose into a box; Yandex Cloud's yc first.

A profile is copied, never minted: agent-box runs no login and no CLI on
the host, as with registry logins. The box gets a ``config.yaml`` holding
only the chosen profiles, each block copied line for line, and the
``credentials/<profile>`` file of each federated one, refresh token
included, so yc in the box renews its own IAM token without a browser.
History, logs and every other profile stay on the host.

The rendered directory is the box's writable ``~/.config/yandex-cloud``:
yc writes its token cache, history and logs there. It is re-rendered from
the host at every launch, so the host stays the source and a profile
unticked on the host leaves the box at its next launch.

The host's ``config.yaml`` is read with a line scanner rather than a YAML
parser the standard library does not have. yc writes it in one plain
shape: ``current``, then ``profiles`` with one block per profile, whose
multi-line keys are block scalars indented under them. A profile's block
therefore ends at the next line indented no deeper than its name.
"""
from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .atomicio import atomic_write_bytes, atomic_write_text
from .paths import CLOUD_RUNTIME_DIR, YC_CONFIG_DIR

_KEY = re.compile(r"^(\s*)([^\s:#][^:#]*?):(?:\s+(.*?))?\s*$")
#: How each profile key reads in the menu; the first one present wins.
_AUTH = (("federation-id", "federation"), ("service-account-key", "service account key"),
         ("token", "OAuth token"), ("instance-service-account", "instance service account"))


def _name(value: str) -> str:
    value = value.strip()
    return value[1:-1] if len(value) > 1 and value[0] == value[-1] and value[0] in "'\"" else value


@dataclass
class Profiles:
    current: str = ""
    #: Each profile's lines, exactly as the host file has them.
    blocks: dict[str, list[str]] = field(default_factory=dict)
    #: Each profile's own keys, to label it without reading a value.
    keys: dict[str, list[str]] = field(default_factory=dict)


def parse(text: str) -> Profiles:
    found, section, column, name, key_column = Profiles(), "", None, None, None
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            if name is not None:
                found.blocks[name].append(line)
            continue
        indent, match = len(line) - len(line.lstrip()), _KEY.match(line)
        if indent == 0:
            section, name = (match.group(2) if match else ""), None
            if section == "current" and match:
                found.current = _name(match.group(3) or "")
        elif section == "profiles":
            column = indent if column is None else column
            if indent <= column:
                # A line at the profiles' own depth that is not a key ends the block it cannot belong to.
                name = _name(match.group(2)) if match and indent == column else None
                if name is not None:
                    found.blocks[name], found.keys[name], key_column = [line], [], None
            elif name is not None:
                found.blocks[name].append(line)
                key_column = indent if key_column is None else key_column
                if match and indent == key_column:
                    found.keys[name].append(match.group(2))
    return found


def auth(found: Profiles, name: str) -> str:
    keys = found.keys.get(name, [])
    label = next((label for key, label in _AUTH if key in keys), "")
    if not label and (YC_CONFIG_DIR / "credentials" / name).is_file():
        # A federated profile may keep its federation only in credentials/.
        label = "federation"
    return label or "unrecognised"


def render(found: Profiles, chosen: list[str]) -> str:
    current = found.current if found.current in chosen else chosen[0]
    plain = re.fullmatch(r"[A-Za-z0-9_.-]+", current)
    lines = [f"current: {current if plain else json.dumps(current)}", "profiles:"]
    for name in chosen:
        lines += found.blocks[name]
    return "\n".join(lines) + "\n"


def _read() -> tuple[Profiles, list[str]]:
    path = YC_CONFIG_DIR / "config.yaml"
    try:
        return parse(path.read_text(encoding="utf-8")), []
    except FileNotFoundError:
        return Profiles(), []
    except (OSError, UnicodeDecodeError) as exc:
        return Profiles(), [f"{path}: unreadable ({exc.__class__.__name__})"]


def choices(chosen: list[str]) -> tuple[list[str], list[str], list[str], list[str]]:
    """Menu options, what each one is, how it signs in, and why the host file could not be read.

    An earlier choice the host no longer has stays listed, so it can be
    seen and unticked rather than silently kept.
    """
    found, problems = _read()
    options = list(found.blocks) + [name for name in chosen if name not in found.blocks]
    details, marks = [], []
    for name in options:
        if name not in found.blocks:
            details.append("Selected earlier but no longer in the host's yc config; untick it to stop forwarding it.")
            marks.append("missing")
            continue
        label = auth(found, name)
        lines = [f"yc profile {name} from {YC_CONFIG_DIR / 'config.yaml'}", f"Sign-in: {label}"]
        if label == "federation":
            lines.append("Its refresh token is forwarded too: yc in the box renews IAM tokens for up to 31 days; "
                         "after that, sign in again with yc on the host.")
        if label == "instance service account":
            lines.append("Works only on a Yandex Cloud VM; the box has no instance metadata.")
        details.append("\n".join(lines))
        marks.append(label)
    return options, details, marks, problems


def runtime_dir(workspace_id: str) -> Path:
    return CLOUD_RUNTIME_DIR / "workspaces" / workspace_id / "yc"


def mounts(config, workspace_id: str) -> list[tuple[Path, Path, bool]]:
    """Writable: yc keeps its renewed tokens, history and logs beside its config."""
    if not config.credentials.cloud.active:
        return []
    return [(runtime_dir(workspace_id), YC_CONFIG_DIR, False)]


def sync(config, workspace_id: str) -> list[str]:
    """Render the chosen profiles for one workspace, or remove them; return what was not forwarded."""
    cloud, directory = config.credentials.cloud, runtime_dir(workspace_id)
    if not cloud.active:
        shutil.rmtree(directory, ignore_errors=True)
        return []
    credentials = directory / "credentials"
    credentials.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    credentials.chmod(0o700)
    found, problems = _read()
    forwarded = [name for name in cloud.yc if name in found.blocks]
    problems += [f"yc profile {name} is not in {YC_CONFIG_DIR / 'config.yaml'}; untick it in agent-box config"
                 for name in cloud.yc if name not in found.blocks and found.blocks]
    if not found.blocks and not problems:
        problems.append(f"{YC_CONFIG_DIR / 'config.yaml'} has no yc profiles; none forwarded")
    if forwarded:
        atomic_write_text(directory / "config.yaml", render(found, forwarded), 0o600)
    else:
        (directory / "config.yaml").unlink(missing_ok=True)
    for name in forwarded:
        source = YC_CONFIG_DIR / "credentials" / name
        try:
            data = source.read_bytes()
        except FileNotFoundError:
            # A key-based profile has none; a stale copy would sign in as a federation no longer there.
            (credentials / name).unlink(missing_ok=True)
            continue
        except OSError as exc:
            (credentials / name).unlink(missing_ok=True)
            problems.append(f"yc profile {name}: {source} unreadable ({exc.__class__.__name__}); its sign-in not forwarded")
            continue
        atomic_write_bytes(credentials / name, data, 0o600)
    for path in credentials.iterdir():
        if path.name not in forwarded:
            path.unlink(missing_ok=True)
    return problems
