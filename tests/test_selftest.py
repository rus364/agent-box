"""The standalone smoke test must not depend on or disclose caller credentials."""
import os
import unittest
from unittest import mock

from agent_box import notify, selftest


class SelftestTests(unittest.TestCase):
    def test_notification_environment_cannot_change_selftest_credentials(self):
        """A shell's notification credentials must not override selftest fixtures."""
        with mock.patch.dict(os.environ, {
            notify.NOTIFY_TOKEN_ENV: "environment-token",
            notify.NOTIFY_CHAT_ID_ENV: "environment-chat",
        }, clear=False):
            self.assertGreater(selftest.run_selftest(), 0)
            self.assertEqual(os.environ[notify.NOTIFY_TOKEN_ENV], "environment-token")
            self.assertEqual(os.environ[notify.NOTIFY_CHAT_ID_ENV], "environment-chat")

    def test_workspace_session_environment_does_not_fail_selftest(self):
        """Inside a box the session's AGENT_BOX_* variables must neither fail checks nor be lost."""
        session = {"AGENT_BOX_RUNTIME_CONFIG": "/nonexistent/config.json",
                   notify.SUPPRESS_NOTIFY_ENV: "1", "AGENT_BOX_PRIMARY_PROVIDER": "codex"}
        with mock.patch.dict(os.environ, session, clear=False):
            self.assertGreater(selftest.run_selftest(), 0)
            for name, value in session.items():
                self.assertEqual(os.environ[name], value)

    def test_failed_credential_check_does_not_disclose_values(self):
        """A bad credential result must identify the check without serializing secrets."""
        token = "test-only-token"
        chat_id = "test-only-chat-id"
        with mock.patch.object(notify, "load_credentials", return_value=(token, chat_id)):
            with self.assertRaises(AssertionError) as caught:
                selftest.run_selftest()

        message = str(caught.exception)
        self.assertIn("credential file round trip", message)
        self.assertNotIn(token, message)
        self.assertNotIn(chat_id, message)
