"""Project policy is strict, inherited, and cannot redirect another account's token."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_box import project_config, runtime
from agent_box.config import Config, TrackerConnection, effective_config, session_account
from agent_box.launch_options import LaunchOptions
from agent_box.errors import AgentBoxError
from agent_box.paths import HOST_LAUNCHER
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

    def test_partial_override_preserves_false_empty_list_and_global(self):
        self.config.workflow.review.models = [{"provider": "codex", "model": "global", "effort": "high", "speed": "inherit", "api_provider": ""}]
        original = self.config.to_dict()
        data = {'box': {'tools': {'browser': True}},
                'workflow': {'review': {'sensitive_paths': [], 'ignored_paths': ['**/*.md'], 'tiers': {'2': []}},
                             'auto_push': False}}
        project_config.validate(data, self.path)
        result = project_config.apply(self.config, data)
        self.assertEqual(result.workflow.review.sensitive_paths, [])
        self.assertEqual(result.workflow.review.ignored_paths, ['**/*.md'])
        self.assertEqual(result.workflow.review.models, self.config.workflow.review.models)
        self.assertEqual(result.workflow.review.tiers['2'], [])
        self.assertFalse(result.workflow.auto_push)
        self.assertTrue(result.box.tools.groups['browser'])
        self.assertTrue(result.box.tools.groups['analysis'])
        self.assertEqual(self.config.to_dict(), original)

    def test_a_project_brings_its_own_registry_mirrors(self):
        """Registry names are free-form keys, so the field rule must accept any of them."""
        mirrors = {'docker.io': ['https://mirror.example'], 'quay.io': ['https://quay-mirror.example']}
        data = {'box': {'k3s': {'registry_mirrors': mirrors}}}
        project_config.validate(data, self.path)
        self.assertEqual(project_config.apply(self.config, data).box.k3s.registry_mirrors, mirrors)

    def test_project_scout_limit_overrides_the_scout_section_without_changing_global(self):
        """A repository may allow fewer or more concurrent scouts; the host's own limit stays as saved."""
        data = {'workflow': {'scout': {'limit': 2}}}
        project_config.validate(data, self.path)
        self.assertEqual(project_config.apply(self.config, data).workflow.scout.limit, 2)
        self.assertEqual(self.config.workflow.scout.limit, 3)

    def test_explicit_sync_events_survive_project_and_runtime_overlays(self):
        """New event defaults must not widen a user's intentionally narrower sync policy."""
        self.config.workflow.enabled = True
        self.config.workflow.sync.events = ["after"]
        data = {"workflow": {"sync": {"events": ["before"]}}}
        project_config.save(self.path, data)
        effective = project_config.apply(self.config, data)
        self.assertEqual(effective.workflow.sync.events, ["before"])
        runtime.sync_tracker_runtime(self.config, self.root / "rendered")
        with patch.dict(os.environ, {"AGENT_BOX_RUNTIME_CONFIG": str(self.root / "rendered/config.json")}), \
                patch.object(project_config, "project_path", return_value=self.path):
            self.assertEqual(effective_config(include_project=False).workflow.sync.events, ["after"])
            self.assertEqual(effective_config().workflow.sync.events, ["before"])
        self.assertEqual(self.config.workflow.sync.events, ["after"])

    def test_project_role_lists_replace_global_lists_whole_and_reach_runtime(self):
        """A project list replaces the global one entirely; merging entries would reorder preferences silently."""
        global_models = [{"provider": "codex", "model": "global-codex", "effort": "high", "speed": "inherit", "api_provider": ""},
                         {"provider": "claude", "model": "global-claude", "effort": "medium", "speed": "inherit", "api_provider": ""}]
        self.config.workflow.worker.models = global_models
        original = self.config.to_dict()
        data = {'workflow': {'worker': {'models': [{'provider': 'claude', 'model': 'opus', 'effort': 'default'}]}}}
        project_config.save(self.path, data)
        result = project_config.apply(self.config, project_config.load(self.path))
        runtime.sync_tracker_runtime(result, self.root / 'rendered')
        artifact = self.root / 'rendered' / 'config.json'
        with patch.dict(os.environ, {'AGENT_BOX_RUNTIME_CONFIG': str(artifact)}), \
             patch.object(project_config, 'load', return_value=project_config.load(self.path)):
            rendered = effective_config()
            self.assertEqual(effective_config(include_project=False).workflow.worker.models, global_models)
        self.assertEqual(rendered.workflow.worker.models,
                         [{"provider": "claude", "model": "opus", "effort": "default", "speed": "inherit", "api_provider": ""}])
        self.assertEqual(rendered.workflow.review, self.config.workflow.review)
        self.assertEqual(self.config.to_dict(), original)
        for bad in ({'models': {'claude': {'model': 'opus'}}}, {'models': [{'provider': 'claude', 'model': 'opus', 'effort': 'ultra'}]},
                    {'models': [{'provider': 'gemini', 'model': 'x'}]}, {'tiers': {'1': [{'provider': 'codex', 'model': 'm', 'scope': 'all'}]}}):
            with self.subTest(bad=bad), self.assertRaises(AgentBoxError):
                project_config.validate({'workflow': {'worker': bad}}, self.path)

    def test_unknown_forbidden_and_malformed_values_never_fall_back(self):
        for data in ({'box': {'tools': {'browser': 1}}}, {'env': {}}, {'workflow': {'enabled': False}},
                     {'agents': {'default': 'codex'}}, {'claude': {'statusline': {'command': 'echo'}}},
                     {'workflow': {'unknown': 1}}, {'workflow': {'merge_mode': ''}},
                     {'workflow': {'review': {'sensitive_paths': [None]}}},
                     {'workflow': {'review': {'ignored_paths': [None]}}},
                     {'workflow': {'review': {'ignored_paths': '**/*.md'}}},
                     {'workflow': {'tiers': {'0': {'codex': {'effort': ''}}}}},
                     {'tracker': {'connections': {'vikunja': {'url': 'https://u:secret@host'}}}},
                     {'box': {'k3s': {'version': 'v1.33'}}}, {'box': {'k3s': {'lifetime': 'forever'}}},
                     [], None):
            with self.subTest(data=data), self.assertRaises(AgentBoxError):
                project_config.validate(data, self.path)
        self.path.write_text('{"secret-value":')
        with self.assertRaises(AgentBoxError) as caught:
            project_config.load(self.path)
        self.assertNotIn('secret-value', str(caught.exception))

    def test_project_role_scopes_reject_invalid_enums(self):
        """Project overrides must not silently restore global or default enum choices."""
        for path in (("worker", "override", "scope"),
                     ("review", "override", "scope")):
            for invalid in ("invalid", "", None, False, 1, [], {}):
                data = {"workflow": {}}
                target = data["workflow"]
                for part in path[:-1]:
                    target = target.setdefault(part, {})
                target[path[-1]] = invalid
                with self.subTest(path=path, invalid=invalid), self.assertRaises(AgentBoxError):
                    project_config.validate(data, self.path)

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

    def test_every_credential_path_of_the_sectioned_schema_is_named_as_forbidden(self):
        """A credential under its section must stop the commit, not read as an unknown field to fix."""
        for data, dotted in (({'credentials': {'github': {'token': True}}}, 'credentials.github.token'),
                             ({'host': {'notifications': {'telegram_token': 'x'}}}, 'host.notifications.telegram_token'),
                             ({'agents': {'external_apis': {}}}, 'agents.external_apis')):
            with self.subTest(path=dotted), self.assertRaises(AgentBoxError) as caught:
                project_config.validate(data, self.path)
            self.assertIn('PROJECT_CREDENTIALS_FORBIDDEN', str(caught.exception))
            self.assertIn(dotted, str(caught.exception))

    def test_binding_setup_and_cutover_preserve_project_overrides(self):
        project_config.save(self.path, {'box': {'tools': {'browser': True}}, 'workflow': {'auto_push': False}})
        write_binding(self.root, self.binding)
        self.assertEqual(Binding.load(self.root), self.binding)
        data = json.loads(setup_content(self.root, Binding('vikunja', self.binding.server, '25'))[self.path])
        self.assertTrue(data['box']['tools']['browser'])
        self.assertFalse(data['workflow']['auto_push'])
        self.assertEqual(data['tracker']['project'], '25')
        self.assertNotIn('server', data)

    def test_launch_settings_need_no_binding_but_tracker_binding_does(self):
        project_config.save(self.path, {'box': {'tools': {'browser': True}}})
        with patch.object(project_config, 'project_path', return_value=self.path):
            self.assertTrue(effective_config(self.config).box.tools.browser())
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

    def test_directory_beneath_bare_repository_uses_its_own_project_file(self):
        """A bare parent has Git metadata but no worktree, so child launch config stays local."""
        subprocess.run(['git', 'init', '--bare', '-q', str(self.root / '.git')], check=True)
        child = self.root / 'ordinary'
        child.mkdir()
        self.assertEqual(project_config.project_path(child), child / '.agent-box.json')
        from agent_box.repo_binding import repository_root
        with self.assertRaisesRegex(AgentBoxError, 'inside a Git worktree'):
            repository_root(child)

    def test_cli_help_loads_project_config_beneath_bare_repository(self):
        """CLI startup must not reject an ordinary directory before parsing commands."""
        subprocess.run(['git', 'init', '--bare', '-q', str(self.root / '.git')], check=True)
        child = self.root / 'ordinary'
        child.mkdir()
        result = subprocess.run([sys.executable, str(HOST_LAUNCHER), '--help'], cwd=child,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

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

    def test_session_tracker_selects_its_account_without_touching_saved_files(self):
        """--tracker/--project bind the session and bring a token by name; neither file changes."""
        write_binding(self.root, self.binding)
        saved = self.path.read_bytes()
        original = self.config.to_dict()
        with patch.dict(os.environ, {'VIKUNJA_TOKEN': 'session-secret'}), \
             patch.object(runtime, 'load_config', return_value=self.config), \
             patch.object(project_config, 'project_path', return_value=self.path), \
             patch.object(runtime, 'TRACKER_RUNTIME_DIR', self.root / 'runtime'), \
             patch.object(runtime, 'container_tracker_url', side_effect=lambda url: url):
            result = runtime.launch_config(LaunchOptions(overrides={'tracker': 'https://other.example', 'project': '34'}))
            snapshot = runtime.session_runtime(result, container=False)
        self.assertEqual(self.config.to_dict(), original)
        self.assertEqual(self.path.read_bytes(), saved)
        with patch.dict(os.environ, {'AGENT_BOX_RUNTIME_CONFIG': str(snapshot)}, clear=True), \
             patch.object(project_config, 'project_path', return_value=self.path):
            binding = Binding.session(self.root)
            self.assertEqual((binding.server, binding.project), ('https://other.example', '34'))
            self.assertEqual(Binding.load(self.root), self.binding)
            adapter = make_adapter(binding)
            self.assertEqual((adapter.client.url, adapter.client.token), ('https://other.example', 'session-secret'))

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
        for overrides, token in (({}, 'global-secret'), ({'tracker': self.binding.server}, 'session-secret')):
            with self.subTest(session=bool(overrides)), patch.dict(os.environ, {'VIKUNJA_TOKEN': 'session-secret'}), \
                 patch.object(runtime, 'load_config', return_value=self.config), \
                 patch.object(project_config, 'project_path', return_value=self.path):
                result = runtime.launch_config(LaunchOptions(overrides=overrides))
                self.assertEqual(result.tracker.provider, 'vikunja')
                self.assertEqual(make_adapter(self.binding, config=result).client.token, token)
                self.assertEqual(result.tracker.connection('jira').url, 'https://project-jira.example')
        self.assertEqual(self.config.to_dict(), original)

    def test_container_ignores_unforwarded_process_account_in_its_snapshot(self):
        """A shell export alone, without --tracker, must not grant a session another account."""
        for environment in ({'VIKUNJA_TOKEN': 'session-secret'},):
            with self.subTest(environment=environment), patch.dict(os.environ, environment), \
                 patch.object(runtime, 'load_config', return_value=self.config), \
                 patch.object(project_config, 'load', return_value={}), \
                 patch.object(runtime, 'TRACKER_RUNTIME_DIR', self.root / 'runtime'), \
                 patch.object(runtime, 'container_tracker_url', side_effect=lambda url: url):
                result = runtime.launch_config()
                snapshot = runtime.session_runtime(result, runtime_state=self.runtime_state)
                self.assertEqual(result.tracker.connection('vikunja').token, 'global-secret')
                self.assertNotIn('session-secret', snapshot.read_text())

    def test_session_tracker_token_comes_only_from_the_named_shell_variable(self):
        """Environment lists and a blank export cannot stand in for the token --tracker reads by name."""
        self.config.tracker.provider = 'vikunja'
        self.config.box.env.set = {'VIKUNJA_TOKEN': 'from-config'}
        for environment in ({}, {'VIKUNJA_TOKEN': ' '}):
            with self.subTest(environment=environment), patch.dict(os.environ, environment, clear=True), \
                 patch.object(runtime, 'load_config', return_value=self.config), \
                 patch.object(project_config, 'load', return_value={}), \
                 self.assertRaisesRegex(AgentBoxError, 'VIKUNJA_TOKEN'):
                runtime.launch_config(LaunchOptions(overrides={'tracker': self.binding.server}))

    def test_host_session_snapshot_carries_the_session_tracker_account(self):
        """A host child reads the snapshot, so the --tracker token must be there and not in argv."""
        self.config.tracker.provider = 'vikunja'
        from agent_box.workspace import Workspace
        with patch.dict(os.environ, {'VIKUNJA_TOKEN': 'session-secret'}, clear=True), \
             patch.object(runtime, 'load_config', return_value=self.config), \
             patch.object(project_config, 'load', return_value={}), \
             patch.object(runtime, 'TRACKER_RUNTIME_DIR', self.root / 'runtime'), \
             patch.object(runtime, 'container_tracker_url', side_effect=lambda url: url), \
             patch.object(runtime.shutil, 'which', return_value='/fake/codex'), \
             patch.object(runtime, 'resolve_workspace', return_value=Workspace(self.root, self.root)), \
             patch.object(runtime.subprocess, 'call', return_value=0) as call:
            options = LaunchOptions(host=True, overrides={'tracker': 'https://other.example'})
            self.assertEqual(runtime.host_main('codex', [], options), 0)
            self.assertNotIn('session-secret', str(call.call_args.args))
            snapshot = json.loads(Path(call.call_args.kwargs['env']['AGENT_BOX_RUNTIME_CONFIG']).read_text())
            self.assertEqual(snapshot['tracker']['connections']['vikunja'],
                             {'url': 'https://other.example', 'token': 'session-secret'})
            self.assertEqual(snapshot['launch'], {'tracker': 'https://other.example'})
            self.assertFalse(snapshot['container'])

    def test_launch_rejects_an_account_for_another_server_unless_tracker_names_one(self):
        """The saved account must match the binding; --tracker is the one way to point elsewhere."""
        write_binding(self.root, self.binding)
        self.config.tracker.connections['vikunja'].url = 'https://wrong-global.example'
        with patch.dict(os.environ, {'VIKUNJA_TOKEN': 'session-secret'}), \
             patch.object(runtime, 'load_config', return_value=self.config), \
             patch.object(project_config, 'project_path', return_value=self.path):
            with self.assertRaisesRegex(AgentBoxError, 'different tracker server'):
                runtime.launch_config()
            result = runtime.launch_config(LaunchOptions(overrides={'tracker': 'https://another.example'}))
        self.assertEqual(session_account(result).connection('vikunja').url, 'https://another.example')

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

    def test_stale_session_snapshot_names_itself_and_restart_instead_of_a_bad_setting(self):
        """A schema change after launch must not look like a mistake in the user's configuration."""
        snapshot = self.root / 'snapshot.json'
        # Older shapes fail either while validating or already while loading.
        validating = Config().to_dict()
        validating['workflow']['review']['tiers'] = {'1': {'provider': 'codex'}}
        loading = {**Config().to_dict(), 'agents': {'external_apis': {'legacy': 'openai'}}}
        for rendered, cause in ((validating, r"workflow\.review\.tiers\.1"), (loading, r"agents\.external_apis\.legacy")):
            snapshot.write_text(json.dumps(rendered))
            with self.subTest(cause=cause), patch.dict(os.environ, {'AGENT_BOX_RUNTIME_CONFIG': str(snapshot)}), \
                 patch.object(project_config, 'load', return_value={}):
                with self.assertRaisesRegex(AgentBoxError, r"snapshot was rendered at launch.*"
                                            + cause + r".*Restart the agent-box session"):
                    effective_config()

    def test_runtime_transport_matches_selected_account_without_reusing_old_endpoint(self):
        data = {'tracker': self.config.tracker.to_dict(),
                'tracker_urls': {'vikunja': 'http://host.containers.internal:8080'}, 'container': True}
        snapshot = self.root / 'runtime.json'
        snapshot.write_text(json.dumps(data))
        with patch.dict(os.environ, {'AGENT_BOX_RUNTIME_CONFIG': str(snapshot)}):
            adapter = make_adapter(self.binding, config=self.config)
            self.assertEqual(adapter.client.url, 'http://host.containers.internal:8080')
            other = Config.from_dict(self.config.to_dict())
            other.tracker.connections['vikunja'] = TrackerConnection('https://new.example', 'new-secret')
            with patch.object(runtime, 'container_tracker_url', return_value='https://new.example'):
                adapter = make_adapter(Binding('vikunja', 'https://new.example', '12'), config=other)
                self.assertEqual(adapter.client.url, 'https://new.example')

    def test_duplicate_keys_cannot_hide_a_stored_credential_from_validation(self):
        self.path.write_text('{"tracker":{"connections":{"vikunja":{"token":"FAKE_SENTINEL"}}},"tracker":{}}')
        with self.assertRaises(AgentBoxError) as caught:
            project_config.load(self.path)
        self.assertNotIn('FAKE_SENTINEL', str(caught.exception))

    def test_disabled_integrations_share_only_nonsecret_policy_with_config_show(self):
        """Global tool group inheritance remains visible without granting account access."""
        import contextlib
        import io
        from agent_box import cli
        self.config.tracker.enabled = False
        self.config.box.tools.groups['browser'] = True
        self.config.box.tools.groups['virtualization'] = True
        self.config.box.env.set['PRIVATE_VALUE'] = 'not-for-the-box'
        data = {'workflow': {'auto_push': False}, 'box': {'tools': {'analysis': False}}}
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
            self.assertTrue(effective.box.tools.browser())
            self.assertTrue(effective.box.tools.groups['virtualization'])
            self.assertFalse(effective.box.tools.groups['analysis'])
            self.assertFalse(effective.workflow.auto_push)
            from test_workflow_config import show_paths
            self.assertEqual(show_paths(output.getvalue())['box.tools.browser'], 'on')
