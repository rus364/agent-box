"""Projects lazily adopt owned package guidance without overwriting user instructions."""
import errno
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_box import config, project_config, repo_binding, runtime, upgrades, workflow_behavior
from agent_box.errors import AgentBoxError
from agent_box.workspace import Workspace


class RepositoryGuidanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "AGENTS.md"
        self.settings = config.Config()
        self.settings.workflow.enabled = True
        binding = repo_binding.Binding("github", "https://api.github.com", "fixture/1")
        self.root.joinpath(".agent-box.json").write_text(json.dumps(repo_binding.binding_document(self.root, binding)))
        p = patch.object(upgrades, "in_session", return_value=False)
        p.start()
        self.addCleanup(p.stop)

    def test_old_intact_block_updates_once_preserving_user_bytes_and_permissions(self):
        """A project first opened after global package apply still needs its own refresh."""
        self.path.write_bytes(b"User prefix\n" + repo_binding.block_text("Old package instructions\n", "workflow").encode()
                              + b"\nUser suffix\r\n")
        self.path.chmod(0o640)
        result = upgrades.repository_guidance(self.settings, self.root)
        self.assertEqual(result["status"], "updated")
        self.assertIn("installed package", result["message"])
        data = self.path.read_bytes()
        self.assertTrue(data.startswith(b"User prefix\n"))
        self.assertTrue(data.endswith(b"\nUser suffix\r\n"))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o640)
        with patch.object(repo_binding, "atomic_write_text") as write:
            self.assertEqual(upgrades.repository_guidance(self.settings, self.root)["status"], "current")
        write.assert_not_called()

    def test_missing_block_is_restored_without_rewriting_binding_or_claude(self):
        """Only AGENTS guidance belongs to this lazy upgrade, not all repository setup."""
        self.path.write_text("User instructions\n")
        binding = self.root.joinpath(".agent-box.json").read_bytes()
        result = upgrades.repository_guidance(self.settings, self.root)
        self.assertEqual(result["status"], "restored")
        self.assertTrue(self.path.read_text().endswith("User instructions\n"))
        self.assertEqual(self.root.joinpath(".agent-box.json").read_bytes(), binding)
        self.assertFalse(self.root.joinpath("CLAUDE.md").exists())

    def test_edited_malformed_duplicate_and_symlinked_instructions_are_preserved(self):
        """Package updates cannot decide which manually changed instructions to discard."""
        block = repo_binding.block_text("old\n", "workflow")
        for text in (block.replace("old\n", "user edit\n"), block + block,
                     "<!-- BEGIN AGENT-BOX workflow -->\nIncomplete\n"):
            self.path.write_text(text)
            report = upgrades.repository_guidance(self.settings, self.root)
            self.assertEqual(report["status"], "warning")
            self.assertEqual(self.path.read_text(), text)
        self.path.unlink()
        target = self.root / "outside.md"
        target.write_text("Keep me\n")
        self.path.symlink_to(target)
        self.assertEqual(upgrades.repository_guidance(self.settings, self.root)["status"], "warning")
        self.assertEqual(target.read_text(), "Keep me\n")

    def test_read_only_failure_returns_diagnostic_without_destroying_old_block(self):
        """A launch may continue, but neither user nor agent should assume guidance was refreshed."""
        old = repo_binding.block_text("old\n", "workflow")
        self.path.write_text(old)
        with patch.object(repo_binding, "atomic_write_text", side_effect=OSError(errno.EROFS, "Read-only file system")):
            result = upgrades.repository_guidance(self.settings, self.root)
        self.assertEqual(result["status"], "warning")
        self.assertEqual(self.path.read_text(), old)

    def test_disabled_unbound_and_immutable_sessions_never_refresh(self):
        """Old rendered runtimes must not downgrade a newer host's repository guidance."""
        with patch.object(repo_binding, "refresh_guidance") as refresh:
            self.settings.workflow.enabled = False
            self.assertIsNone(upgrades.repository_guidance(self.settings, self.root))
            self.settings.workflow.enabled = True
            with patch.object(upgrades, "in_session", return_value=True):
                self.assertIsNone(upgrades.repository_guidance(self.settings, self.root))
            self.root.joinpath(".agent-box.json").unlink()
            self.assertIsNone(upgrades.repository_guidance(self.settings, self.root))
        refresh.assert_not_called()

    def test_changed_file_between_read_and_write_is_not_overwritten(self):
        """An editor saving during refresh must not lose its content."""
        original = repo_binding.update_block
        def changed(text, body, name, path):
            self.path.write_text("Concurrent edit\n")
            return original(text, body, name, path)
        with patch.object(repo_binding, "update_block", side_effect=changed):
            with self.assertRaisesRegex(AgentBoxError, "changed during"):
                repo_binding.refresh_guidance(self.root)
        self.assertEqual(self.path.read_text(), "Concurrent edit\n")

    def test_startup_notice_reaches_host_and_container_snapshots_and_policy_discovery(self):
        """A terminal warning alone cannot tell the agent why AGENTS changed."""
        workspace = Workspace(self.root, self.root)
        with patch.object(repo_binding, "repository_root", return_value=self.root), patch.object(runtime, "warn"):
            runtime.prepare_repository_guidance(self.settings, workspace, "codex", [])
        for container in (False, True):
            destination = self.root / str(container)
            runtime.sync_tracker_runtime(self.settings, destination, container=container)
            with patch.dict(os.environ, {"AGENT_BOX_RUNTIME_CONFIG": str(destination / "config.json")}), \
                    patch.object(project_config, "load", return_value={}):
                report = workflow_behavior.describe(config.effective_config(), "coordinator")
            self.assertEqual(report["startup_guidance"]["status"], "restored")
            self.assertNotIn("workflow_guidance", self.settings.to_dict())

    def test_help_doctor_and_shell_do_not_repair_repository_files(self):
        """Read-only management invocations must not turn into implicit setup."""
        with patch.object(upgrades, "repository_guidance") as refresh:
            for agent, rest in (("codex", ["--help"]), ("claude", ["--version"]), ("doctor", []), ("shell", [])):
                runtime.prepare_repository_guidance(self.settings, Workspace(self.root, self.root), agent, rest)
        refresh.assert_not_called()
