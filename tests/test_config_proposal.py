"""Global changes leave a box only as a proposal, and reach config.json only with the user's confirmation."""
import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agent_box import cli, completion, config as config_module, config_proposal, runtime
from agent_box.config import Config, load_config, save_config
from agent_box.errors import AgentBoxError

REAL_PROPOSAL_PATH = config_proposal.proposal_path


class ConfigProposalTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.global_file = self.root / "config.json"
        self.proposal = self.root / "git" / "agent-box" / "config-proposal.json"

        def die(message):
            raise AgentBoxError(message)

        self.apply = Mock(return_value=["applied"])
        for patcher in (patch.dict(os.environ, {}, clear=True),
                        patch.object(config_module, "CONFIG_FILE", self.global_file),
                        patch.object(config_proposal, "proposal_path", return_value=self.proposal),
                        patch.object(runtime, "apply_config", self.apply),
                        patch.object(cli, "die", side_effect=die)):
            patcher.start()
            self.addCleanup(patcher.stop)
        config = Config()
        config.workflow.review.max_files = 3
        config.host.notifications.telegram_token = "tg-secret-value"
        save_config(config)

    def command(self, *args, answer=None, tty=True):
        reply = Mock(side_effect=EOFError) if answer is EOFError else Mock(return_value=answer)
        with contextlib.redirect_stdout(io.StringIO()) as output, \
                patch.object(cli.sys, "stdin", Mock(isatty=Mock(return_value=tty))), \
                patch("builtins.input", reply):
            cli.config_command(list(args))
        return output.getvalue()

    def test_a_box_session_saves_only_a_proposal_checked_against_its_snapshot(self):
        """A box has no config.json; creating one there would replace the host's settings with defaults."""
        snapshot = Config()
        snapshot.workflow.enabled = True
        runtime.sync_tracker_runtime(snapshot, self.root / "rendered")
        os.environ["AGENT_BOX_RUNTIME_CONFIG"] = str(self.root / "rendered" / "config.json")
        self.global_file.unlink()
        output = self.command("set", "--global", "workflow.review.max_files=7")
        self.assertIn("+ workflow.review.max_files = 7", output)
        self.assertIn("session's global snapshot", output)
        self.assertFalse(self.global_file.exists())
        self.assertEqual(config_proposal.load(self.proposal), {"set": {"workflow.review.max_files": 7}, "reset": []})
        with self.assertRaisesRegex(AgentBoxError, "outside agent sessions"):
            self.command("proposal", "apply", answer="y")
        self.apply.assert_not_called()

    def test_a_confirmed_proposal_is_saved_sparse_applied_and_removed(self):
        """Consent is the only way in; afterwards the file must hold the change and keep everything else."""
        self.command("set", "--global", "workflow.review.max_files=9", "box.browser.enabled=true")
        self.assertEqual(load_config().workflow.review.max_files, 3, "proposing must not write")
        output = self.command("proposal", "apply", answer="y")
        self.assertIn("- workflow.review.max_files = 3", output)
        self.assertNotIn("tg-secret-value", output)
        saved = json.loads(self.global_file.read_text())
        self.assertEqual((saved["workflow"]["review"]["max_files"], saved["box"]["browser"]["enabled"]), (9, True))
        self.assertEqual(load_config().host.notifications.telegram_token, "tg-secret-value")
        self.apply.assert_called_once()
        self.assertFalse(self.proposal.exists())

    def test_decline_discards_while_no_answer_or_no_terminal_changes_nothing(self):
        """Only an explicit yes writes; an unanswered prompt keeps the proposal for later."""
        before = self.global_file.read_text()
        for kwargs, kept in (({"answer": EOFError}, True), ({"answer": "y", "tty": False}, True), ({"answer": "n"}, False)):
            with self.subTest(kwargs=str(kwargs)):
                self.command("set", "--global", "workflow.review.max_files=9")
                with contextlib.suppress(AgentBoxError):
                    self.command("proposal", "apply", **kwargs)
                self.assertEqual(self.global_file.read_text(), before)
                self.assertEqual(self.proposal.exists(), kept)
        self.apply.assert_not_called()

    def test_credentials_and_invalid_values_are_refused_before_a_proposal_exists(self):
        """An agent must not propose what it may not read, and a bad value must fail where the agent can fix it."""
        for args, message in ((('host.notifications.telegram_token="token-secret-value"',), "credential"),
                              (('box.env.forward=["NAME"]',), "environment"),
                              (('workflow.review.max_files="nine"',), "invalid type"),
                              (('workflow.scout.models=["codex:gpt-5.5"]',), r"models\.0 must be an object"),
                              (('workflow.worker.tiers.2=[{"provider": "codex", "model": 5}]',), r"tiers\.2\.0 must be an object"),
                              (('workflow.review.policy="sometimes"',), "must be one of"),
                              (("workflow.review.nope=1",), "not a setting")):
            with self.subTest(args=args):
                with self.assertRaisesRegex(AgentBoxError, message) as error:
                    self.command("set", "--global", *args)
                self.assertNotIn("secret-value", str(error.exception))
                self.assertFalse(self.proposal.exists())

    def test_the_host_rechecks_a_hand_edited_or_outdated_proposal_without_writing(self):
        """The file may be edited by hand, and global settings may change after the proposal was made."""
        before = self.global_file.read_text()
        self.proposal.parent.mkdir(parents=True)
        self.proposal.write_text(json.dumps({"set": {"host.notifications.telegram_token": "token-secret-value"}, "reset": []}))
        with self.assertRaisesRegex(AgentBoxError, "nothing changed") as error:
            self.command("proposal", "apply", answer="y")
        self.assertNotIn("secret-value", str(error.exception))
        self.proposal.unlink()
        self.command("set", "--global", 'workflow.worker.models=[{"provider": "claude", "api_provider": "deepseek", "model": "v4", "effort": "high"}]')
        config = load_config()
        config.agents.external_apis = config_module.Config.from_dict({"agents": {"external_apis": {"deepseek": {
            "models": {"v4": {"supported_efforts": ["low"]}}, "claude": {"url": "https://api.example/anthropic"}}}}}).agents.external_apis
        save_config(config)
        before = self.global_file.read_text()
        with self.assertRaisesRegex(AgentBoxError, "nothing changed"):
            self.command("proposal", "apply", answer="y")
        self.assertEqual(self.global_file.read_text(), before)
        self.assertTrue(self.proposal.exists())
        self.apply.assert_not_called()

    def test_proposals_belong_to_one_checkout_and_later_changes_replace_overlapping_ones(self):
        """Two workspaces must not see or overwrite each other's proposal; a repeated setting keeps the latest."""
        repos = [self.root / name for name in ("a", "b")]
        for repo in repos:
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
        paths = [REAL_PROPOSAL_PATH(repo) for repo in repos]
        for repo, path in zip(repos, paths):
            self.assertIn(repo.resolve() / ".git", path.resolve().parents)
        config_proposal.save(paths[0], {"set": {"box.browser.enabled": True}, "reset": []})
        self.assertEqual(config_proposal.load(paths[1]), {"set": {}, "reset": []})
        self.assertEqual(config_proposal.merged({"set": {"workflow.review.max_files": 9, "box.browser.enabled": True}, "reset": []},
                                                [(("workflow", "review", "max_files"), 4)], [("box", "browser", "enabled")]),
                         {"set": {"workflow.review.max_files": 4}, "reset": ["box.browser.enabled"]})

    def test_apply_is_refused_inside_any_agent_session(self):
        """An agent must not answer the confirmation itself, in a box or in a --host session with a saved file."""
        snapshot = Config()
        snapshot.workflow.enabled = True
        runtime.sync_tracker_runtime(snapshot, self.root / "rendered")
        self.command("set", "--global", "workflow.review.max_files=9")
        os.environ["AGENT_BOX_RUNTIME_CONFIG"] = str(self.root / "rendered" / "config.json")
        self.assertTrue(self.global_file.is_file())
        with self.assertRaisesRegex(AgentBoxError, "outside agent sessions"):
            self.command("proposal", "apply", answer="y")
        self.apply.assert_not_called()
        self.assertTrue(self.proposal.exists())

    def test_discard_removes_the_proposal_and_changes_nothing(self):
        before = self.global_file.read_text()
        self.command("set", "--global", "workflow.review.max_files=9")
        self.assertIn("Discarded", self.command("proposal", "discard"))
        self.assertFalse(self.proposal.exists())
        self.assertEqual(self.global_file.read_text(), before)
        self.assertIn("No pending global proposal", self.command("proposal", "discard"))

    def test_an_unwritable_git_directory_is_a_clear_refusal(self):
        """A sandboxed agent needs an explanation it can relay, not a traceback."""
        with patch.object(config_proposal.migrations, "write", side_effect=PermissionError(13, "Permission denied")), \
                self.assertRaisesRegex(AgentBoxError, "Cannot write the global proposal"):
            self.command("set", "--global", "workflow.review.max_files=9")

    def test_reset_never_takes_a_flag_for_its_path(self):
        """`--reset --dry-run` reading --dry-run as a path would turn a preview into a write."""
        for scope in ("--project", "--global"):
            for args in (("--reset",), ("--reset", "--dry-run", "box.browser.enabled=true")):
                with self.subTest(scope=scope, args=args), self.assertRaisesRegex(AgentBoxError, "expected"):
                    self.command("set", scope, *args)
        self.assertFalse(self.proposal.exists())

    def test_the_scope_is_explicit_and_the_commands_are_documented(self):
        """A write without a scope must not guess one."""
        for args in (("box.browser.enabled=true",), ("--project", "--global", "box.browser.enabled=true")):
            with self.subTest(args=args), self.assertRaisesRegex(AgentBoxError, "expected"):
                self.command("set", *args)
        self.assertIn("No pending global proposal", self.command("proposal"))
        with contextlib.redirect_stdout(io.StringIO()) as output:
            cli.usage_text()
        self.assertIn("config proposal", output.getvalue())
        self.assertIn(("proposal", ""), completion.COMMANDS["config"]([""])[1])


if __name__ == "__main__":
    unittest.main()
