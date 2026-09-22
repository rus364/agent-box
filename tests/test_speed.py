"""Speed requests stay independent and never silently select another model."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_box import agent_profiles, config_menu, provider_runner, routing, runtime, workflow
from agent_box.config import Config, load_config, model_entry, save_config


class SpeedTests(unittest.TestCase):
    def test_default_inherit_preserves_native_profiles(self):
        """An unused speed option must not add speed settings to any role's profile."""
        for provider, model in [('codex', 'gpt-6-astra'), ('claude', 'claude-opus-5')]:
            for role, tier in [('worker', 1), ('scout', None), ('reviewer', 1)]:
                with self.subTest(provider=provider, role=role), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    config = Config()
                    config.workflow.enabled = True
                    spec = model_entry(dict(provider=provider, model=model, effort='high'))
                    if tier is None:
                        config.workflow.role(role).models = [spec]
                    else:
                        config.workflow.role(role).tiers[str(tier)] = [spec]
                    agent_profiles.sync(config, root / 'codex', root / 'claude')
                    content = agent_profiles.body(provider, role, tier, spec)
                    self.assertNotIn('service_tier', content)
                    self.assertNotIn('fast_mode', content)

    def test_speed_roundtrips_without_leaking_to_other_candidates(self):
        """Each candidate keeps its own opt-in across save and rendering."""
        config = Config()
        config.workflow.enabled = True
        fast = model_entry(dict(provider='codex', model='gpt-6-astra', speed='fast'))
        slow = model_entry(dict(provider='codex', model='gpt-6-astra'))
        standard = model_entry(dict(provider='codex', model='gpt-6-astra', speed='standard'))
        config.workflow.scout.models = [fast, slow, standard]
        config.workflow.review.tiers['2'] = [dict(standard)]
        config.workflow.worker.models = [dict(fast)]
        config.workflow.review.models = [dict(slow), dict(fast), dict(standard)]
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
        self.assertEqual('codex:gpt-6-astra:medium speed=fast, codex:gpt-6-astra:medium speed=inherit, codex:gpt-6-astra:medium speed=standard', rows['workflow.scout.models'])
        self.assertEqual([spec['speed'] for spec in loaded.workflow.review.models], ['inherit', 'fast', 'standard'])
        self.assertEqual(loaded.workflow.review.tiers['1'], [])
        self.assertIn('higher provider usage/cost', config_menu.help_for(('workflow', 'speed')))
        self.assertEqual(config.workflow.validate(), [])

    def test_speed_is_only_checked_when_requested_and_unsupported_claude_fast_is_refused(self):
        """An opt-in speed request must not add requirements to a candidate that does not ask for it."""
        route = dict(provider='codex', model='gpt-6-astra', effort='high')
        provider_runner.check_settings(route)
        route['speed'] = 'fast'
        provider_runner.check_settings(route)
        route.update(provider='claude', model='claude-opus-5')
        with patch.dict(os.environ, {'CLAUDE_CODE_USE_BEDROCK': '1'}), \
             self.assertRaisesRegex(provider_runner.ProviderFailure, 'BEDROCK'):
            provider_runner.check_settings(route)

    def test_invalid_speed_and_unknown_models_are_rejected_before_launch(self):
        """Malformed speed and unverified fast models must fail before launch."""
        for provider, model in [('codex', 'gpt-5.3-codex-spark'), ('claude', 'sonnet'),
                                ('claude', 'opus'), ('claude', 'claude-opus-4-7')]:
            spec = dict(provider=provider, model=model, effort='high', speed='fast')
            with self.subTest(provider=provider, model=model), self.assertRaisesRegex(provider_runner.ProviderFailure, 'supported model'):
                provider_runner.build_command(spec, 'worker', Path('/tmp'), Path('/tmp/result'))
        for value in ('true', True, False, 1, None, [], {}, ''):
            config = Config.from_dict({'workflow': {'worker': {'tiers': {'1': [
                {'provider': 'codex', 'model': 'gpt-6-astra', 'speed': value}]}}}})
            self.assertTrue(any('speed must be inherit, standard, or fast' in e for e in config.workflow.validate()))

    def test_cli_roles_only_override_speed_when_requested_without_changing_effort(self):
        """Explicit speed reaches every CLI role without replacing model or effort."""
        for provider, model in [('codex', 'gpt-6-astra'), ('claude', 'claude-opus-5')]:
            for role in ('worker', 'scout', 'reviewer'):
                for speed in ('inherit', 'standard', 'fast'):
                    with self.subTest(provider=provider, role=role, speed=speed):
                        cmd = provider_runner.build_command(dict(provider=provider, model=model, effort='high', speed=speed),
                            role, Path('/tmp'), Path('/tmp/result'))
                        self.assertEqual(cmd[cmd.index('--model') + 1], model)
                        if provider == 'codex':
                            self.assertEqual('service_tier="fast"' in cmd, speed == 'fast')
                            self.assertEqual('features.fast_mode=true' in cmd, speed == 'fast')
                            self.assertEqual('service_tier="default"' in cmd, speed == 'standard')
                            self.assertEqual('features.fast_mode=false' in cmd, speed == 'standard')
                            if speed == 'inherit':
                                self.assertFalse(any('service_tier=' in arg or 'features.fast_mode=' in arg for arg in cmd))
                            self.assertIn('model_reasoning_effort="high"', cmd)
                        else:
                            if speed != 'inherit':
                                self.assertEqual(json.loads(cmd[cmd.index('--settings') + 1]), {'fastMode': speed == 'fast'})
                            else:
                                self.assertNotIn('--settings', cmd)
                            self.assertEqual(cmd[cmd.index('--effort') + 1], 'high')

    def test_routing_and_failed_primary_keep_fallback_speed(self):
        """A fallback uses its own candidate rather than inheriting primary fast."""
        config = Config().workflow
        config.worker.tiers['1'] = [model_entry(dict(provider='codex', model='gpt-6-astra', speed='fast')),
                                    model_entry(dict(provider='claude', model='claude-opus-5', speed='standard'))]
        selected = routing.select(config, 'worker', 1)
        self.assertEqual(selected['speed'], 'fast')
        self.assertEqual(selected['fallbacks'][0]['speed'], 'standard')
        with patch.object(workflow, 'run', side_effect=[provider_runner.ProviderFailure('Unavailable', 'unavailable'),
                                                       '## Done']) as run, \
             patch.object(workflow.gw, '_worktree_clean', return_value=True):
            result = workflow.execute(config, selected, 'worker', Path('/tmp'), 'brief')
        self.assertEqual(run.call_args_list[1].args[0]['speed'], 'standard')
        self.assertEqual(result['route']['speed'], 'standard')

    def test_standard_is_available_without_fast_support_but_external_speed_is_rejected(self):
        """Ordinary speed needs no Fast entitlement; arbitrary endpoints have no shared tier contract."""
        for provider, model in (('codex', 'gpt-5.3-codex-spark'), ('claude', 'sonnet')):
            provider_runner.check_settings(dict(provider=provider, model=model, effort='high', speed='standard'))
            for speed in ('inherit', 'standard', 'fast'):
                spec = dict(provider=provider, model=model, effort='high', speed=speed, api_provider='external')
                if speed == 'inherit':
                    provider_runner.check_settings(spec)
                else:
                    with self.assertRaisesRegex(provider_runner.ProviderFailure, 'external API candidates; use inherit'):
                        provider_runner.check_settings(spec)

    def test_explicit_speed_removes_native_profiles_that_cannot_enforce_it(self):
        """A native profile must not silently inherit the opposite speed from its parent."""
        for provider, model, suffix in (('codex', 'gpt-6-astra', '.toml'), ('claude', 'claude-opus-5', '.md')):
            for role in ('worker', 'scout', 'reviewer'):
                for speed in ('standard', 'fast'):
                    with self.subTest(provider=provider, role=role, speed=speed), tempfile.TemporaryDirectory() as directory:
                        config = Config()
                        config.workflow.enabled = True
                        spec = model_entry(dict(provider=provider, model=model))
                        config.workflow.role(role).models = [spec]
                        codex, claude = Path(directory) / 'codex', Path(directory) / 'claude'
                        agent_profiles.sync(config, codex, claude)
                        profile = Path(directory) / provider / ('agents/ab-' + role + suffix)
                        self.assertTrue(profile.exists())
                        spec['speed'] = speed
                        messages = agent_profiles.sync(config, codex, claude)
                        self.assertTrue(any('managed CLI to request ' + speed in m for m in messages))
                        self.assertFalse(profile.exists())
                        spec['speed'] = 'inherit'
                        agent_profiles.sync(config, codex, claude)
                        self.assertTrue(profile.exists())

    def test_apply_invalid_fast_removes_only_intact_owned_profiles(self):
        """Hand-edited config must not install invalid fast before warnings are printed."""
        for invalid in ({'model': 'unsupported-model', 'speed': 'fast'},
                        {'model': 'gpt-6-astra', 'speed': 'true'}):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as directory:
                config = Config()
                config.workflow.enabled = True
                spec = model_entry(dict(provider='codex', model='gpt-6-astra'))
                config.workflow.worker.tiers['1'] = [spec]
                config.workflow.worker.tiers['2'] = [dict(spec)]
                codex, claude = Path(directory) / 'codex', Path(directory) / 'claude'
                agent_profiles.sync(config, codex, claude)
                profile = codex / 'agents/ab-worker-1.toml'
                edited = codex / 'agents/ab-worker-2.toml'
                edited.write_text(edited.read_text() + '# user edit\n')
                spec.update(invalid)
                messages = agent_profiles.sync(config, codex, claude)
                self.assertFalse(profile.exists())
                self.assertFalse(profile.with_name(profile.name + '.agent-box').exists())
                self.assertTrue(edited.read_text().endswith('# user edit\n'))
                self.assertTrue(any('ab-worker-1.toml' in m for m in messages))


if __name__ == "__main__":
    unittest.main()
