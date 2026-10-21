"""Behavior selection travels with assignments, never a shared profile's last project."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_box import config, config_proposal, delegation, github_review, project_config, runtime, workflow
from agent_box import workflow_behavior as behavior
from agent_box.errors import AgentBoxError
from workflow_fixtures import WorkflowCase, candidate


class BehaviorConfigTests(unittest.TestCase):
    def test_default_and_explicit_versions_survive_saved_and_rendered_settings(self):
        """An old omitted setting adopts v2; selecting v1 must survive a package refresh."""
        self.assertEqual(config.Config.from_dict({}).workflow.behavior_version, "v2")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for version in behavior.VERSIONS:
                settings = config.Config()
                settings.workflow.enabled = True
                settings.workflow.behavior_version = version
                config.save_config(settings, root / "saved.json")
                loaded = config.load_config(root / "saved.json")
                self.assertEqual(loaded.workflow.behavior_version, version)
                runtime.sync_tracker_runtime(loaded, root / "session", container=False)
                with patch.dict(os.environ, {"AGENT_BOX_RUNTIME_CONFIG": str(root / "session/config.json")}), \
                        patch.object(project_config, "load", return_value={"workflow": {"behavior_version": "v1"}}):
                    self.assertEqual(config.effective_config().workflow.behavior_version, "v1")
                self.assertEqual(json.loads((root / "session/config.json").read_text())["workflow"]["behavior_version"], version)

    def test_unknown_versions_are_errors_instead_of_silent_defaults(self):
        """A typo must not select a different engineering contract."""
        for value in ("v3", "", None, 2, True, [], {}):
            with self.subTest(value=value):
                settings = config.Config.from_dict({"workflow": {"behavior_version": value}})
                self.assertTrue(any("behavior_version" in x for x in settings.workflow.validate()))
                with self.assertRaises(AgentBoxError):
                    project_config.validate({"workflow": {"behavior_version": value}}, Path(".agent-box.json"))

    def test_published_settings_adopt_the_default_without_rewriting_the_original(self):
        """A direct upgrade must not require intermediate releases or alter saved opt-ins on read."""
        fixtures = Path(__file__).parent / "fixtures/compatibility"
        for path in (fixtures / "1.0.0/config.json", fixtures / "1.1.0/browser-config.json",
                     fixtures / "2.0.0/permission-config.json"):
            before = path.read_bytes()
            settings = config.load_config(path)
            self.assertEqual(settings.workflow.behavior_version, "v2")
            self.assertEqual(path.read_bytes(), before)

    def test_project_reset_inherits_and_global_proposal_reset_restores_v2(self):
        """Resetting an override must not accidentally pin the project's last effective version."""
        settings = config.Config()
        settings.workflow.behavior_version = "v1"
        override = {"workflow": {"behavior_version": "v2"}}
        self.assertEqual(project_config.apply(settings, override).workflow.behavior_version, "v2")
        reset = project_config.changed(override, resets=[("workflow", "behavior_version")])
        self.assertEqual(project_config.apply(settings, reset).workflow.behavior_version, "v1")
        proposed = config_proposal.applied(settings.to_dict(), {"set": {}, "reset": ["workflow.behavior_version"]})
        self.assertEqual(config.Config.from_dict(proposed).workflow.behavior_version, "v2")

    def test_behavior_discovery_needs_no_task_tracker_or_enabled_workflow(self):
        """Planning must load policy before a task exists, including an unbound checkout."""
        with patch.object(workflow, "effective_config", return_value=config.Config()), \
                patch.object(workflow, "make_adapter") as adapter, patch.object(workflow, "repository_root") as root:
            result = workflow.dispatch(workflow.parser().parse_args(["behavior"]))
        self.assertEqual(result["version"], "v2")
        self.assertIn("user's goal", result["instructions"])
        adapter.assert_not_called()
        root.assert_not_called()

    def test_shared_native_profiles_cannot_pin_another_projects_behavior(self):
        """Each assignment supplies policy; static profiles never choose v1 or v2."""
        for role in ("worker", "scout", "reviewer", "sync"):
            text = delegation.profile_instructions(role)
            self.assertNotIn(behavior.COMMON, text)
            self.assertNotIn("Workflow behavior: v", text)
        self.assertIn("workflow behavior --role worker", delegation.profile_instructions("worker"))


class BehaviorDeliveryTests(WorkflowCase):
    def test_each_worker_and_scout_assignment_uses_the_effective_version(self):
        """The managed CLI must receive the project choice, not the helper's default."""
        brief = self.root / "brief.txt"
        brief.write_text("Inspect the assigned implementation.")
        for role in ("worker", "scout"):
            self.config.workflow.role(role).models = [candidate("claude", "worker", "high")]
            for version in behavior.VERSIONS:
                self.config.workflow.behavior_version = version
                with patch.object(workflow, "execute", return_value={"report": "## Done\n", "route": {},
                        "failures": [], "diagnostics": {"usage": None}}) as execute:
                    self.run_workflow("delegate", "1", "--role", role, "--brief", str(brief))
                prompt = execute.call_args.args[4]
                self.assertIn(f"Workflow behavior: {version} ({role})", prompt)
                self.assertEqual(behavior.COMMON in prompt, version == "v2")

    def test_local_review_keeps_v1_safeguards_and_selects_v2_per_round(self):
        """v1 must keep existing review quality while v2 adds the shared analysis policy."""
        for version in behavior.VERSIONS:
            self.config.workflow.behavior_version = version
            opened = self.native_round()
            prompt = self.delegation_input(opened).read_text()
            self.assertIn('ask "then what?"', prompt)
            self.assertIn("simpler behavior-preserving alternative", prompt)
            self.assertIn(f"Workflow behavior: {version} (reviewer)", prompt)
            self.assertEqual(behavior.COMMON in prompt, version == "v2")
            self.native_result()

    def test_sync_and_github_requests_carry_the_selected_policy(self):
        """These routes construct separate prompts and must not silently stay on v1."""
        self.config.workflow.sync.models = [candidate("codex", "sync", "high")]
        self.adapter.subtree.return_value = ["1"]
        for version in behavior.VERSIONS:
            self.config.workflow.behavior_version = version
            with patch.object(workflow, "execute", return_value={"report": "Sync: UNCHANGED\n",
                    "route": {}, "diagnostics": {"usage": None}}) as execute:
                self.run_workflow("sync", "1")
            sync = execute.call_args.args[4]
            cloud = github_review.request_body("claude", "a" * 40, "b" * 40, {"task": "Requirements"},
                                                behavior_version=version)
            for role, text in (("sync", sync), ("reviewer", cloud)):
                self.assertIn(f"Workflow behavior: {version} ({role})", text)
                self.assertEqual(behavior.COMMON in text, version == "v2")

    def test_inspect_exposes_the_actual_version_without_changing_it(self):
        """The coordinator must see a project override before handing work to a native agent."""
        for version in behavior.VERSIONS:
            self.config.workflow.behavior_version = version
            self.assertEqual(self.run_workflow("inspect", "1")["configuration"]["behavior_version"], version)
