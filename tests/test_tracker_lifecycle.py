"""Lifecycle columns and subtree scope are read from the tracker, never stored.

The repository binding used to carry a view ID and a bucket ID per state, which
made a renamed or deleted column silently wrong and confined the workflow to one
project. These tests protect the replacement: every project resolves its own
board, an ambiguous board stops a mutation, and a recursive binding writes to
the whole subtree and nothing beyond it, while any readable task reads its state.
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
        self.assertEqual(instance.get("6").state, "In progress")

    def test_a_task_outside_the_subtree_reads_its_board_state_but_cannot_be_moved(self):
        """Reading a column writes nothing; an outside blocker read as Unknown looked unfinished forever."""
        tasks = {7: task(7, 9, 90, 900)}
        instance, client = adapter(tasks=tasks)
        outsider = instance.get("7")
        self.assertEqual((outsider.state, outsider.state_reason), ("Backlog", None))
        with self.assertRaisesRegex(TrackerError, "subtree"):
            instance.set_state(outsider, "In progress")
        self.assertEqual(client.writes, [])

    def test_a_non_recursive_binding_reads_but_does_not_write_below_the_root_project(self):
        tasks = {6: task(6, 2, 20, 202)}
        instance, client = adapter(recursive=False, tasks=tasks)
        child = instance.get("6")
        self.assertEqual(child.state, "In progress")
        with self.assertRaisesRegex(TrackerError, "subtree"):
            instance.set_state(child, "In progress")
        self.assertEqual(client.writes, [])

    def test_an_unknown_state_says_why_the_board_names_none(self):
        """A bare Unknown sent the reader to the board to guess between a scope limit and a broken column."""
        boards = {9: {"views": [{"id": 90, "title": "List", "view_kind": "list"}], "buckets": {}},
                  2: board(20, 200, titles=("Backlog", "Ready", "In progress", "In review", "Done", "Icebox"))}
        tasks = {7: task(7, 9, 90, 900), 6: task(6, 2, 20, 205), 5: task(5, 1, 10, 100, buckets=[])}
        instance, _ = adapter(boards=boards, tasks=tasks)
        cases = {"7": "project board: ", "6": "column 'Icebox' maps to no lifecycle state",
                 "5": "no column of its project board"}
        for ident, reason in cases.items():
            read = instance.get(ident)
            self.assertEqual(read.state, "Unknown")
            self.assertIn(reason, read.state_reason)

    def test_a_subtree_task_moves_on_its_own_project_board(self):
        tasks = {6: task(6, 2, 20, 201)}
        instance, client = adapter(tasks=tasks)
        moved = instance.set_state(instance.get("6"), "In progress")
        self.assertEqual(moved.state, "In progress")
        self.assertEqual(client.writes, [("/projects/2/views/20/buckets/202/tasks", {"task_id": 6})])


class BoardResolutionTests(unittest.TestCase):
    def test_done_follows_the_view_configuration_rather_than_a_column_title(self):
        """Vikunja already knows which bucket means done; that answer wins."""
        boards = {1: board(10, 100, done_bucket=100,
                           titles=("Done", "Ready", "In progress", "In review", "Backlog"))}
        instance, _ = adapter(boards=boards)
        self.assertEqual(instance.lifecycle("1")["states"]["Done"], "100")
        self.assertEqual(instance.lifecycle("1")["problems"], [])

    def test_a_missing_column_is_reported_and_blocks_the_move_but_not_the_read(self):
        boards = {1: board(10, 100, titles=("Backlog", "In progress", "In review", "Done"))}
        instance, client = adapter(boards=boards, tasks={5: task(5, 1, 10, 101)})
        self.assertIn("no column for Ready", instance.lifecycle("1")["problems"])
        self.assertEqual(instance.get("5").state, "Unknown")
        with self.assertRaisesRegex(TrackerError, "no column for Ready"):
            instance.set_state(instance.get("5"), "In progress")
        self.assertEqual(client.writes, [])

    def test_columns_are_matched_by_exact_title_and_never_by_synonym(self):
        """Vikunja's default To-Do and Doing are other names, not the lifecycle; guessing would
        put a second vocabulary next to the one every skill and command uses."""
        boards = {1: board(10, 100, done_bucket=104, titles=("To-Do", "Ready", "Doing", "in review", "Done"))}
        instance, _ = adapter(boards=boards)
        resolved = instance.lifecycle("1")
        self.assertEqual(resolved["missing"], ["Backlog", "In progress"])
        self.assertEqual(resolved["states"]["In review"], "103")

    def test_two_columns_claiming_one_state_are_ambiguous_rather_than_first_wins(self):
        boards = {1: board(10, 100, titles=("Backlog", "Ready", "In progress", "In review", "Done"))}
        boards[1]["buckets"][10].append({"id": 106, "title": "In progress"})
        instance, _ = adapter(boards=boards)
        self.assertIn("several columns match In progress", instance.lifecycle("1")["problems"])

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
            instance.set_state(instance.get("5"), "In progress")

    def test_the_report_covers_every_project_the_binding_governs(self):
        instance, _ = adapter()
        self.assertEqual([entry["project"] for entry in instance.lifecycle_report()], ["1", "2"])


if __name__ == "__main__":
    unittest.main()
