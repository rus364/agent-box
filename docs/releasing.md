# Publishing releases

## Accounts and Trusted Publishers

PyPI is the public Python package index used by normal installers. TestPyPI is
an independent rehearsal service with separate accounts, projects and publishers;
uploading there does not publish to PyPI. It is optional in the wider Python
ecosystem; this repository rehearses every release there before production.

Create accounts with two-factor authentication on both services. In each account,
add a pending publisher for the first upload, or a publisher on the existing
project for later releases:

| Field | PyPI | TestPyPI |
| --- | --- | --- |
| Project | `agent-box-tool` | `agent-box-tool` |
| GitHub owner | `rus364` | `rus364` |
| Repository | `agent-box` | `agent-box` |
| Workflow filename | `release.yml` | `release.yml` |
| Environment | `pypi` | `testpypi` |

Create the matching GitHub repository environments. Restrict their deployment
branches/tags to release tags (`v*`); add required reviewers if the repository
owner wants a manual publication gate. Match the environment spelling exactly.
No PyPI password or long-lived API token belongs in GitHub secrets or a handoff.
GitHub obtains a short-lived identity token for each publishing job.

References: [PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/),
[pending publishers](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/),
[TestPyPI](https://packaging.python.org/en/latest/guides/using-testpypi/), and the
[PyPA publishing action](https://github.com/pypa/gh-action-pypi-publish#trusted-publishing).

## Prepare and publish

1. Assess compatibility using the [migration checklist](compatibility.md#writing-a-migration).
   Select the SemVer version, update `project.version` in `pyproject.toml` and
   add a matching `## MAJOR.MINOR.PATCH` entry in `CHANGELOG.md`, then review
   the complete release change. Keep prior public fixtures and migration steps.
2. Run the full suite, selftest and lint. Build in a clean tree with release tools
   installed (`build`, `twine`, `uv`, `pipx`; the workflow pins tested versions):

   ```bash
   python -m build
   python -m twine check --strict dist/*
   python tools/release.py artifacts --install
   python tools/release.py notes
   ```

   `dist` must contain only this version's wheel and sdist. The helper compares
   every packaged runtime file with the source, then tests both distributions,
   uv tool and pipx outside the checkout using temporary integration paths.
   Source documentation keeps relative links, checked against local files and
   headings by the documentation tests. The standard PEP 517 build generates
   absolute, release-tagged README links and project URLs from
   `[tool.agent-box.release]` in `pyproject.toml`. Repository identity, document
   paths and project URL templates live there; the version is not repeated in
   links or scripts. Both wheel and sdist metadata must match this generated
   content and contain no relative destinations. Twine checks rendering, which
   alone does not prove that a link resolves from the PyPI page.
3. For the first release, verify both publisher settings, repository visibility,
   environment restrictions and public-safe workflows before uploading. Disable
   any legacy workflow that still uses private runners until the reviewed hosted
   workflow reaches the default branch. Public contributions must not execute on
   persistent private runners. The Claude action is restricted to the owner.
4. Tag the exact reviewed commit, then push that tag:

   ```bash
   version=$(python -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')
   git tag -a "v$version" -m "agent-box $version" REVIEWED_COMMIT
   git push origin "v$version"
   ```

   `.github/workflows/release.yml` requires a stable `vMAJOR.MINOR.PATCH` tag
   matching the metadata. Tests run on Python 3.11–3.14. One build produces an
   immutable GitHub artifact; subsequent jobs download its exact artifact ID.
5. Watch the Release workflow: TestPyPI upload → verify downloaded hashes and
   clean uv/pipx installations → PyPI upload of the same files → repeat the
   download/install verification. Only the two publishing jobs receive
   `id-token: write`; they do not check out or build source. The PyPA action also
   publishes provenance attestations. Registry verification also compares the
   published description and project URLs to the checked wheel. TestPyPI/PyPI
   success are distinct checks.
6. Verify the published package can build the image, start a workspace and run a
   Claude session locally. Exercise an upgrade while a workspace is active:
   retain its old runtime and data, then explicitly stop/relaunch to use the new
   runtime. Keep these live checks distinct from unit tests and `selftest`.
7. After production verification, the workflow creates the GitHub release using
   the checked changelog entry and the same distributions. Only this final job
   receives `contents: write`; it does not check out or build source.
   Complete the reviewed merge, restore any temporarily disabled workflows once
   their safe configuration is on the default branch, and verify public CI.

No line in this runbook is evidence that an upload or live check already ran.
Record actual workflow URLs, artifact hashes, installation results and live
acceptance with the release task. AUR packaging is not a release gate.

## Recovery

- OIDC rejection: compare owner/repository/workflow/environment and the registry
  hostname with the table. Correct the publisher/environment, then rerun failed
  jobs in the same workflow run so the checked build artifact is retained.
- Build/test failure: fix and review the change before creating a new release
  tag. Never move a tag that already published distribution files.
- Existing filename or partial upload: stop and inspect the registry's filenames
  and SHA-256 digests against the checked artifact. Uploads intentionally do not
  use `skip-existing`; a collision must not conceal different bytes. Do not
  rebuild under an already used version or delete/re-upload it. Use a new patch
  release if recovery cannot preserve the exact reviewed artifacts.
- Expired/missing GitHub artifact: do not pretend a rebuild is the same upload;
  prepare a new reviewed release when any files were already published.
- GitHub release creation failure: inspect the tag, attached files and notes
  before retrying the failed job. Creation intentionally refuses to overwrite
  an existing release; do not rebuild or replace published assets to retry it.
- Post-publication defect: publish a new SemVer version. If needed, the maintainer
  may yank a broken release with an explanation; do not treat deletion as rollback.
- Integration failure on upgrade: repair the reported access/configuration issue
  and launch again, or run `agent-box config apply`. The success marker advances
  only after reconciliation succeeds. Older active workspaces remain on their
  existing runtime until explicitly restarted.
