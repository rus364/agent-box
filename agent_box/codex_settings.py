"""Own one notification block without reserializing the user's TOML."""
import stat
import tomllib
from pathlib import Path

from .atomicio import atomic_write_text
from .errors import AgentBoxError

NOTIFY_COMMAND = ["agent-box", "notify", "codex"]
NOTIFY_MARKER = "# BEGIN AGENT-BOX NOTIFY"
NOTIFY_BLOCK = (
    f"{NOTIFY_MARKER}\n"
    'notify = ["agent-box", "notify", "codex"]\n'
    "# END AGENT-BOX NOTIFY\n"
)


def reconcile_notify(path: Path, enabled: bool) -> list[str]:
    """Prepend only when vacant; remove only our exact leading block.

    Leading placement guarantees a root key without parsing TOML source spans.
    A moved or edited block belongs to the user; preserving it also avoids
    mistaking marker text inside a multiline string for an owned setting.
    """
    if path.is_symlink() or path.parent.is_symlink():
        return [f"warning: preserved symlinked Codex config: {path}"]
    try:
        exists = path.exists()
        source = path.read_bytes().decode("utf-8") if exists else ""
        data = tomllib.loads(source)
        mode = stat.S_IMODE(path.stat().st_mode) if exists else 0o600
    except OSError as exc:
        raise AgentBoxError(f"Cannot read Codex config {path}; fix file access and retry "
                            "the launch or agent-box config apply") from exc
    except (UnicodeError, ValueError):
        raise AgentBoxError(f"Cannot parse Codex config {path}; file preserved; repair its TOML "
                            "and retry the launch or agent-box config apply") from None

    owned = source.startswith(NOTIFY_BLOCK)
    if owned:
        if enabled:
            return []
        result = source[len(NOTIFY_BLOCK):]
    elif "notify" in data or NOTIFY_MARKER in source:
        if enabled and data.get("notify") == NOTIFY_COMMAND:
            return []
        return [f"warning: preserved user-owned or edited Codex notify in {path}; manage it manually"]
    elif enabled:
        result = NOTIFY_BLOCK + source
    else:
        return []
    try:
        atomic_write_text(path, result, mode)
    except OSError as exc:
        raise AgentBoxError(f"Cannot update Codex notify in {path}; fix file access and retry "
                            "the launch or agent-box config apply") from exc
    return [f"{'installed' if enabled else 'removed'} agent-box notify in {path}"]
