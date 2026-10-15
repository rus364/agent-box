"""GitHub-specific workflow edges: PR bodies that would close tasks, and draft cards."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from unittest.mock import patch

from agent_box import provider_runner, workflow, workflow_state
from agent_box import git_workflow as gw
from agent_box.config import Config
from agent_box.github_workflow import closing_references
from agent_box.repo_binding import Binding
from agent_box.tracker.core import TrackerError
from agent_box.tracker.github_extensions import run
from github_fixtures import REPO, issue, writable
from workflow_fixtures import repository


class ClosingKeywordTests(unittest.TestCase):
    def body(self, text):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "body.md"
        path.write_text(text, encoding="utf-8")
        return path

    def test_closing_keywords_are_refused_only_for_a_github_tracker(self):
        """A linked PR moves the card back to In progress and its merge closes the task before finish."""
        tracker, _ = writable()
        body = self.body("Closes #12. Fixes: octo/app#3. resolved https://github.com/octo/app/issues/9. Refs #4.")
        self.assertEqual(closing_references(tracker, body),
                         ["Closes #12", "Fixes: octo/app#3", "resolved https://github.com/octo/app/issues/9"])
        self.assertEqual(closing_references(SimpleNamespace(), "/not/read"), [])
        self.assertEqual(closing_references(tracker, self.body("Implements task #12; closes the gap in setup.")), [])


class DraftTests(unittest.TestCase):
    def test_drafts_are_listed_and_converted_into_issues_on_the_same_card(self):
        tracker, client = writable([issue(1, status="Ready")],
                                   draft_items=[{"item": "D1", "title": "Idea", "status": "Ready"}])
        self.assertEqual(run(tracker, ["drafts", "list"]), [{"item": "D1", "title": "Idea", "body": ""}])
        converted = run(tracker, ["drafts", "convert", "D1"])
        self.assertEqual((converted["id"], converted["title"], converted["state"]), ("2", "Idea", "Ready"))
        self.assertEqual(client.draft_items, [])

    def test_converting_something_that_is_not_a_draft_writes_nothing(self):
        tracker, client = writable([issue(1, status="Ready")])
        with self.assertRaisesRegex(TrackerError, "not a draft item"):
            run(tracker, ["drafts", "convert", "I-2-1"])
        self.assertEqual(client.writes, [])


class FinishTests(unittest.TestCase):
    def test_finished_subtask_is_a_completed_issue_in_done(self):
        """workflow finish needs nothing GitHub-specific: Done reaches both the issue and its card."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = repository(directory.name)
        tracker, client = writable([issue(1, status="Ready", title="Parent", children=[2]),
                                    issue(2, status="Ready", title="Child", parent=1)], login="octocat")
        config = Config()
        config.workflow.enabled = True
        config.workflow.review.policy = "off"
        config.workflow.sync.policy = "off"
        state = Path(directory.name) / "state"
        binding = Binding("github", "https://api.github.com", "octo/2")
        for item in (patch.object(workflow, "effective_config", return_value=config),
                     patch.object(workflow, "repository_root", return_value=root),
                     patch.object(Binding, "session", return_value=binding),
                     patch.object(workflow, "make_adapter", return_value=tracker),
                     patch.object(workflow, "WORKFLOW_STATE", state),
                     patch.object(workflow_state, "WORKFLOW_STATE", state),
                     patch.object(provider_runner, "WORKFLOW_STATE", state)):
            item.start()
            self.addCleanup(item.stop)

        def step(*argv):
            return workflow.dispatch(workflow.parser().parse_args(list(argv)))

        step("begin", "1")
        step("begin", "2")
        (root / "child.txt").write_text("done\n")
        gw.git(root, "add", "-A")
        gw.git(root, "commit", "-m", "child")
        step("validate", "2", "--", "true")
        self.assertEqual(step("finish", "2")["state"], "Done")
        child = client.issues[(REPO, 2)]
        self.assertEqual((child["state"], child["stateReason"], child["status"]), ("CLOSED", "COMPLETED", "Done"))
        self.assertEqual(tracker.get("1").state, "In progress")


if __name__ == "__main__":
    unittest.main()
