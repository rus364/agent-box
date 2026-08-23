"""Flow-Next workspace detection and deterministic runtime policy.

This module owns three small, side-effect-free decisions:

* whether the current workspace is a Flow-Next repository at all
  (:func:`is_flow_next_workspace`);
* which ``FLOW_REVIEW_BACKEND`` (and auto-push signal) a launch of
  ``claude``/``codex`` should receive (:func:`flow_next_launch_env`); and
* which Flowctl executable belongs to the agent being launched
  (:func:`resolve_flowctl`), so the auto-push reconciler (see
  ``agent_box.flow_push``) never has to guess it later or fall back to
  ``shutil.which("flowctl")`` -- upstream Flow-Next explicitly documents that
  a global ``flowctl`` on ``PATH`` is not guaranteed.

Primary-agent selection stays entirely explicit (``agent-box claude`` /
``agent-box codex``); nothing here inspects quota, balances providers, or
falls back between them. Container mode and host mode both call
:func:`flow_next_launch_env` so the policy cannot drift between the two.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from .atomicio import read_json
from .config import Config
from .paths import CLAUDE_HOME, CODEX_HOME

#: Set by the launcher (container or host) for a Claude/Codex launch when the
#: workspace is Flow-Next and ``flow_next.auto_push`` is enabled. The shared
#: auto-push reconciler (see ``agent_box.flow_push``) reads this to know,
#: deterministically, whether auto-push applies to *this* launch -- never by
#: guessing from prose or from whatever config happens to be on disk later.
AUTO_PUSH_ENV = "AGENT_BOX_FLOW_NEXT_AUTO_PUSH"

#: The absolute Flowctl executable resolved for this launch (see
#: :func:`resolve_flowctl`), forwarded so the reconciler never has to
#: re-derive it inside the container/host process that actually runs the
#: Stop/turn-complete hook. Absent when auto-push is off, or when a Flowctl
#: executable could not be determined for this agent/workspace.
FLOWCTL_ENV = "AGENT_BOX_FLOWCTL"

#: Injected into a Flow-Next launch to select the review backend. Flow-Next's
#: own precedence is: task/spec override > this env var > .flow/config.json
#: review.backend > backend-specific default. agent-box's configured policy
#: fills this in when the launching shell didn't already set it explicitly --
#: an explicit ``FLOW_REVIEW_BACKEND`` in the environment agent-box was
#: invoked from wins for that one launch (see :func:`flow_next_launch_env`).
REVIEW_BACKEND_ENV = "FLOW_REVIEW_BACKEND"

#: Every environment variable agent-box's Flow-Next policy owns. A launcher
#: that starts from an inherited environment (host mode) must clear all of
#: these before applying :func:`flow_next_launch_env`'s decision, so a stale
#: value from the parent shell can never survive into a launch the resolver
#: decided should get none (e.g. a non-Flow-Next workspace).
POLICY_ENV_VARS = (REVIEW_BACKEND_ENV, AUTO_PUSH_ENV, FLOWCTL_ENV)


def is_flow_next_workspace(workspace_root: Path) -> bool:
    """Whether ``workspace_root`` is a Flow-Next repository.

    The marker is simply the presence of ``.flow/config.json``. This never
    invokes ``flowctl``, never mutates the project, and is cheap enough to
    call before launching either the container or a host-mode agent.
    """
    return (Path(workspace_root) / ".flow" / "config.json").is_file()


def review_backend_for(agent: str, config: Config) -> str | None:
    """The configured Flow-Next review backend spec for ``agent``, if any."""
    return config.flow_next.review_backends.get(agent)


def _executable_flowctl(candidate: Path) -> Path | None:
    return candidate if candidate.is_file() and os.access(candidate, os.X_OK) else None


def _claude_flowctl() -> Path | None:
    """The Flowctl belonging to the currently installed ``flow-next`` Claude
    plugin.

    A running Claude session resolves its own plugin scripts through
    ``$CLAUDE_PLUGIN_ROOT``, an env var Claude Code sets only *inside* a
    loaded skill/agent -- agent-box's launcher runs before that and has no
    such variable to read. Claude Code itself decides ``CLAUDE_PLUGIN_ROOT``
    from the plugin's own install-path record in
    ``<CLAUDE_HOME>/plugins/installed_plugins.json`` (schema: ``{"plugins":
    {"<plugin>@<marketplace>": [{"scope", "installPath", ...}, ...]}}``), so
    reading that same record here reproduces the identical answer
    deterministically, without a filesystem search and without ever
    launching Claude just to ask it.

    Matches any marketplace whose plugin name (the part before ``@``) is
    ``flow-next`` -- the marketplace name is not agent-box's concern -- and
    returns the first entry (in the file's own order) whose install path
    actually has an executable ``scripts/flowctl``.
    """
    manifest_path = CLAUDE_HOME / "plugins" / "installed_plugins.json"
    try:
        data = read_json(manifest_path)
    except (OSError, ValueError):
        return None
    plugins = data.get("plugins") if isinstance(data, dict) else None
    if not isinstance(plugins, dict):
        return None
    for key, entries in plugins.items():
        if not isinstance(key, str) or key.split("@", 1)[0] != "flow-next":
            continue
        if not isinstance(entries, list):
            continue
        for entry in entries:
            install_path = entry.get("installPath") if isinstance(entry, dict) else None
            if not isinstance(install_path, str) or not install_path:
                continue
            flowctl = _executable_flowctl(Path(install_path) / "scripts" / "flowctl")
            if flowctl is not None:
                return flowctl
    return None


def _codex_flowctl() -> Path | None:
    """The Flowctl belonging to the current Codex Flow-Next install.

    Upstream Flow-Next's Codex mirror resolves
    ``${CODEX_HOME:-$HOME/.codex}/scripts/flowctl`` first, ahead of any other
    rung; ``CODEX_HOME`` (see ``agent_box.paths``) already honors that same
    override, so this is the identical, deterministic answer.
    """
    return _executable_flowctl(CODEX_HOME / "scripts" / "flowctl")


def resolve_flowctl(agent: str) -> Path | None:
    """The absolute Flowctl executable for ``agent``'s currently installed
    Flow-Next plugin, or ``None`` if it cannot be determined.

    Deliberately small and deterministic -- never a broad filesystem search,
    and never ``shutil.which("flowctl")``: upstream Flow-Next explicitly
    documents that a global ``flowctl`` on ``PATH`` is not guaranteed
    (copy-less installs run it from the active plugin/CLI install instead).
    An undeterminable Flowctl means the auto-push reconciler skips rather
    than guesses (see ``agent_box.flow_push``).
    """
    if agent == "claude":
        return _claude_flowctl()
    if agent == "codex":
        return _codex_flowctl()
    return None


def flow_next_launch_env(
    workspace_root: Path,
    agent: str,
    config: Config,
    host_env: Mapping[str, str] | None = None,
) -> list[str]:
    """``KEY=VALUE`` environment overrides for a Flow-Next launch.

    Empty for a non-Flow-Next workspace, or for an agent other than
    ``claude``/``codex``, so ordinary agent-box behavior is unchanged for
    every project that isn't Flow-Next and for ``shell``/``doctor`` runs.

    ``host_env`` (defaulting to the real process environment) is consulted
    for an explicit ``FLOW_REVIEW_BACKEND``: if the shell that invoked
    agent-box set one, it wins for this single launch over the configured
    ``flow_next.review_backends`` entry. This is what lets a one-shot
    ``FLOW_REVIEW_BACKEND=... agent-box claude`` override the configured
    reviewer without agent-box needing a second, redundant env var.

    ``flow_next.auto_push`` applies identically to Claude and Codex launches:
    when it is on, :data:`AUTO_PUSH_ENV` is set so the shared auto-push
    reconciler knows this launch's policy allows it, and
    :func:`resolve_flowctl` is consulted so :data:`FLOWCTL_ENV` carries the
    absolute executable the reconciler should use -- omitted (not guessed)
    when it cannot be determined for this agent/host.
    """
    if agent not in ("claude", "codex"):
        return []
    if not is_flow_next_workspace(workspace_root):
        return []
    if host_env is None:
        host_env = os.environ
    env: list[str] = []
    backend = host_env.get(REVIEW_BACKEND_ENV) or review_backend_for(agent, config)
    if backend:
        env.append(f"{REVIEW_BACKEND_ENV}={backend}")
    if config.flow_next.auto_push:
        env.append(f"{AUTO_PUSH_ENV}=1")
        flowctl = resolve_flowctl(agent)
        if flowctl is not None:
            env.append(f"{FLOWCTL_ENV}={flowctl}")
    return env
