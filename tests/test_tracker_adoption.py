"""Adopting a project that existed before agent-box did.

Two mechanisms are protected here. Board repair may only add the lifecycle
columns a board lacks, because renaming or removing a column the user put there
is a decision, not a repair. The audit may only report mechanical facts, because
everything it lists is what a coordinator then fixes by hand, and a re-run is the
only record of what is left - adoption deliberately stores no progress anywhere.
"""
import unittest

from agent_box.tracker.audit import audit, task_problems
from agent_box.tracker.core import Task, TrackerError
from test_tracker_lifecycle import adapter, board, task


def tasks(*items):
    index = {item.id: item for item in items}
    return list(items), (lambda task_id: index[task_id])


class BoardRepairTests(unittest.TestCase):
    def test_a_missing_column_is_created_between_the_columns_it_belongs_between(self):
        """A lifecycle appended to the end of the board would not read as one."""
        boards = {1: board(10, 100, titles=("Backlog", "Doing", "Done"))}
        instance, client = adapter(boards=boards)
        planned, = instance.initialize_boards(["1"])
        self.assertEqual([(column["title"], column["position"]) for column in planned["create"]],
                         [("Ready", 150.0), ("Review", 250.0)])
        self.assertEqual(client.writes, [])
        applied, = instance.initialize_boards(["1"], apply=True)
        self.assertEqual(applied["problems"], [])
        self.assertEqual(sorted(applied["states"]), ["Backlog", "Doing", "Done", "Ready", "Review"])
        self.assertEqual([bucket["title"] for bucket in instance.lifecycle("1")["buckets"]],
                         ["Backlog", "Ready", "Doing", "Review", "Done"])

    def test_a_complete_board_is_left_alone(self):
        instance, client = adapter(boards={1: board(10, 100, done_bucket=104)})
        planned, = instance.initialize_boards(["1"], apply=True)
        self.assertEqual((planned["create"], planned["applied"]), ([], False))
        self.assertEqual(client.writes, [])

    def test_an_ambiguous_board_is_reported_instead_of_being_extended(self):
        """Which of two columns means Doing is the user's answer, not a repair."""
        boards = {1: board(10, 100, titles=("Backlog", "Ready", "Doing", "Review", "Done"))}
        boards[1]["buckets"][10].append({"id": 106, "title": "In progress", "position": 600})
        instance, client = adapter(boards=boards)
        planned, = instance.initialize_boards(["1"], apply=True)
        self.assertEqual(planned["blocked"], ["several columns match Doing"])
        self.assertFalse(planned["applied"])
        self.assertEqual(client.writes, [])

    def test_a_filter_built_board_is_refused_rather_than_given_a_column(self):
        boards = {1: board(10, 100, titles=("Backlog", "Doing", "Done"))}
        boards[1]["views"][0]["bucket_configuration_mode"] = "filter"
        instance, client = adapter(boards=boards)
        planned, = instance.initialize_boards(["1"], apply=True)
        self.assertIn("board builds its columns from filters instead of manual buckets",
                      planned["blocked"])
        self.assertEqual(client.writes, [])

    def test_the_view_gains_a_done_column_only_when_it_names_none(self):
        """Vikunja's own done bucket is what marks a card finished, so it must exist."""
        boards = {1: board(10, 100, titles=("Backlog", "Ready", "Doing", "Review", "Done"))}
        instance, client = adapter(boards=boards)
        instance.initialize_boards(["1"], apply=True)
        self.assertEqual([body for path, body in client.writes if path == "/projects/1/views/10"],
                         [{"done_bucket_id": 104}])
        instance, client = adapter(boards={1: board(10, 100, done_bucket=104)})
        instance.initialize_boards(["1"], apply=True)
        self.assertEqual(client.writes, [])

    def test_a_project_whose_board_cannot_be_read_stops_only_itself(self):
        """Planning the whole subtree first is what keeps a repair from ending halfway."""
        second = {"id": 21, "title": "Other", "view_kind": "kanban",
                  "bucket_configuration_mode": "manual"}
        boards = {1: board(10, 100, done_bucket=104, titles=("Backlog", "Doing", "Done")),
                  2: board(20, 200, extra_views=(second,))}
        instance, client = adapter(boards=boards)
        first, other = instance.initialize_boards(["1", "2"], apply=True)
        self.assertTrue(first["applied"])
        self.assertEqual([column["title"] for column in first["create"]], ["Ready", "Review"])
        self.assertFalse(other["applied"])
        self.assertEqual((other["view"], other["create"]), (None, []))
        self.assertIn("several kanban views", other["blocked"][0])
        self.assertEqual({path for path, _ in client.writes}, {"/projects/1/views/10/buckets"})


class ImportMarkerTests(unittest.TestCase):
    def test_a_rewrite_may_not_drop_the_marker_a_later_import_identifies_the_task_by(self):
        """Without it the next import creates a second copy of the same task."""
        existing = task(5, 1, 10, 101, description="Goal\n\nVikunja external ID: B-01")
        instance, client = adapter(tasks={5: existing})
        current = instance.get("5")
        with self.assertRaisesRegex(TrackerError, "import marker B-01"):
            instance.update(current, {"description": "Goal\n\n- [ ] Acceptance"})
        self.assertEqual(client.writes, [])


class UnreadableBoardTests(unittest.TestCase):
    def test_a_project_with_several_kanban_views_stays_readable(self):
        """The audit exists to describe boards like this one, so reading one cannot raise."""
        second = {"id": 11, "title": "Other", "view_kind": "kanban",
                  "bucket_configuration_mode": "manual"}
        boards = {1: board(10, 100, extra_views=(second,))}
        instance, _ = adapter(boards=boards, tasks={5: task(5, 1, 10, 101)})
        self.assertEqual(instance.get("5").state, "Unknown")
        entry, = [row for row in instance.lifecycle_report() if row["project"] == "1"]
        self.assertIsNone(entry["view"])
        self.assertIn("several kanban views", entry["problems"][0])
        with self.assertRaisesRegex(TrackerError, "several kanban views"):
            instance.set_state(instance.get("5"), "Doing")

    def test_a_snapshot_carries_the_board_configuration_board_repair_changes(self):
        """Projects and tasks alone cannot restore a column that repair added."""
        from agent_box.tracker import vikunja_api

        _, client = adapter()
        captured, = vikunja_api.board_snapshot(client, 1)
        self.assertEqual(captured["view"]["id"], 10)
        self.assertEqual([bucket["title"] for bucket in captured["buckets"]],
                         ["Backlog", "Ready", "Doing", "Review", "Done"])


class BulkMarkerTests(unittest.TestCase):
    def test_a_bulk_rewrite_is_refused_at_planning_time_like_a_single_one(self):
        """An invariant only one of two write paths enforces is not an invariant."""
        from agent_box.tracker import vikunja_api

        tasks = [{"id": 5, "title": "Task", "description": "Goal\n\nVikunja external ID: B-01"}]
        with self.assertRaisesRegex(TrackerError, "import marker B-01"):
            vikunja_api.plan_bulk_updates([{"task": "5", "changes": {"description": "Goal"}}], tasks)
        kept = "Goal rewritten\n\nVikunja external ID: B-01"
        actions = vikunja_api.plan_bulk_updates([{"task": "5", "changes": {"description": kept}}], tasks)
        self.assertEqual(actions[0].details["changes"], {"description": kept})

    def test_both_write_paths_report_the_refusal_as_a_conflict(self):
        """A caller retries on 'invalid' input and stops on a conflict; the category decides."""
        from agent_box.tracker import vikunja_api

        with self.assertRaises(TrackerError) as bulk:
            vikunja_api.check_external_marker("5", "Goal\n\nVikunja external ID: B-01", "Goal")
        self.assertEqual(bulk.exception.category, "conflict")
        existing = task(5, 1, 10, 101, description="Goal\n\nVikunja external ID: B-01")
        instance, _ = adapter(tasks={5: existing})
        with self.assertRaises(TrackerError) as single:
            instance.update(instance.get("5"), {"description": "Goal"})
        self.assertEqual(single.exception.category, "conflict")


IDENTITY = {"provider": "vikunja", "server": "https://tracker.example"}
TAKEN = "2026-01-01T00:00:00Z"
BEFORE = "2025-12-01T00:00:00Z"
AFTER = "2026-02-01T00:00:00Z"


def snapshot(tasks=(), comments=None, boards=None, projects=("1",), labels=(), **identity):
    return {**IDENTITY, **identity, "taken": TAKEN,
            "projects": [{"id": int(project), "parent_project_id": 0, "title": f"P{project}"}
                         for project in projects],
            "tasks": list(tasks), "comments": comments or {}, "boards": boards or {},
            "labels": [{"id": index, "title": title} for index, title in enumerate(labels, 1)]}


def saved_task(ident, project="1", created=BEFORE, **fields):
    return {"id": ident, "project_id": int(project), "title": f"Task {ident}", "description": "d",
            "done": False, "priority": 0, "labels": [], "assignees": [], "buckets": [],
            "related_tasks": {}, "created": created, **fields}


class Inventory:
    def __init__(self, tasks, projects=("1",), labels=()):
        self.tasks = list(tasks)
        self.projects = [project if isinstance(project, dict) else
                         {"id": int(project), "parent_project_id": 0, "title": f"P{project}",
                          "created": BEFORE}
                         for project in projects]
        self.labels = [{"id": index, "title": title} for index, title in enumerate(labels, 1)]


class RestoreTests(unittest.TestCase):
    def plan(self, saved, current, boards=None, comments=None, live_comments=None,
             projects=("1",), current_projects=None, saved_labels=(), live_labels=()):
        from agent_box.tracker import vikunja_api

        return vikunja_api.plan_restore(
            snapshot(saved, comments, boards, projects, saved_labels), IDENTITY,
            Inventory(current, current_projects or projects, live_labels),
            {str(project): [] for project in projects}, live_comments or {})

    def entities(self, actions):
        return {(action.kind, action.entity): action for action in actions}

    def test_task_content_labels_assignees_and_relations_go_back(self):
        saved = saved_task(5, title="Original", priority=3,
                           labels=[{"id": 9, "title": "topic"}], assignees=[{"id": 4}])
        current = saved_task(5, title="Rewritten", priority=5,
                             labels=[{"id": 9, "title": "topic"}, {"id": 8, "title": "ab:tier:2"}],
                             assignees=[], related_tasks={"blocked": [{"id": 6}]})
        actions, blockers, _ = self.plan([saved, saved_task(6)], [current, saved_task(6)])
        by_entity = self.entities(actions)
        self.assertEqual(by_entity[("restore", "task")].details["changes"],
                         {"title": "Original", "priority": 3})
        self.assertEqual((by_entity[("restore", "labels")].details["add"],
                          by_entity[("restore", "labels")].details["remove_titles"]),
                         ([], ["ab:tier:2"]))
        self.assertEqual(by_entity[("restore", "assignees")].details["assignees"], ["4"])
        self.assertEqual((by_entity[("restore", "relation")].details["kind"],
                          by_entity[("restore", "relation")].details["remove"]), ("blocked", True))
        self.assertEqual(blockers, {})

    def test_a_relation_is_restored_once_although_the_tracker_records_both_ends(self):
        """Diffing raw pairs would post the same edge twice and fail on the second call."""
        saved = [saved_task(5, related_tasks={"blocked": [{"id": 6}]}),
                 saved_task(6, related_tasks={"blocking": [{"id": 5}]})]
        actions, _, _ = self.plan(saved, [saved_task(5), saved_task(6)])
        relations = [action for action in actions if action.entity == "relation"]
        self.assertEqual([(action.details["base"], action.details["kind"], action.details["other"],
                           action.details["remove"]) for action in relations],
                         [("5", "blocked", "6", False)])

    def test_relations_of_tasks_the_snapshot_does_not_describe_are_never_touched(self):
        """Reading one side of the diff from the whole tracker deletes other projects' work.

        A snapshot covers one subtree, so every relation elsewhere would look
        like an edge this snapshot wants removed.
        """
        stranger = saved_task(99, project="9", related_tasks={"blocking": [{"id": 98}]})
        neighbour = saved_task(98, project="9", related_tasks={"blocked": [{"id": 99}]})
        actions, _, _ = self.plan([saved_task(5)], [saved_task(5), stranger, neighbour],
                                  projects=("1",), current_projects=("1", "9"))
        self.assertEqual([action for action in actions if action.entity == "relation"], [])

    def test_a_card_goes_back_to_the_column_it_sat_in(self):
        saved = saved_task(5, buckets=[{"id": 1, "project_view_id": 10, "title": "Backlog"}])
        current = saved_task(5, buckets=[{"id": 2, "project_view_id": 10, "title": "Ready"}])
        actions, _, _ = self.plan([saved], [current])
        placement, = [action.details for action in actions if action.entity == "placement"]
        self.assertEqual((placement["task"], placement["view"], placement["title"]),
                         ("5", "10", "Backlog"))

    def test_a_task_moved_away_comes_back_to_its_project_keeping_its_id(self):
        """Moving a task never changes its ID, so nothing that names the task breaks."""
        moved = saved_task(5, project="9", title="Rewritten")
        actions, blockers, _ = self.plan([saved_task(5)], [moved], projects=("1",),
                                         current_projects=("1", "9"))
        by_entity = self.entities(actions)
        self.assertEqual(by_entity[("restore", "move")].details["changes"], {"project_id": 1})
        self.assertEqual(by_entity[("restore", "task")].details["changes"], {"title": "Task 5"})
        self.assertEqual(blockers, {})

    def test_a_project_that_was_reparented_or_renamed_goes_back(self):
        """A moved subproject is the same bug as a moved task, one level up."""
        current = [{"id": 2, "parent_project_id": 9, "title": "Renamed", "created": BEFORE},
                   {"id": 1, "parent_project_id": 0, "title": "P1", "created": BEFORE}]
        actions, _, _ = self.plan([], [], projects=("1", "2"), current_projects=current)
        project, = [action for action in actions if action.entity == "project"]
        self.assertEqual((project.key, project.details["changes"]),
                         ("2", {"parent_project_id": 0, "title": "P2"}))

    def test_everything_created_after_the_snapshot_is_deleted(self):
        """The snapshot is the state; a restore that keeps later work is a merge."""
        boards = {"1": [{"view": {"id": 10, "view_kind": "kanban", "done_bucket_id": 3},
                         "buckets": [{"id": 1, "title": "Backlog", "position": 100},
                                     {"id": 3, "title": "Done", "position": 300}]}]}
        live_boards = {"1": [{"view": {"id": 10, "view_kind": "kanban", "done_bucket_id": 3},
                              "buckets": [{"id": 1, "title": "Backlog", "position": 100},
                                          {"id": 4, "title": "Ready", "position": 150},
                                          {"id": 3, "title": "Done", "position": 300}]}]}
        from agent_box.tracker import vikunja_api

        actions, blockers, outside = vikunja_api.plan_restore(
            snapshot([saved_task(5)], {}, boards, ("1",), ("topic",)), IDENTITY,
            Inventory([saved_task(5), saved_task(6, created=AFTER)], ("1",), ("topic", "ab:tier:2")),
            live_boards, {})
        deletions = {(action.entity, action.key) for action in actions if action.kind == "delete"}
        self.assertEqual(deletions, {("task", "6"), ("column", "1:10:Ready"), ("label", "2")})
        self.assertEqual((blockers, outside["labels_created"]), ({}, []))

    def test_a_label_outside_agent_boxs_namespace_is_reported_not_deleted(self):
        """A snapshot covers one subtree; labels are global and may serve another."""
        _, _, outside = self.plan([], [], saved_labels=("topic",), live_labels=("topic", "release"))
        self.assertEqual(outside["labels_created"], ["release"])

    def test_a_task_that_predates_the_snapshot_but_is_absent_from_it_is_a_blocker(self):
        """It was moved in from outside, and the snapshot cannot say where from."""
        actions, blockers, _ = self.plan([saved_task(5)],
                                         [saved_task(5), saved_task(7, created=BEFORE)])
        self.assertEqual(blockers["tasks_moved_in"], ["7"])
        self.assertEqual([action for action in actions if action.kind == "delete"], [])

    def test_what_was_deleted_since_cannot_be_restored_and_blocks_the_restore(self):
        """A recreated task gets a new ID and orphans the branch named after the old one."""
        actions, blockers, _ = self.plan([saved_task(5), saved_task(6)], [saved_task(5)],
                                         comments={"5": [{"id": 1, "comment": "kept"}]},
                                         live_comments={"5": []})
        self.assertEqual((blockers["tasks_deleted"], blockers["comments_deleted"]), (["6"], ["1"]))
        self.assertEqual([action for action in actions if action.key == "6"], [])

    def test_a_restore_that_cannot_be_exact_writes_nothing_unless_asked(self):
        from agent_box.tracker import vikunja_api

        with self.assertRaises(TrackerError) as caught:
            vikunja_api.check_restorable({"tasks_deleted": ["6"]})
        self.assertEqual(caught.exception.category, "conflict")
        self.assertIn("tasks deleted 6", str(caught.exception))
        self.assertIsNone(vikunja_api.check_restorable({"tasks_deleted": ["6"]}, allow_partial=True))
        self.assertIsNone(vikunja_api.check_restorable({}))

    def test_every_comment_added_after_the_snapshot_goes_and_an_edited_one_goes_back(self):
        """Nothing marks a comment as the tool's afterwards, so the snapshot decides."""
        live = {"5": [{"id": 1, "comment": "edited", "author": {"id": 1}},
                      {"id": 2, "comment": "new", "author": {"id": 1}},
                      {"id": 3, "comment": "someone else", "author": {"id": 7}}]}
        actions, blockers, _ = self.plan([saved_task(5)], [saved_task(5)],
                                         comments={"5": [{"id": 1, "comment": "original"}]},
                                         live_comments=live)
        self.assertEqual([(action.kind, action.key) for action in actions
                          if action.entity == "comment"],
                         [("restore", "1"), ("delete", "2"), ("delete", "3")])
        self.assertEqual(blockers, {})

    def test_a_snapshot_from_another_tracker_is_refused_before_anything_is_read(self):
        """Task IDs are small integers that collide across instances."""
        from agent_box.tracker import vikunja_api

        elsewhere = snapshot([saved_task(5)], server="https://other.example")
        with self.assertRaises(TrackerError) as caught:
            vikunja_api.check_snapshot_identity(elsewhere, IDENTITY)
        self.assertEqual(caught.exception.category, "conflict")
        incomplete = {key: value for key, value in snapshot().items() if key != "taken"}
        with self.assertRaisesRegex(TrackerError, "missing taken"):
            vikunja_api.check_snapshot_identity(incomplete, IDENTITY)

    def test_a_board_that_named_no_done_column_is_restored_to_naming_none(self):
        """board init sets a done column, so undoing it must be able to unset one."""
        from agent_box.tracker import vikunja_api

        boards = {"1": [{"view": {"id": 10, "view_kind": "kanban", "done_bucket_id": 0},
                         "buckets": [{"id": 104, "title": "Done", "position": 500}]}]}
        instance, client = adapter()
        live = {"1": [{"view": dict(client.boards[1]["views"][0], done_bucket_id=104),
                       "buckets": client.boards[1]["buckets"][10]}]}
        actions, _, _ = vikunja_api.plan_restore(snapshot([], {}, boards), IDENTITY, Inventory([]),
                                                 live, {})
        restoring = [action for action in actions if (action.kind, action.entity) == ("restore", "view")]
        self.assertEqual([action.details["title"] for action in restoring], [None])
        client.boards[1]["views"][0]["done_bucket_id"] = 104
        vikunja_api.apply_restore(client, restoring, [])
        self.assertEqual([body for path, body in client.writes if path == "/projects/1/views/10"],
                         [{"done_bucket_id": 0}])


class AuditTests(unittest.TestCase):
    def problem_codes(self, task_object, get=None):
        return [problem["code"] for problem in task_problems(task_object, get or (lambda _: None))]

    def test_a_task_the_workflow_cannot_run_reports_each_reason_separately(self):
        stray = Task("1", "1", "Fix the build", description="", state="Unknown")
        self.assertEqual(self.problem_codes(stray),
                         ["no-state", "no-tier", "no-description", "no-checklist"])

    def test_progress_and_ownership_recorded_before_the_work_started_are_reported(self):
        early = Task("1", "1", "Fix", description="- [x] done", state="Backlog",
                     assignees=["7"], labels=["ab:tier:2"])
        self.assertEqual(self.problem_codes(early), ["early-progress", "early-assignee"])

    def test_a_finished_task_with_unfinished_acceptance_is_reported(self):
        finished = Task("1", "1", "Fix", description="- [ ] acceptance", state="Done",
                        labels=["ab:tier:1"])
        self.assertEqual(self.problem_codes(finished), ["done-unfinished"])

    def test_conflicting_tier_labels_are_reported_rather_than_resolved(self):
        conflicted = Task("1", "1", "Fix", description="- [ ] a", state="Ready",
                          labels=["ab:tier:1", "ab:tier:3"])
        self.assertEqual(self.problem_codes(conflicted), ["bad-tier"])

    def test_an_unfinished_dependency_is_the_workflow_working_but_a_cycle_is_not(self):
        first = Task("1", "1", "First", description="- [ ] a", state="Ready",
                     labels=["ab:tier:1"], blockers=["2"])
        second = Task("2", "1", "Second", description="- [ ] a", state="Ready",
                      labels=["ab:tier:1"])
        _, get = tasks(first, second)
        self.assertEqual(self.problem_codes(first, get), [])
        second.blockers = ["1"]
        self.assertEqual(self.problem_codes(first, get), ["broken-dependency"])

    def test_an_unreadable_parent_is_one_broken_relation_and_is_counted_once(self):
        orphan = Task("1", "1", "Child", description="- [ ] a", state="Ready",
                      labels=["ab:tier:1"], parent="404")

        def get(task_id):
            raise KeyError(task_id)

        self.assertEqual(self.problem_codes(orphan, get), ["broken-parent"])

    def test_a_parent_in_another_project_is_reported(self):
        child = Task("1", "1", "Child", description="- [ ] a", state="Ready",
                     labels=["ab:tier:1"], parent="2")
        parent = Task("2", "9", "Parent", description="- [ ] a", state="Ready", labels=["ab:tier:1"])
        _, get = tasks(child, parent)
        self.assertIn("split-parent", self.problem_codes(child, get))

    def test_the_report_counts_problems_and_names_the_tasks_sharing_a_title(self):
        """A hundred-task project is read by its counts; the rows say which task."""
        first = Task("1", "1", "Fix", description="- [ ] a", state="Ready", labels=["ab:tier:1"])
        second = Task("2", "1", "  fix ", description="- [ ] a", state="Ready", labels=["ab:tier:1"])
        third = Task("3", "1", "Other", description="", state="Ready")
        items, get = tasks(first, second, third)
        report = audit(items, get, [{"project": "1", "problems": []}])
        self.assertEqual(report["counts"], {"duplicate-title": 2, "no-tier": 1,
                                            "no-description": 1, "no-checklist": 1})
        self.assertEqual([problem["message"] for problem in report["tasks"][0]["problems"]],
                         ["shares its title with 2"])
        self.assertFalse(report["clean"])

    def test_a_conforming_project_audits_clean_and_still_explains_itself(self):
        """The notes are returned in every report; the coordinator translates them."""
        items, get = tasks(Task("1", "1", "Fix", description="- [ ] a", state="Ready",
                                labels=["ab:tier:1"]))
        report = audit(items, get, [{"project": "1", "problems": []}])
        self.assertTrue(report["clean"])
        self.assertEqual((report["counts"], report["boards"]), ({}, []))
        self.assertTrue(report["notes"])

    def test_an_audit_without_a_binding_reports_boards_and_says_so(self):
        """Boards are repaired before the binding exists, tasks only after it does."""
        report = audit(None, lambda _: None, [{"project": "1", "problems": ["no column for Ready"]}])
        self.assertEqual((report["scope"], report["tasks"], report["clean"]), ("boards", [], False))
        self.assertTrue(report["notes"])
        self.assertEqual(report["boards"], [{"project": "1", "problems": ["no column for Ready"]}])

    def test_a_broken_board_keeps_the_project_from_auditing_clean(self):
        items, get = tasks(Task("1", "1", "Fix", description="- [ ] a", state="Ready",
                                labels=["ab:tier:1"]))
        report = audit(items, get, [{"project": "1", "problems": ["no column for Ready"]}])
        self.assertFalse(report["clean"])
        self.assertEqual(report["boards"], [{"project": "1", "problems": ["no column for Ready"]}])
