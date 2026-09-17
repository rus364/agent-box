"""Completed review responses survive applicability failures without paid retries."""
import json
import os
import shlex
from pathlib import Path
import unittest
from unittest.mock import patch

from agent_box import workflow, review_results, git_workflow as gw
from agent_box.errors import AgentBoxError
import workflow_fixtures as fixtures

BINDING = fixtures.BINDING


class ReviewResultTests(unittest.TestCase):
    setUp = fixtures.WorkflowCase.setUp
    run_workflow = fixtures.WorkflowCase.run_workflow

    native_round = fixtures.WorkflowCase.native_round
    native_result = fixtures.WorkflowCase.native_result

    def review(self):
        return self.run_workflow("review", "1", "--brief", str(self.review_brief))

    def response(self, verdict="PASS"):
        finding = "\n- [HIGH] file:1 — private finding\n" if verdict == "NEEDS_WORK" else ""
        return fixtures.answer(f"private-completed-marker\n\nVerdict: {verdict}\n{finding}")

    def retry(self, result):
        return self.run_workflow("review-result", "1", "--round=" + result["round"], "--reconcile")

    def native(self):
        return self.native_result(self.response()["text"])

    def test_related_edit_during_provider_preserves_response_and_one_attempt(self):
        """Informational links cannot discard a completed response or charge a second round."""
        def provider(*args, **kwargs):
            self.task.related.append("99")
            kwargs["diagnostics"].update(usage={"input_tokens": 17})
            return self.response()
        original = workflow.scope(self.root, "main")
        with patch.object(workflow, "run", side_effect=provider) as run:
            result = self.review()
        self.assertEqual(run.call_count, 1)
        self.assertEqual(workflow.scope(self.root, "main"), original)
        self.assertEqual(workflow.read_review_state(self.root, BINDING, self.task)["used"], 1)
        self.assertEqual(result["reconciliation"]["status"], "applied")
        raw = Path(result["result_path"]).read_text()
        self.assertIn("private-completed-marker", raw)
        self.assertEqual(json.loads(raw)["output"]["diagnostics"]["usage"]["input_tokens"], 17)

    def test_a_new_commit_is_terminal_even_when_the_ref_returns(self):
        """An observed stale round cannot become a free new review by restoring its old head."""
        original_head = gw.head_sha(self.root)
        def provider(*args, **kwargs):
            gw.git(self.root, "commit", "--allow-empty", "-m", "changed head")
            return self.response()
        with patch.object(workflow, "run", side_effect=provider):
            result = self.review()
        self.assertEqual(result["reconciliation"]["status"], "stale")
        self.assertEqual(result["reconciliation"]["changes"], ["head"])
        gw.git(self.root, "reset", "--hard", original_head)
        with patch.object(workflow, "run") as run, patch.object(workflow, "start_review") as reserve:
            repeated = self.retry(result)
        self.assertEqual(repeated["reconciliation"]["status"], "stale")
        run.assert_not_called()
        reserve.assert_not_called()
        self.assertNotIn("review", workflow.read_evidence(self.root, self.task))
        self.assertTrue(Path(result["result_path"]).exists())

    def test_card_edits_while_the_reviewer_runs_do_not_discard_its_verdict(self):
        """Acceptance belongs to code: a reworded description, a blocker or a new parent is not new work."""
        def provider(*args, **kwargs):
            self.task.description += "\nClarified requirement"
            self.task.blockers.append("99")
            self.task.labels = ["ab:tier:3"]
            return self.response()
        with patch.object(workflow, "run", side_effect=provider):
            result = self.review()
        self.assertEqual(result["reconciliation"]["status"], "applied")
        self.assertEqual(workflow.read_evidence(self.root, self.task)["review"]["verdict"], "PASS")

    def test_tracker_failure_and_restart_reconcile_without_provider_or_attempt(self):
        """Once the provider responded, tracker availability affects only reconciliation."""
        def provider(*args, **kwargs):
            self.adapter.get.side_effect = AgentBoxError("private tracker error")
            return self.response()
        with patch.object(workflow, "run", side_effect=provider):
            result = self.review()
        self.assertEqual(result["reconciliation"]["status"], "error")
        offline = self.run_workflow("review-result", "1")
        self.assertEqual(offline["result_path"], result["result_path"])
        self.adapter.get.side_effect = None
        with patch.object(workflow, "run") as run, patch.object(workflow, "start_review") as reserve:
            repeated = self.retry(offline)
            with patch.object(workflow, "save_evidence") as save:
                self.retry(repeated)
                save.assert_not_called()
        self.assertEqual(repeated["reconciliation"]["status"], "applied")
        self.assertEqual(repeated["review_budget"]["used"], 1)
        run.assert_not_called()
        reserve.assert_not_called()
        self.assertNotIn("private tracker error", json.dumps(result))
        self.assertNotIn("private-completed-marker", json.dumps(result))
        self.assertNotIn("private-completed-marker", json.dumps(workflow.read_evidence(self.root, self.task)))

    def test_evidence_write_failure_preserves_all_verdicts_and_recovers_once(self):
        """Failure to accept PASS, findings or uncertainty must not lose the actual response."""
        original = workflow.save_evidence
        def fail_acceptance(root, task, value):
            if "review" in value:
                raise OSError("synthetic disk failure")
            original(root, task, value)
        for verdict in ("PASS", "NEEDS_WORK", "INCONCLUSIVE"):
            with self.subTest(verdict=verdict):
                with patch.object(workflow, "run", return_value=self.response(verdict)), \
                     patch.object(workflow, "save_evidence", side_effect=fail_acceptance):
                    result = self.review()
                self.assertEqual(result["reconciliation"]["status"], "error")
                private = json.loads(Path(result["result_path"]).read_text())
                self.assertEqual(private["output"]["report"], self.response(verdict))
                with patch.object(workflow, "run") as run:
                    recovered = self.retry(result)
                run.assert_not_called()
                self.assertEqual(recovered["reconciliation"]["status"], "applied")
                self.assertEqual(workflow.read_evidence(self.root, self.task)["review"]["verdict"], verdict)

    def test_a_new_round_retires_an_unanswered_native_round(self):
        """Only the latest round can be answered, so an earlier unanswered input is removed, not stranded."""
        first = self.native_round()
        first_input = workflow.review_evidence_path(self.root, "1", first["round"])
        self.assertTrue(first_input.is_dir())
        second = self.native_round()
        self.assertFalse(first_input.exists())
        latest = self.native()
        self.assertEqual((latest["round"], latest["reconciliation"]["status"]), (second["round"], "applied"))
        self.assertEqual(latest["review_budget"]["used"], 1)
        with self.assertRaisesRegex(AgentBoxError, "No native review round is pending"):
            self.native()

    def test_missing_modified_or_replaced_receipts_never_apply(self):
        """A receipt seal detects content edits and same-byte replacements before acceptance."""
        for damage in ("missing", "modified", "replaced"):
            with self.subTest(damage=damage):
                self.native_round()
                self.adapter.get.side_effect = [self.task, AgentBoxError("offline")]
                result = self.native()
                self.adapter.get.side_effect = None
                path = Path(result["result_path"])
                raw = path.read_bytes()
                path.unlink()
                if damage != "missing":
                    path.write_bytes(raw + (b" " if damage == "modified" else b""))
                    path.chmod(0o400)
                with self.assertRaisesRegex(AgentBoxError, "missing|modified"):
                    self.retry(result)
                self.assertNotIn("review", workflow.read_evidence(self.root, self.task))

    def test_provider_callback_saves_before_runner_cleanup(self):
        """The real runner's completion hook publishes a receipt while its files still exist."""
        def provider(*args, **kwargs):
            report = self.response()
            kwargs["on_completed"](report)
            state = workflow.read_review_state(self.root, BINDING, self.task)
            record = state["results"][-1]
            value, _ = review_results.read(workflow.WORKFLOW_STATE, workflow.repository_key(self.root), record["receipt"])
            self.assertEqual(value["output"]["report"], report)
            self.assertTrue(Path(value["descriptor"]["input"]["path"]).exists())
            return report
        with patch.object(workflow, "run", side_effect=provider):
            self.assertEqual(self.review()["reconciliation"]["status"], "applied")

    def test_tampered_input_retains_response_without_accepting_or_deleting_replacement(self):
        """Canonical-input corruption is distinct from provider failure and scope drift."""
        target = Path(self.directory.name) / "user-file"
        target.write_text("user-owned")
        seen = []
        def provider(*args, read_paths, **kwargs):
            path = Path(read_paths[0]) / "handoff.txt"
            seen.append(path)
            path.unlink()
            path.symlink_to(target)
            return self.response()
        with patch.object(workflow, "run", side_effect=provider):
            result = self.review()
        path = seen[0]
        self.assertEqual(result["reconciliation"]["status"], "invalid_input")
        self.assertEqual(target.read_text(), "user-owned")
        self.assertTrue(path.is_symlink())
        self.assertTrue(Path(result["result_path"]).exists())
        self.assertNotIn("review", workflow.read_evidence(self.root, self.task))

    def test_retention_is_bounded_and_never_deletes_unowned_files(self):
        """Expiry and count limits prune only marked private files, never links or foreign files."""
        with patch.object(workflow, "run", return_value=self.response()):
            result = self.review()
        path = Path(result["result_path"])
        unowned = path.parent / ("f" * 64 + ".json")
        unowned.write_text("not ours")
        target = Path(self.directory.name) / "user-owned"
        target.write_text("keep")
        link = path.parent / ("e" * 64 + ".json")
        link.symlink_to(target)
        with patch.object(review_results, "MAX_FILES", 1), \
             patch.object(workflow, "run", return_value=self.response()):
            newer = self.review()
        self.assertFalse(path.exists())
        self.assertTrue(Path(newer["result_path"]).exists())
        self.assertEqual(unowned.read_text(), "not ours")
        self.assertTrue(link.is_symlink())
        self.assertEqual(target.read_text(), "keep")
        self.assertEqual(os.stat(newer["result_path"]).st_mode & 0o777, 0o400)

    def test_raw_stream_retention_keeps_twenty_recent_runs_inside_workflow_state(self):
        """A task that never reaches Done cannot grow stream storage without bound or write through links."""
        repository = workflow.repository_key(self.root)
        first = review_results.stream_run(workflow.WORKFLOW_STATE, repository, "1", "old")
        (first / "stdout").write_text("old")
        os.utime(first, (0, 0))
        second = review_results.stream_run(workflow.WORKFLOW_STATE, repository, "2", "round")
        self.assertFalse(first.exists())
        self.assertEqual((second.name, second.stat().st_mode & 0o777), ("1", 0o700))
        os.utime(second, (second.stat().st_mtime - 60,) * 2)  # Created runs can share a timestamp.
        runs = [review_results.stream_run(workflow.WORKFLOW_STATE, repository, "2", "round")
                for _ in range(review_results.MAX_FILES)]
        self.assertEqual([run.name for run in runs[-2:]], ["20", "21"])
        self.assertFalse(second.exists())
        self.assertEqual(len(list(second.parent.iterdir())), review_results.MAX_FILES)
        outside = Path(self.directory.name) / "outside"
        outside.mkdir()
        link = review_results.stream_path(workflow.WORKFLOW_STATE, repository, "3", "round").parent
        link.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(AgentBoxError, "symlink"):
            review_results.stream_run(workflow.WORKFLOW_STATE, repository, "3", "round")
        self.assertEqual(list(outside.iterdir()), [])

    def test_stream_retention_also_bounds_total_bytes(self):
        """Twenty large runs would otherwise keep hundreds of MiB; the receipts' byte ceiling applies too."""
        repository = workflow.repository_key(self.root)
        old = review_results.stream_run(workflow.WORKFLOW_STATE, repository, "1", "round")
        (old / "stdout").write_bytes(b"x" * 20)
        os.utime(old, (old.stat().st_mtime - 60,) * 2)
        newer = review_results.stream_run(workflow.WORKFLOW_STATE, repository, "1", "round")
        (newer / "stdout").write_bytes(b"x" * 20)
        with patch.object(review_results, "MAX_TOTAL_BYTES", 30):
            review_results.stream_run(workflow.WORKFLOW_STATE, repository, "1", "round")
        self.assertEqual((old.exists(), newer.exists()), (False, True))

    def test_done_never_deletes_through_a_linked_streams_directory_and_reports_it(self):
        """Removal on Done follows no link out of workflow state, and its failure is returned, not swallowed."""
        from agent_box.branch_cleanup import discard_review_streams
        repository = workflow.repository_key(self.root)
        outside = Path(self.directory.name) / "outside"
        victim = outside / review_results.streams_root(workflow.WORKFLOW_STATE, repository, "1").name
        victim.mkdir(parents=True)
        (victim / "keep").write_text("user data")
        review_results.directory(workflow.WORKFLOW_STATE, repository)
        (workflow.WORKFLOW_STATE / "review-results" / repository / "streams").symlink_to(outside, target_is_directory=True)
        result = discard_review_streams({"state": "Done"}, self.root, self.task)
        self.assertEqual((victim / "keep").read_text(), "user data")
        self.assertIn("symlink", result["review_streams_error"])
        self.assertEqual(result["state"], "Done")

    def test_information_changes_keep_validation_while_policy_labels_still_trigger_review(self):
        """Metadata is not a test input, but a review-trigger label still has its live consumer."""
        self.run_workflow("validate", "1", "--", "true")
        initial = workflow.scope(self.root, "main")
        self.task.related = ["88"]
        self.task.labels += ["ordinary", self.config.workflow.review.label]
        self.assertEqual(workflow.scope(self.root, "main"), initial)
        self.assertEqual(workflow.evidence(self.root, self.task, initial)["validation"]["exit"], 0)
        self.assertIn("task 1 has review label " + self.config.workflow.review.label,
                      workflow.review_reasons(self.config.workflow.review, [self.task], [], 0))

    def test_written_evidence_then_state_failure_finalizes_without_second_application(self):
        """A torn final state write leaves the receipt and can finish the first evidence write."""
        original = workflow.save_review_state
        def fail_final(root, binding, task, value):
            if value.get("results") and value["results"][-1]["status"] == "applied":
                raise OSError("synthetic final state failure")
            return original(root, binding, task, value)
        with patch.object(workflow, "run", return_value=self.response()), \
             patch.object(workflow, "save_review_state", side_effect=fail_final):
            result = self.review()
        self.assertEqual(result["reconciliation"]["status"], "error")
        self.assertTrue(Path(result["result_path"]).exists())
        saved = self.run_workflow("review-result", "1")
        with patch.object(workflow, "save_evidence") as write, patch.object(workflow, "run") as run:
            recovered = self.retry(saved)
        write.assert_not_called()
        run.assert_not_called()
        self.assertEqual(recovered["reconciliation"]["status"], "applied")
        self.assertEqual(recovered["review_budget"]["used"], 1)

    def test_expired_and_oversized_receipts_do_not_become_acceptance(self):
        """Retention limits are enforced before reuse; failed persistence preserves canonical input."""
        opened = self.native_round()
        with patch.object(review_results, "MAX_BYTES", 1):
            with self.assertRaisesRegex(AgentBoxError, "8 MiB"):
                self.native()
        self.assertTrue(workflow.review_evidence_path(self.root, "1", opened["round"]).is_dir())
        result = self.native()
        self.assertEqual(result["review_budget"]["used"], 1)
        with patch.object(review_results.time, "time", return_value=10**12):
            with self.assertRaisesRegex(AgentBoxError, "expired"):
                self.retry(result)

    def test_dirty_tree_blocks_only_application_and_can_reconcile_the_same_round(self):
        """A report file in the checkout does not change the committed review scope."""
        self.native_round()
        untracked = self.root / "REPORT.md"
        untracked.write_text(self.response()["text"])
        result = self.native()
        self.assertEqual(result["reconciliation"]["status"], "blocked")
        self.assertEqual(result["reconciliation"]["changes"], ["working_tree"])
        untracked.unlink()
        with patch.object(workflow, "run") as run, patch.object(workflow, "start_review") as reserve:
            recovered = self.retry(result)
        self.assertEqual(recovered["reconciliation"]["status"], "applied")
        self.assertEqual(recovered["review_budget"]["used"], 1)
        run.assert_not_called()
        reserve.assert_not_called()

    def test_unindexed_managed_receipt_recovers_after_index_write_or_read_failure(self):
        """Publishing a response and its index can fail separately without requiring redispatch."""
        for failure in ("write", "read"):
            with self.subTest(failure=failure):
                returned = False
                def provider(*args, **kwargs):
                    nonlocal returned
                    returned = True
                    return self.response()
                function = "save_review_state" if failure == "write" else "read_review_state"
                original = getattr(workflow, function)
                def unavailable(*args, **kwargs):
                    if returned:
                        raise OSError("temporary index failure")
                    return original(*args, **kwargs)
                with patch.object(workflow, function, side_effect=unavailable), \
                     patch.object(workflow, "run", side_effect=provider):
                    result = self.review()
                self.assertEqual(result["reconciliation"]["status"], "error")
                self.assertTrue(Path(result["result_path"]).exists())
                if failure == "read":
                    self.assertIsNone(result["review_budget"])
                state = workflow.read_review_state(self.root, BINDING, self.task)
                self.assertFalse(any(item["round"] == result["round"] for item in state.get("results", [])))
                discovered = self.run_workflow("review-result", "1")
                self.assertEqual(discovered["round"], result["round"])
                with patch.object(workflow, "run") as run, patch.object(workflow, "start_review") as reserve:
                    recovered = self.retry(discovered)
                self.assertEqual(recovered["reconciliation"]["status"], "applied")
                # The verdict the failed index write did not count is counted once on recovery.
                self.assertEqual(recovered["review_budget"]["used"], state["used"] + 1)
                run.assert_not_called()
                reserve.assert_not_called()

    def test_unindexed_receipt_replacement_is_rejected_by_its_private_seal(self):
        """Recovery must not adopt bytes replaced before the first budget index was saved."""
        returned = False
        original = workflow.save_review_state
        def provider(*args, **kwargs):
            nonlocal returned
            returned = True
            return self.response()
        def unavailable(*args, **kwargs):
            if returned:
                raise OSError("temporary index failure")
            return original(*args, **kwargs)
        with patch.object(workflow, "save_review_state", side_effect=unavailable), \
             patch.object(workflow, "run", side_effect=provider):
            result = self.review()
        path = Path(result["result_path"])
        raw = path.read_bytes()
        path.unlink()
        path.write_bytes(raw)
        path.chmod(0o400)
        with self.assertRaisesRegex(AgentBoxError, "seal.*changed"):
            self.retry(result)
        self.assertNotIn("review", workflow.read_evidence(self.root, self.task))

    def test_saved_native_response_is_reconciled_not_recorded_twice_after_cleanup_error(self):
        """A completed native answer whose cleanup failed is finished by reconcile, never by a second record."""
        self.native_round()
        with patch.object(workflow, "cleanup_delegation_input", side_effect=OSError("temporary failure")):
            result = self.native()
        self.assertEqual(result["reconciliation"]["status"], "error")
        with self.assertRaisesRegex(AgentBoxError, "No native review round is pending"):
            self.native()
        recovered = self.retry(result)
        self.assertEqual((recovered["reconciliation"]["status"], recovered["review_budget"]["used"]), ("applied", 1))

    def test_generated_recovery_commands_accept_rounds_starting_with_a_dash(self):
        """URL-safe random IDs can begin with '-' and must not be parsed as CLI options."""
        with patch.object(workflow.secrets, "token_urlsafe", return_value="-round"):
            self.native_round()
        untracked = self.root / "REPORT.md"
        untracked.write_text("temporary")
        result = self.native()
        args = workflow.parser().parse_args(shlex.split(result["next_step"])[2:])
        self.assertEqual(args.round, "-round")
        inspected = self.run_workflow("inspect", "1")
        read_args = workflow.parser().parse_args(shlex.split(inspected["review"]["result_command"])[2:])
        self.assertEqual(read_args.round, "-round")
        untracked.unlink()
        self.assertEqual(workflow.dispatch(args)["reconciliation"]["status"], "applied")

    def test_wrong_branch_or_detached_head_blocks_then_reuses_the_same_response(self):
        """Checkout selection is an application condition, not a change to the task's code."""
        for checkout in ("main", "--detach"):
            with self.subTest(checkout=checkout):
                self.native_round()
                gw.git(self.root, "switch", checkout)
                result = self.native()
                self.assertEqual(result["reconciliation"]["status"], "blocked")
                self.assertEqual(result["reconciliation"]["changes"], ["branch"])
                gw.git(self.root, "switch", "ab/1-task")
                with patch.object(workflow, "run") as run, patch.object(workflow, "start_review") as reserve:
                    self.assertEqual(self.retry(result)["reconciliation"]["status"], "applied")
                run.assert_not_called()
                reserve.assert_not_called()

    def test_wrong_checkout_does_not_hide_a_real_task_ref_change(self):
        """Returning to the task branch must still detect its moved commit before acceptance."""
        self.native_round()
        original = gw.head_sha(self.root)
        gw.git(self.root, "commit", "--allow-empty", "-m", "new task head")
        gw.git(self.root, "switch", "main")
        result = self.native()
        self.assertEqual(result["reconciliation"]["status"], "blocked")
        gw.git(self.root, "switch", "ab/1-task")
        result = self.retry(result)
        self.assertEqual(result["reconciliation"]["status"], "stale")
        self.assertIn("head", result["reconciliation"]["changes"])
        gw.git(self.root, "reset", "--hard", original)
        self.assertEqual(self.retry(result)["reconciliation"]["status"], "stale")

    def test_card_edits_after_pass_keep_acceptance_and_a_new_commit_retires_it(self):
        """Checking items, rewording or re-tiering a card after review is bookkeeping, not a new scope."""
        self.run_workflow("validate", "1", "--", "true")
        with patch.object(workflow, "run", return_value=self.response()):
            result = self.review()
        self.assertEqual(result["reconciliation"]["status"], "applied")
        self.task.description = "## Acceptance\n- [x] requirement\n\nMoved section"
        self.task.labels = ["ab:tier:3"]
        self.task.children = []
        current = workflow.scope(self.root, "main")
        self.assertEqual(current, result["scope"])
        saved = workflow.evidence(self.root, self.task, current)
        self.assertEqual((saved["validation"]["exit"], saved["review"]["verdict"]), (0, "PASS"))
        gw.git(self.root, "commit", "--allow-empty", "-m", "new work")
        saved = workflow.evidence(self.root, self.task, workflow.scope(self.root, "main"))
        self.assertFalse({"validation", "review", "human_acceptance"} & saved.keys())

    def test_unindexed_response_edits_are_detected_when_input_and_owner_checks_pass(self):
        """Input integrity cannot substitute for the missing index's response-byte identity."""
        self.native_round()
        with patch.object(workflow, "save_review_state", side_effect=OSError("index unavailable")):
            result = self.native()
        path = Path(result["result_path"])
        value = json.loads(path.read_text())
        value["output"]["report"]["text"] = "replaced response"
        path.unlink()
        path.write_text(json.dumps(value))
        path.chmod(0o400)
        # The simpler owner/mode/id and input checks all pass for this substituted response.
        self.assertEqual(review_results.read_file(path)[0]["output"]["report"]["text"], "replaced response")
        self.assertTrue(workflow.read_delegation_input(self.root, value["descriptor"]))
        with self.assertRaisesRegex(AgentBoxError, "seal.*changed"):
            self.retry(result)
        self.assertNotIn("review", workflow.read_evidence(self.root, self.task))
