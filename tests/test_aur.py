"""AUR publication must preserve release identity and never replace newer metadata."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from tools import aur


def metadata(version='0.1.0', revision='1', name='agent-box', epoch='1'):
    text = f'pkgbase = {name}\n\tpkgver = {version}\n\tpkgrel = {revision}\npkgname = {name}\n'
    return text + (f'epoch = {epoch}\n' if epoch is not None else '')


class AurTests(unittest.TestCase):
    def test_reusable_release_passes_only_the_required_aur_secret(self):
        """Both callers must explicitly pass the key to the gated publishing workflow."""
        workflows = Path(__file__).resolve().parents[1] / '.github/workflows'
        publish = (workflows / 'aur-publish.yml').read_text()
        contract = publish.split('\n  workflow_call:\n', 1)[1].split('\npermissions:', 1)[0]
        self.assertIn('    secrets:\n      AUR_SSH_PRIVATE_KEY:\n        required: true\n', contract)
        for name, job in (('release.yml', 'aur'), ('aur.yml', 'publish')):
            caller = (workflows / name).read_text().split(f'\n  {job}:\n', 1)[1]
            self.assertIn('    uses: ./.github/workflows/aur-publish.yml\n', caller)
            self.assertIn('    secrets:\n      AUR_SSH_PRIVATE_KEY: ${{ secrets.AUR_SSH_PRIVATE_KEY }}\n', caller)
            self.assertNotIn('secrets: inherit', caller)

    def test_aur_key_stays_in_environment_gated_publish_step(self):
        """Early native builds stay credential-free; recovery keeps the same environment gate."""
        workflows = Path(__file__).resolve().parents[1] / '.github/workflows'
        release = (workflows / 'release.yml').read_text()
        build = (workflows / 'aur-build.yml').read_text()
        publish = (workflows / 'aur-publish.yml').read_text().split('\njobs:\n', 1)[1]
        self.assertIn('\n  workflow_dispatch:\n', (workflows / 'aur.yml').read_text())
        self.assertNotIn('secrets.', build)
        self.assertNotIn('AUR_SSH_PRIVATE_KEY', release.split('\n  aur:\n', 1)[0])
        self.assertIn('\n    environment: aur\n', publish)
        self.assertEqual(publish.count('${{ secrets.AUR_SSH_PRIVATE_KEY }}'), 1)
        self.assertIn('          test -n "$AUR_SSH_PRIVATE_KEY"\n', publish)

    def test_arch_overlaps_python_release_but_aur_waits_for_both_outputs(self):
        """Native failures must not block Python publication or permit unchecked AUR pushes."""
        workflows = Path(__file__).resolve().parents[1] / '.github/workflows'
        release = (workflows / 'release.yml').read_text()
        python_jobs, native_jobs = release.split('\n  arch:\n', 1)
        arch, aur_job = native_jobs.split('\n  aur:\n', 1)
        self.assertIn('    needs: build\n', arch)
        self.assertIn('distributions-id: ${{ needs.build.outputs.artifact-id }}', arch)
        self.assertNotIn('arch', python_jobs)
        self.assertIn('    needs: [arch, github-release]\n', aur_job)
        self.assertIn('artifact-id: ${{ needs.arch.outputs.artifact-id }}', aur_job)
        for name, artifact in (('aur-build.yml', 'distributions-id'), ('aur-publish.yml', 'artifact-id')):
            content = (workflows / name).read_text()
            self.assertIn('artifact-ids: ${{ inputs.' + artifact + ' }}', content)
            self.assertIn('digest-mismatch: error', content)

    def test_recipe_build_is_quiet_but_failed_backend_diagnostics_survive(self):
        """User builds cannot run the development suite or conceal a backend failure."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'agent_box_tool-1.0.1').mkdir()
            recipe = root / 'PKGBUILD'
            recipe.write_text(aur.render('1.0.1', '2', 'a' * 64))
            executable = root / 'python'
            executable.write_text('#!/bin/sh\necho backend-diagnostic\nexit "$BUILD_EXIT"\n')
            executable.chmod(0o755)
            env = dict(os.environ, PATH=str(root) + os.pathsep + os.defpath, srcdir=str(root))
            for code in ('0', '7'):
                with self.subTest(code=code):
                    result = subprocess.run(['bash', '-ec', 'source "$1"; ! declare -F check; build',
                                             'bash', str(recipe)], cwd=root,
                                            env=dict(env, BUILD_EXIT=code), capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0 if code == '0' else 1)
                    self.assertEqual(result.stdout, '')
                    self.assertEqual(result.stderr, '' if code == '0' else 'backend-diagnostic\n')
                    self.assertEqual((root / 'build.log').read_text(), 'backend-diagnostic\n')

    def test_release_versions_and_integer_package_revisions_are_required(self):
        """Release inputs cannot introduce prereleases or shell syntax into PKGBUILD."""
        self.assertLess(aur.version_key('1.9.0', 5), aur.version_key('1.10.0', 1))
        for version, revision in [('1.0.0rc1', '1'), ('v1.0.0', '1'), ('01.0.0', '1'),
                                  ('1.0.0;echo injected', '1'), ('1.0.0', '0'), ('1.0.0', '1;echo injected')]:
            with self.subTest(version=version, revision=revision), self.assertRaises(ValueError):
                aur.version_key(version, revision)

    def test_same_release_is_idempotent_but_changed_recipe_needs_a_revision(self):
        """An old workflow or a retry cannot silently undo an AUR maintainer update."""
        with tempfile.TemporaryDirectory() as directory:
            candidate, existing = [Path(directory) / name for name in ('new', 'old')]
            for path in (candidate, existing):
                path.mkdir()
                for name in aur.FILES:
                    (path / name).write_text(metadata() if name == '.SRCINFO' else 'unchanged')
            self.assertFalse(aur.compare_candidate(candidate, existing))
            (candidate / 'PKGBUILD').write_text('changed')
            with self.assertRaisesRegex(ValueError, 'without an epoch/pkgver/pkgrel'):
                aur.compare_candidate(candidate, existing)
            (candidate / '.SRCINFO').write_text(metadata(revision='2'))
            self.assertTrue(aur.compare_candidate(candidate, existing))
            (existing / '.SRCINFO').write_text(metadata('1.0.2'))
            with self.assertRaisesRegex(ValueError, 'downgrade'):
                aur.compare_candidate(candidate, existing)

    def test_ambiguous_existing_identity_is_not_overwritten(self):
        """Duplicate identity fields cannot conceal a different package or version order."""
        for text in (metadata() + 'pkgname = another\n', 'epoch = 1\n' + metadata(),
                     metadata(epoch='-1'), metadata(epoch='1;echo injected'),
                     metadata() + 'pkgver = 2.0.0\n', metadata().replace('pkgbase = agent-box', 'pkgbase = another')):
            with self.subTest(text=text), self.assertRaises(ValueError):
                aur.srcinfo(text)

    def test_epoch_orders_the_reset_without_a_general_downgrade_bypass(self):
        """Arch can order 0.x after the old line while still refusing later downgrades."""
        with tempfile.TemporaryDirectory() as directory:
            candidate, existing = Path(directory) / 'candidate', Path(directory) / 'existing'
            candidate.mkdir()
            existing.mkdir()
            (candidate / '.SRCINFO').write_text(metadata('0.1.0', epoch='1'))
            (existing / '.SRCINFO').write_text(metadata('4.0.0', epoch=None))
            self.assertTrue(aur.compare_candidate(candidate, existing))
            for version, epoch in (('0.1.1', '1'), ('0.0.1', '2')):
                (existing / '.SRCINFO').write_text(metadata(version, epoch=epoch))
                with self.subTest(version=version, epoch=epoch), self.assertRaisesRegex(ValueError, 'downgrade'):
                    aur.compare_candidate(candidate, existing)

    def test_publisher_requires_the_reviewed_epoch_before_contacting_registries(self):
        """A modified epoch must not change resolver order outside the reviewed recipe."""
        with tempfile.TemporaryDirectory() as directory, patch('subprocess.run') as run, \
                patch.object(aur, 'published_digest') as digest:
            root = Path(directory)
            repository = aur.settings()[1]['release']['repository'].rstrip('/')
            (root / '.SRCINFO').write_text(metadata(epoch='2') + f'url = {repository}\n')
            for name in ('PKGBUILD', 'LICENSE'):
                (root / name).write_text('placeholder')
            with self.assertRaisesRegex(ValueError, 'different AUR epoch'):
                aur.publish(root)
            run.assert_not_called()
            digest.assert_not_called()

    def test_prepare_checks_archive_metadata_before_writing_recipe(self):
        """Renaming an archive must not let a different version pass as the target release."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / aur.archive_name('1.0.1')
            prefix = archive.name.removesuffix('.tar.gz')
            def build(version):
                with tarfile.open(archive, 'w:gz') as tar:
                    for name, content in {
                        'PKG-INFO': f'Name: agent-box-tool\nVersion: {version}\n',
                        'pyproject.toml': f'[project]\nname="agent-box-tool"\nversion="{version}"\n',
                    }.items():
                        info = tarfile.TarInfo(prefix + '/' + name)
                        data = content.encode()
                        info.size = len(data)
                        tar.addfile(info, io.BytesIO(data))
            build('1.0.0')
            with self.assertRaisesRegex(ValueError, 'does not match'):
                aur.prepare(archive, '1.0.1', '1', root / 'out')
            self.assertFalse((root / 'out').exists())
            build('1.0.1')
            aur.prepare(archive, '1.0.1', '1', root / 'out')
            text = (root / 'out/PKGBUILD').read_text()
            self.assertIn(hashlib.sha256(archive.read_bytes()).hexdigest(), text)
            self.assertIn('pkgver=1.0.1\npkgrel=1', text)
            self.assertIn('epoch=1\n', text)

    def test_source_download_must_match_independent_registry_digest(self):
        """GitHub Release assets must be the same source bytes already verified on PyPI."""
        with tempfile.TemporaryDirectory() as directory:
            archive = b'test source archive'
            registry = json.dumps({'urls': [{'filename': aur.archive_name('1.0.1'),
                                           'digests': {'sha256': '0' * 64}}]}).encode()
            with patch('urllib.request.urlopen', side_effect=[io.BytesIO(archive), io.BytesIO(registry)]):
                with self.assertRaisesRegex(ValueError, 'differs'):
                    aur.fetch('1.0.1', Path(directory))
            self.assertFalse(list(Path(directory).iterdir()))

    def test_unverified_publish_inputs_never_reach_git(self):
        """Unexpected files and symlinks cannot enter the credential-bearing publication step."""
        with tempfile.TemporaryDirectory() as directory, patch('subprocess.run') as run:
            root = Path(directory)
            for name in aur.FILES:
                (root / name).write_text('placeholder')
            (root / 'extra.sh').write_text('unexpected')
            with self.assertRaisesRegex(ValueError, 'only PKGBUILD'):
                aur.publish(root)
            (root / 'extra.sh').unlink()
            (root / 'PKGBUILD').unlink()
            (root / 'PKGBUILD').symlink_to(root / 'LICENSE')
            with self.assertRaisesRegex(ValueError, 'only PKGBUILD'):
                aur.publish(root)
            run.assert_not_called()

    def test_publisher_rejects_changed_source_and_unreviewed_recipe_before_git(self):
        """A valid version number alone cannot authorize a different source or build script."""
        with tempfile.TemporaryDirectory() as directory, patch('subprocess.run') as run:
            root = Path(directory)
            digest = 'a' * 64
            repository = aur.settings()[1]['release']['repository'].rstrip('/')
            info = metadata('1.0.1') + f'url = {repository}\nsource = {aur.archive_name("1.0.1")}::{aur.source_url("1.0.1")}\nsha256sums = {digest}\n'
            (root / '.SRCINFO').write_text(info)
            (root / 'LICENSE').write_text('license')
            (root / 'PKGBUILD').write_text('unreviewed shell')
            with patch.object(aur, 'published_digest', return_value='b' * 64):
                with self.assertRaisesRegex(ValueError, 'verified published source'):
                    aur.publish(root)
            with patch.object(aur, 'published_digest', return_value=digest):
                with self.assertRaisesRegex(ValueError, 'reviewed PKGBUILD'):
                    aur.publish(root)
            run.assert_not_called()
