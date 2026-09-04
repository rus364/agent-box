import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_box import runtime, workspace
from agent_box.config import Config
from agent_box.workspace import Workspace, resolve_workspace


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


class HostGitDependencyTests(unittest.TestCase):
    """git is a host requirement, and doing without it fails silently.

    Resolution falls back to the current directory, so a box started from a
    subdirectory mounts that subdirectory and the agent simply cannot see the
    rest of the project. Nothing errors; the workspace is just wrong. Hence
    the warning at launch, and hence git in the documented requirements.
    """

    def test_without_git_the_root_falls_back_to_the_current_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            (repo / "sub").mkdir(parents=True)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            with mock.patch.object(workspace.subprocess, "run", side_effect=FileNotFoundError):
                resolved = resolve_workspace(cwd=repo / "sub")
            self.assertEqual(resolved.host_root, (repo / "sub").resolve())

    def test_a_launch_says_so_when_the_host_has_no_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Containerfile").write_text("FROM scratch\n")
            def found(name):
                return None if name == "git" else f"/usr/bin/{name}"
            with mock.patch.object(runtime.shutil, "which", side_effect=found), \
                 mock.patch.object(runtime, "resolve_workspace", return_value=Workspace(root, root)), \
                 mock.patch.object(runtime, "ensure_state_dirs"), \
                 mock.patch.object(runtime, "ensure_refresh_token"), \
                 mock.patch.object(runtime, "load_config", return_value=Config()), \
                 mock.patch.object(runtime, "image_exists", return_value=True), \
                 mock.patch.object(runtime, "image_tool_drift", return_value=([], [])), \
                 mock.patch.object(runtime, "claude_channel_args", return_value=([], [])), \
                 mock.patch.object(runtime, "launch_in_workspace_container", return_value=0), \
                 mock.patch.object(runtime, "warn") as warn, \
                 self.assertRaises(SystemExit):
                runtime.runtime_main(["claude"], root)
        self.assertTrue(any("git not found" in call.args[0] for call in warn.call_args_list))


if __name__ == "__main__":
    unittest.main()
