"""On-demand Claude OAuth renewal; status-line reads never enter this path."""
from __future__ import annotations

import math
import time
from contextlib import contextmanager
from pathlib import Path

from .atomicio import atomic_write_json, read_json
from .errors import AgentBoxError

CLAUDE_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLAUDE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"


def credentials(path: Path) -> dict:
    if path.is_symlink():
        raise AgentBoxError("Claude credential symlink cannot be refreshed")
    try:
        data = read_json(path)
    except FileNotFoundError:
        raise AgentBoxError("Claude credentials missing; sign in with Claude") from None
    except OSError:
        raise AgentBoxError("Claude credentials unreadable") from None
    except ValueError:
        raise AgentBoxError("Claude credentials malformed") from None
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(oauth, dict) or not isinstance(oauth.get("accessToken"), str) or not oauth["accessToken"]:
        raise AgentBoxError("Claude OAuth access token missing; sign in with Claude")
    return data


def expired(oauth: dict) -> bool:
    expiry = oauth.get("expiresAt")
    return type(expiry) in (int, float) and math.isfinite(expiry) and expiry <= time.time() * 1000


@contextmanager
def credential_lock(path: Path, lifetime: float):
    """Use Claude's mkdir locks; never steal a lock from a running CLI.

    Claude Code 2.1.267 uses .oauth_refresh.lock (60s stale) and
    .storage-write.lock (15s stale). Fail on contention instead of polling;
    check ownership and the lease before persisting or removing a directory.
    """
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        raise AgentBoxError("Claude credentials busy; another process may be updating them") from None
    identity = path.stat()
    started = time.monotonic()

    def owned():
        try:
            current = path.stat()
            return (current.st_dev, current.st_ino, current.st_mtime_ns) == (identity.st_dev, identity.st_ino, identity.st_mtime_ns)
        except OSError:
            return False

    def verify():
        if not owned() or time.monotonic() - started >= lifetime:
            raise AgentBoxError("Claude credential lock expired or changed; retry usage")

    try:
        yield verify
    finally:
        if owned():
            path.rmdir()


def refresh(path: Path, previous: dict, request, *, force: bool = False) -> dict:
    """Re-read under Claude's locks and replace only the unchanged OAuth entry."""
    with credential_lock(path.parent / ".oauth_refresh.lock", 45) as verify_refresh, \
            credential_lock(path.parent / ".storage-write.lock", 10) as verify_storage:
        current = credentials(path)
        oauth = current["claudeAiOauth"]
        if oauth != previous or (not force and not expired(oauth)):
            return oauth
        token = oauth.get("refreshToken")
        if not isinstance(token, str) or not token:
            raise AgentBoxError("Claude refresh token missing; sign in with Claude")
        body = {"grant_type": "refresh_token", "refresh_token": token,
                "client_id": oauth.get("clientId") or CLAUDE_CLIENT_ID}
        scopes = oauth.get("scopes")
        if isinstance(scopes, list) and scopes and all(isinstance(s, str) for s in scopes):
            body["scope"] = " ".join(scopes)
        result = request(CLAUDE_TOKEN_URL, body)
        if not isinstance(result, dict):
            raise AgentBoxError("Claude token refresh returned an invalid response")
        access = result.get("access_token")
        replacement = result.get("refresh_token", token)
        seconds = result.get("expires_in")
        if (not isinstance(access, str) or not access or not isinstance(replacement, str) or not replacement
                or type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds <= 0):
            raise AgentBoxError("Claude token refresh returned an invalid response")
        updated = {**oauth, "accessToken": access, "refreshToken": replacement,
                   "expiresAt": int(time.time() * 1000 + seconds * 1000)}
        if isinstance(result.get("scope"), str):
            updated["scopes"] = result["scope"].split()
        refresh_seconds = result.get("refresh_token_expires_in")
        if type(refresh_seconds) in (int, float) and math.isfinite(refresh_seconds) and refresh_seconds > 0:
            updated["refreshTokenExpiresAt"] = int(time.time() * 1000 + refresh_seconds * 1000)
        verify_refresh()
        verify_storage()
        current = credentials(path)
        if current["claudeAiOauth"] != oauth:
            return current["claudeAiOauth"]
        atomic_write_json(path, {**current, "claudeAiOauth": updated}, 0o600)
        return updated
