"""External connections must be explicit, private and independent across CLIs."""
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_box import cli, config_menu, project_config
from agent_box.config import Config, load_config, save_config
from agent_box.errors import AgentBoxError
from agent_box.external_config import validate_connections
import test_config_menu as menu_tests


def external_config():
    return Config.from_dict({'external_providers': {'deepseek': {
        'enabled': True, 'api_key': 'private-api-key',
        'codex': {'url': 'https://api.example/v1', 'models': ['external-small', 'external-large']},
        'claude': {'url': 'https://api.example/anthropic', 'models': ['external-small']}}}})


class ExternalConfigTests(unittest.TestCase):
    def test_global_key_roundtrip_is_private_and_never_printed(self):
        """Stored API keys need the same protected file boundary as tracker tokens."""
        config = external_config()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'config.json'
            save_config(config, path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            loaded = load_config(path)
            self.assertEqual(loaded.external_providers, config.external_providers)
        with patch.object(cli, 'effective_config', return_value=config), contextlib.redirect_stdout(io.StringIO()) as output:
            cli.config_show()
        self.assertNotIn('private-api-key', output.getvalue())
        self.assertIn('https://api.example/anthropic', output.getvalue())
        self.assertTrue(config_menu.secret(('external_providers', 'deepseek', 'api_key')))
        self.assertFalse(Config.from_dict({'external_providers': {'new': {}}}).external_providers['new'].enabled)
        self.assertFalse(validate_connections(config))

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
                    Config.from_dict({'external_providers': data})
                self.assertNotIn('private-api-key', str(raised.exception))
        config = external_config()
        config.external_providers['deepseek'].api_key = ''
        self.assertTrue(validate_connections(config))
        config.external_providers['deepseek'].enabled = False
        self.assertFalse(validate_connections(config))

    def test_project_routes_reference_global_connections_without_owning_secrets(self):
        """A repository may select an API, but cannot redefine where its key goes."""
        value = {'workflow': {'tiers': {'0': {'codex': {'api_provider': 'deepseek', 'model': 'external-small'}}}}}
        project_config.validate(value, Path('.agent-box.json'))
        loaded = Config.from_dict(project_config.merge(external_config().to_dict(), value))
        self.assertEqual(loaded.workflow.tiers['0']['codex']['api_provider'], 'deepseek')
        with self.assertRaisesRegex(AgentBoxError, 'PROJECT_CREDENTIALS_FORBIDDEN') as raised:
            project_config.validate({'external_providers': {'x': {'api_key': 'private-api-key'}}}, Path('.agent-box.json'))
        self.assertNotIn('private-api-key', str(raised.exception))

    def test_endpoint_syntax_rejects_ambiguous_authorities_and_keeps_local_urls(self):
        """Client URL parsers must not reinterpret a saved authority or userinfo."""
        for url in [r'https://host\evil', 'https://@host', 'https://%20',
                    'https://host?', 'https://host#', 'https://host\x7f', 'https://<host>']:
            with self.subTest(url=url), self.assertRaises(AgentBoxError):
                Config.from_dict({'external_providers': {'x': {'codex': {'url': url}}}})
        for url in ['http://localhost:8000/v1', 'http://127.0.0.1:9',
                    'http://[::1]:8000/v1', 'https://api.example/a%20b']:
            config = Config.from_dict({'external_providers': {'x': {'codex': {'url': url}}}})
            self.assertEqual(config.external_providers['x'].codex.url, url)

    def test_menu_creation_and_deletion_are_draft_only(self):
        """Adding/deleting a connection must respect cancel and confirmation."""
        import curses
        start = [*menu_tests.select('external_providers'), '\n']
        add = [*start, '\n', *'new-api', '\n', '\x1b', '\x1b']
        edited, _ = menu_tests.ConfigMenuTests().edit(Config(), [*add, curses.KEY_END, curses.KEY_UP, '\n'])
        self.assertIn('new-api', edited.config.external_providers)
        self.assertFalse(edited.config.external_providers['new-api'].enabled)
        cancelled, _ = menu_tests.ConfigMenuTests().edit(Config(), [*add, '\x1b'])
        self.assertIsNone(cancelled)
        delete = [*start, '\n', curses.KEY_END, '\n', curses.KEY_DOWN, '\n', '\x1b',
                  curses.KEY_END, curses.KEY_UP, '\n']
        config = external_config()
        edited, screen = menu_tests.ConfigMenuTests().edit(config, delete)
        self.assertEqual(edited.config.external_providers, {})
        self.assertIn('deepseek', config.external_providers)
        self.assertNotIn('private-api-key', '\n'.join(screen.drawn))

    def test_all_candidate_menus_select_external_models_and_preserve_other_roles(self):
        """External suggestions must follow the selected CLI and role, including overrides."""
        import curses
        paths = [('workflow', 'tiers', '0', 'codex'), ('workflow', 'tiers', '1', 'claude'),
                 ('workflow', 'review', 'tiers', '2', 'codex'),
                 ('workflow', 'worker', 'override', 'models', 'claude'),
                 ('workflow', 'review', 'override', 'models', 'codex')]
        for path in paths:
            with self.subTest(path=path):
                config = external_config()
                keys = []
                for index, part in enumerate(path):
                    keys += [*menu_tests.select(part, path[:index]), '\n']
                keys += [*menu_tests.select('api_provider', path), '\n', curses.KEY_DOWN, '\n',
                         *menu_tests.select('model', path), '\n', '\n']
                keys += ['\x1b'] * len(path) + [curses.KEY_END, curses.KEY_UP, '\n']
                edited, screen = menu_tests.ConfigMenuTests().edit(config, keys)
                value = edited.config.to_dict()
                for part in path:
                    value = value[part]
                self.assertEqual(value['api_provider'], 'deepseek')
                self.assertEqual(value['model'], 'external-small')
                self.assertEqual(config.workflow.tiers['0']['codex']['api_provider'], '')
                self.assertNotIn('private-api-key', '\n'.join(screen.drawn))

    def test_empty_or_deleted_provider_still_allows_manual_model_entry(self):
        """An absent catalog must open on manual entry rather than lose the draft."""
        import curses
        path = ('workflow', 'tiers', '0', 'codex')
        for deleted in (False, True):
            config = external_config()
            config.external_providers['deepseek'].codex.models = []
            if deleted:
                del config.external_providers['deepseek']
            config.workflow.tiers['0']['codex']['api_provider'] = 'deepseek'
            keys = []
            for index, part in enumerate(path):
                keys += [*menu_tests.select(part, path[:index]), '\n']
            keys += [*menu_tests.select('model', path), '\n', '\n', *'manual-model', '\n']
            keys += ['\x1b'] * len(path) + [curses.KEY_END, curses.KEY_UP, '\n']
            edited, _ = menu_tests.ConfigMenuTests().edit(config, keys)
            self.assertEqual(edited.config.workflow.tiers['0']['codex']['model'], 'manual-model')
            self.assertEqual(edited.config.workflow.tiers['0']['codex']['api_provider'], 'deepseek')

    def test_reset_clears_only_the_selected_external_key_in_the_draft(self):
        """Dynamic fields need real defaults, just like fixed connection fields."""
        import curses
        config = external_config()
        config.external_providers['deepseek'].enabled = False
        keys = [*menu_tests.select('external_providers'), '\n', '\n', curses.KEY_DOWN, 'R',
                '\x1b', '\x1b', curses.KEY_END, curses.KEY_UP, '\n']
        edited, _ = menu_tests.ConfigMenuTests().edit(config, keys)
        self.assertEqual(edited.config.external_providers['deepseek'].api_key, '')
        self.assertEqual(config.external_providers['deepseek'].api_key, 'private-api-key')
