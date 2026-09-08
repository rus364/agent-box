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


class AuditTests(unittest.TestCase):
    def problem_codes(self, task_object, get=None):
        return [problem["code"] for problem in task_problems(task_object, get or (lambda _: None))]

    def test_optional_tier_is_valid_in_every_lifecycle_state(self):
        """Auditing must not require fabricated routing labels at any stage."""
        for state in ("Backlog", "Ready", "Doing", "Review", "Done"):
            for tier in (None, 1, 2, 3):
                with self.subTest(state=state, tier=tier):
                    labels = ["product"] + ([f"ab:tier:{tier}"] if tier else [])
                    task = Task("1", "1", "Fix", state=state, labels=list(labels),
                                description="- [x] acceptance" if state == "Done" else "- [ ] acceptance",
                                assignees=["7"] if state in ("Doing", "Review", "Done") else [])
                    report = audit([task], lambda _: task)
                    self.assertEqual(report["counts"], {})
                    self.assertTrue(report["clean"])
                    self.assertEqual(task.labels, labels)

    def test_invalid_tier_remains_an_error_in_every_lifecycle_state(self):
        """Optional labels do not make malformed or conflicting explicit tiers valid."""
        for state in ("Backlog", "Ready", "Doing", "Review", "Done"):
            for labels in (["ab:tier:0"], ["ab:tier:four"], ["ab:tier:"],
                           ["ab:tier:1", "ab:tier:3"]):
                with self.subTest(state=state, labels=labels):
                    task = Task("1", "1", "Fix", state=state, labels=list(labels),
                                description="- [x] acceptance" if state == "Done" else "- [ ] acceptance",
                                assignees=["7"] if state in ("Doing", "Review", "Done") else [])
                    report = audit([task], lambda _: task)
                    self.assertFalse(report["clean"])
                    self.assertEqual([item["code"] for item in report["errors"]], ["bad-tier"])
                    self.assertEqual(task.labels, labels)

    def test_a_task_the_workflow_cannot_run_reports_each_reason_separately(self):
        stray = Task("1", "1", "Fix the build", description="", state="Unknown")
        self.assertEqual(self.problem_codes(stray),
                         ["no-state", "no-description", "no-checklist"])

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
        self.assertEqual(report["counts"], {"duplicate-title": 2,
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

    def test_backlog_gaps_do_not_force_adoption_to_invent_requirements(self):
        """An unresolved requirement belongs to interview, not a fabricated checklist."""
        items, get = tasks(Task("1", "1", "Clarify the feature", state="Backlog", assignees=["7"]))
        report = audit(items, get)
        self.assertTrue(report["clean"])
        self.assertTrue(report["counts"])
        self.assertEqual(report["errors"], [])

    def test_an_unreadable_child_is_a_structural_error_even_in_backlog(self):
        """An unreadable child cannot safely become part of the parent's workflow tree."""
        parent = Task("1", "1", "Parent", state="Backlog", children=["404"])
        _, get = tasks(parent)
        report = audit([parent], get)
        self.assertFalse(report['clean'])
        self.assertEqual([item['code'] for item in report['errors']], ['broken-child'])

    def test_a_child_in_the_same_project_remains_valid(self):
        parent = Task("1", "1", "Parent", state="Backlog", children=["2"])
        child = Task("2", "1", "Child", state="Backlog", parent="1")
        items, get = tasks(parent, child)
        self.assertTrue(audit(items, get)['clean'])

    def test_ready_work_without_description_still_blocks_cutover(self):
        items, get = tasks(Task("1", "1", "Unspecified", state="Ready"))
        report = audit(items, get)
        self.assertFalse(report["clean"])
        self.assertEqual({item["code"] for item in report["errors"]}, {"no-description"})

    def test_a_broken_board_keeps_the_project_from_auditing_clean(self):
        items, get = tasks(Task("1", "1", "Fix", description="- [ ] a", state="Ready",
                                labels=["ab:tier:1"]))
        report = audit(items, get, [{"project": "1", "problems": ["no column for Ready"]}])
        self.assertFalse(report["clean"])
        self.assertEqual(report["boards"], [{"project": "1", "problems": ["no column for Ready"]}])
