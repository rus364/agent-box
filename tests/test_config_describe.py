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
from test_external_models import rich_config


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
        document = json.loads(output.getvalue())
        self.document = document
        return output.getvalue(), {field["path"]: field for field in document["fields"]}

    def in_session(self, config, launch=None):
        """Describe from inside a box whose snapshot was rendered from ``config``."""
        config._launch = launch or {}
        runtime.sync_tracker_runtime(config, self.root / "rendered")
        with patch.dict(os.environ, {"AGENT_BOX_RUNTIME_CONFIG": str(self.root / "rendered" / "config.json")}):
            return self.describe()

    def test_every_schema_setting_is_described_with_the_menus_own_help(self):
        """An agent explaining a setting differently from the menu, or missing one, would mislead the user.

        The default configuration has no external provider, so a configured one
        is walked too; otherwise that whole schema would escape the check.
        """
        def check(value, path=()):
            for key in config_menu.menu_keys(value, path):
                full = (*path, key)
                self.assertEqual(fields[".".join(full)]["help"], config_menu.help_for(full))
                if isinstance(value[key], dict) and not config_menu.json_field(full):
                    check(value[key], full)

        for config in (Config(), rich_config()):
            save_config(config, self.global_file)
            _, fields = self.describe()
            data = config.to_dict()
            data.pop("version")
            check(data)
            self.assertTrue(all(field["help"] for field in fields.values()))
        self.assertIn("agents.external_apis.deepseek.models.v4-pro.codex.metadata", fields)
        self.assertIn("host.claude_skill_links", fields)
        self.assertEqual(fields["tracker.project"]["help"], config_menu.help_for(("tracker", "project")))
        # Only names the user chooses share generic help; a new fixed field needs its own.
        with self.assertRaises(KeyError):
            config_menu.help_for(("agents", "external_apis", "deepseek", "models", "v4-pro", "new_field"))
        with self.assertRaises(KeyError):
            config_menu.help_for(("agents", "external_apis", "deepseek", "new_field"))

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
        self.assertEqual(fields["box.tools.browser"]["source"], "default")
        self.assertTrue(fields["workflow.review.sensitive_paths"]["project_allowed"])
        self.assertFalse(fields["box.audio"]["project_allowed"])
        self.assertEqual(fields["workflow.merge_mode"]["choices"], ["github", "local"])
        self.assertEqual(fields["workflow.worker.tiers.1"]["type"], "list")
        self.assertIn("opus", fields["workflow.worker.tiers.1"]["suggestions"]["claude"])
        self.assertIn("gpt-5.6-luna", fields["workflow.scout.models"]["suggestions"]["codex"])
        self.assertTrue(fields["workflow.scout.models"]["project_allowed"])
        self.assertEqual(fields["tracker.project"]["source"], "unset")

    def test_choices_are_the_menus_for_external_api_settings(self):
        """A value the configured API model rejects must not be offered as allowed."""
        config = rich_config()
        save_config(config, self.global_file)
        output, fields = self.describe()
        self.assertEqual(fields["agents.external_apis.deepseek.models.v4-pro.effort"]["choices"],
                         ["default", "low", "high", "max"])
        self.assertEqual(fields["agents.external_apis.deepseek.claude.default_model"]["choices"], ["", "v4-flash", "v4-pro"])
        self.assertNotIn(config.agents.external_apis["deepseek"].api_key, output)

    def test_secrets_are_reported_as_set_without_their_values(self):
        """Describe output lands in an agent transcript, so a credential must never appear in it."""
        config = Config()
        config.host.notifications.telegram_token = "tg-secret-value"
        config.tracker.connections["vikunja"] = TrackerConnection("https://tracker.example", "tracker-secret-value")
        config.box.env.set = {"LITERAL": "env-secret-value"}
        config.box.env.forward = ["FORWARDED_NAME"]
        config.agents.claude.statusline.command = "curl -H 'Authorization: command-secret-value'"
        config.credentials.github.token = True
        save_config(config, self.global_file)
        output, fields = self.describe()
        for secret in ("tg-secret-value", "tracker-secret-value", "env-secret-value", "command-secret-value"):
            self.assertNotIn(secret, output)
        self.assertEqual(fields["host.notifications.telegram_token"]["effective"], "<redacted>")
        self.assertEqual(fields["tracker.connections.vikunja.token"]["global"], "<redacted>")
        self.assertEqual({key: fields["tracker.connections.jira.token"][key] for key in ("default", "source", "effective")},
                         {"default": "", "source": "default", "effective": ""})
        self.assertEqual(fields["box.env.set"]["global"], {"LITERAL": "<redacted>"})
        self.assertEqual(fields["box.env.forward"]["global"], ["FORWARDED_NAME"])
        # A token-named switch stores no credential; masking it would hide the setting itself.
        self.assertIs(fields["credentials.github.token"]["effective"], True)

    def test_a_session_snapshot_claims_no_global_value_for_host_only_settings(self):
        """Inside a box a missing section is unknown; reporting defaults would misstate the host's choices."""
        config = Config()
        config.tracker.enabled = True
        config.agents.claude.statusline.enabled = False
        _, fields = self.in_session(config)
        statusline = fields["agents.claude.statusline.enabled"]
        self.assertEqual(statusline["source"], "host-only")
        self.assertNotIn("global", statusline)
        self.assertNotIn("effective", statusline)
        self.assertEqual(fields["host.claude_skill_links"]["source"], "host-only")
        self.assertEqual(fields["tracker.enabled"]["effective"], True)

    def test_a_snapshot_without_tracker_accounts_keeps_global_urls_and_an_unknown_token(self):
        """With the tracker off the box still knows global URLs; a token it cannot see is unknown, not unset."""
        config = Config()
        config.workflow.enabled = True
        config.tracker.connections["vikunja"] = TrackerConnection("https://tracker.example", "tracker-secret-value")
        output, fields = self.in_session(config)
        url, token = fields["tracker.connections.vikunja.url"], fields["tracker.connections.vikunja.token"]
        self.assertEqual((url["global"], url["effective"], url["source"]), ("https://tracker.example", "https://tracker.example", "global"))
        self.assertEqual(token["source"], "host-only")
        self.assertNotIn("effective", token)
        self.assertEqual(fields["tracker.connections.jira.url"]["source"], "default")
        self.assertNotIn("tracker-secret-value", output)

    def test_session_flags_win_over_project_and_global_values(self):
        """Launch flags apply last; reporting the project or global value would misstate what the session uses."""
        config = Config()
        config.tracker.enabled = config.workflow.enabled = True
        config.tracker.connections["vikunja"] = TrackerConnection("https://session.example", "session-token-value")
        config._global_tracker_urls = {"vikunja": "https://global.example"}
        self.project_file.write_text(json.dumps({"workflow": {"review": {"policy": "always"}}, "tracker": {"project": "7"}}))
        output, fields = self.in_session(config, {"review": "off", "project": "12"})
        policy = fields["workflow.review.policy"]
        self.assertEqual((policy["project"], policy["effective"], policy["source"]), ("always", "off", "session"))
        project = fields["tracker.project"]
        self.assertEqual((project["project"], project["effective"], project["source"]), ("7", "12", "session"))
        url = fields["tracker.connections.vikunja.url"]
        self.assertEqual((url["global"], url["effective"], url["source"]),
                         ("https://global.example", "https://session.example", "session"))
        token = fields["tracker.connections.vikunja.token"]
        self.assertEqual((token["effective"], token["source"]), ("<redacted>", "session"))
        self.assertNotIn("global", token, "the snapshot does not carry the host token, so its presence is unknown")
        self.assertNotIn("session-token-value", output)
        self.assertIn("--review", self.document["session_options"])

    def test_a_normalized_project_value_is_not_attributed_to_a_session(self):
        """Without launch flags nothing can come from a session, even when loading trims a value."""
        self.project_file.write_text(json.dumps({"workflow": {"review": {"label": "ab:review "}},
                                                 "tracker": {"connections": {"vikunja": {"url": "https://tracker.example "}}}}))
        _, fields = self.describe()
        label = fields["workflow.review.label"]
        self.assertEqual((label["project"], label["effective"], label["source"]), ("ab:review ", "ab:review", "project"))
        url = fields["tracker.connections.vikunja.url"]
        self.assertEqual((url["effective"], url["source"]), ("https://tracker.example", "project"))
        self.assertNotIn("session_options", self.document)
        self.assertNotIn("session", {field.get("source") for field in fields.values()})

    def test_a_path_narrows_the_output_and_an_unknown_path_is_an_error(self):
        """An agent reads one section at a time; a typo must fail instead of printing nothing."""
        _, fields = self.describe("workflow.review")
        self.assertIn("workflow.review", fields)
        self.assertIn("workflow.review.sensitive_paths", fields)
        self.assertTrue(all(path.startswith("workflow.review") for path in fields))
        with self.assertRaises(AgentBoxError):
            self.describe("workflow.revie")

    def test_an_invalid_project_file_fails_before_any_output(self):
        """Partial JSON from a rejected project file would look like a valid description."""
        self.project_file.write_text(json.dumps({"audio": {"enabled": True}}))
        with contextlib.redirect_stdout(io.StringIO()) as output, self.assertRaises(AgentBoxError):
            cli.config_command(["describe"])
        self.assertEqual(output.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
