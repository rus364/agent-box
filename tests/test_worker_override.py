"""Global worker candidates replace model sources without replacing provider policy."""
import unittest

from agent_box.config import WorkflowConfig
from agent_box.errors import AgentBoxError
from agent_box.routing import select


class WorkerOverrideRoutingTests(unittest.TestCase):
    def config(self):
        config = WorkflowConfig()
        config.worker.override.scope = "untiered"
        for provider in ('codex', 'claude'):
            config.worker.override.models[provider] = {'model': provider + '-global', 'effort': 'high'}
            for tier in config.tiers.values():
                tier[provider] = {'model': provider + '-tier', 'effort': 'low'}
            for tier in config.review.tiers.values():
                tier[provider] = {'model': provider + '-review', 'effort': 'medium'}
        return config

    def test_tier_priority_changes_only_worker_candidates(self):
        """Scope selects worker models while other roles keep their routes."""
        config = self.config()
        for role, tier in (('scout', 0), ('reviewer', 2)):
            before = select(config, role, tier, 'codex', {})
            config.worker.override.scope = "all"
            self.assertEqual(select(config, role, tier, 'codex', {}), before)
        for provider in ('codex', 'claude'):
            for scope, tier, expected in (("off", 2, 'tier'),
                    ("untiered", 2, 'tier'), ("all", 2, 'global'),
                    ("untiered", None, 'global'), ("all", None, 'global')):
                config.worker.override.scope = scope
                result = select(config, 'worker', tier, provider, {})
                self.assertEqual((result['model'], result['tier']), (provider + '-' + expected, tier))
                self.assertEqual(result['effort'], 'high' if expected == 'global' else 'low')

    def test_current_provider_never_escapes_empty_or_unavailable_candidates(self):
        """Current routing excludes the other provider even when fallback is enabled."""
        for provider in ('codex', 'claude'):
            other = 'claude' if provider == 'codex' else 'codex'
            for failure in ('empty', 'unavailable', 'zero-weight', 'exhausted'):
                config = self.config()
                available, usage = [provider, other], {}
                if failure == 'empty':
                    config.worker.override.models[provider]['model'] = ''
                elif failure == 'unavailable':
                    available = [other]
                elif failure == 'zero-weight':
                    config.weights[provider] = 0
                else:
                    usage = {provider: {'windows': [{'remaining': 0, 'reset_at': 2000}]}}
                with self.subTest(provider=provider, failure=failure), self.assertRaisesRegex(
                        AgentBoxError, 'workflow.worker.override.models.*no automatic session/tier fallback'):
                    select(config, 'worker', None, provider, {}, usage, now=1000, available=available)

    def test_weighted_fallback_uses_only_enabled_available_global_models(self):
        """Fallback toggles and exclusions retain the same policy with a new model source."""
        config = self.config()
        config.routing = 'weighted'
        config.weights = {'codex': 3, 'claude': 1}
        state = {}
        routes = [select(config, 'worker', None, 'claude', state) for _ in range(4)]
        self.assertEqual([r['provider'] for r in routes].count('codex'), 3)
        self.assertTrue(all(r['fallbacks'][0]['model'].endswith('-global') for r in routes))
        for fallback in (False, True):
            config.fallback = fallback
            self.assertEqual(bool(select(config, 'worker', None, 'codex', {})['fallbacks']), fallback)
        config.worker.override.models['codex']['model'] = ''
        result = select(config, 'worker', None, 'codex', {})
        self.assertEqual(result['provider'], 'claude')
        self.assertEqual(result['fallbacks'], [])
        with self.assertRaisesRegex(AgentBoxError, 'workflow.worker.override.models'):
            select(config, 'worker', None, 'codex', {}, available=['codex'])
        config.worker.override.models['claude']['model'] = ''
        with self.assertRaisesRegex(AgentBoxError, 'workflow.worker.override.models'):
            select(config, 'worker', None, 'codex', {})

    def test_usage_scores_and_exhaustion_follow_global_model_windows(self):
        """Limits for the replaced tier model cannot exclude the chosen global model."""
        config = self.config()
        config.routing = 'usage-aware'
        config.reserve_percent = 5
        usage = {
            'codex': {'fresh': True, 'windows': [
                {'models': ['codex-tier'], 'seconds': 18000, 'remaining': 0, 'reset_at': 2000},
                {'models': ['codex-global'], 'seconds': 604800, 'remaining': 80, 'reset_at': 87400}]},
            'claude': {'fresh': True, 'windows': [
                {'seconds': 604800, 'remaining': 20, 'reset_at': 87400}]}}
        result = select(config, 'worker', None, 'claude', {}, usage, now=1000)
        self.assertEqual(result['provider'], 'codex')
        self.assertIn('Weekly budget score', result['reason'])
        usage['codex']['windows'][1]['remaining'] = 0
        result = select(config, 'worker', None, 'codex', {}, usage, now=1000)
        self.assertEqual(result['provider'], 'claude')
        self.assertEqual(result['fallbacks'], [])
        usage['codex']['windows'][1]['remaining'] = 3
        usage['claude']['windows'][0]['remaining'] = 3
        with self.assertRaisesRegex(AgentBoxError, 'configured reserve'):
            select(config, 'worker', None, 'codex', {}, usage, now=1000)
        usage['claude']['fresh'] = False
        self.assertIn('fresh comparable usage unavailable',
                      select(config, 'worker', None, 'codex', {}, usage, now=1000)['reason'])
