"""Owned native-agent definitions; provider CLIs remain the portable fallback."""
import hashlib
import json
from pathlib import Path

from .atomicio import atomic_write_text
from .delegation import role_instructions

ROLES = {"scout": (0, 1, 2), "worker": (1, 2, 3), "reviewer": (1, 2, 3)}


def body(provider, role, tier, spec):
    name = f"ab-{role}-{tier}"
    instructions = role_instructions(role)
    if provider == "codex":
        fields = {"name": name, "description": f"Configured agent-box {role}, tier {tier}", "model": spec["model"],
                  "model_reasoning_effort": spec["effort"], "sandbox_mode": "workspace-write" if role == "worker" else "read-only",
                  "developer_instructions": instructions}
        if spec["effort"] == "default":
            fields.pop("model_reasoning_effort")
        return "\n".join(f"{key} = {json.dumps(value)}" for key, value in fields.items()) + "\n"
    tools = "Read, Grep, Glob, Bash, Edit, Write" if role == "worker" else "Read, Grep, Glob"
    effort = "" if spec["effort"] == "default" else f"effort: {json.dumps(spec['effort'])}\n"
    return (f"---\nname: {name}\ndescription: Configured agent-box {role}, tier {tier}\n"
            f"model: {json.dumps(spec['model'])}\n{effort}tools: {tools}\n---\n{instructions}\n")


def content_for(provider, role, tier, spec):
    value = body(provider, role, tier, spec)
    # Claude requires frontmatter at the start, so ownership uses a sidecar.
    return value


def sync(config, codex_home, claude_home):
    messages = []
    for provider, home in (("codex", codex_home), ("claude", claude_home)):
        directory = Path(home) / "agents"
        if directory.is_symlink() or Path(home).is_symlink():
            messages.append(f"Preserved user-owned agent directory: {directory}")
            continue
        for role, tiers in ROLES.items():
            for tier in tiers:
                matrix = config.workflow.review.tiers if role == "reviewer" else config.workflow.tiers
                spec = matrix[str(tier)][provider]
                path = directory / (f"ab-{role}-{tier}" + (".toml" if provider == "codex" else ".md"))
                marker = directory / (path.name + ".agent-box")
                intact = False
                if path.exists() and not path.is_symlink() and marker.is_file() and not marker.is_symlink():
                    intact = marker.read_text().strip() == hashlib.sha256(path.read_bytes()).hexdigest()
                if path.is_symlink() or marker.is_symlink() or (path.exists() or marker.exists()) and not intact:
                    messages.append(f"Preserved user-owned or edited agent profile: {path}")
                    continue
                enabled = config.workflow.enabled and bool(spec["model"])
                if not enabled:
                    if intact:
                        path.unlink()
                        marker.unlink()
                    continue
                directory.mkdir(parents=True, exist_ok=True)
                content = content_for(provider, role, tier, spec)
                atomic_write_text(path, content, 0o644)
                atomic_write_text(marker, hashlib.sha256(content.encode()).hexdigest() + "\n", 0o644)
    return messages


def available(provider, role, tier, spec):
    """Inspect the installed profile only; the coordinator checks the live tool.

    Project/custom agents can shadow this file and explicit spawn can work
    without it. Neither capability is discoverable from the host profile.
    """
    import os
    from .paths import CODEX_HOME, CLAUDE_HOME
    # A native subagent may inherit the coordinator's effort when its profile
    # omits the field, so only the managed CLI can promise the configured default.
    if spec["effort"] == "default":
        return False
    if provider == "claude" and (os.environ.get("CLAUDE_CODE_SAFE_MODE") or
            os.environ.get("CLAUDE_CODE_SUBAGENT_MODEL_FORCE") or
            os.environ.get("CLAUDE_CODE_SUBAGENT_MODEL", spec["model"]) != spec["model"] or
            os.environ.get("CLAUDE_CODE_EFFORT_LEVEL", spec["effort"]) != spec["effort"]):
        return False
    home = CODEX_HOME if provider == "codex" else CLAUDE_HOME
    path = home / "agents" / (f"ab-{role}-{tier}" + (".toml" if provider == "codex" else ".md"))
    try:
        return not path.is_symlink() and path.read_text() == content_for(provider, role, tier, spec)
    except OSError:
        return False
