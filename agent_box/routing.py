"""Deterministic tier selection and bounded subscription-budget scheduling."""
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


def weighted_choice(weights, state, tier):
    key = str(tier)
    saved = state.setdefault(key, {})
    if saved.get("weights") != weights:
        saved.clear()
        saved.update(weights=dict(weights), credits={p: 0 for p in weights})
    credits = saved["credits"]
    for provider, weight in weights.items():
        credits[provider] += weight
    winner = max(weights, key=lambda p: credits[p])
    credits[winner] -= sum(weights.values())
    return winner


def select(config, role, tier, primary, state, usage=None, *, now=None, writer=None, available=None):
    errors = config.validate()
    if errors:
        raise AgentBoxError("; ".join(errors))
    validate_role_tier(role, tier)
    override = config.override_for(role, tier)
    if tier is None and not override:
        raise AgentBoxError("Tier 0 is reserved for scouts; workers and reviewers require tier 1-3")
    if primary not in AGENT_PROVIDERS:
        raise AgentBoxError("Specify the primary provider: codex or claude")
    now = time.time() if now is None else now
    usage = usage or {}
    available = set(AGENT_PROVIDERS if available is None else available)
    matrix = (f"workflow.{'review' if role == 'reviewer' else 'worker'}.override.models" if override else
              f"workflow.review.tiers.{tier}" if role == "reviewer" else f"workflow.tiers.{tier}")
    models = (override.models if override else
              (config.review.tiers if role == "reviewer" else config.tiers)[str(tier)])
    allowed = [p for p in AGENT_PROVIDERS if p in available and models[p]["model"]]
    writer = writer or primary
    if role == "reviewer":
        if writer not in AGENT_PROVIDERS:
            raise AgentBoxError("Reviewer routing requires the actual writer provider")
        if config.review.routing == "same-provider":
            order = [writer] if writer in allowed else []
            reason = "Fresh reviewer from the actual writer provider only"
        else:
            other = "claude" if writer == "codex" else "codex"
            order = [p for p in ([other, writer] if config.fallback else [other]) if p in allowed]
            reason = "Use the other writer provider first; fallback requires unavailability"
    else:
        allowed = [p for p in allowed if config.weights[p] > 0]
        if config.routing == "current":
            allowed = [p for p in allowed if p == primary]
        for provider in list(allowed):
            if models[provider].get("api_provider"):
                continue
            windows = applicable_windows(usage.get(provider, {}), models[provider]["model"])
            if any(w["remaining"] <= 0 and w["reset_at"] > now for w in windows):
                allowed.remove(provider)
        if not allowed:
            raise AgentBoxError(f"No available configured provider in {matrix}; configure models or wait for limits; no automatic session/tier fallback")
        weights = {p: config.weights[p] for p in allowed}
        scores = {}
        if config.routing == "usage-aware":
            for provider in allowed:
                if models[provider].get("api_provider"):
                    continue
                snapshot = usage.get(provider, {})
                weekly = [w for w in applicable_windows(snapshot, models[provider]["model"])
                          if w["seconds"] > 86400 and w["reset_at"] > now]
                if snapshot.get("fresh") and weekly:
                    scores[provider] = min(config.weights[provider] * max(0, w["remaining"] - config.reserve_percent) / 100 /
                                           ((w["reset_at"] - now) / 3600 + config.smoothing_hours) for w in weekly)
        if scores and len(scores) == len(allowed):
            if not any(scores.values()):
                raise AgentBoxError("All weekly budgets are at the configured reserve; change the reserve or wait")
            winner = max(scores, key=scores.get)
            reason = "Weekly budget score: " + ", ".join(f"{p}={v:.6f}" for p, v in scores.items())
        else:
            winner = weighted_choice(weights, state, tier)
            reason = "Current provider only" if config.routing == "current" else "Selected by static weights"
            if config.routing == "usage-aware":
                reason += "; fresh comparable usage unavailable"
        order = [winner] + ([p for p in allowed if p != winner] if config.fallback else [])
    if not order:
        raise AgentBoxError(f"No allowed {role} model configured in {matrix}; no automatic tier downgrade")
    def entry(provider):
        return {"provider": provider, "model": models[provider]["model"], "effort": models[provider]["effort"],
                **({"api_provider": models[provider]["api_provider"]} if models[provider].get("api_provider") else {}),
                "fast": models[provider].get("fast", False), "tier": tier}
    return {**entry(order[0]), "role": role, "reason": reason + (f"; models from {matrix}" if override else ""),
            "fallbacks": [entry(p) for p in order[1:]]}
