# Release notes

## Unreleased — next major version

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
  in the project. Keep this change for a major release; no version/tag is
  published by this implementation.

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
