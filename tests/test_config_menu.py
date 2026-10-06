"""Menu cancellation and secret display are independent of terminal escape codes."""
import json
import re
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
    def test_purpose_order_does_not_follow_configuration_serialization(self):
        """Routing, review and delivery stay together even if storage order changes."""
        data = Config().to_dict()
        data.pop('version')
        data['host'].pop('bin_dir')
        expected = {
            (): ['agents', 'box', 'credentials', 'tracker', 'workflow', 'host'],
            ('agents',): ['default', 'claude', 'codex', 'host_env', 'external_apis'],
            ('agents', 'codex'): ['native_agent_wait', 'remote_control'],
            ('box',): ['tools', 'k3s', 'audio', 'env'],
            ('credentials',): ['git', 'github', 'registries', 'kubernetes', 'cloud'],
            ('host',): ['notifications', 'claude_skill_links'],
            ('workflow',): ['enabled', 'merge_mode', 'auto_push', 'cleanup_local_branches',
                           'cleanup_remote_branches', 'use_tiers', 'worker', 'scout', 'review', 'sync', 'timeout_seconds'],
            ('workflow', 'worker'): ['models', 'tiers'],
            ('workflow', 'scout'): ['limit', 'models'],
            ('workflow', 'sync'): ['policy', 'events', 'paths', 'scope', 'references', 'models', 'tiers'],
            ('workflow', 'review'): ['policy', 'max_files', 'max_lines', 'sensitive_paths',
                                    'ignored_paths', 'label', 'incremental', 'models', 'tiers', 'max_rounds',
                                    'github_provider', 'github_model', 'github_effort', 'wait_seconds'],
        }
        for path, keys in expected.items():
            section = data
            for part in path:
                section = section[part]
            with self.subTest(path=path):
                self.assertEqual(config_menu.menu_keys(dict(reversed(section.items())), path), keys)
                self.assertEqual(config_menu.menu_keys(section, path), keys)
                self.assertCountEqual(keys, section)

    def test_role_model_settings_read_as_their_purpose(self):
        """Labels name what a role's lists hold rather than the storage key."""
        self.assertEqual(config_menu.label_for("use_tiers", ("workflow",)), "Use tiers")
        for role in ("worker", "scout", "review"):
            self.assertEqual(config_menu.label_for("models", ("workflow", role)), "Models")
        for role in ("worker", "review"):
            self.assertEqual(config_menu.label_for("tiers", ("workflow", role)), "Models by tier")
        self.assertEqual(config_menu.label_for("1", ("workflow", "worker", "tiers")), "Tier 1")

    def test_labels_keep_value_columns_and_full_selected_help_at_supported_widths(self):
        """Labels of different lengths share one value column, and full help stays reachable."""
        keys = [*select('workflow'), '\n', *select('worker', ('workflow',)), '\n',
                *select('tiers', ('workflow', 'worker')), '?', '\x1b', *['\x1b'] * 4]
        for size in ((8, 40), (24, 80), (24, 100), (40, 160)):
            with self.subTest(size=size):
                screen = Screen(keys, sizes=[size])
                self.edit(Config(), [], screen=screen)
                for frame, writes in zip(screen.frames, screen.attributes):
                    if 'Models  ' not in frame or 'Models by tier' not in frame or 'PgUp/PgDn: scroll' in frame:
                        continue
                    columns = [text.index(':') for _, x, text, _ in writes
                               if x == 0 and re.match(r' *(Models|Models by tier) *: ', text)]
                    self.assertEqual(len(set(columns)), 1)
                full_help = next(frame for frame in screen.frames if 'Help: workflow.worker.tiers' in frame)
                self.assertIn('Optional worker', full_help)

    def test_editor_field_has_default_background_cursor_and_overflow_markers(self):
        """Input stays legible at 40 columns, with both hidden ends and no reverse fill."""
        import curses
        config = Config()
        config.host.notifications.telegram_chat_id = 'a' * 90
        keys = [*select('host'), '\n', *select('notifications', ('host',)), '\n', curses.KEY_DOWN, curses.KEY_DOWN, '\n',
                curses.KEY_HOME, *[curses.KEY_RIGHT] * 35, curses.KEY_END, 'Z', '\n',
                '\x1b', '\x1b', curses.KEY_END, curses.KEY_UP, '\n']
        screen = Screen(keys, sizes=[(8, 40)])
        result, _ = self.edit(config, [], screen=screen)
        self.assertEqual(result.config.host.notifications.telegram_chat_id, 'a' * 90 + 'Z')
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
        # Grouped rows sit two columns in, under their heading.
        for width, label_columns, value_columns in ((40, 18, 17), (100, 22, 23)):
            for character, columns in (('界', 2), ('e\u0301', 1)):
                with self.subTest(width=width, character=character):
                    config = Config()
                    config.workflow.review.label = character * 30
                    screen = Screen(keys, sizes=[(24, width)])
                    self.edit(config, [], screen=screen)
                    rows = [text for _, x, text, _ in screen.writes
                            if x == 0 and text.startswith('  Label') and ':' in text]
                    expected = '  ' + 'Label'.ljust(label_columns) + ': ' + character * ((value_columns - 3) // columns) + '...'
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
        keys = [*select('host'), '\n', *select('notifications', ('host',)), '\n', curses.KEY_DOWN, curses.KEY_DOWN, '\n',
                curses.KEY_HOME, curses.KEY_RIGHT, curses.KEY_RIGHT, curses.KEY_END, '\n',
                '\x1b', '\x1b', curses.KEY_END, curses.KEY_UP, '\n']
        for value, first_field, cursor_columns, overflow in cases:
            with self.subTest(value=value):
                config = Config()
                config.host.notifications.telegram_chat_id = value
                screen = Screen(keys, sizes=[(8, 40)])
                result, _ = self.edit(config, [], screen=screen)
                self.assertEqual(result.config.host.notifications.telegram_chat_id, value)
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
        config.box.tools.groups['browser'] = True
        config.workflow.auto_push = False
        config.workflow.review.max_lines = 31
        browser = [*select("box"), '\n', *select("tools", ("box",)), '\n', *select("browser", ("box", "tools")), 'r', '\x1b', '\x1b']
        cancelled, _ = self.edit(config, [*browser, '\x1b'])
        self.assertIsNone(cancelled)
        self.assertTrue(config.box.tools.groups['browser'])
        result, screen = self.edit(config, [*browser, *select("workflow"), 'r',
                                           curses.KEY_END, curses.KEY_UP, '\n'])
        self.assertEqual(result.config.to_dict(), Config().to_dict())
        self.assertIn('Reset to default', '\n'.join(screen.drawn))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            save_config(result.config, path)
            self.assertEqual(json.loads(path.read_text()), {'version': 1})

    def test_resetting_host_keeps_the_path_directory_an_action(self):
        """A section reset restores fields; the PATH directory belongs to its own row and survives."""
        import curses
        config = Config()
        config.host.claude_skill_links = False
        config.host.bin_dir = "/kept/bin"
        result, screen = self.edit(config, [*select("host"), 'r', '\n', '\x1b', curses.KEY_END, curses.KEY_UP, '\n'])
        self.assertTrue(result.config.host.claude_skill_links)
        self.assertEqual(result.config.host.bin_dir, "/kept/bin")
        self.assertFalse(any(line.startswith("Bin dir") for line in screen.drawn))

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

    def test_group_headings_are_drawn_in_the_list_but_never_selected(self):
        """A heading sits above its group; Down from the last field of one group lands on the next group's first field."""
        import curses
        keys = config_menu.menu_keys(Config().to_dict()["workflow"], ("workflow",))
        result, screen = self.edit(Config(), [*select("workflow"), "\n", curses.KEY_HOME, curses.KEY_DOWN, " ",
                                              "\x1b", curses.KEY_END, curses.KEY_UP, "\n"])
        frame = next(frame for frame in screen.frames if "Merge mode" in frame)
        lines = [line.split("|")[0].strip() for line in frame.splitlines()]
        self.assertLess(lines.index("Delivery"), next(i for i, line in enumerate(lines) if line.startswith("Merge mode")))
        self.assertIn(f"1/{len(keys)}", frame)
        self.assertEqual(result.config.workflow.merge_mode, "local")
        # At the supported minimum the list holds one row; its group's heading stays above it.
        screen = Screen([*select("workflow"), "\n", curses.KEY_HOME, curses.KEY_DOWN, curses.KEY_END, "\x1b", "\x1b"],
                        sizes=[(8, 40)])
        self.edit(Config(), [], screen=screen)
        for row, heading in (("Merge mode", "Delivery"), ("Timeout seconds", "Delegation")):
            with self.subTest(row=row):
                frame = next(frame for frame in screen.frames if row in frame)
                self.assertIn(heading, frame)

    def test_every_offered_value_of_a_fixed_choice_has_its_own_description(self):
        """A value without one would fall back to the field's paragraph and read as describing every row."""
        for dotted, options in config_menu.CHOICES.items():
            with self.subTest(field=dotted):
                self.assertEqual(set(config_menu.OPTION_HELP[dotted]), set(options))
        for dotted, options in config_menu.MULTI_CHOICES.items():
            self.assertEqual(set(config_menu.OPTION_HELP[dotted]), set(options))
        from agent_box.config import AGENT_EFFORTS
        from agent_box.external_config import EFFORTS
        from agent_box.speed import SPEEDS
        self.assertLessEqual({*EFFORTS, *(e for efforts in AGENT_EFFORTS.values() for e in efforts)}, set(config_menu.EFFORT_HELP))
        self.assertEqual(set(config_menu.SPEED_HELP), set(SPEEDS))

    def test_a_choice_describes_the_value_under_the_cursor_and_keeps_the_field_help_behind_question_mark(self):
        """Moving changes the description, in the sidebar and in the bottom area of a narrow terminal."""
        import curses
        keys = [*select("agents"), "\n", *select("default", ("agents",)), "\n",
                curses.KEY_HOME, curses.KEY_DOWN, "?", "\x1b", "\x1b", "\x1b", "\x1b", "\x1b"]
        claude, codex = (config_menu.OPTION_HELP["agents.default"][agent] for agent in ("claude", "codex"))
        for size in ((24, 80), (24, 140)):
            with self.subTest(size=size):
                screen = Screen(keys, sizes=[size])
                self.edit(Config(), [], screen=screen)
                text = [" ".join(frame.split()) for frame in screen.frames]
                first = next(i for i, frame in enumerate(text) if claude in frame)
                self.assertNotIn(codex, text[first])
                moved = next(i for i, frame in enumerate(text) if i > first and codex in frame)
                self.assertNotIn(claude, text[moved])
                self.assertIn(config_menu.help_for(("agents", "default")), text[moved + 1])

    def test_question_mark_shows_an_option_description_a_short_terminal_cut_together_with_the_field_help(self):
        """At 40x8 the bottom area holds two lines; the rest of the option's text must stay reachable."""
        import curses
        from agent_box.config import TRACKER_PROVIDERS
        keys = [*select("tracker"), "\n", *select("provider", ("tracker",)), "\n",
                curses.KEY_HOME, *[curses.KEY_DOWN] * TRACKER_PROVIDERS.index("jira"), "?", "\x1b", *["\x1b"] * 3]
        screen = Screen(keys, sizes=[(8, 40)])
        self.edit(Config(), [], screen=screen)
        jira = config_menu.OPTION_HELP["tracker.provider"]["jira"]
        cut = next(" ".join(frame.split()) for frame in screen.frames if "?: full explanation" in frame and "jira" in frame)
        self.assertNotIn(jira, cut)
        shown = [" ".join(frame.split()) for frame in screen.frames if "Up/Down PgUp/PgDn: scroll" in frame]
        self.assertTrue(any(jira in frame and " ".join(config_menu.help_for(("tracker", "provider")).split()[:3]) in frame
                            for frame in shown))

    def test_sync_events_are_toggled_as_checkboxes_and_escape_keeps_the_saved_list(self):
        """Space toggles, Enter keeps the selection in vocabulary order, Esc discards it."""
        import curses
        toggles = [*select("workflow"), "\n", *select("sync", ("workflow",)), "\n", *select("events", ("workflow", "sync")), "\n",
                   curses.KEY_HOME, " ", curses.KEY_END, " "]
        save = ["\x1b", "\x1b", curses.KEY_END, curses.KEY_UP, "\n"]
        result, screen = self.edit(Config(), [*toggles, "\n", *save])
        self.assertEqual(result.config.workflow.sync.events, ["before", "after"])
        self.assertTrue(any("[x] before" in frame and "[ ] plan" in frame for frame in screen.frames))
        self.assertTrue(any(config_menu.OPTION_HELP["workflow.sync.events"]["plan"] in " ".join(frame.split())
                            for frame in screen.frames))
        result, _ = self.edit(Config(), [*toggles, "\x1b", *save])
        self.assertEqual(result.config.workflow.sync.events, ["plan", "after"])

    def test_registry_hosts_are_ticked_from_what_the_host_holds(self):
        """A vanished earlier choice stays visible so it can be unticked; no login value is drawn."""
        import curses
        from agent_box import registries
        with tempfile.TemporaryDirectory() as temp:
            auth = Path(temp) / "auth.json"
            auth.write_text(json.dumps({"auths": {"cr.yandex": {"auth": "SECRET-LOGIN"}}}))
            config = Config()
            config.credentials.registries.hosts = ["old.example"]
            keys = [*select("credentials"), "\n", *select("registries", ("credentials",)), "\n",
                    *select("hosts", ("credentials", "registries")), "\n", curses.KEY_HOME, " ", curses.KEY_END, " ", "\n",
                    "\x1b", "\x1b", curses.KEY_END, curses.KEY_UP, "\n"]
            with patch.object(registries, "CONTAINERS_AUTH_FILES", (auth,)), \
                 patch.object(registries, "HELM_REGISTRY_CONFIG", Path(temp) / "missing.json"), \
                 patch.object(registries, "HELM_REPOSITORY_CONFIG", Path(temp) / "missing.yaml"):
                result, screen = self.edit(config, keys)
        self.assertEqual(result.config.credentials.registries.hosts, ["cr.yandex"])
        self.assertTrue(any("[ ] cr.yandex" in frame and "[x] old.example" in frame for frame in screen.frames))
        self.assertFalse(any("SECRET-LOGIN" in frame for frame in screen.frames))

    def test_kubeconfigs_are_ticked_with_their_authentication_beside_each_row(self):
        """An exec file is visible as one before it is chosen, without moving onto it; no token is drawn."""
        import curses
        from agent_box import kubeconfigs
        with tempfile.TemporaryDirectory() as temp:
            kube = Path(temp)
            (kube / "dev").write_text("kind: Config\nusers:\n- name: u\n  user:\n    exec:\n      command: yc\n")
            (kube / "prod").write_text("kind: Config\nusers:\n- name: u\n  user:\n    token: SECRET-TOKEN\n")
            keys = [*select("credentials"), "\n", *select("kubernetes", ("credentials",)), "\n",
                    *select("files", ("credentials", "kubernetes")), "\n", curses.KEY_END, " ", "\n",
                    "\x1b", "\x1b", curses.KEY_END, curses.KEY_UP, "\n"]
            with patch.object(kubeconfigs, "KUBECONFIG_DIR", kube):
                result, screen = self.edit(Config(), keys)
        self.assertEqual(result.config.credentials.kubernetes.files, ["prod"])
        lines = [line for frame in screen.frames for line in frame.splitlines()]
        self.assertTrue(any("[ ] dev" in line and "exec: yc" in line for line in lines))
        self.assertTrue(any("[ ] prod" in line and ": token" in line for line in lines))
        self.assertFalse(any("SECRET-TOKEN" in frame for frame in screen.frames))

    def test_yc_profiles_are_ticked_with_their_sign_in_and_bring_their_tool_group(self):
        """Forwarded profiles without yc in the image look set up and are not, so choosing
        one ticks the yc group; the sign-in kind is drawn, never a token or a key."""
        import curses
        from agent_box import cloud
        with tempfile.TemporaryDirectory() as temp:
            yc = Path(temp)
            (yc / "credentials").mkdir()
            (yc / "config.yaml").write_text("current: dixy\nprofiles:\n  dixy:\n    federation-id: fed\n"
                                            "  lenta:\n    service-account-key:\n      private_key: |\n"
                                            "        SECRET-KEY\n")
            (yc / "credentials/dixy").write_text('{"token": "SECRET-TOKEN"}')
            config = Config()
            config.credentials.cloud.enabled = True
            keys = [*select("credentials"), "\n", *select("cloud", ("credentials",)), "\n",
                    *select("yc", ("credentials", "cloud")), "\n", *select("profiles", ("credentials", "cloud", "yc")),
                    "\n", curses.KEY_HOME, " ", "\n", "\x1b", "\x1b", "\x1b", curses.KEY_END, curses.KEY_UP, "\n"]
            with patch.object(cloud, "YC_CONFIG_DIR", yc):
                result, screen = self.edit(config, keys)
        self.assertEqual(result.config.credentials.cloud.yc, ["dixy"])
        self.assertTrue(result.config.box.tools.groups["yc"])
        lines = [line for frame in screen.frames for line in frame.splitlines()]
        self.assertTrue(any("[ ] dixy" in line and ": federation" in line for line in lines))
        self.assertTrue(any("[ ] lenta" in line and ": service account key" in line for line in lines))
        self.assertTrue(any("Image tools: yc turned on" in " ".join(frame.split()) for frame in screen.frames))
        self.assertFalse(any("SECRET" in frame for frame in screen.frames))

    def test_a_tool_group_stays_ticked_while_configuration_needing_it_is_forwarded(self):
        """Unticking kubectl under forwarded kubeconfigs would build a box with configs and no
        tool to read them; the menu refuses and names what needs the group."""
        import curses
        config = Config()
        config.credentials.kubernetes.enabled = True
        config.credentials.kubernetes.files = ["prod"]
        keys = [*select("box"), "\n", *select("tools", ("box",)), "\n", *select("kubernetes", ("box", "tools")), " ",
                "\x1b", "\x1b", curses.KEY_END, curses.KEY_UP, "\n"]
        result, screen = self.edit(config, keys)
        self.assertTrue(result.config.box.tools.groups["kubernetes"])
        self.assertTrue(any("Kubernetes configs need this group;" in " ".join(frame.split())
                            for frame in screen.frames))

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
        keys = [*select("agents"), "\n", *select("claude", ("agents",)), "\n", *select("statusline", ("agents", "claude")), "\n",
                *select("enabled", ("agents", "claude", "statusline")), " ", "\x1b", "\x1b", "\x1b"]
        result, _ = self.edit(config, [*keys, "\x1b"])
        self.assertIsNone(result)
        self.assertTrue(config.agents.claude.statusline.enabled)
        result, _ = self.edit(config, [*keys, curses.KEY_END, curses.KEY_UP, "\n"])
        self.assertFalse(result.config.agents.claude.statusline.enabled)
        self.assertTrue(config.agents.claude.statusline.enabled)
        self.assertEqual(result.config.host.bin_dir, "")

    def test_saved_token_is_not_drawn_when_opening_its_connection(self):
        import curses
        config = Config()
        config.tracker.connections["vikunja"] = TrackerConnection("https://example.invalid", "secret-token")
        _, screen = self.edit(config, [*select("tracker"), "\n", curses.KEY_DOWN, curses.KEY_DOWN, "\n", "\n", "\x1b", "\x1b", "\x1b", "\x1b"])
        self.assertNotIn("secret-token", "\n".join(screen.drawn))
        self.assertIn("Access token: <configured>", screen.drawn)

    def test_the_tier_switch_is_toggled_and_saved_from_the_workflow_menu(self):
        """Use tiers sits beside the roles it affects, and saving it keeps the tier lists."""
        import curses
        prefix = [*select("workflow"), "\n", *select("use_tiers", ("workflow",)), " "]
        suffix = ["\x1b", curses.KEY_END, curses.KEY_UP, "\n"]
        config = Config()
        config.workflow.worker.tiers["2"] = [{"provider": "codex", "model": "gpt-5.5", "effort": "high",
                                              "speed": "inherit", "api_provider": ""}]
        cancelled, screen = self.edit(config, [*prefix, *["\x1b"] * 2])
        self.assertIsNone(cancelled)
        self.assertFalse(config.workflow.use_tiers)
        self.assertTrue(any("Use tiers" in line for line in screen.drawn))
        result, _ = self.edit(config, [*prefix, *suffix])
        self.assertTrue(result.config.workflow.use_tiers)
        self.assertEqual(result.config.workflow.worker.tiers["2"][0]["model"], "gpt-5.5")

    def test_every_generated_section_and_field_has_specific_help(self):
        data = Config().to_dict()
        data.pop("version")
        data["host"].pop("bin_dir")
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
        config.host.notifications.telegram_chat_id = "abcd"
        keys = [*select("host"), "\n", *select("notifications", ("host",)), "\n", curses.KEY_DOWN, curses.KEY_DOWN, "\n",
                curses.KEY_HOME, "X", curses.KEY_RIGHT, curses.KEY_DC,
                curses.KEY_END, curses.KEY_LEFT, curses.KEY_BACKSPACE, "\n",
                "\x1b", "\x1b", curses.KEY_END, curses.KEY_UP, "\n"]
        result, screen = self.edit(config, keys)
        self.assertEqual(result.config.host.notifications.telegram_chat_id, "Xad")
        self.assertIn((3, len("Value: [ ")), screen.cursor)

    def test_secret_editor_masks_the_value_and_escape_restores_it(self):
        import curses
        config = Config()
        config.host.notifications.telegram_token = "do-not-draw-this"
        result, screen = self.edit(config, [*select("host"), "\n", *select("notifications", ("host",)), "\n", curses.KEY_DOWN,
                                            "\n", curses.KEY_LEFT, "X", "\x1b",
                                            "\x1b", "\x1b", "\x1b"])
        self.assertIsNone(result)
        self.assertNotIn("do-not-draw-this", "\n".join(screen.drawn))
        self.assertEqual(config.host.notifications.telegram_token, "do-not-draw-this")

    def test_environment_values_are_visible_and_malformed_names_are_refused_while_editing(self):
        """env.set and env.forward hold no secrets by design.

        Masking them hid the pre-filled JSON that blind typing then corrupted,
        and a name that saving drops must not be reported as a draft change.
        """
        import curses
        config = Config()
        config.box.env.set = {"TZ": "UTC"}
        open_set = [*select("box"), "\n", *select("env", ("box",)), "\n", *select("set", ("box", "env")), "\n"]
        save = ["\x1b", "\x1b", curses.KEY_END, curses.KEY_UP, "\n"]
        result, screen = self.edit(config, [*open_set, curses.KEY_LEFT, *', "LANG": "C"', "\n", *save])
        self.assertEqual(result.config.box.env.set, {"TZ": "UTC", "LANG": "C"})
        self.assertTrue(any(line.startswith("Set") and '{"TZ": "UTC"}' in line for line in screen.drawn))
        self.assertTrue(any(line.startswith('{"TZ": "UTC"}') for line in screen.drawn), "editor field is masked")

        refused, screen = self.edit(config, [*open_set, "\x15", *'{"my-var": "x"}', "\n", *save])
        self.assertEqual(refused.config.box.env.set, {"TZ": "UTC"})
        self.assertTrue(any(line.startswith('Not saved: "my-var"') for line in screen.drawn))

        open_forward = [*select("box"), "\n", *select("env", ("box",)), "\n", *select("forward", ("box", "env")), "\n"]
        refused, screen = self.edit(config, [*open_forward, "\x15", *'["TOKEN", "1BAD"]', "\n", *save])
        self.assertEqual(refused.config.box.env.forward, [])
        self.assertTrue(any(line.startswith('Not saved: "1BAD"') for line in screen.drawn))

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
        self.assertIn("Help: " + config_menu.menu_keys(Config().to_dict())[0], screen.drawn)

    def test_candidate_list_adds_edits_reorders_and_removes_in_the_draft(self):
        """One editor serves every role list; cancelling the menu keeps the input untouched."""
        import curses
        config = Config()
        opened = [*select("workflow"), "\n", *select("scout", ("workflow",)), "\n", *select("models", ("workflow", "scout")), "\n"]
        claude_suggestion = len(BUNDLED_MODELS["codex"])  # suggestions list every Codex model before Claude's
        keys = [*opened,
                "\n", curses.KEY_HOME, *[curses.KEY_DOWN] * (2 + claude_suggestion), "\n",   # add the first Claude suggestion
                curses.KEY_END, "\n", curses.KEY_HOME, "\n", *"gpt-6-astra", "\n",       # add a custom Codex model
                curses.KEY_HOME, curses.KEY_DOWN, "\n", *[curses.KEY_DOWN] * 3, "\n",        # move it up
                curses.KEY_HOME, "\n", curses.KEY_DOWN, "\n", curses.KEY_HOME, *[curses.KEY_DOWN] * 3, "\n",  # effort high
                curses.KEY_HOME, "\n", *[curses.KEY_DOWN] * 2, "\n", curses.KEY_END, "\n",                        # speed fast
                "\x1b"]
        cancelled, _ = self.edit(config, [*keys, "\x1b", "\x1b", "\x1b"])
        self.assertIsNone(cancelled)
        self.assertEqual(config.workflow.scout.models, [])
        result, screen = self.edit(config, [*keys, "\x1b", "\x1b", curses.KEY_END, curses.KEY_UP, "\n"])
        self.assertEqual(result.config.workflow.scout.models, [
            {"provider": "codex", "model": "gpt-6-astra", "effort": "high", "speed": "fast", "api_provider": ""},
            {"provider": "claude", "model": BUNDLED_MODELS["claude"][0], "effort": "medium", "speed": "inherit", "api_provider": ""}])
        self.assertTrue(any(line.lstrip().startswith("Models") and "codex:gpt-6-astra:high speed=fast" in line for line in screen.drawn))
        self.assertEqual(result.config.workflow.worker, config.workflow.worker)
        removed, _ = self.edit(result.config, [*opened, curses.KEY_HOME, "\n", curses.KEY_END, "\n", "\x1b",
                                               "\x1b", "\x1b", curses.KEY_END, curses.KEY_UP, "\n"])
        self.assertEqual([spec["model"] for spec in removed.config.workflow.scout.models], [BUNDLED_MODELS["claude"][0]])

    def test_scout_speed_selects_standard_inherit_and_can_cancel(self):
        """The speed picker has three distinct choices and cancellation preserves the candidate."""
        import curses
        config = Config()
        config.workflow.scout.models = [{"provider": "codex", "model": "gpt-6-astra", "effort": "high",
                                         "speed": "fast", "api_provider": ""}]
        opened = [*select("workflow"), "\n", *select("scout", ("workflow",)), "\n",
                  *select("models", ("workflow", "scout")), "\n", "\n",
                  curses.KEY_DOWN, curses.KEY_DOWN, "\n"]
        saved = ["\x1b", "\x1b", "\x1b", curses.KEY_END, curses.KEY_UP, "\n"]
        for choice, speed in (([curses.KEY_HOME, curses.KEY_DOWN, "\n"], "standard"),
                              ([curses.KEY_HOME, "\n"], "inherit"), (["\x1b"], "fast")):
            with self.subTest(speed=speed):
                result, _ = self.edit(config, [*opened, *choice, *saved])
                self.assertEqual(result.config.workflow.scout.models[0]["speed"], speed)
                self.assertEqual(config.workflow.scout.models[0]["speed"], "fast")

    def test_picker_offers_both_clis_keeps_an_unknown_current_model_and_refreshes_catalogs_once(self):
        """Any CLI's model may serve any role, so a Codex list offers Claude models too."""
        import curses
        catalogs = {"codex": ("new-codex",), "claude": ("new-claude",)}
        for project in (None, {}):
            config = Config()
            config.workflow.review.tiers["2"] = [{"provider": "claude", "model": "private-model", "effort": "medium",
                                                   "speed": "inherit", "api_provider": ""}]
            path = ("workflow", "review", "tiers")
            keys = [*select("workflow"), "\n", *select("review", path[:1]), "\n", *select("tiers", path[:2]), "\n",
                    curses.KEY_DOWN, "\n", "\n", "\n", "\x1b", "\x1b", "\n", "\n", *["\x1b"] * 7]
            with self.subTest(project=project):
                result, screen = self.edit(config, keys, project=project, catalogs=catalogs)
                self.assertIsNone(result)
                self.assertIn("private-model [claude] (current)", screen.drawn)
                self.assertIn("new-codex [codex]", screen.drawn)
                self.assertIn("new-claude [claude]", screen.drawn)
                self.assertIn("Enter a custom model ID... [codex]", screen.drawn)
                # A kept ID is described as entered by hand, never as coming from a catalog it is not in.
                text = [" ".join(frame.split()) for frame in screen.frames]
                self.assertTrue(any("private-model: the saved ID, entered by hand" in frame for frame in text))
                self.assertFalse(any("private-model: a suggested" in frame for frame in text))

    def test_project_list_edit_records_the_whole_list_and_reset_inherits_again(self):
        """A list is one value: a project either owns all of it or inherits all of it."""
        import curses
        config = Config()
        config.workflow.worker.models = [{"provider": "claude", "model": "opus", "effort": "high", "speed": "inherit", "api_provider": ""}]
        prefix = [*select("workflow"), "\n", *select("worker", ("workflow",)), "\n", *select("models", ("workflow", "worker"))]
        save = ["\x1b", "\x1b", curses.KEY_END, curses.KEY_UP, "\n"]
        added = [*prefix, "\n", curses.KEY_END, "\n", curses.KEY_HOME, "\n", *"gpt-5.5", "\n", "\x1b"]
        result, _ = self.edit(config, [*added, *save], project={})
        self.assertEqual([spec["model"] for spec in result.project["workflow"]["worker"]["models"]], ["opus", "gpt-5.5"])
        self.assertNotIn("tiers", result.project["workflow"]["worker"])
        reset, _ = self.edit(config, [*prefix, "r", *save], project=result.project)
        self.assertEqual(reset.project, {})

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
        first, second = config_menu.menu_keys(Config().to_dict())[:2]
        screen = Screen(["?", curses.KEY_END, "\x1b", curses.KEY_DOWN, "?", "\x1b", "\x1b"], sizes=[(8, 40)])
        with patch.dict(config_menu.HELP, {first: "First paragraph. " * 50 + "LAST_SENTENCE"}):
            self.edit(Config(), [], screen=screen)
        self.assertTrue(any("LAST_SENTENCE" in frame for frame in screen.frames))
        self.assertIn("Help: " + second, "\n".join(screen.frames))

    def test_editor_scrolls_long_values_and_preserves_draft_across_resize(self):
        """Editing the visible tail and then shrinking the terminal must retain the full value."""
        import curses
        config = Config()
        config.host.notifications.telegram_chat_id = "a" * 150
        keys = [*select("host"), "\n", *select("notifications", ("host",)), "\n", curses.KEY_DOWN, curses.KEY_DOWN, "\n",
                curses.KEY_LEFT, "Z", curses.KEY_RESIZE, curses.KEY_RESIZE,
                curses.KEY_HOME, "Q", curses.KEY_END, "\n", "\x1b", "\x1b",
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
        self.assertEqual(result.config.host.notifications.telegram_chat_id, "Q" + "a" * 149 + "Za")
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
        """The root points at Host, and Host opens on its PATH row."""
        result, screen = self.edit(["\n", "\x1b", "\x1b"], choices=[self.bin])
        self.assertIsNone(result)
        self.assertIn("Setup starts here: agent-box is not on PATH yet (open Host)", " ".join(screen.drawn))
        self.assertIn("PATH integration: agent-box is not on PATH yet", screen.drawn)
        self.assertIn("Setup starts here: agent-box is not on PATH yet", screen.drawn)

    def test_saving_without_a_path_answer_asks_before_leaving_the_shell_broken(self):
        import curses
        # The cursor starts on Host; down to "Save and apply"; the gate opens, and Escape declines it,
        # so nothing is saved.
        result, screen = self.edit([curses.KEY_DOWN, "\n", "\x1b", "\x1b"], choices=[self.bin])
        self.assertIsNone(result)
        self.assertIn("Set up PATH now", screen.drawn)
        # Saving anyway stays available: it is the gate's second answer.
        result, _ = self.edit(
            [curses.KEY_DOWN, "\n", curses.KEY_DOWN, "\n"], choices=[self.bin])
        self.assertEqual(result.config.host.bin_dir, "")

    def test_the_path_submenu_starts_on_the_directory_rather_than_on_declining(self):
        import curses
        # The cursor opens on Host and then on its PATH row, Enter opens the
        # PATH submenu, and the directory -- not "leave PATH alone" -- is what
        # Enter takes there.
        result, _ = self.edit(["\n", "\n", "\n", "\x1b", curses.KEY_DOWN, "\n"], choices=[self.bin])
        self.assertEqual(result.config.host.bin_dir, str(self.bin.path))
        self.assertFalse(result.edit_rc)

    def test_a_recorded_directory_is_shown_and_kept_without_reopening_the_menu(self):
        import curses
        config = Config()
        config.host.bin_dir = "/home/somebody/.local/bin"
        result, screen = self.edit(
            [*select("host"), "\n", "\x1b", curses.KEY_DOWN, "\n"], config=config, choices=[self.bin])
        self.assertEqual(result.config.host.bin_dir, "/home/somebody/.local/bin")
        self.assertIn(
            "PATH integration: agent-box keeps /home/somebody/.local/bin/agent-box",
            screen.drawn)

    def test_a_link_agent_box_did_not_make_is_reported_as_unmaintained(self):
        from agent_box import pathsetup
        ours = pathsetup.LinkState("ours", Path("/usr/local/bin/agent-box"))
        _, screen = self.edit([*select("host"), "\n", "\x1b", "\x1b"], choices=[self.bin], link=ours)
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
        keys = [*select('workflow'), '\n', ' ', 'r', 'g', '\x1b', curses.KEY_END, curses.KEY_UP, '\n']
        screen = Screen(keys, sizes=[(8, 40)])
        result, _ = self.edit(config, [], screen=screen, project={})
        self.assertEqual(result.project, {})
        self.assertTrue(result.config.workflow.enabled)
        rows = [(text, attr) for _, x, text, attr in screen.writes
                if x == 0 and text.lstrip().startswith('Enabled') and ':' in text]
        self.assertTrue(rows)
        self.assertTrue(all('[Global only]' in text for text, _ in rows))
        self.assertTrue(all(not attr & curses.A_REVERSE for _, attr in rows))

    def test_use_global_pins_equal_values_while_reset_follows_later_global_changes(self):
        """Copying global is an explicit choice; reset resumes live inheritance."""
        import curses
        from agent_box import project_config
        config = Config()
        browser = [*select("box"), '\n', *select("tools", ("box",)), '\n', *select("browser", ("box", "tools"))]
        save = ['\x1b', '\x1b', curses.KEY_END, curses.KEY_UP, '\n']
        pinned, screen = self.edit(config, [*browser, 'g', *save], project={})
        self.assertEqual(pinned.project, {'box': {'tools': {'browser': False}}})
        self.assertIn('copy and pin', '\n'.join(screen.drawn))
        reset, _ = self.edit(config, [*browser, 'r', *save], project=pinned.project)
        self.assertEqual(reset.project, {})
        config.box.tools.groups['browser'] = True
        self.assertFalse(project_config.apply(config, pinned.project).box.tools.browser())
        self.assertTrue(project_config.apply(config, reset.project).box.tools.browser())

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
        self.assertEqual(pinned.project['workflow']['review']['models'], [])
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
            project = {'tracker': {'project': '12', 'connections': {'vikunja': {'url': 'https://temporary.example'}}}}
            environment = {'VIKUNJA_TOKEN': 'TEMPORARY_SECRET'}
            with patch.dict(os.environ, environment, clear=True), \
                 patch.object(runtime, 'load_config', return_value=config), \
                 patch.object(project_config, 'load', return_value=project), \
                 patch.object(runtime, 'TRACKER_RUNTIME_DIR', root / 'runtime'), \
                 patch.object(runtime, 'container_tracker_url', side_effect=lambda url: url):
                from agent_box.launch_options import LaunchOptions
                launched = runtime.launch_config(LaunchOptions(overrides={'tracker': 'https://temporary.example'}))
                state = runtime.WorkspaceRuntimeState(root / 'workspace', root / 'git', root / 'gh')
                snapshot = runtime.session_runtime(launched, runtime_state=state)
            self.assertNotIn('GLOBAL_SECRET', snapshot.read_text())
            with patch.dict(os.environ, {'AGENT_BOX_RUNTIME_CONFIG': str(snapshot)}, clear=True):
                baseline = effective_config(include_project=False)
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
        browser = [*select("box"), '\n', *select("tools", ("box",)), '\n', *select("browser", ("box", "tools"))]
        save = ['\x1b', '\x1b', curses.KEY_END, curses.KEY_UP, '\n']
        result, screen = self.edit(config, [*browser, ' ', ' ', *save], project={})
        self.assertEqual(result.project, {'box': {'tools': {'browser': False}}})
        self.assertIn('Inherited', '\n'.join(screen.drawn))
        self.assertIn('Overridden', '\n'.join(screen.drawn))
        reset, _ = self.edit(config, [*browser, 'r', *save], project=result.project)
        self.assertEqual(reset.project, {})
        blocked, screen = self.edit(config, [*select('agents'), ' ', 'r', 'g', curses.KEY_END, curses.KEY_UP, '\n'], project={})
        self.assertEqual(blocked.project, {})
        self.assertIn('Global only', '\n'.join(screen.drawn))

    def test_project_save_and_cancel_do_not_apply_or_configure_path(self):
        from agent_box import cli, project_config
        from contextlib import redirect_stdout
        import io
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.agent-box.json'
            result = config_menu.EditResult(Config(), project={'box': {'tools': {'browser': False}}})
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
                environment = {}
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
