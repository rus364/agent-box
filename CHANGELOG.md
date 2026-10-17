# Release notes

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
