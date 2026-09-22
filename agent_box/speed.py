"""Explicit speed requests; provider access and delivered speed remain external facts."""

# Official speed documentation and Codex 0.154.0 catalog, checked 2026-09-11.
# Reject unknown IDs rather than let fast silently switch or ignore a model.
MODELS = {
    "codex": {"gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
              "gpt-5.6", "gpt-5.5", "gpt-5.4"},
    "claude": {"claude-opus-5", "claude-opus-4-8"},
}
SPEEDS = ("inherit", "standard", "fast")


def configuration_error(provider, spec):
    speed = spec.get("speed", "inherit")
    if speed not in SPEEDS:
        return "speed must be inherit, standard, or fast"
    if speed != "inherit" and spec.get("api_provider"):
        return "Explicit speed is not supported for external API candidates; use inherit"
    if speed == "fast" and spec.get("model") and spec["model"] not in MODELS.get(provider, ()):
        return "Fast requires a verified supported model ID: " + ", ".join(sorted(MODELS.get(provider, ())))
    return None


def native_limitation(provider, speed="inherit"):
    if speed != "inherit":
        reason = ("Codex native agents share the root session's service tier" if provider == "codex" else
                  "Claude native profiles inherit speed")
        return reason + "; use managed CLI to request " + str(speed)
    return None
