import json
import os
import stat
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from agent_box import flow_next
from agent_box.config import Config
from agent_box.flow_next import (
    AUTO_PUSH_ENV,
    FLOWCTL_ENV,
    REVIEW_BACKEND_ENV,
    flow_next_launch_env,
    is_flow_next_workspace,
    resolve_flowctl,
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

    def test_auto_push_env_set_for_both_claude_and_codex_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._flow_next_root(tmp)
            config = Config()
            config.flow_next.auto_push = True
            claude_env = flow_next_launch_env(root, "claude", config)
            codex_env = flow_next_launch_env(root, "codex", config)
        self.assertIn(f"{AUTO_PUSH_ENV}=1", claude_env)
        self.assertIn(f"{AUTO_PUSH_ENV}=1", codex_env)

    def test_auto_push_env_absent_when_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._flow_next_root(tmp)
            env = flow_next_launch_env(root, "claude", Config())
        self.assertFalse(any(e.startswith(AUTO_PUSH_ENV) for e in env))

    def test_explicit_host_env_overrides_configured_backend(self):
        """An explicit FLOW_REVIEW_BACKEND in the launching shell's
        environment wins over the configured flow_next.review_backends entry
        for this one launch -- the precedence a one-shot shell override
        depends on."""
        with tempfile.TemporaryDirectory() as tmp:
            root = self._flow_next_root(tmp)
            env = flow_next_launch_env(
                root, "claude", Config(),
                host_env={"FLOW_REVIEW_BACKEND": "codex:gpt-5.6-terra:high"},
            )
        self.assertIn(f"{REVIEW_BACKEND_ENV}=codex:gpt-5.6-terra:high", env)

    def test_explicit_host_env_overrides_for_codex_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._flow_next_root(tmp)
            env = flow_next_launch_env(
                root, "codex", Config(),
                host_env={"FLOW_REVIEW_BACKEND": "codex:gpt-5.6-terra:high"},
            )
        self.assertIn(f"{REVIEW_BACKEND_ENV}=codex:gpt-5.6-terra:high", env)

    def test_no_explicit_host_env_falls_back_to_configured_backend(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._flow_next_root(tmp)
            env = flow_next_launch_env(root, "claude", Config(), host_env={})
        self.assertIn(f"{REVIEW_BACKEND_ENV}=codex:gpt-5.6-sol:medium", env)

    def test_non_flow_next_workspace_ignores_explicit_host_env(self):
        """A non-Flow-Next workspace must not receive an agent-box-generated
        review backend even when the shell happens to export one."""
        with tempfile.TemporaryDirectory() as tmp:
            env = flow_next_launch_env(
                Path(tmp), "claude", Config(),
                host_env={"FLOW_REVIEW_BACKEND": "codex:gpt-5.6-terra:high"},
            )
        self.assertEqual(env, [])

    def test_explicit_host_env_does_not_affect_auto_push(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._flow_next_root(tmp)
            config = Config()
            config.flow_next.auto_push = True
            env = flow_next_launch_env(
                root, "claude", config,
                host_env={"FLOW_REVIEW_BACKEND": "codex:gpt-5.6-terra:high"},
            )
        self.assertIn(f"{REVIEW_BACKEND_ENV}=codex:gpt-5.6-terra:high", env)
        self.assertIn(f"{AUTO_PUSH_ENV}=1", env)


def _make_executable(path: Path, contents: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


class ResolveFlowctlTests(unittest.TestCase):
    """Flowctl resolution is small and deterministic: Claude reads the
    `flow-next` plugin's own install-path record, Codex reads
    `$CODEX_HOME/scripts/flowctl` -- neither is a filesystem search or
    `shutil.which`."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.claude_home = self.root / "claude-home"
        self.codex_home = self.root / "codex-home"
        self._patches = [
            unittest.mock.patch.object(flow_next, "CLAUDE_HOME", self.claude_home),
            unittest.mock.patch.object(flow_next, "CODEX_HOME", self.codex_home),
        ]
        for patch in self._patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _write_installed_plugins(self, plugins: dict) -> None:
        manifest = self.claude_home / "plugins" / "installed_plugins.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps({"version": 2, "plugins": plugins}))

    def test_claude_resolves_from_installed_plugins_install_path(self):
        install_path = self.root / "cache" / "flow-next" / "flow-next" / "4.5.1"
        flowctl = _make_executable(install_path / "scripts" / "flowctl")
        self._write_installed_plugins({
            "flow-next@flow-next": [{"scope": "user", "installPath": str(install_path)}],
        })
        self.assertEqual(resolve_flowctl("claude"), flowctl)

    def test_claude_matches_any_marketplace_name_before_the_at(self):
        install_path = self.root / "cache" / "custom-marketplace" / "flow-next" / "1.0.0"
        flowctl = _make_executable(install_path / "scripts" / "flowctl")
        self._write_installed_plugins({
            "flow-next@custom-marketplace": [{"scope": "user", "installPath": str(install_path)}],
        })
        self.assertEqual(resolve_flowctl("claude"), flowctl)

    def test_claude_missing_manifest_returns_none(self):
        self.assertIsNone(resolve_flowctl("claude"))

    def test_claude_install_path_without_executable_flowctl_returns_none(self):
        install_path = self.root / "cache" / "flow-next" / "flow-next" / "4.5.1"
        (install_path / "scripts").mkdir(parents=True)
        # A flowctl file exists but is not executable.
        (install_path / "scripts" / "flowctl").write_text("#!/bin/sh\n")
        self._write_installed_plugins({
            "flow-next@flow-next": [{"scope": "user", "installPath": str(install_path)}],
        })
        self.assertIsNone(resolve_flowctl("claude"))

    def test_claude_ignores_unrelated_plugins(self):
        install_path = self.root / "cache" / "claude-plugins-official" / "telegram" / "0.0.7"
        _make_executable(install_path / "scripts" / "flowctl")
        self._write_installed_plugins({
            "telegram@claude-plugins-official": [{"scope": "user", "installPath": str(install_path)}],
        })
        self.assertIsNone(resolve_flowctl("claude"))

    def test_claude_malformed_manifest_returns_none(self):
        manifest = self.claude_home / "plugins" / "installed_plugins.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text("not json")
        self.assertIsNone(resolve_flowctl("claude"))

    def test_codex_resolves_from_codex_home_scripts(self):
        flowctl = _make_executable(self.codex_home / "scripts" / "flowctl")
        self.assertEqual(resolve_flowctl("codex"), flowctl)

    def test_codex_missing_flowctl_returns_none(self):
        self.assertIsNone(resolve_flowctl("codex"))

    def test_codex_non_executable_flowctl_returns_none(self):
        path = self.codex_home / "scripts" / "flowctl"
        path.parent.mkdir(parents=True)
        path.write_text("#!/bin/sh\n")
        self.assertIsNone(resolve_flowctl("codex"))

    def test_unsupported_agent_returns_none(self):
        self.assertIsNone(resolve_flowctl("shell"))


class FlowctlLaunchEnvInjectionTests(unittest.TestCase):
    """`AGENT_BOX_FLOWCTL` is forwarded alongside `AGENT_BOX_FLOW_NEXT_AUTO_PUSH`
    only when auto-push is on and a Flowctl executable was actually
    resolved -- never guessed."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.claude_home = self.root / "claude-home"
        self.codex_home = self.root / "codex-home"
        self._patches = [
            unittest.mock.patch.object(flow_next, "CLAUDE_HOME", self.claude_home),
            unittest.mock.patch.object(flow_next, "CODEX_HOME", self.codex_home),
        ]
        for patch in self._patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.workspace_root = self.root / "workspace"
        (self.workspace_root / ".flow").mkdir(parents=True)
        (self.workspace_root / ".flow" / "config.json").write_text("{}")

    def test_flowctl_forwarded_when_resolved_and_auto_push_enabled(self):
        flowctl = _make_executable(self.codex_home / "scripts" / "flowctl")
        config = Config()
        config.flow_next.auto_push = True
        env = flow_next_launch_env(self.workspace_root, "codex", config)
        self.assertIn(f"{FLOWCTL_ENV}={flowctl}", env)

    def test_flowctl_absent_when_unresolved_but_auto_push_still_enabled(self):
        config = Config()
        config.flow_next.auto_push = True
        env = flow_next_launch_env(self.workspace_root, "codex", config)
        self.assertIn(f"{AUTO_PUSH_ENV}=1", env)
        self.assertFalse(any(e.startswith(f"{FLOWCTL_ENV}=") for e in env))

    def test_flowctl_absent_when_auto_push_disabled(self):
        _make_executable(self.codex_home / "scripts" / "flowctl")
        env = flow_next_launch_env(self.workspace_root, "codex", Config())
        self.assertFalse(any(e.startswith(f"{FLOWCTL_ENV}=") for e in env))


if __name__ == "__main__":
    unittest.main()
