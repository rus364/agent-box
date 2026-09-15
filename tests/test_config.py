"""The unified agent-box configuration.

The load/save round trip is trivial; what these tests actually pin down is the
security posture of the defaults. Every switch that hands host credentials to
a container must be off until the user turns it on in ``agent-box config`` --
in particular the GitHub token, which an earlier agent-box forwarded to every
container purely because ``gh`` happened to be authenticated on the host.
"""
import json
import tempfile
import unittest
from pathlib import Path

from agent_box import config as config_module
from agent_box.config import Config, load_config, save_config


class DefaultsAreSafeTests(unittest.TestCase):
    def test_credential_passthrough_is_off_by_default(self):
        config = Config()
        self.assertFalse(config.github.gh_token)
        self.assertFalse(config.github.gh_config)
        self.assertFalse(config.claude.telegram_channel)
        self.assertFalse(config.notifications.enabled)

    def test_provider_api_keys_are_off_by_default(self):
        """An ANTHROPIC_API_KEY exported in your shell is not a decision to
        hand it to an agent container; the agents authenticate through the
        mounted ~/.claude / ~/.codex directories instead."""
        self.assertFalse(Config().providers.api_keys)

    def test_full_git_config_copy_is_off_by_default(self):
        """A real ~/.gitconfig routinely carries credential.helper entries,
        http.<url>.extraHeader tokens and include.path directives."""
        self.assertFalse(Config().git.full_config)

    def test_non_secret_conveniences_default_on(self):
        config = Config()
        self.assertTrue(config.git.identity)       # only user.name / user.email
        self.assertTrue(config.providers.transport)  # endpoints, proxies, CA bundle

    def test_missing_file_yields_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(Path(tmp) / "missing.json")
            self.assertFalse(config.github.gh_token)

    def test_unreadable_file_yields_defaults_rather_than_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("{ not json")
            self.assertFalse(load_config(path).github.gh_token)

    def test_partial_file_does_not_silently_enable_anything(self):
        """A file written by a future/older agent-box must not turn a
        credential switch on just by omitting it."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"version": 1, "claude": {"telegram_channel": True}}))
            config = load_config(path)
            self.assertTrue(config.claude.telegram_channel)
            self.assertFalse(config.github.gh_token)
            self.assertFalse(config.notifications.enabled)
            self.assertFalse(config.providers.api_keys)
            self.assertFalse(config.git.full_config)


class RoundTripTests(unittest.TestCase):
    def test_default_global_file_is_sparse_and_reconstructs_all_program_defaults(self):
        """Saving defaults must leave later program defaults free to take effect."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            save_config(Config(), path)
            self.assertEqual(json.loads(path.read_text()), {'version': 1})
            self.assertEqual(load_config(path).to_dict(), Config().to_dict())

    def test_sparse_global_roundtrip_keeps_nested_false_empty_lists_and_custom_values(self):
        """An omitted sibling inherits; false and empty lists can still override defaults."""
        config = Config()
        config.workflow.auto_push = False
        config.workflow.review.sensitive_paths = []
        config.workflow.review.override.models['codex']['model'] = 'custom-model'
        config.image.tools['analysis'] = False
        config.env.set['CUSTOM'] = ''
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            save_config(config, path)
            data = json.loads(path.read_text())
            self.assertEqual(data, {
                'version': 1, 'env': {'set': {'CUSTOM': ''}}, 'image': {'tools': {'analysis': False}},
                'workflow': {'auto_push': False, 'review': {'sensitive_paths': [],
                    'override': {'models': {'codex': {'model': 'custom-model'}}}}}})
            self.assertEqual(load_config(path).to_dict(), config.to_dict())
            path.write_text(json.dumps(config.to_dict()))
            save_config(load_config(path), path)
            self.assertEqual(json.loads(path.read_text()), data)

    def test_save_load_roundtrip_and_permissions(self):
        config = Config()
        config.claude.telegram_channel = True
        config.notifications.enabled = True
        config.notifications.telegram_token = "123:abc"
        config.notifications.telegram_chat_id = "555"
        config.github.gh_token = True
        config.git.identity = False
        config.git.full_config = True
        config.providers.api_keys = True
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            save_config(config, path)
            # The file holds a bot token, so it must not be world-readable.
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(path.read_text())["version"], 1)
            loaded = load_config(path)
        self.assertTrue(loaded.claude.telegram_channel)
        self.assertEqual(loaded.notifications.telegram_token, "123:abc")
        self.assertEqual(loaded.notifications.telegram_chat_id, "555")
        self.assertTrue(loaded.github.gh_token)
        self.assertFalse(loaded.git.identity)
        self.assertTrue(loaded.git.full_config)
        self.assertTrue(loaded.providers.api_keys)

    def test_unknown_keys_are_dropped_on_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"version": 1, "remote_control": {"enabled": True}}))
            save_config(load_config(path), path)
            self.assertNotIn("remote_control", json.loads(path.read_text()))




class BrowserConfigTests(unittest.TestCase):
    """The browser switch starts a real browser the agent drives, inside the
    same box as the project, so it has to behave like every other opt-in
    capability here: off until asked for, and never silently on."""

    def test_defaults_disabled(self):
        self.assertFalse(Config().browser.enabled)

    def test_old_config_without_browser_still_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({
                "version": 1,
                "claude": {"telegram_channel": False},
                "notifications": {"enabled": False, "telegram_token": "", "telegram_chat_id": ""},
                "github": {"gh_token": False, "gh_config": False},
                "git": {"identity": True, "full_config": False},
                "providers": {"api_keys": False, "transport": True},
                "vikunja": {"enabled": False},
            }))
            config = load_config(path)
        self.assertFalse(config.browser.enabled)
        # The rest of a pre-browser file must survive untouched.
        self.assertTrue(config.git.identity)
        self.assertTrue(config.providers.transport)

    def test_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            config = Config()
            config.browser.enabled = True
            save_config(config, path)
            self.assertTrue(json.loads(path.read_text())["browser"]["enabled"])
            self.assertTrue(load_config(path).browser.enabled)

    def test_serialization_shape(self):
        self.assertEqual(Config().to_dict()["browser"], {"enabled": False})

    def test_a_non_boolean_is_not_treated_as_enabled(self):
        self.assertFalse(config_module.BrowserConfig.from_dict({"enabled": "yes"}).enabled)
        self.assertFalse(config_module.BrowserConfig.from_dict("nonsense").enabled)

    def test_the_section_holds_no_credential(self):
        """There is nothing to authenticate: each session gets an isolated,
        in-memory profile and no host browser state is exposed."""
        self.assertEqual(set(Config().browser.to_dict()), {"enabled"})

    def test_saving_does_not_bump_the_schema_version(self):
        """browser is newer than version 1 but needs no migration: an older
        file loads with the default, so bumping would only break older
        agent-box installs reading the same file."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            save_config(Config(), path)
            self.assertEqual(json.loads(path.read_text())["version"], 1)


class AudioConfigTests(unittest.TestCase):
    """The audio switch hands the box the host microphone, so it must be off
    until asked for and never read as on from a malformed value."""

    def test_defaults_disabled(self):
        self.assertFalse(Config().audio.enabled)

    def test_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            config = Config()
            config.audio.enabled = True
            save_config(config, path)
            self.assertTrue(json.loads(path.read_text())["audio"]["enabled"])
            self.assertTrue(load_config(path).audio.enabled)

    def test_a_non_boolean_is_not_treated_as_enabled(self):
        self.assertFalse(config_module.AudioConfig.from_dict({"enabled": "yes"}).enabled)
        self.assertFalse(config_module.AudioConfig.from_dict("nonsense").enabled)


class NotificationsUsableTests(unittest.TestCase):
    def test_enabled_alone_is_not_enough(self):
        config = Config()
        config.notifications.enabled = True
        self.assertFalse(config.notifications.usable)
        config.notifications.telegram_token = "t"
        self.assertFalse(config.notifications.usable)
        config.notifications.telegram_chat_id = "1"
        self.assertTrue(config.notifications.usable)

    def test_credentials_without_enabled_stay_inactive(self):
        config = Config()
        config.notifications.telegram_token = "t"
        config.notifications.telegram_chat_id = "1"
        self.assertFalse(config.notifications.usable)


class ClaudeStatuslineDefaultTests(unittest.TestCase):
    def test_statusline_defaults_on(self):
        self.assertTrue(config_module.ClaudeConfig().statusline)

    def test_a_config_written_before_the_setting_existed_gets_the_default(self):
        claude = config_module.ClaudeConfig.from_dict({"telegram_channel": True})
        self.assertTrue(claude.statusline)
        self.assertTrue(claude.telegram_channel)

    def test_an_explicit_false_is_kept(self):
        self.assertFalse(
            config_module.ClaudeConfig.from_dict({"statusline": False}).statusline)

    def test_round_trip(self):
        claude = config_module.ClaudeConfig(telegram_channel=True, statusline=False)
        self.assertEqual(
            config_module.ClaudeConfig.from_dict(claude.to_dict()), claude)


class DefaultAgentTests(unittest.TestCase):
    """A bare ``agent-box`` must open the TUI the user chose, and only a known one."""

    def test_claude_is_the_default_and_leaves_the_saved_file_sparse(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            save_config(Config(), path)
            self.assertNotIn("launch", json.loads(path.read_text()))
            self.assertEqual(load_config(path).launch.default_agent, "claude")

    def test_codex_choice_survives_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            config = Config()
            config.launch.default_agent = "codex"
            save_config(config, path)
            self.assertEqual(load_config(path).launch.default_agent, "codex")

    def test_an_unknown_agent_reads_as_claude_instead_of_failing_every_launch(self):
        for value in ("shell", "gemini", "", 3, None):
            with self.subTest(value=value):
                self.assertEqual(
                    Config.from_dict({"launch": {"default_agent": value}}).launch.default_agent, "claude")


if __name__ == "__main__":
    unittest.main()
