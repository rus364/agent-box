"""`config ai` runs an agent session that configures agent-box; starting it must apply nothing."""
import contextlib
import io
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agent_box import cli, completion, config as config_module, config_proposal, runtime
from agent_box.config import Config, load_config, save_config
from agent_box.errors import AgentBoxError
from agent_box.paths import CONTAINER_LAUNCHER_ROOT, MANAGED_SKILLS_SOURCE

SKILL = MANAGED_SKILLS_SOURCE / "ab-config" / "SKILL.md"


class ConfigAiTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.proposal = self.root / "git" / "agent-box" / "config-proposal.json"

        def die(message):
            raise AgentBoxError(message)

        self.launch = Mock(side_effect=SystemExit(0))
        self.apply = Mock(return_value=["applied"])
        self.answers = []
        for patcher in (patch.dict(os.environ, {}, clear=True),
                        patch.object(config_module, "CONFIG_FILE", self.root / "config.json"),
                        patch.object(config_proposal, "proposal_path", return_value=self.proposal),
                        patch.object(cli, "_inside_session", return_value=False),
                        patch.object(runtime, "runtime_main", self.launch),
                        patch.object(runtime, "apply_config", self.apply),
                        patch.object(runtime, "sync_managed_skills", side_effect=AssertionError("launch installed skills")),
                        patch.object(cli, "die", side_effect=die),
                        patch.object(cli.sys, "stdin", Mock(isatty=Mock(return_value=True))),
                        patch("builtins.input", side_effect=lambda prompt: self.answers.pop(0))):
            patcher.start()
            self.addCleanup(patcher.stop)
        save_config(Config())

    def run_ai(self, *args):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            cli.config_command(["ai", *args])
        return output.getvalue()

    def test_the_session_starts_in_the_box_with_the_skill_and_applies_nothing(self):
        """Launching is not applying: config ai must neither install skills nor apply configuration."""
        self.run_ai("codex")
        args, script_dir, _ = self.launch.call_args.args
        self.assertEqual(args[0], "codex")
        self.assertIn(str(CONTAINER_LAUNCHER_ROOT / "agent_box/skills/ab-config/SKILL.md"), args[1])
        self.assertTrue((script_dir / "agent_box/skills/ab-config/SKILL.md").is_file(), "the prompt names a shipped file")
        self.run_ai()
        self.assertEqual(len(self.launch.call_args.args[0]), 1, "without an agent the configured default starts")
        self.apply.assert_not_called()

    def test_a_proposal_made_in_the_session_is_offered_when_it_ends(self):
        """The box can never apply global changes, so the host command offers them as soon as the session returns."""
        def session(*_):
            config_proposal.save(self.proposal, {"set": {"workflow.review.max_files": 9}, "reset": []})
            raise SystemExit(0)

        self.launch.side_effect = session
        self.answers[:] = ["y"]
        output = self.run_ai("claude")
        self.assertIn("+ workflow.review.max_files = 9", output)
        self.assertEqual(load_config().workflow.review.max_files, 9)
        self.apply.assert_called_once()
        self.assertFalse(self.proposal.exists())

    def test_a_pending_proposal_is_offered_first_and_the_session_exit_code_survives(self):
        config_proposal.save(self.proposal, {"set": {"tools.browser": True}, "reset": []})
        self.answers[:] = ["n"]
        self.launch.side_effect = SystemExit(3)
        with self.assertRaises(SystemExit) as exited:
            self.run_ai()
        self.assertEqual(exited.exception.code, 3)
        self.assertFalse(self.proposal.exists(), "declined before the session")
        self.assertFalse(load_config().tools.browser())
        self.apply.assert_not_called()

    def test_an_unreadable_proposal_is_reported_and_the_session_still_starts(self):
        """A broken leftover file must not lock the user out of the guided setup."""
        self.proposal.parent.mkdir(parents=True)
        self.proposal.write_text("{not json")
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            self.run_ai()
        self.assertIn("Unreadable global proposal", errors.getvalue())
        self.launch.assert_called_once()

    def test_inside_an_agent_session_it_refuses_instead_of_nesting_one(self):
        with patch.object(cli, "_inside_session", return_value=True), \
                self.assertRaisesRegex(AgentBoxError, "ab-config/SKILL.md"):
            self.run_ai()
        with self.assertRaisesRegex(AgentBoxError, "expected"):
            self.run_ai("gemini")
        self.launch.assert_not_called()

    def test_the_skill_uses_only_config_commands_that_exist(self):
        """A skill telling the agent to run a missing command would stall the session."""
        text = SKILL.read_text()
        commands = set(re.findall(r"agent-box config ([a-z]+)", text))
        self.assertLessEqual({"describe", "set", "proposal"}, commands)
        self.assertLessEqual(commands, {word for word, _ in completion.COMMANDS["config"]([""])[1]})
        self.assertRegex(text, r"(?i)user's language")
        self.assertRegex(text, r"(?i)never ask for, display or write credentials")

    def test_completion_offers_each_subcommands_own_arguments(self):
        config = completion.COMMANDS["config"]
        self.assertEqual({word for word, _ in config(["set", ""])[1]}, {"--project", "--global", "--dry-run", "--reset"})
        self.assertNotIn(("--dry-run", ""), config(["set", "--dry-run", ""])[1])
        self.assertEqual({word for word, _ in config(["proposal", ""])[1]}, {"apply", "discard"})
        self.assertEqual({word for word, _ in config(["ai", ""])[1]}, {"claude", "codex"})
        self.assertIn(("ai", ""), config([""])[1])


if __name__ == "__main__":
    unittest.main()
