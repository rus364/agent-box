"""External connections must be explicit, private and independent across CLIs."""
import contextlib
import io
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from agent_box import cli, config_menu, project_config
from agent_box.config import Config, load_config, save_config
from agent_box.errors import AgentBoxError
from agent_box.external_config import codex_options, toml_string, validate_connections
import test_config_menu as menu_tests


def external_config():
    return Config.from_dict({'agents': {'external_apis': {'deepseek': {
        'enabled': True, 'api_key': 'private-api-key', 'models': {'external-small': {}, 'external-large': {}},
        'codex': {'url': 'https://api.example/v1'}, 'claude': {'url': 'https://api.example/anthropic'}}}}})


class ExternalTomlRenderingTests(unittest.TestCase):
    def test_strings_render_as_toml_a_codex_override_can_carry(self):
        """JSON's escaping is not TOML's, and Codex parses -c values as TOML.

        json.dumps writes a non-BMP character as a surrogate pair, which TOML
        rejects, and leaves DEL literal when told not to escape non-ASCII,
        which a basic string forbids. Either one is a value Codex cannot read,
        so the launch dies before the CLI starts.
        """
        for value in ("🚀 node", "del\x7fhere", 'quo"te', "back\\slash", "tab\there", "nl\nhere"):
            with self.subTest(value=value):
                self.assertEqual(tomllib.loads("x=" + toml_string(value)), {"x": value})

    def test_text_toml_cannot_spell_is_refused_where_it_enters(self):
        """A lone surrogate has no TOML string, and no argv either.

        Only a JSON source can carry one into an option; refusing it with the
        field named beats writing invalid UTF-8 into the command Codex parses.
        """
        for options, named in (({"web_search": "\ud83d"}, r"Codex options\.web_search"),
                               ({"\ud83d": "live"}, r"Codex options\.'\\ud83d'")):
            with self.subTest(options=options), self.assertRaisesRegex(AgentBoxError, named):
                codex_options(options, "Codex options")


    def test_a_connection_string_utf8_cannot_carry_is_refused_where_it_enters(self):
        """A lone surrogate reaches argv and the environment, which encode UTF-8.

        Only a hand-written JSON escape produces one, so the connection is
        refused with the field named instead of failing at spawn.
        """
        for connection, field in (
            ({"api_key": "\ud83d"}, "agents.external_apis.x.api_key"),
            ({"codex": {"url": "https://api.example/\ud83d"}}, "agents.external_apis.x.codex.url"),
            ({"codex": {"env": {"API_TIMEOUT_MS": "\ud83d"}}},
             "agents.external_apis.x.codex.env.API_TIMEOUT_MS"),
            ({"codex": {"models": {"m\ud83d": {"display_name": "x"}}}},
             "agents.external_apis.x.codex.models"),
        ):
            with self.subTest(field=field), self.assertRaises(AgentBoxError) as raised:
                Config.from_dict({"agents": {"external_apis": {"x": connection}}})
            self.assertIn(field, str(raised.exception))
            self.assertIn("a lone surrogate has no UTF-8 form", str(raised.exception))


class ExternalConfigTests(unittest.TestCase):
    def test_unified_picker_pins_api_with_duplicate_model_ids_and_resets_both(self):
        """A project model must not silently move to another API after global changes."""
        import curses
        config = external_config()
        model = menu_tests.BUNDLED_MODELS['codex'][0]
        provider = config.agents.external_apis['deepseek']
        provider.models = {model: provider.models['external-small']}
        path = ('workflow', 'scout', 'models')
        prefix = []
        for index, part in enumerate(path):
            prefix += [*menu_tests.select(part, path[:index]), *(['\n'] if index < len(path) - 1 else [])]
        save = ['\x1b'] * 2 + [curses.KEY_END, curses.KEY_UP, '\n']
        external = len(menu_tests.BUNDLED_MODELS['codex']) + len(menu_tests.BUNDLED_MODELS['claude'])
        keys = [*prefix, '\n', '\n', curses.KEY_HOME, *[curses.KEY_DOWN] * (4 + external), '\n', '\x1b', *save]
        edited, screen = menu_tests.ConfigMenuTests().edit(config, keys, project={})
        self.assertEqual(edited.project['workflow']['scout']['models'],
                         [{'provider': 'codex', 'model': model, 'effort': 'medium', 'speed': 'inherit', 'api_provider': 'deepseek'}])
        self.assertIn(model + ' [codex]', screen.drawn)
        self.assertIn(model + f' [codex@deepseek: {model}]', screen.drawn)
        reset, _ = menu_tests.ConfigMenuTests().edit(config, [*prefix, 'r', *save], project=edited.project)
        self.assertEqual(reset.project, {})
        copied, _ = menu_tests.ConfigMenuTests().edit(config, [*prefix, 'g', *save], project=edited.project)
        self.assertEqual(copied.project['workflow']['scout']['models'], [])
        cancelled, _ = menu_tests.ConfigMenuTests().edit(config, [*prefix, '\n', '\n', '\x1b', '\x1b', *save], project={})
        self.assertEqual(cancelled.project, {})

    def test_global_key_roundtrip_is_private_and_never_printed(self):
        """Stored API keys need the same protected file boundary as tracker tokens."""
        config = external_config()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'config.json'
            save_config(config, path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            loaded = load_config(path)
            self.assertEqual(loaded.agents.external_apis, config.agents.external_apis)
        with patch.object(cli, 'effective_config', return_value=config), contextlib.redirect_stdout(io.StringIO()) as output:
            cli.config_show()
        self.assertNotIn('private-api-key', output.getvalue())
        self.assertIn('https://api.example/anthropic', output.getvalue())
        self.assertTrue(config_menu.secret(('agents', 'external_apis', 'deepseek', 'api_key')))
        self.assertFalse(Config.from_dict({'agents': {'external_apis': {'new': {}}}}).agents.external_apis['new'].enabled)
        self.assertFalse(validate_connections(config))

    def test_a_malformed_candidate_api_name_is_a_validation_error_not_a_crash(self):
        """The menu and config set run both validators; a list-valued name must reach the schema error."""
        config = external_config()
        config.workflow.scout.models = [{'provider': 'codex', 'model': 'm', 'effort': 'medium', 'speed': 'inherit', 'api_provider': []}]
        self.assertIn('workflow.scout.models.0: invalid API provider name', config_menu.validate(config))

    def test_invalid_connections_fail_without_echoing_values(self):
        """An invalid URL must not expose embedded credentials in diagnostics."""
        bad = [[], {'bad secret': {}}, {'valid': {'enabled': 'private-api-key'}},
               {'valid': {'api_key': ['private-api-key']}},
               {'valid': {'codex': {'url': 'https://private-api-key@host'}}},
               {'valid': {'codex': {'url': 'https://host?token=private-api-key'}}},
               {'valid': {'claude': {'models': 'private-api-key'}}}]
        for data in bad:
            with self.subTest(data_type=type(data).__name__):
                with self.assertRaises(AgentBoxError) as raised:
                    Config.from_dict({'agents': {'external_apis': data}})
                self.assertNotIn('private-api-key', str(raised.exception))
        config = external_config()
        config.agents.external_apis['deepseek'].api_key = ''
        self.assertTrue(validate_connections(config))
        config.agents.external_apis['deepseek'].enabled = False
        self.assertFalse(validate_connections(config))

    def test_project_routes_reference_global_connections_without_owning_secrets(self):
        """A repository may select an API, but cannot redefine where its key goes."""
        value = {'workflow': {'worker': {'tiers': {'1': [
            {'provider': 'codex', 'api_provider': 'deepseek', 'model': 'external-small'}]}}}}
        project_config.validate(value, Path('.agent-box.json'))
        loaded = Config.from_dict(project_config.merge(external_config().to_dict(), value))
        self.assertEqual(loaded.workflow.worker.tiers['1'][0]['api_provider'], 'deepseek')
        for invalid in ({'provider': 'codex', 'model': 'm', 'api_key': 'private-api-key'},
                        {'provider': 'codex', 'model': 1}, 'codex:m'):
            with self.subTest(invalid=invalid), self.assertRaises(AgentBoxError) as raised:
                project_config.validate({'workflow': {'scout': {'models': [invalid]}}}, Path('.agent-box.json'))
            self.assertNotIn('private-api-key', str(raised.exception))
        with self.assertRaisesRegex(AgentBoxError, 'PROJECT_CREDENTIALS_FORBIDDEN') as raised:
            project_config.validate({'agents': {'external_apis': {'x': {'api_key': 'private-api-key'}}}}, Path('.agent-box.json'))
        self.assertNotIn('private-api-key', str(raised.exception))

    def test_endpoint_syntax_rejects_ambiguous_authorities_and_keeps_local_urls(self):
        """Client URL parsers must not reinterpret a saved authority or userinfo."""
        for url in [r'https://host\evil', 'https://@host', 'https://%20',
                    'https://host?', 'https://host#', 'https://host\x7f', 'https://<host>']:
            with self.subTest(url=url), self.assertRaises(AgentBoxError):
                Config.from_dict({'agents': {'external_apis': {'x': {'codex': {'url': url}}}}})
        for url in ['http://localhost:8000/v1', 'http://127.0.0.1:9',
                    'http://[::1]:8000/v1', 'https://api.example/a%20b']:
            config = Config.from_dict({'agents': {'external_apis': {'x': {'codex': {'url': url}}}}})
            self.assertEqual(config.agents.external_apis['x'].codex.url, url)

    def test_menu_creation_and_deletion_are_draft_only(self):
        """Adding/deleting a connection must respect cancel and confirmation."""
        import curses
        start = [*menu_tests.select('agents'), '\n', *menu_tests.select('external_apis', ('agents',)), '\n']
        add = [*start, '\n', *'new-api', '\n', '\x1b', '\x1b', '\x1b']
        edited, _ = menu_tests.ConfigMenuTests().edit(Config(), [*add, curses.KEY_END, curses.KEY_UP, '\n'])
        self.assertIn('new-api', edited.config.agents.external_apis)
        self.assertFalse(edited.config.agents.external_apis['new-api'].enabled)
        cancelled, _ = menu_tests.ConfigMenuTests().edit(Config(), [*add, '\x1b'])
        self.assertIsNone(cancelled)
        delete = [*start, '\n', curses.KEY_END, '\n', curses.KEY_DOWN, '\n', '\x1b', '\x1b',
                  curses.KEY_END, curses.KEY_UP, '\n']
        config = external_config()
        edited, screen = menu_tests.ConfigMenuTests().edit(config, delete)
        self.assertEqual(edited.config.agents.external_apis, {})
        self.assertIn('deepseek', config.agents.external_apis)
        self.assertNotIn('private-api-key', '\n'.join(screen.drawn))

    def test_all_candidate_lists_select_external_models_and_preserve_other_roles(self):
        """External suggestions reach every role list and change only the list being edited."""
        import curses
        paths = [('workflow', 'scout', 'models'), ('workflow', 'worker', 'tiers', '2'),
                 ('workflow', 'review', 'models'), ('workflow', 'review', 'tiers', '1')]
        for path in paths:
            with self.subTest(path=path):
                config = external_config()
                keys = []
                for index, part in enumerate(path):
                    keys += [*(menu_tests.select(part, path[:index]) if index < 3 else [curses.KEY_HOME, *[curses.KEY_DOWN] * (int(part) - 1)]), '\n']
                keys += ['\n', curses.KEY_END, '\n', '\x1b']
                keys += ['\x1b'] * (len(path) - 1) + [curses.KEY_END, curses.KEY_UP, '\n']
                edited, screen = menu_tests.ConfigMenuTests().edit(config, keys)
                value = edited.config.to_dict()
                for part in path:
                    value = value[part]
                self.assertEqual(value, [{'provider': 'claude', 'model': 'external-large', 'effort': 'medium',
                                          'speed': 'inherit', 'api_provider': 'deepseek'}])
                self.assertEqual(len(edited.config.workflow.candidates()), 1)
                self.assertNotIn('private-api-key', '\n'.join(screen.drawn))

    def test_empty_or_deleted_provider_still_allows_manual_model_entry(self):
        """An absent catalog must open on manual entry for the candidate's own API rather than lose it."""
        import curses
        path = ('workflow', 'worker', 'models')
        for deleted in (False, True):
            config = external_config()
            config.agents.external_apis['deepseek'].codex.models = {}
            if deleted:
                del config.agents.external_apis['deepseek']
            config.workflow.worker.models = [{'provider': 'codex', 'model': 'old', 'effort': 'medium', 'speed': 'inherit',
                                              'api_provider': 'deepseek'}]
            keys = []
            for index, part in enumerate(path):
                keys += [*menu_tests.select(part, path[:index]), '\n']
            row = 0 if deleted else 2
            keys += ['\n', '\n', curses.KEY_HOME, *[curses.KEY_DOWN] * row, '\n', '\x15', *'manual-model', '\n', '\x1b']
            keys += ['\x1b'] * 2 + [curses.KEY_END, curses.KEY_UP, '\n']
            with self.subTest(deleted=deleted):
                edited, _ = menu_tests.ConfigMenuTests().edit(config, keys)
                self.assertEqual(edited.config.workflow.worker.models[0]['model'], 'manual-model')
                self.assertEqual(edited.config.workflow.worker.models[0]['api_provider'], 'deepseek')

    def test_reset_clears_only_the_selected_external_key_in_the_draft(self):
        """Dynamic fields need real defaults, just like fixed connection fields."""
        import curses
        config = external_config()
        config.agents.external_apis['deepseek'].enabled = False
        keys = [*menu_tests.select('agents'), '\n', *menu_tests.select('external_apis', ('agents',)), '\n', '\n', curses.KEY_DOWN, 'R',
                '\x1b', '\x1b', '\x1b', curses.KEY_END, curses.KEY_UP, '\n']
        edited, _ = menu_tests.ConfigMenuTests().edit(config, keys)
        self.assertEqual(edited.config.agents.external_apis['deepseek'].api_key, '')
        self.assertEqual(config.agents.external_apis['deepseek'].api_key, 'private-api-key')
