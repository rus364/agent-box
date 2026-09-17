"""A dependency-free terminal form; no writes occur until Save and apply."""
from __future__ import annotations

import copy
import json
import sys
import textwrap
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .config import AGENT_EFFORTS, AGENT_PROVIDERS, IMAGE_TOOL_GROUP_HELP, LAUNCH_AGENTS, Config, EnvConfig, REVIEW_INCREMENTAL, REVIEW_PRESETS, TRACKER_PROVIDERS, TrackerConnection, candidate_label, model_entry
from .errors import AgentBoxError
from .external_config import API_EFFORTS, ExternalProvider, model_defaults, model_settings, valid_model, valid_name, validate_connections
from .model_catalog import load_model_catalogs

CHOICES = {
    "default_agent": LAUNCH_AGENTS,
    "tracker.provider": TRACKER_PROVIDERS,
    "workflow.merge_mode": ("github", "local"),
    "workflow.review.policy": ("off", "auto", "always"),
    "workflow.review.incremental": REVIEW_INCREMENTAL,
    "workflow.review.github_provider": ("auto", "local", "codex", "claude"),
}
HELP = {
    "default_agent": "Agent started by `agent-box` without claude/codex; an explicit claude or codex always wins. Global only; not a project setting.",
    "install": "Where agent-box keeps its PATH symlink. Global only; set through the menu's PATH integration.",
    "install.bin_dir": "Directory holding the agent-box PATH symlink; empty means agent-box manages none. `agent-box config apply` restores the link here.",
    "claude": "Claude session settings, including --host, and its status line. Global only.",
    "claude.telegram_channel": "Start Claude with Anthropic's official Telegram channel plugin. This requires its host-side setup and exposes the session through that channel.",
    "claude.remote_control": "Request Remote Control for new interactive Claude sessions, including --host. Off preserves native behavior. Resume and explicit settings use the ordinary launch. Requires eligible Claude login.",
    "claude.statusline": "Claude's status line and its extra usage segment. Global only; changes apply after Save and apply.",
    "claude.statusline.enabled": "Keep Claude's status line pointed at agent-box. An existing user-owned status line is never overwritten.",
    "claude.statusline.command": "Extra segment in Claude's status line and `agent-box usage all`: shell command whose first output line is shown after Codex usage; empty shows nothing. Runs where the line is drawn (Claude's status line runs inside the box), cached 60s, 5s timeout, failures show —. Pass keys via environment, not in the command.",
    "codex": "Codex interactive session settings, including --host.",
    "codex.remote_control": "Request Remote Control through a private app-server; one agent-box RC session per CODEX_HOME. Unsupported launches keep the ordinary CLI. Off leaves native behavior unchanged.",
    "notifications": "Outbound Telegram completion notifications. The bot token and chat ID stay disabled until this section is enabled and fully configured.",
    "notifications.enabled": "Send outbound completion notifications through Telegram when a bot token and chat ID are configured.",
    "notifications.telegram_token": "Telegram bot token used only for outbound notifications. The value is masked in the menu and saved in the protected config file.",
    "notifications.telegram_chat_id": "Telegram destination chat ID for outbound notifications. It has no effect while notifications are disabled.",
    "github": "Choose which host GitHub credentials, if any, are exposed inside workspace boxes.",
    "github.gh_token": "Read the host gh token and forward it as GH_TOKEN. Disabled by default because it exposes a live credential to the box.",
    "github.gh_config": "Copy the host gh configuration into the box. Disabled by default because that directory normally contains authentication data.",
    "git": "Configure how much host Git configuration is copied into workspace boxes.",
    "git.identity": "Copy only user.name and user.email for commit attribution. This exposes no Git credential settings.",
    "git.full_config": "Copy the complete host Git configuration, which may include credential helpers, headers, URL rewrites, and included files.",
    "external_providers": "Named external APIs. Enable explicitly to expose their keys to agent-box sessions. Each CLI has its own URL and model list; `ext NAME` without claude/codex uses the default agent.",
    "providers": "Control which provider-related environment variables are forwarded from the host.",
    "providers.api_keys": "Forward known Claude and Codex API-key variables by name. Disabled by default because these values are credentials.",
    "providers.transport": "Forward provider endpoint, proxy, CA, Bedrock, and Vertex transport variables. Proxy URLs may themselves contain credentials.",
    "env": "Additional per-launch environment policy. Forward secrets by variable name; literal values are stored in config and exposed in process arguments.",
    "env.forward": "JSON list of host variable names to forward without putting their values in process arguments. Use this form for secrets.",
    "env.set": "JSON object of literal environment values. These values are stored in config and appear in Podman arguments, so do not put secrets here.",
    "skills": "Configure agent-box-owned skill integration with host agent clients.",
    "skills.claude_links": "Link agent-box's managed shared skills into Claude's skill directory when applying configuration.",
    "image": "Select tool groups built into the workspace image. A changed selection takes effect after the image is rebuilt.",
    "image.tools": "Tool groups baked into the workspace image. Each group changes box identity and may make the image larger.",
    "tracker": "Expose configured task-tracker connections to agents. Connections remain unavailable while this integration is disabled.",
    "tracker.enabled": "Expose configured tracker connections to agents. Keep credentials out of repository files.",
    "tracker.provider": "Default tracker for repositories without a more specific binding. Choose one of the supported adapters.",
    "tracker.project": "Project tracker identity: the tracker project ID this repository's tasks belong to. Project setting only; setup and adopt write it.",
    "tracker.recursive": "Project tracker identity: include every project under the bound project in the workflow. Project setting only.",
    "tracker.connections": "Provider URLs and tokens rendered into minimal runtime credential files only when tracker integration is enabled.",
    "browser": "Configure the agent-controlled headless browser inside the workspace box.",
    "browser.enabled": "Headless Chromium in the box; isolated in-memory profile, same trust boundary as agents. Unavailable in --host mode.",
    "audio": "Configure access to the host's sound server from workspace boxes.",
    "audio.enabled": "Mount the host PulseAudio/PipeWire socket so agents can use the microphone and speakers, e.g. Claude Code /voice. Everything in the box gets both. Changing it recreates the box.",
    "workflow": "Opt-in tracked-work orchestration, role models, review, publication, and cleanup policy.",
    "workflow.enabled": "Enable agent-box's tracked-task workflow. Enabling it does not itself expose provider or tracker credentials.",
    "workflow.use_tiers": "Let a task's tier 1-3 select the worker and reviewer Models by tier. Off runs every task on the role's own Models; tier lists and task tiers are kept. A project or --[no-]tiers may override it.",
    "workflow.merge_mode": "Finish parent work through a GitHub pull request or a local fast-forward into origin's default branch.",
    "workflow.scout": "Read-only scouts the coordinator dispatches for bounded questions, and the models they run.",
    "workflow.scout.limit": "Maximum number of concurrent read-only scouts, from 1 through 16.",
    "workflow.scout.models": "Scout candidates in preference order; the first available one runs and the rest are fallbacks. Scouts have no tiers. A candidate of the other CLI than the coordinator's runs as a managed CLI process.",
    "workflow.auto_push": "Within the opt-in workflow, publish the parent task in GitHub mode or the accepted base after a local fast-forward. Turning this off keeps completion local.",
    "workflow.timeout_seconds": "Maximum delegated agent runtime in seconds, from 30 through 86400.",
    "workflow.worker": "The models managed workers run when the coordinator delegates with workflow delegate.",
    "workflow.worker.models": "Delegated worker candidates in preference order; the first available one runs and the rest are fallbacks. Used unless Use tiers is on and the task's tier has its own list. workflow delegate runs them as a managed CLI process and refuses when none applies; the coordinator can spawn a native worker instead.",
    "workflow.worker.tiers": "Optional worker candidates per task tier 1-3, used only when Use tiers is on. A nonempty tier list replaces Models for that tier; an empty one uses Models.",
    "workflow.review": "Review thresholds, attempts, reviewer candidates and GitHub review.",
    "workflow.review.models": "Reviewer candidates in preference order; the first available one runs, skipping a missing CLI or an exhausted usage limit, and the rest are fallbacks. Used unless Use tiers is on and the task's tier has its own list; with no candidates a review inherits the active session.",
    "workflow.review.tiers": "Optional reviewer candidates per task tier 1-3, used only when Use tiers is on. A nonempty tier list replaces Models for that tier; an empty one uses Models.",
    "workflow.review.policy": "Off skips workflow review. Auto triggers on file/line thresholds, sensitive paths, or the review label. Always requires review for every task and the complete parent diff. GitHub merge may still require independent approval.",
    "workflow.review.max_files": "Automatic-review threshold for changed file count. The Review preset action can set this together with max lines.",
    "workflow.review.max_lines": "Automatic-review threshold for changed line count. The Review preset action can set this together with max files.",
    "workflow.review.max_rounds": "Maximum local review attempts, including failed or abandoned attempts, before explicit user allowance is required.",
    "workflow.review.sensitive_paths": "JSON list of glob patterns that always trigger automatic review regardless of size thresholds, e.g. [\"**/auth/**\", \".github/**\"]. Paths are repository-relative and * also matches /.",
    "workflow.review.ignored_paths": "JSON list of glob patterns excluded from automatic-review file/line counts, e.g. [\"**/*.md\", \"docs/**\"]. Same matching as sensitive paths, which still win. The reviewer still sees the whole diff.",
    "workflow.review.incremental": "After an applied review round, decide whether new commits need another round from their delta to that round's head instead of the whole task diff. Off uses the whole diff. A level accepts a NEEDS_WORK round whose findings are all at or below it; a PASS always counts. The delta uses the same triggers and records no PASS.",
    "workflow.review.label": "Tracker label that forces review for a task when the review policy is automatic.",
    "workflow.review.github_provider": "Provider requested for GitHub PR review: auto/local uses local review, Codex cannot pin model or effort, and Claude uses its configured Action.",
    "workflow.review.github_model": "Model expected in a Claude GitHub Action. Codex GitHub review rejects model overrides; leave empty unless the Claude Action pins it.",
    "workflow.review.github_effort": "Effort expected in a Claude GitHub Action. Codex GitHub review rejects effort overrides; leave empty unless the Claude Action pins it.",
    "workflow.review.wait_seconds": "How long a cloud review round waits for the bot's answer in the pull request, in seconds (default 3600). Past it the round ends as a failure and the user decides; silence is never approval. Must be a positive integer.",
    "workflow.cleanup_local_branches": "Delete a proven merged local parent branch after Done. Checked-out and unproven branches are preserved.",
    "workflow.cleanup_remote_branches": "Delete a proven merged parent branch on origin with an exact head lease. Disabled by default.",
}
ACTION_HELP = {
    "path": "Choose whether agent-box maintains a safe PATH symlink. Nothing is changed until Save and apply.",
    "save": "Validate the draft, save it, and apply selected host integrations. This is the only menu action that writes changes.",
    "cancel": "Discard the complete draft and leave configuration and host integration unchanged.",
    "preset": "Set file and line thresholds together from a named review preset; all review fields remain editable.",
}
LABELS = {
    "default_agent": "Default agent", "statusline": "Status line",
    "claude": "Claude", "codex": "Codex", "github": "GitHub", "git": "Git",
    "command": "Custom command",
    "env": "Environment", "providers": "Provider environment", "image": "Workspace image",
    "url": "URL", "token": "Access token", "effort": "Reasoning effort",
    "github_model": "GitHub review model", "github_effort": "GitHub review effort",
    "telegram_chat_id": "Telegram chat ID",
    "worker": "Workers", "scout": "Scouts", "use_tiers": "Use tiers",
    "github_provider": "GitHub review provider",
    "external_providers": "External API providers", "api_provider": "API provider", "api_key": "API key",
}

# Presentation follows the user's decisions, not dataclass serialization order.
MENU_GROUPS = {
    (): (
        ("Agents", ("default_agent", "claude", "codex", "external_providers")),
        ("Tracked work", ("tracker", "workflow")),
        ("Workspace box", ("image", "browser", "audio")),
        ("From host into the box", ("git", "github", "providers", "env")),
        ("On the host", ("skills", "notifications")),
    ),
    ("workflow",): (
        ("Workflow", ("enabled",)),
        ("Delivery", ("merge_mode", "auto_push", "cleanup_local_branches", "cleanup_remote_branches")),
        ("Roles", ("use_tiers", "worker", "scout", "review")),
        ("Delegation", ("timeout_seconds",)),
    ),
    ("workflow", "worker"): (
        ("Models", ("models", "tiers")),
    ),
    ("workflow", "scout"): (
        ("Concurrency", ("limit",)),
        ("Models", ("models",)),
    ),
    ("workflow", "review"): (
        ("Review triggers", ("policy", "max_files", "max_lines", "sensitive_paths", "ignored_paths", "label", "incremental")),
        ("Models", ("models", "tiers")),
        ("Review attempts", ("max_rounds",)),
        ("GitHub review", ("github_provider", "github_model", "github_effort", "wait_seconds")),
    ),
}


def menu_keys(data, path=()):
    ordered = [key for _, keys in MENU_GROUPS.get(path, ()) for key in keys if key in data]
    return ordered + [key for key in data if key not in ordered]


def character_width(character):
    if unicodedata.combining(character):
        return 0
    return 2 if unicodedata.east_asian_width(character) in ("W", "F") else 1


def display_width(text):
    return sum(character_width(character) for character in text)


def column_prefix(text, width):
    """Keep whole wide characters and the combining marks on their last base."""
    used = 0
    for index, character in enumerate(text):
        used += character_width(character)
        if used > width:
            return text[:index]
    return text


def padded(text, width):
    return text + " " * max(0, width - display_width(text))


def clipped(text, width):
    """Reserve terminal columns, not code points, before adding an ellipsis."""
    return text if display_width(text) <= width else column_prefix(text, max(0, width - 3)) + "." * min(3, width)


def label_for(key, path=()):
    if path == ('external_providers',) or path[:1] == ('external_providers',) and path[-1:] == ('models',):
        return key
    if len(path) == 2 and path[0] == "workflow" and key == "tiers":
        return "Models by tier"
    return LABELS.get(key, f"Tier {key}" if key.isdigit() else key.replace("_", " ").capitalize()).replace('Api', 'API')


def json_field(path):
    return (path == ('env', 'set') or path[:1] == ('external_providers',)
            and (len(path) == 4 and path[-1] in ('env', 'provider_options', 'config')
                 or len(path) == 6 and path[3] == 'models' and path[-1] in ('env', 'metadata')))


def help_for(path):
    """Return complete help for every static and generated menu entry."""
    dotted = ".".join(path)
    if path[0] == "workflow" and path[-1] == "fast":
        return ("Request faster responses at higher provider usage/cost. Off preserves provider/native defaults. Independent of effort. "
                "On requires a supported exact model ID and account access. Claude native needs verified inherited fast; otherwise use managed CLI.")
    if dotted in HELP:
        return HELP[dotted]
    if path[:1] == ("external_providers",):
        # Provider names and model IDs are the user's own; every fixed field below needs its own help.
        if len(path) == 2:
            return HELP["external_providers"]
        if len(path) == 3 and path[-1] in LAUNCH_AGENTS:
            return f"{path[-1].title()} endpoint of this API: its own URL, models and environment, used only when {path[-1]} runs against it."
        if len(path) == 5 and path[3] == "models":
            return "Settings for this model ID. Workflow candidates select it through the model picker; the ID sent to the API is the key itself."
        text = {"enabled": "Expose this API connection to agent-box sessions. Off by default.",
                "api_key": "Stored in the protected global config; masked in displays and excluded from command arguments.",
                "url": "HTTP(S) API base URL for this CLI, without credentials, query or fragment. Codex requires Responses API; Claude requires Anthropic Messages API.",
                "models": "Model settings keyed by ID. Add a model, edit its fields, or select a default. Manual workflow IDs remain available.",
                "default_model": "Configured model used without --model; empty uses the first entry. Explicit selection is stable across file ordering.",
                "display_name": "Optional model picker label; the model ID sent to the API stays unchanged.",
                "effort": "Model reasoning default; default leaves the CLI/provider choice. Explicit CLI/workflow effort takes priority.",
                "supported_efforts": "JSON array of supported effort names. Empty means unknown; declared levels constrain explicit requests.",
                "context_window": "Tokens; 0 leaves the CLI default. Claude accepts 200000 or 1048576; 1048576 adds [1m] to its launch model.",
                "auto_compact_token_limit": "Compaction threshold in tokens; 0 leaves the CLI default. Must be below a configured context window.",
                "input_modalities": "Codex input types: JSON array containing text and/or image.",
                "supports_parallel_tool_calls": "Advertise parallel tool calls in the Codex model catalog.",
                "supports_reasoning_summaries": "Advertise reasoning summaries in the Codex model catalog.",
                "metadata": "Additional native Codex model metadata as JSON. Typed model fields cannot be overridden here; fields must be supported by your CLI.",
                "provider_options": "Native Codex model-provider JSON: query_params, http_headers, env_http_headers, retries, timeouts, transport. URL/auth fields belong to agent-box. Header values travel through environment variables; use non-secret query parameters.",
                "config": "Native Codex session JSON, for example {\"web_search\":\"live\"}. CLI overrides win; model effort defaults apply next. API/auth/catalog/Remote Control fields belong to agent-box. Use env or header settings for secrets, never literal secrets here.",
                "env": "JSON object of per-process environment strings. Model values override endpoint values; typed effort/compact fields and explicit effort take priority. API/auth/RC ownership cannot be overridden. Values are masked and never placed in argv."}.get(path[-1])
        if text:
            return text
    if len(path) == 3 and path[:2] == ("image", "tools"):
        return f"{IMAGE_TOOL_GROUP_HELP.get(path[-1], path[-1])}. Rebuild the workspace image to install or remove this group; browsing and saving this selection do not rebuild it."
    if len(path) == 3 and path[:2] == ("tracker", "connections"):
        return f"Connection settings for the {path[-1]} tracker. It is exposed only when tracker integration is enabled."
    if len(path) == 4 and path[:2] == ("tracker", "connections"):
        if path[-1] == "url":
            return f"Base URL for the {path[-2]} tracker API. No network request is made while editing."
        return f"Access token for the {path[-2]} tracker. It is masked here and rendered only for enabled tracker integration."
    if candidate_list(path) and len(path) == 4:
        role = "reviewer" if path[1] == "review" else path[1]
        return (f"{role.title()} candidates for tier {path[-1]} in preference order, used only when Use tiers is on. Nonempty replaces the role's Models for this tier; "
                "empty uses them. Enter edits the list: add, reorder, change or remove candidates.")
    raise KeyError(f"No menu help for {dotted}")


def validate(config):
    errors = config.workflow.validate() + validate_connections(config)
    if config.claude.telegram_channel:
        from .channel import missing_requirements
        errors.extend(missing_requirements())
    return errors


@dataclass
class EditResult:
    """The accepted configuration plus the one-off shell startup edit.

    The symlink directory is stored in configuration so ``config apply`` can
    restore it. Appending to a shell startup file happens only for the answer
    given in this edit and is never repeated behind the user's back.
    """
    config: Config
    edit_rc: bool = False
    project: dict | None = None


def secret(path):
    return ("token" in path[-1] and path[-1] != 'auto_compact_token_limit'
            or path[-1] == "api_key"
            or json_field(path) and path[-1] in ('env', 'provider_options'))


def parse_value(text, current):
    if isinstance(current, (list, dict)):
        value = json.loads(text)
        if not isinstance(value, type(current)):
            raise ValueError(f"Expected a JSON {type(current).__name__}")
        return value
    if isinstance(current, int):
        return int(text)
    if isinstance(current, float):
        return float(text)
    return text


def env_entries(name, value):
    """Refuse in the editor what saving would silently drop.

    Saving normalizes through ``EnvConfig.from_dict``, which discards
    malformed names so they never reach Podman; accepting them here reported
    a draft change that was then lost without a word.
    """
    kept = getattr(EnvConfig.from_dict({name: value}), name)
    dropped = [entry for entry in value if entry not in kept]
    if dropped:
        raise ValueError("Not saved: " + ", ".join(json.dumps(entry, ensure_ascii=False) for entry in dropped)
                         + "; names use letters, digits and _ and do not start with a digit; values are text")
    return kept


def candidate_list(path):
    """A role's ordered candidate list: ``workflow.ROLE.models`` or ``workflow.ROLE.tiers.N``."""
    return (len(path) >= 3 and path[0] == "workflow" and path[1] in ("scout", "worker", "review")
            and (len(path) == 3 and path[2] == "models" or len(path) == 4 and path[2] == "tiers" and path[1] != "scout"))


def candidate_efforts(spec, tree):
    """Efforts a candidate may request: its CLI's, narrowed by a configured API model's declaration."""
    provider, api, value = spec["provider"], spec.get("api_provider"), spec["effort"]
    if not api:
        return tuple(dict.fromkeys([*AGENT_EFFORTS.get(provider, ()), value]))
    models = tree["external_providers"].get(api, {}).get(provider, {}).get("models", {})
    model = model_settings(models, spec["model"], provider)
    return tuple(dict.fromkeys(["default", *(model.get("supported_efforts") or API_EFFORTS[provider][1:]), value]))


def value_choices(path, section, tree):
    """The values the menu offers for ``path``, or None for free input; `config describe` reports the same."""
    name, value = path[-1], section[path[-1]]
    options = CHOICES.get(".".join(path))
    if path[:1] == ('external_providers',):
        if len(path) == 6 and name == 'effort':
            options = tuple(dict.fromkeys(['default', *(section['supported_efforts'] or API_EFFORTS[path[2]][1:]), value]))
        elif len(path) == 4 and name == 'default_model':
            options = ['', *section['models']]
    return options


def edit(config, *, project=None):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise AgentBoxError("agent-box config needs a terminal; edit config.json and run config apply for noninteractive use")
    try:
        import curses
    except ImportError:
        raise AgentBoxError("This Python build lacks stdlib curses; edit config.json and run config apply") from None
    from . import pathsetup
    from .project_config import project_field, merge, validate as validate_project
    project_mode = project is not None
    overrides = copy.deepcopy(project) if project_mode else None
    choices, link = ([], None) if project_mode else (pathsetup.bin_dir_choices(), pathsetup.link_state())
    model_suggestions = load_model_catalogs()
    inherited = copy.deepcopy(config.to_dict())
    if project_mode and hasattr(config, "_global_tracker_urls"):
        # This is an editor-only baseline, not an account for tracker requests.
        inherited["tracker"]["connections"] = {
            provider: TrackerConnection(url).to_dict()
            for provider, url in config._global_tracker_urls.items()}
    global_values = copy.deepcopy(inherited)
    draft = merge(inherited, overrides) if project_mode else copy.deepcopy(inherited)
    draft.pop("version", None)
    draft.pop("install", None)
    for provider in TRACKER_PROVIDERS:
        inherited["tracker"]["connections"].setdefault(provider, TrackerConnection().to_dict())
        draft["tracker"]["connections"].setdefault(provider, TrackerConnection().to_dict())
    if project_mode:
        for target in (inherited, draft):
            target["tracker"].setdefault("project", "")
            target["tracker"].setdefault("recursive", False)
    path_state = {"bin": Path(config.install.bin_dir) if config.install.bin_dir else None, "rc": False}

    def explicit(path):
        value = overrides
        for part in path:
            if not isinstance(value, dict) or part not in value:
                return False
            value = value[part]
        return True

    def record(path, value):
        target = overrides
        for part in path[:-1]:
            target = target.setdefault(part, {})
        target[path[-1]] = copy.deepcopy(value)

    def remove_override(path):
        def remove(target, parts):
            if parts[0] in target:
                if len(parts) == 1:
                    del target[parts[0]]
                else:
                    remove(target[parts[0]], parts[1:])
                    if not target[parts[0]]:
                        del target[parts[0]]
        remove(overrides, path)
        value = inherited
        for part in path:
            value = value.get(part, {})
        return copy.deepcopy(value)

    defaults = Config().to_dict()
    for provider in TRACKER_PROVIDERS:
        defaults['tracker']['connections'][provider] = TrackerConnection().to_dict()

    def default_value(path, source):
        value = source
        for part in path:
            if part not in value:
                # Connection rows are editable defaults, but a section copy
                # must not create accounts that the global map does not hold.
                if path[:1] == ('external_providers',) and len(path) >= 2:
                    model_path = len(path) >= 5 and path[3] == 'models'
                    value = model_defaults(path[2]) if model_path else ExternalProvider().to_dict()
                    for part in path[5:] if model_path else path[2:]:
                        value = value[part]
                    break
                if path[:2] == ('tracker', 'connections') and len(path) >= 3:
                    value = TrackerConnection().to_dict()
                    for part in path[3:]:
                        value = value[part]
                    break
                return None
            value = value[part]
        if project_mode and isinstance(value, dict):
            def allowed(value, parts):
                return {key: allowed(item, (*parts, key)) if isinstance(item, dict) else copy.deepcopy(item)
                        for key, item in value.items() if project_field((*parts, key)) is not None}
            value = allowed(value, path)
        return copy.deepcopy(value)

    def drafted():
        result = Config.from_dict({**draft, "install": {"bin_dir": str(path_state["bin"]) if path_state["bin"] else ""}})
        # Empty connection rows exist for editing, not as configured accounts.
        result.tracker.connections = {provider: connection for provider, connection in result.tracker.connections.items()
                                      if connection != TrackerConnection()}
        return result

    global_only_color = 0

    def add_line(screen, y, value, width, attr=0, x=0):
        try:
            visible = column_prefix(value, max(1, width - 1))
            screen.addnstr(y, x, value, len(visible), attr)
        except curses.error:
            # A resize can arrive between measuring the screen and drawing it.
            pass

    def paint(screen, rows, selected, title, help_text="", notice="", controls=None, group=""):
        screen.erase()
        height, width = screen.getmaxyx()
        if height < 8 or width < 40:
            add_line(screen, 0, "agent-box config", width, curses.A_BOLD)
            if height > 1:
                add_line(screen, 1, "Resize terminal to at least 40 columns x 8 rows (Esc: back)", width)
            screen.refresh()
            return False
        controls = controls or "Enter open  Space set  ? help  Esc back"
        sidebar = width >= 100 and height >= 14
        help_height = min(5, max(2, height // 5))
        list_width = max(46, width // 2) if sidebar else width
        separator = height - 2 if sidebar else height - help_height - 2
        list_height = max(1, separator - 3)
        count = f"{selected + 1}/{len(rows)}"
        title_width = width - len(count) - 3
        if len(title) > title_width:
            title = "... " + title[-(title_width - 4):]
        add_line(screen, 0, title, title_width, curses.A_BOLD)
        add_line(screen, 0, count, len(count) + 1, x=width - len(count) - 1)
        add_line(screen, 1, "Up/Down: move  Home/End  PgUp/PgDn", width)
        add_line(screen, 2, group, list_width, curses.A_BOLD)
        label_width = min(max((display_width(row[0]) for row in rows if isinstance(row, tuple)), default=0),
                          32, list_width // 2 - 2)
        offset = min(max(0, selected - list_height + 1), max(0, len(rows) - list_height))
        for y, (index, row) in enumerate(list(enumerate(rows))[offset:offset + list_height], 3):
            attr = curses.A_REVERSE | curses.A_BOLD if index == selected else 0
            if "Global only" in (row[1] if isinstance(row, tuple) else row):
                attr = global_only_color | (curses.A_BOLD | curses.A_UNDERLINE if index == selected else 0)
            if isinstance(row, tuple):
                label, value = row
                row = padded(clipped(label, label_width), label_width) + ": " + clipped(value, list_width - label_width - 3)
            add_line(screen, y, row, list_width, attr)
        message = help_text
        if notice:
            message = notice + "\n\n" + help_text
        if sidebar:
            for y in range(2, height - 2):
                add_line(screen, y, "|", 2, x=list_width)
            help_x, help_y, help_width = list_width + 3, 3, width - list_width - 3
            add_line(screen, help_y, "About this setting", help_width, curses.A_BOLD, help_x)
            help_y += 2
            help_height = height - help_y - 2
        else:
            help_x, help_y, help_width = 0, separator + 1, width
            add_line(screen, separator, "-" * (width - 1), width)
        wrapped = []
        for paragraph in message.split("\n"):
            wrapped.extend(textwrap.wrap(paragraph, help_width - 1,
                           break_long_words=True, break_on_hyphens=False) or [""])
        if len(wrapped) > help_height:
            wrapped = wrapped[:help_height]
            wrapped[-1] = ("F1: full help" if "F1" in controls else "?: full explanation")
        for index, line in enumerate(wrapped):
            add_line(screen, help_y + index, line, help_width, x=help_x)
        add_line(screen, height - 1, controls, width, curses.A_BOLD)
        screen.refresh()
        return True

    def moved(key, selected, count, screen):
        height, width = screen.getmaxyx()
        page = max(1, height - (5 if width >= 100 and height >= 14 else min(5, max(2, height // 5)) + 5))
        if key == curses.KEY_UP:
            return (selected - 1) % count
        if key == curses.KEY_DOWN:
            return (selected + 1) % count
        if key == curses.KEY_HOME:
            return 0
        if key == curses.KEY_END:
            return count - 1
        if key == curses.KEY_PPAGE:
            return max(0, selected - page)
        if key == curses.KEY_NPAGE:
            return min(count - 1, selected + page)
        return None

    def full_help(screen, title, content):
        offset = 0
        while True:
            screen.erase()
            height, width = screen.getmaxyx()
            if height < 8 or width < 40:
                add_line(screen, 0, "Help", width, curses.A_BOLD)
                if height > 1:
                    add_line(screen, 1, "Resize terminal to at least 40 columns x 8 rows (Esc: back)", width)
                lines, page = [], 1
            else:
                lines = textwrap.wrap(content, width - 1, break_long_words=True, break_on_hyphens=False) or [""]
                page = max(1, height - 3)
                offset = min(offset, max(0, len(lines) - page))
                add_line(screen, 0, title, width, curses.A_BOLD)
                add_line(screen, 1, "Up/Down PgUp/PgDn: scroll  Esc: back", width)
                for index, line in enumerate(lines[offset:offset + page]):
                    add_line(screen, index + 2, line, width)
            screen.refresh()
            key = screen.get_wch()
            if key in ("\x1b", "\n", "\r"):
                return
            if key == curses.KEY_UP:
                offset = max(0, offset - 1)
            elif key == curses.KEY_DOWN:
                offset += 1
            elif key == curses.KEY_PPAGE:
                offset = max(0, offset - page)
            elif key == curses.KEY_NPAGE:
                offset += page
            elif key == curses.KEY_HOME:
                offset = 0
            elif key == curses.KEY_END:
                offset = max(0, len(lines) - page)

    def input_value(screen, label, initial, hidden):
        value, cursor, scroll = initial, len(initial), 0
        explanation = label + ". Enter accepts. Esc restores the previous value. Ctrl-U clears. Home, End, Left, Right, Backspace and Delete edit at the cursor. The < and > markers show text outside the visible field."
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        try:
            while True:
                screen.erase()
                height, width = screen.getmaxyx()
                usable = height >= 8 and width >= 40
                available = max(1, width - 12)
                shown = "*" * len(value) if hidden else value
                columns = [0]
                for character in shown:
                    columns.append(columns[-1] + character_width(character))
                scroll = min(scroll, cursor)
                while scroll and columns[-1] - columns[scroll - 1] < available:
                    scroll -= 1
                # A viewport begins at a base character; otherwise a combining
                # mark would decorate the left scroll marker instead of its text.
                while scroll and scroll < len(shown) and not character_width(shown[scroll]):
                    scroll -= 1
                cursor_width = max(1, character_width(shown[cursor]) if cursor < len(shown) else 1)
                while scroll < cursor and columns[cursor] - columns[scroll] + cursor_width > available:
                    scroll += 1
                    while scroll < cursor and not character_width(shown[scroll]):
                        scroll += 1
                visible = column_prefix(shown[scroll:], available)
                add_line(screen, 0, clipped("Edit " + label, width - 1), width, curses.A_BOLD)
                if usable:
                    add_line(screen, 1, "Left/Right Home/End  Backspace/Delete", width)
                    add_line(screen, 3, "Value: [" + ("<" if scroll else " "), width)
                    add_line(screen, 3, padded(visible, available),
                             available + 1, curses.A_UNDERLINE, x=9)
                    add_line(screen, 3, (">" if scroll + len(visible) < len(shown) else " ") + "]",
                             3, x=9 + available)
                    add_line(screen, 4, f"Position {cursor + 1}/{len(value) + 1}  < > more text", width)
                    add_line(screen, height - 2, "Enter accepts; Esc restores previous value", width)
                    add_line(screen, height - 1, "Enter OK  Esc cancel  ^U clear  F1 help", width, curses.A_BOLD)
                elif height > 1:
                    add_line(screen, 1, "Resize terminal to at least 40 columns x 8 rows (Esc: back)", width)
                if usable and hasattr(screen, "move"):
                    try:
                        screen.move(3, 9 + columns[cursor] - columns[scroll])
                    except curses.error:
                        pass
                screen.refresh()
                key = screen.get_wch()
                if key == "\x1b":
                    return None
                if not usable:
                    continue
                if key in ("\n", "\r"):
                    return value
                if key == curses.KEY_F1:
                    full_help(screen, "Editing a value", explanation)
                if key == curses.KEY_LEFT:
                    cursor = max(0, cursor - 1)
                elif key == curses.KEY_RIGHT:
                    cursor = min(len(value), cursor + 1)
                elif key in (curses.KEY_HOME, "\x01"):
                    cursor = 0
                elif key in (curses.KEY_END, "\x05"):
                    cursor = len(value)
                elif key == curses.KEY_DC:
                    value = value[:cursor] + value[cursor + 1:]
                elif key == "\x15":
                    value, cursor, scroll = "", 0, 0
                elif key in (curses.KEY_BACKSPACE, "\x7f", "\b") and cursor:
                    value = value[:cursor - 1] + value[cursor:]
                    cursor -= 1
                elif isinstance(key, str) and key.isprintable():
                    value = value[:cursor] + key + value[cursor:]
                    cursor += 1
        finally:
            try:
                curses.curs_set(0)
            except curses.error:
                pass

    def choose(screen, rows, title, index=0, explanation="", details=None):
        while True:
            help_text = details[index] if details else explanation
            usable = paint(screen, rows, index, title, help_text,
                           controls="Enter choose  ? help  Esc back")
            key = screen.get_wch()
            if key in ("\x1b", curses.KEY_LEFT):
                return None
            if not usable:
                continue
            destination = moved(key, index, len(rows), screen)
            if destination is not None:
                index = destination
            elif key in (" ", "\n", "\r"):
                return index
            elif key in ("\x1b", curses.KEY_LEFT):
                return None
            elif key in ("?", curses.KEY_F1):
                full_help(screen, title, help_text)

    def pick_model(screen, spec):
        """``(provider, api, model)`` chosen together, or None; any CLI's model may serve any role."""
        current = (spec["provider"], spec.get("api_provider", ""), spec["model"]) if spec else None
        apis = [(provider, '') for provider in AGENT_PROVIDERS]
        apis += [(provider, name) for name, connection in draft['external_providers'].items()
                 for provider in AGENT_PROVIDERS if connection['enabled'] and connection[provider]['url']]
        if current and current[:2] not in apis:
            # A deleted or disabled API stays editable by hand rather than silently moving the candidate.
            apis.insert(0, current[:2])
        suggestions = [(provider, '', model, f'{model} [{provider}]')
                       for provider in AGENT_PROVIDERS for model in model_suggestions[provider]]
        suggestions += [(provider, api, model, (settings['display_name'] or model) + f' [{provider}@{api}: {model}]')
                        for provider, api in apis if api and api in draft['external_providers']
                        for model, settings in draft['external_providers'][api][provider]['models'].items()]
        if current and current not in [row[:3] for row in suggestions]:
            suggestions.insert(0, (*current, f'{current[2]} [{current[0]}{"@" + current[1] if current[1] else ""}]'))
        rows = [f'Enter a custom model ID... [{provider}{"@" + api if api else ""}]' for provider, api in apis]
        offset = len(rows)
        rows += [row[3] + (' (current)' if row[:3] == current else '') for row in suggestions]
        index = next((i + offset for i, row in enumerate(suggestions) if row[:3] == current), offset)
        about = ('Select the CLI, API and model together. A candidate of the other CLI than the coordinator\'s runs as a '
                 'managed CLI process. Configured external models are not live discovery; availability depends on the provider account.')
        picked = choose(screen, rows, 'Model', index, explanation=about)
        if picked is None:
            return None
        if picked < offset:
            provider, api = apis[picked]
            same = current and current[:2] == (provider, api)
            changed = input_value(screen, f'{provider} / {api or "Built-in API"} / model', current[2] if same else '', False)
            return (provider, api, changed) if changed else None
        return suggestions[picked - offset][:3]

    def edit_candidates(screen, models, path):
        """Edit an ordered candidate list in place; True when the draft changed."""
        changed, index = False, 0
        title = " / ".join(label_for(part, path[:i]) for i, part in enumerate(path))
        while True:
            rows = [f"{number}. {candidate_label(spec)}" for number, spec in enumerate(models, 1)] + ["Add model"]
            details = [f"Candidate {number}: Enter to change its model, effort or fast, move it or remove it. " + help_for(path)
                       for number in range(1, len(models) + 1)] + ["Append a candidate; the first available candidate runs. " + help_for(path)]
            index = choose(screen, rows, title, min(index, len(rows) - 1), details=details)
            if index is None:
                return changed
            if index == len(models):
                chosen = pick_model(screen, None)
                if chosen:
                    provider, api, model = chosen
                    models.append(model_entry({"provider": provider, "model": model, "api_provider": api}))
                    changed = True
                continue
            spec = models[index]
            actions = [f"Model: {spec['model']} [{spec['provider']}{'@' + spec['api_provider'] if spec.get('api_provider') else ''}]",
                       f"Effort: {spec['effort']}", f"Fast: {'on' if spec.get('fast') is True else 'off'}",
                       "Move up", "Move down", "Remove"]
            action = choose(screen, actions, f"{title} / {index + 1}", 0,
                            details=["Choose the CLI, API and model.", "Reasoning effort; default omits agent-box's effort override.",
                                     help_for(("workflow", "fast")), "Prefer this candidate earlier.",
                                     "Prefer this candidate later.", "Delete this candidate from the list."])
            if action == 0:
                chosen = pick_model(screen, spec)
                if chosen:
                    spec["provider"], spec["api_provider"], spec["model"] = chosen
                    changed = True
            elif action == 1:
                options = candidate_efforts(spec, draft)
                picked = choose(screen, list(options), f"{title} / {index + 1} / effort",
                                options.index(spec["effort"]) if spec["effort"] in options else 0)
                if picked is not None:
                    spec["effort"], changed = options[picked], True
            elif action == 2:
                spec["fast"], changed = spec.get("fast") is not True, True
            elif action == 3 and index:
                models[index - 1], models[index] = models[index], models[index - 1]
                index, changed = index - 1, True
            elif action == 4 and index < len(models) - 1:
                models[index + 1], models[index] = models[index], models[index + 1]
                index, changed = index + 1, True
            elif action == 5:
                del models[index]
                changed = True

    def path_pending():
        """Would saving now leave the shell unable to find agent-box?"""
        return not project_mode and path_state["bin"] is None and link.kind != "ours"

    def path_row():
        """Describe PATH ownership even when the guided section stays closed."""
        if project_mode:
            return "PATH integration: Global only"
        if path_state["bin"] is not None:
            return f"PATH integration: agent-box keeps {path_state['bin'] / pathsetup.COMMAND}"
        if link.kind == "ours":
            return f"PATH integration: found {link.found}, kept by you, not agent-box"
        return "PATH integration: agent-box is not on PATH yet"

    def menu(screen, data, path=()):
        selected, notice = (len(data), "Setup starts here: agent-box is not on PATH yet") if not path and path_pending() else (0, "")
        while True:
            keys = menu_keys(data, path)
            extras = [("Review preset", "preset", ACTION_HELP["preset"])] if path == ("workflow", "review") else []
            if path == ("external_providers",) and not project_mode:
                extras = [("Add provider", "add_provider", "Create a disabled provider with separate Codex/Claude endpoints.")]
            elif len(path) == 2 and path[0] == "external_providers" and not project_mode:
                extras = [("Delete provider", "delete_provider", "Remove this definition from the draft. Existing workflow references remain visible and must be changed before use.")]
            elif path[:1] == ('external_providers',) and not project_mode:
                if len(path) == 4 and path[-1] == 'models':
                    extras = [('Add model', 'add_model', 'Create a model with editable settings for this CLI.')]
                elif len(path) == 5 and path[3] == 'models':
                    extras = [('Use as default', 'default_model', 'Select this model for launches without --model.'),
                              ('Delete model', 'delete_model', 'Remove this model and clear its default selection if needed.')]
            if not path:
                extras = [(path_row(), "path", path_row() + ". " + ACTION_HELP["path"]), ("Save and apply", "save", ACTION_HELP["save"]), ("Cancel", "cancel", ACTION_HELP["cancel"])]
                if project_mode:
                    extras = [("PATH integration: Global only", "global", "Global only: configure host integration in agent-box config."), ("Save project", "save", "Save explicit project overrides without applying host integrations."), ("Cancel", "cancel", ACTION_HELP["cancel"])]
            labels = [label_for(key, path) for key in keys]
            rows = []
            for key, label in zip(keys, labels):
                value, full = data[key], (*path, key)
                if isinstance(value, dict) and not json_field(full):
                    shown = ">"
                elif isinstance(value, bool):
                    shown = "[x]" if value else "[ ]"
                elif secret(full):
                    shown = "<configured>" if value else "<empty>"
                elif candidate_list(full) and isinstance(value, list):
                    shown = ", ".join(candidate_label(spec) for spec in value) or "(none)"
                elif isinstance(value, str):
                    shown = value or "(not set)"
                else:
                    shown = json.dumps(value, ensure_ascii=False)
                if project_mode:
                    status = "Global only" if project_field(full) is None else "Overridden" if explicit(full) else "Inherited"
                    if project_field(full) is None:
                        shown = (">  " if isinstance(value, dict) else "") + "[Global only]"
                    else:
                        shown += "  [" + status + "]"
                rows.append((label, shown))
            rows += [entry[0] for entry in extras]
            selected = min(selected, len(rows) - 1)
            if selected < len(keys):
                selected_path = (*path, keys[selected])
                hint, help_title = help_for(selected_path), ".".join(selected_path)
                if project_mode:
                    hint = ("Global only. Edit in agent-box config. " if project_field(selected_path) is None
                            else "R: Reset to default (remove override; inherit future global changes). "
                            + ("Copy current global unavailable: no global value for this project identity field. "
                               if default_value(selected_path, global_values) is None
                               else "G: Copy current global (copy and pin current effective global values). ")) + hint
                else:
                    hint = "R: Reset to default (built-in program values; remove global override on save). " + hint
                hint = labels[selected] + "\n" + hint
            else:
                hint, help_title = extras[selected - len(keys)][2], extras[selected - len(keys)][0]
            title = ("agent-box config --project" if project_mode else "agent-box config") + (" / " + " / ".join(label_for(part, path[:index]) for index, part in enumerate(path)) if path else "")
            controls = ("Enter R reset G copy " if project_mode else "Enter open R reset ") + "? help Esc back"
            group = next((heading for heading, fields in MENU_GROUPS.get(path, ())
                          if selected < len(keys) and keys[selected] in fields), "")
            usable = paint(screen, rows, selected, title, hint, notice, controls=controls, group=group)
            key = screen.get_wch()
            if key in ("\x1b", curses.KEY_LEFT):
                return None
            if not usable:
                continue
            destination = moved(key, selected, len(rows), screen)
            if destination is not None:
                selected, notice = destination, ""
            elif key in ("?", getattr(curses, "KEY_F1", -1)):
                full_help(screen, "Help: " + help_title, notice + "\n\n" + hint if notice else hint)
            elif key in ("\x1b", curses.KEY_LEFT):
                return None
            elif key in ("r", "R", "g", "G") and selected < len(keys):
                full = (*path, keys[selected])
                if project_mode and project_field(full) is None:
                    notice = "Global only"
                elif project_mode and key in ("r", "R"):
                    data[keys[selected]] = remove_override(full)
                    notice = "Reset to default: override removed; inheriting global values"
                elif key in ("r", "R") or project_mode:
                    value = default_value(full, global_values if project_mode else defaults)
                    if value is None:
                        notice = "Copy current global unavailable: no global value for this project identity field"
                    else:
                        if project_mode:
                            if full == ("tracker",):
                                value.update({name: overrides["tracker"][name]
                                              for name in ("project", "recursive")
                                              if name in overrides.get("tracker", {})})
                            record(full, value)
                            current = merge(inherited, overrides)
                            for part in full:
                                current = current[part]
                            data[keys[selected]] = current
                        else:
                            data[keys[selected]] = value
                        notice = ("Copy current global: current values copied as explicit project overrides" if project_mode
                                  else "Reset to default: built-in program values restored in draft")
            elif key in (" ", "\n", "\r", curses.KEY_RIGHT):
                if selected >= len(keys):
                    _, action, _ = extras[selected - len(keys)]
                    if action == 'add_model':
                        model = input_value(screen, 'Model ID ([1m] suffix allowed for Claude)', '', False)
                        if model is not None:
                            if not valid_model(model, path[2]):
                                notice = 'Invalid model ID for this CLI'
                            elif model in data:
                                notice = 'Model already exists'
                            else:
                                data[model] = model_defaults(path[2])
                                menu(screen, data[model], (*path, model))
                        continue
                    if action in ('default_model', 'delete_model'):
                        endpoint = draft['external_providers'][path[1]][path[2]]
                        if action == 'default_model':
                            endpoint['default_model'] = path[4]
                            notice = 'Default model updated in draft'
                        elif choose(screen, ['Keep model', 'Delete model'], 'Delete ' + path[4], 0) == 1:
                            del endpoint['models'][path[4]]
                            if endpoint['default_model'] == path[4]:
                                endpoint['default_model'] = ''
                            return None
                        continue
                    if action == "add_provider":
                        name = input_value(screen, "Provider name (lowercase letters, digits, _ or -)", "", False)
                        if name is not None:
                            if not valid_name(name):
                                notice = "Use [a-z][a-z0-9_-]{0,47}"
                            elif name in data:
                                notice = "Provider already exists"
                            else:
                                data[name] = ExternalProvider().to_dict()
                                menu(screen, data[name], (*path, name))
                        continue
                    if action == "delete_provider":
                        chosen = choose(screen, ["Keep provider", "Delete provider"], "Delete " + path[1], 0)
                        if chosen == 1:
                            del draft["external_providers"][path[1]]
                            return None
                        continue
                    if action == "global":
                        notice = "Global only"
                    if action == "cancel":
                        return None
                    if action == "path":
                        path_menu(screen)
                        notice = ""
                    elif action == "preset":
                        names = list(REVIEW_PRESETS)
                        descriptions = [
                            f"{name.capitalize()}: review at {REVIEW_PRESETS[name][0]} changed files or "
                            f"{REVIEW_PRESETS[name][1]} changed lines. Sensitive paths and the review label "
                            "still trigger review independently. Only these two thresholds change."
                            for name in names
                        ]
                        chosen = choose(screen, names, "Review preset", 0, details=descriptions)
                        if chosen is not None:
                            data["max_files"], data["max_lines"] = REVIEW_PRESETS[names[chosen]]
                            if project_mode:
                                record((*path, "max_files"), data["max_files"])
                                record((*path, "max_lines"), data["max_lines"])
                            notice = "Review thresholds updated in the draft"
                    elif action == "save":
                        try:
                            result = drafted()
                        except AgentBoxError as exc:
                            notice = str(exc)
                            continue
                        if project_mode:
                            try:
                                validate_project(overrides, Path(".agent-box.json"))
                                errors = result.workflow.validate()
                            except AgentBoxError as exc:
                                errors = [str(exc)]
                        else:
                            errors = validate(result)
                        if errors:
                            notice = errors[0]
                        elif path_pending() and choices and not path_gate(screen):
                            notice = "PATH left unchanged; agent-box still resolves only by path"
                        else:
                            return EditResult(result, path_state["rc"], overrides)
                    continue
                name, full, value = keys[selected], (*path, keys[selected]), data[keys[selected]]
                if project_mode and project_field(full) is None:
                    notice = "Global only: edit in agent-box config"
                    continue
                accepted = False
                options = value_choices(full, data, draft)
                if isinstance(value, bool):
                    data[name] = not value
                    accepted = True
                elif candidate_list(full) and isinstance(value, list):
                    if edit_candidates(screen, value, full):
                        notice = "Draft updated; save to keep changes"
                        accepted = True
                elif options:
                    if key == " ":
                        data[name] = options[(options.index(value) + 1) % len(options)] if value in options else options[0]
                        accepted = True
                    else:
                        picked = choose(screen, [option or ('First configured model' if name == 'default_model' else "Built-in API") for option in options], ".".join(full), options.index(value) if value in options else 0, help_for(full))
                        if picked is not None:
                            data[name] = options[picked]
                            accepted = True
                elif isinstance(value, dict) and not json_field(full):
                    menu(screen, value, full)
                else:
                    initial = json.dumps(value) if isinstance(value, (dict, list)) else str(value)
                    changed = input_value(screen, " / ".join(label_for(part, full[:index]) for index, part in enumerate(full)), initial, secret(full))
                    if changed is not None:
                        try:
                            parsed = parse_value(changed, value)
                            if full[:1] == ("env",):
                                parsed = env_entries(name, parsed)
                            data[name], notice = parsed, "Draft updated; save to keep changes"
                            accepted = True
                        except (ValueError, json.JSONDecodeError) as exc:
                            notice = str(exc)
                if project_mode and accepted:
                    record(full, data[name])

    def path_menu(screen):
        """Prefer the relevant safe directory while retaining an explicit decline."""
        rows = ["Leave PATH alone; agent-box maintains no symlink"] + ["Create agent-box symlink in " + str(c.path) + (" (also needs a shell startup line)" if c.needs_rc_line else "") for c in choices]
        current = [i for i, c in enumerate(choices, 1) if c.path == path_state["bin"]]
        picked = choose(screen, rows, "PATH: applied only when saving", current[0] if current else (1 if path_pending() and choices else 0), ACTION_HELP["path"])
        if picked is None:
            return
        path_state.update(bin=None, rc=False)
        if picked:
            choice = choices[picked - 1]
            path_state["bin"] = choice.path
            rc_file = pathsetup.rc_file()
            if choice.needs_rc_line and rc_file:
                rc = choose(screen, ["Leave shell startup file unchanged", "Append PATH block to " + str(rc_file)], "Shell PATH block", 1, "The generated marked PATH block is appended only after saving. Existing or edited user-owned blocks are never replaced.")
                path_state["rc"] = rc == 1

    def path_gate(screen):
        """Ask before saving a configuration for a command the shell cannot run."""
        picked = choose(screen, ["Set up PATH now", "Save without putting agent-box on PATH"], path_row(), 0, "Saving without PATH is allowed, but the shell may only find agent-box through its checkout path.")
        if picked is None:
            return False
        if picked == 1:
            return True
        path_menu(screen)
        return path_state["bin"] is not None

    def start(screen):
        nonlocal global_only_color
        try:
            curses.use_default_colors()
            if project_mode and curses.has_colors():
                curses.init_pair(1, curses.COLOR_RED, -1)
                global_only_color = curses.color_pair(1)
        except curses.error:
            pass
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        screen.keypad(True)
        return menu(screen, draft)

    previous_escdelay = curses.get_escdelay() if hasattr(curses, "get_escdelay") else None
    try:
        if hasattr(curses, "set_escdelay"):
            curses.set_escdelay(25)
        return curses.wrapper(start)
    except KeyboardInterrupt:
        return None
    finally:
        if previous_escdelay is not None:
            curses.set_escdelay(previous_escdelay)
