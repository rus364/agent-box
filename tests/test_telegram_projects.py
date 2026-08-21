import tempfile
import unittest
from pathlib import Path

from agent_box.telegram.projects import list_projects, resolve_allowed_project


class ProjectAllowlistTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "code"
        (self.root / "alpha").mkdir(parents=True)
        (self.root / "beta").mkdir(parents=True)
        (self.root / ".hidden").mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_lists_root_and_children_skips_hidden(self):
        entries = list_projects([str(self.root)])
        names = {e.path.name for e in entries}
        self.assertIn("alpha", names)
        self.assertIn("beta", names)
        self.assertNotIn(".hidden", names)

    def test_resolve_allowed_project_inside_root(self):
        allowed = resolve_allowed_project(self.root / "alpha", [str(self.root)])
        self.assertEqual(allowed, (self.root / "alpha").resolve())

    def test_resolve_rejects_path_outside_roots(self):
        with tempfile.TemporaryDirectory() as other:
            self.assertIsNone(resolve_allowed_project(Path(other), [str(self.root)]))

    def test_resolve_rejects_traversal_outside_root(self):
        outside = self.root / ".." / "elsewhere"
        self.assertIsNone(resolve_allowed_project(outside, [str(self.root)]))

    def test_empty_roots_yields_no_projects(self):
        self.assertEqual(list_projects([]), [])

    def test_nonexistent_root_ignored(self):
        self.assertEqual(list_projects(["/does/not/exist"]), [])

    def test_symlink_child_escaping_root_is_not_listed(self):
        with tempfile.TemporaryDirectory() as outside:
            escape_target = Path(outside) / "secret"
            escape_target.mkdir()
            (self.root / "escape").symlink_to(escape_target)
            entries = list_projects([str(self.root)])
            self.assertNotIn(escape_target.resolve(), {e.path for e in entries})
            names = {e.path.name for e in entries}
            self.assertNotIn("escape", names)

    def test_symlink_child_within_root_is_listed(self):
        real = self.root / "alpha"
        (self.root / "alias").symlink_to(real)
        entries = list_projects([str(self.root)])
        # "alias" resolves to the same path as "alpha"; only one entry for it.
        paths = [e.path for e in entries]
        self.assertEqual(paths.count(real.resolve()), 1)

    def test_resolve_allowed_project_rejects_symlink_escape(self):
        with tempfile.TemporaryDirectory() as outside:
            escape_target = Path(outside) / "secret"
            escape_target.mkdir()
            link = self.root / "escape2"
            link.symlink_to(escape_target)
            self.assertIsNone(resolve_allowed_project(link, [str(self.root)]))


if __name__ == "__main__":
    unittest.main()
