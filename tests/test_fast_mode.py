"""Speed requests stay independent and never silently select another model."""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from agent_box import agent_profiles, cli, config_menu, provider_runner, routing, runtime, workflow
from agent_box.config import Config, load_config, save_config
from agent_box.errors import AgentBoxError


class FastModeTests(unittest.TestCase):
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
        output = io.StringIO()
        with patch.object(cli, 'effective_config', return_value=config), \
             patch.object(cli, '_statusline_note', return_value=''), \
             patch.object(cli, '_image_note', return_value=''), \
             patch.object(cli, '_browser_note', return_value=''), contextlib.redirect_stdout(output):
            cli.config_show()
        self.assertIn("'fast': True", output.getvalue())
        self.assertIn('not delivered service', output.getvalue())
        self.assertFalse(loaded.workflow.tiers['1']['codex']['fast'])
        self.assertFalse(loaded.workflow.tiers['0']['claude']['fast'])
        self.assertFalse(loaded.workflow.review.tiers['1']['codex']['fast'])
        for path in [('workflow', 'tiers', '0', 'codex', 'fast'),
                     ('workflow', 'review', 'tiers', '2', 'claude', 'fast'),
                     ('workflow', 'worker', 'override', 'models', 'codex', 'fast')]:
            self.assertIn('higher provider usage/cost', config_menu.help_for(path))
        self.assertEqual(config.workflow.validate(), [])

    def test_managed_session_speed_and_claude_backend_are_checked_before_launch(self):
        """Unknown inherited speed and a disabled provider cannot silently run standard."""
        route = dict(provider='codex', model='gpt-6-astra', effort='high', mode='current-session')
        with self.assertRaisesRegex(provider_runner.ProviderFailure, '--session-fast'):
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

    def test_cli_roles_explicitly_request_on_and_off_without_changing_effort(self):
        """Off must override ambient provider defaults, including on a fallback run."""
        for provider, model in [('codex', 'gpt-6-astra'), ('claude', 'claude-opus-5')]:
            for role in ('worker', 'scout', 'reviewer'):
                for fast in (False, True):
                    with self.subTest(provider=provider, role=role, fast=fast):
                        cmd = provider_runner.build_command(dict(provider=provider, model=model, effort='high', fast=fast),
                            role, Path('/tmp'), Path('/missing'), Path('/tmp/result'))
                        self.assertEqual(cmd[cmd.index('--model') + 1], model)
                        if provider == 'codex':
                            self.assertIn('service_tier=' + json.dumps('fast' if fast else 'default'), cmd)
                            self.assertIn('features.fast_mode=' + str(fast).lower(), cmd)
                            self.assertIn('model_reasoning_effort="high"', cmd)
                        else:
                            self.assertEqual(json.loads(cmd[cmd.index('--settings') + 1]), {'fastMode': fast})
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

    def test_native_profiles_pin_codex_speed_and_remove_unsupported_claude_fast(self):
        """A stale owned Claude profile cannot masquerade as a new fast candidate."""
        config = Config()
        config.workflow.enabled = True
        config.workflow.tiers['1']['claude'].update(model='claude-opus-5')
        with tempfile.TemporaryDirectory() as directory:
            codex, claude = Path(directory) / 'codex', Path(directory) / 'claude'
            agent_profiles.sync(config, codex, claude)
            profile = claude / 'agents/ab-worker-1.md'
            self.assertTrue(profile.exists())
            config.workflow.tiers['1']['claude']['fast'] = True
            self.assertTrue(any('cannot independently pin fast' in m for m in agent_profiles.sync(config, codex, claude)))
            self.assertFalse(profile.exists())
        for fast in (False, True):
            data = tomllib.loads(agent_profiles.body('codex', 'worker', 1,
                dict(model='gpt-6-astra', effort='high', fast=fast)))
            self.assertEqual(data['service_tier'], 'fast' if fast else 'default')
            self.assertEqual(data['features']['fast_mode'], fast)

    def test_native_report_requires_matching_speed_and_rejects_claude_fast(self):
        """Model/effort alone cannot attest the candidate's speed setting."""
        selected = dict(provider='codex', model='gpt-6-astra', effort='high', fast=True,
                        tier=1, fallbacks=[])
        args = argparse.Namespace(provider='codex', model='gpt-6-astra', effort='high', fast=None,
                                  role='worker', fallback_reason=None)
        with self.assertRaisesRegex(AgentBoxError, '--fast'):
            workflow.native_output(args, selected, 1)
        args.fast = 'off'
        with self.assertRaisesRegex(AgentBoxError, 'outside the configured route'):
            workflow.native_output(args, selected, 1)
        selected.update(provider='claude', model='claude-opus-5')
        args.provider, args.model, args.fast = 'claude', 'claude-opus-5', 'on'
        with self.assertRaisesRegex(AgentBoxError, 'managed CLI'):
            workflow.native_output(args, selected, 1)
