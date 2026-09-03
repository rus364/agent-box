"""Command-line dispatch. Kept free of business logic beyond argument parsing."""
from __future__ import annotations

import getpass
import shutil
import sys
from pathlib import Path
from typing import NoReturn

from . import channel as channel_module
from . import claude_settings
from . import notify as notify_module
from . import statusline as statusline_module
from . import usage as usage_module
from .atomicio import read_json
from .config import CLAUDE_TELEGRAM_CHANNEL, load_config, save_config
from .errors import AgentBoxError
from .paths import CLAUDE_HOME, CONFIG_FILE


def die(message: str) -> NoReturn:
    print(f"agent-box: {message}", file=sys.stderr)
    raise SystemExit(1)


def parse_global_args(argv: list[str]) -> tuple[list[str], bool, list[str]]:
    """Split agent-box's own leading options from the rest of argv.

    Grammar: ``agent-box [--mount PATH[:ro]]... [--host] [claude|codex|shell|doctor] [provider args...]``.
    Agent-box's own options must come first; once a non-option token is seen
    (or a bare ``--``), everything after it is passed through untouched, so
    ``agent-box claude --model opus`` or a hypothetical Claude/Codex
    ``--mount``/``--host`` flag of their own is never misinterpreted as ours.

    ``--host`` runs the resolved subcommand directly on the host instead of
    in a container; ``agent-box claude``/``agent-box codex`` keep meaning
    containerized execution.
    """
    mounts: list[str] = []
    host = False
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
        if token == "--host":
            host = True
            i += 1
            continue
        break
    return mounts, host, argv[i:]


def usage_text() -> None:
    print('''Usage:
  agent-box [--mount PATH[:ro]]... [--host] [codex|claude|shell|doctor] [arguments...]
  agent-box attach [session]
  agent-box stop
  agent-box config [show|check]
  agent-box usage <codex|claude|all>
  agent-box statusline
  agent-box notify <test|claude|codex>
  agent-box selftest
  agent-box build | update | rebuild

Sessions:
  An interactive claude/codex/shell session runs inside a tmux server in the
  workspace box, so closing the terminal detaches it instead of orphaning it:
  the agent keeps running and 'agent-box attach' takes it back. Detach on
  purpose with 'C-\\ d'. A detached session keeps its box alive, so a box you
  walked away from ends with 'agent-box stop'. Non-terminal runs
  ('claude -p ...', pipes, CI) are never wrapped.

  agent-box attach          Reattach to this workspace's session (names one
                            when the box holds several)
  agent-box stop            Remove this workspace's box and every session in it

Configuration:
  agent-box config          Interactive setup. Persists to
                            ~/.config/agent-box/config.json
  agent-box config show     Print the current settings (token redacted)
  agent-box config check    Verify the official Claude Telegram channel setup

Environment (overrides for a single command):
  AGENT_BOX_AGENT           Default agent when claude/codex is omitted (default: claude)
  AGENT_BOX_IMAGE           Container image name (default: localhost/agent-box:latest)
  AGENT_BOX_KVM=0           Disable KVM passthrough
  AGENT_BOX_TMUX=0          Run the session directly, with no detachable tmux
  AGENT_BOX_GH_TOKEN=0|1    Withhold / force the host GitHub token for this run
  AGENT_BOX_WORKSPACE_ROOT  Override the host directory mounted into the container

--mount PATH[:ro] additionally bind-mounts PATH (an existing absolute host
directory) into the container at that same path, read-write by default or
read-only with the ':ro' suffix. Repeatable. Must appear before the
claude/codex/shell/doctor subcommand; use '--' to stop agent-box's own
option parsing early if a provider argument would otherwise be ambiguous.

--host runs claude/codex directly on the host instead of in a container: no
Podman, the installed host executable, your normal host git/gh credentials.
'agent-box claude'/'agent-box codex' (without --host) keep meaning
containerized execution. Not valid with --mount, and not valid with
shell/doctor, which are container-only.

Flow-Next workspaces (a '.flow/config.json' present) get a deterministic
FLOW_REVIEW_BACKEND from agent-box config in both modes; see
'flow_next' in 'agent-box config show'. Set FLOW_REVIEW_BACKEND in the
shell before invoking agent-box to override it for that one launch, e.g.
'FLOW_REVIEW_BACKEND=codex:gpt-5.6-terra:high agent-box claude'.

Examples:
  agent-box
  agent-box claude
  agent-box codex
  agent-box --host claude
  agent-box --host codex
  agent-box --mount ~/code claude
  agent-box --mount /mnt/data/specs:ro --mount /srv/generated codex
  agent-box attach
  agent-box stop
  agent-box config
  agent-box usage all''')


# -- usage -----------------------------------------------------------------

def statusline_command(args: list[str]) -> None:
    if args:
        die("agent-box statusline takes no arguments")
    print(statusline_module.render(sys.stdin.read()))


def usage_command(args: list[str]) -> None:
    """How much of each subscription is left. Session state lives in
    `agent-box statusline`, which is the only caller Claude Code's payload
    reaches; this command answers from the cache or OAuth and takes no input.
    """
    if not args:
        die("usage provider required; expected codex, claude, or all")
    provider, rest = args[0], args[1:]
    if provider not in ("claude", "codex", "all"):
        die(f"unsupported usage provider '{provider}'; expected codex, claude, or all")
    if rest:
        die(f"agent-box usage {provider} takes no arguments")
    if provider == "claude":
        print(usage_module.claude_usage())
    elif provider == "codex":
        print(usage_module.codex_usage())
    else:
        try:
            claude = usage_module.claude_usage()
        except Exception:
            claude = usage_module.PLACEHOLDER_USAGE
        try:
            codex = usage_module.codex_usage()
        except Exception:
            codex = usage_module.PLACEHOLDER_USAGE
        # Only the combined line colours its segments: the hues are what
        # tell Claude from Codex once the provider names are gone, which is a
        # distinction a single-provider invocation does not have to make.
        print(usage_module.join_segments(
            usage_module.colorize(claude, usage_module.CLAUDE_COLOR),
            usage_module.colorize(codex, usage_module.CODEX_COLOR),
        ))


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
        notify_module.sync_credentials(load_config())
        if notify_module.load_credentials() is None:
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
        # Codex supports exactly one `notify` program, so this single entry
        # point carries both outbound notifications and the Flow-Next
        # auto-push turn-complete adapter. The payload is read once here and
        # handed to each independently, so neither one can starve the other
        # of stdin.
        payload_text = rest[0] if rest else sys.stdin.read()
        notify_module.notify_codex(payload_text)
        from . import flow_push
        flow_push.run_codex_turn_complete_hook(payload_text)
    else:
        die(f"unsupported notify action '{action}'; expected test, claude, or codex")


# -- flow-next ---------------------------------------------------------------
#
# Internal hook entry point for the managed Claude Stop hook that drives
# Flow-Next auto-push (see agent_box.flow_push). Not meant to be typed by a
# user; agent-box config installs this as the literal hook command in
# ~/.claude/settings.json. Codex's turn-complete adapter is reached through
# `notify_command` above instead, since Codex has only one notify program.

def flow_next_command(args: list[str]) -> None:
    from . import flow_push

    if args == ["stop"]:
        flow_push.run_stop_hook(sys.stdin.read())
    else:
        die("unsupported flow-next action; expected stop")


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


def _prompt_text(label: str, current: str) -> str:
    answer = input(f"{label} [{current or '(not set)'}]: ").strip()
    return answer or current


def _prompt_secret(label: str, current: str) -> str:
    """Read a secret without echoing it.

    ``getpass`` also keeps the token out of the terminal scrollback and any
    screen-sharing or recording of the session, which plain ``input`` would
    not. An empty answer keeps whatever is already stored.
    """
    answer = getpass.getpass(f"{label} [{_redact(current)}]: ").strip()
    return answer or current


def _statusline_note(config) -> str:
    """Say so when Claude's status line is somebody else's, not agent-box's.

    Reconciliation never overwrites a status line it does not own, so "on"
    alone could otherwise look like a promise agent-box is not keeping.
    """
    if not config.claude.statusline:
        return ""
    settings_path = CLAUDE_HOME / "settings.json"
    try:
        settings = read_json(settings_path) if settings_path.is_file() else {}
    except (OSError, ValueError):
        return ""
    if not isinstance(settings, dict) or "statusLine" not in settings:
        return " (installs on the next 'agent-box claude')"
    if claude_settings.statusline_is_ours(settings):
        return ""
    return " (left alone: another status line is configured)"


def _vikunja_note(config) -> str:
    """Say what the container will actually reach, or why it will not.

    "on" alone is not informative here: the switch exposes host state that
    may not exist yet, and the URL the container gets is a rewritten form of
    the host's, which is worth showing rather than leaving to be discovered
    from a failing call.
    """
    if not config.vikunja.enabled:
        return ""
    from . import runtime
    problems = runtime.vikunja_warnings(config)
    if problems:
        return f" ({problems[0].split('but ', 1)[-1]})"
    return f" ({runtime.container_vikunja_url(runtime.host_vikunja_url())})"


def config_show() -> None:
    config = load_config()
    print(f"Config file: {CONFIG_FILE}{'' if CONFIG_FILE.is_file() else ' (not created yet; showing defaults)'}")
    print()
    print(f"  Claude Telegram channel     {'on' if config.claude.telegram_channel else 'off'}")
    print(f"  Claude status line          {'on' if config.claude.statusline else 'off'}{_statusline_note(config)}")
    print(f"  Outbound notifications      {'on' if config.notifications.enabled else 'off'}")
    print(f"    bot token                 {_redact(config.notifications.telegram_token)}")
    print(f"    chat id                   {config.notifications.telegram_chat_id or '(not set)'}")
    print(f"  GitHub token passthrough    {'on' if config.github.gh_token else 'off'}")
    print(f"  GitHub gh config copy       {'on' if config.github.gh_config else 'off'}")
    print(f"  Git identity (name/email)   {'on' if config.git.identity else 'off'}")
    print(f"  Git full config copy        {'on' if config.git.full_config else 'off'}")
    print(f"  Provider API keys           {'on' if config.providers.api_keys else 'off'}")
    print(f"  Provider transport/proxy    {'on' if config.providers.transport else 'off'}")
    print(f"  Vikunja skill access        {'on' if config.vikunja.enabled else 'off'}{_vikunja_note(config)}")
    print(f"  Flow-Next auto-push         {'on' if config.flow_next.auto_push else 'off'}")
    for agent, backend in sorted(config.flow_next.review_backends.items()):
        print(f"    review backend ({agent})    {backend}")


def config_check() -> None:
    """Report on the official Claude Telegram channel, without changing anything."""
    config = load_config()
    enabled = config.claude.telegram_channel
    ready = not channel_module.missing_requirements()
    owner = channel_module.channel_owner()

    print(f"Claude Telegram channel: {'enabled' if enabled else 'disabled'} in agent-box config")
    print(f"Plugin installed:        {'yes' if channel_module.plugin_installed() else 'no'}")
    print(f"Bot token configured:    {'yes' if channel_module.token_configured() else 'no'}")
    if owner:
        print(f"Currently active:        yes ({channel_module.describe_owner(owner)})")
    else:
        print("Currently active:        no")
    print()

    if not ready:
        print(channel_module.setup_instructions())
        raise SystemExit(1)
    if not enabled:
        # Saying "agent-box claude will use the channel" here would be a lie:
        # the prerequisites are in place but the setting is off, so nothing
        # passes --channels.
        print("Prerequisites are ready, but the channel is disabled.")
        print("Enable it with:  agent-box config")
        raise SystemExit(1)
    if owner:
        print("Ready. Another Claude session currently owns the channel; a new")
        print("'agent-box claude' will start normally, without Telegram.")
        return
    print(f"Ready. The next 'agent-box claude' will pass --channels {CLAUDE_TELEGRAM_CHANNEL}")


def config_interactive() -> None:
    config = load_config()
    print(f"agent-box configuration ({CONFIG_FILE})")
    print("Press Enter to keep the current value shown in brackets.\n")

    print("-- Claude Telegram channel (Anthropic's official plugin) --")
    print("Controls the live 'agent-box claude' session from Telegram.")
    missing = channel_module.missing_requirements()
    if missing:
        # Refuse to store an enabled channel that cannot work. The plugin is
        # installed from inside a Claude session, so the bootstrap order is
        # necessarily: run Claude, install and configure the plugin, then come
        # back here. Storing true now would only produce a setting that every
        # later run has to warn about.
        print(f"Not available yet: {' and '.join(missing)}.")
        if config.claude.telegram_channel:
            print("Turning the channel off, because it can no longer work.")
        config.claude.telegram_channel = False
    else:
        config.claude.telegram_channel = _prompt_bool(
            "Enable Claude Telegram channel", config.claude.telegram_channel)

    print("\n-- Claude status line --")
    print("Points Claude's statusLine at 'agent-box statusline'.")
    print("A status line configured by anything else is never overwritten.")
    config.claude.statusline = _prompt_bool(
        "Let agent-box manage the Claude status line", config.claude.statusline)

    print("\n-- Outbound Telegram notifications --")
    print("One-way pings only (Codex turn complete, Claude waiting/finished/failed).")
    config.notifications.enabled = _prompt_bool("Enable outbound notifications", config.notifications.enabled)
    if config.notifications.enabled:
        config.notifications.telegram_token = _prompt_secret(
            "  Telegram bot token", config.notifications.telegram_token)
        config.notifications.telegram_chat_id = _prompt_text(
            "  Telegram chat id", config.notifications.telegram_chat_id)
        if not config.notifications.usable:
            print("  note: notifications stay inactive until both a token and a chat id are set")

    print("\n-- Host integration (each exposes host data to the container) --")
    config.github.gh_token = _prompt_bool(
        "Pass a host GitHub token in as GH_TOKEN", config.github.gh_token)
    config.github.gh_config = _prompt_bool(
        "Copy the host ~/.config/gh (contains that token) in", config.github.gh_config)
    config.git.identity = _prompt_bool(
        "Seed git user.name and user.email (for commit attribution)", config.git.identity)
    config.git.full_config = _prompt_bool(
        "Copy the ENTIRE host git config (may include credential helpers, tokens)",
        config.git.full_config)
    config.providers.api_keys = _prompt_bool(
        "Forward API keys from this shell (ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN)",
        config.providers.api_keys)
    config.providers.transport = _prompt_bool(
        "Forward endpoint/proxy settings from this shell (base URL, proxies, CA bundle)",
        config.providers.transport)

    print("\n-- Vikunja skill access (opt-in) --")
    print("Exposes ~/.config/vikunja (server URL + API token, read-only) and")
    print("~/.local/state/vikunja (resumable import journals) to the container,")
    print("and puts the skill's CLI on its PATH. A loopback URL is rewritten to")
    print("host.containers.internal so the container reaches the host's server.")
    print("The token can read and rewrite your entire task tracker.")
    config.vikunja.enabled = _prompt_bool(
        "Expose the host Vikunja credentials to the container", config.vikunja.enabled)
    from . import runtime
    for problem in runtime.vikunja_warnings(config):
        print(f"  note: {problem}")

    print("\n-- Flow-Next auto-push (opt-in) --")
    print("At the end of a Claude/Codex turn, pushes the current feature branch")
    print("only if Git shows locally unpushed commits, Flow-Next confirms one of")
    print("their tasks is actually done, and every safety check passes (clean")
    print("worktree, not main/master/default, no diverged remote, no force).")
    config.flow_next.auto_push = _prompt_bool(
        "Enable Flow-Next auto-push", config.flow_next.auto_push)

    save_config(config)
    # Keep the derived artifacts in step with what was just saved: the
    # container-visible credential file, and everything agent-box owns in
    # Claude's settings.json -- its notification hooks and its status line, in
    # one write. The flow-next hooks are reconciled separately below; each set
    # owns only its own commands and never disturbs the other.
    notify_module.sync_credentials(config)
    settings = CLAUDE_HOME / "settings.json"
    claude_settings.reconcile_claude_settings(
        settings,
        notifications=config.notifications.enabled,
        statusline=config.claude.statusline)
    from . import flow_push
    if config.flow_next.auto_push:
        flow_push.install_flow_push_hooks(settings)
    else:
        flow_push.remove_flow_push_hooks(settings)

    print(f"\nSaved {CONFIG_FILE}\n")
    config_show()
    if missing:
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

NON_RUNTIME = {
    "usage", "statusline", "notify", "config", "selftest", "help", "-h", "--help",
    "build", "update", "rebuild", "flow-next", "attach", "stop",
}
#: --host only makes sense for a real agent launch, never for these.
HOST_ONLY_AGENTS = {"claude", "codex"}


def main() -> None:
    mounts, host, args = parse_global_args(sys.argv[1:])
    script_dir = Path(__file__).resolve().parent.parent
    try:
        if mounts and args and args[0] in NON_RUNTIME:
            die("--mount is only valid before claude/codex/shell/doctor")
        if host and args and args[0] in NON_RUNTIME:
            die("--host is only valid before claude/codex")
        if host and mounts:
            die("--host does not use --mount; there is no container to mount into")
        if args and args[0] == "usage":
            usage_command(args[1:]); return
        if args and args[0] == "statusline":
            statusline_command(args[1:]); return
        if args and args[0] == "notify":
            notify_command(args[1:]); return
        if args and args[0] == "config":
            config_command(args[1:]); return
        if args and args[0] == "flow-next":
            flow_next_command(args[1:]); return
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
        if args and args[0] in {"attach", "stop"}:
            from . import runtime
            if args[0] == "attach":
                raise SystemExit(runtime.attach_main(args[1:]))
            raise SystemExit(runtime.stop_main(args[1:]))
        if host:
            agent = args[0] if args else None
            if agent not in HOST_ONLY_AGENTS:
                die("--host requires 'claude' or 'codex'; shell/doctor are container-only")
            from . import runtime
            raise SystemExit(runtime.host_main(agent, args[1:]))
        from . import runtime
        runtime.runtime_main(args, script_dir, mounts)
    except AgentBoxError as exc:
        die(str(exc))
    except (KeyboardInterrupt, EOFError):
        raise SystemExit(130)
