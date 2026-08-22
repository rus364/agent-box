import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_box import runtime
from agent_box.mounts import MountSpec
from agent_box.workspace import Workspace


class IsolatedRuntimeTest(unittest.TestCase):
    def setUp(self):
        self._state_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._state_tmp.cleanup)
        state = Path(self._state_tmp.name) / "state"
        self._patches = [
            mock.patch.object(runtime, "STATE_ROOT", state),
            mock.patch.object(runtime, "BUILD_STATE", state / "build"),
            mock.patch.object(runtime, "NOTIFY_STATE_ROOT", state / "notify"),
            mock.patch.object(runtime, "NOTIFY_CREDENTIALS", state / "runtime/notify.json"),
        ]
        for patch in self._patches:
            patch.start()
            self.addCleanup(patch.stop)


class BuildMountsEnsuresStateDirsTests(IsolatedRuntimeTest):
    """podman fails a bind mount outright when its source is missing, so
    build_mounts must create the host-side state directories itself rather
    than trusting a caller to have done it."""

    def test_build_mounts_creates_missing_state_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state"
            with mock.patch.object(runtime, "STATE_ROOT", state_root), \
                 mock.patch.object(runtime, "BUILD_STATE", state_root / "build"), \
                 mock.patch.object(runtime, "NOTIFY_STATE_ROOT", state_root / "notify"):
                self.assertFalse(state_root.exists())
                with tempfile.TemporaryDirectory() as project:
                    workspace = Workspace(Path(project), Path(project))
                    runtime.build_mounts(workspace, [], Path(tmp))
                self.assertTrue((state_root / "cache").is_dir())
                self.assertTrue((state_root / "uv").is_dir())
                self.assertTrue((state_root / "mise").is_dir())
                self.assertTrue((state_root / "notify").is_dir())


class MountOverlapWithProjectRootTests(IsolatedRuntimeTest):
    """A --mount that's a parent of (or identical to) the project root must
    collapse to one podman --mount flag, not two overlapping ones."""

    def test_parent_extra_mount_collapses_with_project_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            project = parent / "project"
            project.mkdir()
            workspace = Workspace(project.resolve(), project.resolve())
            extra = MountSpec(path=parent.resolve(), read_only=False, label="--mount")
            argv = runtime.container_argv(workspace, [extra], project, ["codex"])
            mount_flags = [a for a in argv if a.startswith("type=bind")]
            matching_project = [f for f in mount_flags if f"src={project.resolve()},dst={project.resolve()}" in f]
            matching_parent = [f for f in mount_flags if f"src={parent.resolve()},dst={parent.resolve()}" in f]
            self.assertEqual(matching_project, [], "the narrower project mount must not survive alongside the parent")
            self.assertEqual(len(matching_parent), 1)

    def test_conflicting_ro_rw_between_extra_mount_and_project_root_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            project = parent / "project"
            project.mkdir()
            workspace = Workspace(project.resolve(), project.resolve())
            extra = MountSpec(path=parent.resolve(), read_only=True, label="--mount")
            with self.assertRaises(Exception):
                runtime.container_argv(workspace, [extra], project, ["codex"])


class ContainerArgvTests(IsolatedRuntimeTest):
    def test_same_absolute_path_mounted_no_workspace_rewrite(self):
        with tempfile.TemporaryDirectory() as project:
            workspace = Workspace(Path(project), Path(project))
            argv = runtime.container_argv(workspace, [], Path(project), ["codex", "app-server"])
            self.assertIn("podman", argv)
            self.assertIn(f"--workdir={project}", argv)
            self.assertTrue(any(f"src={project},dst={project}" in a for a in argv))
            self.assertNotIn("/workspace", " ".join(argv))

    def test_extra_env_is_passed_before_the_image(self):
        with tempfile.TemporaryDirectory() as project:
            workspace = Workspace(Path(project), Path(project))
            argv = runtime.container_argv(
                workspace, [], Path(project), ["claude"], extra_env=["FOO=1"])
            self.assertIn("--env=FOO=1", argv)
            self.assertLess(argv.index("--env=FOO=1"), argv.index("claude"))

    def test_extra_mounts_included(self):
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as extra:
            workspace = Workspace(Path(project), Path(project))
            spec = MountSpec(path=Path(extra).resolve(), read_only=True)
            argv = runtime.container_argv(workspace, [spec], Path(project), ["codex"])
            self.assertTrue(any(f"src={spec.path},dst={spec.path}" in a and "ro=true" in a for a in argv))

    def test_hardening_flags_present(self):
        with tempfile.TemporaryDirectory() as project:
            workspace = Workspace(Path(project), Path(project))
            argv = runtime.container_argv(workspace, [], Path(project), ["codex"])
            self.assertIn("--cap-drop=all", argv)
            self.assertIn("--security-opt=no-new-privileges", argv)


if __name__ == "__main__":
    unittest.main()
