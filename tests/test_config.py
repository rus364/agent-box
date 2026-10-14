"""The unified agent-box configuration.

The load/save round trip is trivial; what these tests actually pin down is the
security posture of the defaults. Every switch that hands host credentials to
a container must be off until the user turns it on in ``agent-box config`` --
in particular the GitHub token, which an earlier agent-box forwarded to every
container purely because ``gh`` happened to be authenticated on the host.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_box import cli
from agent_box import config as config_module
from agent_box.config import Config, effective_config, load_config, save_config
from agent_box.errors import AgentBoxError


class DefaultsAreSafeTests(unittest.TestCase):
    def test_credential_passthrough_is_off_by_default(self):
        config = Config()
        self.assertFalse(config.credentials.github.token)
        self.assertFalse(config.credentials.github.config)
        self.assertFalse(config.agents.claude.telegram_channel)
        self.assertFalse(config.host.notifications.enabled)

    def test_provider_api_keys_are_off_by_default(self):
        """An ANTHROPIC_API_KEY exported in your shell is not a decision to
        hand it to an agent container; the agents authenticate through the
        mounted ~/.claude / ~/.codex directories instead."""
        self.assertFalse(Config().agents.host_env.api_keys)

    def test_full_git_config_copy_is_off_by_default(self):
        """A real ~/.gitconfig routinely carries credential.helper entries,
        http.<url>.extraHeader tokens and include.path directives."""
        self.assertFalse(Config().credentials.git.full_config)

    def test_non_secret_conveniences_default_on(self):
        config = Config()
        self.assertTrue(config.credentials.git.identity)       # only user.name / user.email
        self.assertTrue(config.agents.host_env.transport)  # endpoints, proxies, CA bundle

    def test_missing_file_yields_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(Path(tmp) / "missing.json")
            self.assertFalse(config.credentials.github.token)

    def test_partial_file_does_not_silently_enable_anything(self):
        """A file written by a future/older agent-box must not turn a
        credential switch on just by omitting it."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"version": 1, "agents": {"claude": {"telegram_channel": True}}}))
            config = load_config(path)
            self.assertTrue(config.agents.claude.telegram_channel)
            self.assertFalse(config.credentials.github.token)
            self.assertFalse(config.host.notifications.enabled)
            self.assertFalse(config.agents.host_env.api_keys)
            self.assertFalse(config.credentials.git.full_config)


class UnreadableFileTests(unittest.TestCase):
    """A saved file that cannot be read is an error, never the defaults.

    Read as defaults, it let the menu save them over the user's settings,
    `upgrade` unapply every integration and `notify test` delete the rendered
    Telegram credentials, all without saying the file had stopped loading.
    """

    BROKEN = ('{ "agents": ', '[]', '"text"', '\udcff')

    def broken_file(self, tmp, text):
        path = Path(tmp) / "config.json"
        path.write_bytes(b"\xff\xfe" if text == '\udcff' else text.encode())
        return path

    def test_a_file_that_is_not_a_json_object_names_itself(self):
        for text in self.BROKEN:
            with self.subTest(text=text), tempfile.TemporaryDirectory() as tmp:
                path = self.broken_file(tmp, text)
                with self.assertRaises(AgentBoxError) as caught:
                    load_config(path)
                self.assertIn(str(path), str(caught.exception))
                self.assertIn("delete it", str(caught.exception))

    @unittest.skipIf(os.geteuid() == 0, "root reads a file whatever its mode")
    def test_a_file_without_read_permission_is_not_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.broken_file(tmp, '{"version": 1}')
            path.chmod(0)
            with self.assertRaisesRegex(AgentBoxError, "Permission denied"):
                load_config(path)

    def test_no_command_acts_on_defaults_in_place_of_a_broken_file(self):
        """The menu, `notify test` and a launch stop before writing or applying anything."""
        with tempfile.TemporaryDirectory() as tmp:
            path = self.broken_file(tmp, '{ "agents": ')
            commands = {
                "config": lambda: cli.config_interactive(),
                "notify test": lambda: cli.notify_command(["test"]),
                "launch": lambda: effective_config(),
            }
            for name, command in commands.items():
                # A menu that reports no change keeps a regression away from save and apply.
                with self.subTest(command=name), \
                     mock.patch.object(config_module, "CONFIG_FILE", path), \
                     mock.patch("agent_box.config_menu.edit", return_value=None) as menu, \
                     mock.patch.object(cli.notify_module, "sync_credentials") as sync, \
                     mock.patch("agent_box.project_config.load", return_value={}):
                    with self.assertRaises(AgentBoxError):
                        command()
                    menu.assert_not_called()
                    sync.assert_not_called()
                    self.assertEqual(path.read_bytes(), b'{ "agents": ')


class SectionedSchemaTests(unittest.TestCase):
    def test_keys_of_the_former_flat_layout_are_ignored(self):
        """Pre-release: no migration reads an old key, so it must not switch anything on either."""
        old = {"version": 1, "default_agent": "codex", "github": {"gh_token": True, "gh_config": True},
               "tools": {"browser": True, "k3s": {"enabled": True}}, "audio": {"enabled": True},
               "notifications": {"enabled": True, "telegram_token": "t", "telegram_chat_id": "1"},
               "providers": {"api_keys": True}, "git": {"full_config": True},
               "install": {"bin_dir": "/somewhere"}, "skills": {"claude_links": False}}
        self.assertEqual(Config.from_dict(old).to_dict(), Config().to_dict())

    def test_the_root_holds_one_section_per_menu_entry(self):
        """The file and the menu share one tree, so a path read in one is found in the other."""
        self.assertEqual(list(Config().to_dict()),
                         ["version", "agents", "box", "credentials", "tracker", "workflow", "host"])


class RoundTripTests(unittest.TestCase):
    def test_default_global_file_is_sparse_and_reconstructs_all_program_defaults(self):
        """Saving defaults must leave later program defaults free to take effect."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            save_config(Config(), path)
            self.assertEqual(json.loads(path.read_text()), {'version': 1})
            self.assertEqual(load_config(path).to_dict(), Config().to_dict())

    def test_native_agent_wait_defaults_are_cache_aware_and_round_trip_custom_values(self):
        """A user can disable or tune the launch policy without editing Codex config."""
        defaults = Config().agents.codex.native_agent_wait
        self.assertTrue(defaults.enabled)
        self.assertEqual(defaults.interval_seconds, 28 * 60)
        config = Config()
        config.agents.codex.native_agent_wait.enabled = False
        config.agents.codex.native_agent_wait.interval_seconds = 900
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            save_config(config, path)
            self.assertEqual(json.loads(path.read_text()), {
                'version': 1,
                'agents': {'codex': {'native_agent_wait': {'enabled': False, 'interval_seconds': 900}}},
            })
            loaded = load_config(path)
        self.assertFalse(loaded.agents.codex.native_agent_wait.enabled)
        self.assertEqual(loaded.agents.codex.native_agent_wait.interval_seconds, 900)

    def test_native_agent_wait_rejects_values_codex_cannot_accept(self):
        """The editable interval stays within the installed wait tool's bounds."""
        for value in (9, 3601, 28.0):
            with self.subTest(value=value):
                config = Config.from_dict({"agents": {"codex": {"native_agent_wait": {"interval_seconds": value}}}})
                self.assertTrue(config.agents.codex.validate())
        self.assertEqual(Config.from_dict({"agents": {"codex": {"native_agent_wait": {
            "interval_seconds": 3600,
        }}}}).agents.codex.validate(), [])

    def test_sparse_global_roundtrip_keeps_nested_false_empty_lists_and_custom_values(self):
        """An omitted sibling inherits; false and empty lists can still override defaults."""
        config = Config()
        config.workflow.auto_push = False
        config.workflow.review.sensitive_paths = []
        config.workflow.review.models = [{'provider': 'codex', 'model': 'custom-model', 'effort': 'medium', 'speed': 'inherit', 'api_provider': ''}]
        config.box.tools.groups['analysis'] = False
        config.box.env.set['CUSTOM'] = ''
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            save_config(config, path)
            data = json.loads(path.read_text())
            self.assertEqual(data, {
                'version': 1, 'box': {'tools': {'analysis': False}, 'env': {'set': {'CUSTOM': ''}}},
                'workflow': {'auto_push': False, 'review': {'sensitive_paths': [],
                    'models': [{'provider': 'codex', 'model': 'custom-model', 'effort': 'medium', 'speed': 'inherit', 'api_provider': ''}]}}})
            self.assertEqual(load_config(path).to_dict(), config.to_dict())
            path.write_text(json.dumps(config.to_dict()))
            save_config(load_config(path), path)
            self.assertEqual(json.loads(path.read_text()), data)

    def test_save_load_roundtrip_and_permissions(self):
        config = Config()
        config.agents.claude.telegram_channel = True
        config.host.notifications.enabled = True
        config.host.notifications.telegram_token = "123:abc"
        config.host.notifications.telegram_chat_id = "555"
        config.credentials.github.token = True
        config.credentials.git.identity = False
        config.credentials.git.full_config = True
        config.agents.host_env.api_keys = True
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            save_config(config, path)
            # The file holds a bot token, so it must not be world-readable.
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(path.read_text())["version"], 1)
            loaded = load_config(path)
        self.assertTrue(loaded.agents.claude.telegram_channel)
        self.assertEqual(loaded.host.notifications.telegram_token, "123:abc")
        self.assertEqual(loaded.host.notifications.telegram_chat_id, "555")
        self.assertTrue(loaded.credentials.github.token)
        self.assertFalse(loaded.credentials.git.identity)
        self.assertTrue(loaded.credentials.git.full_config)
        self.assertTrue(loaded.agents.host_env.api_keys)

    def test_unknown_keys_are_dropped_on_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"version": 1, "remote_control": {"enabled": True}}))
            save_config(load_config(path), path)
            self.assertNotIn("remote_control", json.loads(path.read_text()))




class BrowserToolTests(unittest.TestCase):
    """The browser group starts a real browser the agent drives, inside the
    same box as the project, so it has to behave like every other opt-in
    capability here: off until asked for, and never silently on."""

    def test_defaults_disabled(self):
        self.assertFalse(Config().box.tools.groups["browser"])
        self.assertFalse(Config().box.tools.browser())

    def test_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            config = Config()
            config.box.tools.groups["browser"] = True
            save_config(config, path)
            self.assertEqual(json.loads(path.read_text())["box"]["tools"], {"browser": True})
            self.assertTrue(load_config(path).box.tools.browser())

    def test_a_non_boolean_is_not_treated_as_enabled(self):
        self.assertFalse(config_module.ToolsConfig.from_dict({"browser": "yes"}).groups["browser"])
        self.assertFalse(config_module.ToolsConfig.from_dict("nonsense").groups["browser"])

    def test_a_session_opt_out_is_never_saved(self):
        """``--no-browser`` shapes one session; the saved group must stay on."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            config = Config()
            config.box.tools.groups["browser"] = True
            config.box.tools.browser_off = True
            self.assertFalse(config.box.tools.browser())
            save_config(config, path)
            self.assertTrue(load_config(path).box.tools.browser())


class AudioConfigTests(unittest.TestCase):
    """The audio switch hands the box the host microphone, so it must be off
    until asked for and never read as on from a malformed value."""

    def test_defaults_disabled(self):
        self.assertFalse(Config().box.audio)

    def test_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            config = Config()
            config.box.audio = True
            save_config(config, path)
            self.assertTrue(json.loads(path.read_text())["box"]["audio"])
            self.assertTrue(load_config(path).box.audio)

    def test_a_non_boolean_is_not_treated_as_enabled(self):
        self.assertFalse(config_module.BoxConfig.from_dict({"audio": "yes"}).audio)
        self.assertFalse(config_module.BoxConfig.from_dict("nonsense").audio)


class NotificationsUsableTests(unittest.TestCase):
    def test_enabled_alone_is_not_enough(self):
        config = Config()
        config.host.notifications.enabled = True
        self.assertFalse(config.host.notifications.usable)
        config.host.notifications.telegram_token = "t"
        self.assertFalse(config.host.notifications.usable)
        config.host.notifications.telegram_chat_id = "1"
        self.assertTrue(config.host.notifications.usable)

    def test_credentials_without_enabled_stay_inactive(self):
        config = Config()
        config.host.notifications.telegram_token = "t"
        config.host.notifications.telegram_chat_id = "1"
        self.assertFalse(config.host.notifications.usable)


class StatuslineDefaultTests(unittest.TestCase):
    """The status line switch and its extra segment are one section of one line."""

    def test_statusline_defaults_on_without_an_extra_segment(self):
        statusline = Config().agents.claude.statusline
        self.assertTrue(statusline.enabled)
        self.assertEqual(statusline.command, "")

    def test_a_sparse_file_with_only_a_command_keeps_the_line_on(self):
        """Saving omits defaults, so a command alone must not read as switching the line off."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            config = Config()
            config.agents.claude.statusline.command = "echo balance"
            save_config(config, path)
            self.assertEqual(json.loads(path.read_text())["agents"]["claude"]["statusline"], {"command": "echo balance"})
            self.assertTrue(load_config(path).agents.claude.statusline.enabled)

    def test_switch_and_command_survive_a_round_trip_together(self):
        config = Config()
        config.agents.claude.statusline.enabled = False
        config.agents.claude.statusline.command = "echo balance"
        self.assertEqual(Config.from_dict(config.to_dict()).agents.claude.statusline, config.agents.claude.statusline)


class DefaultAgentTests(unittest.TestCase):
    """A bare ``agent-box`` must open the TUI the user chose, and only a known one."""

    def test_claude_is_the_default_and_leaves_the_saved_file_sparse(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            save_config(Config(), path)
            self.assertNotIn("default_agent", json.loads(path.read_text()))
            self.assertEqual(load_config(path).agents.default, "claude")

    def test_codex_choice_survives_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            config = Config()
            config.agents.default = "codex"
            save_config(config, path)
            self.assertEqual(load_config(path).agents.default, "codex")

    def test_an_unknown_agent_reads_as_claude_instead_of_failing_every_launch(self):
        for value in ("shell", "gemini", "", 3, None):
            with self.subTest(value=value):
                self.assertEqual(Config.from_dict({"agents": {"default": value}}).agents.default, "claude")


if __name__ == "__main__":
    unittest.main()
