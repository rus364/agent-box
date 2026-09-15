"""Command-line dispatch. Kept free of business logic beyond argument parsing."""
from __future__ import annotations

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
    effective_config,
    effective_config_source,
    load_config,
    save_config,
    session_snapshot_only,
)
from .errors import AgentBoxError
from .paths import CLAUDE_HOME


def die(message: str) -> NoReturn:
    print(f"agent-box: {message}", file=sys.stderr)
    raise SystemExit(1)


def usage_text() -> None:
    from .launch_options import SPEC, help_text
    print(f"""Usage:
  agent-box [OPTIONS] [claude|codex|shell|doctor] [AGENT ARGS...]
  agent-box [OPTIONS] ext API [claude|codex] [AGENT ARGS...]
  agent-box [--host] [--workspace PATH] attach [SESSION] | stop
  agent-box config [--project | show | check | apply]
  agent-box usage [codex|claude|all] [--no-cache] [--json]
  agent-box notify test
  agent-box build | update | rebuild
  agent-box upgrade [--check]
  agent-box uninstall [--purge] [--yes]
  agent-box completion bash|zsh
  agent-box tracker COMMAND | workflow COMMAND
  agent-box selftest | help

Options go before the agent; everything after it reaches the agent unchanged,
and '--' ends them early. Without claude/codex the default agent starts
(launch.default_agent). Launch options shape only this launch. Options in the
"for this session" groups travel with the session, are never saved, and
'agent-box config show' inside the session lists them.
SPEC is {SPEC}, for example claude:opus:high.

{help_text()}

Sessions:
  claude, codex           Start the agent in this workspace's box, or on the host with --host
  shell, doctor           A shell in the box; check the box and its integrations
  ext API                 Start claude/codex against an enabled external API provider
  attach [SESSION]        Reattach to a detached session; detach with C-\\ d
  stop                    Remove the box and its sessions; with --host, this project's host sessions

Configuration:
  config                  Interactive setup, saved to ~/.config/agent-box/config.json and applied
  config --project        Edit this repository's .agent-box.json
  config show             Effective settings, tokens redacted
  config check            Check the Claude Telegram channel setup
  config apply            Apply the saved file after editing it by hand
  usage                   Remaining subscription limits
  notify test             Send a test Telegram notification

Installation:
  build                   Build the workspace image; update refreshes its tools,
                          rebuild also pulls the base image
  upgrade                 Move this checkout to the newest release and apply the
                          configuration; --check only reports
  uninstall               Remove what agent-box wrote outside its directories and every box;
                          --purge also deletes its config and state, --yes skips confirmation
  completion bash|zsh     Print a shell completion script; --help says how to load it

Other:
  tracker, workflow       Task tracker and workflow; see their --help
  selftest                Fast checks without Podman
  statusline, notify claude|codex
                          Entry points for Claude and Codex hooks

Environment:
  AGENT_BOX_ENV_NAME=VALUE  --env NAME=VALUE as a shell prefix
  AGENT_BOX_IMAGE           Workspace image (default localhost/agent-box:latest)
  AGENT_BOX_CONFIG_DIR      Instead of ~/.config/agent-box
  AGENT_BOX_STATE_DIR       Instead of ~/.local/share/agent-box
  AGENT_BOX_AGENTS_DIR      Instead of ~/.agents
  AGENT_BOX_STATUSLINE=0    Leave Claude's status line alone when applying

Examples:
  agent-box
  agent-box --host codex
  agent-box --reviewer claude:opus:high --review always codex
  agent-box --mount /srv/specs:ro --env DATABASE_URL claude
  export VIKUNJA_TOKEN=...; agent-box --tracker https://tasks.example.org --project 12 codex

Details: README.md and docs/integrations/.""")


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
    rest = [arg for arg in args if arg.startswith("-")]
    positional = [arg for arg in args if not arg.startswith("-")]
    if len(positional) > 1:
        die("usage accepts one provider: codex, claude, or all")
    provider = positional[0] if positional else "all"
    if provider not in ("claude", "codex", "all"):
        die(f"unsupported usage provider '{provider}'; expected codex, claude, or all")
    if set(rest) - {"--no-cache", "--json"}:
        die("usage accepts --no-cache and --json")
    if rest:
        import json
        providers = ("codex", "claude") if provider == "all" else (provider,)
        snapshots = [usage_module.usage_snapshot(p, no_cache="--no-cache" in rest) for p in providers]
        if "--json" in rest:
            print(json.dumps(snapshots if provider == "all" else snapshots[0], indent=2))
        else:
            for snapshot in snapshots:
                print(f"{snapshot['provider']}: {usage_module.format_snapshot(snapshot)}")
                if snapshot.get("error"):
                    print(snapshot["error"], file=sys.stderr)
        if "--no-cache" in rest and any(not snapshot["fresh"] for snapshot in snapshots):
            raise SystemExit(1)
        return
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
            usage_module.colorize(usage_module.custom_usage(), usage_module.CUSTOM_COLOR),
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
        payload_text = rest[0] if rest else sys.stdin.read()
        notify_module.notify_codex(payload_text)
    else:
        die(f"unsupported notify action '{action}'; expected test, claude, or codex")


# -- config ----------------------------------------------------------------

def _redact(token: str) -> str:
    if not token:
        return "(not set)"
    return "(set)"


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
        return " (installs on the next 'agent-box config apply')"
    if claude_settings.statusline_is_ours(settings):
        return ""
    return " (left alone: another status line is configured)"


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
    config = effective_config()
    source = effective_config_source()
    print(f"Config source: {source['kind']} ({source['path']})")
    if source["kind"] == "runtime":
        print("Rendered container configuration; host-only settings are not included.")
    if getattr(config, "_launch", None):
        from .launch_options import describe
        print(f"Session options: {describe(config._launch)}")
    print()
    print(f"  Default agent               {config.launch.default_agent}")
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
    print("  External API providers")
    for name, spec in config.external_providers.items():
        print(f"    {name}: {'on' if spec.enabled else 'off'}; key {'<configured>' if spec.api_key else '<empty>'}")
        for agent in ("codex", "claude"):
            endpoint = getattr(spec, agent)
            print(f"      {agent}: {endpoint.url or '(not set)'}; models {', '.join(endpoint.models) or '(none)'}")
            print(f"        default {endpoint.default_model or '(first configured)'}; env {len(endpoint.env)} configured")
            if agent == 'codex':
                print(f"        provider options {len(endpoint.provider_options)} fields; config {len(endpoint.config)} fields")
            for model, settings in endpoint.models.items():
                details = (f"effort {settings['effort']}; levels {','.join(settings['supported_efforts']) or '(unknown)'}; "
                           f"context {settings['context_window'] or '(CLI default)'}; "
                           f"compact {settings['auto_compact_token_limit'] or '(CLI default)'}; env {len(settings['env'])} configured")
                if agent == 'codex':
                    details += (f"; input {','.join(settings['input_modalities'])}; "
                                f"parallel {settings['supports_parallel_tool_calls']}; summaries {settings['supports_reasoning_summaries']}; "
                                f"metadata {len(settings['metadata'])} fields")
                print(f"        {model}: {details}")
    print(f"  Share skills with Claude    {'on' if config.skills.claude_links else 'off'}")
    print(f"  Image tool groups           {', '.join(config.image.enabled_groups()) or '(base only)'}"
          f"{_image_note(config)}")
    print(f"  Tracker                     {'on' if config.tracker.enabled else 'off'} ({config.tracker.provider})")
    for name, connection in config.tracker.connections.items():
        print(f"    {name}: {connection.url or '(not set)'}; token {_redact(connection.token)}")
    print(f"  Workflow                    {'on' if config.workflow.enabled else 'off'}")
    print(f"    merge mode                {config.workflow.merge_mode}")
    print(f"    cleanup local branches    {config.workflow.cleanup_local_branches}")
    print(f"    cleanup remote branches   {config.workflow.cleanup_remote_branches}")
    print(f"    scout limit               {config.workflow.scout_limit}; managed shared slots, native coordinator-counted (no CLI lease)")
    print(f"    worker execution          {config.workflow.worker.execution}")
    direct = config.workflow.worker.execution == "direct"
    model_source = ("active-session" if direct else
                    "chosen after auto execution decision" if config.workflow.worker.execution == "auto" else
                    "session, override, or tier selected at delegation")
    print(f"    worker model source       {model_source}")
    direct_shared = "; scouts only, inapplicable to direct workers" if direct else ""
    direct_worker = "  [inapplicable to direct workers]" if direct else ""
    print(f"    delegated routing         {config.workflow.routing}; weights {config.workflow.weights}{direct_shared}")
    print(f"    review                    {config.workflow.review.policy}; files {config.workflow.review.max_files}; lines {config.workflow.review.max_lines}")
    print(f"    review routing            {config.workflow.review.routing}")
    for role, settings in (("worker", config.workflow.worker), ("reviewer", config.workflow.review)):
        override = settings.override
        suffix = direct_worker if role == "worker" else ""
        print(f"    {role + ' model scope':26s}{override.scope}{suffix}")
        for provider, spec in override.models.items():
            print(f"    {role} override {provider}: {spec}{suffix}")
    print(f"    fallback / auto-push      {config.workflow.fallback} / {config.workflow.auto_push}")
    print("    auto-push scope           " + ("accepted base after local merge" if config.workflow.merge_mode == "local" else "parent task branch after subtask integration"))
    for tier, models in config.workflow.tiers.items():
        suffix = ("  [scouts only; inapplicable to direct workers]" if direct and tier == "0"
                  else direct_worker)
        print(f"    worker/scout tier {tier}: {models}{suffix}")
    for tier, models in config.workflow.review.tiers.items():
        print(f"    reviewer tier {tier}: {models}")
    print("    fast                      opt-in speed request, not delivered service; false preserves provider/native defaults")
    print(f"  Agent browser (containers)  {'on' if config.browser.enabled else 'off'}{_browser_note(config)}")
    print(f"  Host audio (containers)     {'on (microphone and speakers)' if config.audio.enabled else 'off'}")
    forwarded = ", ".join(config.env.forward) or "(none)"
    print(f"  Forwarded env variables     {forwarded}")
    assignments = ", ".join(f"{k}=<configured>" for k in sorted(config.env.set)) or "(none)"
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


def path_status(config) -> list[str]:
    """What is left to do before the shell can run ``agent-box``.

    Printed after applying rather than only inside the menu, because the two
    ways to finish -- shell rc line, or nothing at all -- are both things the
    user does next, in a terminal, with the commands in front of them.
    """
    from . import pathsetup
    if pathsetup.link_state().kind == "ours":
        return []
    bin_dir = Path(config.install.bin_dir) if config.install.bin_dir else pathsetup.DEFAULT_BIN_DIR
    if config.install.bin_dir:
        return [f"{bin_dir} is not on PATH yet in this shell; open a new one, or add it:",
                *pathsetup.rc_block(bin_dir).splitlines()]
    return ["agent-box is not on PATH. Run 'agent-box config' and answer PATH "
            "integration, or do it by hand:", *pathsetup.manual_instructions(bin_dir)]


def config_interactive(project=False) -> None:
    if not project and session_snapshot_only():
        die("refusing to edit the saved configuration: this session reads the rendered "
            f"configuration {effective_config_source()['path']} and does not mount the file; "
            "run 'agent-box config' on the host")
    from . import config_menu, runtime
    from . import project_config
    path = project_config.project_path() if project else None
    result = (config_menu.edit(effective_config(include_project=False), project=project_config.load(path))
              if project else config_menu.edit(load_config()))
    if result is None:
        print("Configuration unchanged.")
        return
    if project:
        project_config.save(path, result.project)
        print(f"Saved project configuration: {path}")
        return
    from . import pathsetup
    config = result.config
    save_config(config)
    for line in runtime.apply_config(config):
        print(line)
    if result.edit_rc:
        print(pathsetup.append_rc_block(pathsetup.rc_file(), Path(config.install.bin_dir)))
    config_show()
    for line in path_status(config):
        print(f"agent-box: {line}")


def config_apply() -> None:
    """``agent-box config apply``: apply the saved file, asking nothing.

    The interactive flow ends by calling exactly this, so a configuration
    answered in the UI and one edited by hand produce the same host state.
    It is a subcommand of ``config`` rather than a verb of its own because
    that is what it is -- there is no separate thing to install, and calling
    it ``install`` only ever competed with the word for cloning the
    repository. ``agent-box upgrade`` applies afterwards on its own, which
    leaves hand-editing ``config.json`` as the one reason to run this.

    Inside a workspace box the saved file is not mounted, so the command
    applies what the rendered session snapshot describes -- the shipped
    skills and the configured agent profiles -- and leaves the host-only
    sections alone; see :func:`agent_box.runtime.apply_config`.
    """
    from . import runtime
    session = session_snapshot_only()
    config = effective_config() if session else load_config()
    if session:
        print(f"agent-box: applying the rendered session configuration "
              f"{effective_config_source()['path']}: skills and profiles only; "
              "notifications, hooks, status line, credentials, PATH and Claude "
              "links are host-only and untouched")
    for line in runtime.apply_config(config, session=session):
        print(f"agent-box: {line}")
    for problem in runtime.tracker_warnings(config) + runtime.audio_warnings(config):
        print(f"agent-box: warning: {problem}", file=sys.stderr)
    if not session:
        for line in path_status(config):
            print(f"agent-box: {line}")


def config_command(args: list[str]) -> None:
    if not args:
        config_interactive()
    elif args == ["--project"]:
        config_interactive(project=True)
    elif args == ["show"]:
        config_show()
    elif args == ["check"]:
        config_check()
    elif args == ["apply"]:
        config_apply()
    else:
        die("expected: agent-box config [--project|show|check|apply]")


# -- completion --------------------------------------------------------

def completion_command(args: list[str]) -> None:
    from . import completion
    asks_help = args[-1:] in (["-h"], ["--help"])
    if not args or asks_help and (len(args) == 1 or len(args) == 2 and args[0] in completion.SHELLS):
        print(completion.HELP)
    elif len(args) == 1 and args[0] in completion.SHELLS:
        print(completion.script(args[0]), end="")
    else:
        die("expected: agent-box completion bash|zsh; see agent-box completion --help")


# -- selftest ----------------------------------------------------------

def selftest() -> None:
    from .selftest import run_selftest
    n = run_selftest()
    print(f"agent-box: {n} self-tests passed")


# -- main ----------------------------------------------------------------

NON_RUNTIME = {
    "usage", "statusline", "notify", "config", "selftest", "help", "-h", "--help",
    "build", "update", "rebuild", "tracker", "workflow", "attach", "stop", "upgrade", "uninstall",
    "completion",
}
#: --host only makes sense for a real agent launch, never for these.
HOST_ONLY_AGENTS = {"claude", "codex"}


def _agent_and_rest(args: list[str]) -> tuple[str | None, list[str]]:
    """An explicit claude/codex, or None so the launch resolves the default agent."""
    return (args[0], args[1:]) if args and args[0] in HOST_ONLY_AGENTS else (None, args)


def main() -> None:
    if sys.argv[1:2] == ["_external"]:
        from . import external_launch
        try:
            raise SystemExit(external_launch.main(sys.argv[2:]))
        except (AgentBoxError, OSError) as exc:
            die(str(exc))
    if sys.argv[1:2] == ["_session"]:
        from . import remote_control
        try:
            raise SystemExit(remote_control.main(sys.argv[2:]))
        except (AgentBoxError, OSError) as exc:
            die(str(exc))
    if sys.argv[1:2] == ["_complete"]:
        # Before the project configuration loads: a broken .agent-box.json must not break Tab.
        from . import completion
        completion.main(sys.argv[2:])
        return
    from . import launch_options, project_config
    try:
        project_config.load()
        options, args = launch_options.parse(sys.argv[1:])
    except AgentBoxError as exc:
        die(str(exc))
    script_dir = Path(__file__).resolve().parent.parent
    try:
        given = options.given()
        if args and args[0] in NON_RUNTIME:
            allowed = {"host", "workspace"} if args[0] in {"attach", "stop"} else set()
            refused = [option for option in given if option.name not in allowed]
            if refused:
                die(f"{launch_options.spelling(refused[0])} is not valid before {args[0]}; "
                    + ("it accepts only --host and --workspace" if allowed else "it applies only when starting an agent"))
        if options.host:
            refused = [option for option in given if option.container]
            if refused:
                die(f"{launch_options.spelling(refused[0])} needs a container; it does not apply with --host")
        if args and args[0] == "usage":
            usage_command(args[1:]); return
        if args and args[0] == "statusline":
            statusline_command(args[1:]); return
        if args and args[0] == "notify":
            notify_command(args[1:]); return
        if args and args[0] == "config":
            config_command(args[1:]); return
        if args and args[0] == "completion":
            completion_command(args[1:]); return
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
        if args and args[0] == "tracker":
            from .tracker import cli as tracker_cli
            raise SystemExit(tracker_cli.main(args[1:]))
        if args and args[0] == "workflow":
            from . import workflow
            raise SystemExit(workflow.main(args[1:]))
        if args and args[0] == "upgrade":
            from . import selfupdate
            raise SystemExit(selfupdate.upgrade_main(args[1:]))
        if args and args[0] == "uninstall":
            from . import runtime
            raise SystemExit(runtime.uninstall_main(args[1:]))
        if args and args[0] in {"attach", "stop"}:
            from . import runtime
            if options.host:
                raise SystemExit(runtime.host_session_main(args[1:], stop=args[0] == "stop",
                                                           workspace_root=options.workspace))
            if args[0] == "attach":
                raise SystemExit(runtime.attach_main(args[1:], options.workspace))
            raise SystemExit(runtime.stop_main(args[1:], options.workspace))
        if args and args[0] == "ext":
            if len(args) < 2:
                die("usage: agent-box [--host] ext NAME [codex|claude] [CLI args]")
            from . import runtime
            name, rest = args[1], args[2:]
            if rest and rest[0] in {"shell", "doctor"}:
                die(f"ext runs claude or codex; {rest[0]} does not use an API provider")
            if options.host:
                raise SystemExit(runtime.host_main(*_agent_and_rest(rest), options, api_provider=name))
            runtime.runtime_main(rest, script_dir, options, api_provider=name)
            return
        if options.host:
            if args and args[0] in {"shell", "doctor"}:
                die("--host runs claude or codex; shell/doctor are container-only")
            from . import runtime
            raise SystemExit(runtime.host_main(*_agent_and_rest(args), options))
        from . import runtime
        runtime.runtime_main(args, script_dir, options)
    except AgentBoxError as exc:
        die(str(exc))
    except (KeyboardInterrupt, EOFError):
        raise SystemExit(130)
