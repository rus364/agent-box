"""Lifecycle columns and subtree scope are read from the tracker, never stored.

The repository binding used to carry a view ID and a bucket ID per state, which
made a renamed or deleted column silently wrong and confined the workflow to one
project. These tests protect the replacement: every project resolves its own
board, an ambiguous board stops a mutation, and a recursive binding reaches the
whole subtree and nothing beyond it.
"""
import unittest

from agent_box.tracker.core import TrackerError
from tracker_fixtures import adapter, board, task


class SubtreeLifecycleTests(unittest.TestCase):
    def test_each_project_in_the_subtree_reads_its_own_board(self):
        """Column IDs differ per project, so one stored mapping could not serve both."""
        tasks = {5: task(5, 1, 10, 101), 6: task(6, 2, 20, 202)}
        instance, _ = adapter(tasks=tasks)
        self.assertEqual(instance.get("5").state, "Ready")
        self.assertEqual(instance.get("6").state, "Doing")

    def test_a_task_outside_the_subtree_has_no_workflow_state_and_cannot_be_moved(self):
        tasks = {7: task(7, 9, 90, 902)}
        instance, client = adapter(tasks=tasks)
        outsider = instance.get("7")
        self.assertEqual(outsider.state, "Unknown")
        with self.assertRaisesRegex(TrackerError, "subtree"):
            instance.set_state(outsider, "Doing")
        self.assertEqual(client.writes, [])

    def test_a_non_recursive_binding_stops_at_the_root_project(self):
        tasks = {6: task(6, 2, 20, 202)}
        instance, client = adapter(recursive=False, tasks=tasks)
        child = instance.get("6")
        self.assertEqual(child.state, "Unknown")
        with self.assertRaisesRegex(TrackerError, "subtree"):
            instance.set_state(child, "Doing")
        self.assertEqual(client.writes, [])

    def test_a_subtree_task_moves_on_its_own_project_board(self):
        tasks = {6: task(6, 2, 20, 201)}
        instance, client = adapter(tasks=tasks)
        moved = instance.set_state(instance.get("6"), "Doing")
        self.assertEqual(moved.state, "Doing")
        self.assertEqual(client.writes, [("/projects/2/views/20/buckets/202/tasks", {"task_id": 6})])


class BoardResolutionTests(unittest.TestCase):
    def test_done_follows_the_view_configuration_rather_than_a_column_title(self):
        """Vikunja already knows which bucket means done; that answer wins."""
        boards = {1: board(10, 100, done_bucket=100,
                           titles=("Done", "Ready", "Doing", "Review", "Backlog"))}
        instance, _ = adapter(boards=boards)
        self.assertEqual(instance.lifecycle("1")["states"]["Done"], "100")
        self.assertEqual(instance.lifecycle("1")["problems"], [])

    def test_a_missing_column_is_reported_and_blocks_the_move_but_not_the_read(self):
        boards = {1: board(10, 100, titles=("Backlog", "Doing", "Review", "Done"))}
        instance, client = adapter(boards=boards, tasks={5: task(5, 1, 10, 101)})
        self.assertIn("no column for Ready", instance.lifecycle("1")["problems"])
        self.assertEqual(instance.get("5").state, "Unknown")
        with self.assertRaisesRegex(TrackerError, "no column for Ready"):
            instance.set_state(instance.get("5"), "Doing")
        self.assertEqual(client.writes, [])

    def test_two_columns_claiming_one_state_are_ambiguous_rather_than_first_wins(self):
        boards = {1: board(10, 100, titles=("Backlog", "Ready", "Doing", "Review", "Done"))}
        boards[1]["buckets"][10].append({"id": 106, "title": "In progress"})
        instance, _ = adapter(boards=boards)
        self.assertIn("several columns match Doing", instance.lifecycle("1")["problems"])

    def test_a_second_kanban_view_stops_the_workflow_instead_of_picking_one(self):
        boards = {1: board(10, 100, extra_views=(
            {"id": 11, "title": "Triage", "view_kind": "kanban"},))}
        instance, _ = adapter(boards=boards)
        problems = instance.lifecycle_report()[0]["problems"]
        self.assertTrue(any("several kanban views" in problem for problem in problems), problems)

    def test_a_filter_built_board_cannot_receive_a_hand_placed_card(self):
        boards = {1: board(10, 100)}
        boards[1]["views"][0]["bucket_configuration_mode"] = "filter"
        instance, _ = adapter(boards=boards, tasks={5: task(5, 1, 10, 101)})
        self.assertIn("filters", " ".join(instance.lifecycle("1")["problems"]))
        with self.assertRaises(TrackerError):
            instance.set_state(instance.get("5"), "Doing")

    def test_the_report_covers_every_project_the_binding_governs(self):
        instance, _ = adapter()
        self.assertEqual([entry["project"] for entry in instance.lifecycle_report()], ["1", "2"])


if __name__ == "__main__":
    unittest.main()
