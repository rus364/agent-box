"""``agent-box upgrade``: moving the checkout, and refusing to when it is not ours.

Installing agent-box is a ``git clone``, so upgrading is a git operation on
that same checkout. The interesting half is the refusals: the command runs
from whatever project the user is standing in, against a directory that may
well be someone's working copy, and the alternative to refusing would be a
stash or a reset -- destroying work to install a newer launcher.

All of this runs against real repositories in a temporary directory, with a
local path as the remote, so the suite still needs no network.
"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_box import runtime, selfupdate
from agent_box.config import Config
from agent_box.errors import AgentBoxError


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@example.com",
         "-c", "commit.gpgsign=false", *args],
        capture_output=True, text=True, check=True)
    return result.stdout.strip()


@unittest.skipUnless(shutil.which("git"), "git is required to upgrade a checkout")
class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.origin = self.root / "origin"
        self.origin.mkdir()
        git(self.origin, "init", "-b", "main", "--quiet")
        self._commit(self.origin, "first")
        self.checkout = self.root / "checkout"
        subprocess.run(["git", "clone", "--quiet", str(self.origin), str(self.checkout)],
                       check=True, capture_output=True)
        # A checkout is only agent-box's if it contains agent-box.
        (self.checkout / "agent_box").mkdir(exist_ok=True)

        # Applying the configuration afterwards is the point of the command,
        # but it writes into the user's real home, so it is stubbed here and
        # asserted on instead.
        self.applied = mock.patch.object(runtime, "apply_config", return_value=[])
        self.apply_config = self.applied.start()
        self.addCleanup(self.applied.stop)
        patches = [
            mock.patch.object(selfupdate, "checkout_root", return_value=self.checkout),
            mock.patch.object(runtime, "image_tool_drift", return_value=([], [])),
            mock.patch("agent_box.config.load_config", return_value=Config()),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _commit(self, repo: Path, message: str) -> str:
        (repo / "file.txt").write_text(f"{message}\n")
        git(repo, "add", "file.txt")
        git(repo, "commit", "--quiet", "-m", message)
        return git(repo, "rev-parse", "HEAD")

    def _head(self) -> str:
        return git(self.checkout, "rev-parse", "HEAD")

    # -- refusals ---------------------------------------------------------

    def test_it_refuses_a_checkout_with_uncommitted_changes(self):
        """The alternative is stashing someone's edits to install a launcher."""
        (self.checkout / "file.txt").write_text("edited\n")
        before = self._head()
        with self.assertRaises(AgentBoxError) as caught:
            selfupdate.upgrade_main([])
        self.assertIn("uncommitted", str(caught.exception))
        self.assertEqual(self._head(), before)
        self.assertEqual((self.checkout / "file.txt").read_text(), "edited\n")

    def test_the_refusal_names_the_changed_files_exactly(self):
        """`git status --porcelain` pads with a leading space that is not a name."""
        (self.checkout / "file.txt").write_text("edited\n")
        (self.checkout / "another.txt").write_text("new\n")
        with self.assertRaises(AgentBoxError) as caught:
            selfupdate.upgrade_main([])
        message = str(caught.exception)
        self.assertIn("  file.txt\n", message + "\n")
        self.assertIn("  another.txt", message)

    def test_it_refuses_a_checkout_with_commits_no_remote_has(self):
        """That is a working copy, not an installation."""
        self._commit(self.origin, "upstream")
        local = self._commit(self.checkout, "mine")
        with self.assertRaises(AgentBoxError) as caught:
            selfupdate.upgrade_main([])
        self.assertIn("no remote has", str(caught.exception))
        self.assertEqual(self._head(), local)

    def test_it_refuses_a_directory_that_is_not_a_git_checkout(self):
        plain = self.root / "plain"
        plain.mkdir()
        with mock.patch.object(selfupdate, "checkout_root", return_value=plain), \
             self.assertRaises(AgentBoxError) as caught:
            selfupdate.upgrade_main([])
        self.assertIn("not a git checkout", str(caught.exception))

    def test_it_refuses_a_checkout_that_is_a_subdirectory_of_a_repository(self):
        """Moving that repository's HEAD would move something else entirely."""
        inner = self.checkout / "inner"
        inner.mkdir()
        with mock.patch.object(selfupdate, "checkout_root", return_value=inner), \
             self.assertRaises(AgentBoxError) as caught:
            selfupdate.upgrade_main([])
        self.assertIn("rather than being one", str(caught.exception))

    def test_it_rejects_unknown_arguments(self):
        with self.assertRaises(AgentBoxError):
            selfupdate.upgrade_main(["--force"])

    # -- moving -----------------------------------------------------------

    def test_without_release_tags_it_fast_forwards_the_tracking_branch(self):
        """Before the first release, following the branch is the sensible default."""
        head = self._commit(self.origin, "second")
        self.assertEqual(selfupdate.upgrade_main([]), 0)
        self.assertEqual(self._head(), head)
        self.assertEqual(git(self.checkout, "symbolic-ref", "--short", "HEAD"), "main")

    def test_it_moves_to_the_newest_release_tag(self):
        """Tags are the releases; git's version ordering decides which is newest."""
        self._commit(self.origin, "second")
        git(self.origin, "tag", "v0.2.0")
        newest = self._commit(self.origin, "third")
        git(self.origin, "tag", "v0.10.0")
        self.assertEqual(selfupdate.upgrade_main([]), 0)
        self.assertEqual(self._head(), newest)

    def test_tags_that_are_not_releases_are_ignored(self):
        """"Newest" is only meaningful over a set with an agreed ordering."""
        head = self._commit(self.origin, "second")
        git(self.origin, "tag", "nightly")
        git(self.origin, "tag", "v1.0.0-rc1")
        self.assertEqual(selfupdate.upgrade_main([]), 0)
        self.assertEqual(self._head(), head, "should have fast-forwarded, not tagged")

    def test_it_applies_the_configuration_afterwards(self):
        """The shipped skills just changed underneath the installed ones."""
        self._commit(self.origin, "second")
        selfupdate.upgrade_main([])
        self.assertTrue(self.apply_config.called)

    def test_an_up_to_date_checkout_changes_nothing(self):
        before = self._head()
        self.assertEqual(selfupdate.upgrade_main([]), 0)
        self.assertEqual(self._head(), before)
        self.assertFalse(self.apply_config.called)

    def test_check_reports_without_moving_anything(self):
        """A command that rewrites your installation should have a dry run."""
        before = self._head()
        self.assertEqual(selfupdate.upgrade_main(["--check"]), 0)
        self.assertEqual(self._head(), before)
        self.assertFalse(self.apply_config.called)


if __name__ == "__main__":
    unittest.main()
