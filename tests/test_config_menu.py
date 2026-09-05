"""Menu cancellation and secret display are independent of terminal escape codes."""
import unittest
from unittest.mock import patch
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
    def edit(self, config, keys):
        import curses
        from agent_box import pathsetup
        screen = Screen(keys)
        with patch.object(config_menu.sys.stdin, "isatty", return_value=True), patch.object(config_menu.sys.stdout, "isatty", return_value=True), \
             patch.object(curses, "wrapper", side_effect=lambda callback: callback(screen)), patch.object(curses, "curs_set"), \
             patch.object(pathsetup, "bin_dir_choices", return_value=[]):
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
        self.assertIsNone(result.bin_dir)

    def test_saved_token_is_not_drawn_when_opening_its_connection(self):
        import curses
        config = Config()
        config.tracker.connections["vikunja"] = TrackerConnection("https://example.invalid", "secret-token")
        _, screen = self.edit(config, [*[curses.KEY_DOWN]*8, "\n", curses.KEY_DOWN, curses.KEY_DOWN, "\n", "\n", "\x1b", "\x1b", "\x1b", "\x1b"])
        self.assertNotIn("secret-token", "\n".join(screen.drawn))
        self.assertIn("token: <configured>", screen.drawn)
