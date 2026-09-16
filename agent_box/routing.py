"""Deterministic model selection: an ordered candidate list per role and tier."""
from __future__ import annotations

import time

from .config import AGENT_PROVIDERS
from .errors import AgentBoxError


def validate_role_tier(role, tier):
    """Tier remains a role boundary even when execution bypasses model routing."""
    if role not in ("worker", "scout", "reviewer") or tier not in (None, 0, 1, 2, 3) or (tier == 0 and role != "scout"):
        raise AgentBoxError("Tier 0 is reserved for scouts; workers and reviewers require tier 1-3")


def applicable_windows(snapshot, model):
    return [window for window in snapshot.get("windows", [])
            if not window.get("models") or any(name in model for name in window["models"])]


def select(config, role, tier, primary, usage=None, *, now=None, writer=None, available=None):
    """The first available candidate of the role's list, with the rest as ordered fallbacks.

    Weighted and usage-scored provider shares were the rejected alternative:
    with one ordered list per role and tier there is nothing left to share
    out, and the list states the preference directly. A candidate whose
    subscription window is exhausted is skipped, not reordered.
    """
    errors = config.validate()
    if errors:
        raise AgentBoxError("; ".join(errors))
    validate_role_tier(role, tier)
    path, models = config.models_for(role, tier)
    pinned = role in config.pins
    if tier is None and not models:
        raise AgentBoxError("Tier 0 is reserved for scouts; workers and reviewers require tier 1-3")
    if primary not in AGENT_PROVIDERS:
        raise AgentBoxError("Specify the primary provider: codex or claude")
    now = time.time() if now is None else now
    usage = usage or {}
    available = set(AGENT_PROVIDERS if available is None else available)
    allowed = [spec for spec in models if spec["provider"] in available]
    if not allowed and models:
        raise AgentBoxError(f"No configured candidate in {path} has its CLI installed here")
    writer = writer or primary
    if pinned:
        order = allowed
        reason = f"Pinned for this session by --{role}"
    elif role == "reviewer":
        if writer not in AGENT_PROVIDERS:
            raise AgentBoxError("Reviewer routing requires the actual writer provider")
        own = [spec for spec in allowed if spec["provider"] == writer]
        if config.review.routing == "same-provider":
            order = own
            reason = "Fresh reviewer from the actual writer provider only"
        else:
            order = [spec for spec in allowed if spec["provider"] != writer] + own
            reason = "Use the other writer provider first; fallback requires unavailability"
    else:
        order = []
        for spec in allowed:
            windows = [] if spec.get("api_provider") else applicable_windows(usage.get(spec["provider"], {}), spec["model"])
            if not any(w["remaining"] <= 0 and w["reset_at"] > now for w in windows):
                order.append(spec)
        if allowed and not order:
            raise AgentBoxError(f"Every candidate in {path} has an exhausted usage limit; wait for a reset or add a candidate")
        reason = "First available candidate"
    if not order:
        raise AgentBoxError(f"No allowed {role} model configured in {path}; no automatic tier downgrade")

    def entry(spec):
        return {"provider": spec["provider"], "model": spec["model"], "effort": spec["effort"],
                **({"api_provider": spec["api_provider"]} if spec.get("api_provider") else {}),
                "fast": spec.get("fast", False), "tier": tier}
    return {**entry(order[0]), "role": role, "reason": f"{reason}; models from {path}",
            "fallbacks": [entry(spec) for spec in order[1:]]}
