"""Speed requests stay independent and never silently select another model."""
import argparse
import json
import os
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from agent_box import agent_profiles, config_menu, provider_runner, routing, runtime, workflow
from agent_box.config import Config, load_config, save_config
from agent_box.errors import AgentBoxError


class FastModeTests(unittest.TestCase):
    def test_disabled_fast_preserves_native_profiles_and_reports_for_all_roles(self):
        """An unused speed option must not force existing native delegates into CLI."""
        for provider, model in [('codex', 'gpt-6-astra'), ('claude', 'claude-opus-5')]:
            for role, tier in [('worker', 1), ('scout', 0), ('reviewer', 1)]:
                with self.subTest(provider=provider, role=role), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    config = Config()
                    config.workflow.enabled = True
                    matrix = config.workflow.review.tiers if role == 'reviewer' else config.workflow.tiers
                    spec = matrix[str(tier)][provider]
                    spec.update(model=model, effort='high')
                    agent_profiles.sync(config, root / 'codex', root / 'claude')
                    content = agent_profiles.body(provider, role, tier, spec)
                    self.assertNotIn('service_tier', content)
                    self.assertNotIn('fast_mode', content)
                    with patch('agent_box.paths.CODEX_HOME', root / 'codex'), \
                         patch('agent_box.paths.CLAUDE_HOME', root / 'claude'), patch.dict(os.environ, {}, clear=True), \
                         patch.object(workflow, 'WORKFLOW_STATE', root / 'state'), \
                         patch.object(workflow.shutil, 'which', return_value='provider-cli'):
                        self.assertTrue(agent_profiles.available(provider, role, tier, spec))
                        config.workflow.review.routing = 'same-provider'
                        routed = workflow.route(config, role, tier, provider, provider)
                        self.assertTrue(routed['native_profile_ready'])
                        self.assertNotIn('fast', routed['native_explicit_settings'])
                        self.assertIsNone(routed['native_limitation'])
                    report = root / 'report.json'
                    payload = ({'verdict': 'PASS', 'summary': 'Reviewed', 'findings': []} if role == 'reviewer' else
                               {'status': 'completed', 'summary': 'Done', 'evidence': []})
                    report.write_text(json.dumps(payload))
                    args = argparse.Namespace(provider=provider, model=model, effort='high', fast=None,
                        role=role, fallback_reason=None, fresh_context=True, file=str(report))
                    selected = dict(provider=provider, model=model, effort='high', fast=False, tier=tier, fallbacks=[])
                    self.assertEqual(workflow.native_output(args, selected, tier)['report'], payload)

    def test_fast_roundtrips_without_leaking_to_other_candidates(self):
        """Each role, provider and tier keeps its own opt-in across save and rendering."""
        config = Config()
        config.workflow.enabled = True
        matrices = [config.workflow.tiers['0'], config.workflow.review.tiers['2'],
                    config.workflow.worker.override.models, config.workflow.review.override.models]
        for matrix in matrices:
            matrix['codex'].update(model='gpt-6-astra', fast=True)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            save_config(config, path)
            loaded = load_config(path)
            self.assertEqual(loaded.to_dict(), config.to_dict())
            runtime.sync_tracker_runtime(loaded, Path(directory) / 'runtime')
            rendered = json.loads((Path(directory) / 'runtime/config.json').read_text())
            self.assertEqual(Config.from_dict(rendered).workflow, loaded.workflow)
        from test_workflow_config import shown
        rows = shown(config)
        self.assertIn('fast on', rows['workflow.tiers.0.codex'])
        self.assertIn('fast off', rows['workflow.tiers.1.codex'])
        self.assertFalse(loaded.workflow.tiers['1']['codex']['fast'])
        self.assertFalse(loaded.workflow.tiers['0']['claude']['fast'])
        self.assertFalse(loaded.workflow.review.tiers['1']['codex']['fast'])
        for path in [('workflow', 'tiers', '0', 'codex', 'fast'),
                     ('workflow', 'review', 'tiers', '2', 'claude', 'fast'),
                     ('workflow', 'worker', 'override', 'models', 'codex', 'fast')]:
            self.assertIn('higher provider usage/cost', config_menu.help_for(path))
        self.assertEqual(config.workflow.validate(), [])

    def test_unconfigured_session_speed_is_preserved_and_unsupported_claude_fast_is_refused(self):
        """An opt-in speed request must not add requirements to existing session routes."""
        route = dict(provider='codex', model='gpt-6-astra', effort='high', mode='current-session')
        provider_runner.check_settings(route)
        route['fast'] = True
        provider_runner.check_settings(route)
        route.update(provider='claude', model='claude-opus-5')
        with patch.dict(os.environ, {'CLAUDE_CODE_USE_BEDROCK': '1'}), \
             self.assertRaisesRegex(provider_runner.ProviderFailure, 'BEDROCK'):
            provider_runner.check_settings(route)

    def test_invalid_fast_and_unknown_models_are_rejected_before_launch(self):
        """String booleans and mutable aliases cannot become working fast by accident."""
        for provider, model in [('codex', 'gpt-5.3-codex-spark'), ('claude', 'sonnet'),
                                ('claude', 'opus'), ('claude', 'claude-opus-4-7')]:
            spec = dict(provider=provider, model=model, effort='high', fast=True)
            with self.subTest(provider=provider, model=model), self.assertRaisesRegex(provider_runner.ProviderFailure, 'supported model'):
                provider_runner.build_command(spec, 'worker', Path('/tmp'), Path('/missing'), Path('/tmp/result'))
        for value in ('true', 1, None, []):
            config = Config.from_dict({'workflow': {'tiers': {'1': {'codex': {'fast': value}}}}})
            self.assertTrue(any('fast must be a boolean' in e for e in config.workflow.validate()))
        config = Config()
        config.workflow.tiers['1']['codex']['fast'] = True
        self.assertEqual(config.workflow.validate(), [])  # An empty model disables the candidate.

    def test_cli_roles_only_override_speed_when_requested_without_changing_effort(self):
        """Off preserves each launch path; on requests fast without replacing effort."""
        for provider, model in [('codex', 'gpt-6-astra'), ('claude', 'claude-opus-5')]:
            for role in ('worker', 'scout', 'reviewer'):
                for fast in (False, True):
                    with self.subTest(provider=provider, role=role, fast=fast):
                        cmd = provider_runner.build_command(dict(provider=provider, model=model, effort='high', fast=fast),
                            role, Path('/tmp'), Path('/missing'), Path('/tmp/result'))
                        self.assertEqual(cmd[cmd.index('--model') + 1], model)
                        if provider == 'codex':
                            self.assertEqual('service_tier="fast"' in cmd, fast)
                            self.assertEqual('features.fast_mode=true' in cmd, fast)
                            if not fast:
                                self.assertFalse(any('service_tier=' in arg or 'features.fast_mode=' in arg for arg in cmd))
                            self.assertIn('model_reasoning_effort="high"', cmd)
                        else:
                            if fast:
                                self.assertEqual(json.loads(cmd[cmd.index('--settings') + 1]), {'fastMode': True})
                            else:
                                self.assertNotIn('--settings', cmd)
                            self.assertEqual(cmd[cmd.index('--effort') + 1], 'high')

    def test_routing_and_failed_primary_keep_fallback_speed(self):
        """A fallback uses its own candidate rather than inheriting primary fast."""
        config = Config().workflow
        config.routing = 'weighted'
        config.weights = {'codex': 2, 'claude': 1}
        config.tiers['1']['codex'].update(model='gpt-6-astra', fast=True)
        config.tiers['1']['claude'].update(model='claude-opus-5', fast=False)
        selected = routing.select(config, 'worker', 1, 'codex', {})
        self.assertTrue(selected['fast'])
        self.assertFalse(selected['fallbacks'][0]['fast'])
        with patch.object(workflow, 'run', side_effect=[provider_runner.ProviderFailure('Unavailable', 'unavailable'),
                                                       {'status': 'completed', 'summary': 'Done', 'evidence': []}]) as run, \
             patch.object(workflow.gw, '_worktree_clean', return_value=True):
            result = workflow.execute(config, selected, 'worker', Path('/tmp'), 'brief')
        self.assertFalse(run.call_args_list[1].args[0]['fast'])
        self.assertFalse(result['route']['fast'])

    def test_mixed_native_reports_require_speed_only_for_the_candidate_that_ran(self):
        """A primary's speed policy must not constrain its fallback, or vice versa."""
        for role in ('worker', 'scout', 'reviewer'):
            for primary_fast in (False, True):
                with self.subTest(role=role, primary_fast=primary_fast), tempfile.TemporaryDirectory() as directory:
                    report = Path(directory) / 'report.json'
                    payload = ({'verdict': 'PASS', 'summary': 'Reviewed', 'findings': []} if role == 'reviewer' else
                               {'status': 'completed', 'summary': 'Done', 'evidence': []})
                    report.write_text(json.dumps(payload))
                    fallback = dict(provider='claude', model='claude-opus-5', effort='high', tier=1, fast=not primary_fast)
                    selected = dict(provider='codex', model='gpt-6-astra', effort='high', tier=1,
                                    fast=primary_fast, fallbacks=[fallback])
                    for index, candidate in enumerate((selected, fallback)):
                        args = argparse.Namespace(provider=candidate['provider'], model=candidate['model'], effort='high',
                            fast=None, role=role, fresh_context=True, file=str(report),
                            fallback_reason='Primary unavailable' if index else None)
                        if candidate['fast']:
                            with self.assertRaisesRegex(AgentBoxError, '--fast on'):
                                workflow.native_output(args, selected, 1)
                            args.fast = 'on'
                        if role == 'reviewer' and index:
                            with self.assertRaisesRegex(AgentBoxError, 'Native reviewer fallback'):
                                workflow.native_output(args, selected, 1)
                            continue
                        result = workflow.native_output(args, selected, 1)
                        self.assertEqual(result['route']['fast'], candidate['fast'])

    def test_native_profiles_request_codex_fast_and_preserve_claude_with_inheritance_notice(self):
        """Claude profiles remain usable when fast is verified through session inheritance."""
        config = Config()
        config.workflow.enabled = True
        config.workflow.tiers['1']['claude'].update(model='claude-opus-5')
        with tempfile.TemporaryDirectory() as directory:
            codex, claude = Path(directory) / 'codex', Path(directory) / 'claude'
            agent_profiles.sync(config, codex, claude)
            profile = claude / 'agents/ab-worker-1.md'
            self.assertTrue(profile.exists())
            config.workflow.tiers['1']['claude']['fast'] = True
            self.assertTrue(any('verify inherited fast' in m for m in agent_profiles.sync(config, codex, claude)))
            self.assertTrue(profile.exists())
        for fast in (False, True):
            data = tomllib.loads(agent_profiles.body('codex', 'worker', 1,
                dict(model='gpt-6-astra', effort='high', fast=fast)))
            if fast:
                self.assertEqual(data['service_tier'], 'fast')
                self.assertTrue(data['features']['fast_mode'])
            else:
                self.assertNotIn('service_tier', data)
                self.assertNotIn('features', data)

    def test_apply_invalid_fast_removes_only_intact_owned_profiles(self):
        """Hand-edited config must not install invalid fast before warnings are printed."""
        for invalid in ({'model': 'unsupported-model', 'fast': True},
                        {'model': 'gpt-6-astra', 'fast': 'true'}):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as directory:
                config = Config()
                config.workflow.enabled = True
                spec = config.workflow.tiers['1']['codex']
                spec.update(model='gpt-6-astra', fast=True)
                codex, claude = Path(directory) / 'codex', Path(directory) / 'claude'
                agent_profiles.sync(config, codex, claude)
                profile = codex / 'agents/ab-worker-1.toml'
                edited = codex / 'agents/ab-scout-1.toml'
                edited.write_text(edited.read_text() + '# user edit\n')
                spec.update(invalid)
                messages = agent_profiles.sync(config, codex, claude)
                self.assertFalse(profile.exists())
                self.assertFalse(profile.with_name(profile.name + '.agent-box').exists())
                self.assertTrue(edited.read_text().endswith('# user edit\n'))
                self.assertTrue(any('ab-worker-1.toml' in m for m in messages))

    def test_native_fast_requests_require_evidence_for_both_providers_and_all_roles(self):
        """Verified inherited fast is valid; model/effort alone cannot attest an opt-in."""
        for provider, model in [('codex', 'gpt-6-astra'), ('claude', 'claude-opus-5')]:
            for role in ('worker', 'scout', 'reviewer'):
                with self.subTest(provider=provider, role=role), tempfile.TemporaryDirectory() as directory:
                    selected = dict(provider=provider, model=model, effort='high', fast=True, tier=1, fallbacks=[])
                    args = argparse.Namespace(provider=provider, model=model, effort='high', fast=None,
                                              role=role, fallback_reason=None, fresh_context=True)
                    for speed in (None, 'off'):
                        args.fast = speed
                        with self.assertRaisesRegex(AgentBoxError, '--fast on'):
                            workflow.native_output(args, selected, 1)
                    report = Path(directory) / 'report.json'
                    payload = ({'verdict': 'PASS', 'summary': 'Reviewed', 'findings': []} if role == 'reviewer' else
                               {'status': 'completed', 'summary': 'Done', 'evidence': []})
                    report.write_text(json.dumps(payload))
                    args.file, args.fast = str(report), 'on'
                    self.assertTrue(workflow.native_output(args, selected, 1)['route']['fast'])
