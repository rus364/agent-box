"""Plan sync changes tracker text through a separate role without rewriting code."""
import contextlib
import io
import json
import os
import tomllib
from unittest.mock import patch

from agent_box import agent_profiles, config_menu, plan_sync, provider_runner, workflow
from agent_box.config import Config
from agent_box.errors import AgentBoxError
from agent_box.provider_runner import ProviderFailure
from agent_box.project_config import PROJECT_FIELDS, apply
from agent_box.repo_binding import block_text
from workflow_fixtures import BINDING, WorkflowCase, candidate


class PlanSyncTests(WorkflowCase):
    def setUp(self):
        super().setUp()
        self.config.workflow.sync.models = [candidate("codex", "gpt-sync", "high")]
        self.adapter.subtree.return_value = ["1"]
        self.binding = BINDING

    def test_managed_executor_cannot_start_or_record_sync_before_any_context_access(self):
        """Wrong-role calls must name the mistake before tracker access or a read-only lock failure."""
        for flags in ([], ["--native"], ["--result", "missing"], ["--reconcile", "missing"]):
            with self.subTest(flags=flags), patch.dict(os.environ, {"AGENT_BOX_SYNC_EXECUTOR": "1"}), \
                    patch.object(workflow, "effective_config", side_effect=AssertionError("context accessed")):
                with self.assertRaisesRegex(AgentBoxError, "sync executor.*tracker"):
                    self.run_workflow("sync", "1", *flags)
        self.adapter.get.assert_not_called()
        self.assertIsNone(plan_sync.last_result(self.root, self.task))

    def test_executor_marker_does_not_block_direct_tracker_edits(self):
        """The recursion diagnostic must leave the executor's actual description/comment work usable."""
        from agent_box.tracker import cli
        self.task.revision = '"current"'
        self.report.write_text("Verified description")
        with patch.dict(os.environ, {"AGENT_BOX_SYNC_EXECUTOR": "1"}), \
                patch.object(cli, "context", return_value=(self.adapter, self.binding, self.root)):
            cli.dispatch(cli.parser().parse_args(["update", "1", "--revision", self.task.revision,
                                                 "--description-file", str(self.report)]))
            cli.dispatch(cli.parser().parse_args(["comment", "1", "--file", str(self.report)]))
        self.adapter.update.assert_called_once_with(self.task, {"description": "Verified description"})
        self.adapter.comment.assert_called_once_with("1", "Verified description")

    def test_cli_rejects_nested_sync_even_with_broken_project_configuration(self):
        """The public entry point must diagnose the wrong role before reading a broken binding."""
        from agent_box import cli, project_config
        binding = self.root / ".agent-box.json"
        binding.write_text("{")
        for prefix in ([], ["--"]):
            for flags in ([], ["--native"], ["--result", "missing"], ["--reconcile", "missing"]):
                stderr = io.StringIO()
                with self.subTest(prefix=prefix, flags=flags), \
                        patch.dict(os.environ, {"AGENT_BOX_SYNC_EXECUTOR": "1"}), \
                        patch("sys.argv", ["agent-box", *prefix, "workflow", "sync", "1", *flags]), \
                        patch.object(project_config, "project_path", return_value=binding), \
                        patch.object(project_config, "load", wraps=project_config.load) as load, \
                        contextlib.redirect_stderr(stderr):
                    with self.assertRaises(SystemExit):
                        cli.main()
                    self.assertRegex(stderr.getvalue(), "sync executor.*tracker")
                    load.assert_not_called()
        self.adapter.get.assert_not_called()

    def test_native_and_managed_technical_reports_preserve_partial_writes_for_recovery(self):
        """An executor can report a failed readback without disguising it as a decision conflict or retrying."""
        report = "Task 2 saved; task 3 write outcome unknown after timeout.\nSync: ERROR\n"
        for native in (True, False):
            with self.subTest(native=native), patch.object(workflow, "candidates", return_value={}), \
                    patch.object(workflow, "execute", return_value={"report": report, "route": {"model": "sync"},
                                                                   "diagnostics": {"usage": None}}) as execute:
                if native:
                    self.run_workflow("sync", "1", "--native")
                    self.report.write_text(report)
                    result = self.run_workflow("sync", "1", "--result", str(self.report))
                else:
                    result = self.run_workflow("sync", "1")
                self.assertEqual(execute.call_count, 0 if native else 1)
                self.assertEqual(result["status"], "ERROR")
                self.assertEqual(result["report"], report)
                self.assertEqual(plan_sync.last_result(self.root, self.task), result)
                self.assertIn("--reconcile FILE", self.run_workflow("inspect", "1")["next_step"])
                self.report.write_text("Readback still unavailable; do not repeat writes.\nSync: ERROR\n")
                unresolved = self.run_workflow("sync", "1", "--reconcile", str(self.report))
                self.assertEqual(unresolved["status"], "ERROR")
                self.assertEqual(unresolved["report"], report)
                self.report.write_text("Both tasks reread; writes confirmed, scope complete.\nSync: UPDATED\n")
                self.assertEqual(self.run_workflow("sync", "1", "--reconcile", str(self.report))["status"], "UPDATED")

    def test_disabled_auto_event_never_launches_but_manual_uses_independent_models(self):
        """Turning review off must not disable an explicitly requested tracker sync."""
        self.config.workflow.review.policy = "off"
        with patch.object(workflow, "execute") as execute:
            self.assertEqual(self.run_workflow("sync", "1", "--event", "after")["status"], "SKIPPED")
            execute.assert_not_called()
        with patch.object(workflow, "candidates", return_value={"provider": "codex"}) as choose, \
                patch.object(workflow, "execute", return_value={
                    "report": "No drift.\nSync: UNCHANGED\n", "route": {"model": "gpt-sync"},
                    "diagnostics": {"usage": None}}) as execute:
            result = self.run_workflow("sync", "1")
        self.assertEqual(result["status"], "UNCHANGED")
        self.assertEqual(choose.call_args.args[1], "sync")
        self.assertNotIn(self.task.description, execute.call_args.args[4])
        self.assertIn("Source: 1", execute.call_args.args[4])

    def test_native_result_records_partial_writes_as_blocked_without_touching_lifecycle(self):
        """A native report is an outcome, never code acceptance or task completion."""
        opened = self.run_workflow("sync", "1", "--native")
        self.assertEqual(opened["route"]["native"], {"codex": "ab-sync"})
        self.assertLess(len(opened["message"].split()), 320)
        self.assertNotIn(self.task.description, opened["message"])
        self.report.write_text("Updated task 2; decision needed for 3.\nSync: BLOCKED\n")
        result = self.run_workflow("sync", "1", "--result", str(self.report))
        self.assertEqual(result["status"], "BLOCKED")
        self.adapter.set_state.assert_not_called()
        self.adapter.update.assert_not_called()
        self.assertNotIn("review", workflow.read_evidence(self.root, self.task))

    def test_no_model_never_turns_sync_into_coordinator_work(self):
        self.config.workflow.sync.models = []
        with self.assertRaisesRegex(AgentBoxError, "separate agent"):
            self.run_workflow("sync", "1")
        self.assertEqual(workflow.execution(self.config, "sync", None, "1")["mode"], "unconfigured")

    def test_false_blocker_reconciliation_preserves_writes_and_original_evidence(self):
        """Historical Done text must not force another paid run after verified corrections."""
        self.config.workflow.sync.policy = "always"
        self.config.workflow.sync.events = ["after"]
        opened = self.run_workflow("sync", "1", "--native", "--event", "after")
        original = "Updated task 2; historical Done task 3 differs.\nSync: BLOCKED\n"
        self.report.write_text(original)
        blocked = self.run_workflow("sync", "1", "--result", str(self.report))
        self.assertIn("--reconcile FILE", self.run_workflow("inspect", "1")["next_step"])
        report = "Reread task 2: corrections saved; task 3 is superseded history.\nSync: UPDATED\n"
        self.report.write_text(report)
        self.config.workflow.sync.models = []
        with patch.object(workflow, "execute") as execute:
            result = self.run_workflow("sync", "1", "--reconcile", str(self.report))
            execute.assert_not_called()
        self.assertEqual(result["status"], "UPDATED")
        self.assertEqual(result["report"], original)
        self.assertEqual(result["reconciliation"], {"previous_status": "BLOCKED", "report": report})
        self.assertEqual(result["head"], opened["head"])
        self.assertEqual(result["route"], blocked["route"])
        self.assertEqual(plan_sync.schedule(self.root, self.binding, self.adapter, self.config,
                                            self.task, "after"), result)
        self.assertNotIn("next_step", plan_sync.describe(self.root, self.task, self.config))
        self.adapter.update.assert_not_called()
        self.adapter.set_state.assert_not_called()

    def test_pending_sync_cannot_be_overwritten_without_verified_recovery(self):
        """A manual retry must not silently discard possible tracker writes from either backend."""
        for status in ("BLOCKED", "ERROR", "NATIVE", "RUNNING"):
            prior = {"status": status, "head": workflow.gw.head_sha(self.root), "event": "plan"}
            plan_sync.save(self.root, self.task, prior)
            for flags in ([], ["--native"]):
                with self.subTest(status=status, flags=flags), patch.object(workflow, "execute") as execute:
                    with self.assertRaisesRegex(AgentBoxError, "--reconcile"):
                        self.run_workflow("sync", "1", *flags)
                    self.assertEqual(plan_sync.last_result(self.root, self.task), prior)
                    execute.assert_not_called()

    def test_reconciliation_can_require_a_fresh_run_after_interrupted_executor_stops(self):
        """Recovery may verify partial writes without claiming synchronization is complete."""
        prior = {"status": "ERROR", "head": workflow.gw.head_sha(self.root), "event": "plan",
                 "reason": "response lost"}
        plan_sync.save(self.root, self.task, prior)
        self.report.write_text("Executor stopped; reread saved edits, remaining scope needs sync.\nSync: REQUIRED\n")
        result = self.run_workflow("sync", "1", "--reconcile", str(self.report))
        self.assertEqual(result["status"], "REQUIRED")
        self.assertEqual(result["reason"], prior["reason"])
        self.assertIn("--event plan", self.run_workflow("inspect", "1")["next_step"])
        self.assertEqual(self.run_workflow("sync", "1", "--native")["status"], "NATIVE")

    def test_reconciliation_never_credits_old_work_to_a_new_head(self):
        """A moved checkout can reconcile old writes but must still schedule new decisions."""
        prior = {"status": "BLOCKED", "head": workflow.gw.head_sha(self.root), "event": "after"}
        plan_sync.save(self.root, self.task, prior)
        workflow.gw.git(self.root, "commit", "--allow-empty", "-m", "new source")
        self.report.write_text("Prior run verified; only blocker was historical Done text.\nSync: UNCHANGED\n")
        result = self.run_workflow("sync", "1", "--reconcile", str(self.report))
        self.assertEqual(result["head"], prior["head"])
        self.config.workflow.sync.policy = "always"
        self.config.workflow.sync.events = ["after"]
        scheduled = plan_sync.schedule(self.root, self.binding, self.adapter, self.config, self.task, "after")
        self.assertEqual(scheduled["status"], "REQUIRED")
        self.assertNotEqual(scheduled["head"], prior["head"])

    def test_invalid_or_unnecessary_reconciliation_preserves_saved_result(self):
        """A bare outcome is not evidence, and successful results need no administrative override."""
        prior = {"status": "BLOCKED", "head": workflow.gw.head_sha(self.root), "event": "after"}
        for report in ("", "Sync: UPDATED\n", "Sync: UNCHANGED\nextra", "why\nSync: UPDATED\nSync: REQUIRED"):
            plan_sync.save(self.root, self.task, prior)
            self.report.write_text(report)
            with self.subTest(report=report), self.assertRaises(AgentBoxError):
                self.run_workflow("sync", "1", "--reconcile", str(self.report))
            self.assertEqual(plan_sync.last_result(self.root, self.task), prior)
        self.report.write_text("Verified all tasks.\nSync: UNCHANGED\n")
        for status in ("UPDATED", "UNCHANGED", "REQUIRED"):
            plan_sync.save(self.root, self.task, {**prior, "status": status})
            with self.subTest(status=status), self.assertRaisesRegex(AgentBoxError, "No pending"):
                self.run_workflow("sync", "1", "--reconcile", str(self.report))

    def test_events_and_paths_decide_frequency_independently_of_review(self):
        settings = self.config.workflow.sync
        settings.policy, settings.events = "always", ["before", "after"]
        self.assertTrue(plan_sync.decision(self.root, self.task, settings, "before")[0])
        self.assertFalse(plan_sync.decision(self.root, self.task, settings, "plan")[0])
        base = workflow.gw.head_sha(self.root)
        (self.root / "logic.py").write_text("pass\n")
        workflow.gw.git(self.root, "add", "logic.py")
        workflow.gw.git(self.root, "commit", "-m", "code")
        workflow.save_evidence(self.root, self.task, {"base_head": base, "head": workflow.gw.head_sha(self.root)})
        self.assertTrue(plan_sync.decision(self.root, self.task, settings, "after")[0])
        settings.policy = "auto"
        self.assertFalse(plan_sync.decision(self.root, self.task, settings, "after")[0])
        settings.paths = ["*.py"]
        self.assertTrue(plan_sync.decision(self.root, self.task, settings, "after")[0])

    def test_enabling_default_sync_includes_accepted_decisions_without_a_run_at_every_begin(self):
        """Interview decisions can stale sibling tasks before any documentation commit is merged."""
        settings = Config().workflow.sync
        self.assertFalse(plan_sync.decision(self.root, self.task, settings, "plan")[0])
        settings.policy = "auto"
        self.assertTrue(plan_sync.decision(self.root, self.task, settings, "plan")[0])
        self.assertTrue(plan_sync.decision(self.root, self.task, settings, "after")[0])
        self.assertFalse(plan_sync.decision(self.root, self.task, settings, "before")[0])

    def test_root_document_changes_trigger_auto_sync(self):
        """Root decisions such as STRATEGY.md must not fall through a subdirectory glob."""
        settings = self.config.workflow.sync
        settings.policy = "auto"
        base = workflow.gw.head_sha(self.root)
        (self.root / "STRATEGY.md").write_text("Accepted decision\n")
        workflow.gw.git(self.root, "add", "STRATEGY.md")
        workflow.gw.git(self.root, "commit", "-m", "decision")
        workflow.save_evidence(self.root, self.task, {"base_head": base, "head": workflow.gw.head_sha(self.root)})
        self.assertTrue(plan_sync.decision(self.root, self.task, settings, "after")[0])

    def source_instructions(self, before, after, extra=None):
        path = self.root / "AGENTS.md"
        for title, content in (("source base", before), ("source update", after)):
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(content)
            if title == "source update" and extra:
                for name, body in extra.items():
                    target = self.root / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(body)
            workflow.gw.git(self.root, "add", "-A")
            workflow.gw.git(self.root, "commit", "--allow-empty", "-m", title)
            if title == "source base":
                base = workflow.gw.head_sha(self.root)
        workflow.save_evidence(self.root, self.task, {"base_head": base, "head": workflow.gw.head_sha(self.root)})
        self.config.workflow.sync.policy = "auto"

    def test_managed_workflow_refresh_never_launches_auto_after_sync(self):
        """Refreshing shipped instructions has no project decision to propagate or model work to pay for."""
        outside = "\n# Решения\r\nUse project authority.\r\n".encode()
        self.source_instructions(block_text("Old workflow\n", "workflow").encode() + outside,
                                 block_text("New workflow\n", "workflow").encode() + outside)
        with patch.object(workflow, "execute", return_value={"report": "Sync: UNCHANGED\n",
                                                           "route": {}, "diagnostics": {"usage": None}}) as execute:
            for flags in ((), ("--native",)):
                result = self.run_workflow("sync", "1", "--event", "after", *flags)
                self.assertEqual(result["status"], "SKIPPED")
            execute.assert_not_called()
        self.assertEqual(plan_sync.schedule(self.root, self.binding, self.adapter, self.config,
                                            self.task, "after")["status"], "SKIPPED")
        settings = self.config.workflow.sync
        self.assertTrue(plan_sync.decision(self.root, self.task, settings, "manual")[0])
        settings.events = ["before", "plan", "after"]
        for event in ("before", "plan"):
            self.assertTrue(plan_sync.decision(self.root, self.task, settings, event)[0])
        settings.policy = "always"
        self.assertTrue(plan_sync.decision(self.root, self.task, settings, "after")[0])

    def test_project_text_around_managed_block_still_triggers_sync(self):
        """The exception must not hide project decisions or whitespace outside the owned block."""
        old = block_text("Old workflow\n", "workflow").encode()
        new = block_text("New workflow\n", "workflow").encode()
        for before, after in ((old + b"Decision A\n", new + b"Decision B\n"),
                              (b"\n" + old + b"Decision\n", new + b"Decision\n"),
                              (old + b"Decision\n", new + b"Decision\n\n"),
                              (old + b"Decision\r\n", new + b"Decision\n")):
            with self.subTest(before=before, after=after):
                self.source_instructions(before, after)
                self.assertTrue(plan_sync.decision(self.root, self.task, self.config.workflow.sync, "after")[0])

    def test_managed_refresh_does_not_hide_another_matching_document(self):
        """A mixed commit still propagates actual documentation decisions."""
        self.source_instructions(block_text("Old\n", "workflow").encode(),
                                 block_text("New\n", "workflow").encode(),
                                 {"docs/decision.md": "Accepted decision\n"})
        self.assertTrue(plan_sync.decision(self.root, self.task, self.config.workflow.sync, "after")[0])

    def test_unverified_instruction_changes_keep_sync_required(self):
        """Missing or damaged ownership evidence cannot justify silently ignoring a changed file."""
        old = block_text("Old\n", "workflow").encode()
        new = block_text("New\n", "workflow").encode()
        for before, after in ((None, new), (old, None),
                              (b"Project decision A\n", b"Project decision B\n"),
                              (old.replace(b"Old", b"Edited"), new),
                              (old, new.replace(b"New", b"Edited")),
                              (old + old, new), (old, new + new),
                              (old, new.replace(b"<!-- END AGENT-BOX workflow -->", b"")),
                              (old + b"\xff", new + b"\xff")):
            with self.subTest(before=before, after=after):
                self.source_instructions(before, after)
                self.assertTrue(plan_sync.decision(self.root, self.task, self.config.workflow.sync, "after")[0])

    def test_unreadable_committed_instructions_do_not_silently_skip_sync(self):
        """A failed blob read leaves the matched path eligible; absence of proof is not no drift."""
        self.source_instructions(block_text("Old\n", "workflow").encode(),
                                 block_text("New\n", "workflow").encode())
        run = plan_sync.subprocess.run

        def unreadable(args, **kwargs):
            if args[:3] == ["git", "cat-file", "blob"]:
                raise OSError("blob read unavailable")
            return run(args, **kwargs)

        with patch.object(plan_sync.subprocess, "run", side_effect=unreadable):
            self.assertTrue(plan_sync.decision(self.root, self.task, self.config.workflow.sync, "after")[0])

    def test_only_completed_after_event_is_reused_at_the_same_head(self):
        """Planning may change task text without a commit; interrupted writes need reconciliation."""
        self.config.workflow.sync.policy = "always"
        self.config.workflow.sync.events = ["before", "after", "plan"]
        for event in ("before", "after", "plan"):
            plan_sync.save(self.root, self.task, {"event": event, "head": workflow.gw.head_sha(self.root),
                                                 "status": "UNCHANGED"})
            scheduled = plan_sync.schedule(self.root, self.binding, self.adapter, self.config, self.task, event)
            self.assertEqual(scheduled["status"], "UNCHANGED" if event == "after" else "REQUIRED")
        plan_sync.save(self.root, self.task, {"event": "plan", "status": "ERROR", "reason": "response lost"})
        scheduled = plan_sync.schedule(self.root, self.binding, self.adapter, self.config, self.task, "after")
        self.assertEqual(scheduled["status"], "ERROR")
        self.assertEqual(plan_sync.describe(self.root, self.task, self.config)["last_result"], scheduled)
        self.assertIn("Reconcile", self.run_workflow("inspect", "1")["next_step"])

    def test_provider_failure_after_dispatch_does_not_repeat_possible_tracker_writes(self):
        selected = {**candidate("codex", "one", "high"), "fallbacks": [candidate("claude", "two", "high")]}
        with patch.object(workflow, "run", side_effect=ProviderFailure("quota after edit", "quota")) as run:
            with self.assertRaisesRegex(AgentBoxError, "may have written"):
                workflow.execute(self.config.workflow, selected, "sync", self.root, "brief")
        self.assertEqual(run.call_count, 1)
        with patch.object(workflow, "run", side_effect=[ProviderFailure("missing CLI", "unavailable", stage="launch"),
                                                       "Sync: UNCHANGED"]) as run:
            result = workflow.execute(self.config.workflow, selected, "sync", self.root, "brief")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(result["route"]["model"], "two")

    def test_project_scope_and_launch_pin_reach_the_live_role_without_a_native_substitute(self):
        self.config = apply(self.config, {"workflow": {"sync": {"scope": "parent", "references": False}}})
        # dispatch's config mock holds the original object, so exercise the shared helper directly.
        opened = plan_sync.run(self.root, self.binding, self.adapter, self.config, self.task, native=True)
        self.assertIn("Scope: parent tree 1", opened["message"])
        self.assertIn("References: false", opened["message"])
        self.config.workflow.pins["sync"] = [candidate("claude", "pinned", "high")]
        route = workflow.execution(self.config, "sync", None, "1")
        self.assertEqual(route["models"], "--sync")
        self.assertEqual(route["native"], {})

    def test_report_with_text_after_outcome_is_not_recorded_as_success(self):
        with self.assertRaisesRegex(AgentBoxError, "no single outcome"):
            plan_sync.complete(self.root, self.task, {}, "Sync: UNCHANGED\nActually could not read tasks")
        self.assertIsNone(plan_sync.last_result(self.root, self.task))

    def test_markdown_emphasis_does_not_require_repeating_completed_tracker_writes(self):
        """Claude wrapped its successful outcome in bold; that changes presentation, not the result."""
        report = "Task 5: corrected SHA256 and reference; reread saved text.\n\n**Sync: UPDATED**\n"
        result = plan_sync.complete(self.root, self.task, {}, report)
        self.assertEqual(result["status"], "UPDATED")
        self.assertEqual(result["report"], report)
        with self.assertRaisesRegex(AgentBoxError, "single outcome"):
            plan_sync.complete(self.root, self.task, {}, "Sync: UNCHANGED\n**Sync: UPDATED**")

    def test_external_source_is_refused_before_dispatch(self):
        self.task.project = "other"
        with patch.object(workflow, "execute") as execute:
            with self.assertRaisesRegex(AgentBoxError, "outside"):
                self.run_workflow("sync", "1", "--native")
            execute.assert_not_called()

    def test_failed_run_is_visible_and_never_reinterpreted_as_no_drift(self):
        with patch.object(workflow, "candidates", return_value={}), \
                patch.object(workflow, "execute", side_effect=AgentBoxError("lost response")):
            with self.assertRaisesRegex(AgentBoxError, "writes may have completed"):
                self.run_workflow("sync", "1")
        self.assertEqual(plan_sync.last_result(self.root, self.task)["status"], "ERROR")

    def test_stale_native_result_cannot_close_a_changed_source(self):
        self.run_workflow("sync", "1", "--native")
        workflow.gw.git(self.root, "commit", "--allow-empty", "-m", "new source")
        self.report.write_text("Sync: UNCHANGED\n")
        with self.assertRaisesRegex(AgentBoxError, "head changed"):
            self.run_workflow("sync", "1", "--result", str(self.report))

    def test_begin_and_integrated_finish_request_configured_event_without_launching_twice(self):
        self.config.workflow.sync.policy = "always"
        self.config.workflow.sync.events = ["before", "after"]
        with patch.object(workflow, "begin", return_value={"task": "1"}), \
                patch.object(workflow, "execute") as execute:
            begun = self.run_workflow("begin", "1")
            self.assertEqual(begun["plan_sync"]["event"], "before")
            execute.assert_not_called()
        self.task.state = "Done"
        with patch.object(workflow, "finish", return_value={"task": "1", "state": "Done"}):
            finished = self.run_workflow("finish", "1")
        self.assertEqual(finished["plan_sync"]["event"], "after")

    def test_parent_after_event_waits_for_merge_and_is_visible_after_interruption(self):
        """Finishing a parent for review is not integration; landing must leave a recoverable request."""
        self.config.workflow.sync.policy = "always"
        with patch.object(workflow, "finish", return_value={"task": "1", "state": "In review"}):
            self.assertNotIn("plan_sync", self.run_workflow("finish", "1"))
        self.task.state = "Done"
        with patch.object(workflow, "landing", return_value={"task": "1", "state": "Done"}):
            result = self.run_workflow("land", "1", "merge")
        self.assertEqual(result["plan_sync"]["status"], "REQUIRED")
        self.assertIn("workflow sync 1 --event after", self.run_workflow("inspect", "1")["next_step"])

    def test_profile_and_runner_keep_tracker_access_separate_from_file_editing(self):
        self.assertLess(len(plan_sync.RULES.split()), 300)
        spec = candidate("codex", "gpt-sync", "high")
        profile = tomllib.loads(agent_profiles.body("codex", "sync", None, spec))
        self.assertNotIn("sandbox_mode", profile)
        self.assertEqual(profile["permissions"]["agent_box_sync"]["extends"], ":read-only")
        self.assertTrue(profile["permissions"]["agent_box_sync"]["network"]["enabled"])
        command = provider_runner.build_command(spec, "sync", self.root, self.report)
        self.assertNotIn("workspace-write", command)
        self.assertIn("permissions.agent_box_sync.network.enabled=true", command)
        claude = provider_runner.build_command(candidate("claude", "haiku", "default"), "sync", self.root, self.report)
        tools = claude[claude.index("--tools") + 1]
        self.assertIn("Bash", tools)
        self.assertNotIn("Edit", tools)
        self.assertNotIn("Write", tools)
        self.assertIn("Bash(agent-box tracker *)", claude[claude.index("--allowedTools") + 1])

    def test_settings_round_trip_menu_and_tiers_share_the_existing_model_mechanism(self):
        settings = self.config.workflow.sync
        settings.policy, settings.scope = "always", "parent"
        settings.events = ["before", "after", "plan"]
        settings.tiers["1"] = [candidate("claude", "tier-sync", "high")]
        parsed = Config.from_dict(json.loads(json.dumps(self.config.to_dict())))
        self.assertEqual(parsed.workflow.sync.scope, "parent")
        path, models = parsed.workflow.models_for("sync", 1)
        self.assertEqual(path, "workflow.sync.tiers.1")
        self.assertEqual(models[0]["model"], "tier-sync")
        self.assertIn("sync", PROJECT_FIELDS["workflow"])
        self.assertIn("sync", config_menu.menu_keys(parsed.workflow.to_dict(), ("workflow",)))
        self.assertEqual(parsed.workflow.validate(), [])

    def test_invalid_sync_settings_fail_before_any_dispatch(self):
        for key, value in (("policy", "sometimes"), ("scope", "all-servers"),
                           ("events", ["merge-whatever"]), ("paths", [1])):
            with self.subTest(key=key):
                config = Config.from_dict({"workflow": {"sync": {key: value}}})
                self.assertTrue(any(f"workflow.sync.{key}" in error for error in config.workflow.validate()))
