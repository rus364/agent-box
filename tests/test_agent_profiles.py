"""Native profile pins and local edits survive the apply/remove lifecycle."""
import tempfile
import unittest
from pathlib import Path
import tomllib
from agent_box.agent_profiles import sync
from agent_box.config import Config


class AgentProfileTests(unittest.TestCase):
    def test_exact_models_are_installed_and_locally_edited_profiles_survive_disable(self):
        with tempfile.TemporaryDirectory() as directory:
            codex, claude = Path(directory) / "codex", Path(directory) / "claude"
            config = Config()
            config.workflow.enabled = True
            config.workflow.tiers["1"]["codex"] = {"model": "pinned-model", "effort": "high"}
            config.workflow.tiers["1"]["claude"] = {"model": "other-model", "effort": "medium"}
            self.assertEqual(sync(config, codex, claude), [])
            profile = codex / "agents/ab-reviewer-1.toml"
            data = tomllib.loads(profile.read_text())
            self.assertEqual((data["model"], data["model_reasoning_effort"], data["sandbox_mode"]), ("pinned-model", "high", "read-only"))
            self.assertTrue((claude / "agents/ab-worker-1.md").read_text().startswith("---\n"))
            profile.write_text(profile.read_text() + "# user customization\n")
            config.workflow.enabled = False
            self.assertTrue(sync(config, codex, claude))
            self.assertTrue(profile.read_text().endswith("# user customization\n"))
            self.assertFalse((claude / "agents/ab-worker-1.md").exists())
