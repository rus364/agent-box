import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from agent_box import flow_push
from agent_box.flow_next import AUTO_PUSH_ENV


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _init_repo(root: Path, branch: str = "main") -> None:
    _git(root, "init", "-q", "-b", branch)
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "README.md").write_text("hello\n")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "initial")


def _init_bare_remote(
    remote_dir: Path, repo: Path, default_branch: str = "main", remote_name: str = "origin",
) -> None:
    """A bare remote whose HEAD symref is deliberately `default_branch`,
    independent of whatever `init.defaultBranch` happens to be configured on
    the machine running the tests."""
    remote_dir.mkdir(parents=True, exist_ok=True)
    _git(remote_dir, "init", "-q", "--bare", "-b", default_branch)
    _git(repo, "remote", "add", remote_name, str(remote_dir))


class DoneDetectionTests(unittest.TestCase):
    def _payload(self, command: str, cwd: str = "/tmp", **extra) -> dict:
        # `hook_event_name` is always present on a real PostToolUse payload
        # (Claude Code's common input fields); the default here matches
        # that, rather than an incomplete synthetic shape.
        payload = {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "cwd": cwd,
        }
        payload.update(extra)
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

    def test_printf_flowctl_done_does_not_arm(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo(repo)
            self.assertFalse(
                flow_push.handle_post_tool_use(self._payload("printf 'flowctl done'", str(repo))))

    def test_python_print_flowctl_done_does_not_arm(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo(repo)
            self.assertFalse(flow_push.handle_post_tool_use(
                self._payload('python -c \'print("flowctl done")\'', str(repo))))

    def test_flowctl_done_or_true_does_not_arm(self):
        """The core original bug: `flowctl done ... || true` still reports a
        normal, successful Bash tool call even when flowctl itself failed."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo(repo)
            self.assertFalse(
                flow_push.handle_post_tool_use(self._payload("flowctl done fn-1 || true", str(repo))))
            self.assertIsNone(flow_push.read_marker(repo))

    def test_flowctl_done_semicolon_does_not_arm(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo(repo)
            self.assertFalse(
                flow_push.handle_post_tool_use(self._payload("flowctl done fn-1; echo ok", str(repo))))

    def test_flowctl_done_piped_does_not_arm(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo(repo)
            self.assertFalse(
                flow_push.handle_post_tool_use(self._payload("flowctl done fn-1 | cat", str(repo))))

    def test_flowctl_done_backgrounded_does_not_arm(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo(repo)
            self.assertFalse(
                flow_push.handle_post_tool_use(self._payload("flowctl done fn-1 &", str(repo))))

    def test_flowctl_done_command_substitution_does_not_arm(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo(repo)
            self.assertFalse(flow_push.handle_post_tool_use(
                self._payload("flowctl done $(echo fn-1)", str(repo))))

    def test_run_in_background_tool_input_does_not_arm(self):
        """Claude's own structured background flag, not a shell `&` -- a
        PostToolUse for a still-running background command carries no
        reliable completion signal either way."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo(repo)
            payload = {
                "hook_event_name": "PostToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "flowctl done fn-1", "run_in_background": True},
                "cwd": str(repo),
            }
            self.assertFalse(flow_push.handle_post_tool_use(payload))

    def test_dict_shaped_error_tool_response_does_not_arm(self):
        """Legacy/alternate-shape defensive compatibility: current docs say
        Bash's `PostToolUse` tool_response is a plain string with no
        separate error field, but an explicit failure indicator on a
        dict-shaped response must still be honored if one is ever seen."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo(repo)
            payload = self._payload("flowctl done fn-1", str(repo), tool_response={"is_error": True})
            self.assertFalse(flow_push.handle_post_tool_use(payload))
            self.assertIsNone(flow_push.read_marker(repo))

    def test_dict_shaped_success_false_tool_response_does_not_arm(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo(repo)
            payload = self._payload("flowctl done fn-1", str(repo), tool_response={"success": False})
            self.assertFalse(flow_push.handle_post_tool_use(payload))

    def test_string_tool_response_is_treated_as_success(self):
        """The documented current shape: PostToolUse's tool_response for
        Bash is a plain string (its stdout), e.g. `"PASSED: 342 tests..."`
        per Claude Code's own hooks reference example -- this is not a
        failure signal, and a PostToolUse firing at all already means the
        command succeeded (a nonzero exit produces PostToolUseFailure
        instead, which this hook never receives)."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo(repo)
            payload = self._payload("flowctl done fn-1", str(repo), tool_response="fn-1 marked done\n")
            self.assertTrue(flow_push.handle_post_tool_use(payload))

    def test_non_bash_tool_does_not_arm(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo(repo)
            payload = {
                "hook_event_name": "PostToolUse", "tool_name": "Write",
                "tool_input": {"command": "flowctl done fn-1"}, "cwd": str(repo),
            }
            self.assertFalse(flow_push.handle_post_tool_use(payload))

    def test_post_tool_use_failure_event_does_not_arm(self):
        """The documented current contract: a failing Bash command produces
        `PostToolUseFailure` (with an `error` field like "Exit code 1:
        ..."), not `PostToolUse` -- and this hook is registered only under
        the `PostToolUse` event key, so it is never even invoked for a real
        failed Bash command. This directly exercises the strict event-name
        check as the last line of defense if it somehow were."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo(repo)
            payload = {
                "hook_event_name": "PostToolUseFailure",
                "tool_name": "Bash",
                "tool_input": {"command": "flowctl done fn-1"},
                "cwd": str(repo),
                "error": "Exit code 1: fn-1 not found\n",
            }
            self.assertFalse(flow_push.handle_post_tool_use(payload))

    def test_missing_hook_event_name_does_not_arm(self):
        """Strict validation: a real PostToolUse payload always carries
        hook_event_name, so an incomplete/synthetic payload without it must
        not be treated as equivalent to a real one."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo(repo)
            payload = {
                "tool_name": "Bash", "tool_input": {"command": "flowctl done fn-1"}, "cwd": str(repo),
            }
            self.assertFalse(flow_push.handle_post_tool_use(payload))


class NestedCwdAndWorktreeTests(unittest.TestCase):
    """PostToolUse/Stop must normalize whatever cwd Claude reports to the
    repository root, exactly like the launcher does, whether Claude was
    started at the repo root, a nested subdirectory, or a linked worktree."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _init_repo(self.repo)
        (self.repo / ".flow").mkdir()
        (self.repo / ".flow" / "config.json").write_text("{}")
        (self.repo / "src").mkdir()
        _git(self.repo, "add", ".flow", "src")
        _git(self.repo, "commit", "--allow-empty", "-q", "-m", "flow-next marker")

    def test_arm_from_nested_directory_records_repo_root_marker(self):
        _git(self.repo, "checkout", "-q", "-b", "feature-nested")
        nested = self.repo / "src"
        armed = flow_push.handle_post_tool_use(
            {"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": "flowctl done fn-1"}, "cwd": str(nested)})
        self.assertTrue(armed)
        # Recorded against the repo root, not the nested directory.
        self.assertIsNotNone(flow_push.read_marker(self.repo))

    def test_stop_from_nested_directory_finds_marker_and_pushes(self):
        remote = self.root / "remote.git"
        _init_bare_remote(remote, self.repo)
        _git(self.repo, "push", "-q", "-u", "origin", "main")
        _git(self.repo, "checkout", "-q", "-b", "feature-nested")
        (self.repo / "src" / "work.txt").write_text("work\n")
        _git(self.repo, "add", "src/work.txt")
        _git(self.repo, "commit", "-q", "-m", "work")
        flow_push.arm_marker(self.repo)
        nested = self.repo / "src"
        import os
        saved = os.environ.get(AUTO_PUSH_ENV)
        os.environ[AUTO_PUSH_ENV] = "1"
        try:
            decision = flow_push.run_stop_hook(json.dumps({"cwd": str(nested)}))
        finally:
            if saved is None:
                os.environ.pop(AUTO_PUSH_ENV, None)
            else:
                os.environ[AUTO_PUSH_ENV] = saved
        self.assertEqual(decision.action, "pushed")

    def test_linked_worktree_arm_and_push(self):
        remote = self.root / "remote.git"
        _init_bare_remote(remote, self.repo)
        _git(self.repo, "push", "-q", "-u", "origin", "main")
        worktree = self.root / "wt"
        _git(self.repo, "worktree", "add", "-q", "-b", "feature-wt", str(worktree))
        self.assertTrue((worktree / ".git").is_file(), "linked worktree's .git must be a file")
        (worktree / "wtfile.txt").write_text("hi\n")
        _git(worktree, "add", "wtfile.txt")
        _git(worktree, "commit", "-q", "-m", "worktree work")
        armed = flow_push.handle_post_tool_use(
            {"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": "flowctl done fn-1"}, "cwd": str(worktree)})
        self.assertTrue(armed)
        decision = flow_push.guarded_push(worktree, auto_push_enabled=True)
        self.assertEqual(decision.action, "pushed")


class RemoteDefaultBranchTests(unittest.TestCase):
    """Fix for the fail-open remote-default-branch bug: an undetermined
    default branch must refuse to push, never assume safety."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()

    def _flow_next_repo_on(self, branch: str, remote_default: str) -> Path:
        _init_repo(self.repo, branch=branch)
        (self.repo / ".flow").mkdir()
        (self.repo / ".flow" / "config.json").write_text("{}")
        _git(self.repo, "add", ".flow")
        _git(self.repo, "commit", "-q", "-m", "flow-next marker")
        remote = self.root / "remote.git"
        _init_bare_remote(remote, self.repo, default_branch=remote_default)
        _git(self.repo, "push", "-q", "-u", "origin", branch)
        return remote

    def test_origin_head_main_blocks_push_on_main(self):
        self._flow_next_repo_on("main", remote_default="main")
        _git(self.repo, "remote", "set-head", "origin", "main")
        flow_push.arm_marker(self.repo)
        decision = flow_push.guarded_push(self.repo, auto_push_enabled=True)
        self.assertEqual(decision.action, "invalidated")

    def test_origin_head_master_blocks_push_on_master(self):
        self._flow_next_repo_on("master", remote_default="master")
        _git(self.repo, "remote", "set-head", "origin", "master")
        flow_push.arm_marker(self.repo)
        decision = flow_push.guarded_push(self.repo, auto_push_enabled=True)
        self.assertEqual(decision.action, "invalidated")

    def test_custom_default_branch_with_cached_symref_blocks_push(self):
        self._flow_next_repo_on("trunk", remote_default="trunk")
        _git(self.repo, "remote", "set-head", "origin", "trunk")
        flow_push.arm_marker(self.repo)
        decision = flow_push.guarded_push(self.repo, auto_push_enabled=True)
        self.assertEqual(decision.action, "invalidated")

    def test_custom_default_branch_without_local_symref_still_blocks_push(self):
        """The exact scenario from the review: remote default is `trunk`,
        current branch is `trunk`, and refs/remotes/origin/HEAD was never
        cached locally (no `git remote set-head`/`clone` involved). The
        bounded `git ls-remote --symref` fallback must still catch this."""
        self._flow_next_repo_on("trunk", remote_default="trunk")
        determined = flow_push._run(
            ["git", "symbolic-ref", "--short", "-q", "refs/remotes/origin/HEAD"], cwd=self.repo,
        )
        self.assertNotEqual(determined.returncode, 0, "test setup must not have a cached origin/HEAD")
        flow_push.arm_marker(self.repo)
        decision = flow_push.guarded_push(self.repo, auto_push_enabled=True)
        self.assertEqual(decision.action, "invalidated")
        self.assertIsNone(flow_push.read_marker(self.repo))
        # Prove it mechanically: trunk must not have moved on the remote.
        remote_sha = subprocess.run(
            ["git", "ls-remote", str(self.root / "remote.git"), "refs/heads/trunk"],
            capture_output=True, text=True, check=True,
        ).stdout.split()[0]
        local_sha = flow_push.head_sha(self.repo)
        pre_push_sha = subprocess.run(
            ["git", "rev-parse", "HEAD~0"], cwd=self.repo, capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertEqual(remote_sha, local_sha)
        self.assertEqual(remote_sha, pre_push_sha)

    def test_feature_branch_with_custom_default_still_pushes(self):
        """The safety fix must not become overly broad: a real feature
        branch alongside a custom `trunk` default must still push."""
        self._flow_next_repo_on("trunk", remote_default="trunk")
        _git(self.repo, "checkout", "-q", "-b", "feature-y")
        (self.repo / "work.txt").write_text("work\n")
        _git(self.repo, "add", "work.txt")
        _git(self.repo, "commit", "-q", "-m", "feature work")
        flow_push.arm_marker(self.repo)
        decision = flow_push.guarded_push(self.repo, auto_push_enabled=True)
        self.assertEqual(decision.action, "pushed")


    def test_unresolvable_remote_head_refuses_to_push(self):
        """An empty bare remote (HEAD unborn, nothing pushed there yet) means
        `git ls-remote --symref` returns nothing -- undetermined, must
        refuse, not "probably fine since it's not main/master"."""
        _init_repo(self.repo, branch="feature-z")
        (self.repo / ".flow").mkdir()
        (self.repo / ".flow" / "config.json").write_text("{}")
        _git(self.repo, "add", ".flow")
        _git(self.repo, "commit", "-q", "-m", "flow-next marker")
        remote = self.root / "remote.git"
        remote.mkdir()
        _git(remote, "init", "-q", "--bare", "-b", "trunk")
        _git(self.repo, "remote", "add", "origin", str(remote))
        # Deliberately never pushed anything -- origin/HEAD stays unborn.
        flow_push.arm_marker(self.repo)
        decision = flow_push.guarded_push(self.repo, auto_push_enabled=True)
        self.assertEqual(decision.action, "skipped")
        self.assertIn("could not be determined", decision.detail)
        self.assertIsNotNone(flow_push.read_marker(self.repo))

    def test_offline_remote_refuses_to_push(self):
        """An unreachable remote path must fail closed, not silently permit
        the push through an unresolvable default-branch guard."""
        _init_repo(self.repo, branch="feature-offline")
        (self.repo / ".flow").mkdir()
        (self.repo / ".flow" / "config.json").write_text("{}")
        _git(self.repo, "add", ".flow")
        _git(self.repo, "commit", "-q", "-m", "flow-next marker")
        _git(self.repo, "remote", "add", "origin", "/nonexistent/path/does-not-exist.git")
        flow_push.arm_marker(self.repo)
        decision = flow_push.guarded_push(self.repo, auto_push_enabled=True)
        self.assertEqual(decision.action, "skipped")
        self.assertIsNotNone(flow_push.read_marker(self.repo))


class StaleCachedOriginHeadTests(unittest.TestCase):
    """The remote default branch decision must be based on a live query of
    `origin` every time, never on a locally cached `refs/remotes/origin/HEAD`
    symref -- that cache can go stale the moment the remote's actual default
    branch changes (e.g. a GitHub repo's default branch setting is changed),
    and this module must never trust it as authoritative."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _init_repo(self.repo, branch="main")
        (self.repo / ".flow").mkdir()
        (self.repo / ".flow" / "config.json").write_text("{}")
        _git(self.repo, "add", ".flow")
        _git(self.repo, "commit", "-q", "-m", "flow-next marker")
        self.remote = self.root / "remote.git"
        _init_bare_remote(self.remote, self.repo, default_branch="main")
        _git(self.repo, "push", "-q", "-u", "origin", "main")
        _git(self.repo, "remote", "set-head", "origin", "main")  # cache: origin/HEAD -> main

    def _change_remote_default(self, new_default: str) -> None:
        """Simulate the remote's default branch changing (e.g. on GitHub)
        without ever refreshing the local cached origin/HEAD symref."""
        subprocess.run(
            ["git", "symbolic-ref", "HEAD", f"refs/heads/{new_default}"],
            cwd=self.remote, check=True,
        )

    def test_remote_still_defaults_to_main_pushes_a_feature_branch(self):
        _git(self.repo, "checkout", "-q", "-b", "feature-z")
        (self.repo / "w.txt").write_text("work\n")
        _git(self.repo, "add", "w.txt")
        _git(self.repo, "commit", "-q", "-m", "work")
        flow_push.arm_marker(self.repo)
        decision = flow_push.guarded_push(self.repo, auto_push_enabled=True)
        self.assertEqual(decision.action, "pushed")

    def test_remote_default_changed_to_trunk_blocks_push_on_trunk_despite_stale_main_cache(self):
        """The exact review scenario: cached origin/HEAD still says `main`,
        but the remote's actual default branch has since become `trunk`,
        and the current branch is that new default. Must not push."""
        # The remote's new default branch must actually exist there.
        _git(self.repo, "branch", "trunk")
        _git(self.repo, "push", "-q", "origin", "trunk")
        self._change_remote_default("trunk")
        cached = flow_push._run(
            ["git", "symbolic-ref", "--short", "-q", "refs/remotes/origin/HEAD"], cwd=self.repo,
        ).stdout.strip()
        self.assertEqual(cached, "origin/main", "the local cache must still (incorrectly) say main")

        _git(self.repo, "checkout", "-q", "trunk")
        remote_sha_before = subprocess.run(
            ["git", "ls-remote", str(self.remote), "refs/heads/trunk"],
            capture_output=True, text=True, check=True,
        ).stdout.split()[0]
        # Advance local trunk so a naive "not a cached default, so it must be
        # a feature branch" reading would have something to push.
        (self.repo / "w.txt").write_text("work\n")
        _git(self.repo, "add", "w.txt")
        _git(self.repo, "commit", "-q", "-m", "would-be push")
        flow_push.arm_marker(self.repo)

        decision = flow_push.guarded_push(self.repo, auto_push_enabled=True)

        self.assertEqual(decision.action, "invalidated")
        self.assertIsNone(flow_push.read_marker(self.repo))
        remote_sha_after = subprocess.run(
            ["git", "ls-remote", str(self.remote), "refs/heads/trunk"],
            capture_output=True, text=True, check=True,
        ).stdout.split()[0]
        self.assertEqual(remote_sha_before, remote_sha_after, "trunk must not have moved on the remote")

    def test_remote_default_reverted_to_main_blocks_push_on_main_despite_stale_trunk_cache(self):
        """The reverse direction: the local cache says `origin/HEAD ->
        trunk`, but the remote's actual default has been changed back to
        `main`, and the current branch is `main`. Must not push."""
        _git(self.repo, "branch", "trunk")
        _git(self.repo, "push", "-q", "origin", "trunk")
        self._change_remote_default("trunk")
        _git(self.repo, "remote", "set-head", "origin", "-a")  # refresh cache to trunk
        cached = flow_push._run(
            ["git", "symbolic-ref", "--short", "-q", "refs/remotes/origin/HEAD"], cwd=self.repo,
        ).stdout.strip()
        self.assertEqual(cached, "origin/trunk")

        self._change_remote_default("main")  # remote reverts; local cache stays stale at trunk
        remote_sha_before = subprocess.run(
            ["git", "ls-remote", str(self.remote), "refs/heads/main"],
            capture_output=True, text=True, check=True,
        ).stdout.split()[0]

        _git(self.repo, "checkout", "-q", "main")
        (self.repo / "w.txt").write_text("work\n")
        _git(self.repo, "add", "w.txt")
        _git(self.repo, "commit", "-q", "-m", "would-be push")
        flow_push.arm_marker(self.repo)

        decision = flow_push.guarded_push(self.repo, auto_push_enabled=True)

        self.assertEqual(decision.action, "invalidated")
        remote_sha_after = subprocess.run(
            ["git", "ls-remote", str(self.remote), "refs/heads/main"],
            capture_output=True, text=True, check=True,
        ).stdout.split()[0]
        self.assertEqual(remote_sha_before, remote_sha_after, "main must not have moved on the remote")



class PushTargetTests(unittest.TestCase):
    """The push always targets `origin/<branch>`; safety decisions must be
    based on that actual target, never an unrelated configured upstream."""

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
        self.origin = self.root / "origin.git"
        _init_bare_remote(self.origin, self.repo, default_branch="main")
        _git(self.repo, "push", "-q", "-u", "origin", "main")

    def _feature_branch(self, name: str = "feature-x") -> None:
        _git(self.repo, "checkout", "-q", "-b", name)
        (self.repo / "work.txt").write_text("work\n")
        _git(self.repo, "add", "work.txt")
        _git(self.repo, "commit", "-q", "-m", "feature work")

    def test_new_branch_no_remote_ref_pushes_with_dash_u(self):
        self._feature_branch()
        flow_push.arm_marker(self.repo)
        decision = flow_push.guarded_push(self.repo, auto_push_enabled=True)
        self.assertEqual(decision.action, "pushed")
        remote_heads = subprocess.run(
            ["git", "ls-remote", "--heads", str(self.origin), "feature-x"],
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertIn("feature-x", remote_heads)

    def test_local_ahead_of_remote_branch_pushes(self):
        self._feature_branch()
        _git(self.repo, "push", "-q", "-u", "origin", "feature-x")
        (self.repo / "work2.txt").write_text("more\n")
        _git(self.repo, "add", "work2.txt")
        _git(self.repo, "commit", "-q", "-m", "more work")
        flow_push.arm_marker(self.repo)
        decision = flow_push.guarded_push(self.repo, auto_push_enabled=True)
        self.assertEqual(decision.action, "pushed")

    def test_local_equals_remote_invalidates_marker(self):
        self._feature_branch()
        _git(self.repo, "push", "-q", "-u", "origin", "feature-x")
        flow_push.arm_marker(self.repo)
        decision = flow_push.guarded_push(self.repo, auto_push_enabled=True)
        self.assertEqual(decision.action, "invalidated")
        self.assertIsNone(flow_push.read_marker(self.repo))

    def test_local_behind_remote_refuses(self):
        self._feature_branch()
        _git(self.repo, "push", "-q", "-u", "origin", "feature-x")
        # Simulate "local behind": reset local back before the pushed commit.
        _git(self.repo, "reset", "-q", "--hard", "HEAD~1")
        flow_push.arm_marker(self.repo)
        decision = flow_push.guarded_push(self.repo, auto_push_enabled=True)
        self.assertEqual(decision.action, "skipped")
        self.assertIsNotNone(flow_push.read_marker(self.repo))

    def test_diverged_remote_refuses_without_repair(self):
        self._feature_branch()
        _git(self.repo, "push", "-q", "-u", "origin", "feature-x")
        (self.repo / "work.txt").write_text("changed locally\n")
        _git(self.repo, "add", "work.txt")
        _git(self.repo, "commit", "-q", "--amend", "-m", "amended")
        flow_push.arm_marker(self.repo)
        decision = flow_push.guarded_push(self.repo, auto_push_enabled=True)
        self.assertEqual(decision.action, "skipped")
        self.assertIn("ancestor", decision.detail)
        self.assertIsNotNone(flow_push.read_marker(self.repo))

    def test_upstream_on_a_different_remote_is_ignored_target_is_origin(self):
        """The exact review scenario: `@{u}` tracks a `fork` remote, but the
        push always goes to `origin`, so the decision must be based on
        origin/feature-x, not fork/feature-x."""
        self._feature_branch()
        fork = self.root / "fork.git"
        _init_bare_remote(fork, self.repo, default_branch="main", remote_name="fork")
        _git(self.repo, "push", "-q", "-u", "fork", "feature-x")  # sets @{u} to fork/feature-x
        # origin has never seen feature-x at all -- a real new-branch push.
        flow_push.arm_marker(self.repo)
        decision = flow_push.guarded_push(self.repo, auto_push_enabled=True)
        self.assertEqual(decision.action, "pushed")
        origin_heads = subprocess.run(
            ["git", "ls-remote", "--heads", str(self.origin), "feature-x"],
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertIn("feature-x", origin_heads)

    def test_upstream_on_fork_ahead_of_origin_still_pushes_to_origin(self):
        """`fork/feature-x` is ahead (has commits origin doesn't), but that
        must not block pushing origin/feature-x when origin itself has
        nothing to conflict with."""
        self._feature_branch()
        fork = self.root / "fork.git"
        _init_bare_remote(fork, self.repo, default_branch="main", remote_name="fork")
        _git(self.repo, "push", "-q", "-u", "fork", "feature-x")
        (self.repo / "fork-only.txt").write_text("only on fork\n")
        _git(self.repo, "add", "fork-only.txt")
        _git(self.repo, "commit", "-q", "-m", "fork-only work")
        _git(self.repo, "push", "-q", "fork", "feature-x")
        flow_push.arm_marker(self.repo)
        decision = flow_push.guarded_push(self.repo, auto_push_enabled=True)
        self.assertEqual(decision.action, "pushed")

    def test_no_configured_upstream_still_resolves_against_origin(self):
        self._feature_branch()
        _git(self.repo, "push", "-q", "-u", "origin", "feature-x")
        _git(self.repo, "branch", "--unset-upstream")
        (self.repo / "more.txt").write_text("more\n")
        _git(self.repo, "add", "more.txt")
        _git(self.repo, "commit", "-q", "-m", "more work")
        flow_push.arm_marker(self.repo)
        decision = flow_push.guarded_push(self.repo, auto_push_enabled=True)
        self.assertEqual(decision.action, "pushed")

    def test_stale_tracking_branch_after_remote_branch_deleted_is_treated_as_new(self):
        self._feature_branch()
        _git(self.repo, "push", "-q", "-u", "origin", "feature-x")
        _git(self.repo, "push", "-q", "origin", "--delete", "feature-x")
        # Local tracking ref is now stale; the actual origin/feature-x is gone.
        flow_push.arm_marker(self.repo)
        decision = flow_push.guarded_push(self.repo, auto_push_enabled=True)
        self.assertEqual(decision.action, "pushed")


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
        _init_bare_remote(self.remote, self.repo, default_branch="main")
        _git(self.repo, "push", "-q", "-u", "origin", "main")

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

    def test_detached_head_means_no_push_and_keeps_marker(self):
        self._feature_branch()
        flow_push.arm_marker(self.repo)
        _git(self.repo, "checkout", "-q", "--detach")
        decision = flow_push.guarded_push(self.repo, auto_push_enabled=True)
        self.assertEqual(decision.action, "skipped")
        # Conservative choice: detached HEAD is often transient (e.g. mid
        # rebase); the marker survives so a later Stop back on the recorded
        # branch can still complete the push. The branch-binding check still
        # prevents it from ever pushing anything unintended in the meantime.
        _git(self.repo, "checkout", "-q", "feature-x")
        self.assertIsNotNone(flow_push.read_marker(self.repo))

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

    def test_dirty_index_means_no_push(self):
        self._feature_branch()
        _git(self.repo, "push", "-q", "-u", "origin", "feature-x")
        flow_push.arm_marker(self.repo)
        (self.repo / "staged.txt").write_text("staged\n")
        _git(self.repo, "add", "staged.txt")
        decision = flow_push.guarded_push(self.repo, auto_push_enabled=True)
        self.assertEqual(decision.action, "skipped")
        self.assertIsNotNone(flow_push.read_marker(self.repo))

    def test_successful_push_removes_marker(self):
        self._feature_branch()
        flow_push.arm_marker(self.repo)
        decision = flow_push.guarded_push(self.repo, auto_push_enabled=True)
        self.assertEqual(decision.action, "pushed")
        self.assertIsNone(flow_push.read_marker(self.repo))

    def test_diverged_branch_is_refused_before_ever_attempting_a_push(self):
        self._feature_branch()
        _git(self.repo, "push", "-q", "-u", "origin", "feature-x")
        (self.repo / "work.txt").write_text("changed locally\n")
        _git(self.repo, "add", "work.txt")
        _git(self.repo, "commit", "-q", "--amend", "-m", "amended")
        flow_push.arm_marker(self.repo)
        # Diverged, so push_preflight itself refuses -- git push never even
        # runs here. See test_real_push_rejection_by_remote_is_retained_and_
        # never_forced below for an actual rejected `git push`.
        decision = flow_push.guarded_push(self.repo, auto_push_enabled=True)
        self.assertEqual(decision.action, "skipped")
        self.assertIsNotNone(flow_push.read_marker(self.repo))

    def test_real_push_rejection_by_remote_is_retained_and_never_forced(self):
        """A real `git push` that reaches the remote and is rejected there
        (not merely refused by our own preflight) -- a rejecting
        `pre-receive` hook on a real bare remote, so this exercises the
        actual final `git push -u origin HEAD` invocation, not a mock."""
        pre_receive = self.remote / "hooks" / "pre-receive"
        pre_receive.write_text("#!/bin/sh\necho 'remote rejects all pushes' >&2\nexit 1\n")
        pre_receive.chmod(pre_receive.stat().st_mode | 0o111)

        self._feature_branch("feature-rejected")
        remote_sha_before = subprocess.run(
            ["git", "ls-remote", "--heads", str(self.remote), "feature-rejected"],
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertEqual(remote_sha_before, "", "the branch must not exist on the remote yet")

        flow_push.arm_marker(self.repo)
        preflight = flow_push.push_preflight(self.repo, auto_push_enabled=True)
        self.assertEqual(preflight.action, "ready", "preflight must succeed so the real push actually runs")

        decision = flow_push.guarded_push(self.repo, auto_push_enabled=True)

        self.assertEqual(decision.action, "failed")
        self.assertIn("reject", decision.detail.lower())
        self.assertIsNotNone(flow_push.read_marker(self.repo), "marker must be kept for a later retry")
        remote_sha_after = subprocess.run(
            ["git", "ls-remote", "--heads", str(self.remote), "feature-rejected"],
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertEqual(remote_sha_after, "", "the rejected push must not have created the branch")

    def test_push_timeout_is_reported_as_failed_and_keeps_marker(self):
        """A controlled timeout around the final push path: the important
        property is that `guarded_push`/`run_stop_hook` never let
        `subprocess.TimeoutExpired` escape."""
        self._feature_branch()
        flow_push.arm_marker(self.repo)
        preflight = flow_push.push_preflight(self.repo, auto_push_enabled=True)
        self.assertEqual(preflight.action, "ready")

        import subprocess as _subprocess
        from unittest import mock
        real_run = flow_push._run

        def fake_run(args, cwd=None, timeout=10):
            if args[:2] == ["git", "push"]:
                raise _subprocess.TimeoutExpired(cmd=args, timeout=timeout)
            return real_run(args, cwd=cwd, timeout=timeout)

        with mock.patch.object(flow_push, "_run", side_effect=fake_run):
            decision = flow_push.guarded_push(self.repo, auto_push_enabled=True)
        self.assertEqual(decision.action, "failed")
        self.assertIn("timed out", decision.detail)
        self.assertIsNotNone(flow_push.read_marker(self.repo))

    def test_stop_hook_survives_an_unexpected_exception_in_the_guard_chain(self):
        """run_stop_hook is the outermost safety net: even a genuinely
        unexpected exception anywhere in the guard chain must not escape
        it or produce a blocking hook decision."""
        self._feature_branch()
        flow_push.arm_marker(self.repo)
        from unittest import mock
        with mock.patch.object(flow_push, "guarded_push", side_effect=RuntimeError("boom")):
            import os
            saved = os.environ.get(AUTO_PUSH_ENV)
            os.environ[AUTO_PUSH_ENV] = "1"
            try:
                decision = flow_push.run_stop_hook(json.dumps({"cwd": str(self.repo)}))
            finally:
                if saved is None:
                    os.environ.pop(AUTO_PUSH_ENV, None)
                else:
                    os.environ[AUTO_PUSH_ENV] = saved
        self.assertEqual(decision.action, "failed")
        self.assertIn("boom", decision.detail)

    def test_no_code_path_constructs_force_flags(self):
        # Checked as a Python string literal (quoted), not a docstring
        # mention -- the docstrings deliberately explain that force pushing
        # never happens, using the same words.
        source = Path(flow_push.__file__).read_text()
        self.assertNotIn('"--force"', source)
        self.assertNotIn("'--force'", source)
        self.assertNotIn('"--force-with-lease"', source)
        self.assertNotIn("'--force-with-lease'", source)
        self.assertNotIn('"--tags"', source)


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

    def test_stop_hook_never_blocks_on_failed_push(self):
        """The Stop hook itself must not create a stop-loop: it always
        returns a decision object and prints to stderr, never raising or
        signalling a blocking exit status."""
        import os
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo(repo)
            (repo / ".flow").mkdir()
            (repo / ".flow" / "config.json").write_text("{}")
            _git(repo, "add", ".flow")
            _git(repo, "commit", "-q", "-m", "flow-next marker")
            _git(repo, "checkout", "-q", "-b", "feature-x")
            _git(repo, "remote", "add", "origin", "/nonexistent/offline-remote.git")
            flow_push.arm_marker(repo)
            os.environ[AUTO_PUSH_ENV] = "1"
            try:
                decision = flow_push.run_stop_hook(json.dumps({"cwd": str(repo)}))
            finally:
                os.environ.pop(AUTO_PUSH_ENV, None)
            self.assertIn(decision.action, ("skipped", "failed"))
            self.assertIsNotNone(flow_push.read_marker(repo))


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

    def test_all_four_notification_auto_push_combinations(self):
        from agent_box import notify
        for notif_on, push_on in ((False, False), (True, False), (False, True), (True, True)):
            with self.subTest(notifications=notif_on, auto_push=push_on), \
                 tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "settings.json"
                    if notif_on:
                        notify.install_claude_notify_hooks(path)
                    if push_on:
                        flow_push.install_flow_push_hooks(path)
                    settings = json.loads(path.read_text()) if path.exists() else {}
                    hooks = settings.get("hooks", {})
                    stop_commands = {
                        h["hooks"][0]["command"] for h in hooks.get("Stop", [])
                    }
                    if notif_on:
                        self.assertIn(notify.NOTIFY_COMMAND, stop_commands)
                    if push_on:
                        self.assertIn(flow_push.STOP_COMMAND, stop_commands)
                        self.assertIn("PostToolUse", hooks)
                    if not push_on:
                        self.assertNotIn("PostToolUse", hooks)

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
