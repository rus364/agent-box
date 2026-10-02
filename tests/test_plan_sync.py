"""Plan sync changes tracker text through a separate role without rewriting code."""
import json
import tomllib
from unittest.mock import patch

from agent_box import agent_profiles, config_menu, plan_sync, provider_runner, workflow
from agent_box.config import Config
from agent_box.errors import AgentBoxError
from agent_box.provider_runner import ProviderFailure
from agent_box.project_config import PROJECT_FIELDS, apply
from workflow_fixtures import BINDING, WorkflowCase, candidate


class PlanSyncTests(WorkflowCase):
    def setUp(self):
        super().setUp()
        self.config.workflow.sync.models = [candidate("codex", "gpt-sync", "high")]
        self.adapter.subtree.return_value = ["1"]
        self.binding = BINDING

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
        with patch.object(workflow, "finish", return_value={"task": "1", "state": "Review"}):
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
