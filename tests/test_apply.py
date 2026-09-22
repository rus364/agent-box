"""Applying a configuration, and the fact that launching an agent does not.

Everything that changes the places agent-box does not own goes through one
function -- ``agent-box config`` at the end of its questions, ``agent-box
config apply`` on its own, ``agent-box upgrade`` once it has moved the
checkout -- so a configuration answered interactively and one edited by hand
produce the same host state.

The negative half matters just as much. Starting Claude or Codex must not
install a skill, write a symlink or rewrite ``settings.json`` on the way past:
a command that runs an agent should be safe to run without wondering what it
will change in your home directory, and "when did that appear?" should have
exactly one answer.
"""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_box import claude_settings, cli, config as config_module, runtime
from agent_box.config import Config, TrackerConnection
from agent_box.workspace import Workspace


def _tree(root: Path):
    """Every file and symlink under root, for an exact before/after comparison."""
    entries = {}
    for path in sorted(root.rglob("*")):
        name = str(path.relative_to(root))
        if path.is_symlink():
            entries[name] = ("link", os.readlink(path))
        elif path.is_file():
            entries[name] = ("file", path.read_bytes())
    return entries


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
        (self.shipped / "ab-tracker/scripts").mkdir(parents=True)
        (self.shipped / "ab-tracker/SKILL.md").write_text("# vikunja\n")
        self.patches = mock.patch.multiple(
            runtime,
            CLAUDE_HOME=self.claude,
            CODEX_HOME=self.root / "home/.codex",
            XDG_CACHE_HOME=self.root / "cache",
            AGENT_SKILLS_DIR=self.shared,
            MANAGED_SKILLS_SOURCE=self.shipped,
            STATE_ROOT=self.state,
            NOTIFY_CREDENTIALS=self.state / "runtime/notify.json",
            TRACKER_RUNTIME_DIR=self.state / "runtime/tracker",
            NOTIFY_STATE_ROOT=self.state / "notify",
            BUILD_STATE=self.state / "build",
        )
        self.patches.start()
        self.addCleanup(self.patches.stop)

    def _enabled(self):
        config = Config()
        config.tracker.enabled = True
        config.tracker.connections["vikunja"] = TrackerConnection("https://tasks.example.com", "tk")
        return config

    def _fully_enabled(self):
        """A saved configuration whose apply leaves every kind of artifact behind."""
        config = self._enabled()
        config.notifications.enabled = True
        config.notifications.telegram_token = "123456:token"
        config.notifications.telegram_chat_id = "42"
        config.workflow.enabled = True
        config.workflow.worker.tiers["1"] = [{"provider": "codex", "model": "chosen-model", "effort": "medium", "speed": "inherit", "api_provider": ""}]
        return config

    def test_it_installs_the_skills_the_configuration_enables(self):
        runtime.apply_config(self._enabled())
        self.assertTrue((self.shared / "ab-tracker/SKILL.md").is_file())

    def test_it_links_the_shared_tree_for_claude(self):
        runtime.apply_config(self._enabled())
        self.assertTrue((self.claude / "skills/ab-tracker").is_symlink())

    def test_the_link_switch_is_honoured(self):
        config = self._enabled()
        config.skills.claude_links = False
        runtime.apply_config(config)
        self.assertFalse((self.claude / "skills/ab-tracker").exists())

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
        self.assertTrue((self.shared / "ab-tracker/SKILL.md").is_file())

    def test_it_renders_the_vikunja_credentials(self):
        runtime.apply_config(self._enabled())
        self.assertTrue((self.state / "runtime/tracker/config.json").is_file())

    def test_it_is_idempotent(self):
        first = runtime.apply_config(self._enabled())
        second = runtime.apply_config(self._enabled())
        self.assertEqual(first, second)
        self.assertTrue((self.shared / "ab-tracker/SKILL.md").is_file())

    def test_turning_an_integration_off_and_applying_again_removes_it(self):
        runtime.apply_config(self._enabled())
        runtime.apply_config(Config())
        self.assertFalse((self.shared / "ab-tracker").exists())
        self.assertFalse((self.state / "runtime/tracker").exists())

    def test_the_report_names_what_was_installed(self):
        report = " ".join(runtime.apply_config(self._enabled()))
        self.assertIn("ab-tracker", report)
        self.assertIn(str(self.claude / "settings.json"), report)

    def test_config_apply_applies_the_saved_configuration(self):
        """The one command for a config.json somebody edited by hand."""
        with mock.patch.object(cli, "load_config", return_value=self._enabled()):
            cli.config_apply()
        self.assertTrue((self.shared / "ab-tracker/SKILL.md").is_file())

    def test_config_apply_reports_an_incomplete_integration(self):
        """Turning Vikunja on without a URL must not look like it worked."""
        broken = Config()
        broken.tracker.enabled = True
        errors = io.StringIO()
        with mock.patch.object(cli, "load_config", return_value=broken), \
             contextlib.redirect_stderr(errors):
            cli.config_apply()
        self.assertIn("warning", errors.getvalue())

    def test_config_apply_in_a_session_refreshes_skills_and_leaves_host_sections_alone(self):
        """The 2026-09-13 incident: applying from a box must not remove host integrations."""
        config = self._fully_enabled()
        with contextlib.redirect_stderr(io.StringIO()):
            runtime.apply_config(config)
        # Every kind of artifact the incident lost, present before the session apply.
        self.assertTrue((self.shared / "ab-tracker/SKILL.md").is_file())
        self.assertTrue((self.shared / "ab-tracker/.agent-box-managed.json").is_file())
        self.assertTrue((self.claude / "skills/ab-tracker").is_symlink())
        self.assertTrue((self.claude / "settings.json").is_file())
        self.assertTrue((self.root / "home/.codex/config.toml").is_file())
        self.assertTrue((self.root / "home/.codex/agents/ab-worker-1.toml").is_file())
        (self.shipped / "ab-tracker/SKILL.md").write_text("# vikunja v2\n")
        snapshot = self.root / "session/config.json"
        snapshot.parent.mkdir(parents=True)
        snapshot.write_text(json.dumps(config.to_dict()))
        before = _tree(self.root)
        saved = self.root / "host/config.json"  # never mounted inside a session box
        output = io.StringIO()
        with mock.patch.dict(os.environ, {"AGENT_BOX_RUNTIME_CONFIG": str(snapshot)}), \
             mock.patch.object(config_module, "CONFIG_FILE", saved), \
             contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(output):
            cli.config_apply()
        self.assertEqual((self.shared / "ab-tracker/SKILL.md").read_text(), "# vikunja v2\n")
        after = _tree(self.root)
        changed = {name for name in set(before) | set(after) if before.get(name) != after.get(name)}
        # Only the refreshed skill moved: notifications, hooks, status line,
        # credentials, PATH and Claude links stayed exactly as the host left them.
        self.assertEqual(changed, {
            "home/.agents/skills/ab-tracker/SKILL.md",
            "home/.agents/skills/ab-tracker/.agent-box-managed.json",
        })
        self.assertIn(f"applying the rendered session configuration {snapshot}", output.getvalue())
        self.assertIn("host-only and untouched", output.getvalue())

    def test_interactive_config_refuses_in_the_same_session(self):
        """The menu edits the saved file, which a session box does not mount."""
        snapshot = self.root / "session/config.json"
        snapshot.parent.mkdir(parents=True)
        snapshot.write_text(json.dumps(self._enabled().to_dict()))
        saved = self.root / "host/config.json"
        errors = io.StringIO()
        with mock.patch.dict(os.environ, {"AGENT_BOX_RUNTIME_CONFIG": str(snapshot)}), \
             mock.patch.object(config_module, "CONFIG_FILE", saved), \
             mock.patch("agent_box.config_menu.edit", side_effect=AssertionError("menu opened")), \
             mock.patch.object(runtime, "apply_config", side_effect=AssertionError("session apply")), \
             contextlib.redirect_stderr(errors), self.assertRaises(SystemExit):
            cli.config_interactive()
        self.assertIn("refusing to edit", errors.getvalue())

    def test_interactive_config_still_opens_when_the_saved_file_is_present(self):
        """A host session on the real host has the file; the menu must still open."""
        config = self._enabled()
        saved = self.root / "host/config.json"
        saved.parent.mkdir(parents=True)
        saved.write_text(json.dumps(config.to_dict()))
        snapshot = self.root / "session/config.json"
        snapshot.parent.mkdir(parents=True)
        snapshot.write_text(json.dumps(config.to_dict()))
        output = io.StringIO()
        with mock.patch.dict(os.environ, {"AGENT_BOX_RUNTIME_CONFIG": str(snapshot)}), \
             mock.patch.object(config_module, "CONFIG_FILE", saved), \
             mock.patch("agent_box.config_menu.edit", return_value=None), \
             contextlib.redirect_stdout(output):
            cli.config_interactive()
        self.assertIn("Configuration unchanged.", output.getvalue())

    def test_config_apply_still_applies_when_the_saved_file_is_present(self):
        """A host session on the real host keeps applying: the file is mounted there."""
        config = self._enabled()
        saved = self.root / "host/config.json"
        saved.parent.mkdir(parents=True)
        saved.write_text(json.dumps(config.to_dict()))
        snapshot = self.root / "session/config.json"
        snapshot.parent.mkdir(parents=True)
        snapshot.write_text(json.dumps(config.to_dict()))
        output = io.StringIO()
        with mock.patch.dict(os.environ, {"AGENT_BOX_RUNTIME_CONFIG": str(snapshot)}), \
             mock.patch.object(config_module, "CONFIG_FILE", saved), \
             contextlib.redirect_stdout(output):
            cli.config_apply()
        self.assertTrue((self.shared / "ab-tracker/SKILL.md").is_file())
        self.assertIn("skills installed", output.getvalue())
        self.assertNotIn("rendered session configuration", output.getvalue())

    def test_apply_is_the_only_config_verb_that_writes(self):
        """`config show` and `config check` are questions, not actions."""
        with mock.patch.object(cli, "config_apply") as apply_config:
            with self.assertRaises(SystemExit):
                cli.config_command(["nonsense"])
        self.assertFalse(apply_config.called)


class LaunchAppliesNothingTests(unittest.TestCase):
    """The whole point of keeping applying out of the launch path."""

    def _launch(self, config: Config, tmp: Path):
        workspace = Workspace(tmp, tmp)
        # runtime_main resolves the Containerfile out of the checkout it was
        # started from; a temporary one stands in so the launch gets far
        # enough to prove it applies nothing.
        (tmp / "Containerfile").write_text("FROM scratch\n")
        with mock.patch.object(runtime.shutil, "which", return_value="/usr/bin/podman"), \
             mock.patch.object(runtime, "resolve_workspace", return_value=workspace), \
             mock.patch.object(runtime, "ensure_state_dirs"), \
             mock.patch.object(runtime, "POLICY_RUNTIME_DIR", tmp / "runtime/policy"), \
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
            (shipped / "ab-tracker").mkdir(parents=True)
            (shipped / "ab-tracker/SKILL.md").write_text("# vikunja\n")
            config = Config()
            config.tracker.enabled = True
            with mock.patch.multiple(
                runtime, CLAUDE_HOME=claude, AGENT_SKILLS_DIR=shared,
                MANAGED_SKILLS_SOURCE=shipped, TRACKER_RUNTIME_DIR=root / "runtime/tracker",
                WORKFLOW_STATE=root / "workflow",
            ):
                self._launch(config, root)
            self.assertFalse((shared / "ab-tracker").exists())
            self.assertFalse((claude / "settings.json").exists())

    def test_a_launch_calls_no_apply(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(runtime, "apply_config") as apply_config:
                self._launch(Config(), root)
            apply_config.assert_not_called()


if __name__ == "__main__":
    unittest.main()
