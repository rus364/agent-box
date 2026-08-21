import subprocess
import tempfile
import unittest
from pathlib import Path

from agent_box.workspace import resolve_workspace


class WorkspaceResolutionTests(unittest.TestCase):
    def test_outside_git_defaults_to_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            sub = Path(tmp) / "project"
            sub.mkdir()
            ws = resolve_workspace(cwd=sub)
            self.assertEqual(ws.host_root, sub.resolve())
            self.assertEqual(ws.cwd, sub.resolve())

    def test_same_absolute_path_preserved_in_container(self):
        # The core invariant of the new model: no /workspace, no rewriting.
        with tempfile.TemporaryDirectory() as tmp:
            ws = resolve_workspace(cwd=Path(tmp))
            self.assertEqual(ws.container_root, ws.host_root)
            self.assertEqual(ws.container_cwd, ws.cwd)

    def test_arbitrary_directory_not_under_home_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = resolve_workspace(cwd=Path(tmp))
            self.assertNotIn("code", str(ws.host_root))

    def test_git_worktree_root_used_and_cwd_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            nested = repo / "a" / "b"
            nested.mkdir(parents=True)
            ws = resolve_workspace(cwd=nested)
            self.assertEqual(ws.host_root, repo.resolve())
            self.assertEqual(ws.cwd, nested.resolve())
            self.assertEqual(ws.container_cwd, nested.resolve())

    def test_nested_git_repo_root_differs_from_outer_dir(self):
        # An unrelated git repo (not a parent of some other project) must
        # resolve to its own root, not leak any other repo's path.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "unrelated-repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            ws = resolve_workspace(cwd=repo)
            self.assertEqual(ws.host_root, repo.resolve())

    def test_override_workspace_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "big-tree"
            (root / "proj").mkdir(parents=True)
            ws = resolve_workspace(cwd=root / "proj", override=str(root))
            self.assertEqual(ws.host_root, root.resolve())
            self.assertEqual(ws.cwd, (root / "proj").resolve())

    def test_override_requires_cwd_inside_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            root.mkdir()
            outside = Path(tmp) / "elsewhere"
            outside.mkdir()
            with self.assertRaises(ValueError):
                resolve_workspace(cwd=outside, override=str(root))

    def test_override_root_itself_is_allowed_as_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            root.mkdir()
            ws = resolve_workspace(cwd=root, override=str(root))
            self.assertEqual(ws.cwd, root.resolve())


if __name__ == "__main__":
    unittest.main()
