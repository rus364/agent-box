"""API identity must survive routing, preparation and accepted managed evidence."""
import base64
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
import test_workflow_handoff as handoff_tests


def candidate():
    return {'model': 'external-small', 'effort': 'high', 'fast': False, 'api_provider': 'deepseek'}


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

    def test_all_roles_and_overrides_bind_the_api_without_native_profiles(self):
        """A model name alone cannot identify which API should receive its request."""
        for cli in ('codex', 'claude'):
            for role, tier, override in [('worker', 1, False), ('scout', 0, False), ('reviewer', 2, False),
                                         ('worker', None, True), ('reviewer', None, True)]:
                with self.subTest(cli=cli, role=role, override=override), tempfile.TemporaryDirectory() as tmp:
                    config = external_config()
                    policy = config.workflow
                    policy.routing = 'current'
                    policy.review.routing = 'same-provider'
                    if override:
                        spec = (policy.review if role == 'reviewer' else policy.worker).override
                        spec.scope = 'all'
                        spec.models[cli] = candidate()
                    else:
                        matrix = policy.review.tiers if role == 'reviewer' else policy.tiers
                        matrix[str(tier)][cli] = candidate()
                    with patch.dict(os.environ, {}, clear=True), \
                         patch.object(workflow, 'WORKFLOW_STATE', Path(tmp)), \
                         patch.object(workflow_state, 'WORKFLOW_STATE', Path(tmp)), \
                         patch.object(workflow.shutil, 'which', return_value='/bin/cli'):
                        route = workflow.route(config, role, tier, cli, cli)
                    self.assertEqual(route['api_provider'], 'deepseek')
                    self.assertEqual(route['provider'], cli)
                    self.assertTrue(route['managed_only'])
                    self.assertIsNone(route['native_explicit_settings'])
                    self.assertFalse(route['native_profile_ready'])
                    self.assertNotIn('private-api-key', json.dumps(route))
                    args = SimpleNamespace(provider=cli, model='external-small', effort='high')
                    with self.assertRaisesRegex(AgentBoxError, 'managed CLI'):
                        workflow.native_output(args, route, tier)

    def test_subscription_exhaustion_never_excludes_an_external_candidate(self):
        """Subscription capacity is not the budget of a separately billed API."""
        config = external_config()
        policy = config.workflow
        policy.routing = 'usage-aware'
        policy.tiers['1']['codex'] = candidate()
        exhausted = {'codex': {'fresh': True, 'windows': [{'remaining': 0, 'reset_at': 10000, 'seconds': 604800}]}}
        route = routing.select(policy, 'worker', 1, 'codex', {}, exhausted, now=1)
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
        with patch.dict(os.environ, {'AGENT_BOX_API_PROVIDER': 'deepseek'}):
            workflow.bind_api_route(spec, config)
            self.assertTrue(spec['managed_only'])
            self.assertNotIn('api_provider', spec)
            args = SimpleNamespace(provider='codex', model='native-model', effort='high')
            with self.assertRaisesRegex(AgentBoxError, 'managed CLI'):
                workflow.native_output(args, spec, 1)
            args = SimpleNamespace(primary='codex', session_model=None, session_effort=None)
            direct = workflow.session_route(args, 'worker', config)
            self.assertEqual(direct['api_provider'], 'deepseek')
            self.assertNotIn('managed_only', direct)
            with self.assertRaisesRegex(AgentBoxError, 'verified --session-model'):
                workflow.session_route(args, 'worker', config, delegated=True)
            args.session_model, args.session_effort = 'external-small', 'high'
            self.assertTrue(workflow.session_route(args, 'worker', config, delegated=True)['managed_only'])

    def test_owned_native_profiles_are_removed_for_external_candidates(self):
        """Applying a configured API must not leave an owned profile pointing at a built-in endpoint."""
        config = external_config()
        config.workflow.enabled = True
        config.workflow.tiers['1']['codex'] = {**candidate(), 'api_provider': ''}
        with tempfile.TemporaryDirectory() as tmp:
            codex, claude = Path(tmp) / 'codex', Path(tmp) / 'claude'
            agent_profiles.sync(config, codex, claude)
            path = codex / 'agents/ab-worker-1.toml'
            self.assertTrue(path.exists())
            config.workflow.tiers['1']['codex']['api_provider'] = 'deepseek'
            agent_profiles.sync(config, codex, claude)
            self.assertFalse(path.exists())
        config.workflow.tiers['1']['codex']['fast'] = True
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
        report = {'status': 'completed', 'summary': 'Done', 'evidence': []}
        with tempfile.TemporaryDirectory() as tmp:
            _, env = external_launch.prepare(['codex'], 'deepseek', 'codex', config, tmp,
                                              {'OPENAI_API_KEY': 'original-key', 'PATH': os.environ['PATH']})
            def popen(command, **kwargs):
                self.assertEqual(kwargs['env']['OPENAI_API_KEY'], 'original-key')
                self.assertNotIn(external_launch.API_ENV, kwargs['env'])
                self.assertNotIn(external_launch.PROVIDER_ENV, kwargs['env'])
                Path(command[command.index('-o') + 1]).write_text(json.dumps(report))
                return Mock(returncode=0)
            with patch.dict(os.environ, env, clear=True), patch.object(provider_runner.subprocess, 'Popen', side_effect=popen):
                self.assertEqual(provider_runner.run({'provider': 'codex', 'model': 'native-model', 'effort': 'high'},
                                                      'worker', Path.cwd(), 'work'), report)


class ExternalDescriptorTests(unittest.TestCase):
    def test_prepared_api_survives_managed_execution_and_native_report_is_refused(self):
        """Exercise the lifecycle descriptor, not just the route dictionary helper."""
        real_route = workflow.route
        fixture = handoff_tests.WorkerHandoffTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.config.external_providers = external_config().external_providers
        fixture.config.workflow.routing = 'current'
        fixture.config.workflow.tiers['1']['codex'] = candidate()
        with patch.object(workflow, 'route', side_effect=real_route):
            prepared = fixture.run_workflow('prepare', '1', '--primary', 'codex')
        descriptor = json.loads(base64.urlsafe_b64decode(prepared['handoff']))
        self.assertEqual(descriptor['route']['api_provider'], 'deepseek')
        self.assertNotIn('private-api-key', json.dumps(descriptor))
        with self.assertRaisesRegex(AgentBoxError, 'managed CLI'):
            fixture.run_workflow('report', '1', '--role', 'worker', '--provider', 'codex', '--model', 'external-small',
                                 '--effort', 'high', '--file', str(fixture.report), '--scope', prepared['scope'],
                                 '--handoff', prepared['handoff'])
        def run(route, *args, **kwargs):
            self.assertEqual(route['api_provider'], 'deepseek')
            return {'status': 'completed', 'summary': 'Done', 'evidence': []}
        with patch.object(workflow, 'run', side_effect=run):
            output = fixture.run_workflow('run', '1', '--handoff', prepared['handoff'], '--brief', str(fixture.review_brief))
        self.assertEqual(output['route']['api_fingerprint'], descriptor['route']['api_fingerprint'])
        self.assertEqual(output['route']['api_provider'], 'deepseek')
        self.assertEqual(workflow.read_evidence(fixture.root, fixture.task)['worker']['route'], output['route'])
