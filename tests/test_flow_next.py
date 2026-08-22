import tempfile
import unittest
from pathlib import Path

from agent_box.config import Config
from agent_box.flow_next import (
    AUTO_PUSH_ENV,
    REVIEW_BACKEND_ENV,
    flow_next_launch_env,
    is_flow_next_workspace,
)


class FlowNextDetectionTests(unittest.TestCase):
    def test_present_flow_config_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".flow").mkdir()
            (root / ".flow" / "config.json").write_text("{}")
            self.assertTrue(is_flow_next_workspace(root))

    def test_absent_flow_config_is_not_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(is_flow_next_workspace(Path(tmp)))

    def test_detection_does_not_mutate_the_repository(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            before = sorted(root.iterdir())
            is_flow_next_workspace(root)
            self.assertEqual(sorted(root.iterdir()), before)

    def test_flow_directory_without_config_file_is_not_flow_next(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".flow").mkdir()
            self.assertFalse(is_flow_next_workspace(root))


class ReviewEnvRoutingTests(unittest.TestCase):
    def _flow_next_root(self, tmp: str) -> Path:
        root = Path(tmp)
        (root / ".flow").mkdir()
        (root / ".flow" / "config.json").write_text("{}")
        return root

    def test_flow_next_claude_gets_expected_backend(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._flow_next_root(tmp)
            env = flow_next_launch_env(root, "claude", Config())
        self.assertIn(f"{REVIEW_BACKEND_ENV}=codex:gpt-5.6-sol:medium", env)

    def test_flow_next_codex_gets_expected_backend(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._flow_next_root(tmp)
            env = flow_next_launch_env(root, "codex", Config())
        self.assertIn(f"{REVIEW_BACKEND_ENV}=codex:gpt-5.6-sol:medium", env)

    def test_non_flow_next_workspace_gets_no_injected_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = flow_next_launch_env(Path(tmp), "claude", Config())
        self.assertEqual(env, [])

    def test_non_claude_codex_agent_gets_no_injected_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._flow_next_root(tmp)
            self.assertEqual(flow_next_launch_env(root, "shell", Config()), [])
            self.assertEqual(flow_next_launch_env(root, "doctor", Config()), [])

    def test_mapping_change_is_reflected_without_code_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._flow_next_root(tmp)
            config = Config()
            config.flow_next.review_backends["claude"] = "claude:future-model:high"
            env = flow_next_launch_env(root, "claude", config)
        self.assertIn(f"{REVIEW_BACKEND_ENV}=claude:future-model:high", env)

    def test_auto_push_env_only_for_claude_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._flow_next_root(tmp)
            config = Config()
            config.flow_next.auto_push = True
            claude_env = flow_next_launch_env(root, "claude", config)
            codex_env = flow_next_launch_env(root, "codex", config)
        self.assertIn(f"{AUTO_PUSH_ENV}=1", claude_env)
        self.assertNotIn(f"{AUTO_PUSH_ENV}=1", codex_env)

    def test_auto_push_env_absent_when_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._flow_next_root(tmp)
            env = flow_next_launch_env(root, "claude", Config())
        self.assertFalse(any(e.startswith(AUTO_PUSH_ENV) for e in env))


if __name__ == "__main__":
    unittest.main()
