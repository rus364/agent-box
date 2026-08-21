"""Command-line dispatch. Kept free of business logic beyond argument parsing."""
from __future__ import annotations

import getpass
import shutil
import sys
from pathlib import Path
from typing import NoReturn

from . import notify as notify_module
from . import usage as usage_module
from .config import RemoteControlConfig, TelegramConfig, load_telegram_config, save_telegram_config
from .errors import AgentBoxError
from .paths import CLAUDE_HOME, TELEGRAM_CONFIG, expand_user


def die(message: str) -> NoReturn:
    print(f"agent-box: {message}", file=sys.stderr)
    raise SystemExit(1)


RUNTIME_COMMANDS = {"claude", "codex", "shell", "doctor"}


def parse_global_args(argv: list[str]) -> tuple[list[str], list[str]]:
    """Split agent-box's own leading options from the rest of argv.

    Grammar: ``agent-box [--mount PATH[:ro]]... [claude|codex|shell|doctor] [provider args...]``.
    Agent-box's own options must come first; once a non-option token is seen
    (or a bare ``--``), everything after it is passed through untouched, so
    ``agent-box claude --model opus`` or a hypothetical Claude/Codex
    ``--mount`` flag of their own is never misinterpreted as ours.
    """
    mounts: list[str] = []
    i = 0
    while i < len(argv):
        token = argv[i]
        if token == "--":
            i += 1
            break
        if token == "--mount":
            if i + 1 >= len(argv):
                die("--mount requires a path")
            mounts.append(argv[i + 1])
            i += 2
            continue
        if token.startswith("--mount="):
            value = token[len("--mount="):]
            if not value:
                die("--mount requires a path")
            mounts.append(value)
            i += 1
            continue
        break
    return mounts, argv[i:]


def usage_text() -> None:
    print('''Usage:
  agent-box [--mount PATH[:ro]]... [codex|claude|shell|doctor] [arguments...]
  agent-box usage <codex|claude> [--statusline]
  agent-box usage all [--statusline]
  agent-box notify <setup|test|status|disable>
  agent-box telegram <serve|status|enable|disable>
  agent-box telegram allow <add|remove|list> [telegram_user_id]
  agent-box telegram roots <add|remove|list> [path]
  agent-box selftest
  agent-box shell [bash arguments...]
  agent-box doctor
  agent-box build
  agent-box update
  agent-box rebuild

Environment:
  AGENT_BOX_AGENT             Default agent when claude/codex is omitted (default: claude)
  AGENT_BOX_IMAGE             Container image name (default: localhost/agent-box:latest)
  AGENT_BOX_KVM=0              Disable KVM passthrough
  AGENT_BOX_GH_TOKEN=0
                               Do not pass the host GitHub token in as GH_TOKEN
  AGENT_BOX_WORKSPACE_ROOT     Override the host directory mounted into the container

--mount PATH[:ro] additionally bind-mounts PATH (an existing absolute host
directory) into the container at that same path, read-write by default or
read-only with the ':ro' suffix. Repeatable. Must appear before the
claude/codex/shell/doctor subcommand; use '--' to stop agent-box's own
option parsing early if a provider argument would otherwise be ambiguous.

Examples:
  agent-box
  agent-box claude
  agent-box codex
  agent-box --mount ~/code claude
  agent-box --mount /mnt/data/specs:ro --mount /srv/generated codex
  agent-box usage all
  agent-box notify setup
  agent-box telegram serve
  agent-box selftest''')


# -- usage -----------------------------------------------------------------

def usage_command(args: list[str]) -> None:
    if not args:
        die("usage provider required; expected codex, claude, or all")
    provider, rest = args[0], args[1:]
    if provider == "claude":
        if rest not in ([], ["--statusline"]):
            die("agent-box usage claude accepts only --statusline")
        print(usage_module.claude_usage(bool(rest)))
    elif provider == "codex":
        if rest:
            die("agent-box usage codex takes no arguments")
        print(usage_module.codex_usage())
    elif provider == "all":
        if rest not in ([], ["--statusline"]):
            die("agent-box usage all accepts only --statusline")
        statusline = rest == ["--statusline"]
        payload = sys.stdin.read() if statusline else None
        try:
            claude = usage_module.claude_usage(statusline, payload)
        except Exception:
            claude = usage_module.PLACEHOLDER_USAGE
        try:
            codex = usage_module.codex_usage()
        except Exception:
            codex = usage_module.PLACEHOLDER_USAGE
        print(f"Claude {claude} │ Codex {codex}")
    else:
        die(f"unsupported usage provider '{provider}'; expected codex, claude, or all")


# -- notify (legacy outbound-only commands, still supported) ---------------

def notify_setup() -> None:
    token = getpass.getpass("Telegram bot token: ")
    chat_id = input("Telegram chat ID: ")
    if not token or not chat_id:
        die("Telegram bot token and chat ID cannot be empty")
    existing = load_telegram_config()
    remote = existing.remote_control if existing else RemoteControlConfig()
    config = TelegramConfig(token=token, notify_chat_id=chat_id, remote_control=remote)
    save_telegram_config(config)
    try:
        notify_module.install_claude_notify_hooks(CLAUDE_HOME / "settings.json")
        if not notify_module.telegram_send("agent-box · Telegram notifications enabled"):
            raise AgentBoxError("Telegram test failed")
    except BaseException:
        TELEGRAM_CONFIG.unlink(missing_ok=True)
        try:
            notify_module.remove_claude_notify_hooks(CLAUDE_HOME / "settings.json")
        except Exception:
            pass
        raise
    print("agent-box: Telegram notifications enabled for Claude and Codex")


def notify_command(args: list[str]) -> None:
    if not args:
        die("notify action required; expected setup, test, status, or disable")
    action, rest = args[0], args[1:]
    if action == "setup":
        if rest: die("agent-box notify setup takes no arguments")
        notify_setup()
    elif action == "test":
        if rest: die("agent-box notify test takes no arguments")
        config = load_telegram_config()
        if config is None or not config.configured: die("Telegram notifications are not configured")
        if not notify_module.telegram_send("🧪 <b>agent-box</b>\nTest notification"): die("Telegram test failed")
        print("agent-box: test notification sent")
    elif action == "status":
        if rest: die("agent-box notify status takes no arguments")
        config = load_telegram_config()
        enabled = config is not None and config.configured
        print("Telegram notifications: enabled" if enabled else "Telegram notifications: disabled")
        if enabled:
            print(f"Config: {TELEGRAM_CONFIG}")
            print(f"Remote control: {'enabled' if config.remote_control.enabled else 'disabled'}")
    elif action == "disable":
        if rest: die("agent-box notify disable takes no arguments")
        TELEGRAM_CONFIG.unlink(missing_ok=True)
        notify_module.remove_claude_notify_hooks(CLAUDE_HOME / "settings.json")
        import shutil as _shutil
        from .paths import NOTIFY_STATE_ROOT, TELEGRAM_STATE_ROOT
        _shutil.rmtree(NOTIFY_STATE_ROOT, ignore_errors=True)
        _shutil.rmtree(TELEGRAM_STATE_ROOT, ignore_errors=True)
        print("agent-box: Telegram notifications disabled")
    elif action == "claude":
        if rest: die("agent-box notify claude takes no arguments")
        notify_module.notify_claude()
    elif action == "codex":
        if len(rest) > 1: die("agent-box notify codex accepts at most one payload argument")
        notify_module.notify_codex(rest[0] if rest else None)
    else:
        die("notify action required; expected setup, test, status, or disable")


# -- telegram remote control -------------------------------------------

def _require_config() -> TelegramConfig:
    config = load_telegram_config()
    if config is None or not config.configured:
        die("Telegram is not configured; run 'agent-box notify setup' first")
    return config


def telegram_command(args: list[str], script_dir: Path) -> None:
    if not args:
        die("telegram action required; expected serve, status, enable, disable, allow, or roots")
    action, rest = args[0], args[1:]
    if action == "serve":
        telegram_serve(rest, script_dir)
    elif action == "status":
        telegram_status()
    elif action == "enable":
        telegram_set_enabled(True)
    elif action == "disable":
        telegram_set_enabled(False)
    elif action == "allow":
        telegram_allow(rest)
    elif action == "roots":
        telegram_roots(rest)
    else:
        die(f"unsupported telegram action '{action}'")


def telegram_serve(args: list[str], script_dir: Path) -> None:
    if args:
        die("agent-box telegram serve takes no arguments")
    config = _require_config()
    if not config.remote_control.enabled:
        die("remote control is disabled; run 'agent-box telegram enable' after configuring 'telegram allow'")
    if not config.remote_control.allowed_user_ids:
        die("no allowed Telegram user IDs configured; run 'agent-box telegram allow add <user_id>'")
    if not (script_dir / "Containerfile").is_file():
        die(f"Containerfile not found: {script_dir / 'Containerfile'}")
    from .telegram.bot import TelegramBot
    bot = TelegramBot(config, script_dir)
    print("agent-box: Telegram remote-control daemon starting (long polling)")
    bot.run()


def telegram_status() -> None:
    config = load_telegram_config()
    if config is None or not config.configured:
        print("Telegram: not configured")
        return
    rc = config.remote_control
    print(f"Notifications: {'configured' if config.notify_chat_id else 'not configured'}")
    print(f"Remote control: {'enabled' if rc.enabled else 'disabled'}")
    print(f"Allowed user IDs: {', '.join(str(u) for u in rc.allowed_user_ids) or '(none)'}")
    print(f"Workspace roots: {', '.join(rc.workspace_roots) or '(none)'}")


def telegram_set_enabled(enabled: bool) -> None:
    config = _require_config()
    if enabled and not config.remote_control.allowed_user_ids:
        die("cannot enable remote control with an empty allowlist; run 'agent-box telegram allow add <user_id>' first")
    config.remote_control.enabled = enabled
    save_telegram_config(config)
    print(f"agent-box: Telegram remote control {'enabled' if enabled else 'disabled'}")


def telegram_allow(args: list[str]) -> None:
    if not args:
        die("expected: agent-box telegram allow <add|remove|list> [telegram_user_id]")
    action, rest = args[0], args[1:]
    config = _require_config()
    if action == "list":
        for uid in config.remote_control.allowed_user_ids:
            print(uid)
        return
    if not rest:
        die(f"agent-box telegram allow {action} requires a Telegram user ID")
    try:
        user_id = int(rest[0])
    except ValueError:
        die("Telegram user ID must be an integer")
    ids = config.remote_control.allowed_user_ids
    if action == "add":
        if user_id not in ids:
            ids.append(user_id)
    elif action == "remove":
        config.remote_control.allowed_user_ids = [u for u in ids if u != user_id]
    else:
        die(f"unsupported telegram allow action '{action}'")
    save_telegram_config(config)
    print(f"agent-box: allowlist updated ({len(config.remote_control.allowed_user_ids)} user(s))")


def telegram_roots(args: list[str]) -> None:
    if not args:
        die("expected: agent-box telegram roots <add|remove|list> [path]")
    action, rest = args[0], args[1:]
    config = _require_config()
    if action == "list":
        for root in config.remote_control.workspace_roots:
            print(root)
        return
    if not rest:
        die(f"agent-box telegram roots {action} requires a path")
    raw = expand_user(rest[0])
    # Stored as a canonical absolute path so the daemon's cwd at 'telegram
    # serve' time can never change what this security-relevant setting means.
    path = str(raw.resolve())
    roots = config.remote_control.workspace_roots
    if action == "add":
        if not raw.is_dir():
            die(f"not a directory: {rest[0]}")
        if path not in roots:
            roots.append(path)
    elif action == "remove":
        config.remote_control.workspace_roots = [r for r in roots if r != path]
    else:
        die(f"unsupported telegram roots action '{action}'")
    save_telegram_config(config)
    print(f"agent-box: workspace roots updated ({len(config.remote_control.workspace_roots)} root(s))")


# -- selftest ----------------------------------------------------------

def selftest() -> None:
    from .selftest import run_selftest
    n = run_selftest()
    print(f"agent-box: {n} self-tests passed")


# -- main ----------------------------------------------------------------

def main() -> None:
    mounts, args = parse_global_args(sys.argv[1:])
    script_dir = Path(__file__).resolve().parent.parent
    non_runtime_subcommands = {"usage", "notify", "telegram", "selftest", "help", "-h", "--help", "build", "update", "rebuild"}
    try:
        if mounts and args and args[0] in non_runtime_subcommands:
            die("--mount is only valid before claude/codex/shell/doctor")
        if args and args[0] == "usage":
            usage_command(args[1:]); return
        if args and args[0] == "notify":
            notify_command(args[1:]); return
        if args and args[0] == "telegram":
            telegram_command(args[1:], script_dir); return
        if args and args[0] == "selftest":
            if len(args) != 1: die("agent-box selftest takes no arguments")
            selftest(); return
        if args and args[0] in {"help", "-h", "--help"}:
            usage_text(); return
        if args and args[0] in {"build", "update", "rebuild"}:
            if len(args) != 1: die(f"agent-box {args[0]} takes no arguments")
            if not shutil.which("podman"): die("podman not found")
            from . import runtime
            command = args[0]
            raise SystemExit(runtime.build_image(script_dir, command != "build", command == "rebuild"))
        from . import runtime
        runtime.runtime_main(args, script_dir, mounts)
    except AgentBoxError as exc:
        die(str(exc))
    except KeyboardInterrupt:
        raise SystemExit(130)
