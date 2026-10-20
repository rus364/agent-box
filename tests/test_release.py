"""Release guards stop wrong tags and incomplete or altered distribution payloads."""
import io
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock
import urllib.error
import zipfile

from tools import release, package_metadata


class ReleaseGuardTests(unittest.TestCase):
    def test_only_matching_stable_semver_tags_can_publish(self):
        """Branches, prereleases and mismatched versions cannot reach production jobs."""
        metadata = {"name": "agent-box-tool", "version": "1.0.0"}
        release.check_tag("v1.0.0", metadata)
        for tag in ("main", "1.0.0", "v01.0.0", "v1.0.0rc1", "v1.0.1", "v1.0.0+build"):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                release.check_tag(tag, metadata)
        with self.assertRaises(ValueError):
            release.check_tag("v1.0.0", {**metadata, "name": "another-project"})

    def test_artifacts_must_contain_identical_complete_runtime_files(self):
        """A valid archive is not enough: omitted or altered package resources block upload."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "agent_box"
            package.mkdir()
            (package / "__init__.py").write_bytes(b'')
            (package / "container-launcher").write_bytes(b'#!/bin/sh\n')
            (root / "pyproject.toml").write_text('[project]\nname="agent-box-tool"\nversion="1.0.0"\nrequires-python=">=3.11"\n')
            with (root / "pyproject.toml").open("a") as config:
                config.write('''[tool.agent-box.release]
repository="https://github.com/example/repo"
readme="README.md"
[tool.agent-box.release.urls]
Documentation="{repository}/blob/{tag}/docs/README.md"
''')
            (root / "README.md").write_text("[Guide](docs/README.md)\n")
            metadata = package_metadata.distribution_metadata(root)
            info_text = ('Name: agent-box-tool\nVersion: 1.0.0\nRequires-Python: >=3.11\n'
                         'Description-Content-Type: text/markdown\n'
                         + ''.join(f'Project-URL: {key}, {url}\n' for key, url in metadata['project_urls'].items())
                         + '\n' + metadata['long_description'])
            (root / "PKG-INFO").write_text(info_text)
            dist = root / "dist"
            dist.mkdir()
            wheel = dist / "agent_box_tool-1.0.0-py3-none-any.whl"
            source = dist / "agent_box_tool-1.0.0.tar.gz"
            with zipfile.ZipFile(wheel, "w") as archive:
                for path in package.iterdir():
                    info = zipfile.ZipInfo("agent_box/" + path.name)
                    info.external_attr = 0o100755 << 16
                    archive.writestr(info, path.read_bytes())
                archive.writestr('agent_box_tool-1.0.0.dist-info/METADATA',
                                 info_text)
            with tarfile.open(source, "w:gz") as archive:
                for path in [root / "pyproject.toml", root / "PKG-INFO", *package.iterdir()]:
                    info = tarfile.TarInfo("agent_box_tool-1.0.0/" + str(path.relative_to(root)))
                    content = path.read_bytes()
                    info.size = len(content)
                    archive.addfile(info, io.BytesIO(content))
            self.assertEqual(release.artifacts(dist, root), [wheel, source])
            (package / "new-resource.conf").write_text("required\n")
            with self.assertRaisesRegex(ValueError, "resources differ"):
                release.artifacts(dist, root)
            (package / "new-resource.conf").unlink()
            (package / "container-launcher").write_text("altered\n")
            with self.assertRaisesRegex(ValueError, "resources differ"):
                release.artifacts(dist, root)

    def test_leftover_distributions_cannot_be_uploaded_with_a_new_release(self):
        """An old artifact in dist must not silently join the intended release."""
        with tempfile.TemporaryDirectory() as directory:
            dist = Path(directory)
            (dist / "old.whl").write_text("leftover")
            with self.assertRaisesRegex(ValueError, "exactly"):
                release.artifacts(dist)


class IndexVisibilityTests(unittest.TestCase):
    def test_installers_wait_for_simple_index_after_release_metadata_is_visible(self):
        """Metadata success alone must not send installers to a still-stale index."""
        with tempfile.TemporaryDirectory() as directory:
            wheel = Path(directory) / "agent_box_tool-1.0.0-py3-none-any.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr("agent_box_tool-1.0.0.dist-info/METADATA",
                                 "Project-URL: Source, https://example.org\n\nDescription")
            source = Path(directory) / "agent_box_tool-1.0.0.tar.gz"
            source.write_bytes(b"source")
            paths = [wheel, source]
            files = [{"filename": p.name, "url": f"https://files.example/{p.name}",
                      "digests": {"sha256": hashlib.sha256(p.read_bytes()).hexdigest()}}
                     for p in paths]
            simple_files = [{"filename": f["filename"], "hashes": f["digests"]} for f in files]
            simple_reads = []

            def response(request, timeout):
                url = request.full_url if hasattr(request, "full_url") else request
                if "/simple/" in url:
                    simple_reads.append(url)
                    self.assertEqual(request.get_header("Accept"), "application/vnd.pypi.simple.v1+json")
                    visible = simple_files[:1] if len(simple_reads) == 1 else simple_files
                    return io.BytesIO(json.dumps({"files": visible}).encode())
                if "/pypi/" in url:
                    return io.BytesIO(json.dumps({"urls": files, "info": {
                        "description": "Description", "project_urls": {"Source": "https://example.org"},
                    }}).encode())
                return io.BytesIO(next(p.read_bytes() for p in paths if url.endswith(p.name)))

            def install(*args):
                self.assertEqual(len(simple_reads), 2, "wait for both files before installing")
                # An actual installer/selftest failure must not become a visibility retry.
                raise subprocess.CalledProcessError(1, "agent-box selftest")

            with mock.patch.object(release.urllib.request, "urlopen", side_effect=response), \
                    mock.patch.object(release.time, "sleep") as sleep, \
                    mock.patch.object(release, "install_tools", side_effect=install) as installers:
                with self.assertRaises(subprocess.CalledProcessError):
                    release.verify_index("https://test.example", paths, "1.0.0")
                sleep.assert_called_once_with(3)
                installers.assert_called_once_with("agent-box-tool==1.0.0", "1.0.0", "https://test.example")

    def test_visible_simple_index_adds_no_sleep(self):
        """Already published releases should not pay a fixed propagation delay."""
        content = {"files": [{"filename": "new.whl", "hashes": {"sha256": "checked"}},
                             {"filename": "old.whl", "hashes": {"sha256": "old"}}]}
        with mock.patch.object(release.urllib.request, "urlopen",
                               return_value=io.BytesIO(json.dumps(content).encode())) as fetch, \
                mock.patch.object(release.time, "sleep") as sleep:
            release.wait_for_simple_index("https://test.example", {"new.whl": "checked"})
            fetch.assert_called_once()
            sleep.assert_not_called()

    def test_simple_index_digest_mismatch_fails_without_retry(self):
        """A collision is an integrity error, not eventual publication visibility."""
        content = {"files": [{"filename": "new.whl", "hashes": {"sha256": "different"}}]}
        with mock.patch.object(release.urllib.request, "urlopen",
                               return_value=io.BytesIO(json.dumps(content).encode())) as fetch, \
                mock.patch.object(release.time, "sleep") as sleep:
            with self.assertRaisesRegex(ValueError, "Simple index digest differs"):
                release.wait_for_simple_index("https://test.example", {"new.whl": "checked"})
            fetch.assert_called_once()
            sleep.assert_not_called()

    def test_missing_or_unreachable_simple_index_has_a_bounded_wait(self):
        """A missing release or unavailable service must eventually fail the gate."""
        for unavailable in (False, True):
            with self.subTest(unavailable=unavailable):
                def response(*args, **kwargs):
                    if unavailable:
                        raise urllib.error.URLError("unavailable")
                    return io.BytesIO(b'{"files": []}')

                with mock.patch.object(release.urllib.request, "urlopen", side_effect=response) as fetch, \
                        mock.patch.object(release.time, "sleep") as sleep, \
                        mock.patch("builtins.print"):
                    with self.assertRaisesRegex(ValueError, "did not appear in the Simple index"):
                        release.wait_for_simple_index("https://test.example", {"new.whl": "checked"})
                    self.assertEqual(fetch.call_count, 20)
                    self.assertEqual(sleep.call_count, 19)
