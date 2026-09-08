"""A dependency-free terminal form; no writes occur until Save and apply."""
from __future__ import annotations

import copy
import json
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path

from .config import AGENT_EFFORTS, IMAGE_TOOL_GROUP_HELP, Config, REVIEW_PRESETS, TRACKER_PROVIDERS
from .errors import AgentBoxError
from .paths import CODEX_MODELS_CACHE

CHOICES = {
    "tracker.provider": TRACKER_PROVIDERS,
    "workflow.merge_mode": ("github", "local"),
    "workflow.routing": ("current", "weighted", "usage-aware"),
    "workflow.review.routing": ("cross-provider", "same-provider"),
    "workflow.review.policy": ("off", "auto", "always"),
    "workflow.review.github_provider": ("auto", "local", "codex", "claude"),
}
# Offline suggestions, not a promise of account access. Sources:
# https://learn.chatgpt.com/docs/models
# https://code.claude.com/docs/en/model-config
BUNDLED_MODELS = {
    "codex": ("gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5", "gpt-5.3-codex-spark"),
    "claude": ("opus", "sonnet", "haiku", "claude-opus-5", "claude-sonnet-5"),
}

HELP = {
    "claude": "Claude host integration settings. Changes are applied only after Save and apply.",
    "claude.telegram_channel": "Start Claude with Anthropic's official Telegram channel plugin. This requires its host-side setup and exposes the session through that channel.",
    "claude.statusline": "Keep Claude's status line pointed at agent-box. An existing user-owned status line is never overwritten.",
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
    "tracker.connections": "Provider URLs and tokens rendered into minimal runtime credential files only when tracker integration is enabled.",
    "browser": "Configure the agent-controlled headless browser inside the workspace box.",
    "browser.enabled": "Headless Chromium in the box; isolated in-memory profile, same trust boundary as agents. Unavailable in --host mode.",
    "workflow": "Opt-in tracked-work orchestration, routing, review, publication, and cleanup policy.",
    "workflow.enabled": "Enable agent-box's tracked-task workflow. Enabling it does not itself expose provider or tracker credentials.",
    "workflow.merge_mode": "Finish parent work through a GitHub pull request or a local fast-forward into origin's default branch.",
    "workflow.routing": "Workers and scouts use the current provider, static weights, or weekly usage-aware weights. Review routing is configured separately.",
    "workflow.weights": "Relative provider capacity for worker and scout routing. A zero weight excludes that provider; all weights cannot be zero.",
    "workflow.tiers": "Worker and scout model candidates. Tier 0 is scouts only; tiers 1-3 represent increasing task complexity. Choose faster models for simple work and more capable models for complex tasks. Each tier has independent provider and effort settings.",
    "workflow.fallback": "Try another eligible provider if the preferred worker, scout, or cross-provider reviewer cannot run. Same-provider review stays with the writer even when fallback is enabled.",
    "workflow.reserve_percent": "Subscription capacity reserved from usage-aware routing. Valid values are 0 through 99 percent.",
    "workflow.smoothing_hours": "Time window, from 1 through 168 hours, used to smooth usage-aware routing decisions.",
    "workflow.scout_limit": "Maximum number of concurrent read-only scouts, from 1 through 16.",
    "workflow.auto_push": "Within the opt-in workflow, publish the parent task in GitHub mode or the accepted base after a local fast-forward. Turning this off keeps completion local.",
    "workflow.timeout_seconds": "Maximum delegated agent runtime in seconds, from 30 through 86400.",
    "workflow.review": "Independent review routing, thresholds, provider selection, and reviewer candidates. Worker tier settings are never inherited.",
    "workflow.review.tiers": "Independent reviewer candidates for tiers 1-3. An empty model explicitly disables that provider candidate.",
    "workflow.review.policy": "Off skips workflow review. Auto triggers on file/line thresholds, sensitive paths, or the review label. Always requires review for every task and the complete parent diff. GitHub merge may still require independent approval.",
    "workflow.review.routing": "Cross-provider prefers a provider different from the writer; same-provider requires the writer's provider.",
    "workflow.review.max_files": "Automatic-review threshold for changed file count. The Review preset action can set this together with max lines.",
    "workflow.review.max_lines": "Automatic-review threshold for changed line count. The Review preset action can set this together with max files.",
    "workflow.review.max_rounds": "Maximum local review attempts, including failed or abandoned attempts, before explicit user allowance is required.",
    "workflow.review.sensitive_paths": "JSON list of glob patterns that always trigger automatic review regardless of size thresholds.",
    "workflow.review.label": "Tracker label that forces review for a task when the review policy is automatic.",
    "workflow.review.github_provider": "Provider requested for GitHub PR review: auto/local uses local review, Codex cannot pin model or effort, and Claude uses its configured Action.",
    "workflow.review.github_model": "Model expected in a Claude GitHub Action. Codex GitHub review rejects model overrides; leave empty unless the Claude Action pins it.",
    "workflow.review.github_effort": "Effort expected in a Claude GitHub Action. Codex GitHub review rejects effort overrides; leave empty unless the Claude Action pins it.",
    "workflow.review.wait_seconds": "Wait budget in seconds for cloud review feedback. The workflow reports this budget to the coordinator; lack of a response never counts as approval. Must be a positive integer.",
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
    "claude": "Claude", "codex": "Codex", "github": "GitHub", "git": "Git",
    "env": "Environment", "providers": "Provider environment", "image": "Workspace image",
    "url": "URL", "token": "Access token", "effort": "Reasoning effort",
    "github_model": "GitHub review model", "github_effort": "GitHub review effort",
}


def label_for(key):
    return LABELS.get(key, f"Tier {key}" if key.isdigit() else key.replace("_", " ").capitalize())


def _valid_model_slug(value):
    return isinstance(value, str) and bool(value) and value.strip() == value and all(c.isalnum() or c in "._-:/" for c in value)


def load_codex_models(path=None):
    """Read visible CLI model slugs without invoking Codex or using a network."""
    try:
        data = json.loads((path if path is not None else CODEX_MODELS_CACHE).read_text())
        models = data.get("models") if isinstance(data, dict) else None
        visible = [item["slug"] for item in models if isinstance(item, dict) and item.get("visibility") == "list" and _valid_model_slug(item.get("slug"))]
    except (OSError, ValueError, TypeError):
        visible = []
    return tuple(dict.fromkeys(visible or BUNDLED_MODELS["codex"]))


def help_for(path):
    """Return complete help for every static and generated menu entry."""
    dotted = ".".join(path)
    if dotted in HELP:
        return HELP[dotted]
    if len(path) == 3 and path[:2] == ("image", "tools"):
        return f"{IMAGE_TOOL_GROUP_HELP.get(path[-1], path[-1])}. Rebuild the workspace image to install or remove this group; browsing and saving this selection do not rebuild it."
    if len(path) == 3 and path[:2] == ("tracker", "connections"):
        return f"Connection settings for the {path[-1]} tracker. It is exposed only when tracker integration is enabled."
    if len(path) == 4 and path[:2] == ("tracker", "connections"):
        if path[-1] == "url":
            return f"Base URL for the {path[-2]} tracker API. No network request is made while editing."
        return f"Access token for the {path[-2]} tracker. It is masked here and rendered only for enabled tracker integration."
    if len(path) == 3 and path[:2] == ("workflow", "weights"):
        return f"Relative {path[-1]} capacity for worker and scout routing. Zero excludes this provider."
    if len(path) == 3 and path[:2] == ("workflow", "tiers"):
        return f"Worker and scout tier {path[-1]} candidates for Codex and Claude. Tier 0 is used only by scouts."
    if len(path) == 4 and path[:2] == ("workflow", "tiers"):
        return f"{path[-1].title()} candidate for worker and scout tier {path[-2]}. Configure model and effort independently."
    if len(path) == 5 and path[:2] == ("workflow", "tiers"):
        subject = f"{path[-2].title()} worker and scout candidate"
        return (f"Model for the {subject}. Choose a suggestion, enter any supported ID, or disable this candidate." if path[-1] == "model" else f"Reasoning effort for the {subject}. Default omits agent-box's effort override.")
    if len(path) == 4 and path[:3] == ("workflow", "review", "tiers"):
        return f"Independent reviewer tier {path[-1]} candidates for Codex and Claude. Empty models disable candidates."
    if len(path) == 5 and path[:3] == ("workflow", "review", "tiers"):
        return f"{path[-1].title()} reviewer candidate for tier {path[-2]}. It does not inherit worker settings."
    if len(path) == 6 and path[:3] == ("workflow", "review", "tiers"):
        subject = f"{path[-2].title()} reviewer candidate"
        return (f"Model for the {subject}. Choose a suggestion, enter any supported ID, or disable this candidate." if path[-1] == "model" else f"Reasoning effort for the {subject}. Default omits agent-box's effort override.")
    raise KeyError(f"No menu help for {dotted}")


def validate(config):
    errors = config.workflow.validate()
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


def secret(path):
    return "token" in path[-1] or path[0] == "env"


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


def _model_path(path):
    return path[-1:] == ("model",) and (path[:2] == ("workflow", "tiers") or path[:3] == ("workflow", "review", "tiers"))


def edit(config):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise AgentBoxError("agent-box config needs a terminal; edit config.json and run config apply for noninteractive use")
    try:
        import curses
    except ImportError:
        raise AgentBoxError("This Python build lacks stdlib curses; edit config.json and run config apply") from None
    from . import pathsetup
    choices, link = pathsetup.bin_dir_choices(), pathsetup.link_state()
    model_suggestions = {"codex": load_codex_models(), "claude": BUNDLED_MODELS["claude"]}
    draft = copy.deepcopy(config.to_dict())
    draft.pop("version", None)
    draft.pop("install", None)
    for provider in TRACKER_PROVIDERS:
        draft["tracker"]["connections"].setdefault(provider, {"url": "", "token": ""})
    path_state = {"bin": Path(config.install.bin_dir) if config.install.bin_dir else None, "rc": False}

    def drafted():
        return Config.from_dict({**draft, "install": {"bin_dir": str(path_state["bin"]) if path_state["bin"] else ""}})

    def add_line(screen, y, value, width, attr=0, x=0):
        try:
            screen.addnstr(y, x, value, max(1, width - 1), attr)
        except curses.error:
            # A resize can arrive between measuring the screen and drawing it.
            pass

    def paint(screen, rows, selected, title, help_text="", notice="", controls=None, editor=False):
        screen.erase()
        height, width = screen.getmaxyx()
        if height < 8 or width < 40:
            add_line(screen, 0, "agent-box config", width, curses.A_BOLD)
            if height > 1:
                add_line(screen, 1, "Resize terminal to at least 40 columns x 8 rows (Esc: back)", width)
            screen.refresh()
            return False
        controls = controls or "Enter open  Space set  ? help  Esc back"
        sidebar = width >= 100 and height >= 14 and not editor
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
        navigation = "Left/Right Home/End  Backspace/Delete" if editor else "Up/Down: move  Home/End  PgUp/PgDn"
        add_line(screen, 1, navigation, width)
        offset = min(max(0, selected - list_height + 1), max(0, len(rows) - list_height))
        for index, row in enumerate(rows[offset:offset + list_height], offset):
            add_line(screen, index - offset + 3, row, list_width,
                     curses.A_REVERSE | curses.A_BOLD if index == selected else 0)
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
        explanation = "Enter accepts. Esc restores the previous value. Ctrl-U clears. Home, End, Left, Right, Backspace and Delete edit at the cursor. Long values scroll horizontally."
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        try:
            while True:
                _, width = screen.getmaxyx()
                available = max(1, width - len("Value: ") - 4)
                if cursor < scroll:
                    scroll = cursor
                elif cursor > scroll + available:
                    scroll = cursor - available
                shown = "*" * len(value) if hidden else value
                usable = paint(screen, ["Value: " + shown[scroll:scroll + available]], 0,
                               "Edit " + label, explanation,
                               controls="Enter OK  Esc cancel  ^U clear  F1 help", editor=True)
                if usable and hasattr(screen, "move"):
                    try:
                        screen.move(3, min(width - 2, len("Value: ") + cursor - scroll))
                        screen.refresh()
                    except curses.error:
                        pass
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

    def model_value(screen, path, current):
        provider = path[-2]
        suggestions = list(model_suggestions[provider])
        rows = ["Enter a custom model ID...", "Disable this candidate (empty model)"]
        rows += [model + (" (current)" if model == current else "") for model in suggestions]
        # The two escape hatches stay above the list, visible even with many
        # cached models. An unset candidate opens on a useful suggestion.
        index = suggestions.index(current) + 2 if current in suggestions else 0 if current else 2
        about = f"Current model: {current or '(disabled)'}. Suggestions are local hints; availability depends on your provider account."
        details = [
            "Type or edit any provider-supported model ID. Esc cancels editing. " + about,
            "Clear the model to exclude this provider from this tier. Other tiers and roles are unchanged. " + about,
            *[f"Use {model} for this candidate. " + about for model in suggestions],
        ]
        picked = choose(screen, rows, provider.title() + " model", index, details=details)
        if picked is None:
            return None
        if picked == 1:
            return ""
        if picked == 0:
            return input_value(screen, ".".join(path), current, False)
        return suggestions[picked - 2]

    def path_pending():
        """Would saving now leave the shell unable to find agent-box?"""
        return path_state["bin"] is None and link.kind != "ours"

    def path_row():
        """Describe PATH ownership even when the guided section stays closed."""
        if path_state["bin"] is not None:
            return f"PATH integration: agent-box keeps {path_state['bin'] / pathsetup.COMMAND}"
        if link.kind == "ours":
            return f"PATH integration: found {link.found}, kept by you, not agent-box"
        return "PATH integration: agent-box is not on PATH yet"

    def menu(screen, data, path=()):
        selected, notice = (len(data), "Setup starts here: agent-box is not on PATH yet") if not path and path_pending() else (0, "")
        while True:
            keys = list(data)
            extras = [("Review preset", "preset", ACTION_HELP["preset"])] if path == ("workflow", "review") else []
            if not path:
                extras = [(path_row(), "path", path_row() + ". " + ACTION_HELP["path"]), ("Save and apply", "save", ACTION_HELP["save"]), ("Cancel", "cancel", ACTION_HELP["cancel"])]
            labels = [label_for(key) for key in keys]
            label_width = min(24, max(map(len, labels), default=0))
            rows = []
            for key, label in zip(keys, labels):
                value, full = data[key], (*path, key)
                if isinstance(value, dict) and full != ("env", "set"):
                    shown = ">"
                elif isinstance(value, bool):
                    shown = "[x]" if value else "[ ]"
                elif secret(full):
                    shown = "<configured>" if value else "<empty>"
                elif isinstance(value, str):
                    shown = value or "(not set)"
                else:
                    shown = json.dumps(value, ensure_ascii=False)
                rows.append(f"{label:<{label_width}}: {shown}")
            rows += [entry[0] for entry in extras]
            selected = min(selected, len(rows) - 1)
            if selected < len(keys):
                selected_path = (*path, keys[selected])
                hint, help_title = help_for(selected_path), ".".join(selected_path)
            else:
                hint, help_title = extras[selected - len(keys)][2], extras[selected - len(keys)][0]
            title = "agent-box config" + (" / " + " / ".join(label_for(part) for part in path) if path else "")
            usable = paint(screen, rows, selected, title, hint, notice)
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
            elif key in (" ", "\n", "\r", curses.KEY_RIGHT):
                if selected >= len(keys):
                    _, action, _ = extras[selected - len(keys)]
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
                            notice = "Review thresholds updated in the draft"
                    elif action == "save":
                        result = drafted()
                        errors = validate(result)
                        if errors:
                            notice = errors[0]
                        elif path_pending() and choices and not path_gate(screen):
                            notice = "PATH left unchanged; agent-box still resolves only by path"
                        else:
                            return EditResult(result, path_state["rc"])
                    continue
                name, full, value = keys[selected], (*path, keys[selected]), data[keys[selected]]
                options = CHOICES.get(".".join(full))
                if name == "effort" and (path[:2] == ("workflow", "tiers") or path[:3] == ("workflow", "review", "tiers")):
                    options = AGENT_EFFORTS[path[-1]]
                if isinstance(value, bool):
                    data[name] = not value
                elif _model_path(full):
                    changed = model_value(screen, full, value)
                    if changed is not None:
                        data[name], notice = changed, "Draft updated; Save and apply commits changes"
                elif options:
                    if key == " ":
                        data[name] = options[(options.index(value) + 1) % len(options)] if value in options else options[0]
                    else:
                        picked = choose(screen, list(options), ".".join(full), options.index(value) if value in options else 0, help_for(full))
                        if picked is not None:
                            data[name] = options[picked]
                elif isinstance(value, dict) and full != ("env", "set"):
                    menu(screen, value, full)
                else:
                    initial = json.dumps(value) if isinstance(value, (dict, list)) else str(value)
                    changed = input_value(screen, ".".join(full), initial, secret(full))
                    if changed is not None:
                        try:
                            data[name], notice = parse_value(changed, value), "Draft updated; Save and apply commits changes"
                        except (ValueError, json.JSONDecodeError) as exc:
                            notice = str(exc)

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
        try:
            curses.use_default_colors()
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
