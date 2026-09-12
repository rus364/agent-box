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
    parse_env_spec,
    save_config,
)
from .errors import AgentBoxError
from .paths import CLAUDE_HOME


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
  agent-box --host attach [session]
  agent-box --host stop
  agent-box config [--project|show|check|apply]
  agent-box usage <codex|claude|all>
  agent-box statusline
  agent-box notify <test|claude|codex>
  agent-box selftest
  agent-box build | update | rebuild
  agent-box upgrade [--check]
  agent-box uninstall [--purge] [--yes]
  agent-box tracker <command> [arguments...]
  agent-box workflow <command> [arguments...]

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
  agent-box config          Interactive setup, starting with putting the
                            agent-box command on your PATH. Persists to
                            ~/.config/agent-box/config.json
  agent-box config show     Print the current settings (token redacted)
  agent-box config check    Verify the official Claude Telegram channel setup
  agent-box config apply    Apply the saved file without asking anything:
                            restore the agent-box symlink in the directory the
                            PATH question chose, install the skills it enables,
                            link them for Claude, and reconcile Claude's
                            settings.json.
                            'agent-box config' and 'agent-box upgrade' both end
                            by doing this, so it is only needed after editing
                            config.json by hand. Starting an agent never
                            applies configuration by itself.

Installation:
  agent-box upgrade         Fetch and move this checkout to the newest release
                            tag (or fast-forward it, until the first tag
                            exists), then apply the saved configuration.
                            Refuses on a checkout with uncommitted changes or
                            unpushed commits. --check only reports.
  agent-box uninstall       Undo what agent-box wrote outside its own
                            directories: the status line and hooks in Claude's
                            settings.json, its skill links and installed
                            skills, its rendered credential files, the symlink
                            it put on your PATH, and every workspace box. Add
                            --purge to also delete agent-box's own config and
                            state (tokens included) and, when it still matches
                            byte for byte, the PATH block it added to your
                            shell startup file; --yes skips that confirmation.

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
workspace image, so --host sessions do not get it. Interactive host sessions
use tmux when installed (requires bash on PATH); otherwise they start directly. Use --host attach
[session] to return, --host stop to end this project's host sessions, or
AGENT_BOX_TMUX=0 to launch directly.

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
    from . import config_menu, runtime
    from . import project_config
    path = project_config.project_path() if project else None
    result = (config_menu.edit(effective_config(environ={}, include_project=False), project=project_config.load(path))
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
    """
    from . import runtime
    config = load_config()
    for line in runtime.apply_config(config):
        print(f"agent-box: {line}")
    for problem in runtime.tracker_warnings(config):
        print(f"agent-box: warning: {problem}", file=sys.stderr)
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


# -- selftest ----------------------------------------------------------

def selftest() -> None:
    from .selftest import run_selftest
    n = run_selftest()
    print(f"agent-box: {n} self-tests passed")


# -- main ----------------------------------------------------------------

NON_RUNTIME = {
    "usage", "statusline", "notify", "config", "selftest", "help", "-h", "--help",
    "build", "update", "rebuild", "tracker", "workflow", "attach", "stop", "upgrade", "uninstall",
    "skills",
}
#: --host only makes sense for a real agent launch, never for these.
HOST_ONLY_AGENTS = {"claude", "codex"}


def main() -> None:
    if sys.argv[1:2] == ["_session"]:
        from . import remote_control
        try:
            raise SystemExit(remote_control.main(sys.argv[2:]))
        except (AgentBoxError, OSError) as exc:
            die(str(exc))
    from . import project_config
    try:
        project_config.load()
    except AgentBoxError as exc:
        die(str(exc))
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
        if host and args and args[0] in NON_RUNTIME - {"attach", "stop"}:
            die("--host is only valid before claude/codex/attach/stop")
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
            if host:
                raise SystemExit(runtime.host_session_main(args[1:], stop=args[0] == "stop"))
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
