"""External invocations must preserve the parent API and Remote Control session."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

from agent_box import cli as cli_module, external_launch as ext, runtime, provider_runner
from agent_box.config import Config
from agent_box.errors import AgentBoxError
from agent_box.workspace import Workspace
from test_external_config import external_config
from test_runtime import IsolatedRuntimeTest


class ExternalProcessTests(unittest.TestCase):
    def test_cli_requires_an_explicit_cli_and_dispatches_host_selection(self):
        """No saved or default CLI may silently decide how an external API is called."""
        with patch.object(cli_module.sys, 'argv', ['agent-box', '--host', 'ext', 'deepseek', 'claude', '--model', 'x']), \
             patch.object(runtime, 'host_main', return_value=0) as host, self.assertRaises(SystemExit):
            cli_module.main()
        host.assert_called_once_with('claude', ['--model', 'x'], [], api_provider='deepseek')
        with patch.object(cli_module.sys, 'argv', ['agent-box', 'ext', 'deepseek']), \
             patch.object(runtime, 'runtime_main') as launch, \
             patch.object(cli_module, 'die', side_effect=SystemExit(1)), self.assertRaises(SystemExit):
            cli_module.main()
        launch.assert_not_called()

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

    def test_model_selection_is_explicit_or_from_that_cli_and_conflicts_fail(self):
        """Service/profile flags could reconnect the API process to the parent's RC server."""
        config = external_config()
        for cli, args in [('codex', ['--remote=unix:///parent']), ('codex', ['--model', 'x', 'resume']),
                          ('codex', ['-pother']), ('codex', ['-cmodel_provider="openai"']),
                          ('codex', ['--config', 'model_providers.x.base_url="https://bad"']),
                          ('codex', ['--config', '"model_providers"."x".base_url="https://bad"']),
                          ('claude', ['--settings', '{"disableRemoteControl":false}']),
                          ('claude', ['-rfoo']), ('claude', ['--from-pr', '123']),
                          ('claude', ['--session-id', 'a-session']), ('claude', ['--tmux']),
                          ('claude', ['--environment', 'cloud-env']), ('claude', ['respawn', 'id']),
                          ('claude', ['--rc']), ('claude', ['--bg']), ('claude', ['auth', 'login'])]:
            with self.subTest(cli=cli, args=args), self.assertRaises(AgentBoxError):
                ext.selected_model(config, 'deepseek', cli, args)
        self.assertEqual(ext.selected_model(config, 'deepseek', 'codex', ['-mother']), 'other')
        self.assertEqual(ext.selected_model(config, 'deepseek', 'claude', []), 'external-small')
        config.external_providers['deepseek'].claude.models = []
        with self.assertRaisesRegex(AgentBoxError, 'requires a model ID'):
            ext.selected_model(config, 'deepseek', 'claude', [])
        config.external_providers['deepseek'].enabled = False
        with self.assertRaisesRegex(AgentBoxError, 'disabled'):
            ext.selected_model(config, 'deepseek', 'codex', [])

    def test_managed_runner_uses_shared_api_preparation_and_keeps_report_protocol(self):
        """Managed calls need the same auth boundary without an interactive RC wrapper."""
        config = external_config()
        report = {'status': 'completed', 'summary': 'Done', 'evidence': []}
        for cli in ('codex', 'claude'):
            def popen(command, **kwargs):
                self.assertNotIn('_session', command)
                self.assertNotIn('private-api-key', str(command))
                key = ext.API_ENV if cli == 'codex' else 'ANTHROPIC_API_KEY'
                self.assertEqual(kwargs['env'][key], 'private-api-key')
                if cli == 'codex':
                    Path(command[command.index('-o') + 1]).write_text(json.dumps(report))
                else:
                    kwargs['stdout'].write(json.dumps({'structured_output': report, 'is_error': False}))
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
        config.codex.remote_control = config.claude.remote_control = True
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
        config.external_providers['disabled'] = Config.from_dict({'external_providers': {
            'disabled': {'api_key': 'unused-secret'}}}).external_providers['disabled']
        with tempfile.TemporaryDirectory() as tmp:
            runtime.sync_tracker_runtime(config, Path(tmp), policy=True)
            path = Path(tmp) / 'config.json'
            rendered = path.read_text()
            self.assertNotIn('unused-secret', rendered)
            self.assertEqual(set(json.loads(rendered)['external_providers']), {'deepseek'})
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
        spec = config.external_providers['deepseek']
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
