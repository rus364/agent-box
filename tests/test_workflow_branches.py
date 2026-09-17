"""Branch, base and pull request are derived from Git, never from a tracker comment."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agent_box import git_workflow as gw
from agent_box import workflow
from agent_box import workflow_state
from agent_box.config import Config
from agent_box.errors import AgentBoxError
from agent_box.repo_binding import Binding
from agent_box.tracker.core import Task
from workflow_fixtures import BINDING, POISON, clone, origin_path, repository



class DerivedIdentityTests(unittest.TestCase):
    def test_branch_and_base_come_from_git_not_from_a_task_comment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = repository(directory)
            gw.git(root, "switch", "-c", "ab/7-parent-task")
            parent = Task("7", "1", "Parent task", state="Doing", assignees=["1"])
            adapter = Mock()
            adapter.get.return_value = parent
            adapter.comments.return_value = [{"comment": POISON}]
            _, info, _, base = workflow.resolve(root, adapter, parent)
            self.assertEqual((info["branch"], info["base"], base), ("ab/7-parent-task", "main", "main"))
            adapter.comments.assert_not_called()

    def test_no_workflow_state_is_ever_written_into_a_comment(self):
        """The reader is gone, so nothing may keep writing the fenced block."""
        source = Path(workflow.__file__).read_text()
        self.assertNotIn("agent-box-workflow", source)
        self.assertFalse(hasattr(workflow, "metadata"))
        self.assertFalse(hasattr(workflow, "record"))

    def test_a_title_change_keeps_the_branch_the_task_already_owns(self):
        with tempfile.TemporaryDirectory() as directory:
            root = repository(directory)
            gw.git(root, "switch", "-c", "ab/7-original-title")
            renamed = Task("7", "1", "A completely different title")
            self.assertEqual(gw.task_branch(root, renamed), "ab/7-original-title")

    def test_two_branches_claiming_one_task_stop_instead_of_guessing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = repository(directory)
            gw.git(root, "branch", "ab/7-first")
            gw.git(root, "branch", "ab/7-second")
            with self.assertRaisesRegex(AgentBoxError, "Several local branches"):
                gw.task_branch(root, Task("7", "1", "Parent"))

    def test_a_neighbouring_task_id_is_not_mistaken_for_this_one(self):
        with tempfile.TemporaryDirectory() as directory:
            root = repository(directory)
            gw.git(root, "branch", "ab/71-other-task")
            self.assertIsNone(gw.task_branch(root, Task("7", "1", "Parent")))

    def test_the_base_branch_is_asked_of_the_remote_not_of_the_last_fetch(self):
        """A renamed default branch must not keep sending work at the old name."""
        with tempfile.TemporaryDirectory() as directory:
            root = repository(directory)
            gw.git(root, "switch", "-c", "release")
            gw.git(root, "push", "-u", "origin", "release")
            gw.git(origin_path(directory), "symbolic-ref", "HEAD", "refs/heads/release")
            # The local copy still says main; the remote is the authority.
            self.assertEqual(gw.git(root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"), "origin/main")
            gw._DEFAULT_BASE.clear()
            self.assertEqual(gw.default_base(root), "release")

    def test_an_unreachable_remote_falls_back_to_the_last_known_default(self):
        with tempfile.TemporaryDirectory() as directory:
            root = repository(directory)
            gw.git(root, "remote", "set-url", "origin", str(Path(directory) / "gone.git"))
            gw._DEFAULT_BASE.clear()
            self.assertEqual(gw.default_base(root), "main")

    def test_a_repository_without_a_known_default_branch_refuses_to_guess(self):
        with tempfile.TemporaryDirectory() as directory:
            root = repository(directory, remote=False)
            with self.assertRaisesRegex(AgentBoxError, "default branch"):
                gw.default_base(root)



class ParentSubtaskBranchTests(unittest.TestCase):
    """Each task keeps its own branch, and a subtask still forks from its parent."""

    def test_begin_forks_the_subtask_from_the_parent_branch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = repository(directory)
            parent = Task("7", "1", "Parent deliverable", state="Ready", assignees=[])
            child = Task("8", "1", "First subtask", state="Ready", parent="7")
            adapter = Mock()
            adapter.identity.return_value = {"id": 1}
            adapter.get.side_effect = lambda ident: {"7": parent, "8": child}[ident]

            def claimed(_adapter, task_id, resume=False):
                task = {"7": parent, "8": child}[task_id]
                starting = task.state == "Ready"
                task.state, task.assignees = "Doing", ["1"]
                return task, starting

            with patch.object(workflow, "claim", side_effect=claimed), \
                 patch.object(workflow, "WORKFLOW_STATE", Path(directory) / "state"):
                result = workflow.begin(root, adapter, child, resume=False)
            self.assertEqual(result["base"], "main")
            self.assertEqual(gw.current_branch(root), "ab/8-first-subtask")
            self.assertTrue(gw._is_ancestor(root, "ab/7-parent-deliverable", "ab/8-first-subtask"))
            # The parent branch already exists for the next subtask; that is not a resume.
            with patch.object(workflow, "claim", side_effect=claimed):
                workflow.begin(root, adapter, parent, resume=False)
            self.assertEqual(gw.current_branch(root), "ab/7-parent-deliverable")



class FreshCloneBranchTests(unittest.TestCase):
    """Work already pushed for a task must be continued, never forked past."""

    def published(self, directory, branch):
        root = repository(directory)
        gw.git(root, "switch", "-c", branch)
        gw.git(root, "commit", "--allow-empty", "-m", "published work")
        gw.git(root, "push", "-u", "origin", branch)
        return root, gw.git(root, "rev-parse", "HEAD")

    def test_a_remote_only_task_branch_is_the_task_branch(self):
        with tempfile.TemporaryDirectory() as directory:
            self.published(directory, "ab/7-parent-deliverable")
            other = clone(directory)
            self.assertEqual(gw._branch_refs(other, "refs/heads/ab/7-*"), [])
            self.assertEqual(gw.task_branch(other, Task("7", "1", "Parent deliverable")),
                             "ab/7-parent-deliverable")

    def test_begin_adopts_the_published_branch_instead_of_forking_the_base(self):
        with tempfile.TemporaryDirectory() as directory:
            _, published = self.published(directory, "ab/7-parent-deliverable")
            other = clone(directory)
            parent = Task("7", "1", "Parent deliverable", state="Ready")
            adapter = Mock()
            adapter.identity.return_value = {"id": 1}
            adapter.get.return_value = parent

            def claimed(_adapter, task_id, resume=False):
                starting = parent.state == "Ready"
                parent.state, parent.assignees = "Doing", ["1"]
                return parent, starting

            with patch.object(workflow, "claim", side_effect=claimed):
                workflow.begin(other, adapter, parent, resume=False)
            self.assertEqual(gw.current_branch(other), "ab/7-parent-deliverable")
            self.assertEqual(gw.head_sha(other), published)
            self.assertEqual(gw.git(other, "rev-parse", "--abbrev-ref", "ab/7-parent-deliverable@{upstream}"),
                             "origin/ab/7-parent-deliverable")

    def test_a_published_subtask_branch_is_adopted_on_top_of_its_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = repository(directory)
            gw.git(root, "switch", "-c", "ab/7-parent-deliverable")
            gw.git(root, "commit", "--allow-empty", "-m", "parent work")
            gw.git(root, "push", "-u", "origin", "ab/7-parent-deliverable")
            gw.git(root, "switch", "-c", "ab/8-first-subtask")
            gw.git(root, "commit", "--allow-empty", "-m", "subtask work")
            gw.git(root, "push", "-u", "origin", "ab/8-first-subtask")
            published = gw.git(root, "rev-parse", "HEAD")
            other = clone(directory)
            parent = Task("7", "1", "Parent deliverable", state="Doing", assignees=["1"], children=["8"])
            child = Task("8", "1", "First subtask", state="Ready", parent="7")
            adapter = Mock()
            adapter.identity.return_value = {"id": 1}
            adapter.get.side_effect = lambda ident: {"7": parent, "8": child}[ident]

            def claimed(_adapter, task_id, resume=False):
                task = {"7": parent, "8": child}[task_id]
                starting = task.state == "Ready"
                task.state, task.assignees = "Doing", ["1"]
                return task, starting

            with patch.object(workflow, "claim", side_effect=claimed):
                workflow.begin(other, adapter, child, resume=True)
            self.assertEqual(gw.current_branch(other), "ab/8-first-subtask")
            self.assertEqual(gw.head_sha(other), published)

    def test_several_published_branches_for_one_task_stop_instead_of_guessing(self):
        with tempfile.TemporaryDirectory() as directory:
            root, _ = self.published(directory, "ab/7-first-attempt")
            gw.git(root, "switch", "-c", "ab/7-second-attempt")
            gw.git(root, "push", "-u", "origin", "ab/7-second-attempt")
            other = clone(directory)
            with self.assertRaisesRegex(AgentBoxError, "Several origin branches"):
                gw.task_branch(other, Task("7", "1", "Parent"))

    def test_a_local_branch_under_a_different_name_than_the_remote_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            self.published(directory, "ab/7-published-name")
            other = clone(directory)
            gw.git(other, "branch", "ab/7-local-name", "main")
            with self.assertRaisesRegex(AgentBoxError, "reconcile them first"):
                gw.task_branch(other, Task("7", "1", "Parent"))

    def test_a_local_branch_behind_a_diverged_remote_is_not_resumed_silently(self):
        with tempfile.TemporaryDirectory() as directory:
            root, _ = self.published(directory, "ab/7-parent")
            other = clone(directory)
            gw.git(other, "switch", "-c", "ab/7-parent", "origin/ab/7-parent")
            gw.git(other, "switch", "main")
            gw.git(root, "commit", "--allow-empty", "-m", "more published work")
            gw.git(root, "push")
            gw.git(other, "fetch", "-q", "origin")
            with self.assertRaisesRegex(AgentBoxError, "diverged"):
                gw.switch_branch(other, "ab/7-parent", "main", resume=True)

    def test_a_local_branch_ahead_of_the_remote_still_resumes(self):
        with tempfile.TemporaryDirectory() as directory:
            self.published(directory, "ab/7-parent")
            other = clone(directory)
            gw.git(other, "switch", "-c", "ab/7-parent", "origin/ab/7-parent")
            gw.git(other, "commit", "--allow-empty", "-m", "local work")
            gw.git(other, "switch", "main")
            gw.switch_branch(other, "ab/7-parent", "main", resume=True)
            self.assertEqual(gw.current_branch(other), "ab/7-parent")



class SubtaskIntegrationTests(unittest.TestCase):
    """Derived branches must still carry a subtask into its parent and to Done."""

    def test_a_validated_subtask_fast_forwards_into_the_parent_branch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = repository(directory)
            parent = Task("7", "1", "Parent deliverable", state="Ready", children=["8"])
            child = Task("8", "1", "First subtask", state="Ready", parent="7",
                         description="- [x] requirement", labels=["ab:tier:1"])
            adapter = Mock()
            adapter.identity.return_value = {"id": 1}
            adapter.get.side_effect = lambda ident: {"7": parent, "8": child}[ident]
            adapter.set_state.side_effect = lambda task, state: setattr(task, "state", state) or task
            config = Config()
            config.workflow.enabled = True
            config.workflow.merge_mode = "local"
            config.workflow.auto_push = True

            def claimed(_adapter, task_id, resume=False):
                task = {"7": parent, "8": child}[task_id]
                starting = task.state == "Ready"
                task.state, task.assignees = "Doing", ["1"]
                return task, starting

            state = Path(directory) / "state"
            with patch.object(workflow, "effective_config", return_value=config), \
                 patch.object(workflow, "repository_root", return_value=root), \
                 patch.object(Binding, "load", return_value=BINDING), patch.object(workflow, "make_adapter", return_value=adapter), \
                 patch.object(workflow_state, "WORKFLOW_STATE", state), \
                 patch.object(workflow, "WORKFLOW_STATE", state), patch.object(workflow, "claim", side_effect=claimed):
                workflow.dispatch(workflow.parser().parse_args(["begin", "8"]))
                self.assertEqual(gw.current_branch(root), "ab/8-first-subtask")
                (root / "change.py").write_text("value = 1\n")
                gw.git(root, "add", ".")
                gw.git(root, "commit", "-m", "subtask work")
                workflow.dispatch(workflow.parser().parse_args(["validate", "8", "--", "true"]))
                with patch.object(gw, "push_parent") as push:
                    result = workflow.dispatch(workflow.parser().parse_args(["finish", "8"]))
                push.assert_not_called()
            self.assertEqual((result["state"], result["parent_branch"]), ("Done", "ab/7-parent-deliverable"))
            self.assertEqual(gw.current_branch(root), "ab/7-parent-deliverable")
            self.assertTrue(gw._is_ancestor(root, "ab/8-first-subtask", "ab/7-parent-deliverable"))
            self.assertEqual(child.state, "Done")



if __name__ == "__main__":
    unittest.main()
