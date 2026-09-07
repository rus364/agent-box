"""Regression coverage for host integration isolation across module reloads."""
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class RuntimeApplyIsolationTests(unittest.TestCase):
    """Notification tests must never reconcile profiles outside their fixture."""

    def test_owned_codex_profile_survives_notification_tests_in_both_orders(self):
        notification = "test_runtime.ClaudeNotifyHookLifecycleTests"
        reload_test = "test_host_home.PasswdHomeTests.test_host_home_ignores_env_home"
        runner = (
            "import sys, unittest; "
            "sys.path.insert(0, 'tests'); "
            "loader = unittest.defaultTestLoader; "
            "suite = unittest.TestSuite(loader.loadTestsFromName(name) for name in sys.argv[1:]); "
            "result = unittest.TextTestRunner().run(suite); "
            "raise SystemExit(not result.wasSuccessful())"
        )

        for order in ((notification, reload_test), (reload_test, notification)):
            with self.subTest(order=order), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                profile = root / "codex/agents/ab-worker-3.toml"
                profile.parent.mkdir(parents=True)
                profile_bytes = b"managed profile\n"
                profile.write_bytes(profile_bytes)
                marker = profile.with_name(profile.name + ".agent-box")
                marker_bytes = (hashlib.sha256(profile_bytes).hexdigest() + "\n").encode()
                marker.write_bytes(marker_bytes)

                env = dict(os.environ)
                env.update({
                    "CODEX_HOME": str(root / "codex"),
                    "CLAUDE_CONFIG_DIR": str(root / "claude"),
                    "AGENT_BOX_AGENTS_DIR": str(root / "agents"),
                    "AGENT_BOX_STATE_DIR": str(root / "state"),
                    "AGENT_BOX_CONFIG_DIR": str(root / "config"),
                    "AGENT_BOX_WORKFLOW_STATE": str(root / "workflow"),
                })
                env.pop("AGENT_BOX_RUNTIME_CONFIG", None)
                result = subprocess.run(
                    [sys.executable, "-c", runner, *order],
                    cwd=Path(__file__).parent.parent,
                    env=env,
                    text=True,
                    capture_output=True,
                    check=False,
                )

                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertTrue(profile.is_file(), f"profile removed after {order}")
                self.assertTrue(marker.is_file(), f"ownership marker removed after {order}")
                self.assertEqual(profile.read_bytes(), profile_bytes)
                self.assertEqual(marker.read_bytes(), marker_bytes)


if __name__ == "__main__":
    unittest.main()
