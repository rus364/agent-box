"""Claude/Codex subscription usage reporting (host-side, no container)."""
from __future__ import annotations

import hashlib
import json
import http.client
import math
import os
import signal
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from .atomicio import atomic_write_json, read_json
from .errors import AgentBoxError
from .paths import CLAUDE_HOME, CODEX_HOME, XDG_CACHE_HOME, env_path
from . import usage_auth

PLACEHOLDER_USAGE = "—/— · —/—"
#: Where Claude usage is cached, whether it arrived over OAuth or in a
#: status-line payload. Both writers and the reader share this one path.
CLAUDE_USAGE_CACHE = XDG_CACHE_HOME / "claude-code/rate-limits.json"
#: Claude Code joins status-line segments nowhere; agent-box owns its own line,
#: so one separator is used for every segment it prints.
STATUSLINE_SEPARATOR = " │ "
#: One muted 256-colour hue per segment, so the three numbers are told apart at
#: a glance without competing with the transcript above them. Claude Code
#: already renders a single-line status dim, which these are chosen to survive.
#: The segments carry no provider names -- hue is what identifies them, which
#: is why colour is applied by the multi-segment lines only (the status line
#: and `usage all`); a single-provider `usage claude` / `usage codex` names
#: its provider in the command itself and prints plain.
CONTEXT_COLOR = "38;5;140"   # muted violet
CLAUDE_COLOR = "38;5;173"    # muted terracotta
CODEX_COLOR = "38;5;110"     # muted steel blue
#: Orchid rather than a second violet: next to ``CONTEXT_COLOR`` on the status
#: line a closer hue would stop telling the two segments apart.
CUSTOM_COLOR = "38;5;133"    # muted orchid
CUSTOM_PLACEHOLDER = "—"
#: The user's command runs on every status-line redraw that misses the cache,
#: so a slow network call is cut off rather than allowed to stall the line.
CUSTOM_TIMEOUT_SECONDS = 5
CUSTOM_CACHE_TTL = 60
CUSTOM_MAX_WIDTH = 40
CUSTOM_USAGE_CACHE = XDG_CACHE_HOME / "agent-box/usage-command.json"
#: End bold and restore the default foreground, and nothing else. A full reset
#: (``0``) would also clear the dim attribute Claude Code wraps the status line
#: in, making everything after it brighter than the rest of the line.
COLOR_OFF = "\x1b[22;39m"


def color_enabled() -> bool:
    """Whether to emit colour. Honours NO_COLOR, never a terminal check.

    A status line is always a piped command, so an ``isatty`` test would
    suppress colour in exactly the case it is meant for.
    """
    return not os.environ.get("NO_COLOR", "")


def colorize(text: str, color: str) -> str:
    if not text or not color_enabled():
        return text
    return f"\x1b[{color}m{text}{COLOR_OFF}"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Bearer tokens and refresh bodies must stay at the selected endpoint.
        return None


def http_json(url: str, *, headers: dict[str, str] | None = None, data: bytes | None = None, timeout: float = 15) -> Any:
    request = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.build_opener(NoRedirect).open(request, timeout=timeout) as response:
        return json.load(response)


def refresh_request(url, body):
    try:
        return http_json(url, headers={"Content-Type": "application/json", "User-Agent": "claude-cli (external, cli)"},
                         data=json.dumps(body).encode(), timeout=5)
    except urllib.error.HTTPError as exc:
        raise AgentBoxError(f"Claude token refresh failed: HTTP {exc.code}; sign in with Claude if authorization was rejected") from None
    except (OSError, http.client.HTTPException):
        raise AgentBoxError("Claude token refresh failed: network error") from None
    except ValueError:
        raise AgentBoxError("Claude token refresh returned an invalid response") from None


def cache_fresh(path: Path, ttl: int) -> bool:
    try:
        st = path.stat()
        return st.st_size > 0 and time.time() - st.st_mtime < ttl
    except OSError:
        return False


def compact_time(seconds: float) -> str:
    s = max(0, int(seconds))
    if s >= 86400:
        return f"{s // 86400}d{(s % 86400) // 3600}h"
    if s >= 3600:
        return f"{s // 3600}h{(s % 3600) // 60}m"
    return f"{s // 60}m"


def format_codex_usage(data: Any) -> str:
    rate = data.get("rate_limit", {}) if isinstance(data, dict) else {}
    windows = [w for w in (rate.get("primary_window"), rate.get("secondary_window")) if isinstance(w, dict)]
    short = next((w for w in windows if w.get("limit_window_seconds") is not None and float(w["limit_window_seconds"]) <= 86400), None)
    week = next((w for w in windows if w.get("limit_window_seconds") is not None and float(w["limit_window_seconds"]) > 86400), None)
    if short is None and week is None:
        raise AgentBoxError("response carried no recognizable rate-limit windows")

    def remaining(w: Any) -> str:
        return "—" if not isinstance(w, dict) or w.get("used_percent") is None else str(int(100 - float(w["used_percent"])))

    def reset(w: Any) -> str:
        return "—" if not isinstance(w, dict) or w.get("reset_after_seconds") is None else compact_time(float(w["reset_after_seconds"]))

    return f"{remaining(short)}/{remaining(week)} · {reset(short)}/{reset(week)}"


def parse_epoch(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def normalize_claude_usage(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {"rate_limits": {}}
    if isinstance(data.get("rate_limits"), dict):
        return {"rate_limits": data["rate_limits"]}

    def window(w: Any) -> dict[str, Any] | None:
        if not isinstance(w, dict):
            return None
        return {"used_percentage": w.get("used_percentage", w.get("utilization")), "resets_at": parse_epoch(w.get("resets_at"))}

    return {"rate_limits": {name: window(value) for name, value in data.items()
                            if name == "five_hour" or name.startswith("seven_day")}}


def format_claude_usage(data: Any, now: float | None = None) -> str:
    now = time.time() if now is None else now
    rate = normalize_claude_usage(data).get("rate_limits", {})

    def fmt(w: Any) -> tuple[str, str]:
        if not isinstance(w, dict):
            return "—", "—"
        used = w.get("used_percentage", w.get("utilization"))
        reset_at = parse_epoch(w.get("resets_at"))
        if used is None or reset_at is None or reset_at <= now:
            return "—", "—"
        return str(int(100 - float(used))), compact_time(reset_at - now)

    a, ar = fmt(rate.get("five_hour"))
    b, br = fmt(rate.get("seven_day"))
    return f"{a}/{b} · {ar}/{br}"


def compact_tokens(count: float) -> str:
    n = max(0, int(count))
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{round(n / 1_000)}k"
    return str(n)


def finite(value: Any) -> bool:
    """A usable payload number. ``json.loads`` accepts ``Infinity`` and ``1e400``,
    which ``int()`` and ``time.localtime`` then refuse by raising."""
    return isinstance(value, (int, float)) and math.isfinite(value)


def format_line_counts(added: int, removed: int) -> str:
    return f"+{added}/−{removed}"


def format_context_window(data: Any, now: float | None = None) -> str:
    """Session context, prompt cache and edits, from a Claude status-line payload.

    Claude Code computes all of it itself and ships it in the status-line JSON
    (``context_window``, ``prompt_cache``, ``cost``), so agent-box neither
    reads the transcript nor asks the API for it -- and, because a status line
    is a plain shell command whose output only reaches the terminal, none of it
    costs session context. Cumulative session in/out tokens are left out on
    purpose: only the transcript has them, and cache reads dominate the sum.

    Each part is dropped on its own when its fields are missing or unusable,
    so the segment is empty before the first assistant message of a session.
    """
    if not isinstance(data, dict):
        return ""
    return " · ".join(part for part in (
        context_usage(data.get("context_window")),
        prompt_cache_state(data.get("prompt_cache"), time.time() if now is None else now),
        session_line_counts(data.get("cost")),
    ) if part)


def prompt_cache_state(cache: Any, now: float) -> str:
    """When the cached prefix goes cold, or what rebuilding it will cost.

    The expiry is a clock time, not a countdown: Claude Code redraws the status
    line only on session events, so a countdown would freeze while the session
    idles -- exactly when whether the cache is still warm matters.
    """
    if not isinstance(cache, dict) or cache.get("caching_observed") is not True:
        return ""
    warm = cache.get("warm")
    expires = cache.get("expires_at")
    if warm is True:
        if not finite(expires):
            return ""
        if expires > now:
            try:
                return f"cache →{time.strftime('%H:%M', time.localtime(expires))}"
            except (OverflowError, OSError, ValueError):
                return ""
    elif warm is not False:
        return ""
    rebuild = cache.get("recache_tokens_if_cold")
    if finite(rebuild) and rebuild > 0:
        return f"cache cold · {compact_tokens(rebuild)}"
    return "cache cold"


def session_line_counts(cost: Any) -> str:
    """Lines Claude Code changed this session; it does not reset on commit."""
    if not isinstance(cost, dict):
        return ""
    counts = [
        value if isinstance(value, int) and value > 0 else 0
        for value in (cost.get("total_lines_added"), cost.get("total_lines_removed"))
    ]
    return format_line_counts(*counts) if any(counts) else ""


def context_usage(window: Any) -> str:
    if not isinstance(window, dict):
        return ""
    used = window.get("used_percentage")
    total = window.get("context_window_size")
    tokens = window.get("total_input_tokens")
    if not (finite(used) and finite(total) and finite(tokens)) or total <= 0:
        return ""
    return f"ctx {int(used)}% · {compact_tokens(tokens)}/{compact_tokens(total)}"


def join_segments(*segments: str) -> str:
    """Join the non-empty status-line segments agent-box has to show."""
    return STATUSLINE_SEPARATOR.join(segment for segment in segments if segment)


def configured_usage_blocks():
    """Read the same display switches on the host and in a rendered session."""
    from .config import StatuslineConfig, effective_config
    try:
        return effective_config(include_project=False).agents.claude.statusline
    except AgentBoxError:
        # A broken snapshot must not turn opted-out network requests back on.
        return StatuslineConfig(claude_usage=False, codex_usage=False, custom_usage=False)


def usage_segments(payload=None):
    """Collect only enabled aggregate blocks, including payload/cache writes."""
    config = configured_usage_blocks()
    segments = []
    for enabled, provider, color in (
        (config.claude_usage, "claude", CLAUDE_COLOR),
        (config.codex_usage, "codex", CODEX_COLOR),
    ):
        if not enabled:
            continue
        try:
            if provider == "claude":
                text = (claude_usage_from_payload(payload) or claude_usage(allow_refresh=False)
                        if payload is not None else claude_usage())
            else:
                text = codex_usage()
        except Exception:
            text = PLACEHOLDER_USAGE
        segments.append(colorize(text, color))
    if config.custom_usage:
        segments.append(colorize(custom_usage(), CUSTOM_COLOR))
    return segments


def configured_custom_command() -> str:
    """``agents.claude.statusline.command`` from the saved file, or from the session snapshot in a box."""
    from .config import effective_config
    try:
        return effective_config(include_project=False).agents.claude.statusline.command
    except AgentBoxError:
        return ""


def custom_usage() -> str:
    """One line printed by the user's ``agents.claude.statusline.command``; "" when none is set.

    Failures are cached like successes: a command that times out would
    otherwise cost every redraw its full timeout. The cache is keyed by a hash
    so a changed command never shows the old one's output, and so the command
    text -- which may carry a key despite the advice -- is not copied to disk.
    """
    command = configured_custom_command()
    if not command:
        return ""
    key = hashlib.sha256(command.encode()).hexdigest()
    try:
        cached = read_json(CUSTOM_USAGE_CACHE)
    except (OSError, ValueError):
        cached = None
    if (isinstance(cached, dict) and cached.get("command") == key and isinstance(cached.get("text"), str)
            and cache_fresh(CUSTOM_USAGE_CACHE, CUSTOM_CACHE_TTL)):
        return cached["text"]
    text = run_custom_command(command)
    try:
        atomic_write_json(CUSTOM_USAGE_CACHE, {"command": key, "text": text}, 0o600)
    except OSError:
        pass
    return text


def run_custom_command(command: str) -> str:
    """The first non-empty output line, printable and short, or a placeholder.

    The command runs in its own process group: ``sh`` exits on a kill while a
    ``curl`` it started keeps the output pipe open, so killing only the shell
    would leave the timeout waiting on the pipe.
    """
    try:
        process = subprocess.Popen(["sh", "-c", command], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return CUSTOM_PLACEHOLDER
    with process:
        try:
            output, _ = process.communicate(timeout=CUSTOM_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
            process.communicate()
            return CUSTOM_PLACEHOLDER
    if process.returncode:
        return CUSTOM_PLACEHOLDER
    line = next((line for line in output.decode(errors="replace").splitlines() if line.strip()), "")
    # Escape sequences in the output would restyle or move the terminal line.
    text = "".join(character for character in line if character.isprintable()).strip()
    if len(text) > CUSTOM_MAX_WIDTH:
        text = text[:CUSTOM_MAX_WIDTH - 1] + "…"
    return text or CUSTOM_PLACEHOLDER


def parse_payload(payload: str) -> dict[str, Any]:
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError:
        return {}
    return obj if isinstance(obj, dict) else {}


def codex_usage() -> str:
    return format_snapshot(usage_snapshot("codex"))


def claude_usage_from_payload(obj: dict[str, Any]) -> str:
    """Claude's usage as the running session reported it, or "" if it did not.

    A status-line payload carries the session's own ``rate_limits``, which are
    fresher and cheaper than an HTTP call, so the numbers are taken from there
    and written to the same cache ``claude_usage`` reads. An empty result means
    the payload said nothing about rate limits and the caller should fall back.
    """
    if not isinstance(obj.get("rate_limits"), dict):
        return ""
    normalized = normalize_claude_usage(obj)
    atomic_write_json(CLAUDE_USAGE_CACHE, normalized)
    return format_claude_usage(normalized)


def claude_usage(*, allow_refresh=True) -> str:
    return format_snapshot(usage_snapshot("claude", allow_refresh=allow_refresh))


def usage_windows(provider, data, observed_at):
    """Normalize budget windows without discarding model-specific restrictions."""
    import math
    windows = []
    if not isinstance(data, dict):
        return windows
    if provider == "codex":
        rates = data.get("rate_limit") or {}
        if not isinstance(rates, dict):
            return windows
        candidates = [(w, []) for w in (rates.get("primary_window"), rates.get("secondary_window"))]
        for raw, models in candidates:
            if not isinstance(raw, dict):
                continue
            used = raw.get("used_percent")
            duration = raw.get("limit_window_seconds")
            reset = raw.get("reset_at")
            if reset is None and isinstance(raw.get("reset_after_seconds"), (int, float)):
                reset = observed_at + raw["reset_after_seconds"]
            if all(isinstance(n, (int, float)) and math.isfinite(n) for n in (used, duration, reset)):
                windows.append({"seconds": duration, "remaining": max(0, min(100, 100 - used)),
                                "reset_at": reset, "models": models})
    else:
        rates = data.get("rate_limits", data)
        if not isinstance(rates, dict):
            return windows
        for name, raw in rates.items():
            if not isinstance(raw, dict) or not (name == "five_hour" or name.startswith("seven_day")):
                continue
            used = raw.get("used_percentage", raw.get("utilization"))
            reset = parse_epoch(raw.get("resets_at"))
            if not all(isinstance(n, (int, float)) and math.isfinite(n) for n in (used, reset)):
                continue
            models = [name[len("seven_day_"):]] if name.startswith("seven_day_") else []
            windows.append({"seconds": 18000 if name == "five_hour" else 604800,
                            "remaining": max(0, min(100, 100 - used)), "reset_at": reset, "models": models})
    return windows


def usage_snapshot(provider, no_cache=False, *, allow_refresh=True):
    """A failed refresh may retain evidence of exhaustion, never claim freshness."""
    if provider not in ("codex", "claude"):
        raise AgentBoxError("Unknown usage provider")
    cache = XDG_CACHE_HOME / "codex/usage.json" if provider == "codex" else CLAUDE_USAGE_CACHE
    now = time.time()
    try:
        cached = read_json(cache)
        observed = cache.stat().st_mtime
    except (OSError, ValueError):
        cached, observed = {}, 0
    ttl = int(os.environ.get(f"{provider.upper()}_USAGE_CACHE_TTL", "60"))
    if not no_cache and cached and now - observed < ttl:
        return {"provider": provider, "fresh": True, "source": "cache", "observed_at": observed,
                "windows": usage_windows(provider, cached, observed)}
    try:
        if provider == "codex":
            auth = env_path("CODEX_AUTH_PATH", CODEX_HOME / "auth.json")
            try:
                credentials = read_json(auth)
            except FileNotFoundError:
                raise AgentBoxError("Codex credentials missing; sign in with Codex") from None
            except OSError:
                raise AgentBoxError("Codex credentials unreadable") from None
            except ValueError:
                raise AgentBoxError("Codex credentials malformed") from None
            tokens = credentials.get("tokens") if isinstance(credentials, dict) else None
            if not isinstance(tokens, dict) or not isinstance(tokens.get("access_token"), str) or not tokens["access_token"]:
                raise AgentBoxError("Codex OAuth access token missing; sign in with Codex")
            headers = {"Authorization": "Bearer " + tokens["access_token"]}
            if tokens.get("account_id"):
                headers["chatgpt-account-id"] = str(tokens["account_id"])
            url = os.environ.get("CODEX_USAGE_URL", "https://chatgpt.com/backend-api/wham/usage")
        else:
            auth = env_path("CLAUDE_CREDENTIALS_PATH", CLAUDE_HOME / ".credentials.json")
            oauth = usage_auth.credentials(auth)["claudeAiOauth"]
            renewed = False
            if allow_refresh and usage_auth.expired(oauth):
                oauth = usage_auth.refresh(auth, oauth, refresh_request)
                renewed = True
            token = oauth["accessToken"]
            headers = {"Authorization": "Bearer " + token, "anthropic-beta": "oauth-2025-04-20",
                       "Content-Type": "application/json", "User-Agent": "claude-cli (external, cli)"}
            url = os.environ.get("CLAUDE_CODE_CUSTOM_OAUTH_URL", "https://api.anthropic.com").rstrip("/") + "/api/oauth/usage"
        try:
            data = http_json(url, headers=headers)
        except urllib.error.HTTPError as exc:
            if provider != "claude" or exc.code != 401 or not allow_refresh or renewed:
                raise
            oauth = usage_auth.refresh(auth, oauth, refresh_request, force=True)
            headers["Authorization"] = "Bearer " + oauth["accessToken"]
            data = http_json(url, headers=headers)
        now = time.time()
        windows = usage_windows(provider, data, now)
        if not windows:
            raise AgentBoxError("No recognizable usage windows")
        try:
            atomic_write_json(cache, data, 0o600)
        except OSError:
            pass  # A read-only cache does not invalidate a successful live observation.
        return {"provider": provider, "fresh": True, "source": "live", "observed_at": now, "windows": windows}
    except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError, AgentBoxError) as exc:
        if isinstance(exc, AgentBoxError):
            error = str(exc)
        elif isinstance(exc, urllib.error.HTTPError):
            error = f"Usage request failed: HTTP {exc.code}"
        elif isinstance(exc, FileNotFoundError):
            error = "Credentials missing; sign in with the provider"
        elif isinstance(exc, PermissionError):
            error = "Credentials unreadable"
        elif isinstance(exc, (OSError, http.client.HTTPException)):
            error = "Usage request failed: network error"
        else:
            error = "Invalid usage response"
        return {"provider": provider, "fresh": False, "source": "cache" if cached else "unavailable",
                "observed_at": observed, "windows": usage_windows(provider, cached, observed),
                "error": f"{provider}: {error}"}


def format_snapshot(snapshot):
    now = time.time()
    windows = [w for w in snapshot["windows"] if not w["models"] and w["reset_at"] > now]
    def segment(weekly):
        window = next((w for w in windows if (w["seconds"] > 86400) == weekly), None)
        return (str(int(window["remaining"])), compact_time(window["reset_at"] - now)) if window else ("—", "—")
    a, ar = segment(False)
    b, br = segment(True)
    return f"{a}/{b} · {ar}/{br}"
