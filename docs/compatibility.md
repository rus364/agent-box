# Experimental releases and stored data

## Semantic Versioning

agent-box uses the initial-development `0.MINOR.PATCH` line of
[Semantic Versioning 2.0.0](https://semver.org/), restarting at `0.1.0`.
The product is experimental. CLI commands, JSON output, configuration, defaults,
platform requirements and stored formats may change without compatibility
guarantees. Breaking changes do not require a 1.x release. Release notes describe
changes and any required fresh setup; published artifacts are never replaced.

The former 1.x–4.x releases are unsupported. Their release notes remain historical
records, not promises made by the 0.x line. There is no stable public API or
supported upgrade/downgrade path from those releases. Reinstalling and configuring
again may be necessary. See [installation](installation.md#updating) and the
[publication reset procedure](releasing.md#the-010-reset).

## Direct upgrades

No automatic migrations, historical compatibility aliases, transitional releases
or successful-upgrade matrix are provided. Current-format defaults still apply
when an optional setting is absent; that is not a promise to preserve older
representations or meanings. Unsupported stored schemas stop the operation
without replacing the original. A new installation never silently deletes or
resets existing user data.

The installer (uv, pipx or pacman) owns package replacement. For supported saved
formats, the first host agent launch after a package change refreshes configured
integrations under a lock. It records success only after application completes.
Unchanged launches and read-only commands do not reapply integrations;
`agent-box config apply` remains available for manual edits and retry.
A fresh installation uses `agent-box config` to choose settings.

Package identity is independent of data schema versions. Its content digest
covers installed resources and editable copies, so a skill change can trigger
integration application without a data-format change. Rendered sessions do not
infer unavailable host settings or rewrite host integrations. Running workspaces
retain immutable runtime copies; stop and relaunch explicitly to adopt a new one.
User project files, volumes and third-party application data are not converted
or reset by changing the agent-box package.

## Stored formats

Schema numbers identify representations, not product maturity. The package reset
to `0.1.0` does not renumber them. Global configuration uses `version: 3`;
project configuration uses `version: 2`; configuration proposals and workspace
creation policies use `format_version: 2`. These schemas have no registered
historical conversion steps. An omitted envelope denotes schema 1, never the
current version; it therefore cannot make an old configuration readable.
Writers stamp the current version. Reads do not rewrite their source.

| Data | Boundary |
| --- | --- |
| Global `config.json` and repository `.agent-box.json` | Independent `version`; unsupported input is refused before saving or applying integrations |
| Workflow evidence, review budgets and Plan sync records | Independent `format_version`; preserve ownership, scope, budgets and recovery state |
| Tracker import mappings, move journals and adoption receipts | Independent `format_version`; preserve external IDs and interrupted-operation recovery |
| Temporary Vikunja board plans | `format_version: 1`; bind reviewed intent to a board snapshot; unsupported formats are refused |
| Global configuration proposals | `format_version`; retain the explicit host confirmation boundary |
| Workspace creation policies | `format_version`; unreadable or unsupported policy cannot authorize cleanup |
| Native host cluster ownership | `format_version: 1`; unknown/unreadable records refuse admission and cleanup |
| Managed skill markers | `format_version`; user-edited and unowned trees remain protected |
| Native profile ownership sidecars | Content hashes distinguish owned writes and interrupted replacement from user edits |
| Review receipts and seals | `format_version`; preserve original bytes/seals and bounded retention |
| Applied-package record | `format_version`; only successful integration application advances package identity |
| Rendered session configuration, synchronized credentials, caches and build signals | Derived from current saved choices or external authorities; regenerated for new sessions |

Claude/Codex files belong to their providers or the user. agent-box reconciles only
owned entries, retaining unrelated content; provider files are not passed through
its schema converter. Unknown ownership records and user-edited integrations stay
protected even though historical package compatibility is unsupported.

## Writing a migration

No historical handlers are shipped or required during experimental development.
`agent_box/migrations.py` retains the generic `Schema`, read/write and conversion
machinery for possible future migration support. Its synthetic tests exercise
copying, ordered steps, validation, private original backups, locking, atomic
replacement and safe retry; they do not promise any old release will load.

If migration support is explicitly introduced later, version each representation
independently and register pure conversion steps only for that supported scope.
Validate before committing and preserve the original before a format-changing
write. The shared writer protects one document; it is not a transaction across
integrations or external services. Existing operation locks still guard
read/modify/write sequences.

Private backups may contain credentials. Never log, commit or silently remove
them. Current-format data preservation and integration ownership tests remain
necessary regardless of whether historical conversion support exists.
