"""Command-line dispatch. Kept free of business logic beyond argument parsing."""
from __future__ import annotations

import json
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
  agent-box config [--project | show | describe [PATH] | check | apply]
  agent-box config set --project|--global [--dry-run] PATH=JSON... [--reset PATH]...
  agent-box config proposal [apply | discard]
  agent-box config ai [claude|codex]
  agent-box usage [codex|claude|all] [--no-cache] [--json]
  agent-box notify test
  agent-box links open URL
  agent-box build | update | rebuild
  agent-box uninstall [--purge] [--yes]
  agent-box completion bash|zsh
  agent-box tracker COMMAND | workflow COMMAND
  agent-box selftest | help

Options go before the agent; everything after it reaches the agent unchanged,
and '--' ends them early. Without claude/codex the default agent starts
(agents.default) and receives only options: a prompt needs the agent named,
so an unknown word is refused as a command instead of becoming a prompt. Launch options shape only this launch. Options in the
"for this session" groups travel with the session, are never saved, and
'agent-box config show' inside the session lists them.
SPEC is {SPEC}, for example claude:opus:high.

{help_text()}

Sessions:
  claude, codex           Start the agent in this workspace's box, or on the host with --host
  shell, doctor           A shell in the box; check the box and its integrations
                          Create from current settings, or enter the running box;
                          explicit shared-access conflicts require agent-box stop
  ext API                 Start claude/codex against an enabled external API provider
  attach [SESSION]        Reattach to a detached session; detach with C-\\ d
  stop [--cluster-data] [--podman-data]
                          Remove the box and its sessions and stop its k3s cluster; --cluster-data also
                          deletes the cluster's data, --podman-data the nested Podman data even when
                          box.podman.persistent_data keeps it; with --host, this project's host sessions
  workspaces [rm ID|PATH... | prune] [--yes]
                          Every project's box, k3s cluster, nested Podman data and state on this host;
                          rm deletes all of it for the named workspaces, prune for every workspace
                          whose directory is gone; both confirm first unless --yes

Configuration:
  config                  Interactive setup, saved to ~/.config/agent-box/config.json and applied
  config --project        Edit this repository's .agent-box.json
  config show             Effective settings, tokens redacted
  config describe [PATH]  JSON per setting for agents: help, layered values, project eligibility; secrets redacted
  config set --project    Validate, diff and save .agent-box.json changes; values are JSON, --reset inherits again
  config set --global     Record checked global changes as this checkout's proposal; credentials stay in the menu
  config proposal         Show the proposal; apply saves and applies it on the host after a y/N confirmation
  config ai [AGENT]       Configure agent-box with an agent session in the box; offers global proposals after it
  config check            Check the Claude Telegram channel setup
  config apply            Apply the saved file after editing it by hand
  usage                   Remaining subscription limits
  notify test             Send a test Telegram notification

Installation:
  build                   Build the workspace image; update refreshes its tools,
                          rebuild also pulls the base image
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

Details: README.md and docs/.""")


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
        if provider == "all":
            blocks = usage_module.configured_usage_blocks()
            providers = tuple(p for p in providers if getattr(blocks, f"{p}_usage"))
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
        print(usage_module.join_segments(*usage_module.usage_segments()))


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

def _statusline_note(config) -> str:
    """Say so when Claude's status line is somebody else's, not agent-box's.

    Reconciliation never overwrites a status line it does not own, so "on"
    alone could otherwise look like a promise agent-box is not keeping.
    """
    if not config.agents.claude.statusline.enabled:
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
    """Host mode also needs Podman when the isolated browser is enabled."""
    if not config.box.browser.enabled:
        return ""
    return " (isolated browser container per session; local Podman required, including --host)"


def _k3s_note(config) -> str:
    if not config.box.k3s.enabled:
        return ""
    return " (sidecar cluster per project; --host sessions get none)"


def _show_notes(config) -> dict[str, str]:
    """Explanations derived from state, keyed by the dotted path they qualify.

    Only these are written by hand. Every value comes from walking the schema,
    so a new setting appears in ``config show`` without code here; the
    rejected alternative, one print per field, silently lost fields.
    """
    notes = {
        # The status line's two settings print as one row, so the note qualifies the row.
        "agents.claude.statusline": _statusline_note(config),
        "box.tools": _image_note(config),
        "box.browser.enabled": _browser_note(config),
        "box.k3s.enabled": _k3s_note(config),
        "workflow.auto_push": (" (accepted base after local merge)" if config.workflow.merge_mode == "local"
                               else " (parent task branch after subtask integration)"),
    }
    return notes


def _show_value(value, hidden: bool) -> str:
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, list):
        # A hidden list holds variable names (box.env.forward), never their values.
        from .config import candidate_label
        return ", ".join(candidate_label(item) if isinstance(item, dict) else str(item) for item in value) or "(none)"
    if isinstance(value, dict):
        return ", ".join(f"{key}=<configured>" if hidden else f"{key}={json.dumps(item)}"
                         for key, item in value.items()) or "(none)"
    if hidden:
        return "(set)" if value else "(not set)"
    return str(value) if value != "" else "(not set)"


def _hidden(at) -> bool:
    from . import config_menu
    # A command line routinely carries a bearer token and a literal env value
    # may be one despite its help; the menu shows both for editing.
    return config_menu.secret(at) or at == ("agents", "claude", "statusline", "command") or at[:2] == ("box", "env")


def _show_rows(data, path=()):
    """``(depth, key, value or None for a heading, dotted path)`` in menu order.

    Below a section's first level, a mapping of plain values renders on one
    line, so a model candidate or a tracker connection stays one row.
    """
    from . import config_menu

    def leaf(value, at):
        return not isinstance(value, dict) or config_menu.json_field(at)

    for key in config_menu.menu_keys(data, path):
        value, at = data[key], (*path, key)
        if leaf(value, at):
            yield len(path), key, _show_value(value, _hidden(at)), at
        elif len(at) > 2 and all(leaf(item, (*at, name)) for name, item in value.items()):
            yield len(path), key, "; ".join(f"{name} {_show_value(item, _hidden((*at, name)))}"
                                             for name, item in value.items()), at
        else:
            yield len(path), key, None, at
            yield from _show_rows(value, at)


def _rendered_only(data, rendered, path=()):
    """``data`` narrowed to the keys the snapshot carries, level by level.

    A free-keyed value such as ``registry_mirrors`` stays whole: its entries
    are the project's data, not settings, and a snapshot rendered for another
    project would hide them. ``tracker.connections`` is not one, so its
    accounts still narrow to the snapshot's.
    """
    from .config_menu import json_field
    return {key: _rendered_only(value, rendered[key], (*path, key))
            if isinstance(value, dict) and isinstance(rendered[key], dict) and not json_field((*path, key))
            else value for key, value in data.items() if key in rendered}


def _snapshot_sections(source) -> dict:
    """What a runtime snapshot renders; its `launch` key holds this session's flags, not settings."""
    rendered = read_json(Path(source["path"]))
    return {key: value for key, value in rendered.items() if key != "launch"} if isinstance(rendered, dict) else {}


_DESCRIBED_TYPES = {bool: "boolean", int: "integer", float: "number", str: "string", list: "list", dict: "object"}


def _masked(value, hidden: bool):
    """Whether a secret is set, without the secret."""
    if not hidden or isinstance(value, (bool, list)):
        # A switch is its own value, and a hidden list holds variable names (box.env.forward).
        return value
    if isinstance(value, dict):
        return {key: "<redacted>" for key in value}
    return "<redacted>" if value else ""


def config_description(prefix: str = "") -> dict:
    """Every setting as data for an agent: help, values by layer, project eligibility.

    Help, masking, order and eligibility are the menu's and the project
    loader's own, so ``config ai`` cannot explain or scope a setting
    differently from ``agent-box config``. A snapshot inside a box renders
    only some sections; the rest keep help and defaults but claim no global
    value, instead of passing defaults off as the host's choices.
    """
    from . import config_menu, project_config
    from .config import TRACKER_PROVIDERS, Config, TrackerConnection
    from .launch_options import describe as describe_launch
    from .model_catalog import BUNDLED_MODELS
    source = effective_config_source()
    baseline = effective_config(include_project=False)
    project_file = project_config.project_path()
    project = project_config.load(project_file)
    defaults = Config().to_dict()
    known = baseline.to_dict()
    # Session flags apply last. Comparing with the same resolution without them
    # attributes a value to the session; loader normalization never does.
    actual = effective_config().to_dict()
    unlaunched = project_config.apply(baseline, project).to_dict()
    launch = getattr(baseline, "_launch", {})
    # The editor's baseline: a session's --tracker account is not a global value.
    global_urls = baseline._global_tracker_urls
    connections = known["tracker"]["connections"]
    # A session bound to another URL does not carry the global token, so its presence is unknown here.
    accounts = {
        name: dict(connections[name]) if connections.get(name, {}).get("url") == url
        else {"url": url} for name, url in global_urls.items()}
    known["tracker"]["connections"] = accounts
    resolved = actual
    if source["kind"] == "runtime":
        rendered = _snapshot_sections(source)
        known, resolved = _rendered_only(known, rendered), _rendered_only(actual, rendered)
        # A snapshot carries every global URL on its own, but a token only for an account it renders.
        rendered_accounts = rendered.get("tracker", {}).get("connections", {})
        if "tracker" in known:
            known["tracker"]["connections"] = {name: row if name in rendered_accounts else {"url": row["url"]}
                                              for name, row in accounts.items()}
    # As in the menu, a tracker without a global account has an empty row by default.
    for name in TRACKER_PROVIDERS:
        defaults["tracker"]["connections"].setdefault(name, TrackerConnection().to_dict())
        if "tracker" in known and name not in global_urls:
            known["tracker"]["connections"].setdefault(name, TrackerConnection().to_dict())
    defaults["tracker"]["connections"]["jira"]["username"] = ""
    tree = project_config.merge(project_config.merge(defaults, known), project)
    tree.pop("version")
    tree["tracker"].setdefault("project", "")
    tree["tracker"].setdefault("recursive", False)
    fields = []

    def walk(data, at=()):
        for key in config_menu.menu_keys(data, at):
            value, full = data[key], (*at, key)
            dotted = ".".join(full)
            leaf = not isinstance(value, dict) or config_menu.json_field(full)
            selected = not prefix or dotted == prefix or dotted.startswith(prefix + ".")
            if selected:
                entry = {"path": dotted, "type": _DESCRIBED_TYPES[type(value)] if leaf else "section",
                         "project_allowed": project_config.project_field(full) is not None}
                if leaf:
                    choices = config_menu.value_choices(full, data, tree)
                    if choices:
                        entry["choices"] = list(choices)
                    if config_menu.candidate_list(full):
                        entry["suggestions"] = {provider: list(models) for provider, models in BUNDLED_MODELS.items()}
                    hidden = _hidden(full)
                    layers = {layer: _lookup(values, full) for layer, values in
                              (("default", defaults), ("global", known), ("project", project))}
                    for layer, found in layers.items():
                        if found is not _MISSING:
                            entry[layer] = _masked(found, hidden)
                    layered = layers["project"] if layers["project"] is not _MISSING else layers["global"]
                    # Project identity has no Config field: the file sets it, or the session's --project.
                    underlying, effective = _lookup(unlaunched, full), _lookup(actual, full)
                    underlying = layered if underlying is _MISSING else underlying
                    effective = underlying if effective is _MISSING else effective
                    if full == ("tracker", "project") and launch.get("project"):
                        effective = launch["project"]
                    # A --tracker account is bound beneath the project overlay rather than applied as a flag.
                    # Compared in the loader's normalization, which strips strings a project file may pad.
                    bound = (full[:2] == ("tracker", "connections")
                             and underlying != (layered.strip() if isinstance(layered, str) else layered))
                    # Unknowable here: no layer carries it and the session renders no value of its own.
                    if layered is _MISSING and layers["default"] is not _MISSING and _lookup(resolved, full) is _MISSING:
                        entry["source"] = "host-only"
                    elif effective is _MISSING:
                        entry["source"] = "unset"
                    else:
                        entry["effective"] = _masked(effective, hidden)
                        if effective != underlying or bound:
                            entry["source"] = "session"
                        elif layers["project"] is not _MISSING:
                            entry["source"] = "project"
                        else:
                            entry["source"] = "default" if layered == layers["default"] else "global"
                entry["help"] = config_menu.help_for(full)
                fields.append(entry)
            if not leaf and (selected or prefix.startswith(dotted + ".")):
                walk(value, full)

    walk(tree)
    if not fields:
        raise AgentBoxError(f"no setting {prefix}; 'agent-box config describe' lists every path")
    result = {"source": source, "project_file": str(project_file)}
    if getattr(baseline, "_launch", None):
        result["session_options"] = describe_launch(baseline._launch)
    return {**result, "fields": fields}


def config_describe(prefix: str = "") -> None:
    """One setting per line, so an agent can read a section without paging a dump."""
    description = config_description(prefix)
    fields = description.pop("fields")
    print(json.dumps(description, ensure_ascii=False)[:-1] + ', "fields": [')
    print(",\n".join(json.dumps(field, ensure_ascii=False) for field in fields))
    print("]}")


CONFIG_SET_USAGE = "expected: agent-box config set --project|--global [--dry-run] PATH=JSON... [--reset PATH]..."
CONFIG_PROPOSAL_USAGE = "expected: agent-box config proposal [apply | discard]"


def _setting_path(text: str) -> tuple[str, ...]:
    path = tuple(text.split("."))
    if not all(path):
        die(f"config set: invalid setting path {text!r}; 'agent-box config describe' lists every path")
    return path


def config_set(args: list[str]) -> None:
    """``config set``: the checked, diffed write ``config ai`` agents use.

    Values are JSON, so a type is never guessed (``"off"`` against ``false``).
    The whole resulting file passes the project loader's validation before
    anything is written; no apply, PATH or user directory is touched.
    """
    import difflib
    from . import project_config
    project = global_scope = dry_run = False
    assignments, resets = [], []
    rest = list(args)
    while rest:
        arg = rest.pop(0)
        if arg == "--project":
            project = True
        elif arg == "--global":
            global_scope = True
        elif arg == "--dry-run":
            dry_run = True
        elif arg == "--reset":
            # Taking `--reset --dry-run` as a path would turn a preview into a write.
            if not rest or rest[0].startswith("-"):
                die(CONFIG_SET_USAGE)
            resets.append(_setting_path(rest.pop(0)))
        elif "=" in arg and not arg.startswith("-"):
            name, text = arg.split("=", 1)
            try:
                value = json.loads(text)
            except ValueError:
                # The value may be a secret typed into the wrong place; name the setting only.
                die(f'config set: the value for {name} must be JSON, for example true, 5, "text" or ["a"]')
            assignments.append((_setting_path(name), value))
        elif not arg.startswith("-"):
            # `PATH VALUE` is the usual slip; the stray word may be a secret, so it is not quoted back.
            die("config set: give each setting as one PATH=JSON argument, for example "
                "'workflow.language=\"English\"'; a value is never a separate argument")
        else:
            die(CONFIG_SET_USAGE)
    paths = [path for path, _ in assignments] + resets
    if project == global_scope or not paths:
        die(CONFIG_SET_USAGE)
    # A section and a field inside it would make the result depend on argument order.
    if any(a[:len(b)] == b for i, a in enumerate(paths) for j, b in enumerate(paths) if i != j):
        die("config set: each setting path may appear once, and not together with a section containing it")
    if global_scope:
        _propose_global(assignments, resets, dry_run)
        return
    file = project_config.project_path()
    before = project_config.load(file)
    after = project_config.changed(before, assignments, resets)
    # The refusal is about the proposal; the file on disk was not changed and holds no such value.
    project_config.validate(after, f"{file} (proposed change; file unchanged)")
    diff = "".join(difflib.unified_diff(
        (json.dumps(before, indent=2, ensure_ascii=False) + "\n").splitlines(keepends=True),
        (json.dumps(after, indent=2, ensure_ascii=False) + "\n").splitlines(keepends=True),
        f"{file} (current)", f"{file} (proposed)"))
    if not diff:
        print(f"No changes: {file}")
        return
    print(diff, end="")
    if dry_run:
        print("Dry run: nothing written")
        return
    project_config.save(file, after)
    print(f"Saved project configuration: {file}")


_MISSING = object()


def _lookup(data, path):
    for part in path:
        if not isinstance(data, dict) or part not in data:
            return _MISSING
        data = data[part]
    return data


def _checked_global(baseline: dict, proposal: dict):
    """The configuration a global proposal makes of ``baseline``, or an error naming the setting.

    Credentials, environment policy and installation stay with the menu, so an
    agent can neither read nor propose them. Each value must keep the
    baseline's type and the menu's choices, and the whole result must pass the
    menu's validation. Nothing here writes.
    """
    from . import config_menu, config_proposal
    from .config import Config
    defaults = Config().to_dict()
    for dotted in [*proposal["set"], *proposal["reset"]]:
        path = tuple(dotted.split("."))
        current = _lookup(baseline, path)
        if path == ("version",) or _hidden(path):
            raise AgentBoxError(f"config set --global: {dotted} is a credential, environment or installation "
                                "setting; change it in 'agent-box config'")
        if current is _MISSING or isinstance(current, dict) and not config_menu.json_field(path):
            raise AgentBoxError(f"config set --global: {dotted} is not a setting; 'agent-box config describe' lists every path")
        if dotted in proposal["reset"] and _lookup(defaults, path) is _MISSING:
            raise AgentBoxError(f"config set --global: {dotted} has no built-in value to reset to")
        if dotted in proposal["set"]:
            value = proposal["set"][dotted]
            if type(value) is not type(current) and not {type(value), type(current)} <= {int, float}:
                raise AgentBoxError(f"config set --global: {dotted} has an invalid type")
            if config_menu.candidate_list(path):
                # Loading keeps only object entries, so a malformed one would save as a silently shorter list.
                from .config import MODEL_ENTRY
                for index, item in enumerate(value):
                    if not isinstance(item, dict) or any(key not in MODEL_ENTRY or type(field) is not MODEL_ENTRY[key]
                                                         for key, field in item.items()):
                        raise AgentBoxError(f"config set --global: {dotted}.{index} must be an object with "
                                            f"{', '.join(MODEL_ENTRY)} fields of the right types")
            choices = config_menu.value_choices(path, _lookup(baseline, path[:-1]), baseline)
            if choices and value not in choices:
                raise AgentBoxError(f"config set --global: {dotted} must be one of: {', '.join(map(str, choices))}")
    config = Config.from_dict(config_proposal.applied(baseline, proposal))
    errors = config_menu.validate(config)
    if errors:
        raise AgentBoxError(f"config set --global: {errors[0]}")
    return config


def _change_lines(before: dict, after: dict, proposal: dict) -> list[str]:
    """Only the proposed settings: a whole-file diff of config.json would print its credentials."""
    lines = []
    for dotted in [*proposal["reset"], *proposal["set"]]:
        path = tuple(dotted.split("."))
        old, new = _lookup(before, path), _lookup(after, path)
        if old != new:
            lines += [f"- {dotted} = {json.dumps(old, ensure_ascii=False)}",
                      f"+ {dotted} = {json.dumps(new, ensure_ascii=False)}"]
    return lines


def _propose_global(assignments, resets, dry_run: bool) -> None:
    """Record global changes as a proposal; only ``config proposal apply`` on the host saves them.

    A box cannot write config.json, and a host write needs the user's own
    terminal confirmation, so both places propose instead of writing.
    """
    from . import config_proposal
    file = config_proposal.proposal_path()
    proposal = config_proposal.merged(config_proposal.load(file), assignments, resets)
    baseline = effective_config(include_project=False).to_dict()
    config = _checked_global(baseline, proposal)
    lines = _change_lines(baseline, config.to_dict(), proposal)
    if not lines:
        if not dry_run:
            config_proposal.discard(file)
        print("No changes to the global configuration")
        return
    where = ("this session's global snapshot" if effective_config_source()["kind"] == "runtime"
             else "the saved global configuration")
    print(f"Proposed global changes, checked against {where}:")
    print("\n".join(lines))
    if dry_run:
        print("Dry run: nothing written")
        return
    config_proposal.save(file, proposal)
    print(f"Saved global proposal: {file}")
    print("Nothing is applied until 'agent-box config proposal apply' runs on the host and the user confirms.")


def config_proposal_command(args: list[str]) -> None:
    """Show, apply after the user's terminal confirmation, or discard this checkout's global proposal."""
    from . import config_proposal
    if args not in ([], ["apply"], ["discard"]):
        die(CONFIG_PROPOSAL_USAGE)
    file = config_proposal.proposal_path()
    if not file.is_file():
        print(f"No pending global proposal: {file}")
        return
    if args == ["discard"]:
        config_proposal.discard(file)
        print(f"Discarded the global proposal: {file}")
        return
    # Any agent session, in a box or --host, reads a snapshot; consent belongs to the user's own terminal.
    session = effective_config_source()["kind"] == "runtime"
    if args == ["apply"] and session:
        die("config proposal apply runs in your own host terminal, outside agent sessions; nothing changed")
    proposal = config_proposal.load(file)
    # The host rechecks against the saved file: it may have changed since the proposal.
    baseline = (effective_config(include_project=False) if session else load_config()).to_dict()
    try:
        config = _checked_global(baseline, proposal)
    except AgentBoxError as exc:
        die(f"{exc}; nothing changed. The proposal is kept: change it with 'agent-box config set --global' "
            "or remove it with 'agent-box config proposal discard'")
    print(f"Global proposal {file}:")
    print("\n".join(_change_lines(baseline, config.to_dict(), proposal)) or "(no effect on the current global configuration)")
    if args != ["apply"]:
        print("Apply on the host with 'agent-box config proposal apply'; remove with 'agent-box config proposal discard'.")
        return
    if not sys.stdin.isatty():
        die("config proposal apply asks for confirmation in a terminal; nothing changed")
    try:
        answer = input("Save and apply these global changes? [y/N] ")
    except EOFError:
        print("\nNo answer; nothing changed")
        return
    if answer.strip().lower() not in ("y", "yes"):
        config_proposal.discard(file)
        print("Declined: the proposal was discarded and nothing changed")
        return
    save_config(config)
    from . import upgrades
    for line in upgrades.reconcile(force=True):
        print(line)
    config_proposal.discard(file)
    print("Saved and applied the global proposal")


CONFIG_AI_SKILL = "ab-config/SKILL.md"


def _inside_session() -> bool:
    """A box, or any agent session reading a snapshot: config ai would nest a session there."""
    return Path("/run/.containerenv").is_file() or effective_config_source()["kind"] == "runtime"


def _offer_global_proposal() -> None:
    """Hand a pending proposal to ``config proposal apply``; a refusal is reported, not fatal here."""
    from . import config_proposal
    try:
        if not config_proposal.proposal_path().is_file():
            return
    except AgentBoxError:
        return  # Outside Git there is nowhere a proposal could be.
    if not sys.stdin.isatty():
        print("agent-box: a global proposal is pending; run 'agent-box config proposal apply' in a terminal")
        return
    try:
        config_proposal_command(["apply"])
    except AgentBoxError as exc:
        print(f"agent-box: {exc}", file=sys.stderr)
    except SystemExit:
        pass  # die() already said why; the proposal stays for later.


def config_ai(args: list[str]) -> None:
    """``config ai [claude|codex]``: configure agent-box together with an agent.

    The session is an ordinary interactive box launch whose first prompt points
    at the shipped ab-config skill on the read-only launcher mount, so launching
    installs and applies nothing. The agent saves project changes through
    ``config set --project`` and can only propose global ones; this host command
    offers a pending proposal before the session and again when it ends or
    detaches, because the box itself can never apply it.
    """
    from . import runtime
    from .launch_options import LaunchOptions
    from .paths import CONTAINER_LAUNCHER_ROOT, MANAGED_SKILLS_SOURCE, PACKAGE_ROOT
    if len(args) > 1 or args and args[0] not in ("claude", "codex"):
        die("expected: agent-box config ai [claude|codex]")
    if _inside_session():
        die("config ai starts its own agent session; run it in your own host terminal. Inside this session, ask "
            f"the agent to read {MANAGED_SKILLS_SOURCE / CONFIG_AI_SKILL} and follow it")
    _offer_global_proposal()
    skill = CONTAINER_LAUNCHER_ROOT / "agent_box/skills" / CONFIG_AI_SKILL
    prompt = f"Read {skill} completely and follow it to configure agent-box for this project with me."
    try:
        runtime.runtime_main([*args, prompt], PACKAGE_ROOT, LaunchOptions())
        code = 0
    except SystemExit as exc:
        code = exc.code
    _offer_global_proposal()
    if code:
        raise SystemExit(code)


def config_show() -> None:
    config = effective_config()
    source = effective_config_source()
    print(f"Config source: {source['kind']} ({source['path']})")
    data = config.to_dict()
    data.pop("version")
    if source["kind"] == "runtime":
        print("Rendered container configuration; host-only settings are not included.")
        # A setting the snapshot omits would otherwise print as its default,
        # at any depth: `agents.claude` carries only remote_control and the status line command.
        data = _rendered_only(data, _snapshot_sections(source))
    if getattr(config, "_launch", None):
        from .launch_options import describe
        print(f"Session options: {describe(config._launch)}")
    print()
    notes = _show_notes(config)
    rows = list(_show_rows(data))
    width = max((2 * depth + len(key) for depth, key, value, _ in rows if value is not None), default=0) + 2
    for depth, key, value, at in rows:
        name = "  " * (depth + 1) + key
        note = notes.get(".".join(at), "")
        print(name + note if value is None else name.ljust(width + 2) + value + note)
    from . import runtime
    from .external_config import model_warnings
    for problem in runtime.k3s_warnings(config) + model_warnings(config):
        print(f"agent-box: warning: {problem}", file=sys.stderr)


def config_check() -> None:
    """Report on the official Claude Telegram channel, without changing anything."""
    config = load_config()
    enabled = config.agents.claude.telegram_channel
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


def config_interactive(project=False, *, baseline=None) -> None:
    if not project and session_snapshot_only():
        die("refusing to edit the saved configuration: this session reads the rendered "
            f"configuration {effective_config_source()['path']} and does not mount the file; "
            "run 'agent-box config' on the host")
    from . import config_menu
    from . import project_config
    path = project_config.project_path() if project else None
    result = (config_menu.edit(baseline or effective_config(include_project=False), project=project_config.load(path))
              if project else config_menu.edit(load_config()))
    if result is None:
        print("Configuration unchanged.")
        return
    if result.open_project:
        config_interactive(project=True, baseline=load_config())
        return
    if project:
        project_config.save(path, result.project)
        print(f"Saved project configuration: {path}")
        return
    config = result.config
    save_config(config)
    from . import upgrades
    for line in upgrades.reconcile(force=True):
        print(line)
    config_show()


def config_apply() -> None:
    """``agent-box config apply``: apply the saved file, asking nothing.

    The interactive flow ends by calling exactly this, so a configuration
    answered in the UI and one edited by hand produce the same host state.
    Run it after editing config.json by hand or to retry application. Installer
    upgrades refresh configured integrations on the next host launch.

    Inside a workspace box the saved file is not mounted, so the command
    applies what the rendered session snapshot describes -- the shipped
    skills and the configured agent profiles -- and leaves the host-only
    sections alone; see :func:`agent_box.runtime.apply_config`.
    """
    from . import runtime, upgrades
    session = session_snapshot_only()
    config = effective_config() if session else load_config()
    if session:
        print(f"agent-box: applying the rendered session configuration "
              f"{effective_config_source()['path']}: skills and profiles only; "
              "notifications, hooks, status line, credentials and Claude "
              "links are host-only and untouched")
    for line in (runtime.apply_config(config, session=True) if session else upgrades.reconcile(force=True)):
        print(f"agent-box: {line}")
    from .external_config import model_warnings
    for problem in (runtime.tracker_warnings(config) + runtime.audio_warnings(config)
                    + runtime.k3s_warnings(config) + model_warnings(config)):
        print(f"agent-box: warning: {problem}", file=sys.stderr)


def config_command(args: list[str]) -> None:
    if not args:
        config_interactive()
    elif args == ["--project"]:
        config_interactive(project=True)
    elif args == ["show"]:
        config_show()
    elif args[:1] == ["describe"] and len(args) <= 2:
        config_describe(*args[1:])
    elif args[:1] == ["set"]:
        config_set(args[1:])
    elif args[:1] == ["proposal"]:
        config_proposal_command(args[1:])
    elif args[:1] == ["ai"]:
        config_ai(args[1:])
    elif args == ["check"]:
        config_check()
    elif args == ["apply"]:
        config_apply()
    else:
        die("expected: agent-box config [--project|show|describe [PATH]|set --project|--global ...|proposal|ai|check|apply]")


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
    "build", "update", "rebuild", "tracker", "workflow", "attach", "stop", "uninstall",
    "completion", "links", "workspaces",
}
#: --host only makes sense for a real agent launch, never for these.
HOST_ONLY_AGENTS = {"claude", "codex"}


def unknown_command(args: list[str], known, launch: str) -> str:
    """Why ``args`` cannot start: an empty string when its first word is known or an option.

    A word that names no command used to reach the default agent as its prompt,
    so a typo started a paid session with the typo as the request. Prompts now
    need the agent named, and only options still reach the default agent.
    """
    if not args or args[0].startswith("-") or args[0] in known:
        return ""
    import difflib
    close = difflib.get_close_matches(args[0], sorted(known), n=1)
    hint = f"; did you mean '{close[0]}'?" if close else ""
    return (f"unknown command '{args[0]}'{hint}\n"
            f"A prompt needs the agent named: {launch} claude|codex PROMPT; see agent-box help")


def _agent_and_rest(args: list[str]) -> tuple[str | None, list[str]]:
    """An explicit claude/codex, or None so the launch resolves the default agent."""
    return (args[0], args[1:]) if args and args[0] in HOST_ONLY_AGENTS else (None, args)


def main() -> None:
    if sys.argv[1:2] == ["_browser"]:
        from . import browser
        raise SystemExit(browser.main(sys.argv[2:]))
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
        options, args = launch_options.parse(sys.argv[1:])
        if args[:2] == ["workflow", "sync"]:
            from .provider_runner import require_sync_coordinator
            require_sync_coordinator()
        project_config.load()
    except AgentBoxError as exc:
        die(str(exc))
    from .paths import PACKAGE_ROOT
    script_dir = PACKAGE_ROOT
    try:
        from .completion import COMMANDS, HIDDEN
        refused = unknown_command(args, COMMANDS.keys() | HIDDEN, "agent-box")
        if refused:
            die(refused)
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
        if args and args[0] == "links":
            from . import links
            raise SystemExit(links.main(args[1:]))
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
            from . import host_podman, runtime
            host_podman.require()
            command = args[0]
            raise SystemExit(runtime.build_image(script_dir, command != "build", command == "rebuild"))
        if args and args[0] == "tracker":
            from .tracker import cli as tracker_cli
            raise SystemExit(tracker_cli.main(args[1:]))
        if args and args[0] == "workflow":
            from . import workflow
            raise SystemExit(workflow.main(args[1:]))
        if args and args[0] == "uninstall":
            from . import runtime
            raise SystemExit(runtime.uninstall_main(args[1:]))
        if args and args[0] == "workspaces":
            from . import workspaces
            raise SystemExit(workspaces.main(args[1:]))
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
            refused = unknown_command(rest, HOST_ONLY_AGENTS, f"agent-box ext {name}")
            if refused:
                die(refused)
            from .upgrades import before_launch
            before_launch()
            if options.host:
                raise SystemExit(runtime.host_main(*_agent_and_rest(rest), options, api_provider=name))
            runtime.runtime_main(rest, script_dir, options, api_provider=name)
            return
        if options.host:
            if args and args[0] in {"shell", "doctor"}:
                die("--host runs claude or codex; shell/doctor are container-only")
            from . import runtime
            from .upgrades import before_launch
            before_launch()
            raise SystemExit(runtime.host_main(*_agent_and_rest(args), options))
        from . import runtime
        if args[:1] != ["doctor"]:
            from .upgrades import before_launch
            before_launch()
        runtime.runtime_main(args, script_dir, options)
    except AgentBoxError as exc:
        die(str(exc))
    except (KeyboardInterrupt, EOFError):
        raise SystemExit(130)
