"""Model tuning must reach real launch artifacts without changing API ownership."""
import copy
import curses
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_box import config_menu, external_launch as ext, provider_runner, runtime, workflow
from agent_box.config import Config, load_config, save_config
from agent_box.errors import AgentBoxError
from agent_box.external_config import model_defaults, validate_connections
from agent_box.workspace import Workspace
import test_config_menu as menu_tests
from test_external_config import external_config


def rich_config():
    data = external_config().to_dict()
    for cli in ('codex', 'claude'):
        endpoint = data['external_providers']['deepseek'][cli]
        endpoint.update(default_model='v4-pro', env={'MODEL_OPTION': 'endpoint', 'ENDPOINT_ONLY': 'private-value'})
        model = dict(display_name='V4 Pro', effort='high', supported_efforts=['low', 'high', 'max'],
                     context_window=1048576, auto_compact_token_limit=786432, env={'MODEL_OPTION': 'model'})
        if cli == 'codex':
            model.update(input_modalities=['text', 'image'], supports_parallel_tool_calls=True,
                         supports_reasoning_summaries=True, metadata={'support_verbosity': True, 'default_verbosity': 'low'})
        endpoint['models'] = {'v4-flash': {}, 'v4-pro': model}
    return Config.from_dict(data)


class ExternalModelTests(unittest.TestCase):
    def test_invalid_effort_stops_host_and_container_before_launch_side_effects(self):
        """Known unsupported effort must not build a box or render session credentials."""
        config = rich_config()
        for cli, options in (('claude', ['--effort', 'medium']), ('codex', ['-cmodel_reasoning_effort=medium'])):
            for host in (True, False):
                with self.subTest(cli=cli, host=host), \
                        patch.object(runtime.shutil, 'which', return_value='/usr/bin/' + cli), \
                        patch.object(runtime, 'resolve_workspace', return_value=Workspace(Path.cwd(), Path.cwd())), \
                        patch.object(runtime, 'launch_config', return_value=config), \
                        patch.object(runtime, 'ensure_state_dirs'), patch.object(runtime, 'ensure_refresh_token'), \
                        patch.object(runtime, 'image_exists', side_effect=AssertionError('Image preparation reached')) as image, \
                        patch.object(runtime, 'session_runtime', side_effect=AssertionError('Snapshot rendering reached')) as snapshot, \
                        patch.object(runtime, 'launch_in_workspace_container') as container, \
                        patch.object(runtime.subprocess, 'call') as call:
                    with self.assertRaisesRegex(AgentBoxError, 'not supported'):
                        if host:
                            runtime.host_main(cli, options, api_provider='deepseek')
                        else:
                            runtime.runtime_main([cli, *options], Path.cwd(), api_provider='deepseek')
                    image.assert_not_called()
                    snapshot.assert_not_called()
                    container.assert_not_called()
                    call.assert_not_called()

    def test_saved_default_and_capabilities_survive_snapshot_and_launch(self):
        """Serialization order cannot change an explicit default or discard vision/effort."""
        config = rich_config()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            save_config(config, root / 'config.json')
            loaded = load_config(root / 'config.json')
            runtime.sync_tracker_runtime(loaded, root / 'snapshot', policy=True)
            snapshot = Config.from_dict(json.loads((root / 'snapshot/config.json').read_text()))
            for cli in ('codex', 'claude'):
                with self.subTest(cli=cli):
                    self.assertEqual(ext.selected_model(snapshot, 'deepseek', cli, []), 'v4-pro')
                    command, env = ext.prepare([cli], 'deepseek', cli, snapshot, root / cli, {})
                    self.assertNotIn('private-value', str(command))
                    if cli == 'codex':
                        rows = json.loads((root / cli / 'models.json').read_text())['models']
                        model = next(row for row in rows if row['slug'] == 'v4-pro')
                        self.assertEqual(model['context_window'], 1048576)
                        self.assertEqual(model['auto_compact_token_limit'], 786432)
                        self.assertEqual(model['input_modalities'], ['text', 'image'])
                        self.assertEqual([e['effort'] for e in model['supported_reasoning_levels']], ['low', 'high', 'max'])
                        self.assertEqual(model['default_reasoning_level'], 'high')
                        self.assertTrue(model['supports_parallel_tool_calls'])
                        self.assertTrue(model['supports_reasoning_summaries'])
                        self.assertTrue(model['support_verbosity'])
                        self.assertIn('model_reasoning_effort="high"', command)
                    else:
                        settings = json.loads((root / cli / 'claude-settings.json').read_text())
                        self.assertEqual(env['ANTHROPIC_MODEL'], 'v4-pro[1m]')
                        self.assertEqual(env['CLAUDE_CODE_EFFORT_LEVEL'], 'high')
                        self.assertEqual(env['CLAUDE_CODE_AUTO_COMPACT_WINDOW'], '786432')
                        self.assertIn({'model': 'v4-pro[1m]', 'label': 'V4 Pro'}, settings['modelPicker']['options'])

    def test_explicit_effort_and_manual_models_win_without_inheriting_another_models_settings(self):
        """Workflow effort must win over defaults; manual IDs cannot inherit unrelated capabilities."""
        config = rich_config()
        for cli, options in (('codex', ['-c', 'model_reasoning_effort="max"']), ('claude', ['--effort', 'max'])):
            with self.subTest(cli=cli), tempfile.TemporaryDirectory() as tmp:
                command, env = ext.prepare([cli, '--model', 'v4-pro', *options], 'deepseek', cli, config, tmp, {})
                if cli == 'codex':
                    self.assertLess(command.index('model_reasoning_effort="high"'), command.index('model_reasoning_effort="max"'))
                else:
                    self.assertEqual(env['CLAUDE_CODE_EFFORT_LEVEL'], 'max')
                    self.assertEqual(command[command.index('--model') + 1], 'v4-pro[1m]')
                _, env = ext.prepare([cli, '--model', 'manual-model', *options], 'deepseek', cli, config, tmp, {})
                self.assertEqual(env['MODEL_OPTION'], 'endpoint')
                self.assertNotIn('CLAUDE_CODE_AUTO_COMPACT_WINDOW', env)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(AgentBoxError, 'not supported'):
                ext.prepare(['claude', '--effort', 'medium'], 'deepseek', 'claude', config, tmp, {})
            self.assertEqual(list(Path(tmp).iterdir()), [])
            command, _ = ext.prepare(['claude', '--model=v4-pro[1m]', '--', '--effort'], 'deepseek', 'claude', config, tmp, {})
            self.assertIn('--model=v4-pro[1m]', command)
            settings = json.loads((Path(tmp) / 'claude-settings.json').read_text())
            self.assertEqual(len(settings['modelPicker']['options']), 2)
            _, env = ext.prepare(['claude', '--name', '--effort', '-p', 'hello'], 'deepseek', 'claude', config, tmp, {})
            self.assertEqual(env['CLAUDE_CODE_EFFORT_LEVEL'], 'high')
            command, _ = ext.prepare(['codex', '-cmodel_reasoning_effort=max', '-c', 'model_reasoning_summary=concise'],
                                     'deepseek', 'codex', config, tmp, {})
            self.assertIn('model_reasoning_effort=max', command)

    def test_model_environment_is_restored_across_external_and_builtin_children(self):
        """Endpoint tuning must not contaminate a nested API or its built-in fallback."""
        config = rich_config()
        config.external_providers['deepseek'].claude.env['CLAUDE_CODE_SUBAGENT_MODEL'] = 'auxiliary-model'
        original = {'MODEL_OPTION': 'parent', 'CLAUDE_CODE_AUTO_COMPACT_WINDOW': '100', 'OPENAI_API_KEY': 'parent-key'}
        with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
            _, first = ext.prepare(['claude'], 'deepseek', 'claude', config, one, original)
            self.assertEqual(first['MODEL_OPTION'], 'model')
            self.assertEqual(first['CLAUDE_CODE_SUBAGENT_MODEL'], 'auxiliary-model')
            self.assertEqual(ext.builtin_environment(first), original)
            _, second = ext.prepare(['codex', '--model', 'manual'], 'deepseek', 'codex', config, two, first)
            self.assertEqual(second['MODEL_OPTION'], 'endpoint')
            self.assertEqual(second['CLAUDE_CODE_AUTO_COMPACT_WINDOW'], '100')
            self.assertEqual(ext.builtin_environment(second), original)

    def test_unsupported_workflow_effort_is_rejected_before_preparation_or_process(self):
        """A picker change must not save an incompatible inherited effort or spend a review run."""
        config = rich_config()
        candidate = dict(provider='codex', model='v4-pro', effort='medium', fast=False, api_provider='deepseek')
        config.workflow.worker.tiers['1'] = [candidate]
        errors = validate_connections(config)
        self.assertTrue(any('workflow.worker.tiers.1.0' in error and 'not supported' in error for error in errors))
        route = {**candidate, **ext.route_identity(config, 'deepseek', 'codex')}
        with patch('agent_box.config.effective_config', return_value=config), \
                patch.object(provider_runner.subprocess, 'Popen') as process:
            for operation in (lambda: provider_runner.preflight(route),
                              lambda: provider_runner.run(route, 'reviewer', Path.cwd(), 'test')):
                with self.assertRaises(provider_runner.ProviderFailure) as failure:
                    operation()
                self.assertEqual(failure.exception.category, 'unsupported')
            process.assert_not_called()
        with tempfile.TemporaryDirectory() as temp, patch.object(workflow, 'WORKFLOW_STATE', Path(temp)), \
                patch.object(workflow.shutil, 'which', return_value='/bin/codex'):
            with self.assertRaisesRegex(AgentBoxError, 'not supported'):
                workflow.route(config, 'worker', 1, 'codex')
            candidate['effort'] = 'default'
            self.assertEqual(validate_connections(config), [])
            selected = workflow.route(config, 'worker', 1, 'codex')
            self.assertEqual(selected['effort'], 'default')

    def test_invalid_model_metadata_and_owned_environment_fail_privately(self):
        """Bad tuning must be rejected at load time without echoing supplied values."""
        bad = [{'effort': 'private-value'}, {'supported_efforts': [['private-value']]},
               {'supported_efforts': ['low'], 'effort': 'high'}, {'context_window': True},
               {'context_window': -1}, {'context_window': 100, 'auto_compact_token_limit': 100},
               {'input_modalities': ['audio']}, {'supports_parallel_tool_calls': 1},
               {'metadata': {'slug': 'private-value'}}, {'metadata': {'x': float('nan')}},
               {'env': {'ANTHROPIC_BASE_URL': 'private-value'}}, {'env': {'AGENT_BOX_API_PROVIDER': 'private-value'}},
               {'env': {'BAD=NAME': 'private-value'}}, {'env': {'OPTION': 1}}]
        for spec in bad:
            with self.subTest(fields=list(spec)), self.assertRaises(AgentBoxError) as error:
                Config.from_dict({'external_providers': {'test': {'codex': {'models': {'model': spec}}}}})
            self.assertNotIn('private-value', str(error.exception))
        for cli, model in (('codex', 'v4[1m]'), ('claude', 'v4[garbage]')):
            with self.subTest(cli=cli), self.assertRaises(AgentBoxError):
                Config.from_dict({'external_providers': {'test': {cli: {'models': {model: {}}}}}})
        with self.assertRaises(AgentBoxError):
            Config.from_dict({'external_providers': {'test': {'codex': {'models': ['model']}}}})

    def test_custom_headers_cannot_replace_the_configured_api_credentials(self):
        """Claude custom headers can carry auth, so neither env level may set them."""
        for cli in ('codex', 'claude'):
            for source in ('endpoint', 'model'):
                for header in ('Authorization: Bearer private-value', 'x-api-key: private-value'):
                    data = rich_config().to_dict()
                    endpoint = data['external_providers']['deepseek'][cli]
                    target = endpoint if source == 'endpoint' else next(iter(endpoint['models'].values()))
                    target['env']['ANTHROPIC_CUSTOM_HEADERS'] = header
                    with self.subTest(cli=cli, source=source, header=header.split(':')[0]):
                        with self.assertRaises(AgentBoxError) as raised:
                            Config.from_dict(data)
                        self.assertNotIn('private-value', str(raised.exception))

    def test_claude_model_alias_collisions_fail_independently_of_order(self):
        """Two definitions of one launched model must not silently compete in the picker."""
        entries = [('v4', {'context_window': 1048576, 'effort': 'high'}),
                   ('v4[1m]', {'effort': 'low', 'env': {'OPTION': 'private-value'}})]
        for ordered in (entries, entries[::-1]):
            with self.subTest(order=[model for model, _ in ordered]):
                with self.assertRaisesRegex(AgentBoxError, 'duplicate launched Claude model') as raised:
                    Config.from_dict({'external_providers': {'test': {'claude': {'models': dict(ordered)}}}})
                self.assertNotIn('private-value', str(raised.exception))
        for context in (0, 200000):
            models = {'v4': {'context_window': context}, 'v4[1m]': {}}
            loaded = Config.from_dict({'external_providers': {'test': {'claude': {'models': models}}}})
            self.assertEqual(set(loaded.external_providers['test'].claude.models), set(models))

    def test_claude_environment_effort_is_validated_at_load_with_typed_priority(self):
        """A configured default must work without requiring an explicit effort to repair it."""
        for source in ('endpoint', 'model'):
            for effort in ('medium', 'ultra', 'private-value'):
                data = rich_config().to_dict()
                endpoint = data['external_providers']['deepseek']['claude']
                model = endpoint['models']['v4-pro']
                model['effort'] = 'default'
                target = endpoint if source == 'endpoint' else model
                target['env']['CLAUDE_CODE_EFFORT_LEVEL'] = effort
                with self.subTest(source=source, effort=effort), self.assertRaises(AgentBoxError) as raised:
                    Config.from_dict(data)
                self.assertNotIn('private-value', str(raised.exception))
        data = rich_config().to_dict()
        endpoint = data['external_providers']['deepseek']['claude']
        endpoint['env']['CLAUDE_CODE_EFFORT_LEVEL'] = 'medium'
        # v4-pro's typed high wins; v4-flash has unknown supported levels.
        loaded = Config.from_dict(data)
        with tempfile.TemporaryDirectory() as temp:
            _, env = ext.prepare(['claude'], 'deepseek', 'claude', loaded, temp, {})
            self.assertEqual(env['CLAUDE_CODE_EFFORT_LEVEL'], 'high')
        endpoint['models']['v4-pro']['effort'] = 'ultra'
        with self.assertRaises(AgentBoxError):
            Config.from_dict(data)

    def test_suffixed_workflow_model_uses_the_same_effort_choices_as_launch(self):
        """A manually entered [1m] ID must not lose the configured model's effort metadata."""
        config = rich_config()
        config.workflow.worker.models = [dict(provider='claude', api_provider='deepseek', model='v4-pro[1m]',
                                              effort='default', fast=False)]
        path = ('workflow', 'worker', 'models')
        keys = []
        for index, part in enumerate(path):
            keys += [*menu_tests.select(part, path[:index]), '\n']
        keys += ['\n', curses.KEY_DOWN, '\n', curses.KEY_DOWN, '\n', '\x1b']
        keys += ['\x1b'] * (len(path) - 1) + [curses.KEY_END, curses.KEY_UP, '\n']
        edited, screen = menu_tests.ConfigMenuTests().edit(config, keys)
        self.assertEqual(edited.config.workflow.worker.models[0]['effort'], 'low')
        frames = [frame for frame in screen.frames if '/ 1 / effort' in frame]
        self.assertTrue(frames)
        self.assertNotIn('medium', '\n'.join(frames))
        self.assertNotIn('xhigh', '\n'.join(frames))

    def test_model_tuning_invalidates_prepared_routes_but_key_rotation_does_not(self):
        """A prepared route binds capabilities/defaults/env along with the endpoint."""
        config = rich_config()
        route = {'provider': 'codex', **ext.route_identity(config, 'deepseek', 'codex')}
        rotated = copy.deepcopy(config)
        rotated.external_providers['deepseek'].api_key = 'rotated'
        ext.check_route(rotated, route)
        for field, value in (('context_window', 200000), ('effort', 'max'), ('env', {'OPTION': 'changed'})):
            changed = copy.deepcopy(config)
            changed.external_providers['deepseek'].codex.models['v4-pro'][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(AgentBoxError, 'changed after prepare'):
                ext.check_route(changed, route)

    def test_managed_process_receives_model_settings_and_workflow_effort(self):
        """Managed execution uses the same metadata as an interactive ext launch."""
        config = rich_config()
        for cli in ('codex', 'claude'):
            def start(command, **kwargs):
                if cli == 'claude':
                    self.assertEqual(kwargs['env']['CLAUDE_CODE_EFFORT_LEVEL'], 'max')
                    self.assertEqual(kwargs['env']['ANTHROPIC_MODEL'], 'v4-pro[1m]')
                else:
                    path = next(arg.partition('=')[2] for arg in command if arg.startswith('model_catalog_json='))
                    rows = json.loads(Path(json.loads(path)).read_text())['models']
                    self.assertTrue(next(r for r in rows if r['slug'] == 'v4-pro')['supports_parallel_tool_calls'])
                raise OSError('test process not started')
            route = dict(provider=cli, model='v4-pro', effort='max', **ext.route_identity(config, 'deepseek', cli))
            with self.subTest(cli=cli), patch('agent_box.config.effective_config', return_value=config), \
                    patch.object(provider_runner.subprocess, 'Popen', side_effect=start), \
                    self.assertRaises(provider_runner.ProviderFailure):
                provider_runner.run(route, 'worker', Path.cwd(), 'test')

    def test_model_menu_add_default_edit_reset_delete_and_cancel_preserve_saved_config(self):
        """The structured editor must work with dynamic IDs, including names matching UI keys."""
        config = external_config()
        data = config.to_dict()
        path = ('external_providers', 'deepseek', 'codex', 'models')
        keys = []
        for index, part in enumerate(path):
            rows = config_menu.menu_keys(data, path[:index])
            keys += [curses.KEY_HOME, *[curses.KEY_DOWN] * rows.index(part), '\n']
            data = data[part]
        add = [*keys, curses.KEY_END, '\n', *'metadata', '\n', '\n', *'Nice model', '\n',
               curses.KEY_END, curses.KEY_UP, '\n']
        save = ['\x1b'] * 5 + [curses.KEY_END, curses.KEY_UP, '\n']
        edited, screen = menu_tests.ConfigMenuTests().edit(config, [*add, *save])
        endpoint = edited.config.external_providers['deepseek'].codex
        self.assertEqual(endpoint.default_model, 'metadata')
        self.assertEqual(endpoint.models['metadata']['display_name'], 'Nice model')
        self.assertNotIn('metadata', config.external_providers['deepseek'].codex.models)
        self.assertNotIn('Deepseek', '\n'.join(screen.drawn))
        self.assertEqual(config_menu.label_for('api'), 'API')
        cancelled, _ = menu_tests.ConfigMenuTests().edit(config, [*add, *['\x1b'] * 6])
        self.assertIsNone(cancelled)
        # The dynamic model has no entry in Config() defaults; reset still knows its schema.
        reset = [*add, curses.KEY_HOME, 'R', *save]
        edited, _ = menu_tests.ConfigMenuTests().edit(config, reset)
        self.assertEqual(edited.config.external_providers['deepseek'].codex.models['metadata'], model_defaults('codex'))
        delete = [*add, curses.KEY_END, '\n', curses.KEY_DOWN, '\n',
                  *['\x1b'] * 4, curses.KEY_END, curses.KEY_UP, '\n']
        edited, _ = menu_tests.ConfigMenuTests().edit(config, delete)
        self.assertNotIn('metadata', edited.config.external_providers['deepseek'].codex.models)
        self.assertEqual(edited.config.external_providers['deepseek'].codex.default_model, '')

    def test_advanced_metadata_editor_is_readable_while_environment_stays_masked(self):
        """Non-secret metadata needs visible JSON editing; env values must stay private."""
        config = rich_config()
        config.external_providers['deepseek'].codex.models['v4-pro']['env']['MODEL_OPTION'] = 'env-secret-value'
        data = config.to_dict()
        path = ('external_providers', 'deepseek', 'codex', 'models', 'v4-pro')
        keys = []
        for index, part in enumerate(path):
            rows = config_menu.menu_keys(data, path[:index])
            keys += [curses.KEY_HOME, *[curses.KEY_DOWN] * rows.index(part), '\n']
            data = data[part]
        rows = config_menu.menu_keys(data, path)
        updated = {'default_verbosity': 'high'}
        keys += [curses.KEY_HOME, *[curses.KEY_DOWN] * rows.index('auto_compact_token_limit'), '\n',
                 '\x15', *'524288', '\n',
                 curses.KEY_HOME, *[curses.KEY_DOWN] * rows.index('metadata'), '\n',
                 '\x15', *json.dumps(updated), '\n',
                 curses.KEY_HOME, *[curses.KEY_DOWN] * rows.index('env'), '\n', '\x1b',
                 *['\x1b'] * len(path), curses.KEY_END, curses.KEY_UP, '\n']
        edited, screen = menu_tests.ConfigMenuTests().edit(config, keys)
        drawn = '\n'.join(screen.drawn)
        self.assertIn(json.dumps(data['metadata']), drawn)
        self.assertIn(json.dumps(updated), drawn)
        self.assertIn('786432', drawn)
        self.assertIn('524288', drawn)
        self.assertNotIn('env-secret-value', drawn)
        self.assertEqual(edited.config.external_providers['deepseek'].codex.models['v4-pro']['metadata'], updated)
        self.assertEqual(edited.config.external_providers['deepseek'].codex.models['v4-pro']['auto_compact_token_limit'], 524288)
        self.assertNotEqual(config.external_providers['deepseek'].codex.models['v4-pro']['metadata'], updated)
