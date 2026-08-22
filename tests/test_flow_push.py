import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from agent_box import flow_push
from agent_box.flow_next import AUTO_PUSH_ENV


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _init_repo(root: Path) -> None:
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "README.md").write_text("hello\n")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "initial")


def _init_bare_remote(remote_dir: Path, repo: Path) -> None:
    remote_dir.mkdir(parents=True, exist_ok=True)
    _git(remote_dir, "init", "-q", "--bare")
    _git(repo, "remote", "add", "origin", str(remote_dir))
    _git(repo, "push", "-q", "-u", "origin", "main")


class DoneDetectionTests(unittest.TestCase):
    def _payload(self, command: str, cwd: str = "/tmp", success: bool = True) -> dict:
        payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd}
        if not success:
            payload["tool_response"] = {"is_error": True}
        return payload

    def test_successful_flowctl_done_arms_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo(repo)
            armed = flow_push.handle_post_tool_use(self._payload("flowctl done fn-1", str(repo)))
            self.assertTrue(armed)
            self.assertIsNotNone(flow_push.read_marker(repo))

    def test_absolute_path_flowctl_done_arms_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo(repo)
            armed = flow_push.handle_post_tool_use(
                self._payload("/opt/flow-next/bin/flowctl done fn-1", str(repo)))
            self.assertTrue(armed)

    def test_flowctl_list_does_not_arm(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo(repo)
            self.assertFalse(flow_push.handle_post_tool_use(self._payload("flowctl list", str(repo))))
            self.assertIsNone(flow_push.read_marker(repo))

    def test_flowctl_show_does_not_arm(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo(repo)
            self.assertFalse(flow_push.handle_post_tool_use(self._payload("flowctl show fn-1", str(repo))))

    def test_echo_flowctl_done_does_not_arm(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo(repo)
            self.assertFalse(
                flow_push.handle_post_tool_use(self._payload('echo "flowctl done"', str(repo))))

    def test_failed_bash_result_does_not_arm(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo(repo)
            armed = flow_push.handle_post_tool_use(
                self._payload("flowctl done fn-1", str(repo), success=False))
            self.assertFalse(armed)
            self.assertIsNone(flow_push.read_marker(repo))

    def test_non_bash_tool_does_not_arm(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo(repo)
            payload = {"tool_name": "Write", "tool_input": {"command": "flowctl done fn-1"}, "cwd": str(repo)}
            self.assertFalse(flow_push.handle_post_tool_use(payload))


class PushGuardTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _init_repo(self.repo)
        (self.repo / ".flow").mkdir()
        (self.repo / ".flow" / "config.json").write_text("{}")
        _git(self.repo, "add", ".flow")
        _git(self.repo, "commit", "-q", "-m", "flow-next marker")
        self.remote = self.root / "remote.git"
        _init_bare_remote(self.remote, self.repo)

    def _feature_branch(self, name: str = "feature-x") -> None:
        _git(self.repo, "checkout", "-q", "-b", name)
        (self.repo / "work.txt").write_text("work\n")
        _git(self.repo, "add", "work.txt")
        _git(self.repo, "commit", "-q", "-m", "feature work")

    def test_no_marker_means_no_push(self):
        self._feature_branch()
        decision = flow_push.guarded_push(self.repo, auto_push_enabled=True)
        self.assertEqual(decision.action, "skipped")

    def test_disabled_for_this_launch_means_no_push(self):
        self._feature_branch()
        flow_push.arm_marker(self.repo)
        decision = flow_push.guarded_push(self.repo, auto_push_enabled=False)
        self.assertEqual(decision.action, "skipped")

    def test_detached_head_means_no_push(self):
        self._feature_branch()
        flow_push.arm_marker(self.repo)
        _git(self.repo, "checkout", "-q", "--detach")
        decision = flow_push.guarded_push(self.repo, auto_push_enabled=True)
        self.assertEqual(decision.action, "skipped")

    def test_main_branch_never_pushed(self):
        flow_push.arm_marker(self.repo)
        decision = flow_push.guarded_push(self.repo, auto_push_enabled=True)
        self.assertEqual(decision.action, "invalidated")
        self.assertIsNone(flow_push.read_marker(self.repo))

    def test_master_branch_never_pushed(self):
        _git(self.repo, "branch", "-m", "master")
        flow_push.arm_marker(self.repo)
        decision = flow_push.guarded_push(self.repo, auto_push_enabled=True)
        self.assertEqual(decision.action, "invalidated")

    def test_detected_remote_default_branch_never_pushed(self):
        self._feature_branch("trunk")
        _git(self.repo, "push", "-q", "-u", "origin", "trunk")
        _git(self.repo, "remote", "set-head", "origin", "trunk")
        flow_push.arm_marker(self.repo)
        decision = flow_push.guarded_push(self.repo, auto_push_enabled=True)
        self.assertEqual(decision.action, "invalidated")

    def test_marker_for_another_branch_means_no_push(self):
        self._feature_branch("branch-a")
        flow_push.arm_marker(self.repo)
        _git(self.repo, "checkout", "-q", "main")
        _git(self.repo, "checkout", "-q", "-b", "branch-b")
        decision = flow_push.guarded_push(self.repo, auto_push_enabled=True)
        self.assertEqual(decision.action, "invalidated")
        self.assertIsNone(flow_push.read_marker(self.repo))

    def test_dirty_worktree_means_no_push_but_keeps_marker(self):
        self._feature_branch()
        _git(self.repo, "push", "-q", "-u", "origin", "feature-x")
        flow_push.arm_marker(self.repo)
        (self.repo / "work.txt").write_text("dirty\n")
        decision = flow_push.guarded_push(self.repo, auto_push_enabled=True)
        self.assertEqual(decision.action, "skipped")
        self.assertIsNotNone(flow_push.read_marker(self.repo))

    def test_nothing_ahead_means_no_push_and_clears_marker(self):
        self._feature_branch()
        _git(self.repo, "push", "-q", "-u", "origin", "feature-x")
        flow_push.arm_marker(self.repo)
        decision = flow_push.guarded_push(self.repo, auto_push_enabled=True)
        self.assertEqual(decision.action, "invalidated")
        self.assertIsNone(flow_push.read_marker(self.repo))

    def test_diverged_remote_refuses_without_repair(self):
        self._feature_branch()
        _git(self.repo, "push", "-q", "-u", "origin", "feature-x")
        # Diverge: amend locally without pushing, so upstream is not an ancestor.
        (self.repo / "work.txt").write_text("changed locally\n")
        _git(self.repo, "add", "work.txt")
        _git(self.repo, "commit", "-q", "--amend", "-m", "amended")
        flow_push.arm_marker(self.repo)
        decision = flow_push.guarded_push(self.repo, auto_push_enabled=True)
        self.assertEqual(decision.action, "skipped")
        self.assertIn("diverged", decision.detail)
        # Marker is kept (not force-repaired away); a human can resolve and retry.
        self.assertIsNotNone(flow_push.read_marker(self.repo))

    def test_new_feature_branch_pushes_with_dash_u(self):
        self._feature_branch()
        flow_push.arm_marker(self.repo)
        result = subprocess.run(
            ["git", "log", "--oneline", "-1"], cwd=self.repo, capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0)
        decision = flow_push.guarded_push(self.repo, auto_push_enabled=True)
        self.assertEqual(decision.action, "pushed")
        self.assertIsNone(flow_push.read_marker(self.repo))
        remote_heads = subprocess.run(
            ["git", "ls-remote", "--heads", str(self.remote), "feature-x"],
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertIn("feature-x", remote_heads)

    def test_existing_fast_forwardable_branch_pushes_normally(self):
        self._feature_branch()
        _git(self.repo, "push", "-q", "-u", "origin", "feature-x")
        (self.repo / "work2.txt").write_text("more\n")
        _git(self.repo, "add", "work2.txt")
        _git(self.repo, "commit", "-q", "-m", "more work")
        flow_push.arm_marker(self.repo)
        decision = flow_push.guarded_push(self.repo, auto_push_enabled=True)
        self.assertEqual(decision.action, "pushed")

    def test_no_code_path_constructs_force_flags(self):
        # Checked as a Python string literal (quoted), not a docstring
        # mention -- the docstrings deliberately explain that force pushing
        # never happens, using the same words.
        source = Path(flow_push.__file__).read_text()
        self.assertNotIn('"--force"', source)
        self.assertNotIn("'--force'", source)
        self.assertNotIn('"--force-with-lease"', source)
        self.assertNotIn("'--force-with-lease'", source)


class PushHookEntryPointTests(unittest.TestCase):
    def test_stop_hook_respects_auto_push_env(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo(repo)
            flow_push.arm_marker(repo)
            saved = os.environ.pop(AUTO_PUSH_ENV, None)
            try:
                decision = flow_push.run_stop_hook(json.dumps({"cwd": str(repo)}))
            finally:
                if saved is not None:
                    os.environ[AUTO_PUSH_ENV] = saved
            self.assertEqual(decision.action, "skipped")


class HookInstallationTests(unittest.TestCase):
    def test_install_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            flow_push.install_flow_push_hooks(path)
            first = json.loads(path.read_text())
            flow_push.install_flow_push_hooks(path)
            second = json.loads(path.read_text())
            self.assertEqual(first, second)
            self.assertEqual(len(second["hooks"]["Stop"]), 1)
            self.assertEqual(len(second["hooks"]["PostToolUse"]), 1)

    def test_install_only_touches_post_tool_use_and_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps({"hooks": {"Notification": [{"hooks": []}]}}))
            flow_push.install_flow_push_hooks(path)
            settings = json.loads(path.read_text())
            self.assertIn("Notification", settings["hooks"])
            self.assertIn("PostToolUse", settings["hooks"])
            self.assertIn("Stop", settings["hooks"])

    def test_never_installs_a_stop_failure_hook(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            flow_push.install_flow_push_hooks(path)
            settings = json.loads(path.read_text())
            self.assertNotIn("StopFailure", settings.get("hooks", {}))

    def test_coexists_with_notification_hooks_under_stop(self):
        from agent_box import notify
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            notify.install_claude_notify_hooks(path)
            flow_push.install_flow_push_hooks(path)
            settings = json.loads(path.read_text())
            stop_hooks = settings["hooks"]["Stop"]
            self.assertEqual(len(stop_hooks), 2)
            commands = {entry["hooks"][0]["command"] for entry in stop_hooks}
            self.assertEqual(commands, {notify.NOTIFY_COMMAND, flow_push.STOP_COMMAND})

    def test_disabling_flow_push_leaves_notification_hooks_intact(self):
        from agent_box import notify
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            notify.install_claude_notify_hooks(path)
            flow_push.install_flow_push_hooks(path)
            flow_push.remove_flow_push_hooks(path)
            settings = json.loads(path.read_text())
            stop_hooks = settings["hooks"]["Stop"]
            self.assertEqual(len(stop_hooks), 1)
            self.assertEqual(stop_hooks[0]["hooks"][0]["command"], notify.NOTIFY_COMMAND)
            self.assertNotIn("PostToolUse", settings["hooks"])

    def test_disabling_notifications_leaves_flow_push_hooks_intact(self):
        from agent_box import notify
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            notify.install_claude_notify_hooks(path)
            flow_push.install_flow_push_hooks(path)
            notify.remove_claude_notify_hooks(path)
            settings = json.loads(path.read_text())
            stop_hooks = settings["hooks"]["Stop"]
            self.assertEqual(len(stop_hooks), 1)
            self.assertEqual(stop_hooks[0]["hooks"][0]["command"], flow_push.STOP_COMMAND)

    def test_works_while_notifications_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            flow_push.install_flow_push_hooks(path)
            settings = json.loads(path.read_text())
            self.assertIn("Stop", settings["hooks"])
            self.assertNotIn("Notification", settings.get("hooks", {}))

    def test_arbitrary_user_hooks_survive_install_and_remove(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            user_hooks = {
                "model": "opus",
                "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
                    {"type": "command", "command": "my-custom-linter"},
                ]}]},
            }
            path.write_text(json.dumps(user_hooks))
            flow_push.install_flow_push_hooks(path)
            flow_push.remove_flow_push_hooks(path)
            settings = json.loads(path.read_text())
            self.assertEqual(settings["model"], "opus")
            self.assertEqual(
                settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"], "my-custom-linter")

    def test_remove_without_install_is_a_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            flow_push.remove_flow_push_hooks(path)  # must not raise
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
