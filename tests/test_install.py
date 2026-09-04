"""Applying a configuration, and the fact that launching an agent does not.

Two commands change the places agent-box does not own -- ``agent-box config``
and ``agent-box install`` -- and they change them through the same function,
so a configuration answered interactively and one edited by hand produce the
same host state.

The negative half matters just as much. Starting Claude or Codex must not
install a skill, write a symlink or rewrite ``settings.json`` on the way past:
a command that runs an agent should be safe to run without wondering what it
will change in your home directory, and "when did that appear?" should have
exactly one answer.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_box import claude_settings, runtime
from agent_box.config import Config
from agent_box.errors import AgentBoxError
from agent_box.workspace import Workspace


class ApplyConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.claude = self.root / "home/.claude"
        self.shared = self.root / "home/.agents/skills"
        self.shipped = self.root / "shipped"
        self.state = self.root / "state"
        self.shared.mkdir(parents=True)
        (self.shipped / "vikunja/scripts").mkdir(parents=True)
        (self.shipped / "vikunja/SKILL.md").write_text("# vikunja\n")
        self.patches = mock.patch.multiple(
            runtime,
            CLAUDE_HOME=self.claude,
            AGENT_SKILLS_DIR=self.shared,
            MANAGED_SKILLS_SOURCE=self.shipped,
            STATE_ROOT=self.state,
            NOTIFY_CREDENTIALS=self.state / "runtime/notify.json",
            VIKUNJA_CREDENTIALS_DIR=self.state / "runtime/vikunja",
            NOTIFY_STATE_ROOT=self.state / "notify",
            BUILD_STATE=self.state / "build",
        )
        self.patches.start()
        self.addCleanup(self.patches.stop)

    def _enabled(self):
        config = Config()
        config.vikunja.enabled = True
        config.vikunja.url = "https://tasks.example.com"
        config.vikunja.token = "tk"
        return config

    def test_it_installs_the_skills_the_configuration_enables(self):
        runtime.apply_config(self._enabled())
        self.assertTrue((self.shared / "vikunja/SKILL.md").is_file())

    def test_it_links_the_shared_tree_for_claude(self):
        runtime.apply_config(self._enabled())
        self.assertTrue((self.claude / "skills/vikunja").is_symlink())

    def test_the_link_switch_is_honoured(self):
        config = self._enabled()
        config.skills.claude_links = False
        runtime.apply_config(config)
        self.assertFalse((self.claude / "skills/vikunja").exists())

    def test_it_installs_the_status_line(self):
        runtime.apply_config(Config())
        settings = json.loads((self.claude / "settings.json").read_text())
        self.assertTrue(claude_settings.statusline_is_ours(settings))

    def test_the_status_line_override_is_honoured(self):
        """AGENT_BOX_STATUSLINE=0 applies everything except that one slot."""
        import os
        with mock.patch.dict(os.environ, {claude_settings.STATUSLINE_ENV: "0"}):
            report = runtime.apply_config(self._enabled())
        self.assertFalse((self.claude / "settings.json").exists())
        self.assertTrue(any("status line left untouched" in line for line in report))
        self.assertTrue((self.shared / "vikunja/SKILL.md").is_file())

    def test_it_renders_the_vikunja_credentials(self):
        runtime.apply_config(self._enabled())
        self.assertTrue((self.state / "runtime/vikunja/token").is_file())

    def test_it_is_idempotent(self):
        first = runtime.apply_config(self._enabled())
        second = runtime.apply_config(self._enabled())
        self.assertEqual(first, second)
        self.assertTrue((self.shared / "vikunja/SKILL.md").is_file())

    def test_turning_an_integration_off_and_applying_again_removes_it(self):
        runtime.apply_config(self._enabled())
        runtime.apply_config(Config())
        self.assertFalse((self.shared / "vikunja").exists())
        self.assertFalse((self.state / "runtime/vikunja").exists())

    def test_the_report_names_what_was_installed(self):
        report = " ".join(runtime.apply_config(self._enabled()))
        self.assertIn("vikunja", report)
        self.assertIn(str(self.claude / "settings.json"), report)

    def test_install_takes_no_arguments(self):
        with self.assertRaises(AgentBoxError):
            runtime.install_main(["--now"])

    def test_install_applies_the_saved_configuration(self):
        with mock.patch.object(runtime, "load_config", return_value=self._enabled()):
            self.assertEqual(runtime.install_main([]), 0)
        self.assertTrue((self.shared / "vikunja/SKILL.md").is_file())

    def test_install_reports_an_incomplete_integration(self):
        broken = Config()
        broken.vikunja.enabled = True
        with mock.patch.object(runtime, "load_config", return_value=broken), \
             mock.patch.object(runtime, "warn") as warn:
            runtime.install_main([])
        self.assertTrue(warn.called)


class LaunchAppliesNothingTests(unittest.TestCase):
    """The whole point of moving the work into `install`."""

    def _launch(self, config: Config, tmp: Path):
        workspace = Workspace(tmp, tmp)
        # runtime_main resolves the Containerfile out of the checkout it was
        # started from; a temporary one stands in so the launch gets far
        # enough to prove it applies nothing.
        (tmp / "Containerfile").write_text("FROM scratch\n")
        with mock.patch.object(runtime.shutil, "which", return_value="/usr/bin/podman"), \
             mock.patch.object(runtime, "resolve_workspace", return_value=workspace), \
             mock.patch.object(runtime, "ensure_state_dirs"), \
             mock.patch.object(runtime, "ensure_refresh_token"), \
             mock.patch.object(runtime, "load_config", return_value=config), \
             mock.patch.object(runtime, "image_exists", return_value=True), \
             mock.patch.object(runtime, "claude_channel_args", return_value=([], [])), \
             mock.patch.object(runtime, "launch_in_workspace_container", return_value=0), \
             self.assertRaises(SystemExit):
            runtime.runtime_main(["claude"], tmp)

    def test_a_launch_installs_no_skill_and_writes_no_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude, shared = root / "claude", root / "agents/skills"
            shared.mkdir(parents=True)
            shipped = root / "shipped"
            (shipped / "vikunja").mkdir(parents=True)
            (shipped / "vikunja/SKILL.md").write_text("# vikunja\n")
            config = Config()
            config.vikunja.enabled = True
            with mock.patch.multiple(
                runtime, CLAUDE_HOME=claude, AGENT_SKILLS_DIR=shared,
                MANAGED_SKILLS_SOURCE=shipped,
            ):
                self._launch(config, root)
            self.assertFalse((shared / "vikunja").exists())
            self.assertFalse((claude / "settings.json").exists())

    def test_a_launch_calls_no_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(runtime, "apply_config") as apply_config:
                self._launch(Config(), root)
            apply_config.assert_not_called()


if __name__ == "__main__":
    unittest.main()
