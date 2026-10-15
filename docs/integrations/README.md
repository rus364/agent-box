# Optional integrations

Each integration is independent of the others, and everything that exposes a
credential or widens what an agent can reach starts off; only Git identity and
Claude skill links are on by default. agent-box works as a plain workspace
runtime with none of them enabled. Each page describes what you can turn on,
what it exposes and what it costs.

| Integration | Default | What it is |
| --- | --- | --- |
| [Shared skills](skills.md) | links on, workflow off | One skill tree both agents read, plus the owned workflow skills |
| [Tracker](tracker.md) | off | Task commands for Vikunja and GitHub Projects |
| [Workflow](workflow.md) | off | Sequential tasks, model routing, fresh review and parent PRs |
| [External API providers](external-providers.md) | off | Named API keys, separate CLI endpoints, explicit launches and workflow candidates |
| [Agent browser](browser.md) | off | A headless browser inside the workspace box for agents to drive |
| [Web links and CLI callbacks](links.md) | host access off | Display CLI links; optionally route localhost callbacks through the host and SSH/Coder |
| [k3s cluster](k3s.md) | off | A per-project Kubernetes cluster in a rootless sidecar next to the box |
| [Host audio](audio.md) | off | The host microphone and speakers in the box, e.g. for Claude Code `/voice` |
| [Telegram](telegram.md) | off | Anthropic's official channel for control, plus one-way notifications |
| [Remote Control](remote-control.md) | off | Native Claude/Codex remote access for new interactive sessions |
| [Git and GitHub](git.md) | identity on, credentials off | What of your Git and GitHub setup reaches a session |
| [Container registries](registries.md) | off | Chosen podman, skopeo and Helm logins from the host |
| [Kubernetes configs](kubeconfigs.md) | off | Chosen kubeconfig files from the host's `~/.kube` |
| [Cloud CLI profiles](cloud-profiles.md) | off | Chosen Yandex Cloud CLI profiles, with their sign-in |
| [Nested Podman](../nested-podman.md) | off | Rootless Podman inside the box, for builds and containers |

All of them are set in `agent-box config` and applied by it, or later by
`agent-box config apply`; starting an agent applies nothing. The settings listed
under [project configuration](../configuration.md#project-configuration)
can also be set per repository.
