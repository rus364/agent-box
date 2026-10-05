"""Putting ``agent-box`` on PATH, and the rules about whose files those are.

Two things here belong to the user and not to agent-box: a directory on their
PATH, and their shell startup file. The tests below are mostly about the
refusals -- a name that is already something else, a block someone has edited,
an rc file that is really a symlink into a dotfiles repository. Getting any of
those wrong silently breaks a login shell, which is the one failure a launcher
must not be able to cause.
"""
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_box import pathsetup
from agent_box.errors import AgentBoxError


class PathEntriesTests(unittest.TestCase):
    def test_it_keeps_order_and_drops_duplicates_and_relative_entries(self):
        """PATH order is precedence, so it has to survive the round trip."""
        entries = pathsetup.path_entries("/a:/b:/a::relative:/c")
        self.assertEqual(entries, [Path("/a"), Path("/b"), Path("/c")])

    def test_it_reads_the_real_environment_by_default(self):
        """The question is what the user's shell resolves, which only PATH says."""
        with mock.patch.dict(os.environ, {"PATH": "/only"}):
            self.assertEqual(pathsetup.path_entries(), [Path("/only")])


class BinDirChoiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.default = self.home / ".local/bin"
        self.patches = mock.patch.multiple(
            pathsetup, HOST_HOME=self.home, DEFAULT_BIN_DIR=self.default)
        self.patches.start()
        self.addCleanup(self.patches.stop)

    def _choices(self, path: str):
        with mock.patch.dict(os.environ, {"PATH": path}):
            return pathsetup.bin_dir_choices()

    def test_a_writable_path_directory_in_the_home_is_offered_first(self):
        """Choosing one of these is the whole job: no shell file to edit."""
        mine = self.home / "bin"
        mine.mkdir()
        choices = self._choices(f"/usr/bin:{mine}")
        self.assertEqual(choices[0].path, mine)
        self.assertTrue(choices[0].on_path)
        self.assertFalse(choices[0].needs_rc_line)

    def test_directories_outside_the_home_are_never_offered(self):
        """/usr/local/bin would need root, which a launcher has no business asking for."""
        choices = self._choices("/usr/local/bin:/usr/bin")
        self.assertEqual([choice.path for choice in choices], [self.default])

    def test_the_default_directory_is_offered_last_and_flagged(self):
        """The fallback costs a directory and an rc line, so it must not look free."""
        mine = self.home / "bin"
        mine.mkdir()
        choices = self._choices(str(mine))
        self.assertEqual(choices[-1].path, self.default)
        self.assertTrue(choices[-1].needs_rc_line)
        self.assertFalse(choices[-1].exists)

    def test_the_default_is_not_offered_twice_when_it_is_already_on_path(self):
        self.default.mkdir(parents=True)
        choices = self._choices(str(self.default))
        self.assertEqual([choice.path for choice in choices], [self.default])
        self.assertTrue(choices[0].on_path)

    def test_the_actions_name_every_change_including_the_directory(self):
        """"Create a symlink" must not quietly also mean "and edit your .bashrc"."""
        steps = " ".join(pathsetup.BinDir(self.default, on_path=False, exists=False).actions())
        self.assertIn(f"create the directory {self.default}", steps)
        self.assertIn("create the symlink", steps)
        self.assertIn("PATH in your shell startup file", steps)


class SymlinkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.checkout = self.root / "checkout"
        (self.checkout / "agent_box").mkdir(parents=True)
        (self.checkout / "agent_box/__init__.py").write_text("")
        (self.checkout / "agent-box").write_text("#!/usr/bin/env python3\n")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        patch = mock.patch.object(pathsetup, "checkout_root", return_value=self.checkout)
        patch.start()
        self.addCleanup(patch.stop)

    def test_it_links_the_shim_and_is_idempotent(self):
        pathsetup.install_symlink(self.bin)
        first = (self.bin / "agent-box").resolve()
        report = pathsetup.install_symlink(self.bin)
        self.assertEqual((self.bin / "agent-box").resolve(), first)
        self.assertIn("already points", report)

    def test_it_creates_the_directory_it_was_given(self):
        target = self.root / "made/up"
        pathsetup.install_symlink(target)
        self.assertTrue((target / "agent-box").is_symlink())

    def test_a_real_file_of_the_same_name_is_never_replaced(self):
        """Whatever that file is, it is the user's, and it got there first."""
        (self.bin / "agent-box").write_text("someone else's script\n")
        with self.assertRaises(AgentBoxError):
            pathsetup.install_symlink(self.bin)
        self.assertEqual((self.bin / "agent-box").read_text(), "someone else's script\n")

    def test_a_symlink_to_something_that_is_not_agent_box_is_never_replaced(self):
        other = self.root / "other-tool"
        other.write_text("")
        (self.bin / "agent-box").symlink_to(other)
        with self.assertRaises(AgentBoxError):
            pathsetup.install_symlink(self.bin)
        self.assertEqual((self.bin / "agent-box").resolve(), other)

    def test_a_link_to_another_agent_box_checkout_is_repointed(self):
        """Two clones is normal; the one you ran config from is the one you meant."""
        older = self.root / "older"
        (older / "agent_box").mkdir(parents=True)
        (older / "agent_box/__init__.py").write_text("")
        (older / "agent-box").write_text("")
        (self.bin / "agent-box").symlink_to(older / "agent-box")
        pathsetup.install_symlink(self.bin)
        self.assertEqual((self.bin / "agent-box").resolve(), self.checkout / "agent-box")

    def test_link_state_tells_this_checkout_from_another_and_from_a_stranger(self):
        with mock.patch.object(pathsetup.shutil, "which", return_value=None):
            self.assertEqual(pathsetup.link_state().kind, "missing")
        pathsetup.install_symlink(self.bin)
        with mock.patch.object(
                pathsetup.shutil, "which", return_value=str(self.bin / "agent-box")):
            self.assertEqual(pathsetup.link_state().kind, "ours")
        stranger = self.root / "stranger"
        stranger.write_text("")
        with mock.patch.object(pathsetup.shutil, "which", return_value=str(stranger)):
            self.assertEqual(pathsetup.link_state().kind, "foreign")

    def test_uninstall_finds_our_links_and_leaves_everything_else(self):
        pathsetup.install_symlink(self.bin)
        stranger = self.bin / "other-tool"
        stranger.write_text("")
        with mock.patch.object(pathsetup, "path_entries", return_value=[self.bin]), \
             mock.patch.object(pathsetup, "DEFAULT_BIN_DIR", self.bin):
            removed = pathsetup.remove_symlinks()
        self.assertEqual(removed, [self.bin / "agent-box"])
        self.assertTrue(stranger.exists())

    def test_a_link_left_dangling_by_a_deleted_checkout_is_still_ours(self):
        """The dangling command is exactly what uninstall is meant to clean up."""
        gone = self.root / "gone/agent-box"
        (self.bin / "agent-box").symlink_to(gone)
        with mock.patch.object(pathsetup, "path_entries", return_value=[self.bin]), \
             mock.patch.object(pathsetup, "DEFAULT_BIN_DIR", self.bin):
            self.assertEqual(pathsetup.remove_symlinks(), [self.bin / "agent-box"])


class RcBlockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.bin = self.home / ".local/bin"
        self.rc = self.home / ".bashrc"
        patch = mock.patch.object(pathsetup, "HOST_HOME", self.home)
        patch.start()
        self.addCleanup(patch.stop)

    def test_the_block_uses_home_relative_form_and_guards_itself(self):
        """It sits next to the user's own PATH lines, so it should read like one."""
        block = pathsetup.rc_block(self.bin)
        self.assertIn('export PATH="${HOME}/.local/bin:${PATH}"', block)
        self.assertIn('== *":${HOME}/.local/bin:"*', block)
        self.assertTrue(block.startswith(pathsetup.RC_BEGIN))

    def test_appending_then_reading_back_recognises_our_own_block(self):
        self.rc.write_text("export EDITOR=vim\n")
        pathsetup.append_rc_block(self.rc, self.bin)
        self.assertEqual(pathsetup.rc_state(self.rc).kind, "exact")
        self.assertIn("export EDITOR=vim", self.rc.read_text())

    def test_appending_twice_changes_nothing(self):
        pathsetup.append_rc_block(self.rc, self.bin)
        first = self.rc.read_text()
        pathsetup.append_rc_block(self.rc, self.bin)
        self.assertEqual(self.rc.read_text(), first)

    def test_it_preserves_the_files_permissions(self):
        """mkstemp makes 0600 files; a .bashrc that silently became private is a surprise."""
        self.rc.write_text("# rc\n")
        self.rc.chmod(0o644)
        pathsetup.append_rc_block(self.rc, self.bin)
        self.assertEqual(stat.S_IMODE(self.rc.stat().st_mode), 0o644)

    def test_it_writes_through_a_symlinked_rc_into_the_real_file(self):
        """Dotfiles are usually symlinks; os.replace at the link would detach them."""
        real = self.home / "dotfiles/bashrc"
        real.parent.mkdir()
        real.write_text("# managed\n")
        self.rc.symlink_to(real)
        pathsetup.append_rc_block(self.rc, self.bin)
        self.assertTrue(self.rc.is_symlink())
        self.assertIn(pathsetup.RC_BEGIN, real.read_text())

    def test_it_refuses_a_hard_linked_rc_file(self):
        """os.replace would break the other link without saying so."""
        self.rc.write_text("# rc\n")
        os.link(self.rc, self.home / "second-name")
        with self.assertRaises(AgentBoxError):
            pathsetup.append_rc_block(self.rc, self.bin)
        self.assertEqual(self.rc.read_text(), "# rc\n")

    def test_removal_takes_our_block_and_the_blank_line_it_added(self):
        self.rc.write_text("export EDITOR=vim\n")
        pathsetup.append_rc_block(self.rc, self.bin)
        removed, _ = pathsetup.remove_rc_block(self.rc)
        self.assertTrue(removed)
        self.assertEqual(self.rc.read_text(), "export EDITOR=vim\n")

    def test_removal_recognises_a_block_written_for_another_directory(self):
        """uninstall runs long after the directory was chosen; it cannot guess it."""
        pathsetup.append_rc_block(self.rc, self.home / "bin")
        removed, _ = pathsetup.remove_rc_block(self.rc)
        self.assertTrue(removed)
        self.assertNotIn(pathsetup.RC_BEGIN, self.rc.read_text())

    def test_a_block_someone_added_a_line_to_is_left_alone_and_reported(self):
        """The extra line is the user's now, and it cannot be removed separately."""
        pathsetup.append_rc_block(self.rc, self.bin)
        text = self.rc.read_text().replace(
            pathsetup.RC_END, "export EXTRA=1\n" + pathsetup.RC_END)
        self.rc.write_text(text)
        removed, note = pathsetup.remove_rc_block(self.rc)
        self.assertFalse(removed)
        self.assertIn("edited", note)
        self.assertEqual(self.rc.read_text(), text)

    def test_a_block_whose_guard_no_longer_matches_its_export_is_left_alone(self):
        """Half-edited means hand-edited: the two directories must still agree."""
        pathsetup.append_rc_block(self.rc, self.bin)
        text = self.rc.read_text().replace(
            'export PATH="${HOME}/.local/bin', 'export PATH="${HOME}/bin')
        self.rc.write_text(text)
        removed, note = pathsetup.remove_rc_block(self.rc)
        self.assertFalse(removed)
        self.assertIn("edited", note)

    def test_a_one_line_block_is_not_mistaken_for_ours(self):
        """The case that started this rule: two lines became one."""
        self.rc.write_text(f"{pathsetup.RC_BEGIN}\n{pathsetup.RC_END}\n")
        removed, note = pathsetup.remove_rc_block(self.rc)
        self.assertFalse(removed)
        self.assertIn("edited", note)

    def test_a_doubled_block_is_left_alone_and_reported(self):
        block = pathsetup.rc_block(self.bin)
        self.rc.write_text(block + "\n" + block)
        removed, note = pathsetup.remove_rc_block(self.rc)
        self.assertFalse(removed)
        self.assertIn("more than once", note)

    def test_an_unpaired_marker_is_left_alone_and_reported(self):
        self.rc.write_text(f"{pathsetup.RC_END}\nsomething\n{pathsetup.RC_BEGIN}\n")
        removed, note = pathsetup.remove_rc_block(self.rc)
        self.assertFalse(removed)
        self.assertIn("unpaired", note)

    def test_appending_over_a_foreign_block_refuses_rather_than_stacking(self):
        """Two agent-box blocks would leave neither removable later."""
        self.rc.write_text(f"{pathsetup.RC_BEGIN}\nexport PATH=/something\n{pathsetup.RC_END}\n")
        with self.assertRaises(AgentBoxError):
            pathsetup.append_rc_block(self.rc, self.bin)

    def test_a_missing_rc_file_reads_as_absent(self):
        self.assertEqual(pathsetup.rc_state(self.home / "nope").kind, "absent")


class ShellDetectionTests(unittest.TestCase):
    def test_the_startup_file_comes_from_the_login_shell(self):
        """$SHELL describes whatever spawned us, not the shell the user logs in with."""
        for shell, expected in (("bash", ".bashrc"), ("zsh", ".zshrc")):
            with mock.patch.object(pathsetup, "login_shell", return_value=shell):
                self.assertEqual(pathsetup.rc_file().name, expected)

    def test_an_unknown_shell_gets_no_guess(self):
        """A bash PATH line in config.fish breaks the shell instead of extending it."""
        with mock.patch.object(pathsetup, "login_shell", return_value="fish"):
            self.assertIsNone(pathsetup.rc_file())


class RecordedSymlinkTests(unittest.TestCase):
    """The recorded directory is what makes a vanished symlink recoverable.

    A ``~/.local/bin`` rebuilt by another installer, or a home restored from
    backup, takes the link with it. Without the directory in the
    configuration the only cure is finding the PATH question in the menu
    again; with it, ``agent-box config apply`` is the cure.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bin = Path(self.tmp.name) / "bin"
        self.bin.mkdir()

    def apply(self, bin_dir):
        from agent_box import runtime
        from agent_box.config import Config
        config = Config()
        config.host.bin_dir = str(bin_dir)
        return runtime.apply_path_symlink(config)

    def test_applying_restores_a_link_that_something_else_removed(self):
        self.apply(self.bin)
        (self.bin / "agent-box").unlink()
        self.apply(self.bin)
        self.assertEqual(
            (self.bin / "agent-box").resolve(), pathsetup.shim_path().resolve())

    def test_no_recorded_directory_means_no_symlink_is_invented(self):
        from agent_box import runtime
        from agent_box.config import Config
        self.assertEqual(runtime.apply_path_symlink(Config()), [])

    def test_a_refusal_is_reported_and_does_not_abort_the_rest_of_the_apply(self):
        (self.bin / "agent-box").write_text("someone else's script\n")
        report = self.apply(self.bin)
        self.assertIn("warning:", report[0])
        self.assertEqual((self.bin / "agent-box").read_text(), "someone else's script\n")


if __name__ == "__main__":
    unittest.main()
