# Shared skills

agent-box ships and maintains its own short `ab-*` skills in
`agent_box/skills/`. They are installed into `~/.agents/skills` and optionally
linked into `~/.claude/skills`, so host and container agents share one copy.
Override the shared tree through `AGENT_BOX_AGENTS_DIR`.

| Skill | Installed when |
| --- | --- |
| `ab-tracker` | Tracker or workflow enabled |
| `ab-setup`, `ab-adopt`, `ab-normalize`, `ab-interview`, `ab-plan`, `ab-work`, `ab-review`, `ab-pr`, `ab-land` | Workflow enabled |

Enable integrations and configure models in `agent-box config`. Save and apply,
`config apply`, or `upgrade` installs/refreshes skills and native agent profiles.
Starting an agent does not install or rewrite them.

Installed skill directories carry a content fingerprint. Intact owned copies
can be updated or removed when disabled. User-owned directories, symlinks and
locally edited copies are preserved with a diagnostic. Claude links are removed
only when they point at the shared tree. Native profiles use matching sidecar
fingerprints and the same refusal policy.

Planning, debugging, proportionate regression testing and fresh review are
integrated into the workflow skills. There is no separate upstream updater,
patching pipeline or unused bundle of practice skills. Sources of the workflow
ideas and library decisions are recorded in [design references](../workflow-references.md).
