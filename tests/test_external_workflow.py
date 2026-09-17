"""API identity must survive routing, preparation and accepted managed evidence."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

from agent_box import agent_profiles, external_launch, provider_runner, routing, workflow, workflow_state
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
                              workflow_fixtures.answer()]):
                output = workflow.execute(config.workflow, selected, 'reviewer', Path.cwd(), 'review')
            self.assertEqual(output['route'], second)
            self.assertEqual(output['failures'][0].get('api_provider'), first.get('api_provider'))

    def test_all_roles_and_role_lists_bind_the_api(self):
        """A model name alone cannot identify which API should receive its request."""
        for cli in ('codex', 'claude'):
            for role, tier, override in [('worker', 1, False), ('reviewer', 2, False),
                                         ('worker', None, True), ('scout', None, True), ('reviewer', None, True)]:
                with self.subTest(cli=cli, role=role, override=override), tempfile.TemporaryDirectory() as tmp:
                    config = external_config()
                    policy = config.workflow
                    configure(policy, role, tier, [candidate(cli)])
                    with patch.dict(os.environ, {}, clear=True), \
                         patch.object(workflow, 'WORKFLOW_STATE', Path(tmp)), \
                         patch.object(workflow_state, 'WORKFLOW_STATE', Path(tmp)), \
                         patch.object(workflow.shutil, 'which', return_value='/bin/cli'):
                        route = workflow.candidates(config, role, tier)
                    self.assertEqual(route['api_provider'], 'deepseek')
                    self.assertEqual(route['provider'], cli)
                    self.assertEqual(route['api_fingerprint'],
                                     external_launch.route_identity(config, 'deepseek', cli)['api_fingerprint'])
                    self.assertNotIn('private-api-key', json.dumps(route))

    def test_subscription_exhaustion_never_excludes_an_external_candidate(self):
        """Subscription capacity is not the budget of a separately billed API."""
        config = external_config()
        policy = config.workflow
        policy.worker.tiers['1'] = [candidate(), candidate()]
        exhausted = {'codex': {'fresh': True, 'windows': [{'remaining': 0, 'reset_at': 10000, 'seconds': 604800}]}}
        route = routing.select(policy, 'worker', 1, exhausted, now=1)
        self.assertEqual(route['api_provider'], 'deepseek')
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True), \
             patch.object(workflow, 'WORKFLOW_STATE', Path(tmp)), patch.object(workflow_state, 'WORKFLOW_STATE', Path(tmp)), \
             patch.object(workflow.shutil, 'which', return_value='/bin/cli'), patch.object(workflow, 'usage_snapshot') as usage:
            workflow.candidates(config, 'worker', 1)
        usage.assert_not_called()

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
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(provider_runner, 'WORKFLOW_STATE', Path(tmp)), \
             patch('agent_box.config.effective_config', return_value=config), \
             patch.object(provider_runner.subprocess, 'Popen') as process:
            with self.assertRaisesRegex(provider_runner.ProviderFailure, 'definition changed') as caught:
                provider_runner.run(route, 'worker', Path.cwd(), 'work')
            process.assert_not_called()
            # A delegate reports this like any refused launch: a category and a readable diagnostic.
            self.assertEqual((caught.exception.category, caught.exception.stage), ('unsupported', 'preflight'))
            self.assertTrue((Path(tmp) / caught.exception.diagnostic['artifact']).exists())

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
                self.assertNotIn(external_launch.BASELINE_ENV, kwargs['env'])
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

