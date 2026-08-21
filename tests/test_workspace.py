import subprocess
import tempfile
import unittest
from pathlib import Path

from agent_box.workspace import resolve_workspace
from agent_box.paths import CONTAINER_HOME, CONTAINER_USER, CONTAINER_WORKSPACE


class WorkspaceResolutionTests(unittest.TestCase):
    def test_outside_git_defaults_to_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            sub = Path(tmp) / "project"
            sub.mkdir()
            ws = resolve_workspace(cwd=sub)
            self.assertEqual(ws.host_root, sub.resolve())
            self.assertEqual(str(ws.relative), ".")
            self.assertEqual(ws.container_root, CONTAINER_WORKSPACE)
            self.assertEqual(ws.container_cwd, CONTAINER_WORKSPACE)

    def test_arbitrary_directory_not_under_home_code(self):
        # A user must not need a ~/code directory at all.
        with tempfile.TemporaryDirectory() as tmp:
            ws = resolve_workspace(cwd=Path(tmp))
            self.assertNotIn("code", str(ws.host_root))

    def test_git_worktree_root_used_and_relative_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            nested = repo / "a" / "b"
            nested.mkdir(parents=True)
            ws = resolve_workspace(cwd=nested)
            self.assertEqual(ws.host_root, repo.resolve())
            self.assertEqual(str(ws.relative), "a/b")
            self.assertEqual(ws.container_cwd, CONTAINER_WORKSPACE / "a/b")

    def test_override_workspace_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "big-tree"
            (root / "proj").mkdir(parents=True)
            ws = resolve_workspace(cwd=root / "proj", override=str(root))
            self.assertEqual(ws.host_root, root.resolve())
            self.assertEqual(str(ws.relative), "proj")

    def test_override_requires_cwd_inside_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            root.mkdir()
            outside = Path(tmp) / "elsewhere"
            outside.mkdir()
            with self.assertRaises(ValueError):
                resolve_workspace(cwd=outside, override=str(root))

    def test_container_identity_is_neutral(self):
        self.assertEqual(CONTAINER_USER, "agentbox")
        self.assertEqual(str(CONTAINER_HOME), "/home/agentbox")
        self.assertNotIn("rus364", str(CONTAINER_HOME))


if __name__ == "__main__":
    unittest.main()
