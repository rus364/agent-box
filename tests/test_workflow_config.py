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


def show_paths(text):
    """`config show` output as {dotted config path: rendered value}."""
    rows, stack = {}, []
    for line in text.split("\n\n", 1)[1].splitlines():
        depth = (len(line) - len(line.lstrip())) // 2 - 1
        key, _, value = line.strip().partition(" ")
        del stack[depth:]
        stack.append(key)
        rows[".".join(stack)] = value.strip()
    return rows


def shown(config):
    """`config show` for ``config``, with the notes that probe the host silenced."""
    from agent_box import cli
    output = io.StringIO()
    with contextlib.redirect_stdout(output), patch.object(cli, 'effective_config', return_value=config), \
         patch.object(cli, '_statusline_note', return_value=''), \
         patch.object(cli, '_image_note', return_value=''), patch.object(cli, '_browser_note', return_value=''):
        cli.config_show()
    return show_paths(output.getvalue())


class ScoutLimitValidationTests(unittest.TestCase):
    def test_scout_limit_accepts_only_whole_numbers_within_range(self):
        """The limit is a count of repository slot locks, so a fraction or out-of-range value cannot be honoured."""
        for value in (0, 17, 2.5):
            with self.subTest(value=value):
                errors = Config.from_dict({"workflow": {"scout": {"limit": value}}}).workflow.validate()
                self.assertIn("workflow.scout.limit must be between 1 and 16", errors)
        self.assertEqual(Config.from_dict({"workflow": {"scout": {"limit": 16}}}).workflow.validate(), [])


class ConfigShowTests(unittest.TestCase):
    def test_every_schema_setting_is_shown_without_per_field_code(self):
        """A setting added to the schema must appear; hand-written rows once dropped sensitive_paths."""
        config = Config.from_dict({"tracker": {"connections": {"vikunja": {"url": "https://tasks.example"}}}})
        rows = shown(config)

        def leaves(data, path=()):
            for key, value in data.items():
                if isinstance(value, dict) and value:
                    yield from leaves(value, (*path, key))
                else:
                    yield (*path, key)

        data = config.to_dict()
        data.pop("version")
        for path in leaves(data):
            with self.subTest(path=".".join(path)):
                self.assertTrue(".".join(path) in rows or f"{path[-1]} " in rows.get(".".join(path[:-1]), ""))

    def test_secret_values_never_reach_the_output(self):
        """Tokens, literal env values and a usage command line may all carry credentials."""
        from agent_box import cli
        config = Config.from_dict({
            "host": {"notifications": {"telegram_token": "SECRET-telegram"}},
            "tracker": {"connections": {"vikunja": {"url": "https://tasks.example", "token": "SECRET-tracker"}}},
            "box": {"env": {"forward": ["FORWARDED"], "set": {"LITERAL": "SECRET-env"}}},
            "agents": {"claude": {"statusline": {"command": "curl -H 'Authorization: Bearer SECRET-usage'"}},
                       "external_apis": {"api": {
                "enabled": True, "api_key": "SECRET-key",
                "models": {"small": {"codex": {"env": {"MODEL_ENV": "SECRET-model-env"}}}},
                "codex": {"url": "https://api.example/v1", "env": {"ENDPOINT_ENV": "SECRET-endpoint-env"},
                          "provider_options": {"http_headers": {"X-Key": "SECRET-header"}}}}}}})
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(cli, 'effective_config', return_value=config), \
             patch.object(cli, '_statusline_note', return_value=''), \
             patch.object(cli, '_image_note', return_value=''), patch.object(cli, '_browser_note', return_value=''):
            cli.config_show()
        self.assertNotIn("SECRET", output.getvalue())
        rows = show_paths(output.getvalue())
        self.assertEqual(rows["box.env.forward"], "FORWARDED")
        self.assertEqual(rows["box.env.set"], "LITERAL=<configured>")
        self.assertIn("command (set)", rows["agents.claude.statusline"])
        self.assertIn("token (set)", rows["tracker.connections.vikunja"])


class WorkflowConfigTests(unittest.TestCase):
    def test_role_models_survive_global_save_and_minimal_runtime(self):
        """Saved candidate lists reach the container without host secrets."""
        from agent_box import runtime
        from agent_box.config import load_config, save_config
        models = [{"provider": "codex", "model": "gpt-5.6-luna", "effort": "high"},
                  {"provider": "claude", "model": "deepseek-v4-flash", "effort": "high", "api_provider": "deepseek"}]
        for tier in ("1", "3"):
            with self.subTest(tier=tier), tempfile.TemporaryDirectory() as directory:
                config = Config.from_dict({"workflow": {"enabled": True,
                    "worker": {"tiers": {tier: models[:1]}},
                    "scout": {"models": models}, "review": {"models": models[::-1]}}})
                config.host.notifications.telegram_token = "host-secret"
                path = Path(directory) / "global.json"
                save_config(config, path)
                loaded = load_config(path)
                self.assertEqual(loaded.workflow, config.workflow)
                self.assertEqual([spec["model"] for spec in loaded.workflow.review.models], ["deepseek-v4-flash", "gpt-5.6-luna"])
                self.assertEqual(json.loads(path.read_text())["version"], 1)
                runtime.sync_tracker_runtime(loaded, Path(directory) / "runtime")
                raw = (Path(directory) / "runtime" / "config.json").read_text()
                self.assertNotIn("host-secret", raw)
                self.assertEqual(Config.from_dict(json.loads(raw)).workflow, config.workflow)

    def test_invalid_candidates_cannot_become_defaults(self):
        """Unusable candidates must fail global and runtime validation."""
        from agent_box.config import effective_config, load_config
        from agent_box.errors import AgentBoxError
        cases = [({"scout": {"models": [{"provider": "gemini", "model": "x"}]}}, "workflow.scout.models.0: provider"),
                  ({"worker": {"tiers": {"2": [{"provider": "codex"}]}}}, "workflow.worker.tiers.2.0: model"),
                  # Malformed entries stay in place so a hand edit cannot silently empty a list.
                  ({"worker": {"models": ["codex:gpt-5.5"]}}, "workflow.worker.models.0: provider"),
                  ({"review": {"tiers": {"1": [{"provider": "codex", "model": 5}]}}}, "workflow.review.tiers.1.0: model"),
                  ({"worker": {"models": {"provider": "codex", "model": "gpt-5.5"}}}, "workflow.worker.models must be a list"),
                  ({"review": {"tiers": [[]]}}, "workflow.review.tiers must be an object"),
                  ({"review": {"tiers": {"0": []}}}, "workflow.review.tiers.0 is not a task tier"),
                  ({"review": {"models": [{"provider": "claude", "model": "opus", "effort": "invalid"}]}},
                   "workflow.review.models.0: invalid effort")]
        for workflow, message in cases:
            with self.subTest(workflow=workflow), tempfile.TemporaryDirectory() as directory:
                artifact = Path(directory) / "config.json"
                artifact.write_text(json.dumps({"workflow": workflow}))
                self.assertTrue(any(message in error for error in load_config(artifact).workflow.validate()))
                with patch.dict(os.environ, {"AGENT_BOX_RUNTIME_CONFIG": str(artifact)}), \
                     patch("agent_box.project_config.load", return_value={}), \
                     self.assertRaisesRegex(AgentBoxError, message.split(":")[0]):
                    effective_config()

    def test_show_lists_candidate_identity_and_speed(self):
        """One row per list shows candidate identity, speed and preference order."""
        config = Config()
        config.workflow.scout.models = [
            {"provider": "codex", "model": "gpt-5.6-luna", "effort": "high", "speed": "inherit", "api_provider": ""},
            {"provider": "claude", "model": "v4", "effort": "high", "speed": "inherit", "api_provider": "deepseek"}]
        rows = shown(config)
        self.assertEqual(rows['workflow.scout.models'], 'codex:gpt-5.6-luna:high speed=inherit, claude@deepseek:v4:high speed=inherit')
        self.assertEqual(rows['workflow.review.models'], '(none)')

    def test_show_reads_the_runtime_artifact_instead_of_host_defaults(self):
        """A container shows the merge route workflow dispatch uses, and no host section as defaults."""
        from agent_box import cli
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "runtime.json"
            artifact.write_text(json.dumps({"workflow": {"enabled": True, "merge_mode": "local"},
                                            "agents": {"claude": {"remote_control": True}},
                                            "launch": {"push": False}}))
            output = io.StringIO()
            with patch.dict(os.environ, {"AGENT_BOX_RUNTIME_CONFIG": str(artifact)}), \
                 patch.object(cli, "load_config", return_value=Config()), \
                 patch.object(cli, "_statusline_note", return_value=""), \
                 patch.object(cli, "_image_note", return_value=""), contextlib.redirect_stdout(output):
                cli.config_show()
            rows = show_paths(output.getvalue())
            self.assertEqual(rows["workflow.merge_mode"], "local")
            self.assertNotIn("host.notifications.enabled", rows)
            self.assertEqual(rows["agents.claude.remote_control"], "on")
            self.assertNotIn("agents.claude.statusline.enabled", rows)
            self.assertNotIn("agents.default", rows)
            self.assertIn(str(artifact), output.getvalue())

    def test_show_from_a_snapshot_keeps_free_keyed_entries_but_not_foreign_accounts(self):
        """A project's own mirror or variable is effective even when an older or another project's
        snapshot never rendered it; a tracker account the snapshot does not carry is not this session's."""
        config = Config.from_dict({
            "box": {"k3s": {"registry_mirrors": {"docker.io": ["https://mirror.example"]}},
                    "env": {"set": {"PROJECT_VAR": "value"}}},
            "tracker": {"connections": {"vikunja": {"url": "https://tasks.example"},
                                        "yandex": {"url": "https://other.example"}}}})
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "runtime.json"
            artifact.write_text(json.dumps({"box": {"k3s": {"registry_mirrors": {}}, "env": {"set": {}}},
                                            "tracker": {"connections": {"vikunja": {"url": "https://tasks.example"}}}}))
            with patch.dict(os.environ, {"AGENT_BOX_RUNTIME_CONFIG": str(artifact)}):
                rows = shown(config)
        self.assertEqual(rows["box.k3s.registry_mirrors"], 'docker.io=["https://mirror.example"]')
        self.assertEqual(rows["box.env.set"], "PROJECT_VAR=<configured>")
        self.assertIn("tracker.connections.vikunja", rows)
        self.assertNotIn("tracker.connections.yandex", rows)

    def test_cleanup_opt_ins_round_trip_through_config_runtime_and_show(self):
        from agent_box import config_menu, runtime
        for local, remote in ((False, False), (True, False), (False, True), (True, True)):
            config = Config.from_dict({'workflow': {
                'enabled': True, 'cleanup_local_branches': local, 'cleanup_remote_branches': remote}})
            config = Config.from_dict(config.to_dict())
            with tempfile.TemporaryDirectory() as directory:
                runtime.sync_tracker_runtime(config, Path(directory))
                rendered = Config.from_dict(json.loads((Path(directory) / 'config.json').read_text()))
            self.assertEqual(rendered.workflow.cleanup_local_branches, local)
            self.assertEqual(rendered.workflow.cleanup_remote_branches, remote)
            rows = shown(config)
            self.assertEqual(rows['workflow.cleanup_local_branches'], 'on' if local else 'off')
            self.assertEqual(rows['workflow.cleanup_remote_branches'], 'on' if remote else 'off')
        self.assertFalse(Config().workflow.cleanup_local_branches)
        self.assertFalse(Config().workflow.cleanup_remote_branches)
        for field in ('cleanup_local_branches', 'cleanup_remote_branches'):
            self.assertIn('workflow.' + field, config_menu.HELP)

    def test_tracker_and_workflow_start_disabled_with_no_candidates(self):
        data = Config().to_dict()
        self.assertIn("tracker", data)
        self.assertFalse(data["tracker"]["enabled"])
        self.assertFalse(data["workflow"]["enabled"])
        self.assertEqual(data["workflow"]["merge_mode"], "github")
        self.assertFalse(data["workflow"]["use_tiers"])
        self.assertEqual(data["workflow"]["scout"], {"limit": 3, "models": []})
        for role in ("worker", "review"):
            self.assertEqual(data["workflow"][role]["models"], [])
            self.assertEqual(data["workflow"][role]["tiers"], {tier: [] for tier in ("1", "2", "3")})

    def test_removed_routing_sections_are_dropped_on_load(self):
        """Pre-release: the provider-share settings are gone, not migrated."""
        data = {
            "tracker": {"enabled": True, "provider": "vikunja", "connections": {
                "vikunja": {"url": "https://tasks.example", "token": "secret"}}},
            "workflow": {"enabled": True, "routing": "usage-aware", "weights": {"codex": 3},
                         "tiers": {"0": {"codex": {"model": "small"}}}, "fallback": False,
                         "worker": {"override": {"scope": "all"}}, "scout": {"tiers": {"0": []}},
                         "review": {"policy": "always", "max_files": 9, "routing": "same-provider"}},
        }
        result = Config.from_dict(data).to_dict()
        self.assertEqual(result["tracker"], data["tracker"])
        for removed in ("routing", "weights", "tiers", "fallback", "reserve_percent", "smoothing_hours"):
            self.assertNotIn(removed, result["workflow"])
        self.assertNotIn("override", result["workflow"]["worker"])
        self.assertNotIn("tiers", result["workflow"]["scout"])
        self.assertNotIn("routing", result["workflow"]["review"])
        self.assertEqual(result["workflow"]["review"]["policy"], "always")

    def test_merge_mode_round_trips_and_rejects_unknown_values(self):
        config = Config.from_dict({"workflow": {"merge_mode": "local"}})
        self.assertEqual(Config.from_dict(config.to_dict()).workflow.merge_mode, "local")
        config.workflow.merge_mode = "magic"
        self.assertTrue(any("merge_mode" in error for error in config.workflow.validate()))

    def test_default_effort_round_trips_for_both_providers(self):
        loaded = Config.from_dict({"workflow": {"scout": {"models": [
            {"provider": "codex", "model": "small", "effort": "default"},
            {"provider": "claude", "model": "haiku", "effort": "default"}]}}})
        config = Config.from_dict(loaded.to_dict())
        self.assertEqual([spec["effort"] for spec in config.workflow.scout.models], ["default", "default"])
        self.assertEqual(config.workflow.validate(), [])

    def test_effort_is_checked_against_the_candidates_own_provider(self):
        """ultra is a Codex level; the same word on a Claude candidate is an error."""
        config = Config.from_dict({"workflow": {"worker": {"tiers": {"1": [
            {"provider": "codex", "model": "codex", "effort": "ultra"},
            {"provider": "claude", "model": "claude", "effort": "unsupported"}]}}}})
        self.assertEqual(config.workflow.validate(), ["workflow.worker.tiers.1.1: invalid effort"])


if __name__ == "__main__":
    unittest.main()
