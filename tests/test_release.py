"""Release guards stop wrong tags and incomplete or altered distribution payloads."""
import io
from pathlib import Path
import tarfile
import tempfile
import unittest
import zipfile

from tools import release


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
                                 'Name: agent-box-tool\nVersion: 1.0.0\nRequires-Python: >=3.11\n')
            with tarfile.open(source, "w:gz") as archive:
                for path in [root / "pyproject.toml", *package.iterdir()]:
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
