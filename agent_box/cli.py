"""Command-line dispatch. Kept free of business logic beyond argument parsing."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import NoReturn

from . import channel as channel_module
from . import notify as notify_module
from . import usage as usage_module
from .config import CLAUDE_TELEGRAM_CHANNEL, load_config, save_config
from .errors import AgentBoxError
from .paths import CLAUDE_HOME, CONFIG_FILE


def die(message: str) -> NoReturn:
    print(f"agent-box: {message}", file=sys.stderr)
    raise SystemExit(1)


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
  agent-box config [show|check]
  agent-box usage <codex|claude|all> [--statusline]
  agent-box notify <test|claude|codex>
  agent-box selftest
  agent-box build | update | rebuild

Configuration:
  agent-box config          Interactive setup. Persists to
                            ~/.config/agent-box/config.json
  agent-box config show     Print the current settings (token redacted)
  agent-box config check    Verify the official Claude Telegram channel setup

Environment (overrides for a single command):
  AGENT_BOX_AGENT           Default agent when claude/codex is omitted (default: claude)
  AGENT_BOX_IMAGE           Container image name (default: localhost/agent-box:latest)
  AGENT_BOX_KVM=0           Disable KVM passthrough
  AGENT_BOX_GH_TOKEN=0|1    Withhold / force the host GitHub token for this run
  AGENT_BOX_WORKSPACE_ROOT  Override the host directory mounted into the container

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
  agent-box config
  agent-box usage all''')


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


# -- notify ----------------------------------------------------------------
#
# Setup lives in `agent-box config`; what stays here is the hook entry points
# the container calls (`notify claude` / `notify codex`) plus a manual test.

def notify_command(args: list[str]) -> None:
    if not args:
        die("notify action required; expected test, claude, or codex")
    action, rest = args[0], args[1:]
    if action == "test":
        if rest:
            die("agent-box notify test takes no arguments")
        if not load_config().notifications.usable:
            die("Telegram notifications are not configured; run 'agent-box config'")
        if not notify_module.telegram_send("🧪 <b>agent-box</b>\nTest notification"):
            die("Telegram test failed")
        print("agent-box: test notification sent")
    elif action == "claude":
        if rest:
            die("agent-box notify claude takes no arguments")
        notify_module.notify_claude()
    elif action == "codex":
        if len(rest) > 1:
            die("agent-box notify codex accepts at most one payload argument")
        notify_module.notify_codex(rest[0] if rest else None)
    else:
        die(f"unsupported notify action '{action}'; expected test, claude, or codex")


# -- config ----------------------------------------------------------------

def _redact(token: str) -> str:
    if not token:
        return "(not set)"
    return f"{token[:6]}…{token[-4:]}" if len(token) > 12 else "(set)"


def _prompt_bool(label: str, current: bool) -> bool:
    default = "Y/n" if current else "y/N"
    answer = input(f"{label} [{default}]: ").strip().lower()
    if not answer:
        return current
    return answer in {"y", "yes", "on", "true", "1"}


def _prompt_text(label: str, current: str, secret: bool = False) -> str:
    shown = _redact(current) if secret else (current or "(not set)")
    answer = input(f"{label} [{shown}]: ").strip()
    return answer or current


def config_show() -> None:
    config = load_config()
    print(f"Config file: {CONFIG_FILE}{'' if CONFIG_FILE.is_file() else ' (not created yet; showing defaults)'}")
    print()
    print(f"  Claude Telegram channel     {'on' if config.claude.telegram_channel else 'off'}")
    print(f"  Outbound notifications      {'on' if config.notifications.enabled else 'off'}")
    print(f"    bot token                 {_redact(config.notifications.telegram_token)}")
    print(f"    chat id                   {config.notifications.telegram_chat_id or '(not set)'}")
    print(f"  GitHub token passthrough    {'on' if config.github.gh_token else 'off'}")
    print(f"  GitHub gh config copy       {'on' if config.github.gh_config else 'off'}")
    print(f"  Git config copy             {'on' if config.git.config else 'off'}")
    print(f"  Provider env passthrough    {'on' if config.providers.env else 'off'}")


def config_check() -> None:
    """Report on the official Claude Telegram channel, without changing anything."""
    config = load_config()
    print(f"Claude Telegram channel: {'enabled' if config.claude.telegram_channel else 'disabled'} in agent-box config")
    print(f"Plugin installed:        {'yes' if channel_module.plugin_installed() else 'no'}")
    print(f"Bot token configured:    {'yes' if channel_module.token_configured() else 'no'}")
    missing = channel_module.missing_requirements()
    print()
    if missing:
        print(channel_module.setup_instructions())
        raise SystemExit(1)
    print(f"Ready. 'agent-box claude' will pass --channels {CLAUDE_TELEGRAM_CHANNEL}")


def config_interactive() -> None:
    config = load_config()
    print(f"agent-box configuration ({CONFIG_FILE})")
    print("Press Enter to keep the current value shown in brackets.\n")

    print("-- Claude Telegram channel (Anthropic's official plugin) --")
    print("Controls the live 'agent-box claude' session from Telegram.")
    config.claude.telegram_channel = _prompt_bool("Enable Claude Telegram channel", config.claude.telegram_channel)

    print("\n-- Outbound Telegram notifications --")
    print("One-way pings only (Codex turn complete, Claude waiting/finished/failed).")
    config.notifications.enabled = _prompt_bool("Enable outbound notifications", config.notifications.enabled)
    if config.notifications.enabled:
        config.notifications.telegram_token = _prompt_text(
            "  Telegram bot token", config.notifications.telegram_token, secret=True)
        config.notifications.telegram_chat_id = _prompt_text(
            "  Telegram chat id", config.notifications.telegram_chat_id)
        if not config.notifications.telegram_token or not config.notifications.telegram_chat_id:
            print("  note: notifications stay inactive until both a token and a chat id are set")

    print("\n-- Host integration (each exposes host data to the container) --")
    config.github.gh_token = _prompt_bool(
        "Pass the host GitHub token in as GH_TOKEN", config.github.gh_token)
    config.github.gh_config = _prompt_bool(
        "Copy the host ~/.config/gh (contains that token) in", config.github.gh_config)
    config.git.config = _prompt_bool(
        "Copy the host git config (user.name/user.email) in", config.git.config)
    config.providers.env = _prompt_bool(
        "Forward provider variables from this shell (ANTHROPIC_API_KEY, proxies)", config.providers.env)

    save_config(config)
    settings = CLAUDE_HOME / "settings.json"
    if config.notifications.enabled:
        notify_module.install_claude_notify_hooks(settings)
    else:
        notify_module.remove_claude_notify_hooks(settings)

    print(f"\nSaved {CONFIG_FILE}\n")
    config_show()
    if config.claude.telegram_channel and channel_module.missing_requirements():
        print()
        print(channel_module.setup_instructions())


def config_command(args: list[str]) -> None:
    if not args:
        config_interactive()
    elif args == ["show"]:
        config_show()
    elif args == ["check"]:
        config_check()
    else:
        die("expected: agent-box config [show|check]")


# -- selftest ----------------------------------------------------------

def selftest() -> None:
    from .selftest import run_selftest
    n = run_selftest()
    print(f"agent-box: {n} self-tests passed")


# -- main ----------------------------------------------------------------

NON_RUNTIME = {"usage", "notify", "config", "selftest", "help", "-h", "--help", "build", "update", "rebuild"}


def main() -> None:
    mounts, args = parse_global_args(sys.argv[1:])
    script_dir = Path(__file__).resolve().parent.parent
    try:
        if mounts and args and args[0] in NON_RUNTIME:
            die("--mount is only valid before claude/codex/shell/doctor")
        if args and args[0] == "usage":
            usage_command(args[1:]); return
        if args and args[0] == "notify":
            notify_command(args[1:]); return
        if args and args[0] == "config":
            config_command(args[1:]); return
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
    except (KeyboardInterrupt, EOFError):
        raise SystemExit(130)
