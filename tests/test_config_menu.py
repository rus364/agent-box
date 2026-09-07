"""Menu cancellation and secret display are independent of terminal escape codes."""
import unittest
from unittest.mock import patch
from pathlib import Path
from agent_box.config import Config, TrackerConnection
from agent_box import config_menu


class Screen:
    def __init__(self, keys):
        self.keys = iter(keys)
        self.drawn = []
    def getmaxyx(self):
        return 24, 120
    def get_wch(self):
        return next(self.keys)
    def addnstr(self, y, x, text, width, attr):
        self.drawn.append(text)
    def erase(self):
        pass
    def refresh(self):
        pass
    def keypad(self, enabled):
        pass


class ConfigMenuTests(unittest.TestCase):
    def test_cleanup_toggles_save_independently_without_enabling_the_other(self):
        import curses
        fields = list(Config().workflow.to_dict())
        for field in ('cleanup_local_branches', 'cleanup_remote_branches'):
            keys = [*[curses.KEY_DOWN] * 10, '\n', *[curses.KEY_DOWN] * fields.index(field),
                    ' ', '\x1b', curses.KEY_DOWN, curses.KEY_DOWN, '\n']
            result, _ = self.edit(Config(), keys)
            self.assertTrue(getattr(result.config.workflow, field))
            other = 'cleanup_remote_branches' if field == 'cleanup_local_branches' else 'cleanup_local_branches'
            self.assertFalse(getattr(result.config.workflow, other))

    def test_merge_mode_is_an_explicit_menu_choice(self):
        self.assertEqual(config_menu.CHOICES["workflow.merge_mode"], ("github", "local"))

    def edit(self, config, keys, choices=(), link=None):
        import curses
        from agent_box import pathsetup
        screen = Screen(keys)
        state = link or pathsetup.LinkState("ours", Path("/somewhere/bin/agent-box"))
        with patch.object(config_menu.sys.stdin, "isatty", return_value=True), patch.object(config_menu.sys.stdout, "isatty", return_value=True), \
             patch.object(curses, "wrapper", side_effect=lambda callback: callback(screen)), patch.object(curses, "curs_set"), \
             patch.object(pathsetup, "link_state", return_value=state), \
             patch.object(pathsetup, "bin_dir_choices", return_value=list(choices)):
            return config_menu.edit(config), screen

    def test_cancel_discards_draft_while_save_returns_the_selected_values(self):
        import curses
        config = Config()
        keys = ["\n", curses.KEY_DOWN, " ", "\x1b"]
        result, _ = self.edit(config, [*keys, "\x1b"])
        self.assertIsNone(result)
        self.assertTrue(config.claude.statusline)
        result, _ = self.edit(config, [*keys, curses.KEY_UP, curses.KEY_UP, "\n"])
        self.assertFalse(result.config.claude.statusline)
        self.assertTrue(config.claude.statusline)
        self.assertEqual(result.config.install.bin_dir, "")

    def test_saved_token_is_not_drawn_when_opening_its_connection(self):
        import curses
        config = Config()
        config.tracker.connections["vikunja"] = TrackerConnection("https://example.invalid", "secret-token")
        _, screen = self.edit(config, [*[curses.KEY_DOWN]*8, "\n", curses.KEY_DOWN, curses.KEY_DOWN, "\n", "\n", "\x1b", "\x1b", "\x1b", "\x1b"])
        self.assertNotIn("secret-token", "\n".join(screen.drawn))
        self.assertIn("token: <configured>", screen.drawn)

    def test_tier_effort_default_can_be_selected_and_saved_for_both_providers(self):
        import curses
        # Open workflow / tiers / tier 0 / provider, then cycle medium to default.
        prefix = [*[curses.KEY_DOWN] * 10, "\n", *[curses.KEY_DOWN] * 4, "\n", "\n"]
        suffix = ["\x1b", "\x1b", "\x1b", "\x1b", curses.KEY_DOWN, curses.KEY_DOWN, "\n"]
        codex, _ = self.edit(Config(), [*prefix, "\n", curses.KEY_DOWN, *[" "] * 5, *suffix])
        claude, _ = self.edit(Config(), [*prefix, curses.KEY_DOWN, "\n", curses.KEY_DOWN, *[" "] * 4, *suffix])
        self.assertEqual(codex.config.workflow.tiers["0"]["codex"]["effort"], "default")
        self.assertEqual(claude.config.workflow.tiers["0"]["claude"]["effort"], "default")

    def test_review_routing_can_be_saved_without_changing_policy_or_worker_routing(self):
        """The review submenu must persist strict routing independently of review triggers."""
        import curses
        prefix = [*[curses.KEY_DOWN] * 10, "\n", *[curses.KEY_DOWN] * 11, "\n"]
        suffix = ["\x1b", "\x1b", curses.KEY_DOWN, curses.KEY_DOWN, "\n"]
        config = Config()
        cancelled, _ = self.edit(config, [*prefix, curses.KEY_DOWN, curses.KEY_DOWN, " ", *["\x1b"] * 3])
        self.assertIsNone(cancelled)
        self.assertEqual(config.workflow.review.routing, "cross-provider")
        result, _ = self.edit(config, [*prefix, curses.KEY_DOWN, curses.KEY_DOWN, " ", *suffix])
        self.assertEqual(result.config.workflow.review.routing, "same-provider")
        self.assertEqual(result.config.workflow.review.policy, "auto")
        self.assertEqual(result.config.workflow.routing, "current")
        result, _ = self.edit(result.config, [*prefix, curses.KEY_DOWN, curses.KEY_DOWN, " ", *suffix])
        self.assertEqual(result.config.workflow.review.routing, "cross-provider")

    def test_reviewer_effort_menu_uses_provider_choices_without_changing_workers(self):
        import curses
        # workflow / review / tiers / tier 1 / provider
        prefix = [*[curses.KEY_DOWN] * 10, "\n", *[curses.KEY_DOWN] * 11, "\n", "\n", "\n"]
        suffix = [*["\x1b"] * 5, curses.KEY_DOWN, curses.KEY_DOWN, "\n"]
        for provider, selection, cycles in (("codex", [], 5), ("claude", [curses.KEY_DOWN], 4)):
            with self.subTest(provider=provider):
                result, _ = self.edit(Config(), [*prefix, *selection, "\n", curses.KEY_DOWN,
                                                 *[" "] * cycles, *suffix])
                self.assertEqual(result.config.workflow.review.tiers["1"][provider]["effort"], "default")
                self.assertEqual(result.config.workflow.tiers["1"][provider]["effort"], "medium")


class PathIntegrationTests(unittest.TestCase):
    """Setup begins with PATH, so the menu must not let it be missed silently.

    The complaint these protect against: the PATH question sat behind a row
    that said nothing about its state, and every default answer -- the cursor
    on the first row, "leave PATH unchanged" first in its submenu -- was the
    one that left the shell unable to run agent-box.
    """

    def setUp(self):
        from agent_box import pathsetup
        self.missing = pathsetup.LinkState("missing")
        self.bin = pathsetup.BinDir(Path("/home/somebody/.local/bin"), on_path=True, exists=True)

    def edit(self, keys, config=None, choices=(), link=None):
        return ConfigMenuTests.edit(self, config or Config(), keys, choices, link or self.missing)

    def test_menu_opens_on_the_path_row_and_says_agent_box_is_not_on_path(self):
        result, screen = self.edit(["\x1b"], choices=[self.bin])
        self.assertIsNone(result)
        self.assertIn("PATH integration: agent-box is not on PATH yet", screen.drawn)
        self.assertIn("Setup starts here: agent-box is not on PATH yet", screen.drawn)

    def test_saving_without_a_path_answer_asks_before_leaving_the_shell_broken(self):
        import curses
        # Down to "Save and apply"; the gate opens, and Escape declines it,
        # so nothing is saved.
        result, screen = self.edit([curses.KEY_DOWN, "\n", "\x1b", "\x1b"], choices=[self.bin])
        self.assertIsNone(result)
        self.assertIn("Set up PATH now", screen.drawn)
        # Saving anyway stays available: it is the gate's second answer.
        result, _ = self.edit(
            [curses.KEY_DOWN, "\n", curses.KEY_DOWN, "\n"], choices=[self.bin])
        self.assertEqual(result.config.install.bin_dir, "")

    def test_the_path_submenu_starts_on_the_directory_rather_than_on_declining(self):
        import curses
        # The cursor opens on the PATH row, Enter opens its submenu, and the
        # directory -- not "leave PATH alone" -- is what Enter takes there.
        result, _ = self.edit(["\n", "\n", curses.KEY_DOWN, "\n"], choices=[self.bin])
        self.assertEqual(result.config.install.bin_dir, str(self.bin.path))
        self.assertFalse(result.edit_rc)

    def test_a_recorded_directory_is_shown_and_kept_without_reopening_the_menu(self):
        import curses
        config = Config()
        config.install.bin_dir = "/home/somebody/.local/bin"
        result, screen = self.edit(
            [curses.KEY_UP, curses.KEY_UP, "\n"], config=config, choices=[self.bin])
        self.assertEqual(result.config.install.bin_dir, "/home/somebody/.local/bin")
        self.assertIn(
            "PATH integration: agent-box keeps /home/somebody/.local/bin/agent-box",
            screen.drawn)

    def test_a_link_agent_box_did_not_make_is_reported_as_unmaintained(self):
        from agent_box import pathsetup
        ours = pathsetup.LinkState("ours", Path("/usr/local/bin/agent-box"))
        _, screen = self.edit(["\x1b"], choices=[self.bin], link=ours)
        self.assertIn(
            "PATH integration: found /usr/local/bin/agent-box, kept by you, not agent-box",
            screen.drawn)
