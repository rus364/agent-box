"""Opting out must stop collection, not merely hide its results."""
import contextlib
import io
import itertools
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_box import cli, runtime, statusline, usage
from agent_box.config import Config, load_config, save_config
from agent_box.errors import AgentBoxError


class UsageBlockTests(unittest.TestCase):
    def test_defaults_collect_nothing_until_explicitly_enabled(self):
        """A fresh or sparse config must not initiate usage requests or commands."""
        for config in (Config(), Config.from_dict({'agents': {'claude': {'statusline': {'command': 'echo balance'}}}})):
            with self.subTest(config=config), mock.patch('agent_box.config.effective_config', return_value=config), \
                    mock.patch.object(usage, 'usage_snapshot') as snapshot, \
                    mock.patch.object(usage, 'custom_usage') as custom:
                self.assertEqual(usage.usage_segments(), [])
                snapshot.assert_not_called()
                custom.assert_not_called()

    def test_each_switch_gates_collection_in_both_displays(self):
        """All combinations keep enabled blocks and never touch disabled sources."""
        for flags in itertools.product((False, True), repeat=3):
            config = Config()
            blocks = config.agents.claude.statusline
            blocks.claude_usage, blocks.codex_usage, blocks.custom_usage = flags
            for display in ('cli', 'statusline'):
                with self.subTest(flags=flags, display=display), contextlib.ExitStack() as stack:
                    stack.enter_context(mock.patch('agent_box.config.effective_config', return_value=config))
                    stack.enter_context(mock.patch.dict(os.environ, {'NO_COLOR': '1'}))
                    collectors = [stack.enter_context(mock.patch.object(usage, name, return_value=text))
                                  for name, text in (('claude_usage', 'CLAUDE'), ('codex_usage', 'CODEX'),
                                                     ('custom_usage', 'CUSTOM'))]
                    payload = stack.enter_context(mock.patch.object(usage, 'claude_usage_from_payload', return_value=''))
                    stack.enter_context(mock.patch.object(statusline, 'format_prompt', return_value='PROMPT'))
                    if display == 'cli':
                        out = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                        cli.usage_command(['all'])
                        line = out.getvalue().strip()
                    else:
                        line = statusline.render('{"rate_limits": {}}')
                    for enabled, collector, text in zip(flags, collectors, ('CLAUDE', 'CODEX', 'CUSTOM')):
                        self.assertEqual(collector.call_count, int(enabled))
                        self.assertEqual(text in line, enabled)
                    self.assertEqual(payload.call_count, int(flags[0] and display == 'statusline'))
                    self.assertNotIn('—', line)
                    self.assertNotIn('│ │', line)

    def test_structured_and_forced_refresh_skip_disabled_providers(self):
        """Refresh flags must not bypass an opt-out; an explicit provider can."""
        config = Config()
        config.agents.claude.statusline.claude_usage = False
        config.agents.claude.statusline.codex_usage = False
        for args, expected in ((['all', '--json'], []), (['--no-cache'], []),
                               (['claude', '--json', '--no-cache'], ['claude']),
                               (['codex', '--json'], ['codex'])):
            with self.subTest(args=args), mock.patch('agent_box.config.effective_config', return_value=config), \
                    mock.patch.object(usage, 'usage_snapshot', return_value={'fresh': True}) as snapshot, \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                cli.usage_command(args)
                self.assertEqual([c.args[0] for c in snapshot.call_args_list], expected)
                if args == ['all', '--json']:
                    self.assertEqual(json.loads(out.getvalue()), [])
        config.agents.claude.statusline.codex_usage = True
        with mock.patch('agent_box.config.effective_config', return_value=config), \
                mock.patch.object(usage, 'usage_snapshot', return_value={'fresh': True}) as snapshot, \
                contextlib.redirect_stdout(io.StringIO()):
            cli.usage_command(['all', '--json', '--no-cache'])
            snapshot.assert_called_once_with('codex', no_cache=True)

    def test_disabled_switches_survive_saved_and_rendered_configuration(self):
        """The status line inside a box must honor the host's opt-outs."""
        config = Config()
        blocks = config.agents.claude.statusline
        blocks.claude_usage = blocks.codex_usage = blocks.custom_usage = False
        blocks.command = 'echo balance'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            save_config(config, root / 'saved.json')
            self.assertEqual(load_config(root / 'saved.json').agents.claude.statusline, blocks)
            runtime.sync_tracker_runtime(config, root / 'session', policy=True)
            snapshot = root / 'session/config.json'
            self.assertNotIn('enabled', json.loads(snapshot.read_text())['agents']['claude']['statusline'])
            with mock.patch.dict(os.environ, {'AGENT_BOX_RUNTIME_CONFIG': str(snapshot)}, clear=True):
                self.assertEqual(usage.configured_usage_blocks(), blocks)

    def test_invalid_snapshot_does_not_reenable_collection(self):
        """Unreadable configuration must not silently restart opted-out requests."""
        with mock.patch('agent_box.config.effective_config', side_effect=AgentBoxError('invalid snapshot')), \
                mock.patch.object(usage, 'usage_snapshot') as snapshot, \
                mock.patch.object(usage, 'custom_usage') as custom:
            self.assertEqual(usage.usage_segments(), [])
            snapshot.assert_not_called()
            custom.assert_not_called()
