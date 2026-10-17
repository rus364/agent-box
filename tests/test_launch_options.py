"""Session options last one session: they win over both configuration files and change neither."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_box import cli, launch_options, notify, project_config, runtime
from agent_box.config import Config, TrackerConnection, effective_config, model_entry
from agent_box.errors import AgentBoxError
from agent_box.launch_options import LaunchOptions
from agent_box.routing import select
from agent_box.workspace import Workspace

PROVIDERS = ["codex", "claude"]


def workflow_config():
    config = Config()
    config.workflow.enabled = True
    config.workflow.scout.models = [model_entry({"provider": provider, "model": provider + "-scout"}) for provider in PROVIDERS]
    for role in (config.workflow.worker, config.workflow.review):
        for tier in role.tiers:
            role.tiers[tier] = [model_entry({"provider": provider, "model": provider + "-tier"}) for provider in PROVIDERS]
    return config


class IsolatedLaunchTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for patcher in (patch.dict(os.environ, {}, clear=True),
                        patch.object(runtime, "TRACKER_RUNTIME_DIR", self.root / "tracker"),
                        patch.object(runtime, "POLICY_RUNTIME_DIR", self.root / "policy"),
                        patch.object(runtime, "container_tracker_url", side_effect=lambda url: url),
                        patch.object(project_config, "load", return_value={})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def launch(self, config, overrides, project=None):
        with patch.object(runtime, "load_config", return_value=config), \
             patch.object(project_config, "load", return_value=project or {}):
            launched = runtime.launch_config(LaunchOptions(overrides=overrides))
            return launched, runtime.session_runtime(launched, container=False)


class SessionPrecedenceTests(IsolatedLaunchTest):
    def test_k3s_override_survives_project_changes_without_becoming_a_saved_setting(self):
        """A one-launch cluster choice wins over the project and remains absent from its global baseline."""
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                config = Config()
                config.box.k3s.enabled = not enabled
                project = {'box': {'k3s': {'enabled': not enabled}}}
                launched, snapshot = self.launch(config, {'k3s': enabled}, project)
                self.assertEqual(config.box.k3s.enabled, not enabled)
                self.assertEqual(launched.box.k3s.enabled, enabled)
                with patch.dict(os.environ, {'AGENT_BOX_RUNTIME_CONFIG': str(snapshot)}), \
                     patch.object(project_config, 'load', return_value=project):
                    self.assertEqual(effective_config().box.k3s.enabled, enabled)
                    self.assertEqual(effective_config(include_project=False).box.k3s.enabled, not enabled)

    def test_session_options_win_over_project_inside_the_session_too(self):
        """The session re-applies the project, so the flags must be re-applied after it."""
        config = workflow_config()
        config.workflow.review.policy = "off"
        project = {"workflow": {"review": {"policy": "auto", "max_rounds": 2}, "auto_push": True}}
        launched, snapshot = self.launch(config, {"review": "always", "push": False}, project)
        self.assertEqual(config.workflow.review.policy, "off")
        with patch.dict(os.environ, {"AGENT_BOX_RUNTIME_CONFIG": str(snapshot)}), \
             patch.object(project_config, "load", return_value=project):
            session = effective_config()
        for result in (launched, session):
            self.assertEqual((result.workflow.review.policy, result.workflow.auto_push, result.workflow.review.max_rounds),
                             ("always", False, 2))

    def test_no_workflow_session_receives_no_tracker_token(self):
        """Switching the tracker off for a session must also keep its credential out of the box."""
        config = workflow_config()
        config.tracker.enabled = True
        config.tracker.connections["vikunja"] = TrackerConnection("https://tasks.example", "SECRET")
        launched, snapshot = self.launch(config, {"workflow": False})
        self.assertFalse(launched.workflow.enabled or launched.tracker.enabled)
        self.assertNotIn("SECRET", snapshot.read_text())

    def test_nested_launch_keeps_the_outer_session_options_and_adds_its_own(self):
        _, snapshot = self.launch(workflow_config(), {"review": "always", "rounds": 4})
        with patch.dict(os.environ, {"AGENT_BOX_RUNTIME_CONFIG": str(snapshot)}):
            nested = runtime.launch_config(LaunchOptions(overrides={"rounds": 7}))
        self.assertEqual((nested.workflow.review.policy, nested.workflow.review.max_rounds), ("always", 7))
        self.assertEqual(nested._launch, {"review": "always", "rounds": 7})

    def test_a_project_copy_inside_the_session_cannot_pick_up_session_options(self):
        """The editor's global baseline is the snapshot's base layer; flags must not be saved from it."""
        _, snapshot = self.launch(workflow_config(), {"review": "always", "push": False})
        with patch.dict(os.environ, {"AGENT_BOX_RUNTIME_CONFIG": str(snapshot)}):
            baseline = effective_config(include_project=False)
        self.assertEqual((baseline.workflow.review.policy, baseline.workflow.auto_push), ("auto", True))

    def test_notify_on_needs_configured_notifications_and_leaves_them_on(self):
        with self.assertRaisesRegex(AgentBoxError, "--notify needs"):
            self.launch(Config(), {"notify": True})
        config = Config()
        config.host.notifications.enabled, config.host.notifications.telegram_token, config.host.notifications.telegram_chat_id = True, "t", "c"
        launched, _ = self.launch(config, {"notify": True})
        self.assertTrue(launched.host.notifications.usable)
        with patch.object(runtime.shutil, "which", return_value="/usr/bin/codex"), \
             patch.object(runtime, "resolve_workspace", return_value=Workspace(self.root, self.root)), \
             patch.object(runtime, "launch_config", return_value=launched), \
             patch.object(runtime, "tmux_enabled", return_value=False), \
             patch.object(runtime.subprocess, "call", return_value=0) as call:
            runtime.host_main("codex", [])
        self.assertNotIn(notify.NOTIFY_OFF_ENV, call.call_args.kwargs["env"])

    def test_session_tracker_follows_the_provider_the_project_selects(self):
        """The saved configuration may name another provider; the account lands where the session reads it."""
        config = workflow_config()
        config.tracker.enabled, config.tracker.provider = True, "jira"
        project = {"tracker": {"provider": "vikunja"}}
        with patch.dict(os.environ, {"VIKUNJA_TOKEN": "session-secret"}):
            launched, snapshot = self.launch(config, {"tracker": "https://tasks.example"}, project)
        self.assertEqual(launched._project_base.tracker.connection("vikunja").token, "session-secret")
        rendered = json.loads(snapshot.read_text())["tracker"]
        self.assertEqual(rendered["connections"]["vikunja"], {"url": "https://tasks.example", "token": "session-secret"})
        with patch.dict(os.environ, {"VIKUNJA_TOKEN": "session-secret"}), \
             self.assertRaisesRegex(AgentBoxError, "supports the vikunja tracker; this configuration selects jira"):
            enabled = workflow_config()
            enabled.tracker.enabled = True
            self.launch(enabled, {"tracker": "https://tasks.example"}, {"tracker": {"provider": "jira"}})

    def test_session_tracker_requires_explicit_vikunja_selection(self):
        """A server URL does not silently restore the old default provider."""
        config = Config()
        config.tracker.enabled = True
        with patch.dict(os.environ, {"VIKUNJA_TOKEN": "session-secret"}):
            with self.assertRaisesRegex(AgentBoxError, "vikunja provider selected.*agent-box config"):
                self.launch(config, {"tracker": "https://tasks.example"})
            launched, _ = self.launch(config, {"tracker": "https://tasks.example"},
                                      {"tracker": {"provider": "vikunja"}})
        self.assertEqual(launched.tracker.provider, "vikunja")
        self.assertEqual(launched.tracker.connection().token, "session-secret")

    def test_notify_inside_a_session_is_refused_for_what_it_is(self):
        """A snapshot carries no notification settings, so the host-side reason would be false there."""
        _, snapshot = self.launch(workflow_config(), {})
        with patch.dict(os.environ, {"AGENT_BOX_RUNTIME_CONFIG": str(snapshot)}), \
             self.assertRaisesRegex(AgentBoxError, "a session cannot turn notifications on"):
            runtime.launch_config(LaunchOptions(overrides={"notify": True}))

    def test_config_show_names_the_session_options(self):
        _, snapshot = self.launch(workflow_config(), {"worker": "codex:gpt-5.5:high", "push": False})
        with patch.dict(os.environ, {"AGENT_BOX_RUNTIME_CONFIG": str(snapshot)}), \
             patch.object(cli, "_image_note", return_value=""), \
             patch.object(cli, "_statusline_note", return_value=""), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            cli.config_show()
        self.assertIn("Session options: --no-push --worker codex:gpt-5.5:high", output.getvalue())

    def test_options_that_need_a_disabled_integration_are_refused(self):
        for config, overrides, message in (
                (Config(), {"review": "always"}, "--review needs workflow"),
                (workflow_config(), {"workflow": False, "rounds": 2}, "drop --no-workflow"),
                (workflow_config(), {"tracker": "https://tasks.example", "workflow": False}, "contradict --no-workflow"),
                (Config(), {"project": "12"}, "--project needs the tracker"),
                (workflow_config(), {"tracker": "https://tasks.example"}, "--tracker needs the tracker"),
                (workflow_config(), {"reviewer": "claude@deepseek:v4"}, "not an enabled external provider")):
            with self.subTest(overrides=overrides), self.assertRaisesRegex(AgentBoxError, message):
                launch_options.apply(config, overrides)

    def test_no_browser_keeps_the_mcp_server_out_without_changing_the_image(self):
        """The session opts out; the build and drift check still see the group."""
        config = Config()
        config.box.tools.groups["browser"] = True
        result = launch_options.apply(config, {"browser": False})
        self.assertFalse(result.box.tools.browser())
        self.assertIn("browser", result.box.tools.enabled_groups())

    def test_describe_spells_overrides_as_the_flags_that_set_them(self):
        self.assertEqual(launch_options.describe({"worker": "codex:gpt-5.5:high", "push": False, "gh-token": True,
                                                  "rounds": 4, "notify": False, "tiers": True}),
                         "--worker codex:gpt-5.5:high --no-push --gh-token --rounds 4 --no-notify --tiers")


class PinnedRouteTests(unittest.TestCase):
    def pinned(self, **overrides):
        return launch_options.apply(workflow_config(), overrides).workflow

    def test_pinned_reviewer_ignores_the_tier(self):
        """--reviewer runs as written, even where a tier list would apply."""
        for tier in (None, 2):
            workflow = self.pinned(reviewer="claude:opus:high", tiers=True)
            workflow.review.tiers["2"] = [model_entry({"provider": "codex", "model": "gpt-5.5"})]
            with self.subTest(tier=tier):
                route = select(workflow, "reviewer", tier, {}, available=PROVIDERS)
                self.assertEqual((route["provider"], route["model"], route["effort"], route["fallbacks"]),
                                 ("claude", "opus", "high", []))

    def test_pinned_worker_and_scout_replace_the_configured_lists(self):
        workflow = self.pinned(worker="codex:gpt-5.5", scout="claude:haiku:low")
        worker = select(workflow, "worker", 2, available=PROVIDERS)
        scout = select(workflow, "scout", None, available=PROVIDERS)
        self.assertEqual((worker["provider"], worker["model"], worker["effort"], worker["fallbacks"]),
                         ("codex", "gpt-5.5", "default", []))
        self.assertEqual((scout["provider"], scout["model"], scout["effort"]), ("claude", "haiku", "low"))

    def test_a_pin_replaces_session_inheritance_and_is_never_saved(self):
        """Untiered work would otherwise inherit the active session and ignore the flag."""
        workflow = self.pinned(reviewer="codex:gpt-5.5:high")
        self.assertEqual(workflow_config().workflow.models_for("reviewer", None), ("workflow.review.models", []))
        self.assertEqual(workflow.models_for("reviewer", None)[0], "--reviewer")
        self.assertNotIn("pins", workflow.to_dict())
        self.assertNotIn("pins", project_config.PROJECT_FIELDS["workflow"])

    def test_a_pinned_provider_without_its_cli_is_an_error_not_a_substitute(self):
        workflow = self.pinned(reviewer="claude:opus")
        with self.assertRaisesRegex(AgentBoxError, "CLI installed"):
            select(workflow, "reviewer", 1, {}, available=["codex"])


class NotifyOffTests(IsolatedLaunchTest):
    def test_no_notify_withholds_credentials_even_where_a_credential_file_exists(self):
        """Host hooks read the credential file, so withholding the forwarded values is not enough."""
        path = self.root / "notify.json"
        path.write_text(json.dumps({"token": "t", "chat_id": "c"}))
        self.assertEqual(notify.load_credentials(path), ("t", "c"))
        with patch.dict(os.environ, {notify.NOTIFY_OFF_ENV: "1"}):
            self.assertIsNone(notify.load_credentials(path))

    def test_host_launch_sets_the_off_switch_for_its_hooks(self):
        config = launch_options.apply(Config(), {"notify": False})
        with patch.object(runtime.shutil, "which", return_value="/usr/bin/codex"), \
             patch.object(runtime, "resolve_workspace", return_value=Workspace(self.root, self.root)), \
             patch.object(runtime, "launch_config", return_value=config), \
             patch.object(runtime, "tmux_enabled", return_value=False), \
             patch.object(runtime.subprocess, "call", return_value=0) as call:
            self.assertEqual(runtime.host_main("codex", []), 0)
        self.assertEqual(call.call_args.kwargs["env"][notify.NOTIFY_OFF_ENV], "1")


if __name__ == "__main__":
    unittest.main()
