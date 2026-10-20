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
    spec = "agent-box-tool==1.0.0"
    missing = ("error: No solution found when resolving dependencies\n"
               "  cause: Because there is no version of agent-box-tool==1.0.0 and you require "
               "agent-box-tool==1.0.0, we can conclude that your requirements are unsatisfiable.\n")

    def result(self, code=0, stderr=""):
        return subprocess.CompletedProcess(["uv"], code, stderr=stderr)

    def test_actual_installer_retries_missing_version_with_fresh_metadata(self):
        """JSON readiness cannot substitute for the installer's negotiated index view."""
        with mock.patch.object(release.subprocess, "run", side_effect=[
                self.result(1, self.missing), self.result()]) as install, \
                mock.patch.object(release.time, "sleep") as sleep, \
                mock.patch.object(release, "installed_checks") as checks, \
                mock.patch.object(release, "run") as pipx:
            release.install_tools(self.spec, "1.0.0", "https://test.example")
            self.assertEqual(install.call_count, 2)
            self.assertNotIn("--refresh", install.call_args_list[0].args[0])
            self.assertIn("--refresh", install.call_args_list[1].args[0])
            sleep.assert_called_once_with(3)
            self.assertEqual(checks.call_count, 2)
            pipx.assert_called_once()

    def test_healthy_install_needs_no_wait_or_cache_refresh(self):
        """Normal releases should not pay a fixed propagation delay."""
        with mock.patch.object(release.subprocess, "run", return_value=self.result()) as install, \
                mock.patch.object(release.time, "sleep") as sleep:
            release.install_from_index(["uv", "tool", "install", self.spec], self.spec, Path("/tmp"), {})
            install.assert_called_once()
            self.assertNotIn("--refresh", install.call_args.args[0])
            sleep.assert_not_called()

    def test_missing_version_has_a_bounded_wait_and_preserves_error(self):
        """A permanently missing version must fail instead of looping forever."""
        with mock.patch.object(release.subprocess, "run", return_value=self.result(1, self.missing)) as install, \
                mock.patch.object(release.time, "sleep") as sleep, \
                mock.patch.object(release.sys, "stderr", io.StringIO()), mock.patch("builtins.print"):
            with self.assertRaises(subprocess.CalledProcessError) as failure:
                release.install_from_index(["uv"], self.spec, Path("/tmp"), {})
            self.assertEqual(failure.exception.stderr, self.missing)
            self.assertEqual(install.call_count, 20)
            self.assertEqual(sleep.call_count, 19)

    def test_other_installer_errors_fail_immediately(self):
        """Authentication, hash, compatibility and unrelated resolution failures are not lag."""
        for error in ("401 Unauthorized", "Hash mismatch", "Requires-Python mismatch",
                      self.missing.replace(self.spec, "another-package==1.0.0")):
            with self.subTest(error=error), \
                    mock.patch.object(release.subprocess, "run", return_value=self.result(1, error)) as install, \
                    mock.patch.object(release.time, "sleep") as sleep:
                with self.assertRaises(subprocess.CalledProcessError):
                    release.install_from_index(["uv"], self.spec, Path("/tmp"), {})
                install.assert_called_once()
                sleep.assert_not_called()

    def test_installed_selftest_failure_does_not_retry_installation(self):
        """An installed but broken release must not be mistaken for propagation lag."""
        with mock.patch.object(release.subprocess, "run", return_value=self.result()) as install, \
                mock.patch.object(release, "installed_checks", side_effect=subprocess.CalledProcessError(1, "selftest")), \
                mock.patch.object(release.time, "sleep") as sleep:
            with self.assertRaises(subprocess.CalledProcessError):
                release.install_tools(self.spec, "1.0.0", "https://test.example")
            install.assert_called_once()
            sleep.assert_not_called()

    def test_local_artifact_failure_is_never_an_index_retry(self):
        """The local artifact gate has no remote propagation to wait for."""
        with mock.patch.object(release, "run", side_effect=subprocess.CalledProcessError(1, "uv")) as install, \
                mock.patch.object(release, "install_from_index") as retry:
            with self.assertRaises(subprocess.CalledProcessError):
                release.install_tools("/tmp/release.whl", "1.0.0")
            install.assert_called_once()
            retry.assert_not_called()

    def test_verified_downloads_precede_installer_acceptance(self):
        """Installer retries cannot bypass published hashes, bytes or package metadata."""
        for mismatch in (None, "digest", "bytes", "metadata"):
            with self.subTest(mismatch=mismatch), tempfile.TemporaryDirectory() as directory:
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
                if mismatch == "digest":
                    files[0]["digests"]["sha256"] = "different"
                downloads = []

                def response(url, timeout):
                    if "/pypi/" in url:
                        return io.BytesIO(json.dumps({"urls": files, "info": {
                            "description": "Different" if mismatch == "metadata" else "Description",
                            "project_urls": {"Source": "https://example.org"},
                        }}).encode())
                    downloads.append(url)
                    content = next(p.read_bytes() for p in paths if url.endswith(p.name))
                    return io.BytesIO(b"different" if mismatch == "bytes" else content)

                with mock.patch.object(release.urllib.request, "urlopen", side_effect=response), \
                        mock.patch.object(release, "install_tools") as installers, \
                        mock.patch.object(release.time, "sleep") as sleep:
                    if mismatch:
                        with self.assertRaises(ValueError):
                            release.verify_index("https://test.example", paths, "1.0.0")
                        installers.assert_not_called()
                    else:
                        release.verify_index("https://test.example", paths, "1.0.0")
                        self.assertEqual(len(downloads), 2)
                        installers.assert_called_once_with(self.spec, "1.0.0", "https://test.example")
                    sleep.assert_not_called()
