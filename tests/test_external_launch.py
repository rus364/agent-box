"""External invocations must preserve the parent API and Remote Control session."""
import json
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import ANY, patch, Mock

from agent_box import cli as cli_module, external_launch as ext, runtime, provider_runner
from agent_box.config import Config
from agent_box.errors import AgentBoxError
from agent_box.workspace import Workspace
from test_external_config import external_config
from test_runtime import IsolatedRuntimeTest


class ExternalProcessTests(unittest.TestCase):
    def test_exec_config_flags_share_one_scope_without_consuming_option_values_or_prompt(self):
        """Exec-level -c must not discard API/auth/catalog overrides in the installed CLI."""
        args = ['-c', 'web_search="cached"', 'exec', '--ignore-user-config',
                '--config=web_search="live"', '-cmodel_reasoning_effort="max"',
                '--output-schema', '-c-literal-path', '--', '-c', 'literal-prompt']
        with tempfile.TemporaryDirectory() as temp:
            command, _ = ext.prepare(['codex', *args], 'deepseek', 'codex', external_config(), temp, {})
        boundary = command.index('exec')
        self.assertEqual(command[boundary:], ['exec', '--ignore-user-config', '--output-schema',
                                             '-c-literal-path', '--', '-c', 'literal-prompt'])
        options = {}
        for index, arg in enumerate(command[:boundary]):
            if arg == '-c':
                options.update(tomllib.loads(command[index + 1]))
        self.assertEqual(options['model_provider'], 'agent_box_external')
        self.assertEqual(options['cli_auth_credentials_store'], 'ephemeral')
        self.assertIn('model_catalog_json', options)
        self.assertEqual(options['web_search'], 'live')
        self.assertEqual(options['model_reasoning_effort'], 'max')

    def test_search_defaults_and_native_session_options_reach_codex_without_cli_overrides(self):
        """A later CLI override must not mask missing defaults or dropped native settings."""
        for search in (None, 'live', 'cached'):
            with self.subTest(search=search), tempfile.TemporaryDirectory() as temp:
                raw = external_config().to_dict()
                endpoint = raw['agents']['external_apis']['deepseek']['codex']
                if search:
                    endpoint['config'] = {'web_search': search, 'model_context_window': 64000,
                                          'model_auto_compact_token_limit': 48000}
                command, _ = ext.prepare(['codex'], 'deepseek', 'codex', Config.from_dict(raw), temp, {})
                options = {}
                for index, arg in enumerate(command[:-1]):
                    if arg == '-c':
                        options.update(tomllib.loads(command[index + 1]))
                self.assertEqual(options['web_search'], search or 'disabled')
                if search:
                    self.assertEqual(options['model_context_window'], 64000)
                    self.assertEqual(options['model_auto_compact_token_limit'], 48000)

    def test_prepare_renders_option_keys_and_values_codex_can_parse(self):
        """A generated -c Codex cannot parse is a launch that never starts.

        Option keys and values reach Codex as TOML text; an astral character in
        a key used to be escaped as a UTF-16 surrogate pair and DEL was left
        literal, both of which TOML rejects -- while the configuration carrying
        them passes validation.
        """
        raw = external_config().to_dict()
        endpoint = raw['agents']['external_apis']['deepseek']['codex']
        endpoint['config'] = {'🚀': 'del\x7fhere', 'web_search': 'live'}
        with tempfile.TemporaryDirectory() as temp:
            command, _ = ext.prepare(['codex'], 'deepseek', 'codex', Config.from_dict(raw), temp, {})
        options = {}
        for index, arg in enumerate(command[:-1]):
            if arg == '-c':
                options.update(tomllib.loads(command[index + 1]))
        self.assertEqual(options['🚀'], 'del\x7fhere')

    def test_native_options_preserve_auth_and_keep_header_values_out_of_argv(self):
        """Native search/transport settings must work without touching subscription auth."""
        raw = external_config().to_dict()
        endpoint = raw['agents']['external_apis']['deepseek']['codex']
        endpoint['provider_options'] = {'query_params': {'api-version': 'test'},
                                        'http_headers': {'X-Tenant': 'private-header'},
                                        'stream_idle_timeout_ms': 12345}
        endpoint['config'] = {'web_search': 'live', 'model_reasoning_effort': 'high'}
        config = Config.from_dict(raw)
        for suffix in ([], ['exec', '--json']):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as temp:
                command, env = ext.prepare(['codex', *suffix, '-c', 'web_search="disabled"'],
                                           'deepseek', 'codex', config, temp, {})
                options = {}
                for index, arg in enumerate(command[:-1]):
                    if arg == '-c':
                        options.update(tomllib.loads(command[index + 1]))
                self.assertEqual(options['web_search'], 'disabled')
                self.assertEqual(options['cli_auth_credentials_store'], 'ephemeral')
                self.assertNotIn('forced_login_method', options)
                provider = options['model_providers']['agent_box_external']
                self.assertEqual(provider['query_params'], {'api-version': 'test'})
                self.assertEqual(provider['stream_idle_timeout_ms'], 12345)
                self.assertEqual(env[provider['env_http_headers']['X-Tenant']], 'private-header')
                self.assertNotIn('private-header', str(command))
                self.assertEqual(ext.builtin_environment(env), {})

    def test_native_options_cannot_replace_auth_or_restore_persistent_credentials(self):
        """Pass-through flexibility must not reintroduce logout or change the selected API."""
        for field, value in [('config', {'cli_auth_credentials_store': 'file'}),
                             ('config', {'model_provider': 'openai'}),
                             ('provider_options', {'base_url': 'https://other.invalid'}),
                             ('provider_options', {'requires_openai_auth': True}),
                             ('provider_options', {'http_headers': {'Authorization': 'secret'}}),
                             ('config', {'web_search': None})]:
            with self.subTest(field=field, value=value), self.assertRaises(AgentBoxError):
                raw = external_config().to_dict()
                raw['agents']['external_apis']['deepseek']['codex'][field] = value
                Config.from_dict(raw)
        with self.assertRaises(AgentBoxError):
            ext.selected_model(external_config(), 'deepseek', 'codex',
                               ['-c', 'cli_auth_credentials_store="file"'])

    def test_native_effort_default_still_obeys_declared_model_support(self):
        """A native config default must not bypass the model's supported effort list."""
        raw = external_config().to_dict()
        endpoint = raw['agents']['external_apis']['deepseek']['codex']
        endpoint['config'] = {'model_reasoning_effort': 'high'}
        endpoint['models']['external-small']['supported_efforts'] = ['low']
        with self.assertRaisesRegex(AgentBoxError, 'default effort is not supported'):
            Config.from_dict(raw)
        endpoint['models']['external-small']['effort'] = 'low'
        config = Config.from_dict(raw)
        with tempfile.TemporaryDirectory() as temp:
            command, _ = ext.prepare(['codex'], 'deepseek', 'codex', config, temp, {})
        efforts = [arg for arg in command if arg.startswith('model_reasoning_effort=')]
        self.assertEqual(efforts[-1], 'model_reasoning_effort="low"')

    def test_cli_leaves_an_omitted_cli_to_the_default_agent_in_both_modes(self):
        """`ext NAME` without claude/codex follows the same default agent as a plain launch."""
        for argv, agent, rest in ((['--host', 'ext', 'deepseek', 'claude', '--model', 'x'], 'claude', ['--model', 'x']),
                                  (['--host', 'ext', 'deepseek', '--model', 'x'], None, ['--model', 'x'])):
            with self.subTest(argv=argv), patch.object(cli_module.sys, 'argv', ['agent-box', *argv]), \
                 patch.object(runtime, 'host_main', return_value=0) as host, self.assertRaises(SystemExit):
                cli_module.main()
            self.assertEqual(host.call_args.args[:2], (agent, rest))
            self.assertEqual(host.call_args.kwargs, {'api_provider': 'deepseek'})
        with patch.object(cli_module.sys, 'argv', ['agent-box', 'ext', 'deepseek']), \
             patch.object(runtime, 'runtime_main') as launch:
            cli_module.main()
        from agent_box.launch_options import LaunchOptions
        launch.assert_called_once_with([], ANY, LaunchOptions(), api_provider='deepseek')
        for argv in (['ext'], ['ext', 'deepseek', 'shell'], ['--host', 'ext', 'deepseek', 'doctor']):
            with self.subTest(argv=argv), patch.object(cli_module.sys, 'argv', ['agent-box', *argv]), \
                 patch.object(runtime, 'runtime_main') as launch, patch.object(runtime, 'host_main') as host, \
                 patch.object(cli_module, 'die', side_effect=SystemExit(1)), self.assertRaises(SystemExit):
                cli_module.main()
            launch.assert_not_called()
            host.assert_not_called()

    def test_separate_cli_endpoints_use_private_auth_and_external_model_catalogs(self):
        """An external process must neither reuse subscription auth nor expose keys in argv."""
        config = external_config()
        original = {'ANTHROPIC_AUTH_TOKEN': 'parent-secret', 'OPENAI_API_KEY': 'parent-openai',
                    'OPENAI_BASE_URL': 'https://parent', 'CLAUDE_CODE_USE_BEDROCK': '1', 'PATH': '/bin'}
        for cli in ('codex', 'claude'):
            with self.subTest(cli=cli), tempfile.TemporaryDirectory() as tmp:
                command, env = ext.prepare([cli, '--model', 'manual-model'], 'deepseek', cli, config, tmp, original)
                self.assertEqual(original['ANTHROPIC_AUTH_TOKEN'], 'parent-secret')
                self.assertNotIn('private-api-key', str(command))
                self.assertNotIn('parent-secret', str(command))
                self.assertEqual(ext.builtin_environment(env), original)
                for path in Path(tmp).iterdir():
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                if cli == 'codex':
                    self.assertEqual(env[ext.API_ENV], 'private-api-key')
                    self.assertNotIn('OPENAI_API_KEY', env)
                    self.assertNotIn('ANTHROPIC_AUTH_TOKEN', env)
                    self.assertIn('https://api.example/v1', str(command))
                    models = json.loads((Path(tmp) / 'models.json').read_text())['models']
                    self.assertEqual([m['slug'] for m in models], ['external-small', 'external-large', 'manual-model'])
                else:
                    self.assertEqual(env['ANTHROPIC_BASE_URL'], 'https://api.example/anthropic')
                    self.assertEqual(env['ANTHROPIC_API_KEY'], 'private-api-key')
                    self.assertEqual(env['CLAUDE_CODE_USE_BEDROCK'], '')
                    settings = json.loads((Path(tmp) / 'claude-settings.json').read_text())
                    self.assertTrue(settings['disableRemoteControl'])
                    self.assertFalse(settings['remoteControlAtStartup'])
                    self.assertTrue(settings['modelPicker']['replaceBuiltInOptions'])
                    self.assertEqual(settings['env']['ANTHROPIC_DEFAULT_MODEL'], 'manual-model')

    def test_nested_external_process_restores_original_builtin_auth(self):
        """A second external process must not capture the first API as its baseline."""
        config = external_config()
        with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
            original = {'OPENAI_API_KEY': 'builtin', 'PATH': '/bin'}
            _, first = ext.prepare(['codex'], 'deepseek', 'codex', config, one, original)
            _, second = ext.prepare(['claude'], 'deepseek', 'claude', config, two, first)
            self.assertEqual(ext.builtin_environment(second), original)
            self.assertEqual(first[ext.API_ENV], 'private-api-key')
        with self.assertRaisesRegex(AgentBoxError, 'Cannot restore'):
            ext.builtin_environment(first)

    def test_native_option_values_and_keys_survive_the_toml_parser(self):
        """Codex parses -c as TOML, so a rendered option has to survive TOML.

        DEL in a value and a non-BMP character in a key both used to render
        through JSON escaping that TOML rejects, which killed the launch
        before Codex started.
        """
        options = {"🚀": "del\x7fhere", "web_search": ["live", {"emoji": "🚀"}],
                   "retries": 3, "stream": True}
        self.assertEqual(tomllib.loads("x=" + ext.toml_value(options))["x"], options)

    def test_remote_control_ownership_is_decided_by_the_root_key(self):
        """The launch owns config keys, not the names nested under them.

        `mcp_servers.remote_dev.enabled=false` is agent-box's own override for
        a desktop MCP whose name merely contains 'remote'; refusing it by
        substring killed the launch it had just been generated for. Codex's
        Remote Control settings stay refused, at the root or under `features`.
        """
        config = external_config()
        self.assertEqual(
            ext.selected_model(config, 'deepseek', 'codex',
                               ['-c', 'mcp_servers.remote_dev.enabled=false']),
            'external-small',
        )
        for args in (['-c', 'remote_models=false'],
                     ['-c', 'features.remote_control=false'],
                     ['-c', 'features={"remote_plugin"=false}']):
            with self.subTest(args=args), self.assertRaises(AgentBoxError):
                ext.selected_model(config, 'deepseek', 'codex', args)

    def test_model_selection_is_explicit_or_from_that_cli_and_conflicts_fail(self):
        """Service/profile flags could reconnect the API process to the parent's RC server."""
        config = external_config()
        for cli, args in [('codex', ['--remote=unix:///parent']), ('codex', ['--model', 'x', 'resume']),
                          ('codex', ['-pother']), ('codex', ['-cmodel_provider="openai"']),
                          ('codex', ['--config', 'model_providers.x.base_url="https://bad"']),
                          ('codex', ['--config', '"model_providers"."x".base_url="https://bad"']),
                          ('claude', ['--settings', '{"disableRemoteControl":false}']),
                          ('claude', ['-rfoo']), ('claude', ['--from-pr', '123']),
                          ('claude', ['-prfoo']), ('claude', ['-pc']),
                          ('claude', ['--session-id', 'a-session']), ('claude', ['--tmux']),
                          ('claude', ['--environment', 'cloud-env']), ('claude', ['respawn', 'id']),
                          ('claude', ['--name', 'x', 'auth', '--help']),
                          ('claude', ['--future-value-option', 'x', 'auth']),
                          ('codex', ['exec', '--model', 'x', 'resume', 'old-id']),
                          ('claude', ['--rc']), ('claude', ['--bg']), ('claude', ['auth', 'login'])]:
            with self.subTest(cli=cli, args=args), self.assertRaises(AgentBoxError):
                ext.selected_model(config, 'deepseek', cli, args)
        self.assertEqual(ext.selected_model(config, 'deepseek', 'codex', ['-mother']), 'other')
        self.assertEqual(ext.selected_model(config, 'deepseek', 'claude', []), 'external-small')
        config.agents.external_apis['deepseek'].claude.models = {}
        with self.assertRaisesRegex(AgentBoxError, 'requires a model ID'):
            ext.selected_model(config, 'deepseek', 'claude', [])
        config.agents.external_apis['deepseek'].enabled = False
        with self.assertRaisesRegex(AgentBoxError, 'disabled'):
            ext.selected_model(config, 'deepseek', 'codex', [])

    def test_fresh_batch_commands_and_print_prompts_remain_supported(self):
        """Batch work is a new API process, and print prompt text is not a CLI subcommand."""
        config = external_config()
        for cli, args in [('codex', ['exec', '--json', 'auth']), ('codex', ['review', '--base', 'main']),
                          ('codex', ['exec', 'review', 'resume']),
                          ('claude', ['-p', 'resume']), ('claude', ['-p', 'auth']),
                          ('claude', ['-pnresume', 'auth']),
                          ('claude', ['--name', 'auth', '-p', 'hello']), ('claude', ['--', 'auth'])]:
            with self.subTest(cli=cli, args=args), tempfile.TemporaryDirectory() as tmp:
                command, _ = ext.prepare([cli, *args], 'deepseek', cli, config, tmp, {})
                if cli == 'codex':
                    self.assertIn('model="external-small"', command)
                    self.assertNotIn('--model', command)

    def test_managed_runner_uses_shared_api_preparation_and_keeps_the_answer(self):
        """Managed calls need the same auth boundary without an interactive RC wrapper."""
        config = external_config()
        report = '## Done\nEdited.' 
        for cli in ('codex', 'claude'):
            def popen(command, **kwargs):
                self.assertNotIn('_session', command)
                self.assertNotIn('private-api-key', str(command))
                key = ext.API_ENV if cli == 'codex' else 'ANTHROPIC_API_KEY'
                self.assertEqual(kwargs['env'][key], 'private-api-key')
                if cli == 'codex':
                    Path(command[command.index('-o') + 1]).write_text(report)
                else:
                    kwargs['stdout'].write(json.dumps({'result': report, 'is_error': False}))
                return Mock(returncode=0)
            with self.subTest(cli=cli), patch('agent_box.config.effective_config', return_value=config), \
                    patch.object(provider_runner.subprocess, 'Popen', side_effect=popen):
                result = provider_runner.run({'provider': cli, 'api_provider': 'deepseek', 'model': 'external-small',
                                              'effort': 'default'}, 'worker', Path.cwd(), 'Do work')
                self.assertEqual(result, report)


class ExternalRuntimeTests(IsolatedRuntimeTest):
    def test_ext_host_and_container_bypass_rc_while_builtin_launch_keeps_it(self):
        """An occupied RC slot must never be inspected by the external launcher."""
        config = external_config()
        config.agents.codex.remote_control = config.agents.claude.remote_control = True
        for cli in ('codex', 'claude'):
            for host in (True, False):
                with self.subTest(cli=cli, host=host), \
                     patch.object(runtime.shutil, 'which', return_value='/usr/bin/' + cli), \
                     patch.object(runtime, 'resolve_workspace', return_value=Workspace(Path.cwd(), Path.cwd())), \
                     patch.object(runtime, 'launch_config', return_value=config), \
                     patch.object(runtime, 'tmux_enabled', return_value=False), \
                     patch.object(runtime, 'remote_control_invocation', return_value=True), \
                     patch.object(runtime, 'remote_control_command') as rc, \
                     patch.object(runtime, 'ensure_state_dirs'), patch.object(runtime, 'ensure_refresh_token'), \
                     patch.object(runtime, 'image_exists', return_value=True), \
                     patch.object(runtime, 'claude_channel_args', return_value=([], [])), \
                     patch.object(runtime, 'codex_runtime_overrides', return_value=[]), \
                     patch.object(runtime, 'is_host_local', return_value=False), \
                     patch.object(runtime, 'launch_in_workspace_container', return_value=0) as container, \
                     patch.object(runtime.subprocess, 'call', return_value=0) as call:
                    if host:
                        runtime.host_main(cli, [], api_provider='deepseek')
                        command = call.call_args.args[0]
                    else:
                        with self.assertRaises(SystemExit):
                            runtime.runtime_main([cli], Path.cwd(), api_provider='deepseek')
                        command = container.call_args.args[2]
                    self.assertIn('_external', command)
                    self.assertNotIn('private-api-key', str(command))
                    rc.assert_not_called()
                    if host:
                        runtime.host_main(cli, [])
                        rc.assert_called_once()

    def test_disabled_connections_are_absent_from_private_session_snapshot(self):
        """Opt-out definitions must not expose unused credentials to the workspace."""
        config = external_config()
        config.agents.external_apis['disabled'] = Config.from_dict({'agents': {'external_apis': {
            'disabled': {'api_key': 'unused-secret'}}}}).agents.external_apis['disabled']
        with tempfile.TemporaryDirectory() as tmp:
            runtime.sync_tracker_runtime(config, Path(tmp), policy=True)
            path = Path(tmp) / 'config.json'
            rendered = path.read_text()
            self.assertNotIn('unused-secret', rendered)
            self.assertEqual(set(json.loads(rendered)['agents']['external_apis']), {'deepseek'})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_invalid_ext_is_rejected_before_snapshot_or_process(self):
        """Bad invocation options must fail before writing session credentials."""
        with patch.object(runtime.shutil, 'which', return_value='/bin/codex'), \
             patch.object(runtime, 'resolve_workspace', return_value=Workspace(Path.cwd(), Path.cwd())), \
             patch.object(runtime, 'launch_config', return_value=external_config()), \
             patch.object(runtime, 'session_runtime') as snapshot, \
             patch.object(runtime.subprocess, 'call') as call, self.assertRaises(AgentBoxError):
            runtime.host_main('codex', ['--remote', 'unix:///parent'], api_provider='deepseek')
        snapshot.assert_not_called()
        call.assert_not_called()

    def test_container_snapshots_map_host_apis_without_changing_host_definitions(self):
        """A shared provider definition must reach the host service in either launch mode."""
        config = external_config()
        spec = config.agents.external_apis['deepseek']
        spec.codex.url = 'http://127.0.0.1:8123/v1'
        spec.claude.url = 'http://localhost:8124/anthropic'
        with tempfile.TemporaryDirectory() as tmp:
            for container in (True, False):
                runtime.sync_tracker_runtime(config, Path(tmp), policy=True, container=container)
                data = json.loads((Path(tmp) / 'config.json').read_text())
                loaded = Config.from_dict(data)
                for cli in ('codex', 'claude'):
                    with tempfile.TemporaryDirectory() as invocation:
                        command, env = ext.prepare([cli], 'deepseek', cli, loaded, invocation, {})
                        target = ('host.containers.internal' if container else
                                  '127.0.0.1' if cli == 'codex' else 'localhost')
                        self.assertIn(target, str(command) if cli == 'codex' else env['ANTHROPIC_BASE_URL'])
        self.assertEqual(spec.codex.url, 'http://127.0.0.1:8123/v1')
        self.assertEqual(spec.claude.url, 'http://localhost:8124/anthropic')
        spec.codex.url = 'https://api.internal/v1'
        spec.claude.url = ''
        with patch.object(runtime, 'is_host_local', return_value=True):
            self.assertEqual(runtime.tracker_host_args(config), ['--add-host=api.internal:host-gateway'])
            self.assertEqual(runtime.container_tracker_url(spec.codex.url), spec.codex.url)
