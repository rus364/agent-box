"""Flow-Next workspace detection and deterministic runtime policy.

This module owns exactly two small, side-effect-free decisions:

* whether the current workspace is a Flow-Next repository at all
  (:func:`is_flow_next_workspace`), and
* which ``FLOW_REVIEW_BACKEND`` (and, for Claude, auto-push signal) a launch
  of ``claude``/``codex`` should receive (:func:`flow_next_launch_env`).

Primary-agent selection stays entirely explicit (``agent-box claude`` /
``agent-box codex``); nothing here inspects quota, balances providers, or
falls back between them. Container mode and host mode both call
:func:`flow_next_launch_env` so the policy cannot drift between the two.
"""
from __future__ import annotations

from pathlib import Path

from .config import Config

#: Set by the launcher (container or host) for a Claude launch when the
#: workspace is Flow-Next and ``flow_next.auto_push`` is enabled. The
#: managed Claude hooks (see ``agent_box.flow_push``) read this to know,
#: deterministically, whether auto-push applies to *this* launch -- never by
#: guessing from prose or from whatever config happens to be on disk later.
AUTO_PUSH_ENV = "AGENT_BOX_FLOW_NEXT_AUTO_PUSH"

#: Injected into a Flow-Next launch to select the review backend. Flow-Next's
#: own precedence is: task/spec override > this env var > .flow/config.json
#: review.backend > backend-specific default. Setting it here makes
#: agent-box's configured policy authoritative for the launch rather than
#: merely a suggestion a stale host value could defeat.
REVIEW_BACKEND_ENV = "FLOW_REVIEW_BACKEND"

#: Every environment variable agent-box's Flow-Next policy owns. A launcher
#: that starts from an inherited environment (host mode) must clear all of
#: these before applying :func:`flow_next_launch_env`'s decision, so a stale
#: value from the parent shell can never survive into a launch the resolver
#: decided should get none (e.g. a non-Flow-Next workspace).
POLICY_ENV_VARS = (REVIEW_BACKEND_ENV, AUTO_PUSH_ENV)


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


def flow_next_launch_env(workspace_root: Path, agent: str, config: Config) -> list[str]:
    """``KEY=VALUE`` environment overrides for a Flow-Next launch.

    Empty for a non-Flow-Next workspace, or for an agent other than
    ``claude``/``codex``, so ordinary agent-box behavior is unchanged for
    every project that isn't Flow-Next and for ``shell``/``doctor`` runs.
    """
    if agent not in ("claude", "codex"):
        return []
    if not is_flow_next_workspace(workspace_root):
        return []
    env: list[str] = []
    backend = review_backend_for(agent, config)
    if backend:
        env.append(f"{REVIEW_BACKEND_ENV}={backend}")
    if agent == "claude" and config.flow_next.auto_push:
        env.append(f"{AUTO_PUSH_ENV}=1")
    return env
