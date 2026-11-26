"""Model tuning must reach real launch artifacts without changing API ownership."""
import copy
import curses
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_box import api_models, config_menu, external_launch as ext, provider_runner, runtime, workflow
from agent_box.config import Config, load_config, save_config
from agent_box.errors import AgentBoxError
from agent_box.external_config import effective_model, external_model_entry, model_defaults, validate_connections
from agent_box.workspace import Workspace
import test_config_menu as menu_tests
from test_external_config import external_config
from test_api_models import sample


def rich_config():
    data = external_config().to_dict()
    provider = data['agents']['external_apis']['deepseek']
    for cli in ('codex', 'claude'):
        provider[cli].update(default_model='v4-pro', env={'MODEL_OPTION': 'endpoint', 'ENDPOINT_ONLY': 'private-value'})
    provider['models'] = {'v4-flash': {}, 'v4-pro': dict(
        display_name='V4 Pro', effort='high', supported_efforts=['low', 'high', 'max'], context_window=1048576,
        input_modalities=['text', 'image'],
        codex=dict(auto_compact_token_limit=786432, env={'MODEL_OPTION': 'model'}, supports_parallel_tool_calls=True,
                   supports_reasoning_summaries=True, metadata={'support_verbosity': True, 'default_verbosity': 'low'}),
        claude=dict(auto_compact_token_limit=786432, env={'MODEL_OPTION': 'model'}))}
    return Config.from_dict(data)


class ExternalModelTests(unittest.TestCase):
    def test_discovered_limits_survive_storage_and_reach_each_clients_supported_settings(self):
        """Nonstandard windows must not be rounded or confused with output and compaction limits."""
        for context, output in ((32000, 4096), (400000, 65536), (1000000, 128000), (2000000, 393216)):
            with self.subTest(context=context), tempfile.TemporaryDirectory() as tmp:
                fields = api_models.model_fields({'id': 'custom', 'context_window': context,
                                                  'max_output_tokens': output})
                raw = external_config().to_dict()
                provider = raw['agents']['external_apis']['deepseek']
                provider['models'] = {'custom': {**fields, 'claude': {'auto_compact_token_limit': context // 2}}}
                for cli in ('claude', 'codex'):
                    provider[cli]['default_model'] = 'custom'
                path = Path(tmp) / 'config.json'
                save_config(Config.from_dict(raw), path)
                config = load_config(path)
                for cli in ('claude', 'codex'):
                    command, env = ext.prepare([cli], 'deepseek', cli, config, Path(tmp) / cli, {})
                    if cli == 'claude':
                        self.assertEqual(env['ANTHROPIC_MODEL'], 'custom')
                        self.assertEqual(env['CLAUDE_CODE_MAX_CONTEXT_TOKENS'], str(context))
                        self.assertEqual(env['CLAUDE_CODE_MAX_OUTPUT_TOKENS'], str(min(32000, output)))
                        self.assertEqual(env['CLAUDE_CODE_AUTO_COMPACT_WINDOW'], str(context // 2))
                        settings = json.loads((Path(tmp) / cli / 'claude-settings.json').read_text())
                        self.assertEqual(settings['env']['CLAUDE_CODE_MAX_CONTEXT_TOKENS'], str(context))
                        self.assertNotIn('behavesAs', str(settings))
                        self.assertNotIn('modelOverrides', settings)
                    else:
                        row = json.loads((Path(tmp) / cli / 'models.json').read_text())['models'][0]
                        self.assertEqual(row['context_window'], context)
                        self.assertNotIn('max_output_tokens', row)

    def test_explicit_native_limits_win_and_model_environment_does_not_leak_to_nested_launches(self):
        """Discovery is a default; configured env and explicit suffixes retain their own meaning."""
        raw = rich_config().to_dict()
        provider = raw['agents']['external_apis']['deepseek']
        provider['models']['v4-pro']['max_output_tokens'] = 4096
        provider['claude']['env']['CLAUDE_CODE_MAX_CONTEXT_TOKENS'] = '500000'
        provider['models']['v4-pro']['claude']['env'].update(
            CLAUDE_CODE_MAX_CONTEXT_TOKENS='600000', CLAUDE_CODE_MAX_OUTPUT_TOKENS='2048')
        config = Config.from_dict(raw)
        with tempfile.TemporaryDirectory() as tmp:
            original = {'CLAUDE_CODE_MAX_CONTEXT_TOKENS': '700000', 'CLAUDE_CODE_MAX_OUTPUT_TOKENS': '1024'}
            _, inherited = ext.prepare(['claude'], 'deepseek', 'claude', rich_config(),
                                       Path(tmp) / 'inherited', original)
            self.assertEqual(inherited['CLAUDE_CODE_MAX_CONTEXT_TOKENS'], '700000')
            self.assertEqual(inherited['CLAUDE_CODE_MAX_OUTPUT_TOKENS'], '1024')
            self.assertEqual(ext.builtin_environment(inherited), original)
            _, env = ext.prepare(['claude'], 'deepseek', 'claude', config, Path(tmp) / 'parent', {})
            self.assertEqual(env['CLAUDE_CODE_MAX_CONTEXT_TOKENS'], '600000')
            self.assertEqual(env['CLAUDE_CODE_MAX_OUTPUT_TOKENS'], '2048')
            self.assertEqual(ext.builtin_environment(env).get('CLAUDE_CODE_MAX_CONTEXT_TOKENS'), None)
            # A different connection without explicit env cannot inherit the parent's model limits.
            config = external_config()
            _, child = ext.prepare(['claude', '--model', 'manual'], 'deepseek', 'claude', config,
                                   Path(tmp) / 'child', env)
            for name in ('CLAUDE_CODE_MAX_CONTEXT_TOKENS', 'CLAUDE_CODE_MAX_OUTPUT_TOKENS',
                         'CLAUDE_CODE_AUTO_COMPACT_WINDOW'):
                self.assertFalse(child.get(name))
            command, tagged = ext.prepare(['claude', '--model', 'v4-pro[1m]'], 'deepseek', 'claude',
                                          rich_config(), Path(tmp) / 'tagged', {})
            self.assertIn('v4-pro[1m]', command)
            self.assertFalse(tagged.get('CLAUDE_CODE_MAX_CONTEXT_TOKENS'))

    def test_router_defaults_stay_unknown_but_explicit_limits_are_preserved(self):
        """Routing metadata must not manufacture fixed capabilities or erase deliberate user settings."""
        fields = api_models.model_fields({'id': 'openrouter/free', 'context_length': 200000,
                                          'input_modalities': ['text', 'image'], 'max_output_tokens': 8192})
        raw = external_config().to_dict()
        provider = raw['agents']['external_apis']['deepseek']
        provider['models'] = {'openrouter/free': fields}
        for cli in ('claude', 'codex'):
            provider[cli]['default_model'] = 'openrouter/free'
        with tempfile.TemporaryDirectory() as tmp:
            config = Config.from_dict(raw)
            _, env = ext.prepare(['claude'], 'deepseek', 'claude', config, Path(tmp) / 'unknown', {})
            self.assertFalse(env.get('CLAUDE_CODE_MAX_CONTEXT_TOKENS'))
            ext.prepare(['codex'], 'deepseek', 'codex', config, Path(tmp) / 'codex', {})
            row = json.loads((Path(tmp) / 'codex/models.json').read_text())['models'][0]
            self.assertIsNone(row['context_window'])
            self.assertEqual(row['input_modalities'], ['text'])
            self.assertFalse(row['supports_parallel_tool_calls'])
            provider['models']['openrouter/free']['context_window'] = 64000
            _, env = ext.prepare(['claude'], 'deepseek', 'claude', Config.from_dict(raw),
                                 Path(tmp) / 'explicit', {})
            self.assertEqual(env['CLAUDE_CODE_MAX_CONTEXT_TOKENS'], '64000')

    def test_custom_patch_tools_require_explicit_external_model_opt_in(self):
        """Function-only APIs must not receive a custom tool just because Codex supports it."""
        for metadata, expected in (({}, None), ({'apply_patch_tool_type': 'freeform'}, 'freeform'),
                                   ({'apply_patch_tool_type': None}, None)):
            with self.subTest(metadata=metadata), tempfile.TemporaryDirectory() as tmp:
                data = rich_config().to_dict()
                data['agents']['external_apis']['deepseek']['models']['v4-pro']['codex']['metadata'].update(metadata)
                config = Config.from_dict(data)
                save_config(config, Path(tmp) / 'config.json')
                loaded = load_config(Path(tmp) / 'config.json')
                ext.prepare(['codex'], 'deepseek', 'codex', loaded, Path(tmp) / 'launch', {})
                rows = json.loads((Path(tmp) / 'launch/models.json').read_text())['models']
                self.assertEqual(next(row for row in rows if row['slug'] == 'v4-pro')['apply_patch_tool_type'], expected)
                ext.prepare(['codex', '--model', 'manual-model'], 'deepseek', 'codex', loaded, Path(tmp) / 'manual', {})
                rows = json.loads((Path(tmp) / 'manual/models.json').read_text())['models']
                self.assertIsNone(next(row for row in rows if row['slug'] == 'manual-model')['apply_patch_tool_type'])

    def test_invalid_effort_stops_host_and_container_before_launch_side_effects(self):
        """Known unsupported effort must not build a box or render session credentials."""
        config = rich_config()
        for cli, options in (('claude', ['--effort', 'medium']), ('codex', ['-cmodel_reasoning_effort=medium'])):
            for host in (True, False):
                with self.subTest(cli=cli, host=host), \
                        patch.object(runtime.host_podman, 'require'), \
                        patch.object(runtime.shutil, 'which', return_value='/usr/bin/' + cli), \
                        patch.object(runtime, 'resolve_workspace', return_value=Workspace(Path.cwd(), Path.cwd())), \
                        patch.object(runtime, 'launch_config', return_value=config), \
                        patch.object(runtime, 'ensure_state_dirs'), patch.object(runtime, 'ensure_refresh_token'), \
                        patch.object(runtime, 'ensure_workspace_image', side_effect=AssertionError('Image preparation reached')) as image, \
                        patch.object(runtime, 'session_runtime', side_effect=AssertionError('Snapshot rendering reached')) as snapshot, \
                        patch.object(runtime, 'existing_workspace_spec', return_value=None), \
                        patch.object(runtime, 'launch_in_workspace_container') as container, \
                        patch.object(runtime.subprocess, 'call') as call:
                    with self.assertRaisesRegex(AgentBoxError, 'not supported'):
                        if host:
                            runtime.host_main(cli, options, api_provider='deepseek')
                        else:
                            runtime.runtime_main([cli, *options], runtime.PACKAGE_ROOT, api_provider='deepseek')
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
                        self.assertEqual(env['ANTHROPIC_MODEL'], 'v4-pro')
                        self.assertEqual(env['CLAUDE_CODE_EFFORT_LEVEL'], 'high')
                        self.assertEqual(env['CLAUDE_CODE_AUTO_COMPACT_WINDOW'], '786432')
                        self.assertIn({'model': 'v4-pro', 'label': 'V4 Pro'}, settings['modelPicker']['options'])

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
                    self.assertEqual(command[command.index('--model') + 1], 'v4-pro')
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
        config.agents.external_apis['deepseek'].claude.env['CLAUDE_CODE_SUBAGENT_MODEL'] = 'auxiliary-model'
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
        candidate = dict(provider='codex', model='v4-pro', effort='medium', speed='inherit', api_provider='deepseek')
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
                workflow.candidates(config, 'worker', 1)
            candidate['effort'] = 'default'
            self.assertEqual(validate_connections(config), [])
            selected = workflow.candidates(config, 'worker', 1)
            self.assertEqual(selected['effort'], 'default')

    def test_invalid_model_metadata_and_owned_environment_fail_privately(self):
        """Bad tuning must be rejected at load time without echoing supplied values."""
        bad = [{'effort': 'private-value'}, {'supported_efforts': [['private-value']]},
               {'max_output_tokens': True}, {'max_output_tokens': -1},
               {'supported_efforts': ['low'], 'effort': 'high'}, {'context_window': True},
               {'context_window': -1}, {'context_window': 100, 'codex': {'auto_compact_token_limit': 100}},
               {'context_window': 400000, 'claude': {'auto_compact_token_limit': 400000}},
               {'input_modalities': ['audio']}, {'input_modalities': ['text', 'text']}, {'codex': 'private-value'},
               {'codex': {'supports_parallel_tool_calls': 1}}, {'claude': {'metadata': {}}},
               {'codex': {'metadata': {'slug': 'private-value'}}}, {'codex': {'metadata': {'x': float('nan')}}},
               {'claude': {'env': {'ANTHROPIC_BASE_URL': 'private-value'}}},
               {'codex': {'env': {'AGENT_BOX_API_PROVIDER': 'private-value'}}},
               {'codex': {'env': {'BAD=NAME': 'private-value'}}}, {'claude': {'env': {'OPTION': 1}}}]
        for spec in bad:
            with self.subTest(fields=spec), self.assertRaises(AgentBoxError) as error:
                Config.from_dict({'agents': {'external_apis': {'test': {'models': {'model': spec}}}}})
            self.assertNotIn('private-value', str(error.exception))
        for model in ('v4[1m]', 'v4[garbage]'):
            with self.subTest(model=model), self.assertRaises(AgentBoxError):
                Config.from_dict({'agents': {'external_apis': {'test': {'models': {model: {}}}}}})
        with self.assertRaises(AgentBoxError):
            Config.from_dict({'agents': {'external_apis': {'test': {'models': ['model']}}}})

    def test_models_are_listed_once_for_both_clis(self):
        """A per-CLI model list is the form this schema replaced; it must not load beside the shared one."""
        for cli in ('codex', 'claude'):
            with self.subTest(cli=cli), self.assertRaises(AgentBoxError):
                Config.from_dict({'agents': {'external_apis': {'test': {cli: {'models': {'v4': {}}}}}}})
        saved = rich_config().to_dict()['agents']['external_apis']['deepseek']
        self.assertEqual(set(saved['models']), {'v4-flash', 'v4-pro'})
        self.assertNotIn('models', saved['codex'])
        self.assertNotIn('models', saved['claude'])

    def test_custom_headers_cannot_replace_the_configured_api_credentials(self):
        """Claude custom headers can carry auth, so neither env level may set them."""
        for cli in ('codex', 'claude'):
            for source in ('endpoint', 'model'):
                for header in ('Authorization: Bearer private-value', 'x-api-key: private-value'):
                    data = rich_config().to_dict()
                    provider = data['agents']['external_apis']['deepseek']
                    target = provider[cli] if source == 'endpoint' else provider['models']['v4-pro'][cli]
                    target['env']['ANTHROPIC_CUSTOM_HEADERS'] = header
                    with self.subTest(cli=cli, source=source, header=header.split(':')[0]):
                        with self.assertRaises(AgentBoxError) as raised:
                            Config.from_dict(data)
                        self.assertNotIn('private-value', str(raised.exception))

    def test_claude_window_and_effort_are_derived_from_the_shared_model(self):
        """Both clients keep exact windows; Claude still uses only its supported effort vocabulary."""
        models = {'wide': {'context_window': 1000000, 'supported_efforts': ['minimal', 'high'], 'effort': 'minimal'},
                  'mid': {'context_window': 400000, 'claude': {'auto_compact_token_limit': 150000}},
                  'small': {'context_window': 32000}, 'narrow': {'supported_efforts': ['minimal']}}
        config = Config.from_dict({'agents': {'external_apis': {'test': {
            'enabled': True, 'api_key': 'k', 'models': models,
            'codex': {'url': 'https://api.example/v1'}, 'claude': {'url': 'https://api.example/anthropic'}}}}})
        provider = config.agents.external_apis['test']
        self.assertEqual({m: s['context_window'] for m, s in provider.codex.models.items()},
                         {'wide': 1000000, 'mid': 400000, 'small': 32000, 'narrow': 0})
        self.assertEqual({m: s['context_window'] for m, s in provider.claude.models.items()},
                         {'wide': 1000000, 'mid': 400000, 'small': 32000, 'narrow': 0})
        # A declaration Claude shares no level with still restricts it, to Claude's default.
        tree = config.to_dict()
        for model, offered in (('wide', ('default', 'high')), ('narrow', ('default',)), ('small', ('default', 'low', 'medium', 'high', 'xhigh', 'max'))):
            spec = {'provider': 'claude', 'api_provider': 'test', 'model': model, 'effort': 'default'}
            self.assertEqual(config_menu.candidate_efforts(spec, tree), offered)
        for model, effort in (('narrow', 'high'), ('wide', 'medium')):
            with self.subTest(model=model), tempfile.TemporaryDirectory() as temp, \
                    self.assertRaisesRegex(AgentBoxError, 'not supported'):
                ext.prepare(['claude', '--model', model, '--effort', effort], 'test', 'claude', config, temp, {})
        with tempfile.TemporaryDirectory() as temp:
            command, env = ext.prepare(['claude', '--model', 'wide'], 'test', 'claude', config, temp, {})
            self.assertEqual(command[command.index('--model') + 1], 'wide')
            self.assertFalse(env['CLAUDE_CODE_EFFORT_LEVEL'])
            command, _ = ext.prepare(['codex', '--model', 'wide'], 'test', 'codex', config, temp, {})
            self.assertIn('model_reasoning_effort="minimal"', command)

    def test_claude_environment_effort_is_validated_at_load_with_typed_priority(self):
        """A configured default must work without requiring an explicit effort to repair it."""
        for source in ('endpoint', 'model'):
            for effort in ('medium', 'ultra', 'private-value'):
                data = rich_config().to_dict()
                provider = data['agents']['external_apis']['deepseek']
                model = provider['models']['v4-pro']
                model['effort'] = 'default'
                target = provider['claude'] if source == 'endpoint' else model['claude']
                target['env']['CLAUDE_CODE_EFFORT_LEVEL'] = effort
                with self.subTest(source=source, effort=effort), self.assertRaises(AgentBoxError) as raised:
                    Config.from_dict(data)
                self.assertNotIn('private-value', str(raised.exception))
        data = rich_config().to_dict()
        provider = data['agents']['external_apis']['deepseek']
        provider['claude']['env']['CLAUDE_CODE_EFFORT_LEVEL'] = 'medium'
        # v4-pro's typed high wins; v4-flash has unknown supported levels.
        loaded = Config.from_dict(data)
        with tempfile.TemporaryDirectory() as temp:
            _, env = ext.prepare(['claude'], 'deepseek', 'claude', loaded, temp, {})
            self.assertEqual(env['CLAUDE_CODE_EFFORT_LEVEL'], 'high')
        provider['models']['v4-pro']['effort'] = 'ultra'
        with self.assertRaises(AgentBoxError):
            Config.from_dict(data)

    def test_claudes_manual_model_env_effort_survives_discovery_and_codex_uses_the_published_preference(self):
        """Discovery must retain an intentional per-client preference and still honor an explicit launch override."""
        data = external_config().to_dict()
        provider = data['agents']['external_apis']['deepseek']
        provider['models']['deepseek-flash'] = external_model_entry()
        provider['models']['deepseek-flash']['claude']['env']['CLAUDE_CODE_EFFORT_LEVEL'] = 'low'
        published = api_models.model_fields(sample('deepseek.json')['data'][0])
        with tempfile.TemporaryDirectory() as tmp:
            for refreshed in (False, True):
                if refreshed:
                    config_menu.sync_models('deepseek', provider, {'deepseek-flash': published})
                loaded = Config.from_dict(data)
                _, env = ext.prepare(['claude', '--model', 'deepseek-flash'], 'deepseek', 'claude', loaded, tmp, {})
                self.assertEqual(env['CLAUDE_CODE_EFFORT_LEVEL'], 'low')
            args, _ = ext.prepare(['codex', '--model', 'deepseek-flash'], 'deepseek', 'codex', loaded, tmp, {})
            self.assertIn('model_reasoning_effort="high"', args)
            _, env = ext.prepare(['claude', '--model', 'deepseek-flash', '--effort', 'max'],
                                 'deepseek', 'claude', loaded, tmp, {})
            self.assertEqual(env['CLAUDE_CODE_EFFORT_LEVEL'], 'max')

    def test_native_endpoint_effort_only_applies_to_models_that_can_use_it(self):
        """One model's published limits must not block syncing its siblings or leak an invalid effort into launch."""
        data = external_config().to_dict()
        provider = data['agents']['external_apis']['deepseek']
        provider['claude']['env']['CLAUDE_CODE_EFFORT_LEVEL'] = 'high'
        provider['codex']['config']['model_reasoning_effort'] = 'high'
        config_menu.sync_models('deepseek', provider, {
            'fixed': api_models.model_fields(sample('anthropic-page2.json')['data'][0]),
            'limited': {'supported_efforts': ['low']}, 'accepts-high': {'supported_efforts': ['high']}, 'unknown': {}})
        config = Config.from_dict(data)
        with tempfile.TemporaryDirectory() as tmp:
            for cli in ('claude', 'codex'):
                for model in ('fixed', 'limited', 'accepts-high', 'unknown', 'unlisted'):
                    options = ['--model', model]
                    self.assertEqual(ext.selected_model(config, 'deepseek', cli, options), model)
                    args, env = ext.prepare([cli, *options], 'deepseek', cli, config, tmp, {})
                    allowed = model not in ('fixed', 'limited')
                    if cli == 'claude':
                        self.assertEqual(env['CLAUDE_CODE_EFFORT_LEVEL'], 'high' if allowed else 'auto')
                    else:
                        self.assertEqual('model_reasoning_effort="high"' in args, allowed)
                    if not allowed:
                        explicit = ['--effort', 'high'] if cli == 'claude' else ['-cmodel_reasoning_effort=high']
                        with self.assertRaisesRegex(AgentBoxError, 'not supported'):
                            ext.prepare([cli, *options, *explicit], 'deepseek', cli, config, tmp, {})

    def test_literal_default_keeps_a_claude_model_environment_preference_on_sync(self):
        """A saved neutral typed preference delegates to the model's native Claude setting."""
        data = external_config().to_dict()
        p = data['agents']['external_apis']['deepseek']
        p['models']['external-small'].update(effort='default', supported_efforts=None)
        p['models']['external-small']['claude']['env']['CLAUDE_CODE_EFFORT_LEVEL'] = 'high'
        with tempfile.TemporaryDirectory() as tmp:
            for levels in (['low'], [], ['low', 'high']):
                config_menu.sync_models('deepseek', p, {'external-small': {'supported_efforts': levels}})
                config = Config.from_dict(data)
                for cli in ('claude', 'codex'):
                    model = getattr(config.agents.external_apis['deepseek'], cli).models['external-small']
                    if cli == 'claude':
                        self.assertEqual(model['effort'], 'high')
                        _, env = ext.prepare([cli], 'deepseek', cli, config, tmp, {})
                        self.assertEqual(env['CLAUDE_CODE_EFFORT_LEVEL'], 'high')
                    else:
                        self.assertEqual(model['supported_efforts'], levels)
                        self.assertEqual(model['effort'], 'default')

    def test_suffixed_workflow_model_uses_the_same_effort_choices_as_launch(self):
        """A manually entered [1m] ID must not lose the configured model's effort metadata."""
        config = rich_config()
        config.workflow.worker.models = [dict(provider='claude', api_provider='deepseek', model='v4-pro[1m]',
                                              effort='default', speed='inherit')]
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
        rotated.agents.external_apis['deepseek'].api_key = 'rotated'
        ext.check_route(rotated, route)
        for field, value in (('context_window', 2000000), ('effort', 'max'), ('codex', {'env': {'OPTION': 'changed'}})):
            data = config.to_dict()
            data['agents']['external_apis']['deepseek']['models']['v4-pro'][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(AgentBoxError, 'changed after prepare'):
                ext.check_route(Config.from_dict(data), route)

    def test_managed_process_receives_model_settings_and_workflow_effort(self):
        """Managed execution uses the same metadata as an interactive ext launch."""
        config = rich_config()
        for cli in ('codex', 'claude'):
            def start(command, **kwargs):
                if cli == 'claude':
                    self.assertEqual(kwargs['env']['CLAUDE_CODE_EFFORT_LEVEL'], 'max')
                    self.assertEqual(kwargs['env']['ANTHROPIC_MODEL'], 'v4-pro')
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
        path = ('agents', 'external_apis', 'deepseek', 'models')
        keys = []
        for index, part in enumerate(path):
            rows = config_menu.menu_keys(data, path[:index])
            keys += [curses.KEY_HOME, *[curses.KEY_DOWN] * rows.index(part), '\n']
            data = data[part]
        add = [*keys, curses.KEY_END, '\n', *'metadata', '\n', '\n', *'Nice model', '\n',
               curses.KEY_END, curses.KEY_UP, '\n']
        save = ['\x1b'] * 5 + [curses.KEY_END, curses.KEY_UP, '\n']
        edited, screen = menu_tests.ConfigMenuTests().edit(config, [*add, *save])
        provider = edited.config.agents.external_apis['deepseek']
        self.assertEqual((provider.codex.default_model, provider.claude.default_model), ('metadata', 'metadata'))
        self.assertEqual(provider.models['metadata']['display_name'], 'Nice model')
        self.assertEqual(provider.claude.models['metadata']['display_name'], 'Nice model')
        self.assertNotIn('metadata', config.agents.external_apis['deepseek'].models)
        self.assertNotIn('Deepseek', '\n'.join(screen.drawn))
        self.assertEqual(config_menu.label_for('api'), 'API')
        cancelled, _ = menu_tests.ConfigMenuTests().edit(config, [*add, *['\x1b'] * 6])
        self.assertIsNone(cancelled)
        # The dynamic model has no entry in Config() defaults; reset still knows its schema.
        reset = [*add, curses.KEY_HOME, 'R', *save]
        edited, _ = menu_tests.ConfigMenuTests().edit(config, reset)
        self.assertEqual(edited.config.agents.external_apis['deepseek'].models['metadata'], external_model_entry())
        delete = [*add, curses.KEY_END, '\n', curses.KEY_DOWN, '\n',
                  *['\x1b'] * 4, curses.KEY_END, curses.KEY_UP, '\n']
        edited, _ = menu_tests.ConfigMenuTests().edit(config, delete)
        provider = edited.config.agents.external_apis['deepseek']
        self.assertNotIn('metadata', provider.models)
        self.assertEqual((provider.codex.default_model, provider.claude.default_model), ('', ''))

    def test_fixed_vocabulary_model_lists_are_toggled_as_checkboxes(self):
        """Supported efforts and input types hold known names, so they are ticked rather than typed as JSON."""
        config = rich_config()
        data = config.to_dict()
        path = ('agents', 'external_apis', 'deepseek', 'models', 'v4-pro')
        keys = []
        for index, part in enumerate(path):
            rows = config_menu.menu_keys(data, path[:index])
            keys += [curses.KEY_HOME, *[curses.KEY_DOWN] * rows.index(part), '\n']
            data = data[part]
        rows = config_menu.menu_keys(data, path)
        efforts = config_menu.list_vocabulary((*path, 'supported_efforts'))
        keys += [curses.KEY_HOME, *[curses.KEY_DOWN] * rows.index('supported_efforts'), '\n',
                 curses.KEY_HOME, *[curses.KEY_DOWN] * efforts.index('medium'), ' ', '\n',
                 curses.KEY_HOME, *[curses.KEY_DOWN] * rows.index('input_modalities'), '\n',
                 curses.KEY_END, ' ', '\n',
                 *['\x1b'] * len(path), curses.KEY_END, curses.KEY_UP, '\n']
        edited, screen = menu_tests.ConfigMenuTests().edit(config, keys)
        model = edited.config.agents.external_apis['deepseek'].models['v4-pro']
        self.assertEqual(model['supported_efforts'], ['low', 'medium', 'high', 'max'])
        self.assertEqual(model['input_modalities'], ['text'])
        self.assertTrue(any('[x] medium' in frame and '[ ] none' in frame for frame in screen.frames))

    def test_advanced_metadata_editor_is_readable_while_environment_stays_masked(self):
        """Non-secret metadata needs visible JSON editing; env values must stay private."""
        config = rich_config()
        config.agents.external_apis['deepseek'].models['v4-pro']['codex']['env']['MODEL_OPTION'] = 'env-secret-value'
        data = config.to_dict()
        path = ('agents', 'external_apis', 'deepseek', 'models', 'v4-pro', 'codex')
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
        saved = edited.config.agents.external_apis['deepseek'].models['v4-pro']['codex']
        self.assertEqual(saved['metadata'], updated)
        self.assertEqual(saved['auto_compact_token_limit'], 524288)
        self.assertNotEqual(config.agents.external_apis['deepseek'].models['v4-pro']['codex']['metadata'], updated)

    def walk(self, config, path):
        """Keys that open ``path`` from the menu root."""
        data, keys = config.to_dict(), []
        for index, part in enumerate(path):
            keys += [curses.KEY_HOME, *[curses.KEY_DOWN] * config_menu.menu_keys(data, path[:index]).index(part), '\n']
            data = data[part]
        return keys

    def test_provider_list_adds_a_searched_model_with_the_settings_it_states(self):
        """Typing narrows a long catalog; the chosen model arrives filled in, configured ones are not offered again."""
        listed = {item['id']: api_models.model_fields(item) for item in sample('deepseek.json')['data']}
        listed['external-small'] = {'display_name': 'Small'}
        config = external_config()
        keys = [*self.walk(config, ('agents', 'external_apis', 'deepseek', 'models')),
                curses.KEY_END, curses.KEY_UP, '\n', *'v4-p', '\n',
                *['\x1b'] * 5, curses.KEY_END, curses.KEY_UP, '\n']
        edited, screen = menu_tests.ConfigMenuTests().edit(config, keys, published=(listed, None))
        provider = edited.config.agents.external_apis['deepseek']
        self.assertEqual(effective_model(provider.models['deepseek-v4-pro'], provider.model_defaults),
                         {**model_defaults(), **listed['deepseek-v4-pro']})
        self.assertEqual(provider.claude.models['deepseek-v4-pro']['context_window'], 1048576)
        self.assertNotIn('deepseek-flash', provider.models)
        lines = [line for frame in screen.frames for line in frame.splitlines()]
        self.assertTrue(any('external-large' in line and 'not listed' in line for line in lines))
        self.assertFalse(any('external-small' in line and 'not listed' in line for line in lines))
        filtered = [frame for frame in screen.frames if '[filter: v4-p]' in frame]
        self.assertTrue(filtered)
        self.assertFalse(any('DeepSeek-V4.1-Flash' in frame or 'Small [' in frame for frame in filtered))

    def test_update_from_api_replaces_only_the_fields_the_list_states(self):
        """Hand-tuned CLI settings must survive refreshing what the provider describes."""
        config = rich_config()
        listed = {'v4-pro': {'display_name': 'V4 Pro (renamed)', 'context_window': 2000000}}
        keys = [*self.walk(config, ('agents', 'external_apis', 'deepseek', 'models', 'v4-pro')),
                curses.KEY_END, curses.KEY_UP, curses.KEY_UP, '\n',
                *['\x1b'] * 5, curses.KEY_END, curses.KEY_UP, '\n']
        edited, screen = menu_tests.ConfigMenuTests().edit(config, keys, published=(listed, None))
        before = config.agents.external_apis['deepseek'].models['v4-pro']
        after = edited.config.agents.external_apis['deepseek'].models['v4-pro']
        self.assertEqual(effective_model(after, {}), {**effective_model(before, {}), **listed['v4-pro']})
        self.assertEqual(after['codex']['auto_compact_token_limit'], 786432)
        self.assertIn('Updated from the provider list', '\n'.join(screen.frames))

    def test_an_unreachable_provider_leaves_manual_entry_and_says_why(self):
        """A network failure is a diagnostic in the menu, never a crash or a lost edit."""
        with patch.object(api_models, 'http_json', side_effect=OSError('unreachable')):
            published = api_models.load(external_config().to_dict()['agents']['external_apis']['deepseek'])
        config = external_config()
        keys = [*self.walk(config, ('agents', 'external_apis', 'deepseek', 'models')),
                curses.KEY_END, '\n', *'by-hand', '\n',
                *['\x1b'] * 5, curses.KEY_END, curses.KEY_UP, '\n']
        edited, screen = menu_tests.ConfigMenuTests().edit(config, keys, published=published)
        self.assertIn('by-hand', edited.config.agents.external_apis['deepseek'].models)
        drawn = '\n'.join(screen.frames)
        self.assertIn('network error or timeout', drawn)
        self.assertIn('Manual model entry still works', drawn)
        self.assertNotIn('Add from provider list', drawn)
        self.assertNotIn('not listed', drawn)
        self.assertNotIn('private-api-key', drawn)

    def test_single_model_update_drops_the_obsolete_published_effort_default(self):
        """A new effort list with no preferred value must be editable and saveable from the menu."""
        raw = rich_config().to_dict()
        raw['agents']['external_apis']['deepseek']['models']['v4-pro'].update(
            effort=None, supported_efforts=None, discovered={'effort': 'high', 'supported_efforts': ['high']})
        config = Config.from_dict(raw)
        keys = [*self.walk(config, ('agents', 'external_apis', 'deepseek', 'models', 'v4-pro')),
                curses.KEY_END, curses.KEY_UP, curses.KEY_UP, '\n',
                *['\x1b'] * 5, curses.KEY_END, curses.KEY_UP, '\n']
        edited, _ = menu_tests.ConfigMenuTests().edit(config, keys, published=({'v4-pro': {'supported_efforts': ['low']}}, None))
        provider = edited.config.agents.external_apis['deepseek']
        self.assertEqual(provider.models['v4-pro']['discovered'], {'supported_efforts': ['low']})
        self.assertEqual(provider.claude.models['v4-pro']['effort'], 'default')
        edited, _ = menu_tests.ConfigMenuTests().edit(edited.config, keys, published=({'v4-pro': {'effort': 'high'}}, None))
        provider = edited.config.agents.external_apis['deepseek']
        self.assertEqual(provider.models['v4-pro']['discovered'], {'effort': 'high'})
        self.assertEqual(provider.codex.models['v4-pro']['effort'], 'high')

    def test_a_manual_effort_survives_an_id_only_refresh_with_conflicting_connection_levels(self):
        """When an explicit level survives discovery loss, unrelated fallback levels cannot invalidate it."""
        raw = external_config().to_dict()
        provider = raw['agents']['external_apis']['deepseek']
        provider['model_defaults'].update(supported_efforts=['low'], effort='low')
        provider['models']['external-small'].update(effort='high', discovered={'supported_efforts': ['high']})
        config_menu.sync_models('deepseek', provider, {'external-small': {}})
        provider = Config.from_dict(raw).agents.external_apis['deepseek']
        for endpoint in (provider.claude, provider.codex):
            spec = endpoint.models['external-small']
            self.assertEqual((spec['effort'], spec['supported_efforts']), ('high', ['low', 'high']))

    def test_a_manual_list_can_restore_the_preferred_effort_from_connection_defaults(self):
        """The final manual list supersedes a published list that temporarily excluded the connection effort."""
        spec = external_model_entry()
        spec.update(supported_efforts=['high'], discovered={'supported_efforts': ['low']})
        effective = effective_model(spec, {'effort': 'high', 'supported_efforts': ['low', 'high']})
        self.assertEqual((effective['effort'], effective['supported_efforts']), ('high', ['high']))

    def test_a_preferred_model_exception_preserves_levels_still_used_by_workflow(self):
        """A manual high preference adds an exception without removing the API's low option."""
        raw = external_config().to_dict()
        p = raw['agents']['external_apis']['deepseek']
        p['models']['external-small'].update(effort='high', discovered={'supported_efforts': ['low', 'high']})
        raw['workflow']['worker']['models'] = [dict(provider='codex', api_provider='deepseek', model='external-small',
                                                   effort='low', speed='inherit')]
        config_menu.sync_models('deepseek', p, {'external-small': {'supported_efforts': ['low']}}, workflow=raw['workflow'])
        model = Config.from_dict(raw).agents.external_apis['deepseek'].codex.models['external-small']
        self.assertEqual((model['effort'], model['supported_efforts']), ('high', ['low', 'high']))

    def test_explicit_absence_of_an_effort_control_is_honored_by_both_launchers_and_pickers(self):
        """A model listed without an effort control accepts only default until the operator overrides that capability."""
        raw = external_config().to_dict()
        p = raw['agents']['external_apis']['deepseek']
        p['model_defaults']['effort'] = 'high'
        rows = {'fixed': api_models.model_fields(sample('anthropic-page2.json')['data'][0]), 'unknown': {}}
        config_menu.sync_models('deepseek', p, rows)
        config = Config.from_dict(raw)
        path = ('agents', 'external_apis', 'deepseek', 'models', 'fixed', 'effort')
        self.assertEqual(config_menu.value_choices(path, p['models']['fixed'], raw), ('default',))
        for cli in ('claude', 'codex'):
            candidate = dict(provider=cli, model='fixed', api_provider='deepseek', effort='default')
            self.assertEqual(config_menu.candidate_efforts(candidate, raw), ('default',))
            candidate['model'] = 'unknown'
            self.assertIn('high', config_menu.candidate_efforts(candidate, raw))
            args = ['--effort', 'high'] if cli == 'claude' else ['-c', 'model_reasoning_effort="high"']
            with self.assertRaisesRegex(AgentBoxError, 'not supported'):
                ext.selected_model(config, 'deepseek', cli, ['--model', 'fixed', *args])
            self.assertEqual(ext.selected_model(config, 'deepseek', cli, ['--model', 'fixed']), 'fixed')
        p['models']['fixed']['supported_efforts'] = ['high']
        self.assertIn('high', config_menu.value_choices(path, p['models']['fixed'], raw))
        connection = Config.from_dict(raw).agents.external_apis['deepseek']
        for endpoint in (connection.codex, connection.claude):
            self.assertEqual(endpoint.models['fixed']['effort'], 'high')

    def test_workflow_picker_uses_the_id_for_a_deliberately_cleared_api_display_name(self):
        """An empty manual label selects the model ID everywhere, including the searchable role picker."""
        config = rich_config()
        model = config.agents.external_apis['deepseek'].models['v4-pro']
        model.update(display_name='', discovered={'display_name': 'Old API label'})
        config.workflow.worker.models = [dict(provider='codex', api_provider='deepseek', model='v4-pro',
                                             effort='default', speed='inherit')]
        path = ('workflow', 'worker', 'models')
        keys = [*self.walk(config, path), '\n', '\n', *'v4-pro', *['\x1b'] * 8]
        _, screen = menu_tests.ConfigMenuTests().edit(config, keys)
        self.assertIn('v4-pro [codex@deepseek: v4-pro] (current)', screen.drawn)
        self.assertIn('v4-pro [claude@deepseek: v4-pro]', screen.drawn)

    def test_sync_rejects_newly_incompatible_workflow_efforts_before_changing_the_draft(self):
        """Save should still succeed with the old catalog after an API list conflicts with a role candidate."""
        config = rich_config()
        provider = config.agents.external_apis['deepseek']
        raw = provider.models['v4-pro']
        raw.update(effort=None, supported_efforts=None, discovered={'supported_efforts': ['low', 'high']})
        config.workflow.worker.models = [dict(provider='codex', api_provider='deepseek', model='v4-pro',
                                              effort='high', speed='inherit')]
        enter = self.walk(config, ('agents', 'external_apis', 'deepseek', 'models'))
        keys = [*enter, 'S', *['\x1b'] * 4, curses.KEY_END, curses.KEY_UP, '\n']
        edited, screen = menu_tests.ConfigMenuTests().edit(config, keys, responses=[
            ({'v4-pro': {}}, None), ({'v4-pro': {'supported_efforts': ['low']}}, None)])
        self.assertEqual(edited.config.to_dict(), config.to_dict())
        self.assertIn('Sync failed: workflow.worker.models.0', '\n'.join(screen.frames))

    def test_failed_sync_keeps_the_previous_browsing_list_for_individual_additions(self):
        """A network failure must leave individual selection available from the last usable list."""
        config = external_config()
        keys = [*self.walk(config, ('agents', 'external_apis', 'deepseek', 'models')), 'S',
                curses.KEY_END, curses.KEY_UP, '\n', '\n',
                *['\x1b'] * 5, curses.KEY_END, curses.KEY_UP, '\n']
        edited, _ = menu_tests.ConfigMenuTests().edit(config, keys, responses=[
            ({'listed-model': {}}, None), ({}, 'timeout')])
        self.assertIn('listed-model', edited.config.agents.external_apis['deepseek'].models)

    def test_missing_cursor_status_on_a_continuation_preserves_the_entire_draft(self):
        """Saving after an incomplete cursor sequence must retain the unseen model's settings and defaults."""
        config = rich_config()
        first = sample('anthropic-page1.json')
        last = sample('anthropic-page2.json')
        last.pop('has_more')
        load = api_models.load
        keys = [*self.walk(config, ('agents', 'external_apis', 'deepseek', 'models')), 'S',
                *['\x1b'] * 4, curses.KEY_END, curses.KEY_UP, '\n']
        with patch.object(api_models, '_fetch', side_effect=[first, last, first, last]):
            edited, screen = menu_tests.ConfigMenuTests().edit(config, keys, responses=load)
        self.assertEqual(edited.config.to_dict(), config.to_dict())
        self.assertIn('has_more', '\n'.join(screen.frames))

    def test_full_sync_refetches_on_retry_and_only_save_replaces_the_catalog(self):
        """A stale preview or partial fetch must never drive deletions or overwrite a manual exception."""
        raw = external_config().to_dict()
        provider = raw['agents']['external_apis']['deepseek']
        provider['models'] = {'retired': {}, 'keep': {'discovered': {'context_window': 50000},
                                                       'max_output_tokens': 2000}}
        provider['codex']['default_model'] = 'retired'
        provider['claude']['default_model'] = 'keep'
        config = Config.from_dict(raw)
        fresh = {'keep': {'context_window': 75000, 'max_output_tokens': 5000}, '~vendor/new': {}}
        responses = [({'preview': {}}, None), ({}, 'incomplete list'), (fresh, None)]
        enter = self.walk(config, ('agents', 'external_apis', 'deepseek', 'models'))
        sync = [curses.KEY_END, curses.KEY_UP, curses.KEY_UP, '\n']
        retry = ['S']
        save = ['\x1b'] * 4 + [curses.KEY_END, curses.KEY_UP, '\n']
        ui = menu_tests.ConfigMenuTests()
        failed, _ = ui.edit(config, [*enter, *sync, *save], responses=responses[:2])
        self.assertEqual(failed.config.to_dict(), config.to_dict())
        cancelled, _ = ui.edit(config, [*enter, *sync, *retry, *['\x1b'] * 5], responses=responses)
        self.assertIsNone(cancelled)
        result, screen = ui.edit(config, [*enter, *sync, *retry, *save], responses=responses)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            save_config(result.config, path)
            saved = load_config(path).agents.external_apis['deepseek']
        self.assertEqual(set(saved.models), set(fresh))
        self.assertEqual((saved.codex.default_model, saved.claude.default_model), ('', 'keep'))
        self.assertEqual(saved.codex.models['keep']['context_window'], 75000)
        self.assertEqual(saved.claude.models['keep']['max_output_tokens'], 2000)
        self.assertIn('1 added, 1 updated, 1 removed', '\n'.join(screen.frames))
        self.assertIn('incomplete list', '\n'.join(screen.frames))

    def test_defaults_discovery_and_manual_overrides_reach_both_clients_after_round_trip(self):
        """Provider fallback edits affect missing fields, while new API values and model exceptions stay distinct."""
        raw = external_config().to_dict()
        p = raw['agents']['external_apis']['deepseek']
        p['model_defaults'] = {'context_window': 64000, 'max_output_tokens': 2048, 'supported_efforts': ['low', 'high'],
                               'effort': 'high', 'input_modalities': ['text', 'image']}
        p['models'] = {'explicit': {'context_window': 32000, 'input_modalities': ['text']}}
        p = Config.from_dict(raw).to_dict()['agents']['external_apis']['deepseek']
        catalog = {'explicit': {'context_window': 128000}, 'reported': {'supported_efforts': ['low']}, 'unknown': {}}
        config_menu.sync_models('deepseek', p, catalog)
        p['model_defaults']['context_window'] = 96000
        raw['agents']['external_apis']['deepseek'] = p
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            save_config(Config.from_dict(raw), path)
            config = load_config(path)
            for model, window, inputs, effort in [('explicit', 32000, ['text'], 'high'),
                        ('reported', 96000, ['text', 'image'], None), ('custom', 96000, ['text', 'image'], 'high')]:
                with self.subTest(model=model):
                    directory = Path(tmp) / model
                    _, env = ext.prepare(['claude', '--model', model], 'deepseek', 'claude', config, directory, {})
                    self.assertEqual(env['CLAUDE_CODE_MAX_CONTEXT_TOKENS'], str(window))
                    self.assertEqual(env['CLAUDE_CODE_MAX_OUTPUT_TOKENS'], '2048')
                    self.assertEqual(env['CLAUDE_CODE_EFFORT_LEVEL'], effort or 'auto')
                    ext.prepare(['codex', '--model', model], 'deepseek', 'codex', config, directory, {})
                    rows = json.loads((directory / 'models.json').read_text())['models']
                    row = next(r for r in rows if r['slug'] == model)
                    self.assertEqual((row['context_window'], row['input_modalities'], row['default_reasoning_level']),
                                     (window, inputs, effort))
            config_menu.sync_models('deepseek', p, {'explicit': {}, 'reported': {}, 'unknown': {}})
            again = Config.from_dict(raw).agents.external_apis['deepseek']
            self.assertEqual(again.codex.models['reported']['effort'], 'high')
            self.assertEqual(again.codex.models['explicit']['context_window'], 32000)

    def test_model_editor_can_pin_an_inherited_value_and_release_it_to_the_api(self):
        """Entering the same number pins it; resetting just that field follows subsequent discovery."""
        config = external_config()
        path = ('agents', 'external_apis', 'deepseek')
        defaults = (*path, 'model_defaults')
        keys = [*self.walk(config, defaults), curses.KEY_HOME,
                *[curses.KEY_DOWN] * list(config.agents.external_apis['deepseek'].model_defaults).index('context_window'),
                '\n', '\x15', *'64000', '\n', *['\x1b'] * 4, curses.KEY_END, curses.KEY_UP, '\n']
        edited, _ = menu_tests.ConfigMenuTests().edit(config, keys)
        model = (*path, 'models', 'external-small')
        field = [*self.walk(edited.config, model), curses.KEY_HOME,
                 *[curses.KEY_DOWN] * list(external_model_entry()).index('context_window')]
        save = [*['\x1b'] * 5, curses.KEY_END, curses.KEY_UP, '\n']
        pinned, screen = menu_tests.ConfigMenuTests().edit(edited.config, [*field, '\n', '\n', *save])
        provider = pinned.config.to_dict()['agents']['external_apis']['deepseek']
        self.assertEqual(provider['models']['external-small']['context_window'], 64000)
        self.assertIn('[inherited]', '\n'.join(screen.frames))
        config_menu.sync_models('deepseek', provider, {'external-small': {'context_window': 256000}})
        config = Config.from_dict({'agents': {'external_apis': {'deepseek': provider}}})
        reset, screen = menu_tests.ConfigMenuTests().edit(config, [*field, 'R', *save])
        self.assertEqual(reset.config.agents.external_apis['deepseek'].claude.models['external-small']['context_window'],
                         256000)
        self.assertIn('[API]', '\n'.join(screen.frames))

    def test_implicit_launch_default_survives_catalog_sync_and_sorted_json_storage(self):
        """An alphabetically earlier new ID must not switch either CLI's existing default after save."""
        raw = external_config().to_dict()
        p = raw['agents']['external_apis']['deepseek']
        for cli in ('codex', 'claude'):
            p[cli]['default_model'] = ''
        p['models'] = {'z-model': external_model_entry()}
        listed = {'z-model': {}, 'a-model': {}}
        config_menu.sync_models('deepseek', p, listed)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            save_config(Config.from_dict(raw), path)
            loaded = load_config(path)
            for cli in ('codex', 'claude'):
                self.assertEqual(ext.selected_model(loaded, 'deepseek', cli, []), 'z-model')
        p['models'].clear()
        for cli in ('codex', 'claude'):
            p[cli]['default_model'] = ''
        config_menu.sync_models('deepseek', p, listed)
        first = copy.deepcopy(p)
        config_menu.sync_models('deepseek', p, listed)
        self.assertEqual(p, first)

    def test_partial_metadata_overrides_only_the_connection_settings_it_describes(self):
        """An audio-only entry accepts no Codex input, and a preferred effort may arrive without a level list."""
        raw = external_config().to_dict()
        p = raw['agents']['external_apis']['deepseek']
        p['model_defaults'].update(input_modalities=['text', 'image'], effort='low')
        fields = api_models.model_fields({'architecture': {'input_modalities': ['audio']},
                                         'reasoning': {'default_effort': 'high'}})
        config_menu.sync_models('deepseek', p, {'partial': fields})
        p = Config.from_dict(raw).agents.external_apis['deepseek']
        self.assertEqual(p.codex.models['partial']['input_modalities'], [])
        for endpoint in (p.codex, p.claude):
            self.assertEqual(endpoint.models['partial']['effort'], 'high')
            self.assertEqual(endpoint.models['partial']['supported_efforts'], [])

    def test_empty_catalog_can_be_saved_and_a_conflicting_refresh_preserves_the_draft(self):
        """An empty successful mirror clears both defaults; invalid new limits never partly update the draft."""
        provider = rich_config().to_dict()['agents']['external_apis']['deepseek']
        provider['models']['v4-pro']['context_window'] = None
        before = copy.deepcopy(provider)
        with self.assertRaises(AgentBoxError):
            config_menu.sync_models('deepseek', provider, {'v4-pro': {'context_window': 10000}, 'new': {}})
        self.assertEqual(provider, before)
        config_menu.sync_models('deepseek', provider, {})
        self.assertEqual(provider['models'], {})
        self.assertEqual((provider['codex']['default_model'], provider['claude']['default_model']), ('', ''))
