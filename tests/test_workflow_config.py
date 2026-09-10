"""Workflow configuration must preserve intent without exposing disabled credentials."""
import unittest
import contextlib
import io
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from agent_box.config import Config


class WorkflowConfigTests(unittest.TestCase):
    def test_execution_and_scopes_survive_global_save_and_minimal_runtime(self):
        """Saved execution and model sources reach the container without host secrets."""
        from agent_box import runtime
        from agent_box.config import load_config, save_config
        self.assertEqual(Config().workflow.worker.execution, "auto")
        for execution in ("auto", "direct", "delegated"):
            for scope in ("off", "untiered", "all"):
                with self.subTest(execution=execution, scope=scope), tempfile.TemporaryDirectory() as directory:
                    config = Config.from_dict({"workflow": {"enabled": True,
                        "worker": {"execution": execution, "override": {"scope": scope}},
                        "review": {"override": {"scope": scope}}}})
                    config.notifications.telegram_token = "host-secret"
                    path = Path(directory) / "global.json"
                    save_config(config, path)
                    loaded = load_config(path)
                    self.assertEqual(loaded.workflow, config.workflow)
                    self.assertEqual(json.loads(path.read_text())["version"], 1)
                    runtime.sync_tracker_runtime(loaded, Path(directory) / "runtime")
                    raw = (Path(directory) / "runtime" / "config.json").read_text()
                    self.assertNotIn("host-secret", raw)
                    self.assertEqual(Config.from_dict(json.loads(raw)).workflow, config.workflow)

    def test_invalid_execution_and_scope_values_cannot_become_defaults(self):
        """Unknown, blank and wrongly typed enums must fail global and runtime validation."""
        from agent_box.config import effective_config, load_config
        from agent_box.errors import AgentBoxError
        for path in (("worker", "execution"), ("worker", "override", "scope"),
                     ("review", "override", "scope")):
            for invalid in ("invalid", "", " all ", None, True, 1, [], {}):
                workflow = {}
                target = workflow
                for part in path[:-1]:
                    target = target.setdefault(part, {})
                target[path[-1]] = invalid
                dotted = "workflow." + ".".join(path)
                with self.subTest(path=dotted, invalid=invalid), tempfile.TemporaryDirectory() as directory:
                    artifact = Path(directory) / "config.json"
                    artifact.write_text(json.dumps({"workflow": workflow}))
                    self.assertTrue(any(dotted in error for error in load_config(artifact).workflow.validate()))
                    with patch.dict(os.environ, {"AGENT_BOX_RUNTIME_CONFIG": str(artifact)}), \
                         patch("agent_box.project_config.load", return_value={}), \
                         self.assertRaisesRegex(AgentBoxError, dotted):
                        effective_config()

    def test_reviewer_override_roundtrips_into_runtime_without_sharing_candidates(self):
        """The container must receive the same opt-in and independent settings as the menu."""
        from agent_box import runtime
        original = Config()
        self.assertEqual(original.workflow.review.override.scope, "off")
        self.assertTrue(all(not spec["model"] for spec in original.workflow.review.override.models.values()))
        for scope in ("off", "untiered", "all"):
            config = Config.from_dict({"workflow": {"enabled": True, "review": {"override": {
                "scope": scope, "models": {
                    "codex": {"model": "custom-codex", "effort": "default"},
                    "claude": {"model": "custom-claude", "effort": "high"}}}}}})
            with tempfile.TemporaryDirectory() as directory:
                runtime.sync_tracker_runtime(config, Path(directory))
                rendered = Config.from_dict(json.loads((Path(directory) / "config.json").read_text()))
            self.assertEqual(rendered.workflow.review.override, config.workflow.review.override)
            self.assertEqual(rendered.workflow.validate(), [])
            rendered.workflow.review.override.models["codex"]["model"] = "changed"
            self.assertEqual(rendered.workflow.review.tiers["1"]["codex"]["model"], "")
            self.assertEqual(rendered.workflow.tiers["1"]["codex"]["model"], "")
            self.assertEqual(original.workflow.review.override.models["codex"]["model"], "")
        config.workflow.review.override.models["claude"]["effort"] = "invalid"
        self.assertIn("workflow.review.override.models.claude: invalid effort", config.workflow.validate())

    def test_worker_override_roundtrips_into_runtime_without_sharing_candidates(self):
        """The container must receive the same opt-in and independent settings as the menu."""
        from agent_box import runtime
        original = Config()
        self.assertEqual(original.workflow.worker.override.scope, "off")
        self.assertTrue(all(not spec["model"] for spec in original.workflow.worker.override.models.values()))
        for scope in ("off", "untiered", "all"):
            config = Config.from_dict({"workflow": {"enabled": True, "worker": {"override": {
                "scope": scope, "models": {
                    "codex": {"model": "custom-codex", "effort": "default"},
                    "claude": {"model": "custom-claude", "effort": "high"}}}}}})
            with tempfile.TemporaryDirectory() as directory:
                runtime.sync_tracker_runtime(config, Path(directory))
                rendered = Config.from_dict(json.loads((Path(directory) / "config.json").read_text()))
            self.assertEqual(rendered.workflow.worker.override, config.workflow.worker.override)
            self.assertEqual(rendered.workflow.validate(), [])
            rendered.workflow.worker.override.models["codex"]["model"] = "changed"
            self.assertEqual(rendered.workflow.review.tiers["1"]["codex"]["model"], "")
            self.assertEqual(rendered.workflow.tiers["1"]["codex"]["model"], "")
            self.assertEqual(original.workflow.worker.override.models["codex"]["model"], "")
        self.assertEqual(rendered.workflow.review.override, original.workflow.review.override)
        config.workflow.worker.override.models["claude"]["effort"] = "invalid"
        self.assertIn("workflow.worker.override.models.claude: invalid effort", config.workflow.validate())

    def test_show_distinguishes_worker_and_reviewer_override_scope(self):
        """A visible worker override must not imply reviewer settings have changed."""
        from agent_box import cli
        config = Config()
        config.workflow.worker.override.scope = "untiered"
        config.workflow.worker.override.models['codex'] = {'model': 'worker-model', 'effort': 'high'}
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(cli, 'effective_config', return_value=config), \
             patch.object(cli, '_statusline_note', return_value=''), \
             patch.object(cli, '_image_note', return_value=''), patch.object(cli, '_browser_note', return_value=''):
            cli.config_show()
        self.assertIn('worker model scope        untiered', output.getvalue())
        self.assertIn('reviewer model scope      off', output.getvalue())
        self.assertIn('worker execution          auto', output.getvalue())
        self.assertEqual(output.getvalue().count('delegated routing'), 1)
        self.assertIn("worker override codex: {'model': 'worker-model', 'effort': 'high'}", output.getvalue())

    def test_show_reads_the_runtime_artifact_instead_of_host_defaults(self):
        """A container must show the same merge route that workflow dispatch uses."""
        from agent_box import cli
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "runtime.json"
            artifact.write_text(json.dumps({"workflow": {"enabled": True, "merge_mode": "local"}}))
            output = io.StringIO()
            with patch.dict(os.environ, {"AGENT_BOX_RUNTIME_CONFIG": str(artifact)}), \
                 patch.object(cli, "load_config", return_value=Config()), \
                 patch.object(cli, "_statusline_note", return_value=""), \
                 patch.object(cli, "_image_note", return_value=""), contextlib.redirect_stdout(output):
                cli.config_show()
            self.assertIn("merge mode                local", output.getvalue())
            self.assertIn(str(artifact), output.getvalue())

    def test_cleanup_opt_ins_round_trip_through_config_runtime_and_show(self):
        from agent_box import cli, config_menu, runtime
        for local, remote in ((False, False), (True, False), (False, True), (True, True)):
            config = Config.from_dict({'workflow': {
                'enabled': True, 'cleanup_local_branches': local, 'cleanup_remote_branches': remote}})
            config = Config.from_dict(config.to_dict())
            with tempfile.TemporaryDirectory() as directory:
                runtime.sync_tracker_runtime(config, Path(directory))
                rendered = Config.from_dict(json.loads((Path(directory) / 'config.json').read_text()))
            self.assertEqual(rendered.workflow.cleanup_local_branches, local)
            self.assertEqual(rendered.workflow.cleanup_remote_branches, remote)
            output = io.StringIO()
            with contextlib.redirect_stdout(output), patch.object(cli, 'effective_config', return_value=config), \
                 patch.object(cli, '_statusline_note', return_value=''), \
                 patch.object(cli, '_image_note', return_value=''), \
                 patch.object(cli, '_browser_note', return_value=''):
                cli.config_show()
            self.assertIn('cleanup local branches    ' + str(local), output.getvalue())
            self.assertIn('cleanup remote branches   ' + str(remote), output.getvalue())
        self.assertFalse(Config().workflow.cleanup_local_branches)
        self.assertFalse(Config().workflow.cleanup_remote_branches)
        for field in ('cleanup_local_branches', 'cleanup_remote_branches'):
            self.assertIn('workflow.' + field, config_menu.HELP)

    def test_tracker_and_workflow_start_disabled(self):
        data = Config().to_dict()
        self.assertIn("tracker", data)
        self.assertFalse(data["tracker"]["enabled"])
        self.assertFalse(data["workflow"]["enabled"])
        self.assertEqual(data["workflow"]["merge_mode"], "github")
        self.assertTrue(all(
            spec["effort"] == "medium"
            for tier in data["workflow"]["tiers"].values()
            for spec in tier.values()))

    def test_routing_and_connections_round_trip_without_legacy_sections(self):
        data = {
            "tracker": {"enabled": True, "provider": "vikunja", "connections": {
                "vikunja": {"url": "https://tasks.example", "token": "secret"}}},
            "workflow": {"enabled": True, "routing": "usage-aware", "weights": {
                "codex": 3, "claude": 1}, "tiers": {"0": {
                    "codex": {"model": "small", "effort": "low"}}, "3": {
                    "claude": {"model": "large", "effort": "high"}}},
                "fallback": False, "review": {"policy": "always", "max_files": 9}},
        }
        result = Config.from_dict(data).to_dict()
        self.assertEqual(result["tracker"], data["tracker"])
        self.assertEqual(result["workflow"]["weights"], {"codex": 3, "claude": 1})
        self.assertEqual(result["workflow"]["tiers"]["3"]["claude"]["model"], "large")
        self.assertFalse(result["workflow"]["fallback"])
        self.assertEqual(result["workflow"]["review"]["policy"], "always")
        self.assertNotIn("flow_next", result)
        self.assertNotIn("vikunja", result)

    def test_invalid_routing_values_are_reported_before_work_is_dispatched(self):
        config = Config.from_dict({"workflow": {"routing": "magic", "weights": {
            "codex": -1, "claude": 0}}})
        self.assertTrue(any("routing" in e for e in config.workflow.validate()))

    def test_merge_mode_round_trips_and_rejects_unknown_values(self):
        config = Config.from_dict({"workflow": {"merge_mode": "local"}})
        self.assertEqual(Config.from_dict(config.to_dict()).workflow.merge_mode, "local")
        config.workflow.merge_mode = "magic"
        self.assertTrue(any("merge_mode" in error for error in config.workflow.validate()))

    def test_default_effort_round_trips_for_both_providers(self):
        loaded = Config.from_dict({"workflow": {"tiers": {"0": {
            "codex": {"model": "small", "effort": "default"},
            "claude": {"model": "haiku", "effort": "default"},
        }}}})
        config = Config.from_dict(loaded.to_dict())
        self.assertEqual(config.workflow.tiers["0"]["codex"]["effort"], "default")
        self.assertEqual(config.workflow.tiers["0"]["claude"]["effort"], "default")
        self.assertEqual(config.workflow.validate(), [])

    def test_unknown_effort_is_rejected_without_changing_existing_levels(self):
        config = Config.from_dict({"workflow": {"tiers": {"1": {
            "codex": {"model": "codex", "effort": "ultra"},
            "claude": {"model": "claude", "effort": "unsupported"},
        }}}})
        errors = config.workflow.validate()
        self.assertFalse(any("codex" in error for error in errors))
        self.assertTrue(any("claude" in error and "effort" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
