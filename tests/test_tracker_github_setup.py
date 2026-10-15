"""Preparing a GitHub project: board init keeps every card's value, setup binds, warnings surface."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_box.repo_binding import Binding
from agent_box.tracker import cli
from agent_box.tracker.core import TrackerError
from github_fixtures import issue, project, writable

BOARD = ("Todo", "In Progress", "Done")
P_SCALE = ("P0", "P1", "P2")


def fields(plan):
    return {field["field"]: field for field in plan["fields"]}


class BoardInitTests(unittest.TestCase):
    def test_board_template_plan_renames_and_inserts_in_lifecycle_order(self):
        tracker, client = writable(projects={2: project(status=BOARD, priority=P_SCALE)})
        plan = fields(tracker.initialize_boards(["octo/2"])[0])
        self.assertEqual(plan["Status"]["rename"], [{"from": "Todo", "to": "Backlog"}])
        self.assertEqual(plan["Status"]["order"], ["Backlog", "Ready", "In Progress", "In review", "Done"])
        self.assertEqual(plan["Priority"]["order"], ["Immediate", "Urgent", "High", "Medium", "Low"])
        self.assertEqual(client.writes, [])

    def test_apply_keeps_each_cards_value_through_a_rename(self):
        """Renaming an option keeps its ID; a card in Todo lands in Backlog, a P1 card reads High."""
        tracker, client = writable([issue(1, status="Todo", priority="P1")],
                                   projects={2: project(status=BOARD, priority=P_SCALE)})
        result = tracker.initialize_boards(["octo/2"], apply=True)[0]
        self.assertEqual((result["applied"], result["problems"]), (True, []))
        task = tracker.get("1")
        self.assertEqual((task.state, task.priority), ("Backlog", 3))

    def test_kanban_template_gains_only_what_it_lacks(self):
        tracker, client = writable(projects={2: project(priority=P_SCALE)})
        plan = fields(tracker.initialize_boards(["octo/2"], apply=True)[0])
        self.assertEqual(client.writes, ["UpdateField"])
        self.assertEqual((plan["Status"]["rename"], plan["Status"]["add"]), ([], []))

    def test_unknown_options_stay_after_the_option_they_followed(self):
        tracker, _ = writable(projects={2: project(status=("Backlog", "Blocked", "Ready", "In progress", "In review", "Done"))})
        plan = fields(tracker.initialize_boards(["octo/2"])[0])
        self.assertEqual(plan["Status"]["unrecognized"], ["Blocked"])
        self.assertEqual(plan["Status"]["order"], ["Backlog", "Blocked", "Ready", "In progress", "In review", "Done"])
        self.assertFalse(plan["Status"]["reorder"])

    def test_priority_options_in_the_wrong_order_are_reordered_keeping_values(self):
        """Views sort by option order, so Low above Immediate would put idle work first."""
        tracker, client = writable([issue(1, status="Ready", priority="Low")],
                                   projects={2: project(priority=("Low", "Medium", "High", "Urgent", "Immediate"))})
        result = fields(tracker.initialize_boards(["octo/2"], apply=True)[0])
        self.assertEqual(result["Priority"]["order"], ["Immediate", "Urgent", "High", "Medium", "Low"])
        self.assertEqual(client.writes, ["UpdateField"])
        self.assertEqual(tracker.get("1").priority, 1)

    def test_options_sharing_a_name_block_the_field_instead_of_losing_one(self):
        """GitHub replaces the whole option list; dropping one would clear every card that used it."""
        tracker, client = writable(projects={2: project(status=("Backlog", "ready", "Ready", "In progress",
                                                                 "In review", "Done"), priority=P_SCALE)})
        plan = fields(tracker.initialize_boards(["octo/2"], apply=True)[0])
        self.assertIn("share the name", plan["Status"]["blocked"])
        self.assertEqual(client.writes, ["UpdateField"])
        self.assertEqual(len(client.projects[2]["fields"]["nodes"][1]["options"]), 6)

    def test_option_added_after_the_plan_is_never_deleted(self):
        """The replacement list comes from the plan; an option added since must stop the write."""
        tracker, client = writable(projects={2: project(status=BOARD, priority=P_SCALE)})
        plan = tracker.board_plan("octo/2")
        client.projects[2]["fields"]["nodes"][2]["options"].append({"id": "late", "name": "P3"})
        with self.assertRaisesRegex(TrackerError, "changed since the plan"):
            tracker.apply_board_plan(plan)
        self.assertIn("late", [o["id"] for o in client.projects[2]["fields"]["nodes"][2]["options"]])

    def test_option_edited_after_the_plan_is_not_overwritten(self):
        tracker, client = writable(projects={2: project(status=BOARD, priority=P_SCALE)})
        plan = tracker.board_plan("octo/2")
        client.projects[2]["fields"]["nodes"][1]["options"][1]["description"] = "edited meanwhile"
        with self.assertRaisesRegex(TrackerError, "changed since the plan"):
            tracker.apply_board_plan(plan)
        self.assertEqual(client.writes, [])

    def test_missing_priority_field_is_created_and_a_clashing_one_is_left(self):
        tracker, client = writable(projects={2: project(priority=None)})
        tracker.initialize_boards(["octo/2"], apply=True)
        self.assertEqual(client.writes, ["CreateField"])
        board = project()
        board["fields"]["nodes"][2] = {"id": "F-priority", "name": "Priority", "dataType": "TEXT"}
        tracker, client = writable(projects={2: board})
        plan = fields(tracker.initialize_boards(["octo/2"], apply=True)[0])
        self.assertIn("non single-select", plan["Priority"]["blocked"])
        self.assertEqual(client.writes, [])

    def test_pull_request_workflows_are_warned_about_in_status_and_setup(self):
        board = project(workflows=[("Pull request merged", True), ("Pull request linked to issue", False),
                                   ("Item closed", True)])
        tracker, _ = writable(projects={2: board})
        warnings = tracker.lifecycle_report()[0]["warnings"]
        self.assertEqual(len(warnings), 1)
        self.assertIn("Pull request merged", warnings[0])


class SetupTests(unittest.TestCase):
    def setup(self, *argv, board=None):
        tracker, _ = writable(projects={2: board or project()})
        config = Config()
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        with patch.object(cli, "make_adapter", return_value=tracker), \
                patch.object(cli, "effective_config", return_value=config), \
                patch.object(cli, "repository_root", return_value=Path(root.name)):
            return cli.dispatch(cli.parser().parse_args(["--provider", "github", "setup", *argv]))

    def test_project_binds_by_owner_number_or_url(self):
        for reference in ("octo/2", "https://github.com/users/octo/projects/2/views/1"):
            with self.subTest(reference=reference):
                result = self.setup("--project", reference)
                self.assertEqual(result["binding"], {"provider": "github", "server": "https://api.github.com",
                                                     "project": "octo/2", "recursive": False})

    def test_board_that_cannot_express_the_lifecycle_is_not_bound(self):
        with self.assertRaisesRegex(TrackerError, "Fix the board lifecycle first"):
            self.setup("--project", "octo/2", "--apply", board=project(status=BOARD))

    def test_recursive_binding_is_refused(self):
        from agent_box.tracker.github import GitHubAdapter
        with self.assertRaisesRegex(TrackerError, "no subprojects"):
            GitHubAdapter("https://api.github.com", "t", Binding("github", "https://api.github.com", "octo/2", True))


from agent_box.config import Config  # noqa: E402

if __name__ == "__main__":
    unittest.main()
