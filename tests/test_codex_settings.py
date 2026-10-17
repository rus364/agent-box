"""Global notify belongs to apply, while the rest of Codex TOML belongs to the user."""
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from agent_box import codex_settings
from agent_box.errors import AgentBoxError


class CodexNotifySettingsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'config.toml'

    def test_install_roundtrip_preserves_toml_bytes_and_permissions(self):
        """Root placement must not turn notify into a key of the last table."""
        original = b'# personal\r\nmodel = "example"\r\n[features]\r\nhooks = false'
        self.path.write_bytes(original)
        self.path.chmod(0o640)
        codex_settings.reconcile_notify(self.path, True)
        data = tomllib.loads(self.path.read_text())
        self.assertEqual(data['notify'], ['agent-box', 'notify', 'codex'])
        self.assertEqual(data['features'], {'hooks': False})
        self.assertTrue(self.path.read_bytes().endswith(original))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o640)
        with mock.patch.object(codex_settings, 'atomic_write_text') as write:
            codex_settings.reconcile_notify(self.path, True)
            write.assert_not_called()
        codex_settings.reconcile_notify(self.path, False)
        self.assertEqual(self.path.read_bytes(), original)

    def test_disabled_missing_config_creates_nothing_and_enabled_creates_root_setting(self):
        """An opt-out must not create a new user configuration."""
        codex_settings.reconcile_notify(self.path, False)
        self.assertFalse(self.path.exists())
        codex_settings.reconcile_notify(self.path, True)
        self.assertEqual(tomllib.loads(self.path.read_text())['notify'], codex_settings.NOTIFY_COMMAND)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_user_notify_is_preserved_with_a_conflict_diagnostic(self):
        """Even an explicit empty command is a user choice, not a vacant slot."""
        for source in ('notify = ["my-notifier"]\n', '"notify" = []\n',
                       'notify = [\n "another", # keep\n "argument"\n]\n'):
            for enabled in (True, False):
                with self.subTest(source=source, enabled=enabled):
                    self.path.write_text(source)
                    messages = codex_settings.reconcile_notify(self.path, enabled)
                    self.assertIn('preserved', messages[0])
                    self.assertIn(str(self.path), messages[0])
                    self.assertEqual(self.path.read_text(), source)

    def test_manual_matching_command_is_never_adopted_or_removed(self):
        """Matching command text does not transfer ownership to agent-box."""
        source = 'notify = ["agent-box", "notify", "codex"]\n'
        self.path.write_text(source)
        self.assertEqual(codex_settings.reconcile_notify(self.path, True), [])
        self.assertIn('preserved', codex_settings.reconcile_notify(self.path, False)[0])
        self.assertEqual(self.path.read_text(), source)

    def test_edited_moved_or_quoted_blocks_are_not_removed(self):
        """Marker-looking text is insufficient proof of an owned root setting."""
        block = codex_settings.NOTIFY_BLOCK
        for source in (block.replace('codex"]', 'custom"]'), '# moved\n' + block,
                       'instructions = """\n' + block + '"""\n', block.replace('notify =', '# notify =')):
            for enabled in (True, False):
                with self.subTest(source=source, enabled=enabled):
                    self.path.write_text(source)
                    codex_settings.reconcile_notify(self.path, enabled)
                    self.assertEqual(self.path.read_text(), source)

    def test_invalid_or_symlinked_config_is_preserved(self):
        """Never repair broken TOML or replace a user's linked configuration."""
        for source in (b'notify = [', b'\xff'):
            self.path.write_bytes(source)
            for enabled in (True, False):
                with self.assertRaisesRegex(AgentBoxError, 'preserved'):
                    codex_settings.reconcile_notify(self.path, enabled)
                self.assertEqual(self.path.read_bytes(), source)
        self.path.unlink()
        target = self.path.with_name('target.toml')
        target.write_text('model = "example"\n')
        self.path.symlink_to(target)
        self.assertIn('symlink', codex_settings.reconcile_notify(self.path, True)[0])
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(target.read_text(), 'model = "example"\n')
