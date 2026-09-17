"""API identity must survive routing, preparation and accepted managed evidence."""
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock

from agent_box import agent_profiles, external_launch, provider_runner, routing, workflow, workflow_state
from agent_box.errors import AgentBoxError
from test_external_config import external_config
import workflow_fixtures


def candidate(provider='codex'):
    return {'provider': provider, 'model': 'external-small', 'effort': 'high', 'fast': False, 'api_provider': 'deepseek'}


def configure(policy, role, tier, models):
    """Untiered work reads the role's own list; a tier reads its tier list."""
    settings = policy.role(role)
    if tier is None:
        settings.models = models
    else:
        settings.tiers[str(tier)] = models


class ExternalRoutingTests(unittest.TestCase):
    def test_reviewer_fallback_preserves_each_candidates_api_identity(self):
        """Fallback may change CLI/API, but the accepted evidence must identify the one that ran."""
        config = external_config()
        native = {'provider': 'codex', 'model': 'native-model', 'effort': 'high', 'fast': False, 'tier': 1}
        external = {'provider': 'claude', 'model': 'external-small', 'effort': 'high', 'fast': False, 'tier': 1,
                    **external_launch.route_identity(config, 'deepseek', 'claude')}
        for first, second in ((native, external), (external, native)):
            selected = {**first, 'fallbacks': [second]}
            with patch.object(workflow, 'run', side_effect=[provider_runner.ProviderFailure('quota', 'quota'),
                              {'verdict': 'PASS', 'summary': 'Reviewed', 'findings': []}]):
                output = workflow.execute(config.workflow, selected, 'reviewer', Path.cwd(), 'review')
            self.assertEqual(output['route'], second)
            self.assertEqual(output['failures'][0].get('api_provider'), first.get('api_provider'))

    def test_all_roles_and_role_lists_bind_the_api_without_native_profiles(self):
        """A model name alone cannot identify which API should receive its request."""
        for cli in ('codex', 'claude'):
            for role, tier, override in [('worker', 1, False), ('scout', 0, False), ('reviewer', 2, False),
                                         ('worker', None, True), ('reviewer', None, True)]:
                with self.subTest(cli=cli, role=role, override=override), tempfile.TemporaryDirectory() as tmp:
                    config = external_config()
                    policy = config.workflow
                    policy.review.routing = 'same-provider'
                    configure(policy, role, tier, [candidate(cli)])
                    with patch.dict(os.environ, {}, clear=True), \
                         patch.object(workflow, 'WORKFLOW_STATE', Path(tmp)), \
                         patch.object(workflow_state, 'WORKFLOW_STATE', Path(tmp)), \
                         patch.object(workflow.shutil, 'which', return_value='/bin/cli'):
                        route = workflow.route(config, role, tier, cli)
                    self.assertEqual(route['api_provider'], 'deepseek')
                    self.assertEqual(route['provider'], cli)
                    self.assertTrue(route['managed_only'])
                    self.assertIsNone(route['native_profile'])
                    self.assertNotIn('private-api-key', json.dumps(route))
                    args = SimpleNamespace(provider=cli, model='external-small', effort='high')
                    with self.assertRaisesRegex(AgentBoxError, 'managed CLI'):
                        workflow.native_output(args, route, tier)

    def test_subscription_exhaustion_never_excludes_an_external_candidate(self):
        """Subscription capacity is not the budget of a separately billed API."""
        config = external_config()
        policy = config.workflow
        policy.worker.tiers['1'] = [candidate(), candidate()]
        exhausted = {'codex': {'fresh': True, 'windows': [{'remaining': 0, 'reset_at': 10000, 'seconds': 604800}]}}
        route = routing.select(policy, 'worker', 1, 'codex', exhausted, now=1)
        self.assertEqual(route['api_provider'], 'deepseek')
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True), \
             patch.object(workflow, 'WORKFLOW_STATE', Path(tmp)), patch.object(workflow_state, 'WORKFLOW_STATE', Path(tmp)), \
             patch.object(workflow.shutil, 'which', return_value='/bin/cli'), patch.object(workflow, 'usage_snapshot') as usage:
            workflow.route(config, 'worker', 1, 'codex')
        usage.assert_not_called()

    def test_native_builtin_candidates_are_disabled_inside_an_external_session(self):
        """A native child would inherit its parent's external endpoint despite the built-in ID."""
        config = external_config()
        spec = {'provider': 'codex', 'model': 'native-model', 'effort': 'high', 'tier': 1, 'fallbacks': []}
        identity = external_launch.route_identity(config, 'deepseek', 'codex')
        with patch.dict(os.environ, {'AGENT_BOX_API_PROVIDER': 'deepseek',
                                   'AGENT_BOX_API_CLI': 'codex',
                                   'AGENT_BOX_API_FINGERPRINT': identity['api_fingerprint']}):
            workflow.bind_api_route(spec, config)
            self.assertTrue(spec['managed_only'])
            self.assertNotIn('api_provider', spec)
            args = SimpleNamespace(provider='codex', model='native-model', effort='high')
            with self.assertRaisesRegex(AgentBoxError, 'managed CLI'):
                workflow.native_output(args, spec, 1)
            args = SimpleNamespace(primary='codex', session_model=None, session_effort=None)
            with self.assertRaisesRegex(AgentBoxError, 'verified --session-model'):
                workflow.session_route(args, 'worker', config)

    def test_candidate_on_the_sessions_own_api_stays_native(self):
        """A native child reaches exactly the session's API, so a separate process buys nothing there.

        Any other identity still needs one: the child cannot switch endpoints, and a
        report from a session without the prepared identity cannot show where it ran.
        """
        for cli, other in (('codex', 'claude'), ('claude', 'codex')):
            for role, tier, override in [('worker', 1, False), ('scout', 0, False), ('reviewer', 2, False),
                                         ('worker', None, True), ('reviewer', None, True)]:
                with self.subTest(cli=cli, role=role, override=override), tempfile.TemporaryDirectory() as tmp:
                    config = external_config()
                    policy = config.workflow
                    policy.review.routing = 'same-provider'
                    _, env = external_launch.prepare([cli], 'deepseek', cli, config, tmp, {})
                    with patch.dict(os.environ, env, clear=True), \
                         patch.object(workflow, 'WORKFLOW_STATE', Path(tmp)), \
                         patch.object(workflow_state, 'WORKFLOW_STATE', Path(tmp)), \
                         patch.object(workflow.shutil, 'which', return_value='/bin/cli'):
                        configure(policy, role, tier, [candidate(cli), candidate(other)])
                        route = workflow.route(config, role, tier, cli)
                        configure(policy, role, tier, [candidate(other), candidate(cli)])
                        elsewhere = workflow.route(config, role, tier, other)
                    self.assertFalse(route.get('managed_only'))
                    self.assertEqual(route['api_fingerprint'], env[external_launch.FINGERPRINT_ENV])
                    self.assertTrue(elsewhere['managed_only'])
                    self.assertNotEqual(elsewhere['api_fingerprint'], route['api_fingerprint'])
                    if role != 'reviewer':
                        continue
                    path = Path(tmp) / 'report.json'
                    path.write_text(json.dumps({'verdict': 'PASS', 'summary': 'Reviewed', 'findings': []}))
                    args = SimpleNamespace(provider=cli, model='external-small', effort='high', role=role,
                                           file=str(path), fresh_context=True)
                    with patch.dict(os.environ, env, clear=True):
                        output = workflow.native_output(args, route, tier)
                    self.assertEqual((output['route']['api_provider'], output['route']['api_fingerprint']),
                                     (route['api_provider'], route['api_fingerprint']))
                    with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(AgentBoxError, 'managed CLI'):
                        workflow.native_output(args, route, tier)

    def test_session_route_from_the_same_external_session_stays_native(self):
        """Current-session delegation inherits the session's API; only a session without it is refused."""
        config = external_config()
        with tempfile.TemporaryDirectory() as tmp:
            _, env = external_launch.prepare(['codex'], 'deepseek', 'codex', config, tmp, {})
            report = Path(tmp) / 'report.json'
            report.write_text(json.dumps({'verdict': 'PASS', 'summary': 'Reviewed', 'findings': []}))
            session = SimpleNamespace(primary='codex', session_model='external-small', session_effort='high')
            args = SimpleNamespace(provider='codex', model='external-small', effort='high', role='reviewer',
                                   file=str(report), fallback_reason=None, fresh_context=True, session_inherited=False)
            with patch.dict(os.environ, env, clear=True):
                route = workflow.session_route(session, 'reviewer', config)
                self.assertFalse(route.get('managed_only'))
                output = workflow.native_output(args, route, None)
            self.assertEqual(output['route']['api_fingerprint'], env[external_launch.FINGERPRINT_ENV])
            with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(AgentBoxError, 'session API differs'):
                workflow.native_output(args, route, None)

    def test_active_api_identity_survives_config_edits_and_rejects_other_sessions(self):
        """Delegation from an external session binds the launched endpoint; config edits cannot substitute it."""
        config = external_config()
        with tempfile.TemporaryDirectory() as tmp:
            _, env = external_launch.prepare(['codex'], 'deepseek', 'codex', config, tmp, {})
            args = SimpleNamespace(primary='codex', session_model='external-small', session_effort='high')
            config.external_providers['deepseek'].codex.url = 'https://changed.example/v1'
            with patch.dict(os.environ, env, clear=True):
                with self.assertRaisesRegex(AgentBoxError, 'definition changed'):
                    workflow.session_route(args, 'worker', config)
                with patch.dict(os.environ, {external_launch.CLI_ENV: 'claude'}):
                    with self.assertRaisesRegex(AgentBoxError, 'differs from the CLI'):
                        workflow.session_route(args, 'worker', config)
            self.assertFalse(any(k in external_launch.builtin_environment(env) for k in
                                 (external_launch.PROVIDER_ENV, external_launch.FINGERPRINT_ENV, external_launch.CLI_ENV)))

    def test_reverse_claude_fast_checks_the_restored_cloud_environment(self):
        """Preflight must check the same provider environment that the managed process receives."""
        with tempfile.TemporaryDirectory() as tmp:
            _, env = external_launch.prepare(['codex'], 'deepseek', 'codex', external_config(), tmp,
                                              {'CLAUDE_CODE_USE_BEDROCK': '1'})
            with patch.dict(os.environ, env, clear=True), \
                 self.assertRaisesRegex(provider_runner.ProviderFailure, 'CLAUDE_CODE_USE_BEDROCK'):
                provider_runner.check_settings({'provider': 'claude', 'model': 'claude-opus-5',
                                                'effort': 'high', 'fast': True})

    def test_owned_native_profiles_are_removed_for_external_candidates(self):
        """Applying a configured API must not leave an owned profile pointing at a built-in endpoint."""
        config = external_config()
        config.workflow.enabled = True
        config.workflow.worker.tiers['1'] = [{**candidate(), 'api_provider': ''}]
        with tempfile.TemporaryDirectory() as tmp:
            codex, claude = Path(tmp) / 'codex', Path(tmp) / 'claude'
            agent_profiles.sync(config, codex, claude)
            path = codex / 'agents/ab-worker-1.toml'
            self.assertTrue(path.exists())
            config.workflow.worker.tiers['1'][0]['api_provider'] = 'deepseek'
            agent_profiles.sync(config, codex, claude)
            self.assertFalse(path.exists())
        config.workflow.worker.tiers['1'][0]['fast'] = True
        self.assertTrue(any('external API' in e for e in config.workflow.validate()))

    def test_endpoint_drift_is_refused_before_process_creation_without_key_fingerprints(self):
        """The prepared endpoint is stable; credential rotation remains private."""
        config = external_config()
        route = {'provider': 'codex', 'model': 'external-small', 'effort': 'high',
                 **external_launch.route_identity(config, 'deepseek', 'codex')}
        config.external_providers['deepseek'].api_key = 'rotated-key'
        self.assertEqual(route['api_fingerprint'], external_launch.route_identity(config, 'deepseek', 'codex')['api_fingerprint'])
        config.external_providers['deepseek'].codex.url = 'https://changed.example/v1'
        with patch('agent_box.config.effective_config', return_value=config), \
             patch.object(provider_runner.subprocess, 'Popen') as process, \
             self.assertRaisesRegex(AgentBoxError, 'definition changed'):
            provider_runner.run(route, 'worker', Path.cwd(), 'work')
        process.assert_not_called()

    def test_managed_builtin_reverse_call_restores_the_original_auth(self):
        """The managed runner must not send an external API token to the built-in provider."""
        config = external_config()
        report = '## Done\nWorked.'
        with tempfile.TemporaryDirectory() as tmp:
            _, env = external_launch.prepare(['codex'], 'deepseek', 'codex', config, tmp,
                                              {'OPENAI_API_KEY': 'original-key', 'PATH': os.environ['PATH']})
            def popen(command, **kwargs):
                self.assertEqual(kwargs['env']['OPENAI_API_KEY'], 'original-key')
                self.assertNotIn(external_launch.API_ENV, kwargs['env'])
                self.assertNotIn(external_launch.PROVIDER_ENV, kwargs['env'])
                Path(command[command.index('-o') + 1]).write_text(report)
                return Mock(returncode=0)
            with patch.dict(os.environ, env, clear=True), patch.object(provider_runner.subprocess, 'Popen', side_effect=popen):
                self.assertEqual(provider_runner.run({'provider': 'codex', 'model': 'native-model', 'effort': 'high'},
                                                      'worker', Path.cwd(), 'work'), report)


class ExternalDelegateTests(unittest.TestCase):
    def test_delegated_worker_runs_its_external_candidate_through_that_api(self):
        """The candidate's API travels to the managed run; the key stays out of the answer and its metadata."""
        fixture = workflow_fixtures.WorkflowCase()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.config.external_providers = external_config().external_providers
        fixture.config.workflow.worker.tiers['1'] = [candidate()]
        def run(route, *args, **kwargs):
            self.assertEqual(route['api_provider'], 'deepseek')
            return '## Done\nWorked.'
        with patch.object(workflow, 'run', side_effect=run):
            output = fixture.run_workflow('delegate', '1', '--role', 'worker', '--brief', str(fixture.review_brief))
        self.assertEqual(output['route']['api_provider'], 'deepseek')
        self.assertEqual(output['route']['api_fingerprint'],
                         external_launch.route_identity(fixture.config, 'deepseek', 'codex')['api_fingerprint'])
        self.assertNotIn('private-api-key', json.dumps(output))

