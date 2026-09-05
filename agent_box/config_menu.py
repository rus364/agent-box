"""A dependency-free terminal form; no writes occur until Save and apply."""
from __future__ import annotations

import copy
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import Config, REVIEW_PRESETS, TRACKER_PROVIDERS
from .errors import AgentBoxError

CHOICES = {
    "tracker.provider": TRACKER_PROVIDERS,
    "workflow.routing": ("current", "weighted", "usage-aware"),
    "workflow.review.policy": ("off", "auto", "always"),
    "workflow.review.github_provider": ("auto", "local", "codex", "claude"),
}


HELP = {
    "browser.enabled": "Headless Chromium in the box; isolated in-memory profile, same trust boundary as agents. Unavailable in --host mode.",
    "tracker.enabled": "Expose configured tracker connections to agents. Keep credentials out of repository files.",
    "workflow.routing": "Workers/scouts: current provider, static weights, or weekly usage-aware weights. Reviewers prefer the other writer provider.",
    "workflow.weights": "Relative subscription capacity; set a provider to zero to exclude it from worker/scout routing.",
    "workflow.tiers": "Tier 0: scouts only. Tiers 1-3: task complexity. Set exact supported model IDs and effort for both providers.",
}


def validate(config):
    errors = config.workflow.validate()
    if config.claude.telegram_channel:
        from .channel import missing_requirements
        errors.extend(missing_requirements())
    return errors


@dataclass
class EditResult:
    config: Config
    bin_dir: Path | None = None
    edit_rc: bool = False


def secret(path):
    return "token" in path[-1] or path[0] == "env"


def parse_value(text, current):
    if isinstance(current, list) or isinstance(current, dict):
        value = json.loads(text)
        if not isinstance(value, type(current)):
            raise ValueError(f"Expected a JSON {type(current).__name__}")
        return value
    if isinstance(current, int):
        return int(text)
    if isinstance(current, float):
        return float(text)
    return text


def edit(config):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise AgentBoxError("agent-box config needs a terminal; edit config.json and run config apply for noninteractive use")
    try:
        import curses
    except ImportError:
        raise AgentBoxError("This Python build lacks stdlib curses; edit config.json and run config apply") from None
    from . import pathsetup
    choices = pathsetup.bin_dir_choices()
    draft = copy.deepcopy(config.to_dict())
    draft.pop("version", None)
    for provider in TRACKER_PROVIDERS:
        draft["tracker"]["connections"].setdefault(provider, {"url": "", "token": ""})
    path_state = {"bin": None, "rc": False}

    def paint(screen, rows, selected, title, notice=""):
        screen.erase()
        height, width = screen.getmaxyx()
        if height < 8 or width < 40:
            raise AgentBoxError("Enlarge the terminal to at least 40 columns and 8 rows")
        def line(y, text, attr=0):
            screen.addnstr(y, 0, text, max(1, width - 1), attr)
        line(0, title, curses.A_BOLD)
        line(1, "Arrows: move  Space: toggle/cycle  Enter: edit/open  Esc: back")
        offset = max(0, selected - (height - 6))
        for index, row in enumerate(rows[offset:offset + height - 4], offset):
            line(index - offset + 3, row, curses.A_REVERSE if index == selected else 0)
        line(height - 1, notice)
        screen.refresh()

    def input_value(screen, label, initial, hidden):
        value = initial
        while True:
            display = "*" * len(value) if hidden else value
            paint(screen, [label, display, "Enter: accept  Esc: cancel  Ctrl-U: clear"], 1, "Edit value")
            key = screen.get_wch()
            if key in ("\n", "\r"):
                return value
            if key == "\x1b":
                return None
            if key == "\x15":
                value = ""
            elif key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                value = value[:-1]
            elif isinstance(key, str) and key.isprintable():
                value += key

    def menu(screen, data, path=()):
        selected, notice = 0, ""
        while True:
            keys = list(data)
            extras = ["Review preset"] if path == ("workflow", "review") else []
            if not path:
                extras = ["PATH integration", "Save and apply", "Cancel"]
            rows = []
            for key, value in data.items():
                full = (*path, key)
                if isinstance(value, dict) and full not in (("env", "set"),):
                    shown = ">"
                elif isinstance(value, bool):
                    shown = "[x]" if value else "[ ]"
                elif secret(full):
                    shown = "<configured>" if value else "<empty>"
                else:
                    shown = json.dumps(value, ensure_ascii=False)
                rows.append(f"{key.replace('_', ' ')}: {shown}")
            rows += extras
            selected = min(selected, len(rows) - 1)
            hint = HELP.get(".".join((*path, keys[selected])), "") if selected < len(keys) else ""
            paint(screen, rows, selected, "agent-box config" + (" / " + " / ".join(path) if path else ""), notice or hint)
            key = screen.get_wch()
            if key == curses.KEY_UP:
                selected = (selected - 1) % len(rows)
            elif key == curses.KEY_DOWN:
                selected = (selected + 1) % len(rows)
            elif key == "\x1b":
                return None
            elif key in (" ", "\n", "\r", curses.KEY_RIGHT):
                if selected >= len(keys):
                    action = extras[selected - len(keys)]
                    if action == "Cancel":
                        return None
                    if action == "PATH integration":
                        path_menu(screen)
                    elif action == "Review preset":
                        presets = {name: False for name in REVIEW_PRESETS}
                        chosen = choose(screen, list(presets), "Review preset (custom thresholds remain editable)")
                        if chosen is not None:
                            data["max_files"], data["max_lines"] = REVIEW_PRESETS[list(presets)[chosen]]
                    elif action == "Save and apply":
                        result = Config.from_dict(draft)
                        errors = validate(result)
                        if errors:
                            notice = errors[0]
                        else:
                            return EditResult(result, path_state["bin"], path_state["rc"])
                    continue
                name = keys[selected]
                full = (*path, name)
                value = data[name]
                options = CHOICES.get(".".join(full))
                if name == "effort" and path[:2] == ("workflow", "tiers"):
                    options = ("low", "medium", "high", "xhigh", "max") if path[-1] == "claude" else ("low", "medium", "high", "xhigh", "max", "ultra")
                if isinstance(value, bool):
                    data[name] = not value
                elif options:
                    data[name] = options[(options.index(value) + 1) % len(options)] if value in options else options[0]
                elif isinstance(value, dict) and full != ("env", "set"):
                    menu(screen, value, full)
                else:
                    initial = json.dumps(value) if isinstance(value, (dict, list)) else str(value)
                    changed = input_value(screen, ".".join(full), initial, secret(full))
                    if changed is not None:
                        try:
                            data[name] = parse_value(changed, value)
                            notice = "Draft updated; Save and apply commits changes"
                        except ValueError as exc:
                            notice = str(exc)

    def choose(screen, rows, title):
        index = 0
        while True:
            paint(screen, rows, index, title)
            key = screen.get_wch()
            if key == curses.KEY_UP:
                index = (index - 1) % len(rows)
            elif key == curses.KEY_DOWN:
                index = (index + 1) % len(rows)
            elif key in (" ", "\n", "\r"):
                return index
            elif key == "\x1b":
                return None

    def path_menu(screen):
        rows = ["Leave PATH and shell startup files unchanged"]
        rows += ["Create agent-box symlink in " + str(c.path) for c in choices]
        picked = choose(screen, rows, "PATH: applied only when saving")
        if picked is None:
            return
        path_state.update(bin=None, rc=False)
        if picked:
            choice = choices[picked - 1]
            path_state["bin"] = choice.path
            if choice.needs_rc_line and pathsetup.rc_file():
                rc = choose(screen, ["Leave shell startup file unchanged", "Append PATH block to " + str(pathsetup.rc_file())], "Shell PATH block")
                path_state["rc"] = rc == 1

    def start(screen):
        curses.curs_set(0)
        screen.keypad(True)
        return menu(screen, draft)
    try:
        return curses.wrapper(start)
    except KeyboardInterrupt:
        return None
