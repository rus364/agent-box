"""Owned native-agent definitions; provider CLIs remain the portable fallback."""
import hashlib
import json
from pathlib import Path

from .atomicio import atomic_write_text
from .config import TIERED_ROLES, TIERS
from .delegation import profile_instructions
from .external_config import has_toml_spelling, toml_string
from .fast_mode import configuration_error, native_limitation


def _codex_field(value):
    """One profile field as TOML text.

    Strings need the project's TOML escaper: a model ID is not restricted to
    TOML's safe characters, and ``json.dumps`` writes a non-BMP one as a
    surrogate pair, which TOML rejects.
    """
    return toml_string(value) if isinstance(value, str) else json.dumps(value)


def profile_name(role, tier=None):
    return f"ab-{role}" + (f"-{tier}" if tier is not None else "")


def body(provider, role, tier, spec):
    name = profile_name(role, tier)
    description = f"Configured agent-box {role}" + (f", tier {tier}" if tier is not None else "")
    instructions = profile_instructions(role)
    if provider == "codex":
        fields = {"name": name, "description": description, "model": spec["model"],
                  "model_reasoning_effort": spec["effort"], "sandbox_mode": "workspace-write" if role == "worker" else "read-only",
                  "developer_instructions": instructions}
        if spec.get("fast", False):
            fields["service_tier"] = "fast"
            fields["features.fast_mode"] = True
        if spec["effort"] == "default":
            fields.pop("model_reasoning_effort")
        return "\n".join(f"{key} = {_codex_field(value)}" for key, value in fields.items()) + "\n"
    tools = "Read, Grep, Glob, Bash, Edit, Write" if role == "worker" else "Read, Grep, Glob"
    effort = "" if spec["effort"] == "default" else f"effort: {json.dumps(spec['effort'])}\n"
    return (f"---\nname: {name}\ndescription: {description}\n"
            f"model: {json.dumps(spec['model'])}\n{effort}tools: {tools}\n---\n{instructions}\n")


def content_for(provider, role, tier, spec):
    value = body(provider, role, tier, spec)
    # Claude requires frontmatter at the start, so ownership uses a sidecar.
    return value


def profile_lists(config):
    """``(role, tier, candidates)`` for each profile: every role's own list, and each nonempty tier list.

    A tier whose list is empty runs the role's own list, so its profile would
    only duplicate the role's; the route then names the role's profile.
    """
    workflow = config.workflow
    lists = [(role, None, workflow.role(role).models) for role in ("scout", *TIERED_ROLES)]
    for role in TIERED_ROLES:
        tiers = workflow.role(role).tiers
        lists += [(role, tier, tiers[str(tier)]) for tier in TIERS
                  if isinstance(tiers, dict) and tiers.get(str(tier))]
    return lists


def profile_candidate(models, provider):
    """The first candidate a native agent of ``provider`` could run, or an empty one.

    A profile serves the provider's own session, so candidates of the other CLI
    and API candidates (which native agents cannot reach) are passed over.
    """
    return next((spec for spec in (models if isinstance(models, list) else [])
                 if spec["provider"] == provider and not spec.get("api_provider")),
                {"provider": provider, "model": "", "effort": "medium", "fast": False, "api_provider": ""})


def sync(config, codex_home, claude_home):
    messages = []
    for provider, home in (("codex", codex_home), ("claude", claude_home)):
        directory = Path(home) / "agents"
        if directory.is_symlink() or Path(home).is_symlink():
            messages.append(f"Preserved user-owned agent directory: {directory}")
            continue
        suffix = ".toml" if provider == "codex" else ".md"
        desired = {profile_name(role, tier) + suffix: (role, tier, models)
                   for role, tier, models in profile_lists(config)}
        # A profile agent-box no longer defines is removed like a disabled one, and only while intact.
        owned = {marker.name.removesuffix(".agent-box") for marker in directory.glob(f"ab-*{suffix}.agent-box")}
        for name in [*desired, *sorted(owned - desired.keys())]:
            role, tier, models = desired.get(name, (None, None, []))
            spec = profile_candidate(models, provider)
            path = directory / name
            marker = directory / (path.name + ".agent-box")
            intact = False
            if path.exists() and not path.is_symlink() and marker.is_file() and not marker.is_symlink():
                intact = marker.read_text().strip() == hashlib.sha256(path.read_bytes()).hexdigest()
            if path.is_symlink() or marker.is_symlink() or (path.exists() or marker.exists()) and not intact:
                messages.append(f"Preserved user-owned or edited agent profile: {path}")
                continue
            enabled = config.workflow.enabled and bool(spec["model"]) and not spec.get("api_provider")
            error = configuration_error(provider, spec)
            if enabled and error:
                messages.append(f"{path.name}: {error}; native profile disabled")
                enabled = False
            if enabled and provider == "claude" and spec.get("fast", False):
                messages.append(f"{path.name}: {native_limitation(provider, True)}")
            if not enabled:
                if intact:
                    path.unlink()
                    marker.unlink()
                continue
            content = content_for(provider, role, tier, spec)
            if not has_toml_spelling(content):
                # Any configured field can carry a lone surrogate, which
                # no UTF-8 write can encode: report the profile as
                # uninstalled instead of raising out of the write. Checking
                # the composed text keeps this from being a field list.
                messages.append(f"{path.name}: the profile would carry text no UTF-8 file can hold; "
                                "native profile not installed")
                continue
            directory.mkdir(parents=True, exist_ok=True)
            atomic_write_text(path, content, 0o644)
            atomic_write_text(marker, hashlib.sha256(content.encode()).hexdigest() + "\n", 0o644)
    return messages
