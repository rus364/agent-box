"""Lifecycle columns and subtree scope are read from the tracker, never stored.

The repository binding used to carry a view ID and a bucket ID per state, which
made a renamed or deleted column silently wrong and confined the workflow to one
project. These tests protect the replacement: every project resolves its own
board, an ambiguous board stops a mutation, and a recursive binding reaches the
whole subtree and nothing beyond it.
"""
import unittest

from agent_box.repo_binding import Binding
from agent_box.tracker.core import TrackerError
from agent_box.tracker.vikunja import VikunjaAdapter


def board(view_id, first, *, done_bucket=None, titles=("Backlog", "Ready", "Doing", "Review", "Done"),
          extra_views=()):
    view = {"id": view_id, "title": "Board", "view_kind": "kanban", "bucket_configuration_mode": "manual"}
    if done_bucket is not None:
        view["done_bucket_id"] = done_bucket
    buckets = [{"id": first + index, "title": title} for index, title in enumerate(titles)]
    return {"views": [view, *extra_views], "buckets": {view_id: buckets}}


def task(ident, project, view_id, bucket_id, **fields):
    return {"id": ident, "project_id": project, "title": f"Task {ident}", "description": "",
            "done": False, "buckets": [{"id": bucket_id, "project_view_id": view_id}],
            "assignees": [], "related_tasks": {}, **fields}


class FakeClient:
    """Only the endpoints the adapter actually reaches, so a new call is visible."""

    def __init__(self, projects, boards, tasks):
        self.url = "https://tracker.example"
        self.revisions = {}
        self.projects = projects
        self.boards = boards
        self.tasks = tasks
        self.writes = []
        self.reads = []

    def paginate(self, path, params=None):
        self.reads.append(path)
        if path == "/projects":
            return [dict(project) for project in self.projects]
        raise AssertionError(path)

    def get(self, path):
        self.reads.append(path)
        parts = path.split("?")[0].strip("/").split("/")
        if parts[0] == "projects" and parts[-1] == "views":
            return {"items": self.boards[int(parts[1])]["views"]}
        if parts[0] == "projects" and parts[-1] == "buckets":
            return {"items": self.boards[int(parts[1])]["buckets"][int(parts[3])]}
        if parts[0] == "tasks":
            return dict(self.tasks[int(parts[1])])
        raise AssertionError(path)

    def put(self, path, body):
        self.writes.append((path, body))
        parts = path.strip("/").split("/")
        self.tasks[int(body["task_id"])]["buckets"] = [
            {"id": int(parts[5]), "project_view_id": int(parts[3])}]
        return {}


PROJECTS = [{"id": 1, "title": "Root", "parent_project_id": 0},
            {"id": 2, "title": "Child", "parent_project_id": 1},
            {"id": 9, "title": "Elsewhere", "parent_project_id": 0}]


def adapter(recursive=True, boards=None, tasks=None):
    defaults = {1: board(10, 100), 2: board(20, 200), 9: board(90, 900)}
    boards = {**defaults, **(boards or {})}
    tasks = tasks or {}
    client = FakeClient(PROJECTS, boards, tasks)
    binding = Binding("vikunja", "https://tracker.example", "1", recursive)
    return VikunjaAdapter(binding.server, "token", binding, client=client), client


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
