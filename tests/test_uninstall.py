"""``agent-box uninstall``: a way back out.

agent-box reconciles the user's Claude settings, installs skills into a shared
tree and renders credential files. Deleting the program without undoing any of
that leaves a ``statusLine`` pointing at a command that no longer exists, hooks
that fail on every turn, and skill symlinks dangling in a directory Claude
still reads. So the removal has to be a command, and it has to be as careful
about ownership as the installs were: only what agent-box put there.

``--purge`` is the separate, irreversible decision -- it deletes the config
file, which is where the Telegram and Vikunja tokens live, and only there is
the PATH line in a shell startup file considered for removal at all.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_box import claude_settings, pathsetup, runtime
from agent_box.config import Config


class UninstallTestCase(unittest.TestCase):
    """Shared sandbox: nothing here may touch the developer's real home."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.claude = self.root / "home/.claude"
        self.shared = self.root / "home/.agents/skills"
        self.shipped = self.root / "shipped"
        self.state = self.root / "state"
        self.config_root = self.root / "config"
        self.credentials = self.state / "runtime/tracker"
        self.notify_credentials = self.state / "runtime/notify.json"
        self.shared.mkdir(parents=True)
        self.shipped.mkdir()
        (self.shipped / "ab-tracker/scripts").mkdir(parents=True)
        (self.shipped / "ab-tracker/SKILL.md").write_text("# vikunja\n")
        self.patches = mock.patch.multiple(
            runtime,
            CLAUDE_HOME=self.claude,
            CODEX_HOME=self.root / "home/.codex",
            AGENT_SKILLS_DIR=self.shared,
            MANAGED_SKILLS_SOURCE=self.shipped,
            STATE_ROOT=self.state,
            CONFIG_ROOT=self.config_root,
            TRACKER_RUNTIME_DIR=self.credentials,
            NOTIFY_CREDENTIALS=self.notify_credentials,
        )
        self.patches.start()
        self.addCleanup(self.patches.stop)
        self.checkout = self.root / "checkout"
        (self.checkout / "agent_box").mkdir(parents=True)
        (self.checkout / "agent_box/__init__.py").write_text("")
        (self.checkout / "agent-box").write_text("")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.rc = self.root / ".bashrc"
        # Sandboxed here rather than around each call: uninstall scans PATH and
        # unlinks what it finds, so a test that reaches uninstall_main without
        # these patches deletes the symlink the developer runs the suite with.
        self.path_patches = mock.patch.multiple(
            pathsetup,
            path_entries=lambda raw=None: [self.bin],
            DEFAULT_BIN_DIR=self.bin,
            rc_file=lambda: self.rc,
            checkout_root=lambda: self.checkout,
        )
        self.path_patches.start()
        self.addCleanup(self.path_patches.stop)

    def _on_path(self):
        """The symlink and the rc line a full install would have left."""
        with mock.patch.object(pathsetup, "HOST_HOME", self.root):
            pathsetup.install_symlink(self.bin)
            pathsetup.append_rc_block(self.rc, self.bin)

    def _installed_state(self):
        """Everything agent-box would have created on a normal install."""
        settings = self.claude / "settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        claude_settings.reconcile_statusline(settings, True)
        enabled = Config()
        enabled.tracker.enabled = True
        runtime.sync_managed_skills(enabled)
        runtime.claude_skill_links(enabled)
        self.credentials.mkdir(parents=True, exist_ok=True)
        (self.credentials / "token").write_text("tk\n")
        self.notify_credentials.parent.mkdir(parents=True, exist_ok=True)
        self.notify_credentials.write_text("{}")
        self.config_root.mkdir(parents=True, exist_ok=True)
        (self.config_root / "config.json").write_text("{}")
        return settings

    def _uninstall(self, *args, podman: str | None = None):
        """Run the command with every path it touches pointed at the sandbox.

        ``podman`` is what ``shutil.which`` reports, since finding it is what
        decides whether the workspace boxes are removed at all.
        """
        with mock.patch.object(runtime.shutil, "which", return_value=podman):
            return runtime.uninstall_main(list(args))

class UninstallTests(UninstallTestCase):
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
        self.assertTrue((self.claude / "skills/ab-tracker").is_symlink())
        self._uninstall()
        self.assertFalse((self.claude / "skills/ab-tracker").exists())

    def test_the_installed_skills_are_removed(self):
        self._installed_state()
        self.assertTrue((self.shared / "ab-tracker/SKILL.md").is_file())
        self._uninstall()
        self.assertFalse((self.shared / "ab-tracker").exists())

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
        with mock.patch.object(runtime, "managed_container_names", return_value=["a", "b"]), \
             mock.patch.object(
                 runtime, "remove_workspace_container_then_confirm_absent") as remove:
            self._uninstall(podman="/usr/bin/podman")
        self.assertEqual([call.args[0] for call in remove.call_args_list], ["a", "b"])

    def test_the_image_is_never_deleted(self):
        """Large, trivially rebuilt, and not something to remove unasked."""
        with mock.patch.object(runtime.subprocess, "run") as run:
            self._uninstall()
        for call in run.call_args_list:
            self.assertNotIn("rmi", call.args[0])


class PathRemovalTests(UninstallTestCase):
    """The symlink goes; the shell startup file is a decision, not a cleanup."""

    def test_the_symlink_it_put_on_path_is_removed(self):
        """Otherwise deleting the checkout leaves a dangling command behind."""
        self._on_path()
        self._uninstall()
        self.assertFalse((self.bin / "agent-box").is_symlink())

    def test_the_shell_startup_file_is_untouched_by_default(self):
        """An export PATH line costs nothing to leave; a wrong deletion costs a lot."""
        self._on_path()
        before = self.rc.read_text()
        self._uninstall()
        self.assertEqual(self.rc.read_text(), before)

    def test_purge_removes_a_block_that_still_matches(self):
        self._on_path()
        self._uninstall("--purge", "--yes")
        self.assertNotIn(pathsetup.RC_BEGIN, self.rc.read_text())

    def test_purge_leaves_a_block_someone_edited(self):
        """By then those lines are the user's, whatever the markers say."""
        self._on_path()
        edited = self.rc.read_text().replace(
            pathsetup.RC_END, "export EXTRA=1\n" + pathsetup.RC_END)
        self.rc.write_text(edited)
        self._uninstall("--purge", "--yes")
        self.assertEqual(self.rc.read_text(), edited)


if __name__ == "__main__":
    unittest.main()
