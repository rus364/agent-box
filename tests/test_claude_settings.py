"""agent_box.claude_settings owns hook install/remove at the *inner handler*
level -- a real Claude hook entry can group several handlers under one
outer matcher/group object, and agent-box may only ever touch the handler(s)
it owns, never an entry wholesale merely because one of its handlers happens
to be agent-box's.

Matcher-scoped mixed-entry coverage uses notification hooks with distinct matchers."""
import json
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from agent_box import claude_settings, notify
from agent_box.errors import AgentBoxError


class MixedEntryRemovalTests(unittest.TestCase):
    def test_apply_retires_only_the_exact_owned_push_handler(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.json"
            path.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [
                {"type": "command", "command": "agent-box flow-next stop"},
                {"type": "command", "command": "my-user-hook"}]}]}}))
            claude_settings.reconcile_claude_settings(path, notifications=False, statusline=None)
            handlers = json.loads(path.read_text())["hooks"]["Stop"][0]["hooks"]
            self.assertEqual(handlers, [{"type": "command", "command": "my-user-hook"}])

    def test_shared_stop_entry_removes_only_owned_handler(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps({"hooks": {"Stop": [{
                "hooks": [
                    {"type": "command", "command": notify.NOTIFY_COMMAND},
                    {"type": "command", "command": "my-important-user-hook"},
                ],
            }]}}))
            notify.remove_claude_notify_hooks(path)
            settings = json.loads(path.read_text())
            stop_entries = settings["hooks"]["Stop"]
            self.assertEqual(len(stop_entries), 1, "the outer entry itself must survive")
            commands = [h["command"] for h in stop_entries[0]["hooks"]]
            self.assertEqual(commands, ["my-important-user-hook"])

    def test_shared_matcher_entry_removes_only_owned_handler(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps({"hooks": {"Notification": [{
                "matcher": "permission_prompt",
                "hooks": [
                    {"type": "command", "command": notify.NOTIFY_COMMAND},
                    {"type": "command", "command": "my-user-handler"},
                ],
            }]}}))
            notify.remove_claude_notify_hooks(path)
            settings = json.loads(path.read_text())
            entries = settings["hooks"]["Notification"]
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["matcher"], "permission_prompt")
            self.assertEqual(
                [h["command"] for h in entries[0]["hooks"]], ["my-user-handler"])

    def test_shared_notification_entry_removes_only_owned_handler(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps({"hooks": {"Notification": [{
                "matcher": "idle_prompt",
                "hooks": [
                    {"type": "command", "command": notify.NOTIFY_COMMAND},
                    {"type": "command", "command": "my-notification-hook"},
                ],
            }]}}))
            notify.remove_claude_notify_hooks(path)
            settings = json.loads(path.read_text())
            entries = settings["hooks"]["Notification"]
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["matcher"], "idle_prompt")
            self.assertEqual(
                [h["command"] for h in entries[0]["hooks"]], ["my-notification-hook"])

    def test_entry_metadata_survives_partial_removal(self):
        """matcher and any other unrelated fields on a surviving entry must
        be preserved exactly, not reconstructed."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps({"hooks": {"Notification": [{
                "matcher": "permission_prompt",
                "timeout": 30,
                "statusMessage": "Running checks...",
                "once": True,
                "hooks": [
                    {"type": "command", "command": notify.NOTIFY_COMMAND},
                    {"type": "command", "command": "my-user-handler"},
                ],
            }]}}))
            notify.remove_claude_notify_hooks(path)
            entry = json.loads(path.read_text())["hooks"]["Notification"][0]
            self.assertEqual(entry["timeout"], 30)
            self.assertEqual(entry["statusMessage"], "Running checks...")
            self.assertEqual(entry["once"], True)

    def test_entry_dropped_entirely_when_every_handler_is_owned(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps({"hooks": {"Stop": [{
                "hooks": [{"type": "command", "command": notify.NOTIFY_COMMAND}],
            }]}}))
            notify.remove_claude_notify_hooks(path)
            settings = json.loads(path.read_text())
            self.assertNotIn("Stop", settings.get("hooks", {}))


class InstallIdempotenceWithMixedEntriesTests(unittest.TestCase):
    def test_reinstall_with_managed_command_already_inside_mixed_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps({"hooks": {"Notification": [{
                "matcher": "permission_prompt",
                "hooks": [
                    {"type": "command", "command": notify.NOTIFY_COMMAND},
                    {"type": "command", "command": "my-user-handler"},
                ],
            }]}}))
            notify.install_claude_notify_hooks(path)
            settings = json.loads(path.read_text())
            entries = [e for e in settings["hooks"]["Notification"] if e["matcher"] == "permission_prompt"]
            self.assertEqual(len(entries), 1, "must not add a second, duplicate outer entry")
            commands = [h["command"] for h in entries[0]["hooks"]]
            self.assertEqual(commands.count(notify.NOTIFY_COMMAND), 1)
            self.assertIn("my-user-handler", commands)

    def test_reinstall_after_normal_install_stays_single_handler(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            notify.install_claude_notify_hooks(path)
            notify.install_claude_notify_hooks(path)
            settings = json.loads(path.read_text())
            self.assertEqual(len(settings["hooks"]["Stop"]), 1)
            self.assertNotIn("PostToolUse", settings["hooks"])

    def test_distinct_matchers_with_the_same_notify_command_both_survive_reinstall(self):
        """Notify installs two different Notification entries (matchers
        permission_prompt / idle_prompt) that intentionally share the same
        command string -- idempotence must be scoped by matcher, or the
        second entry would wrongly look like a duplicate of the first."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            notify.install_claude_notify_hooks(path)
            notify.install_claude_notify_hooks(path)
            settings = json.loads(path.read_text())
            notification_entries = settings["hooks"]["Notification"]
            matchers = sorted(e["matcher"] for e in notification_entries)
            self.assertEqual(matchers, ["idle_prompt", "permission_prompt"])
            self.assertEqual(len(notification_entries), 2)


class ArbitraryUserHooksSurviveTests(unittest.TestCase):
    def test_unrelated_event_and_entries_untouched_by_notify(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps({
                "model": "opus",
                "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
                    {"type": "command", "command": "my-custom-linter"},
                ]}]},
            }))
            notify.install_claude_notify_hooks(path)
            notify.remove_claude_notify_hooks(path)
            settings = json.loads(path.read_text())
            self.assertEqual(settings["model"], "opus")
            self.assertEqual(
                settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"], "my-custom-linter")


class StatuslineReconcileTests(unittest.TestCase):
    OURS = {"type": "command", "command": claude_settings.STATUSLINE_COMMAND}
    THEIRS = {"type": "command", "command": "bash ~/my-prompt.sh"}

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "settings.json"

    def write(self, data: dict) -> None:
        self.path.write_text(json.dumps(data))

    def read(self) -> dict:
        return json.loads(self.path.read_text())

    def test_installs_into_an_empty_slot(self):
        self.write({"model": "opus"})
        self.assertTrue(claude_settings.reconcile_statusline(self.path, True))
        self.assertEqual(self.read(), {"model": "opus", "statusLine": self.OURS})

    def test_creates_the_file_when_there_is_none(self):
        self.assertTrue(claude_settings.reconcile_statusline(self.path, True))
        self.assertEqual(self.read(), {"statusLine": self.OURS})

    def test_an_already_correct_file_is_not_rewritten(self):
        self.write({"statusLine": self.OURS})
        before = self.path.stat().st_mtime_ns
        self.assertFalse(claude_settings.reconcile_statusline(self.path, True))
        self.assertEqual(self.path.stat().st_mtime_ns, before)

    def test_another_tools_status_line_is_never_overwritten(self):
        self.write({"statusLine": self.THEIRS})
        self.assertFalse(claude_settings.reconcile_statusline(self.path, True))
        self.assertEqual(self.read()["statusLine"], self.THEIRS)

    def test_disabling_removes_only_our_own_command(self):
        self.write({"model": "opus", "statusLine": self.OURS})
        self.assertTrue(claude_settings.reconcile_statusline(self.path, False))
        self.assertEqual(self.read(), {"model": "opus"})

    def test_disabling_leaves_another_tools_status_line_in_place(self):
        self.write({"statusLine": self.THEIRS})
        self.assertFalse(claude_settings.reconcile_statusline(self.path, False))
        self.assertEqual(self.read()["statusLine"], self.THEIRS)

    def test_disabling_never_creates_a_missing_file(self):
        self.assertFalse(claude_settings.reconcile_statusline(self.path, False))
        self.assertFalse(self.path.exists())

    def test_an_old_agent_box_command_counts_as_somebody_elses(self):
        # Deliberately not a migration: the previous command string is left
        # exactly where it is rather than being rewritten in place.
        legacy = {"type": "command", "command": "agent-box usage all --statusline"}
        self.write({"statusLine": legacy})
        self.assertFalse(claude_settings.reconcile_statusline(self.path, True))
        self.assertEqual(self.read()["statusLine"], legacy)

    def test_hooks_and_other_settings_survive_both_directions(self):
        settings = {
            "model": "opus",
            "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "mine"}]}]},
        }
        self.write(settings)
        claude_settings.reconcile_statusline(self.path, True)
        claude_settings.reconcile_statusline(self.path, False)
        self.assertEqual(self.read(), settings)

    def test_a_non_object_settings_file_is_an_error(self):
        self.path.write_text("[]")
        with self.assertRaises(AgentBoxError):
            claude_settings.reconcile_statusline(self.path, True)

    def test_hooks_and_status_line_land_in_a_single_write(self):
        self.write({"model": "opus"})
        with mock.patch.object(
            claude_settings, "atomic_write_json", wraps=claude_settings.atomic_write_json
        ) as write_json:
            self.assertTrue(claude_settings.reconcile_claude_settings(
                self.path, notifications=True, statusline=True))
        self.assertEqual(write_json.call_count, 1)
        settings = self.read()
        self.assertEqual(settings["statusLine"], self.OURS)
        self.assertEqual(len(settings["hooks"]["Notification"]), 2)

    def test_the_combined_reconciler_can_take_both_back_out(self):
        self.write({"model": "opus"})
        claude_settings.reconcile_claude_settings(
            self.path, notifications=True, statusline=True)
        with mock.patch.object(
            claude_settings, "atomic_write_json", wraps=claude_settings.atomic_write_json
        ) as write_json:
            self.assertTrue(claude_settings.reconcile_claude_settings(
                self.path, notifications=False, statusline=False))
        self.assertEqual(write_json.call_count, 1)
        self.assertEqual(self.read(), {"model": "opus"})

    def test_an_already_correct_file_is_left_untouched_by_the_combined_pass(self):
        self.write({"model": "opus"})
        claude_settings.reconcile_claude_settings(
            self.path, notifications=True, statusline=True)
        with mock.patch.object(claude_settings, "atomic_write_json") as write_json:
            self.assertFalse(claude_settings.reconcile_claude_settings(
                self.path, notifications=True, statusline=True))
        write_json.assert_not_called()


class StatuslineOverrideTests(unittest.TestCase):
    """``AGENT_BOX_STATUSLINE=0`` means "leave it alone", not "remove it".

    settings.json belongs to the user and is read by Claude sessions that
    never go through agent-box, so a per-run override that deleted the line
    would make a one-shot variable a lasting change, with nothing to put it
    back.
    """

    def test_unset_leaves_the_configured_intent_untouched(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(claude_settings.STATUSLINE_ENV, None)
            self.assertIs(claude_settings.statusline_intent(True), True)
            self.assertIs(claude_settings.statusline_intent(False), False)

    def test_zero_means_do_not_touch_it(self):
        with mock.patch.dict(os.environ, {claude_settings.STATUSLINE_ENV: "0"}):
            self.assertIsNone(claude_settings.statusline_intent(True))
            self.assertIsNone(claude_settings.statusline_intent(False))

    def test_any_other_value_is_not_an_override(self):
        for value in ("1", "yes", ""):
            with self.subTest(value=value):
                with mock.patch.dict(os.environ, {claude_settings.STATUSLINE_ENV: value}):
                    self.assertIs(claude_settings.statusline_intent(True), True)

    def test_an_installed_line_survives_the_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            claude_settings.reconcile_statusline(path, True)
            with mock.patch.dict(os.environ, {claude_settings.STATUSLINE_ENV: "0"}):
                claude_settings.reconcile_claude_settings(
                    path, notifications=False,
                    statusline=claude_settings.statusline_intent(True))
            settings = json.loads(path.read_text())
            self.assertTrue(claude_settings.statusline_is_ours(settings))

    def test_the_override_creates_no_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            with mock.patch.dict(os.environ, {claude_settings.STATUSLINE_ENV: "0"}):
                claude_settings.reconcile_claude_settings(
                    path, notifications=False,
                    statusline=claude_settings.statusline_intent(True))
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
