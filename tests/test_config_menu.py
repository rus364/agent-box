"""Menu cancellation and secret display are independent of terminal escape codes."""
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from agent_box.config import Config, TrackerConnection
from agent_box import config_menu


class Screen:
    def __init__(self, keys, sizes=None):
        self.keys = iter(keys)
        self.drawn = []
        self.sizes = iter(sizes or [])
        self.size = (24, 120)
        self.cursor = []
        self.frames = []
    def getmaxyx(self):
        self.size = next(self.sizes, self.size)
        return self.size
    def get_wch(self):
        return next(self.keys)
    def addnstr(self, y, x, text, width, attr):
        self.drawn.append(text)
        while len(self.frame) <= y:
            self.frame.append(" " * self.size[1])
        visible = text[:width]
        line = self.frame[y]
        self.frame[y] = line[:x] + visible + line[x + len(visible):]
    def erase(self):
        self.frame = [" " * self.size[1] for _ in range(self.size[0])]
    def refresh(self):
        self.frames.append("\n".join(self.frame))
    def keypad(self, enabled):
        pass
    def move(self, y, x):
        self.cursor.append((y, x))


class ConfigMenuTests(unittest.TestCase):
    def test_global_reviewer_candidates_use_picker_effort_and_draft_controls(self):
        """Both override candidates share the model editor without altering tier settings."""
        import curses
        config = Config()
        prefix = [*[curses.KEY_DOWN] * 10, "\n",
                  *[curses.KEY_DOWN] * list(config.workflow.to_dict()).index("review"), "\n",
                  *[curses.KEY_DOWN] * list(config.workflow.review.to_dict()).index("override"), "\n",
                  " ", curses.KEY_DOWN, " ", curses.KEY_DOWN, "\n"]
        for index, provider in enumerate(("codex", "claude")):
            cases = [([curses.KEY_HOME, "\n", *"manual-reviewer", "\n"], "manual-reviewer"),
                     ([curses.KEY_END, "\n"], config_menu.BUNDLED_MODELS[provider][-1]),
                     ([curses.KEY_HOME, curses.KEY_DOWN, "\n"], "")]
            for pick, expected in cases:
                with self.subTest(provider=provider, expected=expected):
                    edit_keys = [*prefix, *[curses.KEY_DOWN] * index, "\n", "\n", *pick,
                                 curses.KEY_DOWN, *[" "] * (5 if provider == "codex" else 4)]
                    cancelled, _ = self.edit(config, [*edit_keys, *["\x1b"] * 6])
                    self.assertIsNone(cancelled)
                    result, _ = self.edit(config, [*edit_keys, *["\x1b"] * 5,
                                                   curses.KEY_DOWN, curses.KEY_DOWN, "\n"])
                    override = result.config.workflow.review.override
                    self.assertTrue(override.enabled)
                    self.assertTrue(override.apply_to_tiered)
                    self.assertEqual(override.models[provider], {"model": expected, "effort": "default"})
                    self.assertEqual(result.config.workflow.review.tiers, config.workflow.review.tiers)
                    self.assertEqual(result.config.workflow.tiers, config.workflow.tiers)
        self.assertFalse(config.workflow.review.override.enabled)

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

    def edit(self, config, keys, choices=(), link=None, screen=None):
        import curses
        from agent_box import pathsetup
        screen = screen or Screen(keys)
        state = link or pathsetup.LinkState("ours", Path("/somewhere/bin/agent-box"))
        with patch.object(config_menu.sys.stdin, "isatty", return_value=True), patch.object(config_menu.sys.stdout, "isatty", return_value=True), \
             patch.object(curses, "wrapper", side_effect=lambda callback: callback(screen)), patch.object(curses, "curs_set"), \
             patch.object(config_menu, "load_codex_models", return_value=config_menu.BUNDLED_MODELS["codex"]), \
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
        self.assertIn("Access token: <configured>", screen.drawn)

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

    def test_every_generated_section_and_field_has_specific_help(self):
        data = Config().to_dict()
        data.pop("version")
        data.pop("install")
        for provider in ("vikunja", "jira", "linear", "github"):
            data["tracker"]["connections"].setdefault(provider, {"url": "", "token": ""})

        def check(value, path=()):
            if not isinstance(value, dict):
                return
            for key, child in value.items():
                full = (*path, key)
                self.assertTrue(config_menu.help_for(full), ".".join(full))
                check(child, full)

        check(data)
        self.assertEqual(set(config_menu.ACTION_HELP), {"path", "save", "cancel", "preset"})

    def test_codex_cache_uses_only_visible_valid_slugs_and_falls_back(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "models_cache.json"
            path.write_text(json.dumps({"models": [
                {"slug": "visible-model", "visibility": "list"},
                {"slug": "hidden-model", "visibility": "hide"},
                {"slug": "bad model", "visibility": "list"},
            ]}))
            self.assertEqual(config_menu.load_codex_models(path), ("visible-model",))
            path.write_text("not json")
            self.assertEqual(config_menu.load_codex_models(path), config_menu.BUNDLED_MODELS["codex"])
            path.write_text('{"models": []}')
            self.assertEqual(config_menu.load_codex_models(path), config_menu.BUNDLED_MODELS["codex"])

    def test_editor_inserts_and_deletes_at_the_visible_cursor(self):
        import curses
        config = Config()
        config.notifications.telegram_chat_id = "abcd"
        keys = [curses.KEY_DOWN, "\n", curses.KEY_DOWN, curses.KEY_DOWN, "\n",
                curses.KEY_HOME, "X", curses.KEY_RIGHT, curses.KEY_DC,
                curses.KEY_END, curses.KEY_LEFT, curses.KEY_BACKSPACE, "\n",
                "\x1b", curses.KEY_UP, curses.KEY_UP, curses.KEY_UP, "\n"]
        result, screen = self.edit(config, keys)
        self.assertEqual(result.config.notifications.telegram_chat_id, "Xad")
        self.assertIn((3, len("Value: ")), screen.cursor)

    def test_secret_editor_masks_the_value_and_escape_restores_it(self):
        import curses
        config = Config()
        config.notifications.telegram_token = "do-not-draw-this"
        result, screen = self.edit(config, [curses.KEY_DOWN, "\n", curses.KEY_DOWN,
                                            "\n", curses.KEY_LEFT, "X", "\x1b",
                                            "\x1b", "\x1b"])
        self.assertIsNone(result)
        self.assertNotIn("do-not-draw-this", "\n".join(screen.drawn))
        self.assertEqual(config.notifications.telegram_token, "do-not-draw-this")

    def test_model_picker_preserves_unknown_value_and_accepts_custom_or_disable(self):
        import curses
        config = Config()
        config.workflow.tiers["0"]["codex"]["model"] = "private-model"
        prefix = [*[curses.KEY_DOWN] * 10, "\n", *[curses.KEY_DOWN] * 4,
                  "\n", "\n", "\n", "\n"]
        finish = [*['\x1b'] * 4, curses.KEY_DOWN, curses.KEY_DOWN, "\n"]
        suggestions = config_menu.BUNDLED_MODELS["codex"]
        with patch.object(config_menu, "load_codex_models", return_value=suggestions):
            kept, _ = self.edit(config, [*prefix, "\x1b", *finish])
            self.assertEqual(kept.config.workflow.tiers["0"]["codex"]["model"], "private-model")

            custom, _ = self.edit(config, [*prefix, curses.KEY_HOME,
                                           "\n", "\x15", *"manual-model", "\n", *finish])
            self.assertEqual(custom.config.workflow.tiers["0"]["codex"]["model"], "manual-model")

            disabled, _ = self.edit(config, [*prefix, curses.KEY_HOME, curses.KEY_DOWN,
                                             "\n", *finish])
            self.assertEqual(disabled.config.workflow.tiers["0"]["codex"]["model"], "")

    def test_default_terminal_colors_and_short_escape_delay_are_restored(self):
        import curses
        with patch.object(curses, "use_default_colors") as colors, \
             patch.object(curses, "get_escdelay", return_value=1000), \
             patch.object(curses, "set_escdelay") as delay:
            result, _ = self.edit(Config(), ["\x1b"])
        self.assertIsNone(result)
        colors.assert_called_once_with()
        self.assertEqual(delay.call_args_list[0].args, (25,))
        self.assertEqual(delay.call_args_list[-1].args, (1000,))

    def test_small_terminal_recovers_after_resize_and_full_help_scrolls(self):
        import curses
        screen = Screen([curses.KEY_RESIZE, "?", curses.KEY_DOWN, "\x1b", "\x1b"],
                        sizes=[(7, 39), (8, 40), (8, 40), (8, 40)])
        from agent_box import pathsetup
        with patch.object(config_menu.sys.stdin, "isatty", return_value=True), \
             patch.object(config_menu.sys.stdout, "isatty", return_value=True), \
             patch.object(curses, "wrapper", side_effect=lambda callback: callback(screen)), \
             patch.object(curses, "curs_set"), \
             patch.object(pathsetup, "link_state", return_value=pathsetup.LinkState("ours", Path("/bin/agent-box"))), \
             patch.object(pathsetup, "bin_dir_choices", return_value=[]):
            self.assertIsNone(config_menu.edit(Config()))
        self.assertIn("Resize terminal to at least 40 columns x 8 rows (Esc: back)", screen.drawn)
        self.assertIn("Help: claude", screen.drawn)

    def test_both_roles_and_providers_offer_suggestions_custom_entry_and_cancel(self):
        """Every model matrix must use the same picker without changing effort or the other role."""
        import curses
        for reviewer in (False, True):
            for provider_index, provider in enumerate(("codex", "claude")):
                config = Config()
                prefix = [*[curses.KEY_DOWN] * 10, "\n"]
                if reviewer:
                    prefix += [*[curses.KEY_DOWN] * 11, "\n", "\n"]
                else:
                    prefix += [*[curses.KEY_DOWN] * 4, "\n"]
                prefix += ["\n", *[curses.KEY_DOWN] * provider_index, "\n", "\n"]
                suffix = [*["\x1b"] * (5 if reviewer else 4), curses.KEY_DOWN, curses.KEY_DOWN, "\n"]
                cases = [(["\n"], config_menu.BUNDLED_MODELS[provider][0]),
                         ([curses.KEY_HOME, "\n", *"custom/deployment", "\n"], "custom/deployment"),
                         ([curses.KEY_HOME, "\n", *"discarded", "\x1b"], "")]
                for editing, expected in cases:
                    with self.subTest(reviewer=reviewer, provider=provider, expected=expected):
                        result, _ = self.edit(config, [*prefix, *editing, *suffix])
                        matrix = result.config.workflow.review.tiers if reviewer else result.config.workflow.tiers
                        candidate = matrix["1" if reviewer else "0"][provider]
                        self.assertEqual(candidate, {"model": expected, "effort": "medium"})
                        other = result.config.workflow.tiers if reviewer else result.config.workflow.review.tiers
                        self.assertTrue(all(not c["model"] for tier in other.values() for c in tier.values()))

    def test_narrow_screen_keeps_help_and_exit_keys_visible(self):
        """A supported 40-column screen must not require guessing an offscreen exit key."""
        screen = Screen(["\x1b"], sizes=[(8, 40)])
        self.edit(Config(), [], screen=screen)
        visible = screen.frames[-1]
        self.assertIn("? help", visible)
        self.assertIn("Esc back", visible)
        self.assertIn("Enter open", visible)

    def test_long_help_reaches_its_end_and_resets_when_another_setting_is_selected(self):
        """Full help must actually scroll, and stale notices must not hide the next field's explanation."""
        import curses
        screen = Screen(["?", curses.KEY_END, "\x1b", curses.KEY_DOWN, "?", "\x1b", "\x1b"], sizes=[(8, 40)])
        with patch.dict(config_menu.HELP, {"claude": "First paragraph. " * 50 + "LAST_SENTENCE"}):
            self.edit(Config(), [], screen=screen)
        self.assertTrue(any("LAST_SENTENCE" in frame for frame in screen.frames))
        self.assertIn("Help: notifications", "\n".join(screen.frames))

    def test_editor_scrolls_long_values_and_preserves_draft_across_resize(self):
        """Editing the visible tail and then shrinking the terminal must retain the full value."""
        import curses
        config = Config()
        config.notifications.telegram_chat_id = "a" * 150
        keys = [curses.KEY_DOWN, "\n", curses.KEY_DOWN, curses.KEY_DOWN, "\n",
                curses.KEY_LEFT, "Z", curses.KEY_RESIZE, curses.KEY_RESIZE,
                curses.KEY_HOME, "Q", curses.KEY_END, "\n", "\x1b",
                curses.KEY_END, curses.KEY_UP, "\n"]
        screen = Screen(keys)
        # Resize on actual editor key events instead of depending on draw-call counts.
        original = screen.get_wch
        sizes = iter([(6, 30), (24, 80)])
        def keypress():
            key = original()
            if key == curses.KEY_RESIZE:
                screen.size = next(sizes)
            return key
        screen.get_wch = keypress
        result, _ = self.edit(config, [], screen=screen)
        self.assertEqual(result.config.notifications.telegram_chat_id, "Q" + "a" * 149 + "Za")
        self.assertTrue(any("Resize terminal" in frame for frame in screen.frames))
        self.assertTrue(all(0 <= x < 120 for _, x in screen.cursor))


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
