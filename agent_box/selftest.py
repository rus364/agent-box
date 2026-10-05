"""`agent-box selftest`: a fast smoke test with no Podman/network dependency.

This exists for convenience (`agent-box selftest` right after install). The
real, comprehensive test suite lives under `tests/` and runs via
`python3 -m unittest`.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from . import notify, usage
from .config import Config


def run_selftest() -> int:
    """Run the checks with every `AGENT_BOX_*` variable removed, then restore them.

    Inside a workspace box the session's own runtime config, notification
    credentials and suppression flag are in the environment; left in place they
    make the checks fail as if regressed, so isolation belongs here rather than
    in a wrapper every caller has to remember.
    """
    saved = {name: os.environ.pop(name) for name in list(os.environ) if name.startswith("AGENT_BOX_")}
    try:
        return _checks()
    finally:
        os.environ.update(saved)


def _checks() -> int:
    n = 0

    def check(actual: Any, expected: Any, label: str) -> None:
        nonlocal n
        n += 1
        if actual != expected:
            # A smoke test must remain safe to run in a credential-bearing shell.
            # Showing values would turn a failed assertion into a secret disclosure.
            raise AssertionError(f"{label}: check failed")

    check(usage.compact_time(3660), "1h1m", "duration")
    check(usage.compact_time(176400), "2d1h", "duration days")
    check(usage.format_codex_usage({"rate_limit": {
        "primary_window": {"used_percent": 25, "limit_window_seconds": 18000, "reset_after_seconds": 3660},
        "secondary_window": {"used_percent": 40, "limit_window_seconds": 604800, "reset_after_seconds": 176400},
    }}), "75/60 · 1h1m/2d1h", "codex windows")
    check(usage.format_codex_usage({"rate_limit": {
        "primary_window": {"used_percent": 100, "limit_window_seconds": 604800, "reset_after_seconds": 385907},
        "secondary_window": None,
    }}), "—/0 · —/4d11h", "codex weekly")
    now = 1_800_000_000
    check(usage.format_claude_usage({"rate_limits": {
        "five_hour": {"used_percentage": 25, "resets_at": now + 3660},
        "seven_day": {"used_percentage": 40, "resets_at": now + 176400},
    }}, now), "75/60 · 1h1m/2d1h", "claude windows")
    check(notify.notify_project("/tmp/project"), "project", "project")
    check(notify.notify_project("/"), "unknown", "root project")
    check(notify.parse_payload("bad"), {}, "bad json")
    check(notify.claude_background_tasks({}), None, "background tasks absent")
    check(notify.claude_background_tasks({"background_tasks": []}), [], "background tasks empty")
    check(
        notify.telegram_notice("💬", "Claude", "a<b", "Waiting & ready"),
        "💬 <b>Claude · a&lt;b</b>\nWaiting &amp; ready",
        "telegram html",
    )
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        notify.update_claude_background_state("session-1", [{"id": "task-1"}], root)
        check(notify.claude_background_active("session-1", root), True, "background active")
        notify.update_claude_background_state("session-1", [], root)
        check(notify.claude_background_active("session-1", root), False, "background cleared")

    check(Config.from_dict(None).agents.claude.telegram_channel, False, "channel off by default")
    check(Config.from_dict(None).credentials.github.token, False, "gh token passthrough off by default")
    check(Config.from_dict(None).agents.host_env.api_keys, False, "provider API keys off by default")
    check(Config.from_dict(None).agents.host_env.transport, True, "provider transport on by default")
    check(Config.from_dict(None).credentials.git.full_config, False, "full git config copy off by default")
    check(Config.from_dict(None).credentials.git.identity, True, "git identity seeding on by default")
    check(Config.from_dict(None).host.notifications.usable, False, "notifications off by default")
    check(Config.from_dict({"host": {"notifications": {"enabled": True, "telegram_token": "t",
                                              "telegram_chat_id": "1"}}}).host.notifications.usable,
          True, "notifications need token and chat id")

    from .workspace import resolve_workspace
    with tempfile.TemporaryDirectory() as tmp:
        sub = Path(tmp) / "sub"
        sub.mkdir()
        ws = resolve_workspace(cwd=sub)
        check(ws.host_root, sub.resolve(), "workspace root outside git defaults to cwd")
        check(ws.container_root, ws.host_root, "same-path model: no /workspace remap")
        check(ws.container_cwd, ws.cwd, "container cwd mirrors host cwd")

    from .identity import detect_host_identity
    from .paths import passwd_home
    identity = detect_host_identity()
    check(identity.uid, os.getuid(), "host identity uid")
    check(identity.home.is_absolute(), True, "host identity home is absolute")
    # $HOME must not be able to move the build identity or any host path.
    real_home = passwd_home()
    saved_home = os.environ.get("HOME")
    os.environ["HOME"] = "/tmp/agent-box-selftest-poisoned-home"
    try:
        check(passwd_home(), real_home, "passwd home ignores $HOME")
        check(detect_host_identity().home, real_home, "build identity home ignores $HOME")
    finally:
        if saved_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved_home

    from .paths import expand_user
    check(expand_user("~"), real_home, "tilde expands to the passwd home")
    check(expand_user("/abs"), Path("/abs"), "absolute path untouched by expand_user")

    from .notify import SUPPRESS_NOTIFY_ENV
    from . import notify as notify_module
    os.environ[SUPPRESS_NOTIFY_ENV] = "1"
    try:
        check(notify_module.outbound_suppressed(), True, "official channel mode detected")
        check(notify_module.claude_interaction_suppressed("Notification", "permission_prompt"), True,
              "official channel suppresses interactive Claude notifications")
        check(notify_module.claude_interaction_suppressed("Stop"), False,
              "official channel keeps Claude completion notifications")
    finally:
        os.environ.pop(SUPPRESS_NOTIFY_ENV, None)
    check(notify_module.outbound_suppressed(), False, "local runs still notify")

    from .mounts import MountSpec, plan_mounts
    check(len(plan_mounts([MountSpec(Path("/a"), False), MountSpec(Path("/a"), False)])), 1, "mount planning collapses duplicates")

    from . import channel
    with tempfile.TemporaryDirectory() as tmp:
        claude_home = Path(tmp)
        check(channel.plugin_installed(claude_home), False, "channel plugin absent")
        check(len(channel.missing_requirements(claude_home)), 2, "channel reports both gaps")
        lock = Path(tmp) / "runtime/telegram-channel.json"
        check(channel.channel_owner(lock), None, "channel free when unlocked")
        check(channel.acquire_channel("/p", lock), True, "channel acquired")
        check(channel.acquire_channel("/p", lock), False, "channel not acquired twice")
        check((channel.channel_owner(lock) or {}).get("project"), "/p", "owner records project")
        # A record naming a process that never existed is stale, not a lock.
        from .atomicio import atomic_write_json
        atomic_write_json(lock, {"pid": 2 ** 30, "pid_start": "1", "boot_id": "x"})
        check(channel.channel_owner(lock), None, "stale lock ignored")
        channel.release_channel(lock)

    with tempfile.TemporaryDirectory() as tmp:
        creds = Path(tmp) / "notify.json"
        config = Config()
        notify.sync_credentials(config, creds)
        check(creds.exists(), False, "disabled notifications write no credential file")
        config.host.notifications.enabled = True
        config.host.notifications.telegram_token = "t"
        config.host.notifications.telegram_chat_id = "1"
        notify.sync_credentials(config, creds)
        check(notify.load_credentials(creds), ("t", "1"), "credential file round trip")
        config.host.notifications.enabled = False
        notify.sync_credentials(config, creds)
        check(creds.exists(), False, "disabling removes the credential file")

    return n
