"""Package descriptions must work on PyPI without version edits in the README."""
from email.message import Message
from pathlib import Path
import tempfile
import unittest

from tools import package_metadata, release
from tools.markdown_links import absolute_links


class PackageMetadataTests(unittest.TestCase):
    def test_build_uses_configured_identity_and_keeps_source_relative(self):
        """A future version or repository move must not leave links on an old release."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'pyproject.toml').write_text('''[project]
version = "12.3.4"
[tool.agent-box.release]
repository = "https://github.com/example/renamed/"
readme = "README.md"
changelog = "CHANGES.md"
[tool.agent-box.release.urls]
Documentation = "{repository}/blob/{tag}/docs/README.md"
''')
            source = '[Install](docs/install.md#setup)\n'
            (root / 'README.md').write_text(source)
            data = package_metadata.distribution_metadata(root)
            self.assertEqual(data['long_description'],
                             '[Install](https://github.com/example/renamed/blob/v12.3.4/docs/install.md#setup)\n')
            self.assertEqual(data['project_urls'], {
                'Documentation': 'https://github.com/example/renamed/blob/v12.3.4/docs/README.md'})
            self.assertEqual((root / 'README.md').read_text(), source)
            (root / 'CHANGES.md').write_text('## Unreleased\nFuture\n## 12.3.4\n[Guide](docs/install.md)\n## 1.0.0\nOld\n')
            self.assertEqual(package_metadata.release_notes(root),
                             '[Guide](https://github.com/example/renamed/blob/v12.3.4/docs/install.md)\n')
            for changelog in ('## 1.0.0\nOld\n', '## 12.3.4\n\n',
                              '## 12.3.4\nFirst\n## 12.3.4\nSecond\n'):
                with self.subTest(changelog=changelog):
                    (root / 'CHANGES.md').write_text(changelog)
                    with self.assertRaisesRegex(ValueError, 'one nonempty entry'):
                        package_metadata.release_notes(root)

    def test_markdown_links_preserve_markup_examples_and_external_destinations(self):
        """Transform link destinations, including badges, without rewriting code examples."""
        page = 'https://github.com/example/repo/blob/v2.0.0/README.md'
        raw = page.replace('/blob/', '/raw/')
        source = '''[Guide](<docs/a file.md#heading> "title")
[section](#section)
[guide][ref]
[ref]: docs/guide.md "Reference"
> [quoted][q]
> [q]: docs/quoted.md
[![badge](images/badge.svg)](docs/guide.md)
<a href="docs/guide.md">Guide</a><img src="images/logo.svg">
[external](https://example.com/a)
`[example](do-not-change.md)`
```markdown
[example](also-do-not-change.md)
```
'''
        result = absolute_links(source, page, raw)
        self.assertIn('<https://github.com/example/repo/blob/v2.0.0/docs/a%20file.md#heading> "title"', result)
        self.assertIn(f'[section]({page}#section)', result)
        self.assertIn('[ref]: https://github.com/example/repo/blob/v2.0.0/docs/guide.md "Reference"', result)
        self.assertIn('> [q]: https://github.com/example/repo/blob/v2.0.0/docs/quoted.md', result)
        self.assertIn('[![badge](https://github.com/example/repo/raw/v2.0.0/images/badge.svg)](https://github.com/example/repo/blob/v2.0.0/docs/guide.md)', result)
        self.assertIn('href="https://github.com/example/repo/blob/v2.0.0/docs/guide.md"', result)
        self.assertIn('src="https://github.com/example/repo/raw/v2.0.0/images/logo.svg"', result)
        self.assertTrue(result.endswith(source[source.index('[external]'):]))
        with self.assertRaisesRegex(ValueError, 'relative to the document'):
            absolute_links('[wrong](/docs/guide.md)', page, raw)

    def test_built_description_and_project_links_cannot_drift(self):
        """A backend accidentally publishing the source README must fail before upload."""
        expected = package_metadata.distribution_metadata(release.ROOT)
        info = Message()
        info['Description-Content-Type'] = expected['long_description_content_type']
        for name, url in expected['project_urls'].items():
            info['Project-URL'] = f'{name}, {url}'
        info.set_payload(expected['long_description'])
        release.check_publication_metadata(info, release.ROOT)
        info.set_payload((release.ROOT / 'README.md').read_text())
        with self.assertRaisesRegex(ValueError, 'Built description'):
            release.check_publication_metadata(info, release.ROOT)
        info.set_payload(expected['long_description'])
        del info['Project-URL']
        info['Project-URL'] = 'Documentation, https://example.com/old-version'
        with self.assertRaisesRegex(ValueError, 'Built description'):
            release.check_publication_metadata(info, release.ROOT)

    def test_current_changelog_has_publishable_notes(self):
        """Missing release notes must be caught in ordinary CI, before tagging."""
        self.assertTrue(package_metadata.release_notes(release.ROOT).strip())
