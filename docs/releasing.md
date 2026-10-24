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

CI runs on pushes to `main`, pull requests and manual dispatches. For a branch
without a PR, use `gh workflow run ci.yml --ref BRANCH` when hosted validation is
needed. Release tags run only Release, which owns the release test matrix and
runs lint once in its build job. CI keeps build/install checks on each supported
Python version and native Arch packaging checks. Both workflows use Ruff 0.17.1;
update their pins together.

1. Follow the [experimental policy](compatibility.md#semantic-versioning).
   Select an unused `0.MINOR.PATCH` version, update `project.version` in `pyproject.toml` and
   add a matching `## MAJOR.MINOR.PATCH` entry in `CHANGELOG.md`, then review
   the complete release change. Describe incompatible changes and required fresh
   setup without adding historical migrations or an upgrade matrix.
2. Validate the changes being released. For a version-and-changelog-only commit
   on top of already accepted code, run the metadata/documentation tests and
   check the intended tag and release notes:

   ```bash
   python tests/run_suite.py discover -s tests -p test_package_metadata.py
   python tests/run_suite.py discover -s tests -p test_docs_links.py
   version=$(python -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')
   python tools/release.py tag --tag "v$version"
   python tools/release.py notes
   ```

   Use this sequence as the release task's `workflow validate` command. Changes
   to implementation, migrations or packaging also need their relevant checks;
   select them for the actual diff. Do not repeat the local full suite, build
   and installer checks solely to prepare a version bump. Release CI runs the
   full Python matrix and validates one immutable build before any upload.
   A prior CI result on another commit never replaces those release gates.

   Define any additional runtime acceptance while implementing the affected
   change, before publication. Scope it to the changed behavior and available
   environment; record what remains unverified. A release request alone does
   not authorize an additional manual runtime test campaign.

   For local packaging diagnosis, the same checks remain available with the
   workflow's pinned `build`, `twine`, `uv` and `pipx` tools:

   ```bash
   python -m build
   python -m twine check --strict dist/*
   python tools/release.py artifacts --install
   ```

   `dist` must contain only this version's wheel and sdist. The helper compares
   every packaged runtime file with the source, then tests both distributions,
   uv tool and pipx using temporary integration paths. The PEP 517 build derives
   absolute, release-tagged README links and project URLs from
   `[tool.agent-box.release]` in `pyproject.toml`; documentation tests check local
   link destinations, and wheel/sdist metadata must match the generated content.
   Twine checks rendering, which alone does not prove a public link resolves.
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

   `.github/workflows/release.yml` requires a `vMAJOR.MINOR.PATCH` tag without suffixes
   matching the metadata. Tests run on Python 3.11–3.14. One build produces an
   immutable GitHub artifact; subsequent jobs download its exact artifact ID.
5. Watch the Release workflow: TestPyPI upload → verify downloaded hashes and
   clean uv/pipx installations → PyPI upload of the same files → repeat the
   download/install verification. Only the two publishing jobs receive
   `id-token: write`; they do not check out or build source. The PyPA action also
   publishes provenance attestations. Registry verification also compares the
   published description and project URLs to the checked wheel. TestPyPI/PyPI
   success are distinct checks. After release metadata and downloaded hashes pass,
   verification retries uv's exact-version-not-found resolution failure up to
   20 attempts, three seconds apart, refreshing its index cache on retries.
   Healthy installs incur no delay. Other installer failures, digest mismatches
   and installed metadata/selftest failures stop immediately.
6. Actions creates the GitHub release automatically after production verification,
   using the checked changelog entry and the same distributions. It explicitly
   marks the release as Latest, including when its numeric version is lower than
   a former release. Only that job
   receives `contents: write`; it does not check out or build source. Native Arch
   checks start as soon as the distribution artifact is ready, in parallel with
   Python tests and publication. AUR publication waits for both the checked Arch
   metadata and GitHub Release; it does not gate Python publication.
7. Record the workflow result, run URL, artifact hashes and verification limits.
   A successful Release workflow is sufficient acceptance for routine publication.
   Do not automatically rebuild images, start workspaces or provider sessions,
   or run active-workspace upgrade probes after release. Additional manual
   checks require an explicit user request or diagnosis of a concrete failure;
   they do not become a recurring release gate. Use the existing pipeline;
   additional runner infrastructure is a separate task.
   Complete the reviewed merge and verify public CI. Restore any temporarily
   disabled workflows only once their safe configuration is on the default branch.

Publication belongs to Actions. The coordinator prepares/reviews the commit,
pushes its tag, watches the run, and investigates failures. A successful run
already checks registry metadata, downloaded hashes and isolated uv/pipx installs;
repeat those downloads/installs manually only for an explicit user request or
diagnosis of a concrete failure. When recovery is
needed, inspect the failure and rerun only failed/dependent jobs; never rerun
successful uploads just to wait for an index.

No line in this runbook is evidence that an upload or live check already ran.
Record actual workflow URLs, artifact hashes, installation results and live
acceptance with the release task. AUR failure makes the overall Release run fail without blocking the independent
PyPI/GitHub publication path; any published artifacts remain valid.
Recover through the standalone AUR workflow below, without recreating the tag or
republishing Python artifacts.

## The 0.1.0 reset

Keep the PyPI and TestPyPI project `agent-box-tool`, the AUR package `agent-box`,
the command and Python import. The Python version is plain `0.1.0`, with no epoch.
Arch uses `epoch=1`, `pkgver=0.1.0`, `pkgrel=1`; subsequent Arch releases retain
that epoch. Neither choice provides application data migration.

The owner removed the old GitHub Releases and their uploaded assets. Their Git
tags and historical changelog entries remain. Preserve existing PyPI artifacts:
deleting/recreating that project would release its name and still would not
allow previously used distribution filenames to be uploaded again. An eventual
stable release must also use unused filenames/version identifiers.

1. Prepare, review and publish `v0.1.0` through the existing pipeline above.
   TestPyPI rehearses the exact artifacts before PyPI; both verification jobs
   request `agent-box-tool==0.1.0`, so older releases cannot win resolution.
   GitHub attaches the checked files and AUR publishes the checked epoch-bearing
   recipe. Do not recreate removed historical releases to make the reset work.
2. After the new release has passed verification on each Python index, use that
   index's project management UI to yank the former `1.0.0`, `1.0.1`, `1.1.0`,
   `2.0.0`, `3.0.0`, `3.0.1`, `3.0.2` and `4.0.0` releases. Give the reason
   `Unsupported former release line; development restarts at experimental 0.1.0`.
   Yanking preserves artifacts and exact-version access. Never yank the old line
   before a usable replacement has been verified.
3. Check fresh, unpinned resolution after yanking, separately on TestPyPI and
   PyPI. With the release tooling installed, the existing helper uses temporary
   tool and integration directories:

   ```bash
   python -c 'from tools.release import install_tools; install_tools("agent-box-tool", "0.1.0", "https://test.pypi.org")'
   python -c 'from tools.release import install_tools; install_tools("agent-box-tool", "0.1.0", "https://pypi.org")'
   ```

Ordinary upgrades may retain an already installed 4.x package; pins and lockfiles
may still select a yanked release. No supported transition is promised. Users
choosing 0.x may reinstall through their existing installer and configure again;
do not silently delete their settings or introduce a conversion tool. The release
acceptance is a clean current installation, not an old-version upgrade matrix.

References: [PyPI yanking](https://docs.pypi.org/project-management/yanking/),
[deleted project/filename restrictions](https://docs.pypi.org/publishing/troubleshooting/)
and [Arch epoch](https://man.archlinux.org/man/PKGBUILD.5.en).

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
- Integration application failure with a supported format: repair the reported
  access/configuration issue and launch again, or run `agent-box config apply`.
  Unsupported stored formats need fresh setup, not an automatic conversion.
  The success marker advances
  only after reconciliation succeeds. Older active workspaces remain on their
  existing runtime until explicitly restarted.

## Arch / AUR publication

`packaging/aur/PKGBUILD.in` is the source recipe. The AUR name and public maintainer
contact and epoch live in `[tool.agent-box.aur]` in `pyproject.toml`; version and repository
identity reuse the existing release metadata. The template itself is licensed
under `packaging/aur/LICENSE` (0BSD); the application remains MIT.

CI builds a candidate source archive in a disposable Arch container, explicitly
runs the source test suite after makepkg, and verifies system installation,
selftest outside the source tree, reinstallation and removal with retained user
state. No host home or
container socket is mounted into the build. The pinned Arch image is updated
by changing its digest in the CI and reusable Arch build workflows and rerunning
those checks.
The published recipe does not run development tests during user installation.
Successful builds keep backend progress in `src/build.log`; a failed build prints
that log and stops. Explicit package-data patterns in the current source include
skills and runtime assets; the recipe does not patch historical source archives.

Release calls `aur-build.yml` with the checked distribution artifact ID, so Arch
builds the exact sdist without waiting for public assets. Artifact downloads fail
on digest mismatches. The native build runs the source suite, package installation,
selftest, reinstallation and removal checks, then generates `.SRCINFO` with
`makepkg --printsrcinfo`. Only the three checked AUR files are handed off by
immutable artifact ID. After GitHub Release succeeds, `aur-publish.yml` verifies
that metadata against the published PyPI sdist hash before pushing to AUR. Build
and test jobs receive no AUR credentials.

The standalone `aur.yml` recovery workflow uses the same reusable build and
publish jobs. Without a distribution artifact input, the build fetches the
existing GitHub Release source and checks its hash against PyPI. Neither path
republishes Python artifacts.

For the first submission, or a packaging-only correction to an existing version,
run the **AUR** workflow on `main` with the existing Python release version and
Arch `revision` (`pkgrel`). Normal upstream releases start at `pkgrel=1`;
packaging-only changes to the same version require a higher `pkgrel`.
The workflow file must exist on the default branch for manual dispatch.

Configure GitHub Environment `aur` to allow only the default branch and release
tags. Add `AUR_SSH_PRIVATE_KEY` there, using a dedicated Ed25519 key whose public
half is registered to the maintainer's AUR account. Never put the private key
in this repository or a tracker comment. The reusable caller must explicitly pass
the named secret even when it exists only in the `aur` environment; the publish
job receives that environment's value. The required `workflow_call` declaration
checks the handoff, while the publish step also rejects an empty value. Standalone
dispatch uses the same environment directly. No repository-level copy is needed.
`packaging/aur/known_hosts` pins the
server key verified against the fingerprints at [the AUR homepage](https://aur.archlinux.org/).
If it changes, verify the new fingerprint independently before updating the pin.

The publishing job receives the key only in its final step. It never builds or
sources `PKGBUILD`: it verifies the checked recipe, compares the current AUR
epoch/version/revision, commits metadata and pushes normally to `master`. A global
publication concurrency group serializes our workflow writes. A newer AUR version,
a changed recipe with the same epoch/version/revision, an unexpected existing package,
or a concurrent external update refuses the push. An identical rerun is a no-op.
An AUR failure leaves the existing PyPI/GitHub release intact; correct the cause
and rerun the AUR workflow for that version, increasing `pkgrel` if needed.

For a reviewed local first publication, run the same container check with
`tools/check_arch.sh release /result VERSION PKGREL` (repository mounted read-only
at `/repo`, disposable writable output at `/result`), then use
`python tools/aur.py publish --directory RESULT/metadata` with the dedicated
key and pinned known-hosts file configured through `GIT_SSH_COMMAND`. This is
also a recovery route when the GitHub workflow is not yet on `main`.

AUR maintenance also includes checking user reports and rebuilds after Arch
Python updates. See [Arch Python packaging](https://wiki.archlinux.org/title/Python_package_guidelines)
and [AUR submission guidelines](https://wiki.archlinux.org/title/AUR_submission_guidelines).
