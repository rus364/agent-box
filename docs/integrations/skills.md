# Shared skills

agent-box ships its own short `ab-*` skills in `agent_box/skills/` and installs
them into the shared tree `~/.agents/skills` (override with
`AGENT_BOX_AGENTS_DIR`). With **Host → Claude skill links** on (the default),
that tree is also linked into `~/.claude/skills`, so host and container agents
read one copy.

| Skill | Installed when |
| --- | --- |
| `ab-tracker` | Tracker or workflow enabled |
| `ab-setup`, `ab-adopt`, `ab-normalize`, `ab-interview`, `ab-plan`, `ab-work`, `ab-sync`, `ab-review`, `ab-pr`, `ab-land` | Workflow enabled |
| `ab-config` | Never installed; `agent-box config ai` reads it from the checkout |

Saving in `agent-box config`, `config apply` and `upgrade` install or refresh
the skills and the native agent profiles; starting an agent never does.

**Your own skills are safe.** Installed skill directories carry a content
fingerprint. agent-box updates or removes only intact copies it installed;
user-owned directories, symlinks and locally edited copies are kept with a
diagnostic. Claude links are removed only when they point into the shared
tree. Native agent profiles follow the same rules.

Planning, debugging, proportionate testing and fresh review are built into the
workflow skills; the sources they draw on are recorded in the
[design references](../design/workflow-references.md).
