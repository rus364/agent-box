"""AUR publication must preserve release identity and never replace newer metadata."""
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from tools import aur


def metadata(version='1.0.1', revision='1', name='agent-box'):
    return f'pkgbase = {name}\n\tpkgver = {version}\n\tpkgrel = {revision}\npkgname = {name}\n'


class AurTests(unittest.TestCase):
    def test_stable_versions_and_integer_package_revisions_are_required(self):
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
            with self.assertRaisesRegex(ValueError, 'without a pkgver/pkgrel'):
                aur.compare_candidate(candidate, existing)
            (candidate / '.SRCINFO').write_text(metadata(revision='2'))
            self.assertTrue(aur.compare_candidate(candidate, existing))
            (existing / '.SRCINFO').write_text(metadata('1.0.2'))
            with self.assertRaisesRegex(ValueError, 'downgrade'):
                aur.compare_candidate(candidate, existing)

    def test_ambiguous_existing_identity_is_not_overwritten(self):
        """Split packages and epochs need an explicit maintainer decision."""
        for text in (metadata() + 'pkgname = another\n', 'epoch = 1\n' + metadata(),
                     metadata() + 'pkgver = 2.0.0\n', metadata().replace('pkgbase = agent-box', 'pkgbase = another')):
            with self.subTest(text=text), self.assertRaises(ValueError):
                aur.srcinfo(text)

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
            info = metadata() + f'url = {repository}\nsource = {aur.archive_name("1.0.1")}::{aur.source_url("1.0.1")}\nsha256sums = {digest}\n'
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
