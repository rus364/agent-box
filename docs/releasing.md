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
   Select the SemVer version, update `pyproject.toml` and `CHANGELOG.md`, and review
   the complete release change. Keep prior public fixtures and migration steps.
2. Run the full suite, selftest and lint. Build in a clean tree with release tools
   installed (`build`, `twine`, `uv`, `pipx`; the workflow pins tested versions):

   ```bash
   python -m build
   python -m twine check --strict dist/*
   python tools/release.py artifacts --install
   ```

   `dist` must contain only this version's wheel and sdist. The helper compares
   every packaged runtime file with the source, then tests both distributions,
   uv tool and pipx outside the checkout using temporary integration paths.
3. For the first release, verify both publisher settings, repository visibility,
   environment restrictions and public-safe workflows before uploading. Disable
   any legacy workflow that still uses private runners until the reviewed hosted
   workflow reaches the default branch. Public contributions must not execute on
   persistent private runners. The Claude action is restricted to the owner.
4. Tag the exact reviewed commit, then push that tag. For the first release:

   ```bash
   git tag -a v1.0.0 -m 'agent-box 1.0.0' REVIEWED_COMMIT
   git push origin v1.0.0
   ```

   `.github/workflows/release.yml` requires a stable `vMAJOR.MINOR.PATCH` tag
   matching the metadata. Tests run on Python 3.11–3.14. One build produces an
   immutable GitHub artifact; subsequent jobs download its exact artifact ID.
5. Watch the Release workflow: TestPyPI upload → verify downloaded hashes and
   clean uv/pipx installations → PyPI upload of the same files → repeat the
   download/install verification. Only the two publishing jobs receive
   `id-token: write`; they do not check out or build source. The PyPA action also
   publishes provenance attestations. TestPyPI/PyPI success are distinct checks.
6. Verify the published package can build the image, start a workspace and run a
   Claude session locally. Exercise an upgrade while a workspace is active:
   retain its old runtime and data, then explicitly stop/relaunch to use the new
   runtime. Keep these live checks distinct from unit tests and `selftest`.
7. Publish the GitHub release for the same tag with the matching changelog entry.
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
- Post-publication defect: publish a new SemVer version. If needed, the maintainer
  may yank a broken release with an explanation; do not treat deletion as rollback.
- Integration failure on upgrade: repair the reported access/configuration issue
  and launch again, or run `agent-box config apply`. The success marker advances
  only after reconciliation succeeds. Older active workspaces remain on their
  existing runtime until explicitly restarted.
