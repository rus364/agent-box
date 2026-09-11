"""Menu cancellation and secret display are independent of terminal escape codes."""
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from agent_box.config import Config, TrackerConnection
from agent_box import config_menu
from agent_box.model_catalog import BUNDLED_MODELS


def select(key, path=()):
    """Navigate by field identity so behavior tests survive presentation changes."""
    import curses
    data = Config().to_dict()
    for part in path:
        data = data[part]
    keys = config_menu.menu_keys(data, path)
    return [curses.KEY_HOME, *[curses.KEY_DOWN] * keys.index(key)]


class Screen:
    def __init__(self, keys, sizes=None):
        self.keys = iter(keys)
        self.drawn = []
        self.sizes = iter(sizes or [])
        self.size = (24, 120)
        self.cursor = []
        self.frames = []
        self.writes = []
        self.frame_writes = []
        self.attributes = []
    def getmaxyx(self):
        self.size = next(self.sizes, self.size)
        return self.size
    def get_wch(self):
        return next(self.keys)
    def addnstr(self, y, x, text, width, attr):
        self.drawn.append(text)
        self.writes.append((y, x, text[:width], attr))
        self.frame_writes.append((y, x, text[:width], attr))
        while len(self.frame) <= y:
            self.frame.append(" " * self.size[1])
        visible = text[:width]
        line = self.frame[y]
        self.frame[y] = line[:x] + visible + line[x + len(visible):]
    def erase(self):
        self.frame_writes = []
        self.frame = [" " * self.size[1] for _ in range(self.size[0])]
    def refresh(self):
        self.frames.append("\n".join(self.frame))
        self.attributes.append(self.frame_writes.copy())
    def keypad(self, enabled):
        pass
    def move(self, y, x):
        self.cursor.append((y, x))


class ConfigMenuTests(unittest.TestCase):
    def test_fast_toggle_is_draft_safe_and_independent_for_each_role(self):
        """Saving one speed choice must preserve effort and other candidates."""
        import curses
        for path, model in ((('workflow', 'tiers', '0', 'codex'), 'gpt-6-astra'),
                            (('workflow', 'review', 'tiers', '2', 'claude'), 'claude-opus-5')):
            config = Config()
            matrix = config.workflow.tiers if path[1] == 'tiers' else config.workflow.review.tiers
            matrix[path[-2]][path[-1]]['model'] = model
            keys = []
            for index, key in enumerate(path):
                keys += [*select(key, path[:index]), '\n']
            keys += [*select('fast', path), ' ']
            cancelled, _ = self.edit(config, [*keys, *['\x1b'] * (len(path) + 1)])
            self.assertIsNone(cancelled)
            result, _ = self.edit(config, [*keys, *['\x1b'] * len(path), curses.KEY_END, curses.KEY_UP, '\n'])
            data = result.config.to_dict()
            for key in path:
                data = data[key]
            self.assertEqual(data, {'model': model, 'effort': 'medium', 'fast': True})
            self.assertFalse(matrix[path[-2]][path[-1]]['fast'])

    def test_purpose_order_does_not_follow_configuration_serialization(self):
        """Routing, review and delivery stay together even if storage order changes."""
        data = Config().to_dict()
        data.pop('version')
        data.pop('install')
        expected = {
            (): ['workflow', 'tracker', 'image', 'browser', 'env', 'providers',
                 'github', 'git', 'claude', 'skills', 'notifications'],
            ('workflow',): ['enabled', 'worker', 'tiers', 'routing', 'weights', 'fallback',
                           'reserve_percent', 'smoothing_hours', 'review', 'merge_mode',
                           'auto_push', 'cleanup_local_branches', 'cleanup_remote_branches',
                           'scout_limit', 'timeout_seconds'],
            ('workflow', 'worker'): ['execution', 'override'],
            ('workflow', 'review'): ['policy', 'max_files', 'max_lines', 'sensitive_paths',
                                    'label', 'routing', 'override', 'tiers', 'max_rounds',
                                    'github_provider', 'github_model', 'github_effort', 'wait_seconds'],
        }
        for path, keys in expected.items():
            section = data
            for part in path:
                section = section[part]
            with self.subTest(path=path):
                self.assertEqual(config_menu.menu_keys(dict(reversed(section.items())), path), keys)
                self.assertCountEqual(keys, section)

    def test_long_labels_keep_value_columns_and_full_selected_help_at_supported_widths(self):
        """A long override label must neither shift its value nor disappear from help."""
        keys = [*select('workflow'), '\n', *select('worker', ('workflow',)), '\n',
                *select('override', ('workflow', 'worker')), '\n',
                '?', '\x1b', *['\x1b'] * 4]
        for size in ((8, 40), (24, 80), (24, 100), (40, 160)):
            with self.subTest(size=size):
                screen = Screen(keys, sizes=[size])
                self.edit(Config(), [], screen=screen)
                rows = [text for _, x, text, _ in screen.writes if x == 0 and ':' in text]
                override_rows = [row for row in rows if row.startswith('Model override')]
                self.assertTrue(override_rows)
                for frame, writes in zip(screen.frames, screen.attributes):
                    if 'Models' not in frame or 'Model override' not in frame:
                        continue
                    columns = [text.index(':') for _, x, text, _ in writes
                               if x == 0 and text.startswith(('Model override', 'Models')) and ':' in text]
                    self.assertEqual(len(set(columns)), 1)
                full_help = next(frame for frame in screen.frames if 'Help: workflow.worker' in frame)
                self.assertIn('Model override scope', full_help)
                if size[1] == 40:
                    self.assertTrue(any('...:' in row for row in override_rows))

    def test_editor_field_has_default_background_cursor_and_overflow_markers(self):
        """Input stays legible at 40 columns, with both hidden ends and no reverse fill."""
        import curses
        config = Config()
        config.notifications.telegram_chat_id = 'a' * 90
        keys = [*select('notifications'), '\n', curses.KEY_DOWN, curses.KEY_DOWN, '\n',
                curses.KEY_HOME, *[curses.KEY_RIGHT] * 35, curses.KEY_END, 'Z', '\n',
                '\x1b', curses.KEY_END, curses.KEY_UP, '\n']
        screen = Screen(keys, sizes=[(8, 40)])
        result, _ = self.edit(config, [], screen=screen)
        self.assertEqual(result.config.notifications.telegram_chat_id, 'a' * 90 + 'Z')
        editors = [(frame, attrs) for frame, attrs in zip(screen.frames, screen.attributes)
                   if frame.startswith('Edit ')]
        self.assertTrue(editors)
        self.assertTrue(any('Value: [<' in frame and '>]' in frame for frame, _ in editors))
        for frame, attrs in editors:
            self.assertIn('Value: [', frame)
            self.assertIn('Position ', frame)
            self.assertIn('Esc cancel', frame)
            self.assertTrue(all(not attr & (curses.A_REVERSE | curses.A_COLOR) for _, _, _, attr in attrs))
        self.assertTrue(all(y == 3 and 9 <= x < 37 for y, x in screen.cursor))

    def test_unicode_values_are_clipped_before_the_terminal_or_sidebar_boundary(self):
        """Wide characters use two columns; combining marks stay with their base."""
        keys = [*select('workflow'), '\n', *select('review', ('workflow',)), '\n',
                *select('label', ('workflow', 'review')), *['\x1b'] * 3]
        for width, label_columns, value_columns in ((40, 18, 19), (100, 23, 24)):
            for character, columns in (('界', 2), ('e\u0301', 1)):
                with self.subTest(width=width, character=character):
                    config = Config()
                    config.workflow.review.label = character * 30
                    screen = Screen(keys, sizes=[(24, width)])
                    self.edit(config, [], screen=screen)
                    rows = [text for _, x, text, _ in screen.writes
                            if x == 0 and text.startswith('Label') and ':' in text]
                    expected = 'Label'.ljust(label_columns) + ': ' + character * ((value_columns - 3) // columns) + '...'
                    self.assertTrue(rows)
                    self.assertTrue(all(text == expected for text in rows))

    def test_unicode_editor_scroll_and_cursor_use_displayed_columns(self):
        """CJK scrolls by cells, while a combining accent never adds a cursor column."""
        import curses
        cases = (
            ('界' * 20, '界' * 13 + '  ', [35, 9, 11, 13, 35], True),
            ('e\u0301' * 20, 'e\u0301' * 20 + ' ' * 8, [29, 9, 10, 10, 29], False),
            ('e\u0301' * 40, 'e\u0301' * 27 + ' ', [36, 9, 10, 10, 36], True),
        )
        keys = [*select('notifications'), '\n', curses.KEY_DOWN, curses.KEY_DOWN, '\n',
                curses.KEY_HOME, curses.KEY_RIGHT, curses.KEY_RIGHT, curses.KEY_END, '\n',
                '\x1b', curses.KEY_END, curses.KEY_UP, '\n']
        for value, first_field, cursor_columns, overflow in cases:
            with self.subTest(value=value):
                config = Config()
                config.notifications.telegram_chat_id = value
                screen = Screen(keys, sizes=[(8, 40)])
                result, _ = self.edit(config, [], screen=screen)
                self.assertEqual(result.config.notifications.telegram_chat_id, value)
                fields = [text for y, x, text, _ in screen.writes if y == 3 and x == 9]
                self.assertEqual(fields[0], first_field)
                self.assertTrue(all(not text.startswith('\u0301') for text in fields))
                self.assertEqual(screen.cursor, [(3, column) for column in cursor_columns])
                left = [text for y, x, text, _ in screen.writes if y == 3 and x == 0 and text.startswith('Value: [')]
                right = [text for y, x, text, _ in screen.writes if y == 3 and x == 37]
                self.assertEqual(left[0], 'Value: [<' if overflow else 'Value: [ ')
                self.assertEqual(right[0], ' ]')
                self.assertEqual(right[1], '>]' if overflow else ' ]')

    def test_global_field_and_section_reset_restore_program_defaults_and_sparse_save(self):
        """Reset removes stored global choices, without changing the input draft on cancel."""
        import curses
        from agent_box.config import save_config
        config = Config()
        config.browser.enabled = True
        config.workflow.auto_push = False
        config.workflow.review.max_lines = 31
        browser = [*select("browser"), '\n', 'r', '\x1b']
        cancelled, _ = self.edit(config, [*browser, '\x1b'])
        self.assertIsNone(cancelled)
        self.assertTrue(config.browser.enabled)
        result, screen = self.edit(config, [*browser, *select("workflow"), 'r',
                                           curses.KEY_END, curses.KEY_UP, '\n'])
        self.assertEqual(result.config.to_dict(), Config().to_dict())
        self.assertIn('Reset to default', '\n'.join(screen.drawn))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            save_config(result.config, path)
            self.assertEqual(json.loads(path.read_text()), {'version': 1})

    def test_global_reviewer_candidates_use_picker_effort_and_draft_controls(self):
        """Both override candidates share the model editor without altering tier settings."""
        import curses
        config = Config()
        prefix = [*select("workflow"), "\n",
                  *select("review", ("workflow",)), "\n",
                  *select("override", ("workflow", "review")), "\n",
                  " ", " ", curses.KEY_DOWN, "\n"]
        for index, provider in enumerate(("codex", "claude")):
            cases = [([curses.KEY_HOME, "\n", *"manual-reviewer", "\n"], "manual-reviewer"),
                     ([curses.KEY_END, "\n"], BUNDLED_MODELS[provider][-1]),
                     ([curses.KEY_HOME, curses.KEY_DOWN, "\n"], "")]
            for pick, expected in cases:
                with self.subTest(provider=provider, expected=expected):
                    edit_keys = [*prefix, *[curses.KEY_DOWN] * index, "\n", "\n", *pick,
                                 curses.KEY_DOWN, *[" "] * (5 if provider == "codex" else 4)]
                    cancelled, _ = self.edit(config, [*edit_keys, *["\x1b"] * 6])
                    self.assertIsNone(cancelled)
                    result, _ = self.edit(config, [*edit_keys, *["\x1b"] * 5,
                                                   curses.KEY_END, curses.KEY_UP, "\n"])
                    override = result.config.workflow.review.override
                    self.assertEqual(override.scope, "all")
                    self.assertEqual(override.models[provider], {"fast": False, "model": expected, "effort": "default"})
                    self.assertEqual(result.config.workflow.review.tiers, config.workflow.review.tiers)
                    self.assertEqual(result.config.workflow.tiers, config.workflow.tiers)
        self.assertEqual(config.workflow.review.override.scope, "off")

    def test_global_worker_candidates_use_picker_effort_and_draft_controls(self):
        """Both override candidates share the model editor without altering tier settings."""
        import curses
        config = Config()
        prefix = [*select("workflow"), "\n",
                  *select("worker", ("workflow",)), "\n",
                  *select("override", ("workflow", "worker")), "\n",
                  " ", " ", curses.KEY_DOWN, "\n"]
        for index, provider in enumerate(("codex", "claude")):
            cases = [([curses.KEY_HOME, "\n", *"manual-worker", "\n"], "manual-worker"),
                     ([curses.KEY_END, "\n"], BUNDLED_MODELS[provider][-1]),
                     ([curses.KEY_HOME, curses.KEY_DOWN, "\n"], "")]
            for pick, expected in cases:
                with self.subTest(provider=provider, expected=expected):
                    edit_keys = [*prefix, *[curses.KEY_DOWN] * index, "\n", "\n", *pick,
                                 curses.KEY_DOWN, *[" "] * (5 if provider == "codex" else 4)]
                    cancelled, _ = self.edit(config, [*edit_keys, *["\x1b"] * 6])
                    self.assertIsNone(cancelled)
                    result, screen = self.edit(config, [*edit_keys, *["\x1b"] * 5,
                                                   curses.KEY_END, curses.KEY_UP, "\n"])
                    self.assertIn("Global worker override", "\n".join(screen.drawn))
                    self.assertEqual(result.config.workflow.review, config.workflow.review)
                    override = result.config.workflow.worker.override
                    self.assertEqual(override.scope, "all")
                    self.assertEqual(override.models[provider], {"fast": False, "model": expected, "effort": "default"})
                    self.assertEqual(result.config.workflow.review.tiers, config.workflow.review.tiers)
                    self.assertEqual(result.config.workflow.tiers, config.workflow.tiers)
        self.assertEqual(config.workflow.worker.override.scope, "off")

    def test_cleanup_toggles_save_independently_without_enabling_the_other(self):
        import curses
        for field in ('cleanup_local_branches', 'cleanup_remote_branches'):
            keys = [*select("workflow"), '\n', *select(field, ("workflow",)),
                    ' ', '\x1b', curses.KEY_END, curses.KEY_UP, '\n']
            result, _ = self.edit(Config(), keys)
            self.assertTrue(getattr(result.config.workflow, field))
            other = 'cleanup_remote_branches' if field == 'cleanup_local_branches' else 'cleanup_local_branches'
            self.assertFalse(getattr(result.config.workflow, other))

    def test_merge_mode_is_an_explicit_menu_choice(self):
        self.assertEqual(config_menu.CHOICES["workflow.merge_mode"], ("github", "local"))

    def test_worker_execution_choices_save_globally_and_as_project_overrides(self):
        """Execution can be chosen independently while model scope remains inherited."""
        import curses
        for project in (None, {}):
            for steps, execution in enumerate(("auto", "direct", "delegated")):
                keys = [*select("workflow"), '\n', *select("worker", ("workflow",)), '\n',
                        *select("execution", ("workflow", "worker")), *[' '] * (steps or 3),
                        '\x1b', '\x1b', curses.KEY_END, curses.KEY_UP, '\n']
                with self.subTest(project=project, execution=execution):
                    result, _ = self.edit(Config(), keys, project=project)
                    self.assertEqual(result.config.workflow.worker.execution, execution)
                    self.assertEqual(result.config.workflow.worker.override.scope, "off")
                    if project is not None:
                        self.assertEqual(result.project, {"workflow": {"worker": {"execution": execution}}})

    def test_direct_marks_only_delegated_worker_consumers_inapplicable(self):
        """Direct bypasses worker routing, while shared settings remain meaningful to scouts."""
        config = Config()
        config.workflow.worker.execution = "direct"
        keys = [*select("workflow"), "\n", *select("worker", ("workflow",)), "\n",
                "\x1b", "\x1b", "\x1b"]
        _, screen = self.edit(config, keys, screen=Screen(keys, sizes=[(40, 160)]))
        rendered = "\n".join(screen.frames)
        self.assertIn("Global worker override", rendered)
        self.assertIn("[Inapplicable to direct workers]", rendered)
        self.assertIn("[Scouts only; direct workers inapplicable]", rendered)

        config.workflow.worker.execution = "auto"
        _, screen = self.edit(config, keys, screen=Screen(keys, sizes=[(40, 160)]))
        self.assertNotIn("direct workers inapplicable", "\n".join(screen.frames))

    def edit(self, config, keys, choices=(), link=None, screen=None, project=None, catalogs=None):
        import curses
        from agent_box import pathsetup
        screen = screen or Screen(keys)
        state = link or pathsetup.LinkState("ours", Path("/somewhere/bin/agent-box"))
        with patch.object(config_menu.sys.stdin, "isatty", return_value=True), patch.object(config_menu.sys.stdout, "isatty", return_value=True), \
             patch.object(curses, "wrapper", side_effect=lambda callback: callback(screen)), patch.object(curses, "curs_set"), \
             patch.object(config_menu, "load_model_catalogs", return_value=catalogs or BUNDLED_MODELS) as refresh, \
             patch.object(pathsetup, "link_state", return_value=state), \
             patch.object(pathsetup, "bin_dir_choices", return_value=list(choices)):
            result = config_menu.edit(config, project=project)
            refresh.assert_called_once_with()
            return result, screen

    def test_cancel_discards_draft_while_save_returns_the_selected_values(self):
        import curses
        config = Config()
        keys = [*select("claude"), "\n", curses.KEY_DOWN, " ", "\x1b"]
        result, _ = self.edit(config, [*keys, "\x1b"])
        self.assertIsNone(result)
        self.assertTrue(config.claude.statusline)
        result, _ = self.edit(config, [*keys, curses.KEY_END, curses.KEY_UP, "\n"])
        self.assertFalse(result.config.claude.statusline)
        self.assertTrue(config.claude.statusline)
        self.assertEqual(result.config.install.bin_dir, "")

    def test_saved_token_is_not_drawn_when_opening_its_connection(self):
        import curses
        config = Config()
        config.tracker.connections["vikunja"] = TrackerConnection("https://example.invalid", "secret-token")
        _, screen = self.edit(config, [*select("tracker"), "\n", curses.KEY_DOWN, curses.KEY_DOWN, "\n", "\n", "\x1b", "\x1b", "\x1b", "\x1b"])
        self.assertNotIn("secret-token", "\n".join(screen.drawn))
        self.assertIn("Access token: <configured>", screen.drawn)

    def test_tier_effort_default_can_be_selected_and_saved_for_both_providers(self):
        import curses
        # Open workflow / tiers / tier 0 / provider, then cycle medium to default.
        prefix = [*select("workflow"), "\n", *select("tiers", ("workflow",)), "\n", "\n"]
        suffix = ["\x1b", "\x1b", "\x1b", "\x1b", curses.KEY_END, curses.KEY_UP, "\n"]
        codex, _ = self.edit(Config(), [*prefix, "\n", curses.KEY_DOWN, *[" "] * 5, *suffix])
        claude, _ = self.edit(Config(), [*prefix, curses.KEY_DOWN, "\n", curses.KEY_DOWN, *[" "] * 4, *suffix])
        self.assertEqual(codex.config.workflow.tiers["0"]["codex"]["effort"], "default")
        self.assertEqual(claude.config.workflow.tiers["0"]["claude"]["effort"], "default")

    def test_review_routing_can_be_saved_without_changing_policy_or_worker_routing(self):
        """The review submenu must persist strict routing independently of review triggers."""
        import curses
        prefix = [*select("workflow"), "\n", *select("review", ("workflow",)), "\n"]
        suffix = ["\x1b", "\x1b", curses.KEY_END, curses.KEY_UP, "\n"]
        config = Config()
        cancelled, _ = self.edit(config, [*prefix, *select("routing", ("workflow", "review")), " ", *["\x1b"] * 3])
        self.assertIsNone(cancelled)
        self.assertEqual(config.workflow.review.routing, "cross-provider")
        result, _ = self.edit(config, [*prefix, *select("routing", ("workflow", "review")), " ", *suffix])
        self.assertEqual(result.config.workflow.review.routing, "same-provider")
        self.assertEqual(result.config.workflow.review.policy, "auto")
        self.assertEqual(result.config.workflow.routing, "current")
        result, _ = self.edit(result.config, [*prefix, *select("routing", ("workflow", "review")), " ", *suffix])
        self.assertEqual(result.config.workflow.review.routing, "cross-provider")

    def test_reviewer_effort_menu_uses_provider_choices_without_changing_workers(self):
        import curses
        # workflow / review / tiers / tier 1 / provider
        prefix = [*select("workflow"), "\n", *select("review", ("workflow",)), "\n", *select("tiers", ("workflow", "review")), "\n", "\n"]
        suffix = [*["\x1b"] * 5, curses.KEY_END, curses.KEY_UP, "\n"]
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

    def test_editor_inserts_and_deletes_at_the_visible_cursor(self):
        import curses
        config = Config()
        config.notifications.telegram_chat_id = "abcd"
        keys = [*select("notifications"), "\n", curses.KEY_DOWN, curses.KEY_DOWN, "\n",
                curses.KEY_HOME, "X", curses.KEY_RIGHT, curses.KEY_DC,
                curses.KEY_END, curses.KEY_LEFT, curses.KEY_BACKSPACE, "\n",
                "\x1b", curses.KEY_END, curses.KEY_UP, "\n"]
        result, screen = self.edit(config, keys)
        self.assertEqual(result.config.notifications.telegram_chat_id, "Xad")
        self.assertIn((3, len("Value: [ ")), screen.cursor)

    def test_secret_editor_masks_the_value_and_escape_restores_it(self):
        import curses
        config = Config()
        config.notifications.telegram_token = "do-not-draw-this"
        result, screen = self.edit(config, [*select("notifications"), "\n", curses.KEY_DOWN,
                                            "\n", curses.KEY_LEFT, "X", "\x1b",
                                            "\x1b", "\x1b"])
        self.assertIsNone(result)
        self.assertNotIn("do-not-draw-this", "\n".join(screen.drawn))
        self.assertEqual(config.notifications.telegram_token, "do-not-draw-this")

    def test_model_picker_preserves_unknown_value_and_accepts_custom_or_disable(self):
        import curses
        config = Config()
        config.workflow.tiers["0"]["codex"]["model"] = "private-model"
        prefix = [*select("workflow"), "\n", *select("tiers", ("workflow",)),
                  "\n", "\n", "\n", "\n"]
        finish = [*['\x1b'] * 4, curses.KEY_END, curses.KEY_UP, "\n"]
        kept, screen = self.edit(config, [*prefix, "\n", *finish])
        self.assertEqual(kept.config.workflow.tiers["0"]["codex"]["model"], "private-model")
        self.assertIn("private-model (current)", screen.drawn)

        custom, _ = self.edit(config, [*prefix, curses.KEY_HOME,
                                       "\n", "\x15", *"manual-model", "\n", *finish])
        self.assertEqual(custom.config.workflow.tiers["0"]["codex"]["model"], "manual-model")

        disabled, _ = self.edit(config, [*prefix, curses.KEY_HOME, curses.KEY_DOWN,
                                         "\n", *finish])
        self.assertEqual(disabled.config.workflow.tiers["0"]["codex"]["model"], "")

    def test_catalogs_refresh_once_per_global_or_project_entry_not_per_picker(self):
        """Both providers keep unavailable selections across repeated picker visits."""
        import curses
        catalogs = {"codex": ("new-codex",), "claude": ("new-claude",)}
        for project in (None, {}):
            for provider_index, provider in enumerate(catalogs):
                config = Config()
                config.workflow.tiers["0"][provider]["model"] = "private-model"
                prefix = [*select("workflow"), "\n", *select("tiers", ("workflow",)),
                          "\n", "\n", *[curses.KEY_DOWN] * provider_index, "\n", "\n"]
                keys = [*prefix, "\x1b", "\n", "\n", *["\x1b"] * 5]
                result, screen = self.edit(config, keys, project=project, catalogs=catalogs)
                self.assertIsNone(result)
                self.assertIn("private-model (current)", screen.drawn)
                self.assertIn(catalogs[provider][0], screen.drawn)

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
             patch.object(config_menu, "load_model_catalogs", return_value=BUNDLED_MODELS), \
             patch.object(pathsetup, "link_state", return_value=pathsetup.LinkState("ours", Path("/bin/agent-box"))), \
             patch.object(pathsetup, "bin_dir_choices", return_value=[]):
            self.assertIsNone(config_menu.edit(Config()))
        self.assertIn("Resize terminal to at least 40 columns x 8 rows (Esc: back)", screen.drawn)
        self.assertIn("Help: workflow", screen.drawn)

    def test_both_roles_and_providers_offer_suggestions_custom_entry_and_cancel(self):
        """Every model matrix must use the same picker without changing effort or the other role."""
        import curses
        for reviewer in (False, True):
            for provider_index, provider in enumerate(("codex", "claude")):
                config = Config()
                prefix = [*select("workflow"), "\n"]
                if reviewer:
                    prefix += [*select("review", ("workflow",)), "\n", *select("tiers", ("workflow", "review")), "\n"]
                else:
                    prefix += [*select("tiers", ("workflow",)), "\n"]
                prefix += ["\n", *[curses.KEY_DOWN] * provider_index, "\n", "\n"]
                suffix = [*["\x1b"] * (5 if reviewer else 4), curses.KEY_END, curses.KEY_UP, "\n"]
                cases = [(["\n"], BUNDLED_MODELS[provider][0]),
                         ([curses.KEY_HOME, "\n", *"custom/deployment", "\n"], "custom/deployment"),
                         ([curses.KEY_HOME, "\n", *"discarded", "\x1b"], "")]
                for editing, expected in cases:
                    with self.subTest(reviewer=reviewer, provider=provider, expected=expected):
                        result, _ = self.edit(config, [*prefix, *editing, *suffix])
                        matrix = result.config.workflow.review.tiers if reviewer else result.config.workflow.tiers
                        candidate = matrix["1" if reviewer else "0"][provider]
                        self.assertEqual(candidate, {"fast": False, "model": expected, "effort": "medium"})
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
        with patch.dict(config_menu.HELP, {"workflow": "First paragraph. " * 50 + "LAST_SENTENCE"}):
            self.edit(Config(), [], screen=screen)
        self.assertTrue(any("LAST_SENTENCE" in frame for frame in screen.frames))
        self.assertIn("Help: tracker", "\n".join(screen.frames))

    def test_editor_scrolls_long_values_and_preserves_draft_across_resize(self):
        """Editing the visible tail and then shrinking the terminal must retain the full value."""
        import curses
        config = Config()
        config.notifications.telegram_chat_id = "a" * 150
        keys = [*select("notifications"), "\n", curses.KEY_DOWN, curses.KEY_DOWN, "\n",
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


class ProjectMenuTests(unittest.TestCase):
    edit = ConfigMenuTests.edit

    def test_global_only_leaf_stays_visible_and_read_only_at_minimum_width(self):
        """A long label must not hide the scope restriction or expose a global value."""
        import curses
        config = Config()
        config.workflow.enabled = True
        keys = ['\n', ' ', 'r', 'g', '\x1b', curses.KEY_END, curses.KEY_UP, '\n']
        screen = Screen(keys, sizes=[(8, 40)])
        result, _ = self.edit(config, [], screen=screen, project={})
        self.assertEqual(result.project, {})
        self.assertTrue(result.config.workflow.enabled)
        rows = [(text, attr) for _, x, text, attr in screen.writes
                if x == 0 and text.startswith('Enabled') and ':' in text]
        self.assertTrue(rows)
        self.assertTrue(all('[Global only]' in text for text, _ in rows))
        self.assertTrue(all(not attr & curses.A_REVERSE for _, attr in rows))

    def test_worker_override_project_copy_and_reset_preserve_inheritance(self):
        """Project copy pins worker settings; reset restores future global values."""
        import curses
        from agent_box import project_config
        config = Config()
        config.workflow.worker.override.scope = "untiered"
        prefix = [*select("workflow"), '\n',
                  *select("worker", ("workflow",)), '\n',
                  *select("override", ("workflow", "worker"))]
        save = ['\x1b', '\x1b', curses.KEY_END, curses.KEY_UP, '\n']
        pinned, _ = self.edit(config, [*prefix, 'g', *save], project={})
        self.assertEqual(pinned.project['workflow']['worker']['override']['scope'], 'untiered')
        reset, _ = self.edit(config, [*prefix, 'r', *save], project=pinned.project)
        self.assertEqual(reset.project, {})
        config.workflow.worker.override.scope = "off"
        self.assertEqual(project_config.apply(config, pinned.project).workflow.worker.override.scope, "untiered")
        self.assertEqual(project_config.apply(config, reset.project).workflow.worker.override.scope, "off")

    def test_use_global_pins_equal_values_while_reset_follows_later_global_changes(self):
        """Copying global is an explicit choice; reset resumes live inheritance."""
        import curses
        from agent_box import project_config
        config = Config()
        browser = [*select("browser"), '\n']
        save = ['\x1b', curses.KEY_END, curses.KEY_UP, '\n']
        pinned, screen = self.edit(config, [*browser, 'g', *save], project={})
        self.assertEqual(pinned.project, {'browser': {'enabled': False}})
        self.assertIn('copy and pin', '\n'.join(screen.drawn))
        reset, _ = self.edit(config, [*browser, 'r', *save], project=pinned.project)
        self.assertEqual(reset.project, {})
        config.browser.enabled = True
        self.assertFalse(project_config.apply(config, pinned.project).browser.enabled)
        self.assertTrue(project_config.apply(config, reset.project).browser.enabled)

    def test_section_copy_filters_global_only_and_keeps_empty_explicit_values(self):
        """Section pinning must never serialize the global integration permission."""
        import curses
        config = Config()
        config.workflow.enabled = True
        config.workflow.auto_push = False
        config.workflow.review.sensitive_paths = []
        workflow = [*select("workflow")]
        save = [curses.KEY_END, curses.KEY_UP, '\n']
        pinned, _ = self.edit(config, [*workflow, 'g', *save], project={})
        expected = config.workflow.to_dict()
        del expected['enabled']
        self.assertEqual(pinned.project, {'workflow': expected})
        self.assertEqual(pinned.project['workflow']['review']['override']['models']['codex']['model'], '')
        reset, _ = self.edit(config, [*workflow, 'r', *save], project=pinned.project)
        self.assertEqual(reset.project, {})
        self.assertTrue(reset.config.workflow.enabled)

    def test_tracker_section_copy_preserves_identity_without_copying_tokens_or_permission(self):
        """The shared tracker section includes global secrets and project-only identity."""
        import curses
        from agent_box.config import TRACKER_PROVIDERS
        config = Config()
        config.tracker.enabled = True
        config.tracker.connections = {provider: TrackerConnection(f'https://{provider}.example', 'FAKE_SECRET')
                                      for provider in TRACKER_PROVIDERS}
        project = {'tracker': {'project': '12', 'recursive': True}}
        result, screen = self.edit(config, [*select("tracker"), 'g',
                                          curses.KEY_END, curses.KEY_UP, '\n'], project=project)
        self.assertEqual(result.project, {'tracker': {'project': '12', 'recursive': True,
            'provider': 'vikunja', 'connections': {provider: {'url': f'https://{provider}.example'}
                                                 for provider in TRACKER_PROVIDERS}}})
        self.assertNotIn('FAKE_SECRET', '\n'.join(screen.drawn))

    def test_tracker_section_copy_does_not_create_accounts_from_empty_menu_rows(self):
        """A section snapshot copies configured accounts, so unused URL editors cannot block save."""
        import curses
        config = Config()
        for connections in ({}, {'vikunja': TrackerConnection('https://tracker.example', 'FAKE_SECRET')}):
            with self.subTest(configured=bool(connections)):
                config.tracker.connections = connections
                result, _ = self.edit(config, [*select("tracker"), 'g',
                                              curses.KEY_END, curses.KEY_UP, '\n'], project={})
                self.assertEqual(result.project, {'tracker': {'provider': 'vikunja', 'connections': {
                    provider: {'url': connection.url} for provider, connection in connections.items()}}})

    def test_tracker_section_copy_removes_stale_project_account_from_draft_and_file(self):
        """The draft must reflect copied account keys immediately, including removed overrides."""
        import curses
        config = Config()
        config.tracker.connections['vikunja'] = TrackerConnection('https://global.example', 'FAKE_SECRET')
        project = {'tracker': {'connections': {'jira': {'url': 'https://project.example'}}}}
        result, _ = self.edit(config, [*select("tracker"), 'g',
                                      curses.KEY_END, curses.KEY_UP, '\n'], project=project)
        self.assertNotIn('jira', result.project['tracker']['connections'])
        self.assertNotIn('jira', result.config.tracker.connections)
        self.assertEqual(result.config.tracker.connections['vikunja'].url, 'https://global.example')

    def test_connections_copy_replaces_stale_accounts_and_preserves_project_identity(self):
        """Copying the connections map replaces its keys, including an empty global map."""
        import curses
        from agent_box import project_config
        for connections in ({}, {'vikunja': TrackerConnection('https://global.example', 'FAKE_SECRET')}):
            config = Config()
            config.tracker.connections = connections
            project = {'tracker': {'project': '12', 'recursive': True,
                                  'connections': {'jira': {'url': 'https://project.example'}}}}
            result, _ = self.edit(config, [*select("tracker"), '\n', *[curses.KEY_DOWN] * 2, 'g',
                                          '\x1b', curses.KEY_END, curses.KEY_UP, '\n'], project=project)
            expected = {provider: {'url': connection.url} for provider, connection in connections.items()}
            self.assertEqual(result.project, {'tracker': {'project': '12', 'recursive': True,
                                                         'connections': expected}})
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / '.agent-box.json'
                project_config.save(path, result.project)
                reloaded = project_config.apply(config, project_config.load(path))
            self.assertEqual(result.config.to_dict(), reloaded.to_dict())

    def test_project_copy_after_temporary_launch_uses_original_global_urls(self):
        """A real launch snapshot retains editor URLs without retaining the unused global token."""
        import curses
        import os
        from agent_box import project_config, runtime
        from agent_box.config import effective_config
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = Config()
            config.tracker.enabled = True
            config.tracker.connections['vikunja'] = TrackerConnection('https://global.example', 'GLOBAL_SECRET')
            config.env.forward = ['VIKUNJA_URL', 'VIKUNJA_TOKEN']
            project = {'tracker': {'project': '12', 'connections': {'vikunja': {'url': 'https://temporary.example'}}}}
            environment = {'VIKUNJA_URL': 'https://temporary.example', 'VIKUNJA_TOKEN': 'TEMPORARY_SECRET'}
            with patch.dict(os.environ, environment, clear=True), \
                 patch.object(runtime, 'load_config', return_value=config), \
                 patch.object(project_config, 'load', return_value=project), \
                 patch.object(runtime, 'TRACKER_RUNTIME_DIR', root / 'runtime'), \
                 patch.object(runtime, 'container_tracker_url', side_effect=lambda url: url):
                launched = runtime.launch_config()
                state = runtime.WorkspaceRuntimeState(root / 'workspace', root / 'git', root / 'gh')
                snapshot = runtime.session_runtime(launched, runtime_state=state)
            self.assertNotIn('GLOBAL_SECRET', snapshot.read_text())
            with patch.dict(os.environ, {'AGENT_BOX_RUNTIME_CONFIG': str(snapshot)}, clear=True):
                baseline = effective_config(environ={}, include_project=False)
                self.assertEqual(baseline.tracker.connection('vikunja').token, 'TEMPORARY_SECRET')
                self.assertEqual(baseline.tracker.connection('vikunja').url, 'https://temporary.example')
                result, screen = self.edit(baseline, [*select("tracker"), 'g',
                                                    curses.KEY_END, curses.KEY_UP, '\n'], project=project)
            self.assertEqual(result.project['tracker']['connections'], {'vikunja': {'url': 'https://global.example'}})
            self.assertEqual(result.project['tracker']['project'], '12')
            self.assertNotIn('TEMPORARY_SECRET', '\n'.join(screen.drawn))

    def test_project_reset_copy_help_and_exit_controls_fit_minimum_terminal(self):
        """Both distinct project actions remain discoverable at 40 columns."""
        screen = Screen(['\x1b'], sizes=[(8, 40)])
        self.edit(Config(), [], screen=screen, project={})
        visible = screen.frames[-1]
        for control in ('R reset', 'G copy', '? help', 'Esc back'):
            self.assertIn(control, visible)

    def test_identity_copy_is_unavailable_and_reset_removes_only_the_selected_identity(self):
        """Project identity has no global counterpart to invent and pin."""
        import curses
        project = {'tracker': {'project': '12', 'recursive': True}}
        identity = [*select("tracker"), '\n', *[curses.KEY_DOWN] * 3]
        save = ['\x1b', curses.KEY_END, curses.KEY_UP, '\n']
        result, screen = self.edit(Config(), [*identity, 'g', *save], project=project)
        self.assertEqual(result.project, project)
        self.assertIn('Copy current global unavailable', '\n'.join(screen.drawn))
        reset, _ = self.edit(Config(), [*identity, 'r', *save], project=project)
        self.assertEqual(reset.project, {'tracker': {'recursive': True}})

    def test_copy_empty_global_url_requires_correction_before_save(self):
        """A copy action cannot bypass the required project URL validation."""
        import curses
        url = [*select("tracker"), '\n', *[curses.KEY_DOWN] * 2, '\n', '\n']
        save = [*['\x1b'] * 3, curses.KEY_END, curses.KEY_UP, '\n']
        result, screen = self.edit(Config(), [*url, 'g', *save, '\x1b'], project={})
        self.assertIsNone(result)
        self.assertIn('must be an HTTP(S) URL', '\n'.join(screen.drawn))

    def test_new_project_and_cancel_do_not_persist_copies_or_path_actions(self):
        """Opening a new project starts inherited and cancellation drops copied values."""
        import curses
        from agent_box import pathsetup
        with patch.object(pathsetup, 'path_entries', side_effect=AssertionError('host PATH read')):
            result, _ = self.edit(Config(), [curses.KEY_END, curses.KEY_UP, '\n'], project={})
            self.assertEqual(result.project, {})
            project = {}
            cancelled, _ = self.edit(Config(), [*select("workflow"), 'g', '\x1b'], project=project)
            self.assertIsNone(cancelled)
            self.assertEqual(project, {})

    def test_project_can_keep_equal_override_reset_and_block_global_fields(self):
        """Explicit equality is a choice; reset removes the choice from the file."""
        import curses
        config = Config()
        browser = [*select("browser"), '\n']
        save = ['\x1b', curses.KEY_END, curses.KEY_UP, '\n']
        result, screen = self.edit(config, [*browser, ' ', ' ', *save], project={})
        self.assertEqual(result.project, {'browser': {'enabled': False}})
        self.assertIn('Inherited', '\n'.join(screen.drawn))
        self.assertIn('Overridden', '\n'.join(screen.drawn))
        reset, _ = self.edit(config, [*browser, 'r', *save], project=result.project)
        self.assertEqual(reset.project, {})
        blocked, screen = self.edit(config, [*select('claude'), ' ', 'r', 'g', curses.KEY_END, curses.KEY_UP, '\n'], project={})
        self.assertEqual(blocked.project, {})
        self.assertIn('Global only', '\n'.join(screen.drawn))

    def test_project_reviewer_picker_retains_manual_disable_and_default_effort(self):
        import curses
        config = Config()
        prefix = [*select("workflow"), '\n',
                  *select("review", ("workflow",)), '\n',
                  *select("override", ("workflow", "review")), '\n',
                  curses.KEY_DOWN, '\n', '\n', '\n']
        for pick, expected in (([curses.KEY_HOME, '\n', *'custom-model', '\n'], 'custom-model'),
                               ([curses.KEY_HOME, curses.KEY_DOWN, '\n'], '')):
            keys = [*prefix, *pick, curses.KEY_DOWN, *[' '] * 5,
                    *['\x1b'] * 5, curses.KEY_END, curses.KEY_UP, '\n']
            result, _ = self.edit(config, keys, project={})
            self.assertEqual(result.project['workflow']['review']['override']['models']['codex'],
                             {'model': expected, 'effort': 'default'})
            self.assertEqual(result.config.workflow.review.override.scope, "off")

    def test_project_save_and_cancel_do_not_apply_or_configure_path(self):
        from agent_box import cli, project_config
        from contextlib import redirect_stdout
        import io
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.agent-box.json'
            result = config_menu.EditResult(Config(), project={'browser': {'enabled': False}})
            with patch.object(project_config, 'project_path', return_value=path), \
                 patch.object(cli, 'effective_config', return_value=Config()), \
                 patch.object(config_menu, 'edit', return_value=result), \
                 patch.object(cli, 'save_config', side_effect=AssertionError('global write')), \
                 patch('agent_box.runtime.apply_config', side_effect=AssertionError('global apply')), \
                 patch('agent_box.pathsetup.append_rc_block', side_effect=AssertionError('PATH write')), \
                 redirect_stdout(io.StringIO()):
                cli.config_interactive(project=True)
                self.assertEqual(json.loads(path.read_text()), result.project)
                with patch.object(config_menu, 'edit', return_value=None):
                    cli.config_interactive(project=True)
                self.assertEqual(json.loads(path.read_text()), result.project)

    def test_project_cli_copy_uses_available_global_source_without_process_account(self):
        """G must not pin a temporary account, and a session snapshot outranks the host file."""
        import curses
        import io
        import os
        from contextlib import redirect_stdout
        from agent_box import cli, config as config_module, project_config
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / '.agent-box.json'
            global_path = root / 'global.json'
            runtime_path = root / 'runtime.json'
            config = Config()
            config.tracker.connections['vikunja'] = TrackerConnection('https://global.example', 'GLOBAL_SECRET')
            config_module.save_config(config, global_path)
            config.tracker.connections['vikunja'] = TrackerConnection('https://runtime.example', 'RUNTIME_SECRET')
            runtime_path.write_text(json.dumps(config.to_dict()))
            original_global = global_path.read_bytes()
            original_runtime = runtime_path.read_bytes()
            for source, expected in (('host', 'https://global.example'), ('runtime', 'https://runtime.example')):
                environment = {'VIKUNJA_URL': 'https://temporary.example', 'VIKUNJA_TOKEN': 'TEMPORARY_SECRET'}
                if source == 'runtime':
                    environment['AGENT_BOX_RUNTIME_CONFIG'] = str(runtime_path)
                screen = Screen([*select("tracker"), 'g', curses.KEY_END, curses.KEY_UP, '\n'])
                with self.subTest(source=source), redirect_stdout(io.StringIO()), \
                     patch.dict(os.environ, environment, clear=True), \
                     patch.object(config_module, 'CONFIG_FILE', global_path), \
                     patch.object(project_config, 'project_path', return_value=path), \
                     patch.object(config_menu.sys.stdin, 'isatty', return_value=True), \
                     patch.object(config_menu.sys.stdout, 'isatty', return_value=True), \
                     patch.object(curses, 'wrapper', side_effect=lambda callback: callback(screen)), \
                     patch.object(curses, 'curs_set'), \
                     patch.object(config_menu, 'load_model_catalogs', return_value=BUNDLED_MODELS), \
                     patch.object(cli, 'save_config', side_effect=AssertionError('global write')), \
                     patch('agent_box.runtime.apply_config', side_effect=AssertionError('global apply')), \
                     patch('agent_box.pathsetup.append_rc_block', side_effect=AssertionError('PATH write')):
                    cli.config_interactive(project=True)
                    self.assertEqual(json.loads(path.read_text()), {
                        'tracker': {'provider': 'vikunja', 'connections': {'vikunja': {'url': expected}}}})
                    for private in ('https://temporary.example', 'GLOBAL_SECRET', 'RUNTIME_SECRET', 'TEMPORARY_SECRET'):
                        self.assertNotIn(private, path.read_text())
                        self.assertNotIn(private, '\n'.join(screen.drawn))
                    self.assertEqual(global_path.read_bytes(), original_global)
                    self.assertEqual(runtime_path.read_bytes(), original_runtime)
