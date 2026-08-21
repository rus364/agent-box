"""Telegram remote-control sessions must not also broadcast to notify_chat_id.

The bug: a Telegram machine-mode session runs the same `claude` binary with the
same mounted `~/.claude/settings.json`, so the `agent-box notify claude` hooks
fired for it exactly as they do for an interactive run. Those hooks post to the
single global `notify_chat_id`.

That is a real audience mismatch, not just noise. A remote-control session is
owner-scoped -- only its `owner_user_id` may see or drive it, and every event it
produces is already relayed to the chat (and forum topic) that owns it. Letting
the legacy hook path fire in addition republished that owner's prompts and turn
completions to whichever chat the *host* configured for notifications, which the
session's owner never chose and may not even be in.

The fix is an env flag set only for machine-mode containers. Local
`agent-box claude` / `agent-box codex` runs are untouched.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_box import notify, runtime
from agent_box.workspace import Workspace

FLAG = f"--env={runtime.SUPPRESS_NOTIFY_ENV}=1"


def argv_for(kind, project):
    workspace = Workspace(Path(project), Path(project))
    if kind == "machine":
        return runtime.machine_podman_argv(workspace, [], Path(project), ["claude"])
    return runtime.interactive_podman_args(workspace, [], Path(project))


class ContainerFlagTests(unittest.TestCase):
    def test_machine_mode_sets_the_suppression_flag(self):
        with tempfile.TemporaryDirectory() as project:
            self.assertIn(FLAG, argv_for("machine", project))

    def test_interactive_mode_does_not_set_it(self):
        with tempfile.TemporaryDirectory() as project:
            argv = argv_for("interactive", project)
        self.assertNotIn(FLAG, argv)
        self.assertFalse([a for a in argv if runtime.SUPPRESS_NOTIFY_ENV in a])

    def test_flag_is_the_only_difference_in_notification_policy(self):
        """Machine mode must not quietly diverge in mounts/env beyond this."""
        with tempfile.TemporaryDirectory() as project:
            workspace = Workspace(Path(project), Path(project))
            machine = runtime.base_podman_args(
                workspace, [], Path(project), include_kvm=False, outbound_notifications=False)
            local = runtime.base_podman_args(
                workspace, [], Path(project), include_kvm=False, outbound_notifications=True)
        self.assertEqual([a for a in machine if a != FLAG], local)


class SuppressionTests(unittest.TestCase):
    def _send_calls(self, env):
        calls = []

        def fake_client(token):
            raise AssertionError("telegram_send must not build a client when suppressed")

        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch("agent_box.notify.TelegramClient", fake_client), \
             mock.patch("agent_box.notify.load_telegram_config",
                        side_effect=lambda *a, **kw: calls.append("config") or None):
            result = notify.telegram_send("hello")
        return result, calls

    def test_suppressed_send_is_a_silent_success(self):
        result, calls = self._send_calls({runtime.SUPPRESS_NOTIFY_ENV: "1"})
        # True, not False: nothing failed. There was simply nothing to send.
        self.assertTrue(result)
        self.assertEqual(calls, [], "the config must not even be read when suppressed")

    def test_unsuppressed_send_still_reads_the_config(self):
        env = {k: v for k, v in os.environ.items() if k != runtime.SUPPRESS_NOTIFY_ENV}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch("agent_box.notify.load_telegram_config", return_value=None):
            self.assertTrue(notify.telegram_send("hello"))
        self.assertFalse(notify.outbound_suppressed())

    def test_blank_flag_does_not_suppress(self):
        with mock.patch.dict(os.environ, {runtime.SUPPRESS_NOTIFY_ENV: "  "}):
            self.assertFalse(notify.outbound_suppressed())

    def _claude_hook(self, env, payload):
        sent = []
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch("agent_box.notify.TelegramClient",
                        side_effect=AssertionError("no API call expected")), \
             mock.patch("agent_box.notify.load_telegram_config",
                        return_value=mock.Mock(configured=True, notify_chat_id="42", token="t")), \
             mock.patch("agent_box.notify.telegram_send",
                        side_effect=lambda text, **kw: sent.append(text) or True), \
             mock.patch("agent_box.notify.sys.stdin.read", return_value=json.dumps(payload)):
            notify.notify_claude()
        return sent

    def test_hook_still_updates_background_state_when_suppressed(self):
        """Suppression is about *sending*, not about losing local bookkeeping."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.dict(os.environ, {runtime.SUPPRESS_NOTIFY_ENV: "1"}):
                notify.update_claude_background_state("s1", [{"id": "t"}], root)
                self.assertTrue(notify.claude_background_active("s1", root))

    def test_permission_prompt_is_not_sent_from_a_machine_mode_container(self):
        payload = {"hook_event_name": "Notification", "notification_type": "permission_prompt",
                   "session_id": "s", "cwd": "/p", "message": "approve?"}
        with mock.patch.dict(os.environ, {runtime.SUPPRESS_NOTIFY_ENV: "1"}), \
             mock.patch("agent_box.notify.load_telegram_config",
                        side_effect=AssertionError("config must not be read")), \
             mock.patch("agent_box.notify.sys.stdin.read", return_value=json.dumps(payload)):
            notify.notify_claude()  # must not raise

    def test_local_run_still_sends_the_permission_prompt(self):
        env = {k: v for k, v in os.environ.items() if k != runtime.SUPPRESS_NOTIFY_ENV}
        payload = {"hook_event_name": "Notification", "notification_type": "permission_prompt",
                   "session_id": "s", "cwd": "/p", "message": "approve?"}
        sent = []
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch("agent_box.notify.telegram_send",
                        side_effect=lambda text, **kw: sent.append(text) or True), \
             mock.patch("agent_box.notify.sys.stdin.read", return_value=json.dumps(payload)):
            notify.notify_claude()
        self.assertEqual(len(sent), 1)
        self.assertIn("Permission required", sent[0])

    def test_codex_turn_complete_is_suppressed_too(self):
        payload = json.dumps({"type": "agent-turn-complete", "cwd": "/p",
                              "last-assistant-message": "done"})
        with mock.patch.dict(os.environ, {runtime.SUPPRESS_NOTIFY_ENV: "1"}), \
             mock.patch("agent_box.notify.load_telegram_config",
                        side_effect=AssertionError("config must not be read")):
            notify.notify_codex(payload)  # must not raise


if __name__ == "__main__":
    unittest.main()
