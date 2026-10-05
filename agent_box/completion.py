"""Shell completion: thin bash/zsh scripts ask ``agent-box _complete`` on every Tab.

Candidates come from the structures that parse the command line -- the launch
option table, the command table below and the argparse parsers of ``tracker``
and ``workflow`` -- so a new option or subcommand completes without touching
the shell scripts. A generated static script was the rejected alternative: it
restated every command in two shell languages and drifted from the code.

``_complete`` prints a mode line (``words``, ``files`` or ``dirs``), then one
``WORD[<TAB>DESCRIPTION]`` line per candidate matching the current word. For
``files`` and ``dirs`` the shell completes paths itself.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .launch_options import FLAGS

SHELLS = ("bash", "zsh")

HELP = """Usage: agent-box completion bash|zsh

Print a completion script for the shell. agent-box computes the candidates on
every Tab, so new options and commands complete without regenerating it.

  bash, this shell:   source <(agent-box completion bash)
  bash, every shell:  echo 'source <(agent-box completion bash)' >> ~/.bashrc
  zsh, this shell:    source <(agent-box completion zsh)
  zsh, every shell:   echo 'source <(agent-box completion zsh)' >> ~/.zshrc

zsh needs compinit loaded before the line; bash needs no bash-completion package."""


def script(shell: str) -> str:
    return Path(__file__).with_name(f"completion.{shell}").read_text(encoding="utf-8")


def fixed(*choices):
    """A command whose few arguments are words; a word already given is not offered again."""
    return lambda args: ("words", [(choice, "") for choice in choices if choice not in args[:-1]])


def files(args):
    return "files", []


def argparse_command(load):
    return lambda args: parser_candidates(load(), args)


def tracker_parser():
    from .tracker.cli import parser
    return parser()


def workflow_parser():
    from .workflow import parser
    return parser()


def external_command(args):
    """``ext API [claude|codex] [AGENT ARGS...]``; API names are the enabled external providers."""
    if len(args) == 1:
        from .config import effective_config
        config = effective_config(include_project=False)
        return "words", [(name, "") for name, spec in config.agents.external_apis.items() if spec.enabled]
    if len(args) == 2:
        return "words", [("claude", ""), ("codex", "")]
    return files(args)


def config_words(args):
    """``config`` subcommands, then the arguments of the one already given."""
    if len(args) > 1 and args[0] == "set":
        once = ("--project", "--global", "--dry-run")
        return "words", [(word, "") for word in (*once, "--reset") if word == "--reset" or word not in args[1:-1]]
    if len(args) > 1 and args[0] in ("proposal", "ai"):
        return fixed(*(("apply", "discard") if args[0] == "proposal" else ("claude", "codex")))(args[1:])
    return fixed("--project", "show", "describe", "set", "proposal", "ai", "check", "apply")(args)


#: Every command ``cli.main`` dispatches, except HIDDEN; a test fails when one is missing.
COMMANDS = {
    "claude": files, "codex": files, "shell": fixed(), "doctor": fixed(), "ext": external_command,
    "attach": fixed(), "stop": fixed("--cluster-data"),
    "config": config_words,
    "usage": fixed("codex", "claude", "all", "--no-cache", "--json"),
    "notify": fixed("test"),
    "build": fixed(), "update": fixed(), "rebuild": fixed(),
    "upgrade": fixed("--check"), "uninstall": fixed("--purge", "--yes"),
    "tracker": argparse_command(tracker_parser), "workflow": argparse_command(workflow_parser),
    "completion": fixed(*SHELLS), "selftest": fixed(), "help": fixed(),
}
#: Hook entry points and help spellings: dispatched, but not something a person types.
HIDDEN = {"statusline", "-h", "--help"}


def option_values(option, environ):
    if option.metavar.startswith("PATH"):
        return "dirs", []
    if option.metavar.startswith("NAME"):
        return "words", [(name, "") for name in sorted(environ)]
    # `off|auto|always` offers its literal alternatives, never a placeholder such as SPEC.
    return "words", [(choice, "") for choice in option.choices or option.metavar.split("|") if choice.islower()]


def complete(words, environ):
    """Mode and candidates for the last of ``words`` (the arguments after ``agent-box``)."""
    *done, current = words
    index, pending = 0, None
    while index < len(done):
        token = done[index]
        if token == "--":
            return after_options(done[index + 1:] + [current])
        flag, inline, _ = token.partition("=")
        option, positive = FLAGS.get(flag, (None, None))
        if option is None:
            return after_options(done[index:] + [current])
        pending = option if option.metavar and positive and not inline else None
        index += 2 if pending else 1
    if index > len(done):
        return option_values(pending, environ)
    flag, inline, _ = current.partition("=")
    option, positive = FLAGS.get(flag, (None, None))
    if inline and option and option.metavar and positive:
        mode, values = option_values(option, environ)
        return mode, [(f"{flag}={value}", description) for value, description in values]
    if current.startswith("-"):
        return "words", [(flag, option.help) for flag, (option, _) in FLAGS.items()]
    return after_options([current])


def after_options(args):
    if len(args) == 1:
        return "words", [(name, "") for name in COMMANDS]
    handler = COMMANDS.get(args[0])
    # Anything else starts the default agent, whose arguments are its own.
    return handler(args[1:]) if handler else files(args)


def action_values(action, otherwise):
    if action.choices is None:
        return otherwise
    return "words", [(str(choice), "") for choice in action.choices]


def parser_candidates(parser, args):
    """Walk an argparse parser the way it would parse ``args``, then offer what may come next.

    Reads argparse's ``_actions``, ``_SubParsersAction`` and ``_choices_actions``:
    private, but unchanged for many Python releases, and the only way to learn
    a parser's shape without a second description of every command.
    """
    *done, current = args
    optionals = {flag: action for action in parser._actions for flag in action.option_strings}
    positionals = [action for action in parser._actions if not action.option_strings]
    pending = None
    repeated = 0
    for index, token in enumerate(done):
        if pending:
            pending = None
            continue
        if token == "--":
            # Only a wrapped command follows the separator (`workflow validate ID -- CMD`).
            return files(args)
        if token.startswith("-") and token != "-":
            flag, inline, _ = token.partition("=")
            action = optionals.get(flag)
            pending = action if action is not None and action.nargs != 0 and not inline else None
            continue
        if not positionals:
            continue
        action = positionals[0]
        if isinstance(action, argparse._SubParsersAction):
            sub = action.choices.get(token)
            return parser_candidates(sub, done[index + 1:] + [current]) if sub else ("words", [])
        if action.nargs == argparse.REMAINDER:
            return files(args)
        if action.nargs in ("*", "+"):
            repeated += 1
        else:
            positionals.pop(0)
    if pending:
        return action_values(pending, files(args))
    if current.startswith("-"):
        flag, inline, _ = current.partition("=")
        action = optionals.get(flag)
        if inline and action is not None and action.nargs != 0:
            mode, values = action_values(action, files(args))
            return mode, [(f"{flag}={value}", description) for value, description in values]
        return "words", [(flag, action.help or "") for flag, action in optionals.items()
                         if flag.startswith("--") and action.help != argparse.SUPPRESS]
    if not positionals:
        return "words", []
    action = positionals[0]
    if isinstance(action, argparse._SubParsersAction):
        # `choices` holds every subcommand; `_choices_actions` only those added with help=.
        helps = {choice.dest: choice.help or "" for choice in action._choices_actions}
        return "words", [(name, helps.get(name, "")) for name in action.choices]
    if action.nargs == argparse.REMAINDER:
        return files(args)
    if action.nargs in ("*", "+") and len(positionals) > 1 and repeated:
        # Once it has a value, a repeated positional may end at any later word, so
        # the positional after it completes this one just as legitimately - unless
        # that word has already been typed, which its own choices reveal.
        follower = positionals[1]
        if not (follower.choices and done and done[-1] in follower.choices):
            return action_values(follower, action_values(action, ("words", [])))
    return action_values(action, ("words", []))


def main(words, environ=None, out=None) -> None:
    out = sys.stdout if out is None else out
    words = words or [""]
    try:
        mode, candidates = complete(words, os.environ if environ is None else environ)
    except Exception:
        # A Tab press must never print a traceback into the prompt; offering nothing is the failure mode.
        return
    lines = [mode]
    for word, description in dict(candidates).items():
        if word.startswith(words[-1]):
            lines.append(f"{word}\t{description}" if description else word)
    out.write("\n".join(lines) + "\n")
