"""Deterministic model selection: an ordered candidate list per role, and per task tier when enabled."""
from __future__ import annotations

import time

from .config import AGENT_PROVIDERS, TIERED_ROLES, TIERS
from .errors import AgentBoxError


def applicable_windows(snapshot, model):
    return [window for window in snapshot.get("windows", [])
            if not window.get("models") or any(name in model for name in window["models"])]


def select(config, role, tier, usage=None, *, now=None, available=None):
    """The first available candidate of the role's list, with the rest as ordered fallbacks.

    ``tier`` is the effective task tier, and scouts have none. Weighted and
    usage-scored provider shares were the rejected alternative: with one
    ordered list per role there is nothing left to share out, and the list
    states the preference directly. Reviewers once ran the other provider than
    the writer first; the list now states that preference too. A candidate
    whose subscription window is exhausted is skipped, not reordered.
    """
    errors = config.validate()
    if errors:
        raise AgentBoxError("; ".join(errors))
    if tier is not None and (role not in TIERED_ROLES or tier not in TIERS):
        raise AgentBoxError(f"A {role} has no tier {tier}; task tiers 1-3 select worker and reviewer lists")
    path, models = config.models_for(role, tier)
    pinned = role in config.pins
    now = time.time() if now is None else now
    usage = usage or {}
    available = set(AGENT_PROVIDERS if available is None else available)
    allowed = [spec for spec in models if spec["provider"] in available]
    if not allowed and models:
        raise AgentBoxError(f"No configured candidate in {path} has its CLI installed here")
    if not pinned:
        # A pin names its model outright; a list skips a known exhausted subscription window.
        usable = [spec for spec in allowed if spec.get("api_provider") or not any(
            w["remaining"] <= 0 and w["reset_at"] > now
            for w in applicable_windows(usage.get(spec["provider"], {}), spec["model"]))]
        if allowed and not usable:
            raise AgentBoxError(f"Every candidate in {path} has an exhausted usage limit; wait for a reset or add a candidate")
        allowed = usable
    if not allowed:
        raise AgentBoxError(f"No {role} candidate configured in {path}")
    reason = f"Pinned for this session by --{role}" if pinned else "First available candidate"

    def entry(spec):
        return {"provider": spec["provider"], "model": spec["model"], "effort": spec["effort"],
                **({"api_provider": spec["api_provider"]} if spec.get("api_provider") else {}),
                "speed": spec.get("speed", "inherit"), "tier": tier}
    return {**entry(allowed[0]), "role": role, "reason": f"{reason}; models from {path}",
            "fallbacks": [entry(spec) for spec in allowed[1:]]}
