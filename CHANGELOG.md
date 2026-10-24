# Release notes

## 0.1.0

- Restart the product as **experimental 0.x** under the existing `agent-box-tool`
  distribution and `agent-box` command. Interfaces, configuration and stored
  formats have no compatibility guarantees. The former 1.x–4.x line is
  unsupported; reinstalling and configuring again may be necessary.
- Remove historical browser and shared-cache conversions, their parser/session
  aliases and old-release migration fixtures. Keep the generic schema/storage
  machinery, current schema identities and non-destructive refusal of unsupported
  data. No migration bridge or supported upgrade path is provided.
- Publish the Arch package with `epoch=1` so its ordinary version ordering can
  represent the reset; Python artifacts remain plain `0.1.0`. Keep package
  identity, source hash and recipe validation. GitHub publication explicitly
  marks the new release as Latest. Earlier GitHub Releases were removed;
  Git tags and historical notes remain. PyPI releases are yanked only after
  the new artifacts are published and verified.
- **Breaking:** remove whole-host `/tmp` sharing:
  `box.host_tmp`, `--host-tmp` and `--no-host-tmp` no longer configure workspace,
  browser or k3s mounts. Remove these flags from launch commands. Containers
  keep their own `/tmp`; existing explicit `--mount` behavior is unchanged.
  There is no replacement setting, fallback or migration. An old `host_tmp`
  field is ignored by normal configuration parsing. Running immutable runtimes
  keep their existing mounts until explicit stop/relaunch; host temporary files
  are not changed or cleaned up.
- Keep the general tool cache at its usual path in new workspace containers'
  writable filesystem by default, without an extra mount or volume. Enable
  **Workspace box → Keep tool cache between boxes** to retain it on the host.
  Installed tools remain persistent. Historical cache defaults are not migrated.
  Global schema 3 and workspace creation policy format 2 remain independent of
  package numbering. Changing the current setting requires stop/relaunch and
  never deletes old cache data.
- Share immutable runtime copies between workspace and browser launchers. Retire
  unused copies created by the new retention protocol after seven days, preserving
  legacy copies, recent/live copies and
  failing closed when references cannot be inventoried. Rotate abandoned atomic
  write staging older than one day using writer locks; add no cache-clean command.

## 4.0.0

- **Breaking:** Vikunja `board init` and `adopt boards`
  now inspect metadata by default. Replace bare `--apply` with an explicit
  lifecycle `--mapping FILE`, review the saved plan, then use `--plan FILE --apply`.
  Renames keep column/card IDs; plans set order and default/done buckets and reject
  changed boards. Setup/adopt propose meanings from names without reading cards;
  separately requested inexpensive-scout samples read one bounded bucket page.
  The previous automatic additive initializer is removed. Repository bindings
  and existing native boards need no stored-data migration.

- **Breaking:** workflow deliveries now default
  to 65,536 UTF-8 bytes (`workflow.text_max_bytes`). Oversized supplemental notes
  and managed reports use the configured scout for one labelled summary, retaining
  originals and outcome/finding identity. This may add a managed scout call; an
  unavailable scout, invalid summary or oversized authoritative input stops delivery
  and requires user notification. Limits never rise automatically.
- Keep diffs out of every role's initial assignment. Supply change boundaries,
  changed paths and separately saved evidence for source/diff inspection on demand.
  Mandatory repository instruction imports count toward the initial input budget.
- Add temporary `text write`/`text locate` storage, 4 KiB pages and seven-day default
  retention in the existing shared workflow directory. Native roles receive generated
  file paths; managed input and native final answers keep their direct transport.
  `workflow delegate --native` uses the same assignment builder as managed execution;
  `delegate` and `sync --output reference` can deliver large captured reports as pages.
- Require an explicit `workflow behavior --role coordinator|worker|reviewer|scout|sync`.
  This command first appeared after published v3.0.2; correcting its unpublished
  default does not itself require a major release or stored-data migration.
- Add Workflow behavior versions: v2 defaults to proportionate impact analysis,
  reuse and evidence-based disagreement; select v1 to retain prior guidance.
  The choice follows project overrides and reaches local, managed and GitHub
  review assignments without changing permissions, models or review budgets.
- Refresh intact managed AGENTS.md guidance lazily when a bound project is opened
  with an updated package. Preserve user edits and expose refresh diagnostics to
  the agent through its session. Existing immutable runtimes require relaunch.
  Stored configuration schemas are unchanged; omitted behavior selection uses v2.

Upgrade directly from any earlier public release through the installer that owns
the package. Existing configuration, boards, cards and explicit settings are
preserved; no new stored-data conversion or intermediate release is required.
For Vikunja board preparation, replace bare `--apply` with `--mapping FILE` to
save a plan, review it, then apply that same file with `--plan FILE --apply`.
Before sending large workflow inputs, configure the scout route and a deliberate
positive global `workflow.text_max_bytes`, or reduce assignment scope. Missing
settings use 65,536 bytes and seven-day temporary-text retention. Set
`workflow.behavior_version` to `v1` to retain the previous engineering guidance;
otherwise new assignments use v2. Stop/relaunch existing immutable workspaces to
adopt the new runtime and shared text mount; active sessions and workspace data
are retained until that explicit restart.

## 3.0.2

- Retry uv's exact-version-not-found resolution failure with refreshed index
  metadata instead of relying on a separate JSON readiness request. Retries are
  bounded, healthy installs incur no wait, and integrity or selftest failures
  still stop publication.
- Start native Arch checks from the checked source artifact in parallel with
  Python release checks and publication. AUR publication waits for both checked
  metadata and GitHub Release; standalone recovery reuses the same jobs.
- Shorten version-only release preparation by relying on Release CI for the full
  test matrix, build and installer checks. Clarify automatic publication and
  scoped post-publication runtime acceptance. Include the reusable workflows in
  source archives so native packaging tests can validate their contracts.

This patch changes release tooling, CI and documentation. Application runtime and
stored formats are unchanged from 3.0.1; no new migration or user action is needed.

## 3.0.1

- Wait for checked release files to appear in the installer-facing Simple index
  before verifying uv/pipx installs. This tolerates publication lag after release
  metadata becomes visible, while retaining hash checks and bounded failures.
- Avoid duplicate CI on release tags and PR branch pushes. Keep automatic checks
  on `main` and pull requests, with manual dispatch for other branches. Pin CI
  actions and use the same Ruff version in CI and Release, running release lint
  once instead of once per Python version.

This patch updates release tooling and CI; application runtime and stored formats
are unchanged from 3.0.0. Direct upgrades retain the existing compatibility rules.

## 3.0.0

- **Breaking:** container Claude and Codex no longer bypass CLI permissions
  automatically. In **Agents → Claude/Codex**, **Bypass permissions (container)**
  controls `agents.claude.bypass_permissions` and
  `agents.codex.bypass_permissions` independently, both off by default even for
  older configurations. Enable either explicitly to restore its former flag.
  Provider-native settings and explicit user CLI arguments still apply;
  `--host` and managed workflow role policy are unchanged. Keeping Codex bypass
  off removes agent-box's explicit-permission conflict with remote `/resume`;
  authenticated remote resume and phone acceptance need their own verification.
  The global JSON schema stays at 2; no saved data conversion is needed.
  Stop/relaunch running immutable workspaces to adopt the new runtime.

Upgrade directly from any 1.x or 2.x release through the installer that owns
the package. Missing bypass settings now mean off; no saved configuration is
migrated into full permissions. To restore the previous behavior, enable
**Bypass permissions (container)** separately for each agent in the host
configuration menu and save/apply. Existing native provider settings and explicit
CLI permission arguments still apply. Running sessions keep their immutable
runtime until explicitly stopped and relaunched; workspace data is retained.
The local remote-TUI check and isolated tests do not prove phone pairing or
authenticated Remote Control acceptance.

## 2.0.0

- **Breaking:** `box.k3s.enabled` now also selects the managed project cluster
  in native `--host` sessions. Both modes share one rootless-Podman sidecar,
  kubeconfig and creation policy. Host use requires local Podman and kubectl;
  remote Podman and nested host-k3s are unsupported. User kubeconfig files stay
  unchanged. To retain ordinary host Kubernetes access, stop project sessions
  and recreate with `--no-k3s` or disable the saved setting. Joining sessions
  cannot choose a conflicting cluster policy. Stop/relaunch old runtimes before
  sharing; no existing volume is migrated or deleted by upgrading. Host owners
  have an independent version-1 runtime record; future schemas are refused.
  Container stop, workspace removal and uninstall refuse while native sessions
  remain; end them with `agent-box --host stop` first.

- Give each native browser MCP process its own container identity. Overlapping
  Codex thread starts/resumes no longer collide or remove each other's browser.
  Restart the agent-box launcher to adopt the fix in existing sessions.

- Add separate off-by-default personal Chrome/Chromium access through the official
  Playwright extension and the existing pinned container runtime. Configure
  **Host → Personal browser** or use `--[no-]personal-browser` for one session.
  Browser consent remains required; profiles and cookies are never copied.
  Enabled host and workspace launches now check extension registration and open
  its store page in the selected profile when absent, with installation/relaunch
  guidance. Disabled or unverifiable registrations get distinct diagnostics.

- Reconcile workspace images automatically before new launches using a fingerprint
  of build inputs. Builds retain layer cache, serialize with explicit build commands
  and preserve the previous image on failure. Running workspaces remain unchanged.

- Move the opt-in isolated browser to a pinned Playwright MCP/Chromium container
  for both workspace and native `--host` sessions. Configure **Workspace box →
  Browser**; `--no-browser` works in both modes. No native browser stack is installed.
- Migrate global/project schema 1 directly to schema 2, preserving
  `box.tools.browser` as `box.browser.enabled`, including explicit project false.
  Running immutable workspaces need an explicit stop/relaunch to adopt the update.
- **Breaking image contract:** new workspace images no longer provide
  `playwright-mcp`, Chromium or `agent-box-browser-smoke`. Use the per-session
  `agent_box_browser` MCP integration, or install direct browser scripting tools
  in the project.

- Add off-by-default host `/tmp` sharing through `box.host_tmp` for workspaces
  and their sidecars, with checks against the actual creation-time mount.
  This broadens host access and must be selected explicitly in host settings.
- Add the optional AWS CLI v2 image group and selected AWS profile forwarding
  under **Credentials → Cloud CLI profiles → Amazon Web Services (aws)**.
  Preserve profile names, role-source chains and referenced SSO/login caches;
  diagnose unsupported or missing dependencies. No profile is selected on upgrade.
- Wait for the Codex Remote Control transport before showing pairing readiness;
  allow Ctrl-C to cancel startup. Transport readiness does not prove phone pairing.
- Fix the reusable AUR workflow secret handoff and include workflow contract
  fixtures in source archives.

Upgrade directly from any 1.x release through the installer that owns the package.
Global and project configuration migrate from schema 1 to 2 while preserving
saved settings and explicit disabled choices. Browser tooling in custom scripts
needs the replacement described above. Stop/relaunch existing sessions to adopt
the new immutable runtime and browser/k3s policy; active runtimes and workspace
volumes are retained. New personal-browser, host-tmp and AWS access stay opt-in.
Automated release checks cover packages and migrations; successful cloud login,
personal-browser consent and phone pairing require their own live acceptance.

## 1.1.0

- Add opt-in workspace TCP/UDP publication, selected host-service forwarding
  through pasta, and explicit host networking. Configure them under
  **Workspace box → Network** or with `--publish`, `--host-port`, `--network`,
  `--no-publish` and `--no-host-ports`. Running boxes require matching network
  authorization; temporary host access is never silently inherited.
- Extend `box.k3s.ports` with explicit IPv4/IPv6 bind addresses and UDP while
  preserving existing loopback TCP mappings. Move its editor to Network without
  changing its saved key. Projects can publish only on loopback.

- Add `workflow pause` and `workflow resume` to preserve unfinished task work,
  return to the base and recover existing branches without guessing. Pause
  never changes lifecycle, ownership, priority or blocker relations; decisions
  to defer/block work require the user's agreement.
- Add off-by-default `workflow.pause_push` and per-pause `--push`/`--no-push`
  for verified remote checkpoints, independent of delivery `auto_push`.

- Exclude moved Jira issues from project search and exact-title resolution
  after rereading stale Cloud search results.
- Add an opt-in Jira tracker adapter for Cloud email/API-token authentication
  and Server/Data Center personal access tokens, with native issues, subtasks,
  workflow transitions, links, comments, assignees and checklist revisions.
- Keep existing URL/token accounts compatible; Jira Cloud adds an optional
  username field. Server/DC has local live validation; Cloud has isolated
  contract tests and still awaits live acceptance.
- Automate stable Arch/AUR publication after verified PyPI and GitHub releases.
  Keep development tests in Arch CI rather than user installation, preserve
  packaged runtime resources and show backend build output only on failure.

This MINOR release adds compatible functionality without changing persisted
schema versions. Direct upgrades from 1.0.0 and 1.0.1 retain saved settings;
missing networking and pause-publication settings keep their safe defaults.
Running workspaces retain their immutable runtime: explicitly stop/relaunch
them to use the new runtime. Update through the installer that owns the package.

## 1.0.1

- Fix documentation and license links in the PyPI description: use absolute
  GitHub URLs pinned to the release tag.
- Pin the Documentation and Changelog project links to the same release, so
  they work before its changes reach the default branch.
- Generate package links from the version and repository settings during the
  build, keeping relative links in source documentation. Validate the built
  description and project URLs before upload and after registry publication.
- Create GitHub Releases automatically after PyPI verification, using the
  matching changelog entry and the same checked distribution files.

No runtime behavior or persisted data formats changed.

## 1.0.0

First public release of **agent-box**, distributed on PyPI as `agent-box-tool`.
The command remains `agent-box`; Python 3.11+ and Linux are required, with no
Python runtime dependencies. Workspace containers require rootless Podman 5+.

- Run Claude Code and Codex CLI in reusable per-project workspaces, with
  detachable sessions, selected host mounts and opt-in credential forwarding.
- Configure owned integrations, skills, native agent profiles and portable
  tracker/workflow support from the built-in configuration interface.
- Install through uv tool or pipx from PyPI or the public Git repository.
- Use SemVer and direct forward data migrations across all later releases,
  including major versions. This release establishes the first supported
  public data formats and regression fixtures.
- Refresh configured integrations automatically on the first host launch after
  a package update. Failed application remains retryable; `config apply` is
  available for manual edits and recovery.
- Preserve running workspaces on their immutable runtime until explicit
  stop/relaunch. Package upgrades preserve saved choices and workspace data.

See [installation](docs/installation.md) and
[compatibility](docs/compatibility.md) for the supported upgrade boundaries.
AUR packaging is separate follow-up work.
