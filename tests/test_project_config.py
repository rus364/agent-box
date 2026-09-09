"""Project policy is strict, inherited, and cannot redirect another account's token."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_box import project_config, runtime
from agent_box.config import Config, TrackerConnection, effective_config
from agent_box.errors import AgentBoxError
from agent_box.repo_binding import Binding, write_binding, setup_content
from agent_box.tracker.registry import make_adapter


class ProjectConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / '.agent-box.json'
        self.config = Config()
        self.config.tracker.enabled = True
        self.config.tracker.connections['vikunja'] = TrackerConnection('https://tracker.example', 'global-secret')
        self.binding = Binding('vikunja', 'https://tracker.example', '12', True)
        self.runtime_state = runtime.WorkspaceRuntimeState(self.root / 'workspace', self.root / 'git', self.root / 'gh')
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_partial_override_preserves_false_empty_list_empty_model_and_global(self):
        self.config.workflow.review.override.enabled = True
        original = self.config.to_dict()
        data = {'browser': {'enabled': False}, 'image': {'tools': {'browser': True}},
                'workflow': {'review': {'sensitive_paths': [], 'override': {'models': {'codex': {'model': ''}}}},
                             'auto_push': False}}
        project_config.validate(data, self.path)
        result = project_config.apply(self.config, data)
        self.assertEqual(result.workflow.review.sensitive_paths, [])
        self.assertTrue(result.workflow.review.override.enabled)
        self.assertEqual(result.workflow.review.override.models['codex']['model'], '')
        self.assertFalse(result.workflow.auto_push)
        self.assertTrue(result.image.tools['browser'])
        self.assertTrue(result.image.tools['analysis'])
        self.assertEqual(self.config.to_dict(), original)

    def test_worker_override_project_values_reach_runtime_without_changing_global(self):
        """Sparse project false/empty choices supersede global worker candidates only."""
        override = self.config.workflow.worker.override
        override.enabled = override.apply_to_tiered = True
        override.models['codex'] = {'model': 'global-codex', 'effort': 'high'}
        override.models['claude'] = {'model': 'global-claude', 'effort': 'medium'}
        original = self.config.to_dict()
        data = {'workflow': {'worker': {'override': {'apply_to_tiered': False,
                'models': {'codex': {'model': ''}, 'claude': {'effort': 'default'}}}}}}
        project_config.save(self.path, data)
        result = project_config.apply(self.config, project_config.load(self.path))
        runtime.sync_tracker_runtime(result, self.root / 'rendered')
        artifact = self.root / 'rendered' / 'config.json'
        with patch.dict(os.environ, {'AGENT_BOX_RUNTIME_CONFIG': str(artifact)}), \
             patch.object(project_config, 'load', return_value=project_config.load(self.path)):
            rendered = effective_config()
            self.assertEqual(effective_config(include_project=False).workflow.worker.override, override)
        chosen = rendered.workflow.worker.override
        self.assertTrue(chosen.enabled)
        self.assertFalse(chosen.apply_to_tiered)
        self.assertEqual(chosen.models['codex'], {'model': '', 'effort': 'high'})
        self.assertEqual(chosen.models['claude'], {'model': 'global-claude', 'effort': 'default'})
        self.assertEqual(rendered.workflow.review, self.config.workflow.review)
        self.assertEqual(self.config.to_dict(), original)
        disabled = project_config.apply(self.config, {'workflow': {'worker': {'override': {'enabled': False}}}})
        self.assertIsNone(disabled.workflow.override_for('worker', None))
        for bad in ({'enabled': 'true'}, {'models': {'claude': {'effort': 'ultra'}}}):
            with self.assertRaises(AgentBoxError):
                project_config.validate({'workflow': {'worker': {'override': bad}}}, self.path)

    def test_unknown_forbidden_and_malformed_values_never_fall_back(self):
        for data in ({'browser': {'enabled': 1}}, {'env': {}}, {'workflow': {'enabled': False}},
                     {'workflow': {'unknown': 1}}, {'workflow': {'merge_mode': ''}},
                     {'workflow': {'review': {'sensitive_paths': [None]}}},
                     {'workflow': {'tiers': {'0': {'codex': {'effort': ''}}}}},
                     {'tracker': {'connections': {'vikunja': {'url': 'https://u:secret@host'}}}},
                     [], None):
            with self.subTest(data=data), self.assertRaises(AgentBoxError):
                project_config.validate(data, self.path)
        self.path.write_text('{"secret-value":')
        with self.assertRaises(AgentBoxError) as caught:
            project_config.load(self.path)
        self.assertNotIn('secret-value', str(caught.exception))

    def test_credential_presence_is_rejected_before_other_validation(self):
        for token in ('do-not-display', '', None):
            data = {'unknown': {}, 'tracker': {'connections': {'vikunja': {'token': token}}}}
            with self.assertRaises(AgentBoxError) as caught:
                project_config.validate(data, self.path)
            message = str(caught.exception)
            self.assertIn('PROJECT_CREDENTIALS_FORBIDDEN', message)
            self.assertIn(str(self.path), message)
            self.assertIn('tracker.connections.vikunja.token', message)
            self.assertNotIn('do-not-display', message)

    def test_binding_setup_and_cutover_preserve_project_overrides(self):
        project_config.save(self.path, {'browser': {'enabled': True}, 'workflow': {'auto_push': False}})
        write_binding(self.root, self.binding)
        self.assertEqual(Binding.load(self.root), self.binding)
        data = json.loads(setup_content(self.root, Binding('vikunja', self.binding.server, '25'))[self.path])
        self.assertTrue(data['browser']['enabled'])
        self.assertFalse(data['workflow']['auto_push'])
        self.assertEqual(data['tracker']['project'], '25')
        self.assertNotIn('server', data)

    def test_launch_settings_need_no_binding_but_tracker_binding_does(self):
        project_config.save(self.path, {'browser': {'enabled': True}})
        with patch.object(project_config, 'project_path', return_value=self.path):
            self.assertTrue(effective_config(self.config).browser.enabled)
        with self.assertRaisesRegex(AgentBoxError, 'Incomplete repository binding'):
            Binding.load(self.root)

    def test_git_subdirectories_and_worktrees_resolve_the_project_file(self):
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        child = self.root / 'nested'
        child.mkdir()
        self.assertEqual(project_config.project_path(child), self.path)
        subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=Test', '-c', 'user.email=test@example.org',
                        'commit', '-qm', 'empty', '--allow-empty'], check=True)
        worktree = self.root / 'worktree'
        subprocess.run(['git', '-C', str(self.root), 'worktree', 'add', '-q', '--detach', str(worktree)], check=True)
        self.assertEqual(project_config.project_path(worktree), worktree / '.agent-box.json')

    def test_non_repository_and_missing_git_still_validate_the_cwd_project(self):
        """Help remains usable without Git but must still reject forbidden project fields."""
        from contextlib import redirect_stdout, redirect_stderr
        import io
        from agent_box import cli, repo_binding
        with patch.object(Path, 'cwd', return_value=self.root):
            self.assertEqual(project_config.project_path(self.root), self.path)
            with patch.object(repo_binding.subprocess, 'run', side_effect=FileNotFoundError('git')):
                with patch.object(cli.sys, 'argv', ['agent-box', '--help']), redirect_stdout(io.StringIO()):
                    cli.main()
                self.path.write_text(json.dumps({'tracker': {'connections': {'vikunja': {'token': ''}}}}))
                with patch.object(cli.sys, 'argv', ['agent-box', '--help']), \
                     redirect_stderr(io.StringIO()) as output, self.assertRaises(SystemExit) as stopped:
                    cli.main()
                self.assertNotEqual(stopped.exception.code, 0)
                self.assertIn('PROJECT_CREDENTIALS_FORBIDDEN', output.getvalue())

    def test_git_discovery_errors_stop_help_before_actions_without_tracebacks(self):
        """A repository inspection failure must not silently switch to a subdirectory file."""
        from contextlib import redirect_stderr
        import io
        from agent_box import cli, repo_binding
        failures = [subprocess.TimeoutExpired(['git'], 15), PermissionError('git'),
                    subprocess.CompletedProcess(['git'], 128, '', 'fatal: detected dubious ownership'),
                    subprocess.CompletedProcess(['git'], 128, '', "fatal: not a git repository: '/invalid-GIT_DIR'")]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__), \
                 patch.object(repo_binding.subprocess, 'run') as run, \
                 patch.object(cli.sys, 'argv', ['agent-box', '--help']), \
                 patch.object(cli, 'usage_text') as help_action, \
                 redirect_stderr(io.StringIO()) as output:
                if isinstance(failure, Exception):
                    run.side_effect = failure
                else:
                    run.return_value = failure
                with self.assertRaises(SystemExit) as stopped:
                    cli.main()
                self.assertNotEqual(stopped.exception.code, 0)
                self.assertIn('Git', output.getvalue())
                self.assertNotIn('Traceback', output.getvalue())
                help_action.assert_not_called()

    def test_session_connection_cannot_mutate_global_or_bypass_project_identity(self):
        original = self.config.to_dict()
        with patch.object(project_config, 'load', return_value={}), patch.dict(os.environ, {
                'VIKUNJA_URL': self.binding.server, 'VIKUNJA_TOKEN': 'session-secret'}):
            result = effective_config(self.config)
            adapter = make_adapter(self.binding, config=result)
            self.assertEqual(adapter.client.token, 'session-secret')
        self.assertEqual(self.config.to_dict(), original)
        for env in ({'VIKUNJA_URL': self.binding.server}, {'VIKUNJA_TOKEN': 'session-secret'},
                    {'VIKUNJA_URL': '', 'VIKUNJA_TOKEN': 'session-secret'},
                    {'VIKUNJA_URL': 'https://other.example', 'VIKUNJA_TOKEN': 'session-secret'}):
            with patch.dict(os.environ, env), self.assertRaises(AgentBoxError):
                make_adapter(self.binding, config=self.config)

    def test_project_url_cannot_redirect_global_token(self):
        config = project_config.apply(self.config, {'tracker': {'connections': {'vikunja': {'url': 'https://other.example'}}}})
        with self.assertRaises(AgentBoxError):
            make_adapter(Binding('vikunja', 'https://other.example', '12'), config=config)

    def test_launch_checks_selected_project_provider_without_requiring_unused_accounts_to_match(self):
        """An unused Jira override must not prevent launch with the matching Vikunja account."""
        self.config.tracker.provider = 'jira'
        self.config.tracker.connections['jira'] = TrackerConnection('https://global-jira.example', 'jira-secret')
        project_config.save(self.path, {'tracker': {'provider': 'vikunja', 'project': '12', 'connections': {
            'vikunja': {'url': self.binding.server}, 'jira': {'url': 'https://project-jira.example'}}}})
        original = self.config.to_dict()
        for environment, token in (({}, 'global-secret'), ({'VIKUNJA_URL': self.binding.server,
                                                          'VIKUNJA_TOKEN': 'session-secret'}, 'session-secret')):
            with self.subTest(session=bool(environment)), patch.dict(os.environ, environment), \
                 patch.object(runtime, 'load_config', return_value=self.config), \
                 patch.object(project_config, 'project_path', return_value=self.path):
                result = runtime.launch_config([(name, None) for name in environment])
                self.assertEqual(result.tracker.provider, 'vikunja')
                self.assertEqual(make_adapter(self.binding, config=result).client.token, token)
                self.assertEqual(result.tracker.connection('jira').url, 'https://project-jira.example')
        self.assertEqual(self.config.to_dict(), original)

    def test_container_ignores_unforwarded_process_account_in_its_snapshot(self):
        """A shell export alone must not grant a container the temporary account."""
        for environment in ({'VIKUNJA_URL': self.binding.server, 'VIKUNJA_TOKEN': 'session-secret'},
                            {'VIKUNJA_TOKEN': 'session-secret'}):
            with self.subTest(environment=environment), patch.dict(os.environ, environment), \
                 patch.object(runtime, 'load_config', return_value=self.config), \
                 patch.object(project_config, 'load', return_value={}), \
                 patch.object(runtime, 'TRACKER_RUNTIME_DIR', self.root / 'runtime'), \
                 patch.object(runtime, 'container_tracker_url', side_effect=lambda url: url):
                result = runtime.launch_config()
                snapshot = runtime.session_runtime(result, runtime_state=self.runtime_state)
                self.assertEqual(result.tracker.connection('vikunja').token, 'global-secret')
                self.assertNotIn('session-secret', snapshot.read_text())

    def test_container_selects_account_from_existing_environment_channels(self):
        """All explicit launch channels keep their existing overlay precedence."""
        from agent_box.config import EnvConfig
        pair = {'VIKUNJA_URL': self.binding.server, 'VIKUNJA_TOKEN': 'session-secret'}
        cases = [(EnvConfig(forward=list(pair)), pair, None),
                 (EnvConfig(set=pair), {}, None),
                 (EnvConfig(), pair, [(name, None) for name in pair]),
                 (EnvConfig(), {}, list(pair.items())),
                 (EnvConfig(), {'AGENT_BOX_ENV_' + name: value for name, value in pair.items()}, None)]
        for policy, environment, specs in cases:
            self.config.env = policy
            with self.subTest(policy=policy, specs=specs), patch.dict(os.environ, environment, clear=True), \
                 patch.object(runtime, 'load_config', return_value=self.config), \
                 patch.object(project_config, 'load', return_value={}):
                result = runtime.launch_config(specs)
                self.assertEqual(result.tracker.connection('vikunja').token, 'session-secret')

    def test_host_launch_inherits_process_account_without_forwarding(self):
        """Host execution and its snapshot must select the account inherited by the child."""
        from agent_box.workspace import Workspace
        pair = {'VIKUNJA_URL': self.binding.server, 'VIKUNJA_TOKEN': 'session-secret'}
        with patch.dict(os.environ, pair, clear=True), \
             patch.object(runtime, 'load_config', return_value=self.config), \
             patch.object(project_config, 'load', return_value={}), \
             patch.object(runtime, 'TRACKER_RUNTIME_DIR', self.root / 'runtime'), \
             patch.object(runtime, 'container_tracker_url', side_effect=lambda url: url), \
             patch.object(runtime.shutil, 'which', return_value='/fake/codex'), \
             patch.object(runtime, 'resolve_workspace', return_value=Workspace(self.root, self.root)), \
             patch.object(runtime.subprocess, 'call', return_value=0) as call:
            self.assertEqual(runtime.host_main('codex', []), 0)
            environment = call.call_args.kwargs['env']
            self.assertEqual(environment['VIKUNJA_TOKEN'], 'session-secret')
            snapshot = json.loads(Path(environment['AGENT_BOX_RUNTIME_CONFIG']).read_text())
            self.assertEqual(snapshot['tracker']['connections']['vikunja']['token'], 'session-secret')
            self.assertFalse(snapshot['container'])

    def test_launch_rejects_selected_account_mismatch_and_incomplete_session_pair(self):
        """The launcher must reject invalid account selection before any consumer can connect."""
        write_binding(self.root, self.binding)
        cases = [({}, 'https://wrong-global.example'),
                 ({'VIKUNJA_URL': 'https://wrong-session.example', 'VIKUNJA_TOKEN': 'session-secret'}, self.binding.server),
                 ({'VIKUNJA_URL': self.binding.server}, self.binding.server),
                 ({'VIKUNJA_TOKEN': 'session-secret'}, self.binding.server),
                 ({'VIKUNJA_URL': '', 'VIKUNJA_TOKEN': 'session-secret'}, self.binding.server),
                 ({'VIKUNJA_URL': self.binding.server, 'VIKUNJA_TOKEN': ''}, self.binding.server)]
        for environment, global_url in cases:
            self.config.tracker.connections['vikunja'].url = global_url
            with self.subTest(environment=environment, global_url=global_url), \
                 patch.dict(os.environ, environment), \
                 patch.object(runtime, 'load_config', return_value=self.config), \
                 patch.object(project_config, 'project_path', return_value=self.path), \
                 self.assertRaises(AgentBoxError):
                runtime.launch_config([(name, None) for name in environment])

    def test_session_snapshots_survive_other_sessions_and_global_disable(self):
        with patch.object(runtime, 'TRACKER_RUNTIME_DIR', self.root / 'runtime'), \
             patch.object(runtime, 'container_tracker_url', side_effect=lambda url: url):
            first = runtime.session_runtime(self.config, runtime_state=self.runtime_state)
            first_content = first.read_bytes()
            second_config = Config.from_dict(self.config.to_dict())
            second_config.workflow.auto_push = False
            second_config.tracker.connections['vikunja'].token = 'other-session-secret'
            second = runtime.session_runtime(second_config, container=False)
            runtime.sync_tracker_runtime(Config())
            self.assertNotEqual(first, second)
            self.assertEqual(first.read_bytes(), first_content)
            self.assertFalse(json.loads(second.read_text())['container'])
            self.assertEqual(first.stat().st_mode & 0o777, 0o600)
            self.assertNotIn('other-session-secret', first.read_text())

    def test_runtime_baseline_does_not_carry_one_projects_policy_into_another(self):
        first = project_config.apply(self.config, {'workflow': {'review': {'max_lines': 19}}})
        with patch.object(runtime, 'TRACKER_RUNTIME_DIR', self.root / 'runtime'), \
             patch.object(runtime, 'container_tracker_url', side_effect=lambda url: url):
            snapshot = runtime.session_runtime(first, container=False)
            with patch.dict(os.environ, {'AGENT_BOX_RUNTIME_CONFIG': str(snapshot)}), \
                 patch.object(project_config, 'load', return_value={}):
                self.assertEqual(effective_config().workflow.review.max_lines, 200)
                self.assertEqual(effective_config(include_project=False).workflow.review.max_lines, 200)
                with patch.object(project_config, 'load', return_value={'workflow': {'review': {'max_lines': 31}}}):
                    self.assertEqual(effective_config().workflow.review.max_lines, 31)

    def test_runtime_transport_matches_selected_account_without_reusing_old_endpoint(self):
        data = {'tracker': self.config.tracker.to_dict(),
                'tracker_urls': {'vikunja': 'http://host.containers.internal:8080'}, 'container': True}
        snapshot = self.root / 'runtime.json'
        snapshot.write_text(json.dumps(data))
        with patch.dict(os.environ, {'AGENT_BOX_RUNTIME_CONFIG': str(snapshot)}):
            adapter = make_adapter(self.binding, config=self.config)
            self.assertEqual(adapter.client.url, 'http://host.containers.internal:8080')
            with patch.dict(os.environ, {'VIKUNJA_URL': 'https://new.example', 'VIKUNJA_TOKEN': 'new-secret'}), \
                 patch.object(runtime, 'container_tracker_url', return_value='https://new.example'):
                adapter = make_adapter(Binding('vikunja', 'https://new.example', '12'), config=self.config)
                self.assertEqual(adapter.client.url, 'https://new.example')

    def test_duplicate_keys_cannot_hide_a_stored_credential_from_validation(self):
        self.path.write_text('{"tracker":{"connections":{"vikunja":{"token":"FAKE_SENTINEL"}}},"tracker":{}}')
        with self.assertRaises(AgentBoxError) as caught:
            project_config.load(self.path)
        self.assertNotIn('FAKE_SENTINEL', str(caught.exception))

    def test_disabled_integrations_share_only_nonsecret_policy_with_config_show(self):
        """Global browser/image inheritance remains visible without granting account access."""
        import contextlib
        import io
        from agent_box import cli
        self.config.tracker.enabled = False
        self.config.browser.enabled = True
        self.config.image.tools['virtualization'] = True
        self.config.env.set['PRIVATE_VALUE'] = 'not-for-the-box'
        data = {'workflow': {'auto_push': False}, 'image': {'tools': {'analysis': False}}}
        config = project_config.apply(self.config, data)
        with patch.object(runtime, 'TRACKER_RUNTIME_DIR', self.root / 'credentials'), \
             patch.object(runtime, 'POLICY_RUNTIME_DIR', self.root / 'policy'):
            snapshot = runtime.session_runtime(config, runtime_state=self.runtime_state)
            rendered = snapshot.read_text()
            self.assertNotIn('global-secret', rendered)
            self.assertNotIn('not-for-the-box', rendered)
            self.assertEqual(json.loads(rendered)['tracker']['connections'], {})
            self.assertEqual(runtime.tracker_mounts(config, self.runtime_state), [
                (self.root / 'policy/workspaces/workspace', runtime.CONTAINER_POLICY_RUNTIME_DIR, True)])
            self.assertFalse((self.root / 'credentials').exists())
            with patch.dict(os.environ, {'AGENT_BOX_RUNTIME_CONFIG': str(snapshot)}), \
                 patch.object(project_config, 'load', return_value=data), \
                 patch.object(cli, '_image_note', return_value=''), \
                 patch.object(cli, '_browser_note', return_value=''), \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                cli.config_show()
                effective = effective_config()
            self.assertTrue(effective.browser.enabled)
            self.assertTrue(effective.image.tools['virtualization'])
            self.assertFalse(effective.image.tools['analysis'])
            self.assertFalse(effective.workflow.auto_push)
            self.assertIn('Agent browser (containers)  on', output.getvalue())
