"""``agent-box uninstall``: a way back out.

agent-box reconciles the user's Claude settings, installs skills into a shared
tree and renders credential files. Deleting the program without undoing any of
that leaves a ``statusLine`` pointing at a command that no longer exists, hooks
that fail on every turn, and skill symlinks dangling in a directory Claude
still reads. So the removal has to be a command, and it has to be as careful
about ownership as the installs were: only what agent-box put there.

``--purge`` is the separate, irreversible decision -- it deletes the config
file, which is where the Telegram and Vikunja tokens live.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_box import claude_settings, runtime
from agent_box.config import Config


class UninstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.claude = self.root / "home/.claude"
        self.shared = self.root / "home/.agents/skills"
        self.shipped = self.root / "shipped"
        self.state = self.root / "state"
        self.config_root = self.root / "config"
        self.credentials = self.state / "runtime/vikunja"
        self.notify_credentials = self.state / "runtime/notify.json"
        self.shared.mkdir(parents=True)
        self.shipped.mkdir()
        (self.shipped / "vikunja/scripts").mkdir(parents=True)
        (self.shipped / "vikunja/SKILL.md").write_text("# vikunja\n")
        self.patches = mock.patch.multiple(
            runtime,
            CLAUDE_HOME=self.claude,
            AGENT_SKILLS_DIR=self.shared,
            MANAGED_SKILLS_SOURCE=self.shipped,
            STATE_ROOT=self.state,
            CONFIG_ROOT=self.config_root,
            VIKUNJA_CREDENTIALS_DIR=self.credentials,
            NOTIFY_CREDENTIALS=self.notify_credentials,
        )
        self.patches.start()
        self.addCleanup(self.patches.stop)

    def _installed_state(self):
        """Everything agent-box would have created on a normal install."""
        settings = self.claude / "settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        claude_settings.reconcile_statusline(settings, True)
        enabled = Config()
        enabled.vikunja.enabled = True
        runtime.sync_managed_skills(enabled)
        runtime.claude_skill_links(enabled)
        self.credentials.mkdir(parents=True, exist_ok=True)
        (self.credentials / "token").write_text("tk\n")
        self.notify_credentials.parent.mkdir(parents=True, exist_ok=True)
        self.notify_credentials.write_text("{}")
        self.config_root.mkdir(parents=True, exist_ok=True)
        (self.config_root / "config.json").write_text("{}")
        return settings

    def _uninstall(self, *args):
        with mock.patch.object(runtime.shutil, "which", return_value=None):
            return runtime.uninstall_main(list(args))

    def test_the_status_line_is_removed(self):
        settings = self._installed_state()
        self._uninstall()
        self.assertNotIn("statusLine", json.loads(settings.read_text()))

    def test_a_status_line_someone_else_configured_survives(self):
        settings = self.claude / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"statusLine": {"type": "command", "command": "mine"}}))
        self._uninstall()
        self.assertEqual(json.loads(settings.read_text())["statusLine"]["command"], "mine")

    def test_the_skill_links_are_removed(self):
        self._installed_state()
        self.assertTrue((self.claude / "skills/vikunja").is_symlink())
        self._uninstall()
        self.assertFalse((self.claude / "skills/vikunja").exists())

    def test_the_installed_skills_are_removed(self):
        self._installed_state()
        self.assertTrue((self.shared / "vikunja/SKILL.md").is_file())
        self._uninstall()
        self.assertFalse((self.shared / "vikunja").exists())

    def test_a_skill_the_user_owns_is_left_behind(self):
        """Ownership runs the same direction on the way out as on the way in."""
        mine = self.shared / "handwritten"
        mine.mkdir()
        (mine / "SKILL.md").write_text("# mine\n")
        self._uninstall()
        self.assertTrue((mine / "SKILL.md").is_file())

    def test_the_rendered_credentials_are_removed(self):
        self._installed_state()
        self._uninstall()
        self.assertFalse(self.credentials.exists())
        self.assertFalse(self.notify_credentials.exists())

    def test_config_and_state_survive_without_purge(self):
        """Uninstalling the wiring is not the same as discarding the settings."""
        self._installed_state()
        self._uninstall()
        self.assertTrue((self.config_root / "config.json").is_file())

    def test_purge_deletes_config_and_state_when_confirmed(self):
        self._installed_state()
        self._uninstall("--purge", "--yes")
        self.assertFalse(self.config_root.exists())
        self.assertFalse(self.state.exists())

    def test_purge_asks_first_and_a_refusal_keeps_everything(self):
        self._installed_state()
        with mock.patch("builtins.input", return_value="n"):
            self._uninstall("--purge")
        self.assertTrue((self.config_root / "config.json").is_file())

    def test_purge_accepts_an_explicit_yes_at_the_prompt(self):
        self._installed_state()
        with mock.patch("builtins.input", return_value="y"):
            self._uninstall("--purge")
        self.assertFalse(self.config_root.exists())

    def test_an_interrupted_prompt_keeps_everything(self):
        self._installed_state()
        with mock.patch("builtins.input", side_effect=EOFError):
            self._uninstall("--purge")
        self.assertTrue((self.config_root / "config.json").is_file())

    def test_running_on_a_clean_machine_is_harmless(self):
        self.assertEqual(self._uninstall(), 0)

    def test_an_unknown_argument_is_rejected(self):
        from agent_box.errors import AgentBoxError
        with self.assertRaises(AgentBoxError):
            self._uninstall("--everything")

    def test_every_workspace_box_is_removed(self):
        with mock.patch.object(runtime.shutil, "which", return_value="/usr/bin/podman"), \
             mock.patch.object(runtime, "managed_container_names", return_value=["a", "b"]), \
             mock.patch.object(
                 runtime, "remove_workspace_container_then_confirm_absent") as remove:
            runtime.uninstall_main([])
        self.assertEqual([call.args[0] for call in remove.call_args_list], ["a", "b"])

    def test_the_image_is_never_deleted(self):
        """Large, trivially rebuilt, and not something to remove unasked."""
        with mock.patch.object(runtime.subprocess, "run") as run:
            self._uninstall()
        for call in run.call_args_list:
            self.assertNotIn("rmi", call.args[0])


if __name__ == "__main__":
    unittest.main()
