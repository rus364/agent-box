"""A dependency-free terminal form; no writes occur until Save and apply."""
from __future__ import annotations

import copy
import json
import sys
import textwrap
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .config import AGENT_EFFORTS, AGENT_PROVIDERS, K3S_LIFETIMES, TOOL_GROUP_HELP, LAUNCH_AGENTS, Config, CredentialsConfig, EnvConfig, REVIEW_INCREMENTAL, REVIEW_PRESETS, TRACKER_PROVIDERS, TrackerConnection, candidate_label, model_entry
from . import api_models
from .errors import AgentBoxError
from .external_config import API_EFFORTS, EFFORTS, ExternalProvider, cli_models, model_defaults, model_settings, valid_model, valid_name, validate_connections
from .model_catalog import load_model_catalogs

CHOICES = {
    "agents.default": LAUNCH_AGENTS,
    "tracker.provider": TRACKER_PROVIDERS,
    "workflow.merge_mode": ("github", "local"),
    "workflow.sync.policy": ("off", "auto", "always"),
    "workflow.sync.scope": ("parent", "project"),
    "workflow.review.policy": ("off", "auto", "always"),
    "workflow.review.incremental": REVIEW_INCREMENTAL,
    "workflow.review.github_provider": ("auto", "local", "codex", "claude"),
    "box.k3s.lifetime": K3S_LIFETIMES,
}
HELP = {
    "agents": "Which agent starts, how Claude and Codex run, and which provider APIs and host variables they may use.",
    "box": "What the workspace box is built with, the k3s cluster beside it, sound, and its environment.",
    "credentials": "Host identity and credentials the box's own tools may use. Everything that exposes a credential is off by default.",
    "host": "What agent-box does on the host itself: notifications, Claude skill links and the PATH symlink. Global only.",
    "workflow.language": ("Working language of project text, such as English or Deutsch: tracker tasks and comments, pull requests and "
                 "review replies, commit messages, code comments and documentation. Conversation with you stays in your "
                 "language; agent handoffs and agent-box output stay English. Empty keeps each text's current rule. "
                 "A project may override it."),
    "agents.default": "Agent started by `agent-box` without claude/codex; an explicit claude or codex always wins. Global only; not a project setting.",
    "host.bin_dir": "Directory holding the agent-box PATH symlink; empty means agent-box manages none. `agent-box config apply` restores the link here.",
    "agents.claude": "Claude session settings, including --host, and its status line. Global only.",
    "agents.claude.telegram_channel": "Start Claude with Anthropic's official Telegram channel plugin. This requires its host-side setup and exposes the session through that channel.",
    "agents.claude.remote_control": "Request Remote Control for new interactive Claude sessions, including --host. Off preserves native behavior. Resume and explicit settings use the ordinary launch. Requires eligible Claude login.",
    "agents.claude.statusline": "Claude's status line and its extra usage segment. Global only; changes apply after Save and apply.",
    "agents.claude.statusline.enabled": "Keep Claude's status line pointed at agent-box. An existing user-owned status line is never overwritten.",
    "agents.claude.statusline.command": "Extra segment in Claude's status line and `agent-box usage all`: shell command whose first output line is shown after Codex usage; empty shows nothing. Runs where the line is drawn (Claude's status line runs inside the box), cached 60s, 5s timeout, failures show —. Pass keys via environment, not in the command.",
    "agents.codex": "Codex interactive session settings, including --host.",
    "agents.codex.remote_control": "Request Remote Control through a private app-server; one agent-box RC session per CODEX_HOME. Unsupported launches keep the ordinary CLI. Off leaves native behavior unchanged.",
    "agents.codex.native_agent_wait": "Cache-aware waiting for native Codex subagents in tracked workflow sessions. Global only.",
    "agents.codex.native_agent_wait.enabled": "Set the root coordinator's default wait interval for native subagents. Off leaves Codex's native wait default unchanged.",
    "agents.codex.native_agent_wait.interval_seconds": "Seconds between coordinator turns while native subagents are still running, from 10 through 3600. Default 1680 keeps each turn inside the 30-minute prompt-cache guarantee.",
    "host.notifications": "Outbound Telegram completion notifications. The bot token and chat ID stay disabled until this section is enabled and fully configured.",
    "host.notifications.enabled": "Send outbound completion notifications through Telegram when a bot token and chat ID are configured.",
    "host.notifications.telegram_token": "Telegram bot token used only for outbound notifications. The value is masked in the menu and saved in the protected config file.",
    "host.notifications.telegram_chat_id": "Telegram destination chat ID for outbound notifications. It has no effect while notifications are disabled.",
    "credentials.github": "Choose which host GitHub credentials, if any, are exposed inside workspace boxes.",
    "credentials.github.token": "Read the host gh token and forward it as GH_TOKEN. Disabled by default because it exposes a live credential to the box.",
    "credentials.github.config": "Copy the host gh configuration into the box. Disabled by default because that directory normally contains authentication data.",
    "credentials.registries": "Forward the container registry and Helm repository logins you choose from the host's own login files. Copies are re-rendered at every launch, so a login renewed on the host reaches the box at its next launch. Off by default; --[no-]registries overrides it for one session.",
    "credentials.registries.enabled": "Forward the chosen logins into workspace boxes, read-only. Disabled by default because they reach private images and charts. Changing it recreates the box.",
    "credentials.registries.hosts": "Registry hosts found in the podman/skopeo and Helm OCI auth files on the host. Each source is filtered into its own copy; logins kept by a credential helper cannot be forwarded.",
    "credentials.registries.helm_repositories": "Helm repositories found in the host's repositories.yaml, forwarded with their credentials. The box keeps its own index cache; refresh it with helm repo update.",
    "credentials.kubernetes": "Forward the kubeconfig files you choose from the host's ~/.kube into the box's ~/.kube, each whole, unmerged and under its own name; the box's ~/.kube is then read-only. KUBECONFIG is left to k3s. Copies are re-rendered at every launch. Off by default; --[no-]kubeconfigs overrides it for one session.",
    "credentials.kubernetes.enabled": "Forward the chosen kubeconfigs into workspace boxes, read-only. Disabled by default because a kubeconfig reaches a real cluster. Turning it on with files chosen also ticks the kubernetes tool group. Changing it recreates the box.",
    "credentials.kubernetes.files": "Kubeconfigs found in the host's ~/.kube, symlinks included, each marked with how it authenticates. exec: yc works once its yc profile is forwarded in Cloud CLI profiles; other exec plugins do not run inside the box. A chosen config is kubectl's default cluster unless k3s is on, which keeps its own as the default.",
    "credentials.cloud": "Forward the cloud CLI profiles you choose from the host into the box, each with its sign-in: the agent can then do everything those accounts may do in the cloud, not only reach one cluster. Re-rendered at every launch; nothing runs or changes on the host. Off by default; --[no-]cloud-profiles overrides it for one session.",
    "credentials.cloud.enabled": "Forward the chosen profiles into workspace boxes. Disabled by default because a profile reaches a whole cloud account. Turning it on with profiles chosen also ticks their tool group. Changing it recreates the box.",
    "credentials.cloud.yc": "Yandex Cloud CLI profiles from the host's ~/.config/yandex-cloud. The box gets a config.yaml with only these profiles and their credentials as its writable ~/.config/yandex-cloud; history and logs stay on the host.",
    "credentials.cloud.yc.profiles": "Profiles found in the host's yc config, each marked with how it signs in. A federated profile's refresh token is forwarded too, so yc in the box renews IAM tokens for up to 31 days; after that, sign in again with yc on the host. Choosing one ticks the yc tool group.",
    "credentials.git": "Configure how much host Git configuration is copied into workspace boxes.",
    "credentials.git.identity": "Copy only user.name and user.email for commit attribution. This exposes no Git credential settings.",
    "credentials.git.full_config": "Copy the complete host Git configuration, which may include credential helpers, headers, URL rewrites, and included files.",
    "agents.external_apis": "Named external APIs, such as DeepSeek or OpenRouter. Enable explicitly to expose their keys to agent-box sessions. One model list serves both CLIs, each with its own URL; opening a provider loads the models it publishes. `ext NAME` without claude/codex uses the default agent.",
    "agents.host_env": "Control which provider-related environment variables are forwarded from the host.",
    "agents.host_env.api_keys": "Forward known Claude and Codex API-key variables by name. Disabled by default because these values are credentials.",
    "agents.host_env.transport": "Forward provider endpoint, proxy, CA, Bedrock, and Vertex transport variables. Proxy URLs may themselves contain credentials.",
    "box.env": "Additional per-launch environment policy. Forward secrets by variable name; literal values are stored in config and exposed in process arguments.",
    "box.env.forward": "JSON list of host variable names to forward without putting their values in process arguments. Use this form for secrets.",
    "box.env.set": "JSON object of literal environment values. These values are stored in config and appear in Podman arguments, so do not put secrets here.",
    "host.claude_skill_links": "Link agent-box's managed shared skills into Claude's skill directory when applying configuration.",
    "box.tools": "Tool groups baked into the workspace image. Each group changes box identity and may make the image larger; a changed selection takes effect after the image is rebuilt.",
    "box.k3s": "Per-project k3s cluster in a sidecar container of the host's rootless Podman; the box gets kubectl access, never extra privileges. Not in the image and not available in --host mode.",
    "box.k3s.enabled": "Run the cluster and give the box a kubeconfig. The agent becomes cluster-admin; privileged pods reach only the sidecar, never host files. kubectl comes from the kubernetes group. Changes box identity.",
    "box.k3s.lifetime": "persistent keeps the cluster running until `agent-box stop`, so deployments survive between sessions. box stops it when agent-box removes the box, or at the next agent-box command for the project if the box ended on its own.",
    "box.k3s.persistent_data": "Keep the cluster's volume when the sidecar stops. Off deletes it at every stop, so each start is a clean cluster. `agent-box stop --cluster-data` deletes it on demand.",
    "box.k3s.traefik": "Run k3s's Traefik ingress controller. Ingresses are reachable inside the cluster or through kubectl port-forward; nothing is published on the host. Recreates the sidecar on the same data.",
    "box.k3s.metrics_server": "Run metrics-server for kubectl top and HorizontalPodAutoscalers. Recreates the sidecar on the same data.",
    "box.k3s.servicelb": "Run k3s's ServiceLB so LoadBalancer services, Traefik's included, get an address on the node. Off leaves them pending. Recreates the sidecar on the same data.",
    "box.k3s.local_storage": "Run the local-path provisioner, the default StorageClass that binds PVCs to the cluster's volume. Recreates the sidecar on the same data.",
    "box.k3s.network_policy": "Enforce NetworkPolicy with k3s's embedded controller. Recreates the sidecar on the same data.",
    "box.k3s.cpus": "CPU limit of the sidecar, such as 2 or 1.5; empty means no limit. Recreates the sidecar on the same data.",
    "box.k3s.memory": "Memory limit of the sidecar, such as 4g or 512m; empty means no limit. Pods beyond it are OOM-killed inside the cluster. Recreates the sidecar on the same data.",
    "box.k3s.ports": "JSON list of HOST:CONTAINER pairs published on the host's 127.0.0.1 only, such as [\"8080:80\"] to open Traefik's ingress in your browser. Never exposed to the LAN. Recreates the sidecar on the same data.",
    "box.k3s.manifests": "Project-relative directory k3s applies at start and on every change (YAML and HelmChart resources), mounted read-only; empty mounts nothing. Must stay inside the project. Recreates the sidecar on the same data.",
    "box.k3s.registry_mirrors": "JSON object of registry name to mirror URLs, such as {\"docker.io\": [\"https://mirror.example\"]}, rendered into k3s's registries.yaml. No credentials: URLs with a user or password are refused. Recreates the sidecar on the same data.",
    "box.k3s.extra_args": "JSON list of extra `k3s server` flags, each starting with --, added after agent-box's own, such as [\"--cluster-cidr=10.60.0.0/16\"]. Recreates the sidecar on the same data.",
    "box.k3s.ready_timeout": "Seconds a launch waits for a Ready node before failing with the sidecar's log, from 30 through 1800.",
    "box.k3s.version": "rancher/k3s tag such as v1.33.5-k3s1; empty uses the release agent-box pins by digest. k3s cannot downgrade existing data: use `agent-box stop --cluster-data` first.",
    "tracker": "Expose configured task-tracker connections to agents. Connections remain unavailable while this integration is disabled.",
    "tracker.enabled": "Expose configured tracker connections to agents. Keep credentials out of repository files.",
    "tracker.provider": "Default tracker for repositories without a more specific binding. Choose one of the supported adapters.",
    "tracker.project": "Project tracker identity: the tracker project ID this repository's tasks belong to. Project setting only; setup and adopt write it.",
    "tracker.recursive": "Project tracker identity: include every project under the bound project in the workflow. Project setting only.",
    "tracker.connections": "Provider URLs and tokens rendered into minimal runtime credential files only when tracker integration is enabled.",
    "box.audio": "Mount the host PulseAudio/PipeWire socket so agents can use the microphone and speakers, e.g. Claude Code /voice. Everything in the box gets both. Changing it recreates the box.",
    "box.links": "Web links and temporary localhost callback routes from the box to your workstation. Separate from the agent's Playwright browser.",
    "box.links.enabled": "Expose a host URL broker to the shared box, with temporary loopback forwarding for CLI login callbacks. Off by default; changing it recreates the box. Remote workstations also need the displayed SSH/Coder tunnel.",
    "box.links.open_browser": "Open links with the host's desktop browser after preparing callback forwarding. Off by default. SSH, Coder and headless launches always display instructions instead.",
    "box.links.ssh_destination": "SSH alias or user@server used in workstation tunnel instructions. Your local SSH config can supply ProxyJump. Empty uses SSH_CONNECTION when available, otherwise a USER@SERVER placeholder.",
    "box.links.coder_workspace": "Coder workspace name used in workstation port-forward instructions. Empty uses CODER_WORKSPACE_NAME when available.",
    "workflow": "Opt-in tracked-work orchestration, role models, review, publication, and cleanup policy.",
    "workflow.enabled": "Enable agent-box's tracked-task workflow. Enabling it does not itself expose provider or tracker credentials.",
    "workflow.use_tiers": "Let a task's tier 1-3 select worker, reviewer and Plan sync Models by tier. Off runs each role's own Models; tier lists and task tiers are kept. A project or --[no-]tiers may override it.",
    "workflow.merge_mode": "Finish parent work through a GitHub pull request or a local fast-forward into origin's default branch.",
    "workflow.scout": "Read-only scouts the coordinator dispatches for bounded questions, and the models they run.",
    "workflow.scout.limit": "Maximum number of concurrent read-only scouts, from 1 through 16.",
    "workflow.scout.models": "Scout candidates in preference order; the first available one runs and the rest are fallbacks. Empty: the coordinator researches itself in its own session. Scouts have no tiers. A candidate of the other CLI than the coordinator's runs as a managed CLI process.",
    "workflow.auto_push": "Within the opt-in workflow, publish the parent task in GitHub mode or the accepted base after a local fast-forward. Turning this off keeps completion local.",
    "workflow.timeout_seconds": "Maximum delegated agent runtime in seconds, from 30 through 86400.",
    "workflow.worker": "The models managed workers run when the coordinator delegates with workflow delegate.",
    "workflow.worker.models": "Delegated worker candidates in preference order; the first available one runs and the rest are fallbacks. Empty: the coordinator implements in its own session. Used unless Use tiers is on and the task's tier has its own list. A candidate of the other CLI than the coordinator's runs as a managed CLI process.",
    "workflow.worker.tiers": "Optional worker candidates per task tier 1-3, used only when Use tiers is on. A nonempty tier list replaces Models for that tier; an empty one uses Models.",
    "workflow.sync": "Plan sync reads the repository and updates live tracker tasks; it never edits project files.",
    "workflow.sync.policy": "Off disables automatic runs. Always runs at every selected event; auto runs before work/after planning and after changes matching Paths. Manual sync works in every mode, independently of review.",
    "workflow.sync.events": "Events that start automatic Plan sync: before (before implementation), after (after integration), plan (after planning or an accepted decision).",
    "workflow.sync.scope": "Discovery boundary: source parent tree or bound project, respecting recursion. Read affected candidates, not every task.",
    "workflow.sync.references": "Repair references needed by affected tasks: repository path#heading and purpose. Never edit documents or backfill unrelated tasks.",
    "workflow.sync.paths": "Repository-relative globs for auto after-events; intact managed AGENTS.md workflow-block-only changes are skipped. Always ignores this filter.",
    "workflow.sync.models": "Independent ordered candidates with model, effort and speed. Empty: configure a model before sync; the coordinator never performs it instead.",
    "workflow.sync.tiers": "Optional candidates per source task tier, following Use tiers. Empty tier lists inherit Models.",
    "workflow.review": "Review thresholds, attempts, reviewer candidates and GitHub review.",
    "workflow.review.models": "Reviewer candidates in preference order; the first available one runs, skipping a missing CLI or an exhausted usage limit, and the rest are fallbacks. Used unless Use tiers is on and the task's tier has its own list; with no candidates a review inherits the active session.",
    "workflow.review.tiers": "Optional reviewer candidates per task tier 1-3, used only when Use tiers is on. A nonempty tier list replaces Models for that tier; an empty one uses Models.",
    "workflow.review.other_vendor_first": "Run a reviewer from another vendor than the code's authors first, keeping list order within each group; the authors' own candidates stay as last fallbacks. A vendor is a candidate's external API name, else its CLI (codex, claude). The coordinator names the authors with --author on workflow inspect and review. It orders configured candidates only: with an empty list the session's own native reviewer runs. A --reviewer pin and a GitHub cloud provider are unaffected.",
    "workflow.review.policy": "Off skips workflow review. Auto triggers on file/line thresholds, sensitive paths, or the review label. Always requires review for every task and the complete parent diff. GitHub merge may still require independent approval.",
    "workflow.review.max_files": "Automatic-review threshold for changed file count. The Review preset action can set this together with max lines.",
    "workflow.review.max_lines": "Automatic-review threshold for changed line count. The Review preset action can set this together with max files.",
    "workflow.review.max_rounds": "Maximum review verdicts, local or read back from a pull request, before explicit user allowance is required. A round that ended without a verdict spends none of them and is counted as a failure instead.",
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
    "agents": "Agents", "box": "Workspace box", "credentials": "Credentials", "host": "Host",
    "language": "Working language",
    "default": "Default agent", "statusline": "Status line",
    "claude": "Claude", "codex": "Codex", "github": "GitHub", "git": "Git",
    "command": "Custom command", "k3s": "k3s cluster", "links": "Web links", "claude_skill_links": "Claude skill links",
    "env": "Environment", "host_env": "Provider environment", "tools": "Image tools",
    "registries": "Container registries", "hosts": "Registry hosts", "helm_repositories": "Helm repositories",
    "url": "URL", "token": "Access token", "effort": "Reasoning effort",
    "github_model": "GitHub review model", "github_effort": "GitHub review effort",
    "telegram_chat_id": "Telegram chat ID",
    "worker": "Workers", "scout": "Scouts", "sync": "Plan sync", "use_tiers": "Use tiers",
    "github_provider": "GitHub review provider", "other_vendor_first": "Other vendor first",
    "external_apis": "External APIs", "api_provider": "API provider", "api_key": "API key",
}
#: Labels a bare key cannot carry because the same key means something else elsewhere.
PATH_LABELS = {
    "credentials.github.token": "Forward gh token", "credentials.github.config": "Copy gh config",
    # The image's kubernetes tool group keeps its own label.
    "credentials.kubernetes": "Kubernetes configs", "credentials.kubernetes.files": "Kubeconfig files",
    "credentials.cloud": "Cloud CLI profiles", "credentials.cloud.yc": "Yandex Cloud (yc)",
    "credentials.cloud.yc.profiles": "yc profiles", "box.tools.yc": "Yandex Cloud CLI (yc)",
}

#: What each offered value means, shown for the value under the cursor; the field's own
#: help stays behind "?". One shared paragraph for every value made the reader map the
#: prose back to the row themselves.
OPTION_HELP = {
    "agents.default": {
        "claude": "A bare `agent-box` starts Claude Code.",
        "codex": "A bare `agent-box` starts Codex CLI.",
    },
    "tracker.provider": {
        "vikunja": "Vikunja: projects, subprojects, columns and task relations.",
        "jira": "Jira: reserved; its adapter is not implemented yet, so tracker commands refuse it.",
        "linear": "Linear: reserved; its adapter is not implemented yet, so tracker commands refuse it.",
        "github": "GitHub Projects: a project's Status column and this repository's issues, with sub-issues and dependencies.",
    },
    "workflow.merge_mode": {
        "github": "Parent work lands through a GitHub pull request; publishing needs GitHub integration.",
        "local": "Parent work fast-forwards into origin's default branch locally; no pull request.",
    },
    "workflow.sync.policy": {
        "off": "No automatic Plan sync; `workflow sync` still runs on demand.",
        "auto": "Runs before work and after planning, and after changes matching Paths.",
        "always": "Runs at every selected event, whatever changed.",
    },
    "workflow.sync.scope": {
        "parent": "Look for affected tasks only in the source task's parent tree.",
        "project": "Look for affected tasks anywhere in the bound project, respecting recursion.",
    },
    "workflow.sync.events": {
        "before": "Before implementation starts on a task.",
        "after": "After a task is integrated into its parent or base.",
        "plan": "After planning or an accepted decision changes tasks.",
    },
    "workflow.review.policy": {
        "off": "No workflow review; GitHub merge may still require independent approval.",
        "auto": "Review when file or line thresholds, sensitive paths or the review label trigger it.",
        "always": "Review every task and the complete parent diff.",
    },
    "workflow.review.incremental": {
        "off": "After an applied round, new commits are reviewed against the whole task diff.",
        "LOW": "A NEEDS_WORK round with only LOW findings lets its fixes be judged by their delta.",
        "MEDIUM": "A NEEDS_WORK round with findings up to MEDIUM lets its fixes be judged by their delta.",
        "HIGH": "A NEEDS_WORK round with findings up to HIGH lets its fixes be judged by their delta.",
        "CRITICAL": "Any NEEDS_WORK round lets its fixes be judged by their delta.",
    },
    "workflow.review.github_provider": {
        "auto": "Use local review; the same as local.",
        "local": "Review locally with the configured reviewer candidates.",
        "codex": "Ask the Codex GitHub review bot in the parent pull request; it cannot pin model or effort.",
        "claude": "Ask the configured Claude GitHub Action in the parent pull request.",
    },
    "box.k3s.lifetime": {
        "persistent": "The cluster runs until `agent-box stop`, so deployments survive between sessions.",
        "box": "The cluster stops when agent-box removes the box, or at the next agent-box command after the box ended.",
    },
}
EFFORT_HELP = {
    "default": "Send no effort override; the CLI or provider default applies.",
    "none": "No reasoning; the quickest answers.",
    "minimal": "The least reasoning short of none.",
    "low": "Light reasoning for simple, well-specified work.",
    "medium": "Balanced reasoning for ordinary tasks.",
    "high": "Deeper reasoning for harder tasks; slower and more expensive.",
    "xhigh": "Extra-high reasoning for difficult problems.",
    "max": "Maximum reasoning the model offers.",
    "ultra": "Codex's highest reasoning level; the slowest and most expensive.",
}
SPEED_HELP = {
    "inherit": "Keep the session's or provider's speed; the only choice for external APIs and native agents.",
    "standard": "Explicitly turn fast mode off; runs as a managed CLI.",
    "fast": "Request faster output at higher provider usage or cost; runs as a managed CLI.",
}
MODALITY_HELP = {
    "text": "Text prompts.",
    "image": "Images attached to prompts, such as screenshots.",
}
#: Fields whose list holds members of a fixed vocabulary, edited as checkboxes; order is the vocabulary's.
#: External API model lists are keyed by dynamic names, so `list_vocabulary` adds those.
MULTI_CHOICES = {
    "workflow.sync.events": ("before", "after", "plan"),
}
#: Lists ticked from what the host holds, found when the field is opened.
HOST_CHOICES = {("credentials", "registries", "hosts"), ("credentials", "registries", "helm_repositories"),
                ("credentials", "kubernetes", "files"), ("credentials", "cloud", "yc", "profiles")}

# Presentation follows the user's decisions, not dataclass serialization order.
MENU_GROUPS = {
    (): (
        ("", ("agents", "box", "credentials", "tracker", "workflow", "host")),
    ),
    ("agents",): (
        ("Launch", ("default",)),
        ("Agent CLIs", ("claude", "codex")),
        ("Provider access", ("host_env", "external_apis")),
    ),
    ("box",): (
        ("Image", ("tools",)),
        ("Beside the box", ("k3s",)),
        ("Inside the box", ("audio", "links", "env")),
    ),
    ("credentials",): (
        ("", ("git", "github", "registries", "kubernetes", "cloud")),
    ),
    ("host",): (
        ("", ("notifications", "claude_skill_links")),
    ),
    ("workflow",): (
        ("Workflow", ("enabled",)),
        ("Delivery", ("merge_mode", "auto_push", "cleanup_local_branches", "cleanup_remote_branches")),
        ("Project text", ("language",)),
        ("Roles", ("use_tiers", "worker", "scout", "review", "sync")),
        ("Delegation", ("timeout_seconds",)),
    ),
    ("agents", "codex"): (
        ("Native agents", ("native_agent_wait",)),
        ("Remote access", ("remote_control",)),
    ),
    ("workflow", "worker"): (
        ("Models", ("models", "tiers")),
    ),
    ("workflow", "scout"): (
        ("Concurrency", ("limit",)),
        ("Models", ("models",)),
    ),
    ("workflow", "sync"): (
        ("Run policy", ("policy", "events", "paths")),
        ("Task scope", ("scope", "references")),
        ("Models", ("models", "tiers")),
    ),
    ("workflow", "review"): (
        ("Review triggers", ("policy", "max_files", "max_lines", "sensitive_paths", "ignored_paths", "label", "incremental")),
        ("Models", ("models", "tiers", "other_vendor_first")),
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


#: Named external APIs; every path below holds user-chosen provider and model names.
EXTERNAL = ("agents", "external_apis")


def external(path):
    """The part of ``path`` below ``agents.external_apis``, or None outside it."""
    return path[len(EXTERNAL):] if path[:len(EXTERNAL)] == EXTERNAL else None


def label_for(key, path=()):
    if ".".join((*path, key)) in PATH_LABELS:
        return PATH_LABELS[".".join((*path, key))]
    if path == EXTERNAL or external(path) is not None and path[-1:] == ('models',):
        return key
    if len(path) == 2 and path[0] == "workflow" and key == "tiers":
        return "Models by tier"
    return LABELS.get(key, f"Tier {key}" if key.isdigit() else key.replace("_", " ").capitalize()).replace('Api', 'API')


def json_field(path):
    rel = external(path)
    return (path in (('box', 'env', 'set'), ('box', 'k3s', 'registry_mirrors')) or rel is not None
            and (len(rel) == 3 and rel[1] in LAUNCH_AGENTS and rel[-1] in ('env', 'provider_options', 'config')
                 or len(rel) == 5 and rel[1] == 'models' and rel[-1] in ('env', 'metadata')))


def help_for(path):
    """Return complete help for every static and generated menu entry."""
    dotted = ".".join(path)
    if path[0] == "workflow" and path[-1] == "speed":
        return ("Inherit preserves session/provider speed; standard explicitly disables fast; fast requests higher speed at higher provider usage/cost. "
                "Independent of effort. Explicit speed uses managed CLI; native agents inherit speed. External APIs support inherit only.")
    if dotted in HELP:
        return HELP[dotted]
    rel = external(path)
    if rel is not None:
        # Provider names and model IDs are the user's own; every fixed field below needs its own help.
        if len(rel) == 1:
            return HELP["agents.external_apis"]
        if len(rel) == 2 and path[-1] in LAUNCH_AGENTS:
            return f"{path[-1].title()} endpoint of this API: its own URL, default model and environment, used only when {path[-1]} runs against it."
        if len(rel) == 3 and rel[1] == "models":
            return ("Settings for this model ID, shared by every CLI with a URL here. Workflow candidates select it through the "
                    "model picker; the ID sent to the API is the key itself.")
        if len(rel) == 4 and rel[1] == "models" and path[-1] in LAUNCH_AGENTS:
            return f"Settings only {path[-1]} uses for this model."
        text = {"enabled": "Expose this API connection to agent-box sessions. Off by default.",
                "api_key": "Stored in the protected global config; masked in displays and excluded from command arguments.",
                "url": ("HTTP(S) API base URL for this CLI, without credentials, query or fragment. Codex requires the Responses API, "
                        "for example https://api.deepseek.com or https://openrouter.ai/api/v1; Claude requires the Anthropic "
                        "Messages API, for example https://api.deepseek.com/anthropic or https://openrouter.ai/api."),
                "models": ("Models of this API for both CLIs, keyed by ID. Opening this list loads the provider's model list "
                           "(GET {Codex URL}/models, else {Claude URL}/v1/models): add a model from it with its settings filled in, "
                           "or enter an ID by hand. A listed model is a suggestion, not proof of account access."),
                "default_model": "Configured model used without --model; empty uses the first entry. Explicit selection is stable across file ordering.",
                "display_name": "Optional model picker label; the model ID sent to the API stays unchanged.",
                "effort": "Model reasoning default; default leaves the CLI/provider choice. Explicit CLI/workflow effort takes priority. Claude launches a level it lacks with its own default.",
                "supported_efforts": "Effort names the model supports. None checked means unknown; declared levels constrain explicit requests. Claude uses the ones among its own levels.",
                "context_window": "Tokens; 0 leaves the CLI default. Claude knows 200K and 1M windows: from 1000000 tokens it launches the model with [1m], from 200000 with the 200K window.",
                "auto_compact_token_limit": "Compaction threshold in tokens; 0 leaves the CLI default. Must be below a configured context window.",
                "input_modalities": "Input types for the Codex model catalog: text, image or both; at least one.",
                "supports_parallel_tool_calls": "Advertise parallel tool calls in the Codex model catalog.",
                "supports_reasoning_summaries": "Advertise reasoning summaries in the Codex model catalog.",
                "metadata": "Additional native Codex model metadata as JSON. Typed model fields cannot be overridden here; fields must be supported by your CLI.",
                "provider_options": "Native Codex model-provider JSON: query_params, http_headers, env_http_headers, retries, timeouts, transport. URL/auth fields belong to agent-box. Header values travel through environment variables; use non-secret query parameters.",
                "config": "Native Codex session JSON, for example {\"web_search\":\"live\"}. CLI overrides win; model effort defaults apply next. API/auth/catalog/Remote Control fields belong to agent-box. Use env or header settings for secrets, never literal secrets here.",
                "env": "JSON object of per-process environment strings. Model values override endpoint values; typed effort/compact fields and explicit effort take priority. API/auth/RC ownership cannot be overridden. Values are masked and never placed in argv."}.get(path[-1])
        if text:
            return text
    if len(path) == 3 and path[:2] == ("box", "tools"):
        return f"{TOOL_GROUP_HELP.get(path[-1], path[-1])}. Rebuild the workspace image to install or remove this group; browsing and saving this selection do not rebuild it."
    if len(path) == 3 and path[:2] == ("tracker", "connections"):
        return f"Connection settings for the {path[-1]} tracker. It is exposed only when tracker integration is enabled."
    if path == ("tracker", "connections", "github", "url"):
        return "GitHub API URL; empty means https://api.github.com. GitHub Enterprise Server: https://HOST/api/v3."
    if path == ("tracker", "connections", "github", "token"):
        return ("Classic token or `gh auth token` with scopes repo and project; fine-grained tokens cannot reach "
                "projects of a personal account. It can also push code, so enabling the tracker gives the box that access.")
    if len(path) == 4 and path[:2] == ("tracker", "connections"):
        if path[-1] == "url":
            return f"Base URL for the {path[-2]} tracker API. No network request is made while editing."
        return f"Access token for the {path[-2]} tracker. It is masked here and rendered only for enabled tracker integration."
    if candidate_list(path) and len(path) == 4:
        role = "reviewer" if path[1] == "review" else path[1]
        return (f"{role.title()} candidates for tier {path[-1]} in preference order, used only when Use tiers is on. Nonempty replaces the role's Models for this tier; "
                "empty uses them. Enter edits the list: add, reorder, change or remove candidates.")
    raise KeyError(f"No menu help for {dotted}")


def needed_tools(draft):
    """Tool groups the drafted forwarding needs: the menu keeps their ticks with it."""
    return CredentialsConfig.from_dict(draft.get("credentials")).needed_tools()


def validate(config):
    errors = (config.workflow.validate() + config.agents.codex.validate() + config.box.validate()
              + validate_connections(config))
    if config.agents.claude.telegram_channel:
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
    return (len(path) >= 3 and path[0] == "workflow" and path[1] in ("scout", "worker", "review", "sync")
            and (len(path) == 3 and path[2] == "models" or len(path) == 4 and path[2] == "tiers" and path[1] != "scout"))


def candidate_efforts(spec, tree):
    """Efforts a candidate may request: its CLI's, narrowed by a configured API model's declaration."""
    provider, api, value = spec["provider"], spec.get("api_provider"), spec["effort"]
    if not api:
        return tuple(dict.fromkeys([*AGENT_EFFORTS.get(provider, ()), value]))
    models = cli_models(tree["agents"]["external_apis"].get(api, {}).get("models", {}), provider)
    model = model_settings(models, spec["model"], provider)
    declared = [e for e in model["supported_efforts"] if e in API_EFFORTS[provider]]
    return tuple(dict.fromkeys(["default", *(declared if model["supported_efforts"] else API_EFFORTS[provider][1:]), value]))


def value_choices(path, section, tree):
    """The values the menu offers for ``path``, or None for free input; `config describe` reports the same."""
    name, value = path[-1], section[path[-1]]
    options = CHOICES.get(".".join(path))
    rel = external(path)
    if rel is not None:
        if len(rel) == 4 and rel[1] == 'models' and name == 'effort':
            options = tuple(dict.fromkeys(['default', *(section['supported_efforts'] or EFFORTS[1:]), value]))
        elif len(rel) == 3 and rel[1] in LAUNCH_AGENTS and name == 'default_model':
            options = ['', *tree['agents']['external_apis'][rel[0]]['models']]
    return options


def option_help(path, options):
    """What each offered value means, or None when only the field's own help describes them."""
    rel = external(path)
    if rel is not None and len(rel) == 3 and rel[1] in LAUNCH_AGENTS and path[-1] == 'default_model':
        return ["Launches without --model use the first configured model." if not option
                else f"Launches without --model use {option}." for option in options]
    if rel is not None and path[-1] in ('effort', 'supported_efforts'):
        known = EFFORT_HELP
    elif rel is not None and path[-1] == 'input_modalities':
        known = MODALITY_HELP
    else:
        known = OPTION_HELP.get(".".join(path))
    if not known:
        return None
    return [known.get(option) or help_for(path) for option in options]


def list_vocabulary(path):
    """The fixed values a list field may hold, edited as checkboxes, or None for a free list."""
    rel = external(path)
    if rel is not None and len(rel) == 4 and rel[1] == 'models':
        if path[-1] == 'supported_efforts':
            return EFFORTS[1:]
        if path[-1] == 'input_modalities':
            return tuple(MODALITY_HELP)
    return MULTI_CHOICES.get(".".join(path))


def group_headings(keys, path):
    """Row index -> heading for the first row of each named group; an unnamed group draws none."""
    headings = {}
    for heading, fields in MENU_GROUPS.get(path, ()):
        first = next((index for index, key in enumerate(keys) if key in fields), None)
        if heading and first is not None:
            headings[first] = heading
    return headings


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
    # PATH integration is an action in the Host section, not a free-text field.
    draft["host"].pop("bin_dir", None)
    for provider in TRACKER_PROVIDERS:
        inherited["tracker"]["connections"].setdefault(provider, TrackerConnection().to_dict())
        draft["tracker"]["connections"].setdefault(provider, TrackerConnection().to_dict())
    if project_mode:
        for target in (inherited, draft):
            target["tracker"].setdefault("project", "")
            target["tracker"].setdefault("recursive", False)
    path_state = {"bin": Path(config.host.bin_dir) if config.host.bin_dir else None, "rc": False}

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
    # Resetting Host must not bring the PATH directory back as a free-text field.
    defaults["host"].pop("bin_dir")
    for provider in TRACKER_PROVIDERS:
        defaults['tracker']['connections'][provider] = TrackerConnection().to_dict()

    def default_value(path, source):
        value = source
        for part in path:
            if part not in value:
                # Connection rows are editable defaults, but a section copy
                # must not create accounts that the global map does not hold.
                rel = external(path)
                if rel:
                    model_path = len(rel) >= 3 and rel[1] == 'models'
                    value = model_defaults() if model_path else ExternalProvider().to_dict()
                    for part in rel[3:] if model_path else rel[1:]:
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
        result = Config.from_dict({**draft, "host": {**draft["host"], "bin_dir": str(path_state["bin"]) if path_state["bin"] else ""}})
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

    def paint(screen, rows, selected, title, help_text="", notice="", controls=None, headings=None):
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
        label_width = min(max((display_width(row[0]) for row in rows if isinstance(row, tuple)), default=0),
                          32, list_width // 2 - 2)
        # Headings are drawn between rows, never stored in them, so selection and the
        # counter only ever see rows that can be chosen.
        lines = []
        for index, row in enumerate(rows):
            if headings and index in headings:
                lines.append((None, headings[index]))
            lines.append((index, row))
        indent = "  " if headings else ""
        at = next((line for line, (index, _) in enumerate(lines) if index == selected), 0)
        offset = min(max(0, at - list_height + 1), max(0, len(lines) - list_height))
        if offset and lines[offset][0] is not None:
            # A group whose heading scrolled away, or never fits a short terminal, keeps it above the list.
            add_line(screen, 2, next((row for index, row in reversed(lines[:offset]) if index is None), ""),
                     list_width, curses.A_BOLD | curses.A_UNDERLINE)
        for y, (index, row) in enumerate(lines[offset:offset + list_height], 3):
            if index is None:
                add_line(screen, y, row, list_width, curses.A_BOLD | curses.A_UNDERLINE)
                continue
            attr = curses.A_REVERSE | curses.A_BOLD if index == selected else 0
            if "Global only" in (row[1] if isinstance(row, tuple) else row):
                attr = global_only_color | (curses.A_BOLD | curses.A_UNDERLINE if index == selected else 0)
            if isinstance(row, tuple):
                label, value = row
                row = padded(clipped(label, label_width), label_width) + ": " + clipped(value, list_width - label_width - 3 - len(indent))
            add_line(screen, y, indent + row, list_width, attr)
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
                lines = []
                for paragraph in content.split("\n"):
                    lines.extend(textwrap.wrap(paragraph, width - 1, break_long_words=True, break_on_hyphens=False) or [""])
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

    def described(detail, explanation):
        """Full help for a row: its own description, which a short terminal may have cut, then the field's."""
        if not explanation or detail == explanation:
            return detail or explanation
        return detail + "\n\n" + explanation

    def choose(screen, rows, title, index=0, explanation="", details=None, searchable=False):
        """Pick one row; ``details`` describe each row under the cursor, ``explanation`` is the field's help behind ?.

        ``searchable`` lets typed text, spaces included, narrow the rows to those
        containing it regardless of case, for lists as long as a provider's
        catalog; Enter then chooses and Esc clears the filter before leaving.
        The result is always an index into ``rows``."""
        query = ""
        while True:
            shown = [i for i, row in enumerate(rows) if query.casefold() in row.casefold()]
            if index not in shown:
                index = shown[0] if shown else index
            help_text = details[index] if details and shown else explanation
            usable = paint(screen, [rows[i] for i in shown] or [f"No row contains {query!r}"],
                           shown.index(index) if shown else 0, title + (f"  [filter: {query}]" if query else ""),
                           help_text, controls=("Type to filter  Enter choose" if searchable else "Enter choose")
                           + "  ? help  Esc back")
            key = screen.get_wch()
            if key in ("\x1b", curses.KEY_LEFT):
                if query:
                    query = ""
                    continue
                return None
            if not usable:
                continue
            if searchable and key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                query = query[:-1]
                continue
            if searchable and isinstance(key, str) and key.isprintable() and key != "?":
                query += key
                continue
            destination = moved(key, shown.index(index), len(shown), screen) if shown else None
            if destination is not None:
                index = shown[destination]
            elif key in (" ", "\n", "\r") and shown:
                return index
            elif key in ("?", curses.KEY_F1):
                full_help(screen, title, described(help_text, explanation))

    def check(screen, options, title, chosen, details, explanation="", marks=None):
        """Toggle members of a fixed vocabulary; the result keeps the vocabulary's order, None on Esc.

        Options and their descriptions come from the caller, so a vocabulary found at run
        time, such as the registries logged in on the host, uses the same screen. ``marks``
        puts a short note beside each option, readable without moving the cursor onto it."""
        marked, index = {option for option in options if option in chosen}, 0
        while True:
            rows = [("[x] " if option in marked else "[ ] ") + option for option in options]
            if marks:
                rows = list(zip(rows, marks))
            usable = paint(screen, rows, index, title, details[index],
                           controls="Space toggle  Enter accept  ? help  Esc cancel")
            key = screen.get_wch()
            if key in ("\x1b", curses.KEY_LEFT):
                return None
            if not usable:
                continue
            destination = moved(key, index, len(rows), screen)
            if destination is not None:
                index = destination
            elif key == " ":
                marked ^= {options[index]}
            elif key in ("\n", "\r"):
                return [option for option in options if option in marked]
            elif key in ("?", curses.KEY_F1):
                full_help(screen, title, described(details[index], explanation))

    def pick_model(screen, spec):
        """``(provider, api, model)`` chosen together, or None; any CLI's model may serve any role."""
        current = (spec["provider"], spec.get("api_provider", ""), spec["model"]) if spec else None
        apis = [(provider, '') for provider in AGENT_PROVIDERS]
        apis += [(provider, name) for name, connection in draft['agents']['external_apis'].items()
                 for provider in AGENT_PROVIDERS if connection['enabled'] and connection[provider]['url']]
        if current and current[:2] not in apis:
            # A deleted or disabled API stays editable by hand rather than silently moving the candidate.
            apis.insert(0, current[:2])
        suggestions = [(provider, '', model, f'{model} [{provider}]')
                       for provider in AGENT_PROVIDERS for model in model_suggestions[provider]]
        suggestions += [(provider, api, model, (settings['display_name'] or model) + f' [{provider}@{api}: {model}]')
                        for provider, api in apis if api and api in draft['agents']['external_apis']
                        for model, settings in draft['agents']['external_apis'][api]['models'].items()]
        unlisted = current and current not in [row[:3] for row in suggestions]
        if unlisted:
            suggestions.insert(0, (*current, f'{current[2]} [{current[0]}{"@" + current[1] if current[1] else ""}]'))
        rows = [f'Enter a custom model ID... [{provider}{"@" + api if api else ""}]' for provider, api in apis]
        details = [f'Type any model ID for the {provider} CLI '
                   + (f'through the {api} external API.' if api else 'through its own login.') for provider, api in apis]
        offset = len(rows)
        rows += [row[3] + (' (current)' if row[:3] == current else '') for row in suggestions]
        details += [f'{model}: the saved ID, entered by hand; neither the {provider} CLI\'s model list nor a configured API lists it.'
                    if unlisted and number == 0
                    else f'{model}: configured under External APIs / {api} / {provider}, reached through that endpoint.' if api
                    else f'{model}: a suggested {provider} model, run through the CLI\'s own login.'
                    for number, (provider, api, model, _) in enumerate(suggestions)]
        index = next((i + offset for i, row in enumerate(suggestions) if row[:3] == current), offset)
        about = ('Select the CLI, API and model together. A candidate of the other CLI than the coordinator\'s runs as a '
                 'managed CLI process. Configured external models are not live discovery; availability depends on the provider account.')
        picked = choose(screen, rows, 'Model', index, explanation=about, details=details, searchable=True)
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
            details = [f"Candidate {number}: Enter to change its model, effort or speed, move it or remove it. " + help_for(path)
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
                       f"Effort: {spec['effort']}", f"Speed: {spec.get('speed', 'inherit')}",
                       "Move up", "Move down", "Remove"]
            effort_about = "Reasoning effort; default omits agent-box's effort override."
            action = choose(screen, actions, f"{title} / {index + 1}", 0,
                            details=["Choose the CLI, API and model.", effort_about,
                                     help_for(("workflow", "speed")), "Prefer this candidate earlier.",
                                     "Prefer this candidate later.", "Delete this candidate from the list."])
            if action == 0:
                chosen = pick_model(screen, spec)
                if chosen:
                    spec["provider"], spec["api_provider"], spec["model"] = chosen
                    changed = True
            elif action == 1:
                options = candidate_efforts(spec, draft)
                picked = choose(screen, list(options), f"{title} / {index + 1} / effort",
                                options.index(spec["effort"]) if spec["effort"] in options else 0,
                                explanation=effort_about, details=[EFFORT_HELP.get(option, effort_about) for option in options])
                if picked is not None:
                    spec["effort"], changed = options[picked], True
            elif action == 2:
                from .speed import SPEEDS
                picked = choose(screen, list(SPEEDS), f"{title} / {index + 1} / speed",
                                SPEEDS.index(spec.get("speed", "inherit")) if spec.get("speed", "inherit") in SPEEDS else 0,
                                explanation=help_for(("workflow", "speed")), details=[SPEED_HELP[speed] for speed in SPEEDS])
                if picked is not None:
                    spec["speed"], changed = SPEEDS[picked], True
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

    def keep_tools(notice):
        """Forwarded configuration keeps its tool group ticked, whichever action took the tick away.

        Checked after every action rather than guarded per action: a toggle,
        a reset of the row and a reset of the whole tools section all reach
        the same draft, and forwarding turned on needs the same tick.
        """
        needed = needed_tools(draft)
        kept = [group for group in needed if not draft["box"]["tools"].get(group)]
        for group in kept:
            draft["box"]["tools"][group] = True
            if project_mode:
                record(("box", "tools", group), True)
        if not kept:
            return notice
        return (f"Image tools: {', '.join(kept)} on for {', '.join(needed[group] for group in kept)}; "
                "built by the next agent-box build and kept on while they are forwarded")

    published = {}

    def provider_list(screen, api):
        """``(models, error)`` the provider publishes for its current key and URLs.

        Asked once per combination in a menu session: entering the provider again
        after editing its key or a URL asks again, browsing does not."""
        spec = draft["agents"]["external_apis"][api]
        identity = (spec["api_key"], spec["codex"]["url"], spec["claude"]["url"])
        if published.get(api, (None,))[0] != identity:
            paint(screen, [f"Loading the {api} model list..."], 0, "agent-box config / External APIs / " + api,
                  f"Asking the provider for its models; at most {api_models.TIMEOUT} seconds per URL.", controls="Please wait")
            published[api] = (identity, *api_models.load(spec))
        return published[api][1:]

    def listed_summary(model, fields):
        """What the provider states about one model, for the row under the cursor."""
        facts = [f"{fields['context_window']} tokens of context" if "context_window" in fields else "",
                 "input " + ", ".join(fields["input_modalities"]) if "input_modalities" in fields else "",
                 "efforts " + ", ".join(fields["supported_efforts"]) + (f" (default {fields['effort']})" if fields.get("effort", "default") != "default" else "")
                 if "supported_efforts" in fields else ""]
        stated = "; ".join(fact for fact in facts if fact)
        return f"{model}: " + (stated + "." if stated else "the provider states only its ID; fill the settings by hand.")

    def menu(screen, data, path=()):
        selected, notice = 0, ""
        if path_pending() and path in ((), ("host",)):
            # Lead an unfinished setup to the PATH row inside Host.
            selected = menu_keys(data, path).index("host") if not path else len(data)
            notice = "Setup starts here: agent-box is not on PATH yet" + (" (open Host)" if not path else "")
        while True:
            notice = keep_tools(notice)
            keys = menu_keys(data, path)
            extras = [("Review preset", "preset", ACTION_HELP["preset"])] if path == ("workflow", "review") else []
            rel = external(path)
            listed = None
            if rel and not project_mode and rel[0] in draft["agents"]["external_apis"] and (
                    len(rel) == 1 or rel[1] == "models" and len(rel) in (2, 3)):
                listed, list_error = provider_list(screen, rel[0])
                if list_error:
                    listed = None
                    notice = notice or "Model list: " + list_error + ". Manual model entry still works."
                elif len(rel) == 2:
                    notice = notice or (f"Model list: {len(listed)} models published by {rel[0]}; "
                                        "rows marked 'not listed' are absent from it.")
            if path == EXTERNAL and not project_mode:
                extras = [("Add provider", "add_provider", "Create a disabled provider with separate Codex/Claude endpoints.")]
            elif rel is not None and len(rel) == 1 and not project_mode:
                extras = [("Delete provider", "delete_provider", "Remove this definition from the draft. Existing workflow references remain visible and must be changed before use.")]
            elif rel is not None and not project_mode:
                if len(rel) == 2 and path[-1] == 'models':
                    extras = [('Add model', 'add_model', 'Enter a model ID by hand and fill its settings yourself.')]
                    if listed:
                        extras.insert(0, ('Add from provider list', 'add_listed',
                                          'Pick one of the models the provider publishes, filtered by typing; the settings '
                                          'its list states are filled in and stay editable.'))
                elif len(rel) == 3 and rel[1] == 'models':
                    extras = [('Use as default', 'default_model', 'Select this model for Codex and Claude launches without --model; '
                               'each CLI endpoint keeps its own Default model field.'),
                              ('Delete model', 'delete_model', 'Remove this model and clear any default selection naming it.')]
                    if listed and rel[2] in listed:
                        extras.insert(0, ('Update from API', 'update_listed',
                                          'Replace the settings the provider list states (' + (", ".join(listed[rel[2]]) or "none")
                                          + ') with its current values; every other setting stays.'))
            if not path:
                extras = [("Save and apply", "save", ACTION_HELP["save"]), ("Cancel", "cancel", ACTION_HELP["cancel"])]
                if project_mode:
                    extras = [("Save project", "save", "Save explicit project overrides without applying host integrations."), ("Cancel", "cancel", ACTION_HELP["cancel"])]
            elif path == ("host",):
                extras = [(path_row(), "path", path_row() + ". " + ACTION_HELP["path"])]
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
                elif (list_vocabulary(full) or full in HOST_CHOICES) and isinstance(value, list):
                    shown = ", ".join(map(str, value)) or "(none)"
                elif isinstance(value, str):
                    shown = value or "(not set)"
                else:
                    shown = json.dumps(value, ensure_ascii=False)
                if listed is not None and rel is not None and len(rel) == 2 and key not in listed:
                    shown += "  not listed"
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
            headings = group_headings(keys, path)
            if headings and extras:
                # Otherwise the actions would read as members of the last group.
                headings[len(keys)] = "Actions"
            usable = paint(screen, rows, selected, title, hint, notice, controls=controls, headings=headings)
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
                        model = input_value(screen, 'Model ID, for example deepseek-v4-pro or openai/gpt-5.6-luna; '
                                            'Claude derives [1m] from the context window', '', False)
                        if model is not None:
                            if not valid_model(model):
                                notice = 'Invalid model ID'
                            elif model in data:
                                notice = 'Model already exists'
                            else:
                                data[model] = model_defaults()
                                menu(screen, data[model], (*path, model))
                        continue
                    if action == 'add_listed':
                        candidates = [model for model in listed if model not in data and valid_model(model)]
                        if not candidates:
                            notice = 'Every model the provider lists is already configured'
                            continue
                        labels = [(f"{listed[m]['display_name']} [{m}]" if 'display_name' in listed[m] else m) for m in candidates]
                        picked = choose(screen, labels, 'Add from the ' + path[2] + ' model list', 0,
                                        explanation='Models the provider publishes and this list does not hold yet. '
                                        'A listed model is a suggestion, not proof that the account may use it.',
                                        details=[listed_summary(m, listed[m]) for m in candidates], searchable=True)
                        if picked is not None:
                            model = candidates[picked]
                            data[model] = {**model_defaults(), **copy.deepcopy(listed[model])}
                            menu(screen, data[model], (*path, model))
                        continue
                    if action == 'update_listed':
                        data.update(copy.deepcopy(listed[path[4]]))
                        notice = 'Updated from the provider list: ' + (', '.join(listed[path[4]]) or 'nothing it states')
                        continue
                    if action in ('default_model', 'delete_model'):
                        provider = draft['agents']['external_apis'][path[2]]
                        if action == 'default_model':
                            for cli in LAUNCH_AGENTS:
                                provider[cli]['default_model'] = path[4]
                            notice = 'Default model updated in draft for Codex and Claude'
                        elif choose(screen, ['Keep model', 'Delete model'], 'Delete ' + path[4], 0) == 1:
                            del provider['models'][path[4]]
                            for cli in LAUNCH_AGENTS:
                                if provider[cli]['default_model'] == path[4]:
                                    provider[cli]['default_model'] = ''
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
                        chosen = choose(screen, ["Keep provider", "Delete provider"], "Delete " + path[2], 0)
                        if chosen == 1:
                            del draft["agents"]["external_apis"][path[2]]
                            return None
                        continue
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
                elif full in HOST_CHOICES and isinstance(value, list):
                    if name == "files":
                        from . import kubeconfigs
                        options, details, marks, problems = kubeconfigs.choices(value)
                        empty = f"No kubeconfig found in {kubeconfigs.KUBECONFIG_DIR}"
                    elif name == "profiles":
                        from . import cloud
                        options, details, marks, problems = cloud.choices(value)
                        empty = f"No yc profile found in {cloud.YC_CONFIG_DIR / 'config.yaml'}"
                    else:
                        from . import registries
                        (options, details, problems), marks = registries.choices(name, value), None
                        empty = "Nothing found on the host: log in with podman, helm registry or helm repo add first"
                    if not options:
                        notice = (problems or [empty])[0]
                        continue
                    picked = check(screen, options, ".".join(full), value, details,
                                   "\n\n".join([help_for(full), *problems]), marks)
                    if picked is not None:
                        data[name], notice = picked, "Draft updated; save to keep changes"
                        accepted = True
                elif list_vocabulary(full) and isinstance(value, list):
                    vocabulary = list_vocabulary(full)
                    picked = check(screen, vocabulary, ".".join(full), value, option_help(full, vocabulary), help_for(full))
                    if picked is not None:
                        data[name], notice = picked, "Draft updated; save to keep changes"
                        accepted = True
                elif options:
                    if key == " ":
                        data[name] = options[(options.index(value) + 1) % len(options)] if value in options else options[0]
                        accepted = True
                    else:
                        picked = choose(screen, [option or ('First configured model' if name == 'default_model' else "Built-in API") for option in options], ".".join(full), options.index(value) if value in options else 0, help_for(full),
                                        details=option_help(full, options))
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
                            if full[:2] == ("box", "env"):
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
