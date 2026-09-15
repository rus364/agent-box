"""`config describe` is what `config ai` agents read; it must match the menu and never leak a secret."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_box import cli, config as config_module, config_menu, project_config, runtime
from agent_box.config import Config, TrackerConnection, save_config
from agent_box.errors import AgentBoxError


class ConfigDescribeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.global_file = self.root / "config.json"
        self.project_file = self.root / ".agent-box.json"
        for patcher in (patch.dict(os.environ, {}, clear=True),
                        patch.object(config_module, "CONFIG_FILE", self.global_file),
                        patch.object(project_config, "project_path", return_value=self.project_file)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def describe(self, *args):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            cli.config_command(["describe", *args])
        return output.getvalue(), {field["path"]: field for field in json.loads(output.getvalue())["fields"]}

    def test_every_menu_setting_is_described_with_the_menus_own_help(self):
        """An agent explaining a setting differently from the menu, or missing one, would mislead the user."""
        _, fields = self.describe()
        data = Config().to_dict()
        data.pop("version")
        data.pop("install")

        def check(value, path=()):
            for key in config_menu.menu_keys(value, path):
                full = (*path, key)
                self.assertEqual(fields[".".join(full)]["help"], config_menu.help_for(full))
                if isinstance(value[key], dict) and not config_menu.json_field(full):
                    check(value[key], full)

        check(data)
        self.assertEqual(fields["tracker.project"]["help"], config_menu.help_for(("tracker", "project")))
        self.assertTrue(all(field["help"] for field in fields.values()))

    def test_layers_sources_and_project_eligibility_follow_the_project_loader(self):
        """The agent must know which value wins, where it comes from, and whether a project may change it."""
        config = Config()
        config.workflow.review.max_files = Config().workflow.review.max_files + 2
        save_config(config, self.global_file)
        self.project_file.write_text(json.dumps({"workflow": {"review": {"max_lines": 10}}}))
        _, fields = self.describe()
        self.assertEqual({key: fields["workflow.review.max_files"][key] for key in ("default", "global", "source", "effective")},
                         {"default": config.workflow.review.max_files - 2, "global": config.workflow.review.max_files,
                          "source": "global", "effective": config.workflow.review.max_files})
        self.assertEqual((fields["workflow.review.max_lines"]["project"], fields["workflow.review.max_lines"]["source"],
                          fields["workflow.review.max_lines"]["effective"]), (10, "project", 10))
        self.assertEqual(fields["browser.enabled"]["source"], "default")
        self.assertTrue(fields["workflow.review.sensitive_paths"]["project_allowed"])
        self.assertFalse(fields["audio.enabled"]["project_allowed"])
        self.assertEqual(fields["workflow.merge_mode"]["choices"], ["github", "local"])
        self.assertIn("xhigh", fields["workflow.tiers.1.codex.effort"]["choices"])
        self.assertIn("opus", fields["workflow.tiers.1.claude.model"]["suggestions"])
        self.assertEqual(fields["tracker.project"]["source"], "unset")

    def test_secrets_are_reported_as_set_without_their_values(self):
        """Describe output lands in an agent transcript, so a credential must never appear in it."""
        config = Config()
        config.notifications.telegram_token = "tg-secret-value"
        config.tracker.connections["vikunja"] = TrackerConnection("https://tracker.example", "tracker-secret-value")
        config.env.set = {"LITERAL": "env-secret-value"}
        config.env.forward = ["FORWARDED_NAME"]
        config.usage.command = "curl -H 'Authorization: command-secret-value'"
        save_config(config, self.global_file)
        output, fields = self.describe()
        for secret in ("tg-secret-value", "tracker-secret-value", "env-secret-value", "command-secret-value"):
            self.assertNotIn(secret, output)
        self.assertEqual(fields["notifications.telegram_token"]["effective"], "<redacted>")
        self.assertEqual(fields["tracker.connections.vikunja.token"]["global"], "<redacted>")
        self.assertEqual(fields["tracker.connections.jira.token"]["source"], "unset")
        self.assertNotIn("effective", fields["tracker.connections.jira.token"])
        self.assertEqual(fields["env.set"]["global"], {"LITERAL": "<redacted>"})
        self.assertEqual(fields["env.forward"]["global"], ["FORWARDED_NAME"])

    def test_a_session_snapshot_claims_no_global_value_for_host_only_settings(self):
        """Inside a box a missing section is unknown; reporting defaults would misstate the host's choices."""
        config = Config()
        config.tracker.enabled = True
        config.claude.statusline = False
        runtime.sync_tracker_runtime(config, self.root / "rendered")
        with patch.dict(os.environ, {"AGENT_BOX_RUNTIME_CONFIG": str(self.root / "rendered" / "config.json")}):
            _, fields = self.describe()
        statusline = fields["claude.statusline"]
        self.assertEqual(statusline["source"], "host-only")
        self.assertNotIn("global", statusline)
        self.assertNotIn("effective", statusline)
        self.assertEqual(fields["tracker.enabled"]["effective"], True)

    def test_a_path_narrows_the_output_and_an_unknown_path_is_an_error(self):
        _, fields = self.describe("workflow.review")
        self.assertIn("workflow.review", fields)
        self.assertIn("workflow.review.sensitive_paths", fields)
        self.assertTrue(all(path.startswith("workflow.review") for path in fields))
        with self.assertRaises(AgentBoxError):
            self.describe("workflow.revie")

    def test_an_invalid_project_file_fails_before_any_output(self):
        self.project_file.write_text(json.dumps({"audio": {"enabled": True}}))
        with contextlib.redirect_stdout(io.StringIO()) as output, self.assertRaises(AgentBoxError):
            cli.config_command(["describe"])
        self.assertEqual(output.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
