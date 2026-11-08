"""Deterministic model selection: an ordered candidate list per role, and per task tier when enabled."""
from __future__ import annotations

import time

from .config import AGENT_PROVIDERS, TIERED_ROLES, TIERS, vendor
from .errors import AgentBoxError
from .workflow_state import balance_vendor


def applicable_windows(snapshot, model):
    return [window for window in snapshot.get("windows", [])
            if not window.get("models") or any(name in model for name in window["models"])]


def eligible_weights(config, models):
    """Positive vendor weights in first-candidate order, for a caller's eligible list."""
    return {vendor(spec): config.balance.weights[vendor(spec)] for spec in models
            if config.balance.weights.get(vendor(spec), 0) > 0}


def select(config, role, tier, usage=None, *, now=None, available=None, authors=(), balance=balance_vendor):
    """Choose a usable opener, retaining list order for all remaining fallbacks.

    Static vendor weights describe subscriptions, independently of how many
    models each has. Smooth round-robin avoids random streaks and usage-score
    argmax herding on lagging data. Off keeps the first available candidate.
    ``tier`` is effective (scouts have none); CLI and reserve filters run before
    balancing. Pins bypass balancing, and reviewer authors stay last as
    ``models_for`` describes. Only launch selection consumes a turn;
    ``balance`` may supply an in-memory allocator for tests.
    """
    errors = config.validate()
    if errors:
        raise AgentBoxError("; ".join(errors))
    if tier is not None and (role not in TIERED_ROLES or tier not in TIERS):
        raise AgentBoxError(f"A {role} has no tier {tier}; task tiers 1-3 select worker and reviewer lists")
    path, models = config.models_for(role, tier, authors)
    pinned = role in config.pins
    now = time.time() if now is None else now
    usage = usage or {}
    available = set(AGENT_PROVIDERS if available is None else available)
    allowed = [spec for spec in models if spec["provider"] in available]
    if not allowed and models:
        raise AgentBoxError(f"No configured candidate in {path} has its CLI installed here")
    if not pinned:
        # A pin names its model outright; lists preserve subscription headroom.
        usable = [spec for spec in allowed if spec.get("api_provider") or not any(
            w["remaining"] <= config.reserve_percent and w["reset_at"] > now
            for w in applicable_windows(usage.get(spec["provider"], {}), spec["model"]))]
        if allowed and not usable:
            raise AgentBoxError(f"Every candidate in {path} is at or below the {config.reserve_percent}% usage reserve; "
                                "wait for a reset or add a candidate")
        allowed = usable
    if not allowed:
        raise AgentBoxError(f"No {role} candidate configured in {path}")
    reason = f"Pinned for this session by --{role}" if pinned else "First available candidate"
    if not pinned and config.balance.mode == "weighted":
        group = allowed
        if role == "reviewer" and authors and config.review.other_vendor_first:
            # Author weights cannot displace a usable independent reviewer.
            group = [spec for spec in allowed if vendor(spec) not in authors] or allowed
        weights = eligible_weights(config, group)
        if weights:
            # A sole weighted vendor takes every turn, so it opens without spending a counter.
            winner = balance(role, tier, weights) if len(weights) > 1 else next(iter(weights))
            index = next(index for index, spec in enumerate(allowed) if vendor(spec) == winner)
            allowed.insert(0, allowed.pop(index))
            reason = "Balanced by weights " + ", ".join(f"{name}={weight}" for name, weight in weights.items())
            reason += f"; chose {winner}"
    if not pinned and role == "reviewer" and authors and config.review.other_vendor_first:
        reason += f" from vendors other than {', '.join(authors)}, then theirs"

    def entry(spec):
        return {"provider": spec["provider"], "model": spec["model"], "effort": spec["effort"],
                **({"api_provider": spec["api_provider"]} if spec.get("api_provider") else {}),
                "speed": spec.get("speed", "inherit"), "tier": tier}
    return {**entry(allowed[0]), "role": role, "reason": f"{reason}; models from {path}",
            "fallbacks": [entry(spec) for spec in allowed[1:]]}
