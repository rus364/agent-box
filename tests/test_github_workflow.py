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
        info = {"pr": 9, "github_repo": "o/r", "branch": "ab/task", "base": "main"}
        args = SimpleNamespace(command="land", action="merge", method="squash")
        saved = {"validation": {"exit": 0}, "review": {"verdict": "PASS"}}
        with patch.object(github.gw, "github_origin", return_value="o/r"), patch.object(github.gw, "current_branch", return_value="ab/task"), \
             patch.object(github.gw, "head_sha", return_value="head"), patch.object(github, "view", return_value=pr), \
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

    def test_silence_pending_checks_and_stale_approval_block_merge(self):
        from agent_box.github_workflow import blockers
        pr = {"state": "OPEN", "isDraft": False, "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN", "headRefOid": "new",
              "latestReviews": [{"state": "APPROVED", "commit": {"oid": "old"}}],
              "statusCheckRollup": [{"status": "IN_PROGRESS", "name": "tests"}]}
        reasons = blockers(pr, [], {"validation": {"exit": 0}})
        self.assertTrue(any("CI" in reason for reason in reasons))
        self.assertTrue(any("approval" in reason for reason in reasons))
