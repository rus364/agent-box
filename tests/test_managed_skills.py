"""Skills agent-box ships, installs and owns -- and the ones it must not touch.

An integration the user has to assemble by hand before switching it on is not
an integration. agent-box therefore carries the skills its integrations need
inside the package and installs them into the shared agent tree itself.

That only stays safe because ownership is explicit and one-directional: a
directory agent-box created carries a marker and may be replaced whenever the
shipped copy changes, while a directory it did not create is the user's --
possibly a symlink into a checkout they are working in -- and is never
overwritten to install a convenience.

The second half of the file covers the symlinks into Claude's own skills
directory, which are a switch rather than an assumption because they land in
configuration that Claude sessions outside agent-box also read.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_box import runtime
from agent_box.config import Config


def _skill(root: Path, name: str, body: str = "# skill\n") -> Path:
    directory = root / name
    (directory / "scripts").mkdir(parents=True)
    (directory / "SKILL.md").write_text(body)
    (directory / "scripts" / "run.py").write_text("print('hi')\n")
    return directory


class ManagedSkillInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.shipped = self.root / "shipped"
        self.shipped.mkdir()
        self.installed = self.root / "home/.agents/skills"
        self.patches = mock.patch.multiple(
            runtime,
            MANAGED_SKILLS_SOURCE=self.shipped,
            AGENT_SKILLS_DIR=self.installed,
        )
        self.patches.start()
        self.addCleanup(self.patches.stop)

    def test_a_shipped_skill_is_installed(self):
        _skill(self.shipped, "ab-tracker")
        runtime.install_managed_skill("ab-tracker")
        self.assertTrue((self.installed / "ab-tracker/SKILL.md").is_file())
        self.assertTrue((self.installed / "ab-tracker/scripts/run.py").is_file())

    def test_an_installed_skill_is_marked_as_agent_box_owned(self):
        _skill(self.shipped, "ab-tracker")
        runtime.install_managed_skill("ab-tracker")
        marker = json.loads(
            (self.installed / "ab-tracker" / runtime.MANAGED_SKILL_MARKER).read_text())
        self.assertEqual(marker["skill"], "ab-tracker")
        self.assertEqual(
            marker["digest"], runtime.skill_tree_digest(self.shipped / "ab-tracker"))

    def test_installing_twice_changes_nothing(self):
        _skill(self.shipped, "ab-tracker")
        runtime.install_managed_skill("ab-tracker")
        stamp = (self.installed / "ab-tracker/SKILL.md").stat().st_mtime_ns
        runtime.install_managed_skill("ab-tracker")
        self.assertEqual((self.installed / "ab-tracker/SKILL.md").stat().st_mtime_ns, stamp)

    def test_a_changed_shipped_copy_replaces_the_installed_one(self):
        """Upgrading agent-box has to upgrade the skills it ships."""
        _skill(self.shipped, "ab-tracker", body="# old\n")
        runtime.install_managed_skill("ab-tracker")
        (self.shipped / "ab-tracker/SKILL.md").write_text("# new\n")
        runtime.install_managed_skill("ab-tracker")
        self.assertEqual((self.installed / "ab-tracker/SKILL.md").read_text(), "# new\n")

    def test_a_file_dropped_from_the_shipped_copy_disappears(self):
        _skill(self.shipped, "ab-tracker")
        runtime.install_managed_skill("ab-tracker")
        (self.shipped / "ab-tracker/scripts/run.py").unlink()
        runtime.install_managed_skill("ab-tracker")
        self.assertFalse((self.installed / "ab-tracker/scripts/run.py").exists())

    def test_failed_upgrade_copy_keeps_the_owned_tree_and_can_retry(self):
        """Partial copies cannot become apparently user-owned and prevent recovery."""
        _skill(self.shipped, "ab-tracker", body="# old\n")
        runtime.install_managed_skill("ab-tracker")
        (self.shipped / "ab-tracker/SKILL.md").write_text("# new\n")
        with mock.patch.object(runtime.shutil, "copytree", side_effect=OSError("copy failed")):
            with self.assertRaises(OSError):
                runtime.install_managed_skill("ab-tracker")
        target = self.installed / "ab-tracker"
        self.assertEqual((target / "SKILL.md").read_text(), "# old\n")
        self.assertEqual(runtime.managed_skill_digest(target), runtime.skill_tree_digest(target))
        runtime.install_managed_skill("ab-tracker")
        self.assertEqual((target / "SKILL.md").read_text(), "# new\n")

    def test_an_unmanaged_directory_is_never_overwritten(self):
        """It is the user's -- hand-written, or a checkout they are editing."""
        _skill(self.shipped, "ab-tracker", body="# shipped\n")
        mine = _skill(self.installed, "ab-tracker", body="# mine\n")
        with mock.patch.object(runtime, "warn") as warn:
            runtime.install_managed_skill("ab-tracker")
        self.assertEqual((mine / "SKILL.md").read_text(), "# mine\n")
        self.assertTrue(warn.called)

    def test_partial_old_tree_cleanup_never_breaks_published_skill_or_retry(self):
        """Only retired trees may be partly deleted, never the installed skill path."""
        for remove in (False, True):
            with self.subTest(remove=remove):
                name = "remove" if remove else "replace"
                source = _skill(self.shipped, name, body="# old\n")
                runtime.install_managed_skill(name)
                (source / "SKILL.md").write_text("# new\n")
                target = self.installed / name
                def partial(path, *args, **kwargs):
                    for content in Path(path).rglob("SKILL.md"):
                        content.unlink()
                        break
                    raise OSError("interrupted cleanup")
                operation = runtime.remove_managed_skill if remove else runtime.install_managed_skill
                with mock.patch.object(runtime.shutil, "rmtree", side_effect=partial):
                    with self.assertRaises(OSError):
                        operation(name)
                operation(name)
                if remove:
                    self.assertFalse(target.exists())
                else:
                    self.assertEqual((target / "SKILL.md").read_text(), "# new\n")
                    self.assertEqual(runtime.managed_skill_digest(target), runtime.skill_tree_digest(target))

    def test_failed_publication_after_retiring_old_skill_can_retry(self):
        """A gap between two renames is recoverable without an unmarked live tree."""
        source = _skill(self.shipped, "fixture", body="# old\n")
        runtime.install_managed_skill("fixture")
        (source / "SKILL.md").write_text("# new\n")
        target = self.installed / "fixture"
        replace = runtime.os.replace
        def interrupted(source, destination):
            if destination == target:
                raise OSError("interrupted publication")
            return replace(source, destination)
        with mock.patch.object(runtime.os, "replace", side_effect=interrupted):
            with self.assertRaises(OSError):
                runtime.install_managed_skill("fixture")
        runtime.install_managed_skill("fixture")
        self.assertEqual((target / "SKILL.md").read_text(), "# new\n")

    def test_an_unmanaged_directory_survives_removal_too(self):
        _skill(self.installed, "ab-tracker", body="# mine\n")
        runtime.remove_managed_skill("ab-tracker")
        self.assertTrue((self.installed / "ab-tracker/SKILL.md").is_file())

    def test_a_managed_skill_is_removed_when_asked(self):
        _skill(self.shipped, "ab-tracker")
        runtime.install_managed_skill("ab-tracker")
        runtime.remove_managed_skill("ab-tracker")
        self.assertFalse((self.installed / "ab-tracker").exists())

    def test_a_skill_that_does_not_ship_warns_instead_of_failing(self):
        with mock.patch.object(runtime, "warn") as warn:
            runtime.install_managed_skill("nonesuch")
        self.assertTrue(warn.called)
        self.assertFalse((self.installed / "nonesuch").exists())

    def test_the_digest_ignores_bytecode_caches(self):
        """Otherwise a skill would look changed after its first run."""
        skill = _skill(self.shipped, "ab-tracker")
        before = runtime.skill_tree_digest(skill)
        (skill / "scripts/__pycache__").mkdir()
        (skill / "scripts/__pycache__/run.pyc").write_bytes(b"\x00")
        self.assertEqual(runtime.skill_tree_digest(skill), before)


class ManagedSkillSyncTests(unittest.TestCase):
    """What the configuration asks for is what ends up installed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.shipped = self.root / "shipped"
        self.shipped.mkdir()
        _skill(self.shipped, "ab-tracker")
        self.installed = self.root / "home/.agents/skills"
        self.patches = mock.patch.multiple(
            runtime,
            MANAGED_SKILLS_SOURCE=self.shipped,
            AGENT_SKILLS_DIR=self.installed,
        )
        self.patches.start()
        self.addCleanup(self.patches.stop)

    def _enabled(self):
        config = Config()
        config.tracker.enabled = True
        return config

    def test_an_enabled_integration_installs_its_skill(self):
        runtime.sync_managed_skills(self._enabled())
        self.assertTrue((self.installed / "ab-tracker/SKILL.md").is_file())

    def test_a_disabled_integration_installs_nothing(self):
        runtime.sync_managed_skills(Config())
        self.assertFalse((self.installed / "ab-tracker").exists())

    def test_turning_an_integration_off_removes_its_skill(self):
        runtime.sync_managed_skills(self._enabled())
        runtime.sync_managed_skills(Config())
        self.assertFalse((self.installed / "ab-tracker").exists())

    def test_turning_it_off_leaves_the_users_own_copy_alone(self):
        _skill(self.installed, "ab-tracker", body="# mine\n")
        runtime.sync_managed_skills(Config())
        self.assertEqual((self.installed / "ab-tracker/SKILL.md").read_text(), "# mine\n")


class ClaudeSkillLinkTests(unittest.TestCase):
    """The links into ~/.claude/skills, which outlive any single session."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.shared = self.root / "home/.agents/skills"
        self.shared.mkdir(parents=True)
        self.claude = self.root / "home/.claude"
        self.patches = mock.patch.multiple(
            runtime, AGENT_SKILLS_DIR=self.shared, CLAUDE_HOME=self.claude)
        self.patches.start()
        self.addCleanup(self.patches.stop)

    def _linked(self):
        config = Config()
        config.host.claude_skill_links = True
        return config

    def _unlinked(self):
        config = Config()
        config.host.claude_skill_links = False
        return config

    def test_a_shared_skill_is_linked_for_claude(self):
        skill = _skill(self.shared, "ab-tracker")
        runtime.claude_skill_links(self._linked())
        link = self.claude / "skills/ab-tracker"
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), skill.resolve())

    def test_a_directory_without_a_skill_file_is_not_linked(self):
        (self.shared / "notes").mkdir()
        runtime.claude_skill_links(self._linked())
        self.assertFalse((self.claude / "skills/notes").exists())

    def test_the_switch_off_creates_nothing(self):
        _skill(self.shared, "ab-tracker")
        runtime.claude_skill_links(self._unlinked())
        self.assertFalse((self.claude / "skills/ab-tracker").exists())

    def test_turning_the_switch_off_removes_the_links_it_made(self):
        _skill(self.shared, "ab-tracker")
        runtime.claude_skill_links(self._linked())
        runtime.claude_skill_links(self._unlinked())
        self.assertFalse((self.claude / "skills/ab-tracker").exists())

    def test_turning_it_off_leaves_foreign_entries_alone(self):
        """agent-box removes its own links, not whatever else lives there."""
        elsewhere = _skill(self.root, "handwritten")
        (self.claude / "skills").mkdir(parents=True)
        (self.claude / "skills/handwritten").symlink_to(elsewhere)
        real = self.claude / "skills/real"
        real.mkdir()
        runtime.claude_skill_links(self._unlinked())
        self.assertTrue((self.claude / "skills/handwritten").is_symlink())
        self.assertTrue(real.is_dir())

    def test_an_existing_name_is_kept_with_a_warning(self):
        _skill(self.shared, "ab-tracker")
        (self.claude / "skills").mkdir(parents=True)
        mine = _skill(self.claude / "skills", "ab-tracker", body="# mine\n")
        with mock.patch.object(runtime, "warn") as warn:
            runtime.claude_skill_links(self._linked())
        self.assertEqual((mine / "SKILL.md").read_text(), "# mine\n")
        self.assertTrue(warn.called)

    def test_a_link_left_dangling_by_a_removed_skill_is_cleaned_up(self):
        skill = _skill(self.shared, "ab-tracker")
        runtime.claude_skill_links(self._linked())
        import shutil
        shutil.rmtree(skill)
        runtime.claude_skill_links(self._linked())
        self.assertFalse((self.claude / "skills/ab-tracker").is_symlink())


if __name__ == "__main__":
    unittest.main()
