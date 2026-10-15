"""GitHub writes: both halves of the lifecycle, native relations, and the binding as the write boundary."""
import unittest

from agent_box.tracker.core import TrackerError, claim
from github_fixtures import REPO, issue, project, writable


def raw(client, number, repo=REPO):
    return client.issues[(repo, number)]


class LifecycleWriteTests(unittest.TestCase):
    def test_done_moves_the_card_and_closes_the_issue(self):
        tracker, client = writable([issue(1, status="In review")])
        task = tracker.set_state(tracker.get("1"), "Done")
        self.assertEqual((task.state, raw(client, 1)["state"], raw(client, 1)["status"]), ("Done", "CLOSED", "Done"))

    def test_failure_between_the_two_writes_is_finished_by_a_rerun(self):
        """The card moves first, so a lost close reads as Unknown instead of a Done nothing revisits."""
        tracker, client = writable([issue(1, status="In review")], fail=["CloseIssue"])
        with self.assertRaises(TrackerError):
            tracker.set_state(tracker.get("1"), "Done")
        stalled = tracker.get("1")
        self.assertEqual(stalled.state, "Unknown")
        self.assertEqual(tracker.set_state(stalled, "Done").state, "Done")
        self.assertEqual(raw(client, 1)["state"], "CLOSED")

    def test_rerun_corrects_a_completed_close_the_board_made_while_ours_was_lost(self):
        tracker, client = writable([issue(1, status="Ready")], auto_close=True, fail=["CloseIssue"])
        with self.assertRaises(TrackerError):
            tracker.set_state(tracker.get("1"), "Done", unimplemented=True)
        self.assertEqual(raw(client, 1)["stateReason"], "COMPLETED")
        task = tracker.set_state(tracker.get("1"), "Done", unimplemented=True)
        self.assertEqual((task.state_reason, raw(client, 1)["stateReason"]), ("closed as not planned", "NOT_PLANNED"))

    def test_not_planned_survives_the_board_closing_the_issue_first(self):
        """Auto-close may close the issue as completed between our writes; the reason given still wins."""
        tracker, client = writable([issue(1, status="Ready")], auto_close=True)
        task = tracker.set_state(tracker.get("1"), "Done", unimplemented=True)
        self.assertEqual((task.state_reason, raw(client, 1)["stateReason"]), ("closed as not planned", "NOT_PLANNED"))

    def test_closing_without_implementation_is_not_planned(self):
        tracker, client = writable([issue(1, status="Ready")])
        task = tracker.set_state(tracker.get("1"), "Done", unimplemented=True)
        self.assertEqual((task.state_reason, raw(client, 1)["stateReason"]), ("closed as not planned", "NOT_PLANNED"))

    def test_leaving_done_reopens_the_issue(self):
        """Reopening on GitHub leaves the card in Done; both halves have to move."""
        tracker, client = writable([issue(1, status="Done", state="CLOSED", reason="COMPLETED")])
        task = tracker.set_state(tracker.get("1"), "Ready")
        self.assertEqual((task.state, raw(client, 1)["state"]), ("Ready", "OPEN"))

    def test_a_status_write_that_shows_late_is_not_a_refusal(self):
        """Project writes and workflows land seconds later; one stale read must not fail the step."""
        tracker, _ = writable([issue(1, status="Ready")], lag=2)
        self.assertEqual(tracker.set_state(tracker.get("1"), "In review").state, "In review")

    def test_closing_is_confirmed_only_once_the_card_reaches_done(self):
        """A closed issue reads as Done from any column, so the card itself must be checked."""
        for lag, confirmed in ((2, True), (10, False)):
            tracker, client = writable([issue(1, status="Ready")], lag=lag)
            with self.subTest(lag=lag):
                if confirmed:
                    tracker.set_state(tracker.get("1"), "Done")
                    self.assertEqual(raw(client, 1)["status"], "Done")
                else:
                    with self.assertRaisesRegex(TrackerError, "not confirmed"):
                        tracker.set_state(tracker.get("1"), "Done")

    def test_a_late_workflow_overwriting_the_column_is_written_again(self):
        """Item closed may land after a quick close and reopen and put the card back in Done."""
        tracker, client = writable([issue(1, status="Done", state="CLOSED", reason="COMPLETED")])
        apply = client.op_SetField

        def overwritten_once(**values):
            result = apply(**values)
            if not getattr(client, "overwrote", False):
                client.overwrote = True
                client.pending.append([0, lambda: client.issues[("octo/app", 1)].update(status="Done")])
            return result
        client.op_SetField = overwritten_once
        task = tracker.set_state(tracker.get("1"), "Ready")
        self.assertEqual((task.state, client.issues[("octo/app", 1)]["status"]), ("Ready", "Ready"))

    def test_reopening_waits_for_the_close_workflow_it_may_still_trigger(self):
        """Item closed from a quick close can land after Ready was already confirmed."""
        tracker, client = writable([issue(1, status="Done", state="CLOSED", reason="COMPLETED")])
        reopen = client.op_ReopenIssue

        def late_item_closed(**values):
            result = reopen(**values)
            client.pending.append([1, lambda: client.issues[("octo/app", 1)].update(status="Done")])
            return result
        client.op_ReopenIssue = late_item_closed
        task = tracker.set_state(tracker.get("1"), "Ready")
        self.assertEqual((task.state, client.issues[("octo/app", 1)]["status"]), ("Ready", "Ready"))

    def test_a_write_that_never_shows_is_reported(self):
        tracker, _ = writable([issue(1, status="Ready")], lag=10)
        with self.assertRaisesRegex(TrackerError, "not confirmed"):
            tracker.set_state(tracker.get("1"), "In review")

    def test_claim_assigns_the_token_user_and_starts_work(self):
        tracker, client = writable([issue(1, status="Ready")])
        task, started = claim(tracker, "1")
        self.assertEqual((task.assignees, task.state, started), (["octocat"], "In progress", True))


class BoundaryTests(unittest.TestCase):
    def test_issues_off_the_board_or_in_other_repositories_are_never_written(self):
        tracker, client = writable([issue(1), issue(2, repo="other/lib", status="Ready"), issue(3, status="Ready")])
        for task_id in ("1", "other/lib#2"):
            with self.subTest(task=task_id), self.assertRaisesRegex(TrackerError, "outside the repository binding"):
                tracker.comment(task_id, "note")
        with self.assertRaisesRegex(TrackerError, "outside the repository binding"):
            tracker.relate("3", "blocked-by", "other/lib#2")
        self.assertEqual(client.writes, [])

    def test_board_that_cannot_express_the_state_refuses_before_writing(self):
        tracker, client = writable([issue(1, status="Ready")],
                                   projects={2: project(status=("Backlog", "Ready", "In progress", "Done"))})
        with self.assertRaisesRegex(TrackerError, "In review"):
            tracker.set_state(tracker.get("1"), "In review")
        self.assertEqual(client.writes, [])


class FieldWriteTests(unittest.TestCase):
    def test_update_refuses_a_task_changed_since_it_was_read(self):
        """A Status move changes only the card's timestamp; it must still count as a change."""
        tracker, client = writable([issue(1, status="Ready")])
        stale = tracker.get("1")
        raw(client, 1)["itemUpdatedAt"] = "2026-05-05T00:00:00Z"
        with self.assertRaisesRegex(TrackerError, "Task changed"):
            tracker.update(stale, {"description": "new"})
        self.assertEqual(client.writes, [])

    def test_text_priority_and_assignees_are_written_and_confirmed(self):
        tracker, client = writable([issue(1, status="Ready", priority="Low")])
        task = tracker.update(tracker.get("1"), {"title": "T", "description": "- [x] done", "priority": 3,
                                                 "assignees": ["octocat"]})
        self.assertEqual((task.title, task.description, task.priority, task.assignees),
                         ("T", "- [x] done", 3, ["octocat"]))
        self.assertEqual(tracker.update(task, {"priority": 0}).priority, 0)

    def test_priority_without_its_option_is_refused_before_writing(self):
        cases = [(project(priority=("P0", "P1", "P2")), 3, "no single Priority option High"),
                 (project(priority=("High", "high")), 3, "no single Priority option High"),
                 (project(priority=None), 0, "no single-select Priority field")]
        for board, priority, message in cases:
            tracker, client = writable([issue(1, status="Ready")], projects={2: board})
            with self.subTest(priority=priority, message=message), self.assertRaisesRegex(TrackerError, message):
                tracker.update(tracker.get("1"), {"title": "T", "priority": priority})
            self.assertEqual(client.writes, [])

    def test_create_puts_a_new_issue_on_the_board_in_backlog_under_its_parent(self):
        """Item added and Auto-add sub-issues may be off; the adapter does not rely on them."""
        tracker, client = writable([issue(1, status="In progress")])
        task = tracker.create("octo/2", "Child", "body", parent="1", priority=2)
        self.assertEqual((task.id, task.project, task.state, task.priority, task.parent),
                         ("2", "octo/2", "Backlog", 2, "1"))
        self.assertEqual(tracker.get("1").children, ["2"])

    def test_create_with_an_unwritable_parent_creates_nothing(self):
        tracker, client = writable([issue(1, repo="other/lib", status="Ready")])
        with self.assertRaises(TrackerError):
            tracker.create("octo/2", "Child", parent="other/lib#1")
        self.assertEqual(client.writes, [])

    def test_tier_and_normalized_labels_replace_only_their_own(self):
        tracker, client = writable([issue(1, status="Ready", labels=["bug", "ab:tier:1"])], labels=["bug", "ab:tier:1"])
        self.assertEqual(sorted(tracker.set_tier("1", 3).labels), ["ab:tier:3", "bug"])
        self.assertIn("ab:normalized", tracker.set_normalized("1").labels)
        self.assertEqual(sorted(tracker.set_normalized("1", False).labels), ["ab:tier:3", "bug"])


class RelationWriteTests(unittest.TestCase):
    def setUp(self):
        self.tracker, self.client = writable([issue(n, status="Ready") for n in range(1, 5)])

    def test_every_kind_is_added_and_removed_natively(self):
        cases = [("parent", lambda: self.tracker.get("1").parent == "2"),
                 ("child", lambda: "2" in self.tracker.get("1").children),
                 ("blocks", lambda: "1" in self.tracker.get("2").blockers),
                 ("blocked-by", lambda: "2" in self.tracker.get("1").blockers),
                 ("related", lambda: "1" in self.tracker.get("2").related)]
        for kind, present in cases:
            with self.subTest(kind=kind):
                self.tracker.relate("1", kind, "2")
                self.assertTrue(present())
                self.tracker.relate("1", kind, "2", remove=True)
                self.assertFalse(present())

    def test_cycles_and_second_parents_are_refused(self):
        self.tracker.relate("1", "blocked-by", "2")
        with self.assertRaisesRegex(TrackerError, "cycle"):
            self.tracker.relate("2", "blocked-by", "1")
        self.tracker.relate("3", "parent", "1")
        with self.assertRaisesRegex(TrackerError, "different parent"):
            self.tracker.relate("3", "parent", "2")

    def test_marking_a_duplicate_closes_it_and_unmarking_keeps_it_closed(self):
        """GitHub records a duplicate only as a closing reason, and unmarking does not reopen."""
        task = self.tracker.relate("1", "duplicate", "2")
        self.assertEqual((task.state, task.state_reason), ("Done", "closed as a duplicate of 2"))
        task = self.tracker.relate("1", "duplicate", "2", remove=True)
        self.assertEqual((task.state, raw(self.client, 1)["duplicateOf"]), ("Done", None))
        with self.assertRaisesRegex(TrackerError, "only an open issue"):
            self.tracker.relate("1", "duplicate", "3")

    def test_marking_again_after_reopening_closes_the_issue_again(self):
        """Reopening drops GitHub's duplicate mark, so a second mark is a fresh close."""
        self.tracker.relate("1", "duplicate", "2")
        self.tracker.set_state(self.tracker.get("1"), "Ready")
        task = self.tracker.relate("1", "duplicate", "2")
        self.assertEqual((task.state, raw(self.client, 1)["state"], raw(self.client, 1)["status"]),
                         ("Done", "CLOSED", "Done"))

    def test_duplicate_on_a_broken_board_writes_nothing_and_a_rerun_finishes_the_card(self):
        board = self.client.projects[2]["fields"]["nodes"][1]
        done = board["options"].pop()
        with self.assertRaisesRegex(TrackerError, "Done"):
            self.tracker.relate("1", "duplicate", "2")
        self.assertEqual(self.client.writes, [])
        board["options"].append(done)
        self.client.op_CloseIssue(issueId=f"N-{REPO}-1", stateReason="DUPLICATE", duplicateIssueId=f"N-{REPO}-2")
        self.tracker._projects.clear()
        self.tracker.relate("1", "duplicate", "2")
        self.assertEqual(raw(self.client, 1)["status"], "Done")


if __name__ == "__main__":
    unittest.main()
