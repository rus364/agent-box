"""`config set --project` is how `config ai` agents write; a bad change must never reach the file."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_box import cli, completion, project_config, runtime
from agent_box.errors import AgentBoxError


class ConfigSetTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.file = Path(temp.name) / ".agent-box.json"
        self.binding = {"tracker": {"provider": "vikunja", "project": "12", "recursive": True,
                                    "connections": {"vikunja": {"url": "https://tracker.example"}}}}
        self.file.write_text(json.dumps(self.binding, indent=2) + "\n")

        def die(message):
            raise AgentBoxError(message)

        for patcher in (patch.dict(os.environ, {}, clear=True),
                        patch.object(project_config, "project_path", return_value=self.file),
                        patch.object(cli, "die", side_effect=die)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_set(self, *args):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            cli.config_command(["set", *args])
        return output.getvalue()

    def saved(self):
        return json.loads(self.file.read_text())

    def test_set_writes_nested_values_after_showing_the_diff_and_keeps_the_binding(self):
        """The agent shows the user this diff; the file must then hold exactly that change."""
        with patch.object(runtime, "apply_config", side_effect=AssertionError("apply reached")):
            output = self.run_set("--project", "workflow.review.max_files=5", 'workflow.review.sensitive_paths=["src/auth/**"]')
        self.assertIn('+      "max_files": 5', output)
        self.assertIn("Saved project configuration", output)
        self.assertEqual(self.saved(), {**self.binding, "workflow": {"review": {"max_files": 5, "sensitive_paths": ["src/auth/**"]}}})

    def test_dry_run_prints_the_same_diff_and_writes_nothing(self):
        """Previewing is how the agent asks for consent, so it must not already have changed the file."""
        before = self.file.read_text()
        output = self.run_set("--project", "--dry-run", "tools.browser=true")
        self.assertIn('+    "browser": true', output)
        self.assertIn("Dry run: nothing written", output)
        self.assertEqual(self.file.read_text(), before)

    def test_explicit_false_empty_and_list_values_are_choices_not_removals(self):
        """false and an empty list override inheritance; dropping them would inherit again."""
        self.run_set("--project", "workflow.auto_push=false", 'workflow.worker.tiers.1=[]',
                     "workflow.review.ignored_paths=[]")
        workflow = self.saved()["workflow"]
        self.assertIs(workflow["auto_push"], False)
        self.assertEqual(workflow["worker"]["tiers"]["1"], [])
        self.assertEqual(workflow["review"]["ignored_paths"], [])

    def test_reset_removes_the_override_and_empty_sections_so_the_path_inherits(self):
        self.run_set("--project", "workflow.review.max_files=5", "tools.browser=false")
        output = self.run_set("--project", "--reset", "workflow.review.max_files")
        self.assertIn('-      "max_files": 5', output)
        self.assertEqual(self.saved(), {**self.binding, "tools": {"browser": False}})
        self.assertIn("No changes", self.run_set("--project", "--reset", "workflow.review.max_lines"))

    def test_invalid_changes_leave_the_file_untouched_and_never_echo_the_value(self):
        """A refused change must not half-apply, and a secret typed into it must not reach the transcript."""
        before = self.file.read_text()
        refused = {
            ("audio.enabled=true",): "is not a project field",
            ("workflow.review.max_files=\"five\"",): "invalid type",
            ('tracker.connections.vikunja.token="token-secret-value"',): "PROJECT_CREDENTIALS_FORBIDDEN",
            ("statusline.command=curl-secret-value",): "must be JSON",
            ("workflow.review.policy=\"sometimes\"",): "",
            ("--reset",): "expected",
            ("tools.browser=true", "tools.browser=false"): "once",
            ('workflow.review={"max_files": 5}', "workflow.review.max_lines=3"): "containing",
            ("--reset", "workflow.review", "workflow.review.max_lines=3"): "containing",
        }
        for args, message in refused.items():
            with self.subTest(args=args), contextlib.redirect_stdout(io.StringIO()) as output:
                with self.assertRaises(AgentBoxError) as error:
                    cli.config_command(["set", "--project", *args])
                self.assertIn(message, str(error.exception))
                self.assertNotIn("secret-value", str(error.exception) + output.getvalue())
                if "PROJECT" in message or "field" in message:
                    # Blaming the file on disk would send the user hunting for a secret that is not there.
                    self.assertIn("proposed change; file unchanged", str(error.exception))
                self.assertEqual(self.file.read_text(), before)

    def test_the_project_scope_is_required_and_the_command_is_documented(self):
        """Global writes arrive separately; a missing scope must not guess one."""
        with self.assertRaises(AgentBoxError):
            self.run_set("tools.browser=true")
        with contextlib.redirect_stdout(io.StringIO()) as output:
            cli.usage_text()
        self.assertIn("config set --project", output.getvalue())
        self.assertIn(("set", ""), completion.COMMANDS["config"]([""])[1])


if __name__ == "__main__":
    unittest.main()
