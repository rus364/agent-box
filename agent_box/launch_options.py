"""Launch options: one table parses them, renders their help and applies them.

Session overrides travel in the launch's configuration snapshot under
``launch`` and are applied after the project configuration, so for that
session they win over both configuration files without being written to
either. A per-option environment variable was the rejected alternative: it
spread one launch's choices over as many spellings as there were settings.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .config import AGENT_EFFORTS, AGENT_PROVIDERS, API_EFFORTS, TrackerConnection, model_entry, parse_env_spec, valid_name
from .errors import AgentBoxError

SPEC = "PROVIDER[@API]:MODEL[:EFFORT]"


@dataclass(frozen=True)
class Option:
    name: str
    group: str
    help: str
    metavar: str = ""
    #: "switch" is --NAME, "negated" only --no-NAME, "pair" both spellings.
    form: str = "switch"
    container: bool = False
    #: Carried into the session snapshot, as opposed to shaping this launch only.
    session: bool = True
    choices: tuple[str, ...] = ()


LAUNCH, WORKFLOW, TRACKER, ACCESS = "Launch", "Workflow for this session", "Tracker for this session", "Access for this session"
OPTIONS = (
    Option("host", LAUNCH, "Run the host's claude/codex, without a container", session=False),
    Option("workspace", LAUNCH, "Workspace root instead of the Git top level", "PATH", session=False),
    Option("mount", LAUNCH, "Also bind-mount a host directory; repeatable", "PATH[:ro]", container=True, session=False),
    Option("env", LAUNCH, "Set NAME, or forward it from this shell by name; repeatable", "NAME[=VALUE]", session=False),
    Option("tmux", LAUNCH, "Start directly, without a detachable tmux session", form="negated", session=False),
    Option("kvm", LAUNCH, "Do not pass /dev/kvm through", form="negated", container=True, session=False),
    Option("worker", WORKFLOW, "Pin the managed worker model", "SPEC"),
    Option("reviewer", WORKFLOW, "Pin the reviewer, whatever review routing and tiers say", "SPEC"),
    Option("scout", WORKFLOW, "Pin the scout model", "SPEC"),
    Option("review", WORKFLOW, "When review is required", "off|auto|always", choices=("off", "auto", "always")),
    Option("rounds", WORKFLOW, "Review attempt limit per task", "N"),
    Option("merge", WORKFLOW, "Delivery route", "github|local", choices=("github", "local")),
    Option("push", WORKFLOW, "Publish after integration", form="pair"),
    Option("workflow", WORKFLOW, "Start without workflow and tracker", form="negated"),
    Option("tracker", TRACKER, "Tracker server; the token is read from VIKUNJA_TOKEN by name", "URL"),
    Option("project", TRACKER, "Tracker project ID", "ID"),
    Option("gh-token", ACCESS, "GitHub token", form="pair", container=True),
    Option("gh-config", ACCESS, "Copy of the host gh configuration", form="pair", container=True),
    Option("git-config", ACCESS, "Copy of the full host Git configuration", form="pair", container=True),
    Option("api-keys", ACCESS, "Provider API key variables", form="pair", container=True),
    Option("browser", ACCESS, "Agent browser", form="pair", container=True),
    Option("remote-control", ACCESS, "Remote control; the --no- form also keeps Claude off Telegram", form="pair"),
    Option("notify", ACCESS, "Telegram notifications; turning them on needs them set up in agent-box config",
           form="pair"),
)
BY_NAME = {option.name: option for option in OPTIONS}
FLAGS = {**{f"--{o.name}": (o, True) for o in OPTIONS if o.form != "negated"},
         **{f"--no-{o.name}": (o, False) for o in OPTIONS if o.form != "switch"}}
WORKFLOW_KEYS = {o.name for o in OPTIONS if o.group == WORKFLOW and o.name != "workflow"}


@dataclass
class LaunchOptions:
    host: bool = False
    workspace: str = ""
    mounts: list[str] = field(default_factory=list)
    env: list[tuple[str, str | None]] = field(default_factory=list)
    tmux: bool = True
    kvm: bool = True
    #: Session overrides by key; the snapshot stores exactly this mapping.
    overrides: dict[str, object] = field(default_factory=dict)

    def given(self):
        """The options used, for refusing those that cannot apply where they were given."""
        used = {"host": self.host, "workspace": self.workspace, "mount": self.mounts,
                "env": self.env, "tmux": not self.tmux, "kvm": not self.kvm}
        names = [name for name, value in used.items() if value] + list(self.overrides)
        return [BY_NAME[name] for name in dict.fromkeys(names)]


def spelling(option):
    return {"switch": f"--{option.name}", "negated": f"--no-{option.name}",
            "pair": f"--[no-]{option.name}"}[option.form]


def parse_spec(flag, text):
    """``PROVIDER[@API]:MODEL[:EFFORT]``; a trailing segment is an effort only if it names one."""
    head, _, rest = text.partition(":")
    provider, at, api = head.partition("@")
    model, colon, effort = rest.rpartition(":")
    if not colon or effort not in (API_EFFORTS if api else AGENT_EFFORTS).get(provider, ()):
        model, effort = rest, "default"
    if provider not in AGENT_PROVIDERS or not model or (at and not valid_name(api)):
        raise AgentBoxError(f"{flag} expects {SPEC}, for example claude:opus:high")
    return [model_entry({"provider": provider, "model": model, "effort": effort, "api_provider": api})]


def convert(option, flag, text):
    if option.choices and text not in option.choices:
        raise AgentBoxError(f"{flag} expects {option.metavar}")
    if option.name in ("worker", "reviewer", "scout"):
        parse_spec(flag, text)
    if option.name == "rounds":
        if not text.isdigit() or int(text) < 1:
            raise AgentBoxError(f"{flag} expects a positive integer")
        return int(text)
    if option.name == "tracker":
        from .repo_binding import server_identity
        return server_identity(text)
    return text


def parse(argv: list[str]) -> tuple[LaunchOptions, list[str]]:
    """Leading agent-box options, and the untouched rest.

    Options end at the first token that is not one of ours or a bare ``--``,
    so an agent's own ``--model`` after ``claude``/``codex``, or directly
    after the options when the default agent starts, is never ours. A
    misspelled option therefore reaches the agent, which rejects it.
    """
    options = LaunchOptions()
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            return options, argv[index + 1:]
        flag, inline, text = token.partition("=")
        option, positive = FLAGS.get(flag, (None, None))
        if option is None:
            break
        if not option.metavar or not positive:
            if inline:
                raise AgentBoxError(f"{flag} takes no value")
            value = positive
        else:
            if not inline:
                index += 1
                text = argv[index] if index < len(argv) else ""
            if not text:
                raise AgentBoxError(f"{flag} requires {option.metavar}")
            value = convert(option, flag, text)
        index += 1
        if option.name == "mount":
            options.mounts.append(value)
        elif option.name == "env":
            try:
                options.env.append(parse_env_spec(value))
            except ValueError as exc:
                raise AgentBoxError(str(exc)) from None
        elif not option.session:
            setattr(options, option.name, value)
        else:
            if option.name in options.overrides:
                raise AgentBoxError(f"{flag} is given twice")
            options.overrides[option.name] = value
    return options, argv[index:]


def apply(config, overrides):
    """Apply session overrides in place; applying the same mapping again changes nothing."""
    workflow = config.workflow
    if "workflow" in overrides and overrides.keys() & {"tracker", "project"}:
        raise AgentBoxError("--tracker and --project contradict --no-workflow")
    for key, value in overrides.items():
        if key in ("worker", "reviewer", "scout"):
            workflow.pins[key] = parse_spec(f"--{key}", value)
        elif key == "review":
            workflow.review.policy = value
        elif key == "rounds":
            workflow.review.max_rounds = value
        elif key == "merge":
            workflow.merge_mode = value
        elif key == "push":
            workflow.auto_push = value
        elif key == "workflow":
            workflow.enabled = config.tracker.enabled = False
        elif key == "tracker":
            config.tracker.connections.setdefault(config.tracker.provider, TrackerConnection()).url = value
        elif key == "gh-token":
            config.github.gh_token = value
        elif key == "gh-config":
            config.github.gh_config = value
        elif key == "git-config":
            config.git.full_config = value
        elif key == "api-keys":
            config.providers.api_keys = value
        elif key == "browser":
            config.browser.enabled = value
        elif key == "remote-control":
            config.claude.remote_control = config.codex.remote_control = value
            config.claude.telegram_channel = config.claude.telegram_channel and value
        elif key == "notify" and not value:
            config.notifications.enabled = False
    used = [key for key in overrides if key in WORKFLOW_KEYS]
    if used and not workflow.enabled:
        raise AgentBoxError(f"--{used[0]} needs workflow; enable it in agent-box config"
                            + (" and drop --no-workflow" if "workflow" in overrides else ""))
    tracker_keys = [key for key in ("tracker", "project") if key in overrides]
    if tracker_keys and not config.tracker.enabled:
        # A session may point the tracker elsewhere, never open a gate the saved configuration keeps closed.
        raise AgentBoxError(f"--{tracker_keys[0]} needs the tracker; enable it in agent-box config")
    if overrides.get("browser") and not config.image.tools.get("browser"):
        raise AgentBoxError("--browser needs the browser image tool group; enable it in agent-box config, then agent-box build")
    for role, models in workflow.pins.items():
        api = models[0]["api_provider"]
        if api and not (api in config.external_providers and config.external_providers[api].enabled):
            raise AgentBoxError(f"--{role} names API provider {api}, which is not an enabled external provider")
    errors = workflow.validate() if used else []
    if errors:
        raise AgentBoxError(errors[0])
    config._launch = dict(overrides)
    return config


def bind_tracker_account(config, url, environ=None):
    """Put the ``--tracker`` account beneath the project overlay, where the snapshot renders it from.

    The effective provider decides, since the project may select one the
    saved configuration does not. The token is read by name from the
    launching shell, so its value never enters an argv.
    """
    environ = os.environ if environ is None else environ
    if config.tracker.provider != "vikunja":
        raise AgentBoxError(f"--tracker supports the vikunja tracker; this configuration selects {config.tracker.provider}")
    token = environ.get("VIKUNJA_TOKEN", "")
    if not token.strip():
        raise AgentBoxError("--tracker needs VIKUNJA_TOKEN exported in this shell; the token is never passed as an argument")
    for target in {id(c): c for c in (getattr(config, "_project_base", config), config)}.values():
        target.tracker.connections["vikunja"] = TrackerConnection(url, token)


def describe(overrides):
    """The session's overrides as the flags that set them."""
    parts = []
    for key, value in overrides.items():
        parts.append(f"--{key} {value}" if BY_NAME[key].metavar else
                     f"--{key}" if value is True else f"--no-{key}")
    return " ".join(parts)


def session_overrides():
    """This session's launch overrides, read from its snapshot; empty outside a session."""
    from .atomicio import read_json
    path = os.environ.get("AGENT_BOX_RUNTIME_CONFIG")
    try:
        launch = read_json(Path(path)).get("launch") if path else None
    except (OSError, ValueError, AttributeError):
        return {}
    return launch if isinstance(launch, dict) else {}


def help_text():
    lines = []
    for group in (LAUNCH, WORKFLOW, TRACKER, ACCESS):
        lines += ["", f"{group}:"]
        for option in (o for o in OPTIONS if o.group == group):
            usage = f"{spelling(option)} {option.metavar}".rstrip()
            suffix = " (container)" if option.container else ""
            if len(usage) > 22:
                lines += [f"  {usage}", f"  {'':22}  {option.help}{suffix}"]
            else:
                lines.append(f"  {usage:22}  {option.help}{suffix}")
    return "\n".join(lines[1:])
