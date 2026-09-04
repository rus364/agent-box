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
from .config import (
    CLAUDE_TELEGRAM_CHANNEL,
    FLOW_REVIEW_BACKEND_AGENTS,
    IMAGE_TOOL_GROUP_HELP,
    FLOW_REVIEW_BACKEND_CHOICES,
    load_config,
    parse_env_spec,
    save_config,
)
from .errors import AgentBoxError
from .paths import CLAUDE_HOME, CONFIG_FILE


def die(message: str) -> NoReturn:
    print(f"agent-box: {message}", file=sys.stderr)
    raise SystemExit(1)


def parse_global_args(argv: list[str]) -> tuple[list[str], bool, list[str], list[str]]:
    """Split agent-box's own leading options from the rest of argv.

    Grammar: ``agent-box [--mount PATH[:ro]]... [--env NAME[=VALUE]]... [--host] [claude|codex|shell|doctor] [provider args...]``.
    Agent-box's own options must come first; once a non-option token is seen
    (or a bare ``--``), everything after it is passed through untouched, so
    ``agent-box claude --model opus`` or a hypothetical Claude/Codex
    ``--mount``/``--host`` flag of their own is never misinterpreted as ours.

    ``--host`` runs the resolved subcommand directly on the host instead of
    in a container; ``agent-box claude``/``agent-box codex`` keep meaning
    containerized execution.
    """
    mounts: list[str] = []
    env_specs: list[str] = []
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
        if token == "--env":
            if i + 1 >= len(argv):
                die("--env requires NAME=VALUE or NAME")
            env_specs.append(argv[i + 1])
            i += 2
            continue
        if token.startswith("--env="):
            value = token[len("--env="):]
            if not value:
                die("--env requires NAME=VALUE or NAME")
            env_specs.append(value)
            i += 1
            continue
        if token == "--host":
            host = True
            i += 1
            continue
        break
    return mounts, host, env_specs, argv[i:]


def usage_text() -> None:
    print('''Usage:
  agent-box [--mount PATH[:ro]]... [--env NAME[=VALUE]]... [--host] [codex|claude|shell|doctor] [arguments...]
  agent-box attach [session]
  agent-box stop
  agent-box config [show|check]
  agent-box usage <codex|claude|all>
  agent-box statusline
  agent-box notify <test|claude|codex>
  agent-box selftest
  agent-box build | update | rebuild
  agent-box install
  agent-box uninstall [--purge] [--yes]
  agent-box skills vendor <upstream>

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
  agent-box install         Apply the saved configuration without asking
                            anything: install the skills it enables, link them
                            for Claude, and reconcile Claude's settings.json.
                            'agent-box config' ends by doing this, so it is
                            only needed after updating the agent-box checkout
                            or editing config.json by hand. Starting an agent
                            never applies configuration by itself.
  agent-box uninstall       Undo what agent-box wrote outside its own
                            directories: the status line and hooks in Claude's
                            settings.json, its skill links and installed
                            skills, its rendered credential files, and every
                            workspace box. Add --purge to also delete
                            agent-box's own config and state (tokens
                            included); --yes skips that confirmation.

Environment (overrides for a single command):
  AGENT_BOX_AGENT           Default agent when claude/codex is omitted (default: claude)
  AGENT_BOX_IMAGE           Container image name (default: localhost/agent-box:latest)
  AGENT_BOX_KVM=0           Disable KVM passthrough
  AGENT_BOX_TMUX=0          Run the session directly, with no detachable tmux
  AGENT_BOX_GH_TOKEN=0|1    Withhold / force the host GitHub token for this run
  AGENT_BOX_WORKSPACE_ROOT  Override the host directory mounted into the container
  AGENT_BOX_STATUSLINE=0    Leave Claude's statusLine untouched when applying
  AGENT_BOX_AGENTS_DIR      Override ~/.agents, the shared agent/skill tree
  AGENT_BOX_ENV_<NAME>      Set <NAME> inside the session (see --env below)

--env NAME=VALUE sets a variable inside the session; --env NAME forwards the
value that name already has in this shell, without putting it in the process
list, which is the form to use for anything secret. Repeatable, and both win
over the lists stored in 'agent-box config'. The same thing can be said as a
shell prefix, which is why the namespace exists:

  AGENT_BOX_ENV_DATABASE_URL=postgres://... agent-box codex

A bare 'NAME=VALUE agent-box codex' cannot work: inside the process such a
variable is indistinguishable from the hundreds inherited from your shell, so
honouring it would mean handing the container your whole environment.

--mount PATH[:ro] additionally bind-mounts PATH (an existing absolute host
directory) into the container at that same path, read-write by default or
read-only with the ':ro' suffix. Repeatable. Must appear before the
claude/codex/shell/doctor subcommand; use '--' to stop agent-box's own
option parsing early if a provider argument would otherwise be ambiguous.

--host runs claude/codex directly on the host instead of in a container: no
Podman, the installed host executable, your normal host git/gh credentials.
'agent-box claude'/'agent-box codex' (without --host) keep meaning
containerized execution. Not valid with --mount, and not valid with
shell/doctor, which are container-only. The opt-in agent browser lives in the
workspace image, so --host sessions do not get it.

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


def _prompt_choice(label: str, choices: tuple[str, ...], current: str) -> str:
    """Pick one of a fixed set of values, or none of them.

    A menu rather than free text wherever a wrong answer only fails much
    later: a mistyped Flow-Next backend spec, for instance, surfaces inside a
    review Flow-Next has already started, with an error that points at
    Flow-Next rather than at the typo here.
    """
    print(f"  {label}:")
    for index, choice in enumerate(choices, 1):
        marker = " (current)" if choice == current else ""
        print(f"    {index}) {choice}{marker}")
    none_marker = " (current)" if not current else ""
    print(f"    0) none -- leave it to the tool's own default{none_marker}")
    default = str(choices.index(current) + 1) if current in choices else "0"
    while True:
        answer = input(f"  Choice [{default}]: ").strip() or default
        if answer == "0":
            return ""
        if answer.isdigit() and 1 <= int(answer) <= len(choices):
            return choices[int(answer) - 1]
        print(f"  Enter a number between 0 and {len(choices)}.")


def _prompt_env_names(label: str, current: list[str]) -> list[str]:
    """A whitespace- or comma-separated list of variable names."""
    shown = " ".join(current) or "(none)"
    answer = input(f"{label} [{shown}]: ").strip()
    if not answer:
        return list(current)
    if answer == "-":
        return []
    return [name for name in answer.replace(",", " ").split() if name]


def _prompt_env_assignments(label: str, current: dict[str, str]) -> dict[str, str]:
    """Whitespace-separated ``NAME=VALUE`` pairs."""
    shown = " ".join(f"{k}={v}" for k, v in sorted(current.items())) or "(none)"
    answer = input(f"{label} [{shown}]: ").strip()
    if not answer:
        return dict(current)
    if answer == "-":
        return {}
    pairs = {}
    for token in answer.split():
        name, sep, value = token.partition("=")
        if sep and name:
            pairs[name] = value
        else:
            print(f"  ignoring '{token}': expected NAME=VALUE")
    return pairs


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
    return f" ({runtime.container_vikunja_url(config.vikunja.url)})"


def _image_note(config) -> str:
    """Say when the built image no longer matches what is configured."""
    from . import runtime
    try:
        missing, extra = runtime.image_tool_drift(config)
    except Exception:
        return ""
    if missing:
        return f" (image lacks {', '.join(missing)}; run 'agent-box build')"
    if extra:
        return f" (image still has {', '.join(extra)}; run 'agent-box build')"
    return ""


def _browser_note(config) -> str:
    """Name what the switch actually starts, and where the pages land.

    "on" alone would understate it: this is a real browser the agent drives,
    and everything it loads is inside the workspace box with the agent, the
    project and every other enabled integration.
    """
    if not config.browser.enabled:
        return ""
    if not config.image.tools.get("browser"):
        return " (but the browser tool group is off, so the image has none)"
    return " (headless Chromium in the box; --host sessions unaffected)"


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
    print(f"  Share skills with Claude    {'on' if config.skills.claude_links else 'off'}")
    print(f"  Superpowers skills          {'on' if config.skills.superpowers else 'off'}")
    print(f"  Image tool groups           {', '.join(config.image.enabled_groups()) or '(base only)'}"
          f"{_image_note(config)}")
    print(f"  Vikunja integration         {'on' if config.vikunja.enabled else 'off'}{_vikunja_note(config)}")
    if config.vikunja.enabled:
        print(f"    server                    {config.vikunja.url or '(not set)'}")
        print(f"    api token                 {_redact(config.vikunja.token)}")
    print(f"  Agent browser (containers)  {'on' if config.browser.enabled else 'off'}{_browser_note(config)}")
    print(f"  Flow-Next auto-push         {'on' if config.flow_next.auto_push else 'off'}")
    for agent in FLOW_REVIEW_BACKEND_AGENTS:
        backend = config.flow_next.review_backends.get(agent)
        shown = backend or "(Flow-Next's own default)"
        label = f"review backend ({agent})"
        print(f"    {label:<26}{shown}")
    forwarded = ", ".join(config.env.forward) or "(none)"
    print(f"  Forwarded env variables     {forwarded}")
    assignments = ", ".join(f"{k}={v}" for k, v in sorted(config.env.set.items())) or "(none)"
    print(f"  Set env variables           {assignments}")


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

    print("\n-- Shared skills --")
    print("~/.agents/skills is read by Codex directly; these symlinks let Claude")
    print("read the same files. They live in your real ~/.claude/skills, so they")
    print("also apply to Claude sessions started outside agent-box.")
    config.skills.claude_links = _prompt_bool(
        "Link shared skills into ~/.claude/skills", config.skills.claude_links)
    print("agent-box also ships three engineering-practice skills, vendored from")
    print("obra/superpowers (MIT): systematic debugging, test-driven development,")
    print("and verification before completion. They change how an agent works, so")
    print("they are off unless you want them.")
    config.skills.superpowers = _prompt_bool(
        "Install the superpowers practice skills", config.skills.superpowers)

    print("\n-- Vikunja (opt-in) --")
    print("Task tracking for both agents. agent-box installs the skill itself and")
    print("holds the server URL and API token here, so there is nothing to set up")
    print("on the host first. A loopback or LAN address is redirected so the")
    print("container reaches your server rather than itself.")
    print("The token can read and rewrite your entire task tracker.")
    config.vikunja.enabled = _prompt_bool("Enable the Vikunja integration", config.vikunja.enabled)
    if config.vikunja.enabled:
        config.vikunja.url = _prompt_text(
            "  Server URL (e.g. http://localhost:3456)", config.vikunja.url)
        config.vikunja.token = _prompt_secret("  API token", config.vikunja.token)
    from . import runtime
    for problem in runtime.vikunja_warnings(config):
        print(f"  note: {problem}")

    print("\n-- Extra environment variables --")
    print("Anything your projects need that agent-box does not know about.")
    print("Enter names to forward from this shell (values stay out of the process")
    print("list, so use this for secrets), or NAME=VALUE pairs stored in the")
    print("config file. Enter '-' to clear a list.")
    config.env.forward = _prompt_env_names("  Forward from this shell", config.env.forward)
    config.env.set = _prompt_env_assignments("  Set explicitly", config.env.set)

    print("\n-- Image contents --")
    print("Optional tool groups built into the workspace image. Everything else")
    print("your projects need belongs in the project, through mise. Changing an")
    print("answer here takes effect on the next 'agent-box build'.")
    for group, description in IMAGE_TOOL_GROUP_HELP.items():
        config.image.tools[group] = _prompt_bool(
            f"  {description}", config.image.tools.get(group, False))

    print("\n-- Agent-controlled browser (opt-in) --")
    print("Gives containerized Claude and Codex a Playwright MCP server driving a")
    print("headless Chromium inside the workspace box, so an agent can open and")
    print("read web pages by itself. Each session gets an isolated, in-memory")
    print("profile: no cookies, logins or browsing state survive it, and none of")
    print("your own browser's profile, cookies or credentials are exposed.")
    print("Anything the browser loads is inside the box, on the same side of the")
    print("trust boundary as the agent and your mounted project.")
    print("'agent-box --host claude/codex' does not get this browser.")
    config.browser.enabled = _prompt_bool(
        "Let containerized agents drive a headless browser", config.browser.enabled)
    if config.browser.enabled and not config.image.tools.get("browser"):
        print("  note: the browser tool group is off, so the image has no browser to")
        print("        drive. Turn it on above, or the sessions will find none.")

    print("\n-- Flow-Next auto-push (opt-in) --")
    print("At the end of a Claude/Codex turn, pushes the current feature branch")
    print("only if Git shows locally unpushed commits, Flow-Next confirms one of")
    print("their tasks is actually done, and every safety check passes (clean")
    print("worktree, not main/master/default, no diverged remote, no force).")
    config.flow_next.auto_push = _prompt_bool(
        "Enable Flow-Next auto-push", config.flow_next.auto_push)

    print("\n-- Flow-Next review routing (optional) --")
    print("Which backend reviews the work, chosen by the agent you launched.")
    print("Leave both at 'none' to let Flow-Next resolve it from .flow/config.json.")
    for agent in FLOW_REVIEW_BACKEND_AGENTS:
        chosen = _prompt_choice(
            f"Review backend when the primary agent is {agent}",
            FLOW_REVIEW_BACKEND_CHOICES,
            config.flow_next.review_backends.get(agent, ""))
        if chosen:
            config.flow_next.review_backends[agent] = chosen
        else:
            config.flow_next.review_backends.pop(agent, None)

    save_config(config)
    # Applying is not a second feature: `agent-box install` runs exactly this,
    # so a configuration saved here and one edited by hand end up producing
    # the same host state.
    print(f"\nSaved {CONFIG_FILE}\n")
    for line in runtime.apply_config(config):
        print(f"  {line}")
    print()
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
    "build", "update", "rebuild", "flow-next", "attach", "stop", "install", "uninstall",
    "skills",
}
#: --host only makes sense for a real agent launch, never for these.
HOST_ONLY_AGENTS = {"claude", "codex"}


def main() -> None:
    mounts, host, env_specs, args = parse_global_args(sys.argv[1:])
    script_dir = Path(__file__).resolve().parent.parent
    try:
        parsed_env = [parse_env_spec(spec) for spec in env_specs]
    except ValueError as exc:
        die(str(exc))
    try:
        if mounts and args and args[0] in NON_RUNTIME:
            die("--mount is only valid before claude/codex/shell/doctor")
        if env_specs and args and args[0] in NON_RUNTIME:
            die("--env is only valid before claude/codex/shell/doctor")
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
        if args and args[0] == "skills":
            from . import vendor
            raise SystemExit(vendor.vendor_main(args[1:]))
        if args and args[0] == "install":
            from . import runtime
            raise SystemExit(runtime.install_main(args[1:]))
        if args and args[0] == "uninstall":
            from . import runtime
            raise SystemExit(runtime.uninstall_main(args[1:]))
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
            raise SystemExit(runtime.host_main(agent, args[1:], parsed_env))
        from . import runtime
        runtime.runtime_main(args, script_dir, mounts, parsed_env)
    except AgentBoxError as exc:
        die(str(exc))
    except (KeyboardInterrupt, EOFError):
        raise SystemExit(130)
