"""A prior PASS cannot authorize landing work that has since been reopened."""
import unittest
from agent_box.tracker.core import Task


class MergeAcceptanceTests(unittest.TestCase):
    def test_merge_dispatch_rechecks_reopened_children_and_unchecked_acceptance(self):
        from unittest.mock import Mock, patch
        from types import SimpleNamespace
        from pathlib import Path
        from agent_box import github_workflow as github
        from agent_box.config import Config
        from agent_box.errors import AgentBoxError
        parent = Task("1", "1", "Parent", state="Review", assignees=["1"], description="- [x] Accepted", children=["2"])
        child = Task("2", "1", "Child", state="Done", description="- [x] Accepted", parent="1")
        adapter = Mock()
        adapter.identity.return_value = {"id": 1}
        adapter.get.side_effect = lambda ident: {"1": parent, "2": child}[ident]
        config = Config()
        config.github.gh_token = True
        pr = {"number": 9, "url": "https://github.com/o/r/pull/9", "state": "OPEN", "isDraft": False,
              "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN", "headRefOid": "head",
              "headRefName": "ab/task", "baseRefName": "main", "latestReviews": [], "statusCheckRollup": []}
        info = {"branch": "ab/task", "base": "main"}
        args = SimpleNamespace(command="land", action="merge", method="squash")
        saved = {"validation": {"exit": 0}, "review": {"verdict": "PASS"}}
        with patch.object(github.gw, "github_origin", return_value="o/r"), patch.object(github.gw, "current_branch", return_value="ab/task"), \
             patch.object(github.gw, "head_sha", return_value="head"), patch.object(github, "view", return_value=pr), \
             patch.object(github, "find_pr", return_value=9), \
             patch.object(github, "threads", return_value=[]), patch.object(github, "gh") as command:
            child.state = "Doing"
            with self.assertRaisesRegex(AgentBoxError, "subtasks"):
                github.operate(args, Path("/tmp"), config, adapter, parent, parent, info, saved)
            child.state = "Done"
            parent.description = "- [ ] Accepted"
            with self.assertRaisesRegex(AgentBoxError, "checklist"):
                github.operate(args, Path("/tmp"), config, adapter, parent, parent, info, saved)
            command.assert_not_called()
            adapter.set_state.assert_not_called()

    def test_the_pull_request_is_looked_up_on_github_not_read_from_a_comment(self):
        """A stored number let tracker text decide which PR the workflow lands."""
        from unittest.mock import patch
        from pathlib import Path
        from agent_box import github_workflow as github
        from agent_box.errors import AgentBoxError
        rows = [{"number": 9, "state": "OPEN"}, {"number": 4, "state": "CLOSED"}]
        with patch.object(github, "gh", return_value=rows) as command:
            self.assertEqual(github.find_pr(Path("/tmp"), "o/r", "ab/task", "main"), 9)
        arguments = command.call_args.args
        self.assertIn("--head", arguments)
        self.assertEqual(arguments[arguments.index("--head") + 1], "ab/task")
        with patch.object(github, "gh", return_value=[{"number": 12, "state": "MERGED"}]):
            self.assertEqual(github.find_pr(Path("/tmp"), "o/r", "ab/task", "main"), 12)
        with patch.object(github, "gh", return_value=[{"number": 4, "state": "CLOSED"}]):
            self.assertIsNone(github.find_pr(Path("/tmp"), "o/r", "ab/task", "main"))
        with patch.object(github, "gh", return_value=[{"number": 9, "state": "OPEN"},
                                                      {"number": 10, "state": "OPEN"}]):
            with self.assertRaisesRegex(AgentBoxError, "Several pull requests"):
                github.find_pr(Path("/tmp"), "o/r", "ab/task", "main")

    def test_an_old_merged_pr_cannot_complete_work_added_after_it(self):
        """A reused branch keeps matching by head and base long after its merge."""
        from unittest.mock import Mock, patch
        from types import SimpleNamespace
        from pathlib import Path
        from agent_box import github_workflow as github
        from agent_box.config import Config
        from agent_box.errors import AgentBoxError
        parent = Task("1", "1", "Parent", state="Review", assignees=["1"], description="- [x] Accepted")
        adapter = Mock()
        adapter.identity.return_value = {"id": 1}
        adapter.get.side_effect = lambda ident: {"1": parent}[ident]
        config = Config()
        config.github.gh_token = True
        merged = {"number": 9, "url": "https://github.com/o/r/pull/9", "state": "MERGED",
                  "isDraft": False, "headRefOid": "oldhead", "headRefName": "ab/task",
                  "baseRefName": "main", "mergedAt": "2026-01-01T00:00:00Z",
                  "mergeCommit": {"oid": "mergecommit"}, "latestReviews": [], "statusCheckRollup": []}
        info = {"branch": "ab/task", "base": "main"}
        args = SimpleNamespace(command="land", action="reconcile", method="squash")
        with patch.object(github.gw, "github_origin", return_value="o/r"), \
             patch.object(github.gw, "current_branch", return_value="ab/task"), \
             patch.object(github, "view", return_value=merged), \
             patch.object(github, "find_pr", return_value=9), \
             patch.object(github, "gh") as command:
            with patch.object(github.gw, "head_sha", return_value="newhead"):
                with self.assertRaisesRegex(AgentBoxError, "not merged"):
                    github.operate(args, Path("/tmp"), config, adapter, parent, parent, info, {})
                adapter.set_state.assert_not_called()
                command.assert_not_called()
            # The normal reconcile after a real merge still completes the task.
            with patch.object(github.gw, "head_sha", return_value="oldhead"):
                result = github.operate(args, Path("/tmp"), config, adapter, parent, parent, info, {})
        self.assertEqual((result["state"], result["merge"]), ("Done", {"oid": "mergecommit"}))
        adapter.set_state.assert_called_once_with(parent, "Done")

    def test_a_new_open_pr_wins_over_the_branch_history_and_several_do_not(self):
        from unittest.mock import patch
        from pathlib import Path
        from agent_box import github_workflow as github
        from agent_box.errors import AgentBoxError
        history = [{"number": 4, "state": "MERGED"}, {"number": 9, "state": "OPEN"},
                   {"number": 5, "state": "CLOSED"}]
        with patch.object(github, "gh", return_value=history):
            self.assertEqual(github.find_pr(Path("/tmp"), "o/r", "ab/task", "main"), 9)
        with patch.object(github, "gh", return_value=[{"number": 4, "state": "MERGED"},
                                                      {"number": 6, "state": "MERGED"}]):
            with self.assertRaisesRegex(AgentBoxError, "Several pull requests"):
                github.find_pr(Path("/tmp"), "o/r", "ab/task", "main")

    def test_silence_pending_checks_and_stale_approval_block_merge(self):
        from agent_box.github_workflow import blockers
        pr = {"state": "OPEN", "isDraft": False, "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN", "headRefOid": "new",
              "latestReviews": [{"state": "APPROVED", "commit": {"oid": "old"}}],
              "statusCheckRollup": [{"status": "IN_PROGRESS", "name": "tests"}]}
        reasons = blockers(pr, [], {"validation": {"exit": 0}})
        self.assertTrue(any("CI" in reason for reason in reasons))
        self.assertTrue(any("approval" in reason for reason in reasons))
