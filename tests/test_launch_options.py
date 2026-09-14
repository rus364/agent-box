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
from agent_box.config import Config, TrackerConnection, effective_config
from agent_box.errors import AgentBoxError
from agent_box.launch_options import LaunchOptions
from agent_box.routing import select
from agent_box.workspace import Workspace

PROVIDERS = ["codex", "claude"]


def workflow_config():
    config = Config()
    config.workflow.enabled = True
    for tiers in (config.workflow.tiers, config.workflow.review.tiers):
        for tier in tiers.values():
            for provider in PROVIDERS:
                tier[provider]["model"] = provider + "-tier"
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
        config.notifications.enabled, config.notifications.telegram_token, config.notifications.telegram_chat_id = True, "t", "c"
        launched, _ = self.launch(config, {"notify": True})
        self.assertTrue(launched.notifications.usable)
        with patch.object(runtime.shutil, "which", return_value="/usr/bin/codex"), \
             patch.object(runtime, "resolve_workspace", return_value=Workspace(self.root, self.root)), \
             patch.object(runtime, "launch_config", return_value=launched), \
             patch.object(runtime, "tmux_enabled", return_value=False), \
             patch.object(runtime.subprocess, "call", return_value=0) as call:
            runtime.host_main("codex", [])
        self.assertNotIn(notify.NOTIFY_OFF_ENV, call.call_args.kwargs["env"])

    def test_config_show_names_the_session_options(self):
        _, snapshot = self.launch(workflow_config(), {"execution": "direct", "push": False})
        with patch.dict(os.environ, {"AGENT_BOX_RUNTIME_CONFIG": str(snapshot)}), \
             patch.object(cli, "_image_note", return_value=""), \
             patch.object(cli, "_statusline_note", return_value=""), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            cli.config_show()
        self.assertIn("Session options: --worker direct --no-push", output.getvalue())

    def test_options_that_need_a_disabled_integration_are_refused(self):
        for config, overrides, message in (
                (Config(), {"review": "always"}, "--review needs workflow"),
                (workflow_config(), {"workflow": False, "execution": "direct"}, "drop --no-workflow"),
                (workflow_config(), {"tracker": "https://tasks.example", "workflow": False}, "contradict --no-workflow"),
                (Config(), {"project": "12"}, "--project needs the tracker"),
                (workflow_config(), {"reviewer": "claude@deepseek:v4"}, "not an enabled external provider")):
            with self.subTest(overrides=overrides), self.assertRaisesRegex(AgentBoxError, message):
                launch_options.apply(config, overrides)

    def test_browser_without_its_image_group_is_refused_instead_of_starting_broken(self):
        with self.assertRaisesRegex(AgentBoxError, "browser image tool group"):
            launch_options.apply(Config(), {"browser": True})
        config = Config()
        config.image.tools["browser"] = True
        self.assertTrue(launch_options.apply(config, {"browser": True}).browser.enabled)

    def test_copies_seeded_at_box_creation_are_refused_for_a_running_box(self):
        """A running box keeps the gh/Git copies it was created with; the flag must not look applied."""
        with patch.object(runtime.shutil, "which", return_value="/usr/bin/podman"), \
             patch.object(runtime, "resolve_workspace", return_value=Workspace(self.root, self.root)), \
             patch.object(runtime, "ensure_state_dirs"), patch.object(runtime, "ensure_refresh_token"), \
             patch.object(runtime, "load_config", return_value=Config()), \
             patch.object(runtime, "image_exists", return_value=True), \
             patch.object(runtime, "tmux_enabled", return_value=False), \
             patch.object(runtime, "claude_channel_args", return_value=([], [])), \
             patch.object(runtime, "inspect_workspace_container", return_value={"State": {"Status": "running"}}), \
             patch.object(runtime, "launch_in_workspace_container", return_value=0) as launch, \
             self.assertRaisesRegex(AgentBoxError, "already running"):
            runtime.runtime_main(["claude"], Path(__file__).resolve().parents[1],
                                 LaunchOptions(overrides={"git-config": True}))
        launch.assert_not_called()

    def test_describe_spells_overrides_as_the_flags_that_set_them(self):
        self.assertEqual(launch_options.describe({"execution": "direct", "push": False, "gh-token": True,
                                                  "rounds": 4, "notify": False}),
                         "--worker direct --no-push --gh-token --rounds 4 --no-notify")


class PinnedRouteTests(unittest.TestCase):
    def pinned(self, **overrides):
        return launch_options.apply(workflow_config(), overrides).workflow

    def test_pinned_reviewer_ignores_writer_relative_routing_and_tier(self):
        """--reviewer claude from a claude session still reviews with claude, as written."""
        for routing in ("cross-provider", "same-provider"):
            for writer in PROVIDERS:
                for tier in (None, 2):
                    workflow = self.pinned(reviewer="claude:opus:high")
                    workflow.review.routing = routing
                    with self.subTest(routing=routing, writer=writer, tier=tier):
                        route = select(workflow, "reviewer", tier, writer, {}, writer=writer, available=PROVIDERS)
                        self.assertEqual((route["provider"], route["model"], route["effort"], route["fallbacks"]),
                                         ("claude", "opus", "high", []))

    def test_pinned_worker_and_scout_escape_current_routing_and_zero_weight(self):
        workflow = self.pinned(worker="codex:gpt-5.5", scout="claude:haiku:low")
        workflow.routing, workflow.weights = "current", {"codex": 0, "claude": 1}
        worker = select(workflow, "worker", None, "claude", {}, available=PROVIDERS)
        scout = select(workflow, "scout", 0, "codex", {}, available=PROVIDERS)
        self.assertEqual((worker["provider"], worker["model"], worker["effort"]), ("codex", "gpt-5.5", "default"))
        self.assertEqual((scout["provider"], scout["model"], scout["effort"]), ("claude", "haiku", "low"))

    def test_a_pin_replaces_session_inheritance_and_is_never_saved(self):
        """Untiered work would otherwise inherit the active session and ignore the flag."""
        workflow = self.pinned(reviewer="codex:gpt-5.5:high")
        self.assertIsNone(workflow_config().workflow.override_for("reviewer", None))
        self.assertIsNotNone(workflow.override_for("reviewer", None))
        self.assertNotIn("pins", workflow.to_dict())
        self.assertNotIn("pins", project_config.PROJECT_FIELDS["workflow"])

    def test_a_pinned_provider_without_its_cli_is_an_error_not_a_substitute(self):
        workflow = self.pinned(reviewer="claude:opus")
        with self.assertRaisesRegex(AgentBoxError, "not installed"):
            select(workflow, "reviewer", 1, "codex", {}, writer="codex", available=["codex"])


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
