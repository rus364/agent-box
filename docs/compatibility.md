# Compatibility and upgrades

## Semantic Versioning

agent-box uses [Semantic Versioning 2.0.0](https://semver.org/) starting at
1.0.0. PATCH releases fix defects without breaking the public contract; MINOR
releases add backward-compatible functionality; incompatible public changes
require a MAJOR release. Release notes describe changes and any user action.
Released artifacts are immutable: a fix gets a new version.

The public contract includes documented CLI commands/options, documented JSON
output fields and meanings, saved global/project settings and their meanings,
integration ownership rules, and stated platform requirements. Additive JSON
fields are allowed; consumers must ignore fields they do not use. Human progress
messages, internal Python functions and private workflow artifacts are not a
stable programming API. Removing supported Python/platform versions or changing
an existing default incompatibly requires a major release.

Release 3.0.0 changes container permission bypass from unconditional
to opt-in for both agents. Missing `agents.claude.bypass_permissions` and
`agents.codex.bypass_permissions` now mean `false`, including on direct upgrade
from 1.x or 2.x. Enable either explicitly to restore its former behavior; see
[container permissions](configuration.md#defaults). The new boolean fields are
additive within global schema 2: no saved field is renamed or reinterpreted,
and no conversion is needed. The package major version changes independently
of the unchanged JSON format. Running immutable
runtimes require explicit stop/relaunch to adopt it.

## Direct upgrades

Workflow behavior versions are independent of package and persisted schema
versions. The additive `workflow.behavior_version` field uses `v2` when absent,
including on upgrades from published configurations; explicit `v1` preserves the
previous engineering guidance. It changes agent guidance, not existing workflow
permissions, lifecycle or review gates. No stored representation conversion is
needed; global/project schema numbers stay unchanged. New assignments use the
effective selection, while already running agent contexts and immutable workspace
runtimes retain their existing execution boundary. Repository guidance refresh is
lazy per opened project and preserves user-owned text; see
[engineering guidance](integrations/workflow.md#engineering-guidance).

Every public version can upgrade directly to any later release, including across
major versions. Use the installer that owns the package (uv/pipx or the Arch
package manager); intermediate packages are never required. The first host agent launch after an update migrates saved
data and refreshes already configured integrations automatically. `config apply`
remains available for manual edits and retry. Concurrent first launches serialize
application; success is recorded only after every application step completes.
Read-only commands do not apply integrations. A fresh install without a saved
configuration still uses `agent-box config` to choose its settings.

Package identity is recorded separately from data schema versions. A content
digest covers installed resources and editable copies, so changes to skills can
trigger application even when no data format changed. A session using a rendered
snapshot never performs automatic host application: it cannot infer missing host
settings, and its runtime may deliberately be older than the installed package.

Running workspaces retain immutable runtime copies and are not interrupted by a
package upgrade. To adopt a new runtime, stop and launch that workspace explicitly.
Workspaces, source files, volumes and third-party application data are preserved;
upgrading PostgreSQL, k3s, provider CLIs or workloads is not an agent-box data
migration. Downgrades are not supported: a binary encountering a future schema
refuses it without overwriting it. Restore a suitable backup with the matching
package when a rollback is necessary.

## Stored formats

Schema numbers are independent per format and independent of package SemVer.
The first public formats are version 1. An omitted version explicitly means 1,
including hand-written configuration fragments; it never means the latest schema.
Global and project configuration now use schema 2: `box.tools.browser` moves to
`box.browser.enabled`, preserving explicit values and absent project overrides.
Pending global proposals use format 2 to migrate browser assignments/resets while
retaining the host confirmation boundary. Other formats retain their independent versions. Writers include the version. Migrations transform a copy before current parsers
can discard renamed fields. Reading alone does not rewrite the file.

| Data | Boundary |
| --- | --- |
| Global `config.json` and repository `.agent-box.json` | Independent `version`; global migration persists on package application or explicit config writes; project writes migrate through the project configuration writer |
| Workflow evidence, review budget and Plan sync records | Independent `format_version`; retain ownership, scope, budgets and recovery state |
| Tracker import mappings, move journals, adoption receipts | Independent `format_version`; retain external IDs and interrupted-operation recovery |
| Global configuration proposals | `format_version`; preserve the existing explicit host confirmation boundary |
| Workspace creation policies | `format_version`; unreadable/future cleanup policy retains data |
| Native host cluster ownership | New `format_version: 1`; records enabled and disabled creation policy plus process identities; future/unreadable records refuse admission and cleanup. No older format needs conversion; active sessions from an older runtime require stop/relaunch |
| Managed skill markers | `format_version`; user-edited/unowned trees remain protected |
| Native profile ownership sidecars | One SHA-256 line after commit; old/new hash pair during replacement, allowing retry before or after the content write; unknown shapes and edited content remain unowned |
| Review receipts and seals | `format_version`; preserve original bytes/seals while reading; existing bounded retention still applies |
| Applied-package record | `format_version`; only successful reconciliation advances package identity |
| Rendered session configuration, synchronized credentials, caches, build signals | Derived from saved choices or external authorities; regenerate for new sessions without rewriting live snapshots |

Claude/Codex files belong to their providers or the user. agent-box reconciles only
its owned entries, retaining unrelated content; their whole files are never passed
through agent-box's schema converter. Container schema and runtime digest checks
continue to enforce the explicit restart boundary.

## Writing a migration

1. Assess every persisted format and public behavior affected by the change.
   Keep the version-1 fixtures under `tests/fixtures/compatibility/1.0.0` unchanged;
   add fixtures for later published formats and meaningful user settings.
2. For a changed stored representation, increment that format's `Schema.version`
   and add the `N -> N+1` pure function to its `steps` in `agent_box/migrations.py`.
   Retain all earlier steps. Additive fields can remain in the same schema only
   if existing readers/writers and default semantics stay compatible.
3. Preserve false, empty and explicit values. Account for historical omitted
   defaults; a new default must not silently enable credential exposure. When an
   old choice has no automatic equivalent, fail with an actionable explanation
   instead of dropping it. Keep converters free of filesystem/network side effects.
4. Validate before committing. Use the shared writer under the operation's own
   read/modify/write lock. It refuses future formats, retains an exact private
   original in the migration state directory before a schema-changing write, and
   atomically replaces a single document. Multi-file changes require their own
   resumable operation protocol; an atomic file rename is not a transaction over
   integrations or external services.
5. Test oldest-to-current and skipped releases, failure before/after backup,
   repeated execution, future-version refusal, and user data/opt-in preservation.
   Exercise actual load/save or operation entry points as well as pure conversion.
   An integration-layout change needs ownership and interrupted-apply tests even
   when no JSON schema changes. Package upgrades need active-workspace coverage.

Backup names contain the original schema and content digest. Their contents may
include credentials, so files are private and must not be logged or committed.
They are retained for explicit recovery; do not delete them as part of an upgrade.
No conversion for unpublished historical layouts is part of the public contract.
