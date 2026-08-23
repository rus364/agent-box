"""agent_box.claude_settings owns hook install/remove at the *inner handler*
level -- a real Claude hook entry can group several handlers under one
outer matcher/group object, and agent-box may only ever touch the handler(s)
it owns, never an entry wholesale merely because one of its handlers happens
to be agent-box's.

Matcher-scoped mixed-entry coverage below is driven through `notify` (its
Notification hooks carry a matcher) rather than `flow_push`, since Flow-Next
auto-push's only managed Claude hook (`Stop`) carries no matcher of its own."""
import json
import tempfile
import unittest
from pathlib import Path

from agent_box import flow_push, notify


class MixedEntryRemovalTests(unittest.TestCase):
    def test_shared_stop_entry_removes_only_owned_handler(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps({"hooks": {"Stop": [{
                "hooks": [
                    {"type": "command", "command": flow_push.STOP_COMMAND},
                    {"type": "command", "command": "my-important-user-hook"},
                ],
            }]}}))
            flow_push.remove_flow_push_hooks(path)
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
                "hooks": [{"type": "command", "command": flow_push.STOP_COMMAND}],
            }]}}))
            flow_push.remove_flow_push_hooks(path)
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
            flow_push.install_flow_push_hooks(path)
            flow_push.install_flow_push_hooks(path)
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
    def test_unrelated_event_and_entries_untouched_by_flow_push(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps({
                "model": "opus",
                "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
                    {"type": "command", "command": "my-custom-linter"},
                ]}]},
            }))
            flow_push.install_flow_push_hooks(path)
            flow_push.remove_flow_push_hooks(path)
            settings = json.loads(path.read_text())
            self.assertEqual(settings["model"], "opus")
            self.assertEqual(
                settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"], "my-custom-linter")


if __name__ == "__main__":
    unittest.main()
