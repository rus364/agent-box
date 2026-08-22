import tempfile
import unittest
from pathlib import Path

from agent_box.mounts import MountError, MountSpec, parse_mount_arg, plan_mounts


class ParseMountArgTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_plain_path_is_read_write(self):
        spec = parse_mount_arg(str(self.dir))
        self.assertFalse(spec.read_only)
        self.assertEqual(spec.path, self.dir.resolve())

    def test_ro_suffix(self):
        spec = parse_mount_arg(f"{self.dir}:ro")
        self.assertTrue(spec.read_only)

    def test_rw_suffix_explicit(self):
        spec = parse_mount_arg(f"{self.dir}:rw")
        self.assertFalse(spec.read_only)

    def test_relative_path_rejected(self):
        with self.assertRaises(MountError):
            parse_mount_arg("relative/path")

    def test_nonexistent_path_rejected(self):
        with self.assertRaises(MountError):
            parse_mount_arg(str(self.dir / "does-not-exist"))

    def test_file_not_directory_rejected(self):
        f = self.dir / "file.txt"
        f.write_text("x")
        with self.assertRaises(MountError):
            parse_mount_arg(str(f))

    def test_empty_value_rejected(self):
        with self.assertRaises(MountError):
            parse_mount_arg("")

    def test_path_with_spaces(self):
        spaced = self.dir / "has spaces"
        spaced.mkdir()
        spec = parse_mount_arg(str(spaced))
        self.assertEqual(spec.path, spaced.resolve())

    def test_symlink_resolved_to_real_path(self):
        real = self.dir / "real"
        real.mkdir()
        link = self.dir / "link"
        link.symlink_to(real)
        spec = parse_mount_arg(str(link))
        self.assertEqual(spec.path, real.resolve())


class PlanMountsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        (self.root / "parent" / "child").mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def _spec(self, rel, ro=False):
        return MountSpec(path=self.root / rel, read_only=ro, label=rel)

    def test_identical_duplicate_collapses(self):
        result = plan_mounts([self._spec("parent"), self._spec("parent")])
        self.assertEqual(len(result), 1)

    def test_identical_duplicate_conflicting_mode_rejected(self):
        with self.assertRaises(MountError):
            plan_mounts([self._spec("parent", ro=True), self._spec("parent", ro=False)])

    def test_parent_contains_child_collapses_to_parent(self):
        result = plan_mounts([self._spec("parent"), self._spec("parent/child")])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].path, self.root / "parent")

    def test_order_independent_parent_child_collapse(self):
        result = plan_mounts([self._spec("parent/child"), self._spec("parent")])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].path, self.root / "parent")

    def test_ro_rw_conflict_between_parent_and_child_rejected(self):
        with self.assertRaises(MountError):
            plan_mounts([self._spec("parent", ro=True), self._spec("parent/child", ro=False)])

    def test_matching_mode_parent_child_collapses(self):
        result = plan_mounts([self._spec("parent", ro=True), self._spec("parent/child", ro=True)])
        self.assertEqual(len(result), 1)

    def test_unrelated_paths_both_kept(self):
        (self.root / "other").mkdir()
        result = plan_mounts([self._spec("parent"), self._spec("other")])
        self.assertEqual({r.path for r in result}, {self.root / "parent", self.root / "other"})

    def test_empty_input(self):
        self.assertEqual(plan_mounts([]), [])


if __name__ == "__main__":
    unittest.main()
