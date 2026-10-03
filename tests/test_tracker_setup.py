"""Refreshing managed instructions must not silently narrow a repository's tracker scope."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agent_box import completion, project_config
from agent_box.config import Config, TrackerConnection
from agent_box.errors import AgentBoxError
from agent_box.repo_binding import Binding, block_text, write_setup
from agent_box.tracker import cli


class SetupScopeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / ".agent-box.json"
        self.config = Config()
        self.config.tracker.connections["vikunja"] = TrackerConnection("https://tasks.example", "fixture")
        self.adapter = Mock()
        self.adapter.project.side_effect = lambda reference: {"id": int(reference)}
        self.adapter.lifecycle_report.return_value = []
        for patcher in (patch.object(cli, "effective_config", return_value=self.config),
                        patch.object(cli, "make_adapter", return_value=self.adapter),
                        patch.object(cli, "repository_root", return_value=self.root)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def setup(self, *flags, project="12"):
        return cli.dispatch(cli.parser().parse_args(["setup", "--project", project, *flags]))

    def bind(self, recursive=True, server="https://tasks.example"):
        write_setup(self.root, Binding("vikunja", server, "12", recursive))

    def test_refresh_preserves_recursive_scope_and_overrides_in_preview_and_apply(self):
        """Refreshing an old AGENTS block must keep access to child projects and their tasks."""
        self.bind()
        data = project_config.load(self.path)
        data["workflow"] = {"sync": {"events": ["after"]}}
        project_config.save(self.path, data)
        instructions = block_text("Old workflow instructions\n", "workflow") + "Project rules\n"
        (self.root / "AGENTS.md").write_text(instructions)
        self.assertTrue(self.setup()["binding"]["recursive"])
        self.assertEqual((self.root / "AGENTS.md").read_text(), instructions)
        self.assertTrue(self.setup("--apply")["binding"]["recursive"])
        self.assertTrue(Binding.load(self.root).recursive)
        self.assertEqual(project_config.load(self.path)["workflow"], data["workflow"])
        self.assertIn("ab-sync", (self.root / "AGENTS.md").read_text())
        self.assertTrue((self.root / "AGENTS.md").read_text().endswith("Project rules\n"))

    def test_explicit_flags_can_narrow_and_expand_the_same_binding(self):
        """Preserving omission must still allow the user to change scope deliberately."""
        self.bind()
        self.assertFalse(self.setup("--no-recursive", "--apply")["binding"]["recursive"])
        self.assertFalse(self.setup()["binding"]["recursive"])
        self.assertTrue(self.setup("--recursive", "--apply")["binding"]["recursive"])
        self.assertTrue(Binding.load(self.root).recursive)

    def test_new_or_different_binding_starts_at_the_root_only(self):
        """Recursion belongs to one tracker project, not whichever project setup selects next."""
        self.assertFalse(self.setup()["binding"]["recursive"])
        self.assertFalse(self.path.exists())
        self.bind()
        self.assertFalse(self.setup(project="13")["binding"]["recursive"])
        self.config.tracker.connections["vikunja"].url = "https://other.example"
        self.assertFalse(self.setup()["binding"]["recursive"])
        self.assertTrue(self.setup("--recursive")["binding"]["recursive"])
        self.assertEqual(Binding.load(self.root).server, "https://tasks.example")

    def test_equivalent_server_spelling_preserves_the_same_project_scope(self):
        """A hostname's casing or trailing slash is not a different tracker identity."""
        self.bind(server="https://TASKS.example/")
        self.assertTrue(self.setup()["binding"]["recursive"])

    def test_project_policy_without_a_binding_still_allows_initial_setup(self):
        """An existing project configuration need not already name a tracker."""
        self.path.write_text(json.dumps({"workflow": {"auto_push": False}}))
        self.assertFalse(self.setup("--apply")["binding"]["recursive"])
        self.assertFalse(project_config.load(self.path)["workflow"]["auto_push"])

    def test_invalid_existing_binding_is_not_silently_replaced(self):
        """An unreadable scope is not evidence that the user chose a root-only binding."""
        self.path.write_text('{"tracker":{"project":"12","recursive":true}}')
        before = self.path.read_bytes()
        with self.assertRaisesRegex(AgentBoxError, "Incomplete repository binding"):
            self.setup("--apply")
        self.assertEqual(self.path.read_bytes(), before)

    def test_completion_offers_the_explicit_scope_flags(self):
        """The safe narrowing option must be discoverable through the same parser as setup."""
        _, choices = completion.COMMANDS["tracker"](["setup", "--"])
        self.assertTrue({"--recursive", "--no-recursive"} <= {name for name, _ in choices})
