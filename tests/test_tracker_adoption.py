"""Adoption reports native task facts without inventing requirements or meaning."""
import unittest

from agent_box.tracker.audit import audit, task_problems
from agent_box.tracker.core import Task, TrackerError
from tracker_fixtures import adapter, board, task


def tasks(*items):
    index = {item.id: item for item in items}
    return list(items), (lambda task_id: index[task_id])


class DescriptionRewriteTests(unittest.TestCase):
    def test_a_description_may_be_rewritten_whole_even_after_an_import(self):
        """The import links rows to tasks through its checkpoint, so the body stays the user's text."""
        existing = task(5, 1, 10, 101, description="Goal\n\nVikunja external ID: B-01")
        instance, client = adapter(tasks={5: existing})
        instance.update(instance.get("5"), {"description": "Goal\n\n- [ ] Acceptance"})
        self.assertEqual([body["description"] for _, body in client.writes if body and "description" in body],
                         ["Goal\n\n- [ ] Acceptance"])

    def test_a_bulk_rewrite_is_planned_like_any_other_description_change(self):
        from agent_box.tracker import vikunja_api

        tasks = [{"id": 5, "title": "Task", "description": "Goal\n\nVikunja external ID: B-01"}]
        actions = vikunja_api.plan_bulk_updates([{"task": "5", "changes": {"description": "Goal"}}], tasks)
        self.assertEqual(actions[0].details["changes"], {"description": "Goal"})


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
            instance.set_state(instance.get("5"), "In progress")


class AuditTests(unittest.TestCase):
    def problem_codes(self, task_object, get=None):
        return [problem["code"] for problem in task_problems(task_object, get or (lambda _: None))]

    def test_optional_tier_is_valid_in_every_lifecycle_state(self):
        """Auditing must not require fabricated routing labels at any stage."""
        for state in ("Backlog", "Ready", "In progress", "In review", "Done"):
            for tier in (None, 1, 2, 3):
                with self.subTest(state=state, tier=tier):
                    labels = ["product"] + ([f"ab:tier:{tier}"] if tier else [])
                    task = Task("1", "1", "Fix", state=state, labels=list(labels),
                                description="- [x] acceptance" if state == "Done" else "- [ ] acceptance",
                                assignees=["7"] if state in ("In progress", "In review", "Done") else [])
                    report = audit([task], lambda _: task)
                    self.assertEqual(report["counts"], {})
                    self.assertTrue(report["clean"])
                    self.assertEqual(task.labels, labels)

    def test_invalid_tier_remains_an_error_in_every_lifecycle_state(self):
        """Optional labels do not make malformed or conflicting explicit tiers valid."""
        for state in ("Backlog", "Ready", "In progress", "In review", "Done"):
            for labels in (["ab:tier:0"], ["ab:tier:four"], ["ab:tier:"],
                           ["ab:tier:1", "ab:tier:3"]):
                with self.subTest(state=state, labels=labels):
                    task = Task("1", "1", "Fix", state=state, labels=list(labels),
                                description="- [x] acceptance" if state == "Done" else "- [ ] acceptance",
                                assignees=["7"] if state in ("In progress", "In review", "Done") else [])
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
