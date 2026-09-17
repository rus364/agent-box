"""Economical review: after an applied round, new commits are judged by their delta from its head."""
import unittest

from agent_box import git_workflow as gw
from agent_box import workflow
from agent_box.config import Config
from workflow_fixtures import BINDING, WorkflowCase


class DeltaReviewTests(WorkflowCase):

    def setUp(self):
        super().setUp()
        self.config.workflow.review.incremental = "LOW"
        self.commit("change.py", 300)

    def commit(self, path, lines, message="work"):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        previous = target.read_text() if target.exists() else ""
        target.write_text(previous + "".join(f"line {len(previous)} {n}\n" for n in range(lines)))
        gw.git(self.root, "add", path)
        gw.git(self.root, "commit", "-m", message)

    def reviewed(self, text):
        self.native_round()
        result = self.native_result(text)
        self.assertEqual(result["reconciliation"]["status"], "applied")
        return result["round"]

    def budget_used(self):
        return workflow.read_review_budget(self.root, BINDING, self.task, 3)["used"]

    def test_a_two_line_fix_of_low_findings_is_accepted_by_the_delta_threshold(self):
        """Task 408's case: fixing a LOW finding must not cost a paid round because the whole diff is large."""
        round_id = self.reviewed("Verdict: NEEDS_WORK\n\n- [LOW] change.py:1 — typo\n")
        self.commit("change.py", 2, "fix typo")
        result = self.run_workflow("review", "1")
        self.assertIn(f"delta threshold of round {round_id}", result["outcome"])
        self.assertEqual((result["delta"]["files"], result["delta"]["lines"], result["delta"]["reasons"]), (1, 2, []))
        self.assertEqual(self.budget_used(), 1)
        self.assertNotIn("review", workflow.evidence(self.root, self.task, workflow.scope(self.root, "main")))
        inspected = self.run_workflow("inspect", "1")["review"]
        self.assertFalse(inspected["required"])
        self.assertTrue(inspected["reasons"], "the whole diff still exceeds the thresholds")
        self.assertEqual(inspected["delta"]["round"], round_id)
        self.assertIsNone(inspected["independent_verdict"], "the outcome is not a PASS")
        self.assertEqual(workflow.review_blockers(self.config.workflow.review, [self.task], ["change.py"], 302,
                                                  {"delta_review": inspected["delta"]}), [])

    def test_finish_and_local_landing_accept_the_delta_and_show_its_round(self):
        """The outcome gates completion like a PASS would, and landing reports which round it rests on."""
        self.config.workflow.merge_mode, self.config.workflow.auto_push = "local", False
        self.task.description = "## Acceptance\n- [x] requirement"
        round_id = self.reviewed("Verdict: NEEDS_WORK\n\n- [LOW] change.py:1 — typo\n")
        self.commit("change.py", 2, "fix typo")
        self.run_workflow("validate", "1", "--", "true")
        self.assertEqual(self.run_workflow("finish", "1")["state"], "Review")
        self.task.state = "Review"
        inspected = self.run_workflow("land", "1", "inspect")
        self.assertEqual((inspected["blockers"], inspected["review"]["delta"]["round"]), ([], round_id))
        self.assertIsNone(inspected["review"]["verdict"])
        self.assertEqual(self.run_workflow("land", "1", "merge")["state"], "Done")

    def test_a_finding_above_the_level_requires_a_round_whatever_the_delta(self):
        """The setting bounds what may stay unreviewed; a HIGH finding's fix is exactly what review must see."""
        self.reviewed("Verdict: NEEDS_WORK\n\n- [HIGH] change.py:1 — unbounded\n- [LOW] change.py:2 — typo\n")
        self.commit("change.py", 2, "fix")
        self.assertIsNone(self.run_workflow("inspect", "1")["review"]["delta"])
        self.assertIn("round", self.run_workflow("review", "1", "--native"))
        self.config.workflow.review.incremental = "CRITICAL"
        self.native_result("Verdict: NEEDS_WORK\n\n- [CRITICAL] change.py:1 — still unbounded\n")
        self.commit("change.py", 2, "fix again")
        self.assertFalse(self.run_workflow("inspect", "1")["review"]["required"], "CRITICAL sets no restriction")

    def test_a_delta_that_touches_a_sensitive_path_or_exceeds_a_threshold_requires_a_round(self):
        """The delta passes the same triggers as a whole diff, so small steps cannot hide risky ones."""
        self.reviewed("Verdict: PASS\n\nReviewed.\n")
        self.commit("auth/token.py", 1, "sensitive")
        inspected = self.run_workflow("inspect", "1")["review"]
        self.assertTrue(inspected["required"])
        self.assertIn("sensitive path auth/token.py", " ".join(inspected["delta"]["reasons"]))
        blockers = workflow.review_blockers(self.config.workflow.review, [self.task], ["change.py"], 301,
                                            {"delta_review": inspected["delta"]})
        self.assertIn("still triggers review", blockers[0])
        self.assertIn("round", self.run_workflow("review", "1", "--native"))

    def test_the_delta_adds_up_from_the_reviewed_head_not_from_the_last_accepted_one(self):
        """Accepting one small step must not reset the baseline for the next."""
        self.config.workflow.review.max_lines = 10
        self.reviewed("Verdict: PASS\n\nReviewed.\n")
        self.commit("change.py", 6, "first step")
        self.assertFalse(self.run_workflow("inspect", "1")["review"]["required"])
        self.commit("change.py", 6, "second step")
        inspected = self.run_workflow("inspect", "1")["review"]
        self.assertEqual((inspected["required"], inspected["delta"]["lines"]), (True, 12))

    def test_off_and_a_task_without_its_own_rounds_judge_the_whole_diff(self):
        """Off keeps the previous behaviour, and a parent's first review is of its whole diff."""
        self.assertIsNone(self.run_workflow("inspect", "1")["review"]["delta"])
        self.assertIn("round", self.run_workflow("review", "1", "--native"))
        self.native_result()
        self.commit("change.py", 2, "small")
        self.config.workflow.review.incremental = "off"
        inspected = self.run_workflow("inspect", "1")["review"]
        self.assertEqual((inspected["required"], inspected["delta"]), (True, None))
        self.assertIn("round", self.run_workflow("review", "1", "--native"))

    def test_a_round_that_was_not_applied_or_inconclusive_gives_no_baseline(self):
        """A stale verdict describes other code, and INCONCLUSIVE established nothing."""
        self.native_round()
        self.commit("change.py", 1, "during review")
        self.assertEqual(self.native_result()["reconciliation"]["status"], "stale")
        self.commit("change.py", 1, "after")
        self.assertIsNone(self.run_workflow("inspect", "1")["review"]["delta"])
        self.reviewed("Verdict: INCONCLUSIVE\n\nCould not run it.\n")
        self.commit("change.py", 1, "after inconclusive")
        self.assertIsNone(self.run_workflow("inspect", "1")["review"]["delta"])

    def test_a_result_of_another_round_or_task_gives_no_baseline(self):
        """The index names a receipt; the receipt must describe this task's own round, as review-result also checks."""
        self.reviewed("Verdict: PASS\n\nReviewed.\n")
        self.commit("change.py", 2, "small")
        state = workflow.read_review_state(self.root, BINDING, self.task)
        self.assertIsNotNone(self.run_workflow("inspect", "1")["review"]["delta"])
        state["results"][-1]["round"] = "another-round"
        workflow.save_review_state(self.root, BINDING, self.task, state)
        self.assertIsNone(self.run_workflow("inspect", "1")["review"]["delta"])

    def test_the_setting_is_validated_and_kept_across_save(self):
        """Severity names are exact, so a lowercase level cannot silently turn the mode off."""
        config = Config.from_dict({"workflow": {"review": {"incremental": "MEDIUM"}}})
        self.assertEqual((config.workflow.review.incremental, config.workflow.validate()), ("MEDIUM", []))
        self.assertEqual(Config().workflow.review.incremental, "off")
        invalid = Config.from_dict({"workflow": {"review": {"incremental": "low"}}})
        self.assertTrue(any("incremental" in error for error in invalid.workflow.validate()))


if __name__ == "__main__":
    unittest.main()
