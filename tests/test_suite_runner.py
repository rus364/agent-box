"""The suite entrypoint must not inherit writable host integration locations."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from run_suite import ROOT, isolated_environment


class SuiteRunnerTests(unittest.TestCase):
    def test_child_redirects_inherited_paths_and_preserves_failure_status(self):
        """A failing test may write integrations, but only inside the runner fixture."""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            host = directory / "host"
            host.mkdir()
            sentinel = host / "sentinel"
            sentinel.write_text("keep")
            fixture = directory / "test_fixture.py"
            fixture.write_text('''import os
from pathlib import Path
import unittest
from agent_box import paths

class Fixture(unittest.TestCase):
    def test_integration_writes(self):
        self.assertFalse(any(os.isatty(fd) for fd in (0, 1, 2)))
        for name in ("AGENT_BOX_RUNTIME_CONFIG", "AGENT_BOX_SUPPRESS_NOTIFY",
                     "AGENT_BOX_PRIMARY_PROVIDER", "AGENT_BOX_NOTIFY_TOKEN"):
            self.assertNotIn(name, os.environ)
        root = paths.STATE_ROOT.parent
        targets = [paths.CONFIG_FILE, paths.TOOLS_REFRESH_FILE,
                   paths.CLAUDE_HOME / "settings.json", paths.CODEX_HOME / "config.toml",
                   paths.AGENT_SKILLS_DIR / "fixture", paths.NOTIFY_CREDENTIALS,
                   paths.TRACKER_RUNTIME_DIR / "token", paths.WORKFLOW_STATE / "fixture"]
        for target in targets:
            self.assertTrue(target.is_relative_to(root), str(target))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("fixture")
        self.assertNotIn(os.environ["TEST_HOST"], os.environ["PATH"])
        self.fail("intentional failure after isolated writes")
''')
            env = isolated_environment(host)
            # A workspace session's own variables must not reach the tests.
            env.update(TEST_HOST=str(host), AGENT_BOX_RUNTIME_CONFIG=str(sentinel), PATH=str(host),
                       AGENT_BOX_SUPPRESS_NOTIFY="1", AGENT_BOX_PRIMARY_PROVIDER="codex",
                       AGENT_BOX_NOTIFY_TOKEN="session-token")
            result = subprocess.run(
                [sys.executable, str(ROOT / "tests/run_suite.py"), "discover", "-s", str(directory)],
                env=env, capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("intentional failure after isolated writes", result.stdout)
            self.assertEqual(list(host.iterdir()), [sentinel])
            self.assertEqual(sentinel.read_text(), "keep")
