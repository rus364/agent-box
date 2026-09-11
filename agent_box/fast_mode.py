"""Explicit speed requests; provider access and delivered speed remain external facts."""

# Official speed documentation and Codex 0.154.0 catalog, checked 2026-09-11.
# Reject unknown IDs rather than let fast silently switch or ignore a model.
MODELS = {
    "codex": {"gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
              "gpt-5.6", "gpt-5.5", "gpt-5.4"},
    "claude": {"claude-opus-5", "claude-opus-4-8"},
}


def configuration_error(provider, spec):
    fast = spec.get("fast", False)
    if type(fast) is not bool:
        return "fast must be a boolean"
    if fast and spec.get("model") and spec["model"] not in MODELS.get(provider, ()):
        return "Fast requires a verified supported model ID: " + ", ".join(sorted(MODELS.get(provider, ())))
    return None


def native_limitation(provider):
    if provider == "claude":
        return "Claude native profiles cannot independently pin fast on/off; use managed CLI unless inherited off is verified"
    return None
