"""Native profile pins and local edits survive the apply/remove lifecycle."""
import tempfile
import unittest
from pathlib import Path
import tomllib
from unittest.mock import patch
from agent_box.agent_profiles import available, sync
from agent_box.config import Config


class AgentProfileTests(unittest.TestCase):
    def test_empty_reviewer_models_never_install_worker_models_as_reviewers(self):
        with tempfile.TemporaryDirectory() as directory:
            codex, claude = Path(directory) / "codex", Path(directory) / "claude"
            config = Config()
            config.workflow.enabled = True
            for provider in ("codex", "claude"):
                config.workflow.tiers["2"][provider]["model"] = "worker-model"
                config.workflow.review.tiers["2"][provider] = {"model": "reviewer-model", "effort": "default"}
            sync(config, codex, claude)
            self.assertNotIn("model_reasoning_effort", tomllib.loads((codex / "agents/ab-reviewer-2.toml").read_text()))
            self.assertNotIn("effort:", (claude / "agents/ab-reviewer-2.md").read_text())
            for provider in ("codex", "claude"):
                config.workflow.review.tiers["2"][provider]["model"] = ""
            sync(config, codex, claude)
            for home, suffix in ((codex, "toml"), (claude, "md")):
                self.assertFalse((home / f"agents/ab-reviewer-2.{suffix}").exists())
                self.assertTrue((home / f"agents/ab-worker-2.{suffix}").exists())

    def test_exact_models_are_installed_and_locally_edited_profiles_survive_disable(self):
        with tempfile.TemporaryDirectory() as directory:
            codex, claude = Path(directory) / "codex", Path(directory) / "claude"
            config = Config()
            config.workflow.enabled = True
            config.workflow.tiers["1"]["codex"] = {"model": "pinned-model", "effort": "high"}
            config.workflow.tiers["1"]["claude"] = {"model": "other-model", "effort": "medium"}
            config.workflow.review.tiers["1"]["codex"] = {"model": "review-model", "effort": "low"}
            config.workflow.review.tiers["1"]["claude"] = {"model": "review-claude", "effort": "high"}
            self.assertEqual(sync(config, codex, claude), [])
            profile = codex / "agents/ab-reviewer-1.toml"
            data = tomllib.loads(profile.read_text())
            self.assertEqual((data["model"], data["model_reasoning_effort"], data["sandbox_mode"]), ("review-model", "low", "read-only"))
            worker = tomllib.loads((codex / "agents/ab-worker-1.toml").read_text())
            self.assertEqual(worker["model"], "pinned-model")
            self.assertIn('model: "review-claude"', (claude / "agents/ab-reviewer-1.md").read_text())
            self.assertTrue((claude / "agents/ab-worker-1.md").read_text().startswith("---\n"))
            profile.write_text(profile.read_text() + "# user customization\n")
            config.workflow.enabled = False
            self.assertTrue(sync(config, codex, claude))
            self.assertTrue(profile.read_text().endswith("# user customization\n"))
            self.assertFalse((claude / "agents/ab-worker-1.md").exists())

    def test_default_effort_is_omitted_and_native_readiness_is_conservative(self):
        with tempfile.TemporaryDirectory() as directory:
            codex, claude = Path(directory) / "codex", Path(directory) / "claude"
            config = Config()
            config.workflow.enabled = True
            spec = {"model": "chosen-model", "effort": "default"}
            config.workflow.tiers["1"]["codex"] = spec
            config.workflow.tiers["1"]["claude"] = spec
            self.assertEqual(sync(config, codex, claude), [])
            codex_profile = codex / "agents/ab-worker-1.toml"
            claude_profile = claude / "agents/ab-worker-1.md"
            self.assertNotIn("model_reasoning_effort", tomllib.loads(codex_profile.read_text()))
            self.assertNotIn("effort:", claude_profile.read_text())
            with patch("agent_box.paths.CODEX_HOME", codex), \
                 patch("agent_box.paths.CLAUDE_HOME", claude):
                self.assertFalse(available("codex", "worker", 1, spec))
                self.assertFalse(available("claude", "worker", 1, spec))
