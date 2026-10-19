# agent-box documentation

Start with the [project README](../README.md) for installation and a first
session.

## Using agent-box

- [Installation, updating and removal](installation.md) — requirements, setup, shell completion, installer updates, `uninstall`.
- [Using agent-box](usage.md) — sessions, the workspace box, options, mounts, environment, host mode, usage limits.
- [Configuration](configuration.md) — layers, the menu, defaults, project configuration, `config ai`.
- [Compatibility and upgrades](compatibility.md) — SemVer, direct upgrades and migration requirements.
- [The workspace image](image.md) — included tools, optional groups, `build`, `update`, `rebuild`.
- [Workspace and security model](security.md) — what a box can reach and how it is hardened.
- [Workspace networking](networking.md) — publish services, reach selected host ports and choose host networking.
- [Nested rootless Podman](nested-podman.md) — run Podman inside the workspace box.
- [Troubleshooting](troubleshooting.md) — diagnostics, common problems, known limitations.

## Integrations

Optional capabilities: the [integration index](integrations/README.md) lists
each one with its default and what it exposes.

## Design records

For contributors: decisions, evidence and provenance behind the implementation.

- [Publishing releases](releasing.md) — Trusted Publishers, release validation and recovery.
- [Release notes](../CHANGELOG.md) — changes in published versions.

- [Workflow design notes](design/workflow.md) — how workflow guarantees are kept: branches, delegation, review rounds, landing, cleanup.
- [Tracker design notes](design/tracker.md) — how the tracker contract is kept: resolution, writes, adoption, GitHub state.
- [Integration design notes](design/integrations.md) — credential copies, notifications, Remote Control, web links, external APIs, nested Podman, developing agent-box in a box.
- [Workflow design references](design/workflow-references.md) — researched sources, reused libraries and adaptations.
- [Native launch contract](design/native-launch.md) — evidence for native subagent and managed launches.
- [Networking design and evidence](design/networking.md) — original audit, design decisions and implementation validation.
- [Third-party notices](design/third-party-notices.md) — licenses of adapted material.
