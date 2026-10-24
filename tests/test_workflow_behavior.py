"""Behavior selection travels with assignments, never a shared profile's last project."""
import json
import contextlib
import io
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_box import config, config_proposal, delegation, github_review, project_config, runtime, workflow
from agent_box import git_workflow as gw
from agent_box import workflow_behavior as behavior
from agent_box.errors import AgentBoxError
from workflow_fixtures import WorkflowCase, candidate
from test_text_delivery import delivered_bytes


class BehaviorConfigTests(unittest.TestCase):
    def test_default_and_explicit_versions_survive_saved_and_rendered_settings(self):
        """The default is v2; an explicit supported behavior selection must survive saving."""
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
            result = workflow.dispatch(workflow.parser().parse_args(["behavior", "--role", "coordinator"]))
        self.assertEqual(result["version"], "v2")
        self.assertEqual(result["role"], "coordinator")
        self.assertIn("user's goal", result["instructions"])
        adapter.assert_not_called()
        root.assert_not_called()

    def test_cli_never_infers_a_role_and_lists_valid_roles_on_missing_or_invalid_input(self):
        """A generic repository instruction must not turn a delegate into a coordinator."""
        for argv in (["behavior"], ["behavior", "--role", "unknown"]):
            with contextlib.redirect_stderr(io.StringIO()) as stderr, \
                    patch.object(workflow, "effective_config") as load, self.assertRaises(SystemExit) as error:
                workflow.main(argv)
            self.assertEqual(error.exception.code, 2)
            for role in behavior.ROLES:
                self.assertIn(role, stderr.getvalue())
            load.assert_not_called()
        for version in behavior.VERSIONS:
            settings = config.Config()
            settings.workflow.behavior_version = version
            for role in behavior.ROLES:
                with patch.object(workflow, "effective_config", return_value=settings), \
                        contextlib.redirect_stdout(io.StringIO()) as stdout:
                    self.assertEqual(workflow.main(["behavior", "--role", role]), 0)
                result = json.loads(stdout.getvalue())
                self.assertEqual((result["role"], result["version"]), (role, version))
                self.assertEqual(result["instructions"], behavior.instructions(version, role))

    def test_public_entrypoint_refuses_a_missing_role_without_workflow_opt_in(self):
        """The top-level CLI must preserve the explicit-role error before tracker work."""
        with tempfile.TemporaryDirectory() as directory:
            for suffix in ([], ["--role", "unknown"]):
                result = subprocess.run([sys.executable, "-m", "agent_box", "workflow", "behavior", *suffix],
                                        cwd=directory, capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 2)
                for role in behavior.ROLES:
                    self.assertIn(role, result.stderr)

    def test_shared_native_profiles_cannot_pin_another_projects_behavior(self):
        """Each assignment supplies policy; static profiles never choose v1 or v2."""
        for role in ("worker", "scout", "reviewer", "sync"):
            text = delegation.profile_instructions(role)
            self.assertNotIn(behavior.COMMON, text)
            self.assertNotIn("Workflow behavior: v", text)
        self.assertIn("workflow behavior --role worker", delegation.profile_instructions("worker"))


class BehaviorDeliveryTests(WorkflowCase):
    @contextlib.contextmanager
    def project_override(self, version):
        """Load a real rendered global setting and an opposite project choice through effective_config."""
        self.config.workflow.behavior_version = "v2" if version == "v1" else "v1"
        session = Path(self.directory.name) / "session"
        runtime.sync_tracker_runtime(self.config, session, container=False)
        path = self.root / ".agent-box.json"
        project_config.save(path, {"workflow": {"behavior_version": version}})
        gw.git(self.root, "add", ".agent-box.json")
        gw.git(self.root, "commit", "-m", f"fixture project behavior {version}")
        with patch.dict(os.environ, {"AGENT_BOX_RUNTIME_CONFIG": str(session / "config.json")}), \
                patch.object(project_config, "repository_root", return_value=self.root), \
                patch.object(workflow, "effective_config", side_effect=config.effective_config):
            yield

    def command(self, *args):
        with contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(io.StringIO()):
            code = workflow.main(list(args))
        self.assertEqual(code, 0, stdout.getvalue())
        return stdout.getvalue()

    def test_native_and_managed_workers_and_scouts_get_identical_project_selected_assignments(self):
        """The coordinator copies only a generated locator, never reconstructs requirements or policy."""
        for role in ("worker", "scout"):
            self.config.workflow.role(role).models = [candidate("codex", "model", "high")]
        original = self.task.description = "Authoritative requirement: preserve 日本語 and the final line"
        self.review_brief.write_text("Only inspect the assigned path; do not change requirements.")
        for version in behavior.VERSIONS:
            with self.project_override(version):
                for role in ("worker", "scout"):
                    native = json.loads(self.command("delegate", "1", "--native", "--role", role,
                                                     "--brief", str(self.review_brief)))
                    self.assertNotIn(original, native["message"])
                    prepared = delivered_bytes(native["input"]).decode()
                    self.assertIn(original, prepared)
                    self.assertIn(f"Workflow behavior: {version} ({role})", prepared)
                    self.assertEqual(behavior.COMMON in prepared, version == "v2")
                    self.assertNotIn(f"Workflow behavior: {version} (coordinator)", prepared)
                    with patch.object(workflow, "execute", return_value={"report": "Done", "route": {},
                            "failures": [], "diagnostics": {"usage": None}}) as execute:
                        self.command("delegate", "1", "--role", role, "--brief", str(self.review_brief))
                    self.assertEqual(execute.call_args.args[4], prepared)

    def test_native_and_managed_review_and_sync_cli_use_project_overrides(self):
        """Routes with their own builders must still use the effective project selection."""
        self.config.workflow.review.policy = "always"
        self.config.workflow.review.incremental = "off"
        self.config.workflow.review.max_rounds = 4
        self.config.workflow.sync.models = [candidate("codex", "sync", "high")]
        self.adapter.subtree.return_value = ["1"]
        for version in behavior.VERSIONS:
            with self.project_override(version):
                native = json.loads(self.command("review", "1", "--native"))
                text = delivered_bytes(native["input"]).decode()
                self.assertEqual(text, self.delegation_input(native).read_text())
                self.assertIn(f"Workflow behavior: {version} (reviewer)", text)
                self.assertNotIn(f"Workflow behavior: {version} (coordinator)", text)
                self.native_result()
                from workflow_fixtures import answer
                with patch.object(workflow, "run", return_value=answer()) as run:
                    self.command("review", "1")
                self.assertIn(f"Workflow behavior: {version} (reviewer)", run.call_args.args[3])
                opened = json.loads(self.command("sync", "1", "--native"))
                prepared = delivered_bytes(opened["input"]).decode()
                self.assertIn(f"Workflow behavior: {version} (sync)", prepared)
                self.assertNotIn(f"Workflow behavior: {version} (coordinator)", prepared)
                report = self.root.parent / "sync-report.md"
                report.write_text("Sync: UNCHANGED\n")
                self.command("sync", "1", "--result", str(report))
                with patch.object(workflow, "execute", return_value={"report": "Sync: UNCHANGED\n", "route": {},
                        "diagnostics": {"usage": None}}) as execute:
                    self.command("sync", "1")
                self.assertEqual(execute.call_args.args[4], prepared)

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
