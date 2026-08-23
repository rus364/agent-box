import json
import os
import stat
import subprocess
import tempfile
import textwrap
import unittest
import unittest.mock
from pathlib import Path

from agent_box import flow_push
from agent_box.flow_next import AUTO_PUSH_ENV, FLOWCTL_ENV


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


def _write_fake_flowctl(path: Path, statuses: dict[str, str | dict[str, object]]) -> Path:
    """A tiny fake `flowctl` executable, standing in for realistic Flow-Next
    `show <id> --json` output.

    Each value in `statuses` is either:

    * a plain string -- shorthand for the ordinary, fully-authoritative
      case: `{"id", "status": <value>, "status_source": "flow-state"}`; or
    * a dict -- used verbatim (merged with `"id"`) as the full JSON payload,
      so a test can represent any real or degenerate shape: a `"committed"`
      `status_source`, a `"done"` status with `status_source` omitted
      entirely, or any other field combination Flow-Next might emit.

    An id not present in `statuses` at all answers `status: "todo"` (an
    ordinary incomplete task, not an error). Anything other than `show <id>
    --json` (a different subcommand, missing `--json`) exits non-zero with
    no stdout, exactly like a real CLI misuse would.
    """
    script = textwrap.dedent(f"""\
        #!/usr/bin/env python3
        import json, sys
        statuses = {statuses!r}
        args = sys.argv[1:]
        if len(args) == 3 and args[0] == "show" and args[2] == "--json":
            task_id = args[1]
            entry = statuses.get(task_id, "todo")
            if isinstance(entry, dict):
                payload = {{"id": task_id, **entry}}
            else:
                payload = {{"id": task_id, "status": entry, "status_source": "flow-state"}}
            print(json.dumps(payload))
            sys.exit(0)
        sys.exit(1)
        """)
    path.write_text(script)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _add_task_file(repo: Path, task_id: str, message: str | None = None) -> None:
    tasks_dir = repo / ".flow" / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    (tasks_dir / f"{task_id}.md").write_text(f"# {task_id}\n")
    _git(repo, "add", ".flow/tasks")
    _git(repo, "commit", "-q", "-m", message or f"complete {task_id}")


class FlowPushTestCase(unittest.TestCase):
    """Common fixture: a Flow-Next repo (`.flow/config.json` committed) with
    a bare `origin` whose default branch is `main`, already pushed."""

    default_branch = "main"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        _init_repo(self.repo, branch=self.default_branch)
        (self.repo / ".flow").mkdir()
        (self.repo / ".flow" / "config.json").write_text("{}")
        _git(self.repo, "add", ".flow")
        _git(self.repo, "commit", "-q", "-m", "flow-next marker")
        self.remote = self.root / "remote.git"
        _init_bare_remote(self.remote, self.repo, default_branch=self.default_branch)
        _git(self.repo, "push", "-q", "-u", "origin", self.default_branch)
        self.flowctl_path = self.root / "flowctl"

    def _feature_branch(self, name: str = "feature-x") -> None:
        _git(self.repo, "checkout", "-q", "-b", name)

    def _fake_flowctl(self, statuses: dict[str, str]) -> Path:
        return _write_fake_flowctl(self.flowctl_path, statuses)

    def _complete_task(self, task_id: str = "fn-1.1", status: str = "done") -> Path:
        """Commit a task receipt for `task_id` and return a Flowctl that
        reports it as `status`."""
        _add_task_file(self.repo, task_id)
        return self._fake_flowctl({task_id: status})

    def push(self, flowctl: Path | None) -> flow_push.PushDecision:
        return flow_push.guarded_push(self.repo, auto_push_enabled=True, flowctl=flowctl)


# -- completion detection -----------------------------------------------------

class CompletionDetectionTests(FlowPushTestCase):
    """Git proves what changed locally; Flow-Next's own CLI proves whether
    the corresponding task is actually done. Neither alone is enough."""

    def test_no_task_files_changed_means_no_push(self):
        self._feature_branch()
        (self.repo / "code.py").write_text("print('hi')\n")
        _git(self.repo, "add", "code.py")
        _git(self.repo, "commit", "-q", "-m", "ordinary work")
        flowctl = self._fake_flowctl({})
        decision = self.push(flowctl)
        self.assertEqual(decision.action, "skipped")
        self.assertIn("no Flow-Next task files changed", decision.detail)

    def test_task_file_changed_but_not_done_means_no_push(self):
        self._feature_branch()
        _add_task_file(self.repo, "fn-1.1")
        flowctl = self._fake_flowctl({"fn-1.1": "in_progress"})
        decision = self.push(flowctl)
        self.assertEqual(decision.action, "skipped")
        self.assertIn("no confirmed Flow-Next task completion", decision.detail)

    def test_task_file_changed_and_confirmed_done_pushes(self):
        self._feature_branch()
        flowctl = self._complete_task("fn-1.1")
        decision = self.push(flowctl)
        self.assertEqual(decision.action, "pushed")
        pushed = subprocess.run(
            ["git", "ls-remote", "--heads", str(self.remote), "feature-x"],
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertIn("feature-x", pushed)

    def test_done_sourced_from_committed_snapshot_does_not_authorize(self):
        """`status_source: "committed"` means the answer only fell back to
        the tracked task snapshot on disk, which Flow-Next documents can be
        arbitrarily stale (e.g. a fresh or diff-scoped checkout that never
        ran Flow-Next's runtime state). A `"done"` status from that source
        must not authorize a push -- only the runtime-state-backed
        `"flow-state"` answer is trusted."""
        self._feature_branch()
        _add_task_file(self.repo, "fn-1.1")
        flowctl = self._fake_flowctl({"fn-1.1": {"status": "done", "status_source": "committed"}})
        decision = self.push(flowctl)
        self.assertEqual(decision.action, "skipped")
        self.assertIn("no confirmed Flow-Next task completion", decision.detail)

    def test_done_with_missing_status_source_does_not_authorize(self):
        """No provenance at all must never be treated as good enough --
        there is no default/fallback that assumes authoritative."""
        self._feature_branch()
        _add_task_file(self.repo, "fn-1.1")
        flowctl = self._fake_flowctl({"fn-1.1": {"status": "done"}})
        decision = self.push(flowctl)
        self.assertEqual(decision.action, "skipped")
        self.assertIn("no confirmed Flow-Next task completion", decision.detail)

    def test_in_progress_with_authoritative_source_does_not_authorize(self):
        self._feature_branch()
        _add_task_file(self.repo, "fn-1.1")
        flowctl = self._fake_flowctl({"fn-1.1": {"status": "in_progress", "status_source": "flow-state"}})
        decision = self.push(flowctl)
        self.assertEqual(decision.action, "skipped")

    def test_done_with_authoritative_flow_state_source_pushes(self):
        self._feature_branch()
        _add_task_file(self.repo, "fn-1.1")
        flowctl = self._fake_flowctl({"fn-1.1": {"status": "done", "status_source": "flow-state"}})
        decision = self.push(flowctl)
        self.assertEqual(decision.action, "pushed")

    def test_push_preflight_end_to_end_refuses_committed_sourced_done_task(self):
        """End-to-end proof at the `push_preflight` level (not just the
        higher-level `guarded_push` wrapper): a changed task file whose
        Flowctl result is `{"status": "done", "status_source": "committed"}`
        must never reach "ready", so the real `git push` subprocess is never
        even attempted."""
        self._feature_branch()
        _add_task_file(self.repo, "fn-1.1")
        flowctl = self._fake_flowctl({"fn-1.1": {"status": "done", "status_source": "committed"}})

        preflight = flow_push.push_preflight(self.repo, True, flowctl)

        self.assertNotEqual(preflight.action, "ready")
        self.assertEqual(preflight.action, "skipped")
        remote_heads = subprocess.run(
            ["git", "ls-remote", "--heads", str(self.remote), "feature-x"],
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertEqual(remote_heads, "", "no push may have been attempted, let alone succeeded")

    def test_previously_completed_task_outside_the_unpushed_range_does_not_authorize(self):
        """A task done in an *already-pushed* commit must not license a later,
        unrelated push -- only a receipt that changed within the currently
        unpushed range counts."""
        self._feature_branch()
        flowctl = self._complete_task("fn-1.1")
        first = self.push(flowctl)
        self.assertEqual(first.action, "pushed")

        # New, unrelated local commit: the already-done task's file does not
        # change again here.
        (self.repo / "more.py").write_text("print('more')\n")
        _git(self.repo, "add", "more.py")
        _git(self.repo, "commit", "-q", "-m", "unrelated follow-up work")

        second = self.push(flowctl)
        self.assertEqual(second.action, "skipped")
        self.assertIn("no Flow-Next task files changed", second.detail)

    def test_multiple_changed_tasks_one_done_is_sufficient(self):
        """A turn can legitimately touch more than one task's receipt (e.g.
        plan-sync neighboring metadata); only one needs to be confirmed done."""
        self._feature_branch()
        _add_task_file(self.repo, "fn-1.1", message="complete fn-1.1")
        _add_task_file(self.repo, "fn-1.2", message="touch fn-1.2 (still open)")
        flowctl = self._fake_flowctl({"fn-1.1": "done", "fn-1.2": "in_progress"})
        decision = self.push(flowctl)
        self.assertEqual(decision.action, "pushed")

    def test_malformed_flowctl_json_means_no_push(self):
        self._feature_branch()
        _add_task_file(self.repo, "fn-1.1")
        self.flowctl_path.write_text("#!/usr/bin/env python3\nprint('not json')\n")
        self.flowctl_path.chmod(self.flowctl_path.stat().st_mode | stat.S_IEXEC)
        decision = self.push(self.flowctl_path)
        self.assertEqual(decision.action, "skipped")
        self.assertIn("no confirmed Flow-Next task completion", decision.detail)

    def test_flowctl_nonzero_exit_means_no_push(self):
        self._feature_branch()
        _add_task_file(self.repo, "fn-1.1")
        self.flowctl_path.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(1)\n")
        self.flowctl_path.chmod(self.flowctl_path.stat().st_mode | stat.S_IEXEC)
        decision = self.push(self.flowctl_path)
        self.assertEqual(decision.action, "skipped")

    def test_unresolved_flowctl_means_no_push(self):
        self._feature_branch()
        _add_task_file(self.repo, "fn-1.1")
        decision = self.push(None)
        self.assertEqual(decision.action, "skipped")
        self.assertIn("Flowctl could not be resolved", decision.detail)

    def test_nonexistent_flowctl_path_means_no_push(self):
        self._feature_branch()
        _add_task_file(self.repo, "fn-1.1")
        decision = self.push(self.root / "does-not-exist")
        self.assertEqual(decision.action, "skipped")

    def test_flowctl_invoked_as_argv_never_shell(self):
        """`task_id` reaches the subprocess as one argv element; a value
        that would be dangerous to a shell must not be treated specially."""
        self._feature_branch()
        task_id = "fn-1.1"
        _add_task_file(self.repo, task_id)
        recorder = self.root / "calls.json"
        script = textwrap.dedent(f"""\
            #!/usr/bin/env python3
            import json, sys
            with open({str(recorder)!r}, "w") as f:
                json.dump(sys.argv[1:], f)
            print(json.dumps({{"id": sys.argv[2], "status": "done", "status_source": "flow-state"}}))
            """)
        self.flowctl_path.write_text(script)
        self.flowctl_path.chmod(self.flowctl_path.stat().st_mode | stat.S_IEXEC)
        decision = self.push(self.flowctl_path)
        self.assertEqual(decision.action, "pushed")
        self.assertEqual(json.loads(recorder.read_text()), ["show", task_id, "--json"])


class PostDoneCommitsTests(FlowPushTestCase):
    """The bug this refactor fixes: Flow-Next legitimately creates further
    commits (a task receipt commit, then a plan-sync commit) after `flowctl
    done` itself runs. The final HEAD -- not the commit `done` ran at -- must
    be what gets pushed, and there must be no requirement that HEAD equal
    that commit."""

    def test_final_head_is_pushed_after_post_done_commits(self):
        self._feature_branch()
        (self.repo / "impl.py").write_text("print('impl')\n")
        _git(self.repo, "add", "impl.py")
        _git(self.repo, "commit", "-q", "-m", "implementation/evidence commit")

        _add_task_file(self.repo, "fn-1.1", message="task receipt commit")

        (self.repo / "spec.txt").write_text("plan-sync\n")
        _git(self.repo, "add", "spec.txt")
        _git(self.repo, "commit", "-q", "-m", "plan-sync commit")

        final_head = flow_push.head_sha(self.repo)
        flowctl = self._fake_flowctl({"fn-1.1": "done"})

        decision = self.push(flowctl)

        self.assertEqual(decision.action, "pushed")
        remote_head = subprocess.run(
            ["git", "ls-remote", str(self.remote), "refs/heads/feature-x"],
            capture_output=True, text=True, check=True,
        ).stdout.split()[0]
        self.assertEqual(remote_head, final_head)


class ChangedFlowTaskIdsTests(FlowPushTestCase):
    def test_derives_id_from_flat_task_path(self):
        self._feature_branch()
        base = flow_push.head_sha(self.repo)
        _add_task_file(self.repo, "fn-9-thing.3")
        head = flow_push.head_sha(self.repo)
        self.assertEqual(flow_push.changed_flow_task_ids(self.repo, base, head), ["fn-9-thing.3"])

    def test_nested_path_under_tasks_is_ignored(self):
        self._feature_branch()
        base = flow_push.head_sha(self.repo)
        nested = self.repo / ".flow" / "tasks" / "sub" / "fn-1.1.md"
        nested.parent.mkdir(parents=True, exist_ok=True)
        nested.write_text("x\n")
        _git(self.repo, "add", ".flow/tasks")
        _git(self.repo, "commit", "-q", "-m", "nested")
        head = flow_push.head_sha(self.repo)
        self.assertEqual(flow_push.changed_flow_task_ids(self.repo, base, head), [])

    def test_non_task_flow_paths_are_ignored(self):
        self._feature_branch()
        base = flow_push.head_sha(self.repo)
        (self.repo / ".flow" / "specs").mkdir(parents=True, exist_ok=True)
        (self.repo / ".flow" / "specs" / "fn-1.md").write_text("x\n")
        _git(self.repo, "add", ".flow/specs")
        _git(self.repo, "commit", "-q", "-m", "spec change")
        head = flow_push.head_sha(self.repo)
        self.assertEqual(flow_push.changed_flow_task_ids(self.repo, base, head), [])


# -- nested cwd / worktree normalization --------------------------------------

class NestedCwdAndWorktreeTests(FlowPushTestCase):
    """Every completion event must normalize whatever cwd an agent reports
    to the repository root, exactly like the launcher does, whether the
    agent was started at the repo root, a nested subdirectory, or a linked
    worktree."""

    def test_stop_from_nested_directory_finds_and_pushes(self):
        (self.repo / "src").mkdir()
        _git(self.repo, "add", "src")
        _git(self.repo, "commit", "--allow-empty", "-q", "-m", "src dir")
        self._feature_branch("feature-nested")
        (self.repo / "src" / "work.txt").write_text("work\n")
        _git(self.repo, "add", "src/work.txt")
        _git(self.repo, "commit", "-q", "-m", "work")
        flowctl = self._complete_task("fn-1.1")
        nested = self.repo / "src"
        with unittest.mock.patch.dict(os.environ, {
            AUTO_PUSH_ENV: "1", FLOWCTL_ENV: str(flowctl),
        }):
            decision = flow_push.run_stop_hook(json.dumps({"cwd": str(nested)}))
        self.assertEqual(decision.action, "pushed")

    def test_linked_worktree_pushes(self):
        worktree = self.root / "wt"
        _git(self.repo, "worktree", "add", "-q", "-b", "feature-wt", str(worktree))
        self.assertTrue((worktree / ".git").is_file(), "linked worktree's .git must be a file")
        _add_task_file(worktree, "fn-2.1")
        flowctl = self._fake_flowctl({"fn-2.1": "done"})
        decision = flow_push.guarded_push(worktree, auto_push_enabled=True, flowctl=flowctl)
        self.assertEqual(decision.action, "pushed")


# -- branch/remote safety (unchanged guard chain) -----------------------------

class RemoteDefaultBranchTests(unittest.TestCase):
    """An undetermined default branch must refuse to push, never assume
    safety."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.flowctl_path = self.root / "flowctl"

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

    def _complete_task(self, task_id: str = "fn-1.1") -> Path:
        _add_task_file(self.repo, task_id)
        return _write_fake_flowctl(self.flowctl_path, {task_id: "done"})

    def test_origin_head_main_blocks_push_on_main(self):
        self._flow_next_repo_on("main", remote_default="main")
        _git(self.repo, "remote", "set-head", "origin", "main")
        flowctl = self._complete_task()
        decision = flow_push.guarded_push(self.repo, True, flowctl)
        self.assertEqual(decision.action, "skipped")

    def test_origin_head_master_blocks_push_on_master(self):
        self._flow_next_repo_on("master", remote_default="master")
        _git(self.repo, "remote", "set-head", "origin", "master")
        flowctl = self._complete_task()
        decision = flow_push.guarded_push(self.repo, True, flowctl)
        self.assertEqual(decision.action, "skipped")

    def test_custom_default_branch_with_cached_symref_blocks_push(self):
        self._flow_next_repo_on("trunk", remote_default="trunk")
        _git(self.repo, "remote", "set-head", "origin", "trunk")
        flowctl = self._complete_task()
        decision = flow_push.guarded_push(self.repo, True, flowctl)
        self.assertEqual(decision.action, "skipped")

    def test_custom_default_branch_without_local_symref_still_blocks_push(self):
        """remote default is `trunk`, current branch is `trunk`, and
        refs/remotes/origin/HEAD was never cached locally. The bounded `git
        ls-remote --symref` fallback must still catch this."""
        self._flow_next_repo_on("trunk", remote_default="trunk")
        determined = flow_push._run(
            ["git", "symbolic-ref", "--short", "-q", "refs/remotes/origin/HEAD"], cwd=self.repo,
        )
        self.assertNotEqual(determined.returncode, 0, "test setup must not have a cached origin/HEAD")
        flowctl = self._complete_task()
        remote_sha_before = subprocess.run(
            ["git", "ls-remote", str(self.root / "remote.git"), "refs/heads/trunk"],
            capture_output=True, text=True, check=True,
        ).stdout.split()[0]

        decision = flow_push.guarded_push(self.repo, True, flowctl)

        self.assertEqual(decision.action, "skipped")
        remote_sha_after = subprocess.run(
            ["git", "ls-remote", str(self.root / "remote.git"), "refs/heads/trunk"],
            capture_output=True, text=True, check=True,
        ).stdout.split()[0]
        self.assertEqual(remote_sha_before, remote_sha_after)

    def test_feature_branch_with_custom_default_still_pushes(self):
        """The safety fix must not become overly broad: a real feature
        branch alongside a custom `trunk` default must still push."""
        self._flow_next_repo_on("trunk", remote_default="trunk")
        _git(self.repo, "checkout", "-q", "-b", "feature-y")
        flowctl = self._complete_task()
        decision = flow_push.guarded_push(self.repo, True, flowctl)
        self.assertEqual(decision.action, "pushed")

    def test_unresolvable_remote_head_refuses_to_push(self):
        """An empty bare remote (HEAD unborn) means `git ls-remote --symref`
        returns nothing -- undetermined, must refuse."""
        _init_repo(self.repo, branch="feature-z")
        (self.repo / ".flow").mkdir()
        (self.repo / ".flow" / "config.json").write_text("{}")
        _git(self.repo, "add", ".flow")
        _git(self.repo, "commit", "-q", "-m", "flow-next marker")
        remote = self.root / "remote.git"
        remote.mkdir()
        _git(remote, "init", "-q", "--bare", "-b", "trunk")
        _git(self.repo, "remote", "add", "origin", str(remote))
        flowctl = self._complete_task()
        decision = flow_push.guarded_push(self.repo, True, flowctl)
        self.assertEqual(decision.action, "skipped")
        self.assertIn("could not be determined", decision.detail)

    def test_offline_remote_refuses_to_push(self):
        _init_repo(self.repo, branch="feature-offline")
        (self.repo / ".flow").mkdir()
        (self.repo / ".flow" / "config.json").write_text("{}")
        _git(self.repo, "add", ".flow")
        _git(self.repo, "commit", "-q", "-m", "flow-next marker")
        _git(self.repo, "remote", "add", "origin", "/nonexistent/path/does-not-exist.git")
        flowctl = self._complete_task()
        decision = flow_push.guarded_push(self.repo, True, flowctl)
        self.assertEqual(decision.action, "skipped")


class StaleCachedOriginHeadTests(unittest.TestCase):
    """The remote default branch decision must be based on a live query of
    `origin` every time, never on a locally cached `refs/remotes/origin/HEAD`
    symref."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.flowctl_path = self.root / "flowctl"
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
        subprocess.run(
            ["git", "symbolic-ref", "HEAD", f"refs/heads/{new_default}"], cwd=self.remote, check=True,
        )

    def _complete_task(self, task_id: str = "fn-1.1") -> Path:
        _add_task_file(self.repo, task_id)
        return _write_fake_flowctl(self.flowctl_path, {task_id: "done"})

    def test_remote_still_defaults_to_main_pushes_a_feature_branch(self):
        _git(self.repo, "checkout", "-q", "-b", "feature-z")
        flowctl = self._complete_task()
        decision = flow_push.guarded_push(self.repo, True, flowctl)
        self.assertEqual(decision.action, "pushed")

    def test_remote_default_changed_to_trunk_blocks_push_on_trunk_despite_stale_main_cache(self):
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
        flowctl = self._complete_task()

        decision = flow_push.guarded_push(self.repo, True, flowctl)

        self.assertEqual(decision.action, "skipped")
        remote_sha_after = subprocess.run(
            ["git", "ls-remote", str(self.remote), "refs/heads/trunk"],
            capture_output=True, text=True, check=True,
        ).stdout.split()[0]
        self.assertEqual(remote_sha_before, remote_sha_after, "trunk must not have moved on the remote")

    def test_remote_default_reverted_to_main_blocks_push_on_main_despite_stale_trunk_cache(self):
        _git(self.repo, "branch", "trunk")
        _git(self.repo, "push", "-q", "origin", "trunk")
        self._change_remote_default("trunk")
        _git(self.repo, "remote", "set-head", "origin", "-a")  # refresh cache to trunk
        self._change_remote_default("main")  # remote reverts; local cache stays stale at trunk
        remote_sha_before = subprocess.run(
            ["git", "ls-remote", str(self.remote), "refs/heads/main"],
            capture_output=True, text=True, check=True,
        ).stdout.split()[0]

        _git(self.repo, "checkout", "-q", "main")
        flowctl = self._complete_task()

        decision = flow_push.guarded_push(self.repo, True, flowctl)

        self.assertEqual(decision.action, "skipped")
        remote_sha_after = subprocess.run(
            ["git", "ls-remote", str(self.remote), "refs/heads/main"],
            capture_output=True, text=True, check=True,
        ).stdout.split()[0]
        self.assertEqual(remote_sha_before, remote_sha_after, "main must not have moved on the remote")


class EffectivePushDestinationTests(unittest.TestCase):
    """`origin` can fetch from one repository and push to a completely
    different one (`remote.origin.pushurl`), including several push URLs at
    once. The safety preflight must inspect exactly the repository `git
    push` will actually reach, never `origin`'s fetch URL, and must refuse
    outright whenever that destination isn't pinned down to exactly one
    repository."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.repo_a = self.root / "repo-a.git"  # fetch URL: wrong repo
        self.repo_b = self.root / "repo-b.git"  # actual push URL
        _git(self.root, "init", "-q", "--bare", "-b", "main", str(self.repo_a))
        _git(self.root, "init", "-q", "--bare", "-b", "trunk", str(self.repo_b))
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.flowctl_path = self.root / "flowctl"
        _init_repo(self.repo, branch="main")
        (self.repo / ".flow").mkdir()
        (self.repo / ".flow" / "config.json").write_text("{}")
        _git(self.repo, "add", ".flow")
        _git(self.repo, "commit", "-q", "-m", "flow-next marker")
        _git(self.repo, "remote", "add", "origin", str(self.repo_a))
        _git(self.repo, "config", "remote.origin.pushurl", str(self.repo_b))
        _git(self.repo, "checkout", "-q", "-b", "trunk")
        _git(self.repo, "push", "-q", "-u", "origin", "trunk")
        _git(self.repo, "checkout", "-q", "main")
        _git(self.repo, "branch", "-D", "trunk")

    def _complete_task(self, task_id: str = "fn-1.1") -> Path:
        _add_task_file(self.repo, task_id)
        return _write_fake_flowctl(self.flowctl_path, {task_id: "done"})

    def test_fetch_url_and_pushurl_differ_blocks_push_on_pushurl_default_branch(self):
        _git(self.repo, "checkout", "-q", "-b", "trunk")
        remote_sha_before = subprocess.run(
            ["git", "ls-remote", str(self.repo_b), "refs/heads/trunk"],
            capture_output=True, text=True, check=True,
        ).stdout.split()[0]
        flowctl = self._complete_task()

        decision = flow_push.guarded_push(self.repo, True, flowctl)

        self.assertEqual(decision.action, "skipped")
        remote_sha_after = subprocess.run(
            ["git", "ls-remote", str(self.repo_b), "refs/heads/trunk"],
            capture_output=True, text=True, check=True,
        ).stdout.split()[0]
        self.assertEqual(remote_sha_before, remote_sha_after, "repo-B/trunk must not have moved")

    def test_feature_branch_pushes_to_the_actual_pushurl_repository(self):
        _git(self.repo, "checkout", "-q", "-b", "feature-q")
        flowctl = self._complete_task()

        decision = flow_push.guarded_push(self.repo, True, flowctl)

        self.assertEqual(decision.action, "pushed")
        pushed = subprocess.run(
            ["git", "ls-remote", "--heads", str(self.repo_b), "feature-q"],
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertIn("feature-q", pushed)
        untouched = subprocess.run(
            ["git", "ls-remote", "--heads", str(self.repo_a), "feature-q"],
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertEqual(untouched, "")

    def test_multiple_push_urls_refuses_and_modifies_nothing(self):
        repo_c = self.root / "repo-c.git"
        _git(self.root, "init", "-q", "--bare", "-b", "main", str(repo_c))
        _git(self.repo, "remote", "set-url", "--push", "--add", "origin", str(repo_c))
        urls = subprocess.run(
            ["git", "remote", "get-url", "--push", "--all", "origin"],
            cwd=self.repo, capture_output=True, text=True, check=True,
        ).stdout.splitlines()
        self.assertEqual(len(urls), 2, "test setup must actually configure two push URLs")

        _git(self.repo, "checkout", "-q", "-b", "feature-multi")
        flowctl = self._complete_task()

        decision = flow_push.guarded_push(self.repo, True, flowctl)

        self.assertEqual(decision.action, "skipped")
        for repo in (self.repo_a, self.repo_b, repo_c):
            result = subprocess.run(
                ["git", "ls-remote", "--heads", str(repo), "feature-multi"],
                capture_output=True, text=True, check=True,
            ).stdout
            self.assertEqual(result, "", f"{repo} must not have received the push")

    def test_no_remote_configured_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo(repo, branch="feature-no-remote")
            (repo / ".flow").mkdir()
            (repo / ".flow" / "config.json").write_text("{}")
            _git(repo, "add", ".flow")
            _git(repo, "commit", "-q", "-m", "flow-next marker")
            _add_task_file(repo, "fn-1.1")
            flowctl = _write_fake_flowctl(Path(tmp) / "flowctl", {"fn-1.1": "done"})
            decision = flow_push.guarded_push(repo, True, flowctl)
            self.assertEqual(decision.action, "skipped")

    def test_push_url_rewriting_is_resolved_through_git_not_guessed(self):
        real_remote = self.root / "rewrite-target.git"
        _git(self.root, "init", "-q", "--bare", "-b", "main", str(real_remote))
        repo = self.root / "rewrite-repo"
        repo.mkdir()
        _init_repo(repo, branch="main")
        (repo / ".flow").mkdir()
        (repo / ".flow" / "config.json").write_text("{}")
        _git(repo, "add", ".flow")
        _git(repo, "commit", "-q", "-m", "flow-next marker")

        placeholder = "agent-box-test://placeholder"
        _git(repo, "remote", "add", "origin", placeholder)
        _git(repo, "config", f"url.{real_remote}.pushInsteadOf", placeholder)
        resolved = subprocess.run(
            ["git", "remote", "get-url", "--push", "--all", "origin"],
            cwd=repo, capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertEqual(resolved, str(real_remote), "the rewrite must resolve to the real repository")
        _git(repo, "push", "-q", "-u", "origin", "main")

        _git(repo, "checkout", "-q", "-b", "feature-rewrite")
        _add_task_file(repo, "fn-2.1")
        flowctl = _write_fake_flowctl(self.root / "flowctl2", {"fn-2.1": "done"})

        decision = flow_push.guarded_push(repo, True, flowctl)

        self.assertEqual(decision.action, "pushed")
        pushed = subprocess.run(
            ["git", "ls-remote", "--heads", str(real_remote), "feature-rewrite"],
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertIn("feature-rewrite", pushed)


class PushEffectNeutralizationTests(FlowPushTestCase):
    """Safe argv is not the same as safe effective behavior: `push.
    followTags`/`push.recurseSubmodules` can silently widen a plain `git
    push -u origin HEAD` into pushing unrelated tags or an entirely
    different (submodule) repository."""

    def test_follow_tags_does_not_push_annotated_tag(self):
        self._feature_branch("feature-tags")
        flowctl = self._complete_task("fn-1.1")
        _git(self.repo, "tag", "-a", "v1", "-m", "release v1")
        _git(self.repo, "config", "push.followTags", "true")

        decision = self.push(flowctl)

        self.assertEqual(decision.action, "pushed")
        branch_on_remote = subprocess.run(
            ["git", "ls-remote", "--heads", str(self.remote), "feature-tags"],
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertIn("feature-tags", branch_on_remote)
        tag_on_remote = subprocess.run(
            ["git", "ls-remote", "--tags", str(self.remote), "v1"],
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertEqual(tag_on_remote, "", "the annotated tag must NOT have been pushed")
        self.assertEqual(
            subprocess.run(
                ["git", "config", "--get", "push.followTags"], cwd=self.repo,
                capture_output=True, text=True, check=True,
            ).stdout.strip(),
            "true",
        )

    def test_recurse_submodules_does_not_push_the_submodule_repository(self):
        sub_remote = self.root / "sub-remote.git"
        _git(self.root, "init", "-q", "--bare", "-b", "main", str(sub_remote))
        sub_work = self.root / "sub-work"
        sub_work.mkdir()
        _init_repo(sub_work, branch="main")
        _git(sub_work, "remote", "add", "origin", str(sub_remote))
        _git(sub_work, "push", "-q", "-u", "origin", "main")

        self._feature_branch("feature-submodule")
        subprocess.run(
            ["git", "-c", "protocol.file.allow=always", "submodule", "add", "-q",
             str(sub_remote), "subdir"],
            cwd=self.repo, check=True, capture_output=True, text=True,
        )
        _git(self.repo, "commit", "-q", "-m", "add submodule")

        sub_sha_before = subprocess.run(
            ["git", "ls-remote", "--heads", str(sub_remote), "main"],
            capture_output=True, text=True, check=True,
        ).stdout.split()[0]

        subdir = self.repo / "subdir"
        (subdir / "extra.txt").write_text("submodule change\n")
        _git(subdir, "add", "extra.txt")
        _git(subdir, "commit", "-q", "-m", "submodule change")
        _git(self.repo, "add", "subdir")
        _git(self.repo, "commit", "-q", "-m", "bump submodule pointer")
        _git(self.repo, "config", "push.recurseSubmodules", "on-demand")
        flowctl = self._complete_task("fn-1.1")

        decision = self.push(flowctl)

        self.assertEqual(decision.action, "pushed")
        main_repo_branch = subprocess.run(
            ["git", "ls-remote", "--heads", str(self.remote), "feature-submodule"],
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertIn("feature-submodule", main_repo_branch)
        sub_sha_after = subprocess.run(
            ["git", "ls-remote", "--heads", str(sub_remote), "main"],
            capture_output=True, text=True, check=True,
        ).stdout.split()[0]
        self.assertEqual(
            sub_sha_before, sub_sha_after, "the submodule's own repository must not have been pushed")

    def test_mirror_config_does_not_expand_the_push(self):
        """`remote.origin.mirror=true` combined with an explicit refspec
        makes git fail outright rather than expand the push -- already safe
        by construction, verified here so the invariant doesn't regress."""
        self._feature_branch("feature-mirror")
        flowctl = self._complete_task("fn-1.1")
        _git(self.repo, "config", "remote.origin.mirror", "true")

        decision = self.push(flowctl)

        self.assertEqual(decision.action, "failed")
        branch_on_remote = subprocess.run(
            ["git", "ls-remote", "--heads", str(self.remote), "feature-mirror"],
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertEqual(branch_on_remote, "", "the mirror-config push must not have gone through at all")

    def test_custom_remote_push_refspec_is_overridden_by_explicit_head(self):
        self._feature_branch("feature-refspec")
        flowctl = self._complete_task("fn-1.1")
        _git(self.repo, "config", "remote.origin.push", "refs/heads/feature-refspec:refs/heads/renamed")

        decision = self.push(flowctl)

        self.assertEqual(decision.action, "pushed")
        renamed = subprocess.run(
            ["git", "ls-remote", "--heads", str(self.remote), "renamed"],
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertEqual(renamed, "", "the custom push refspec must not have been honored")
        real_branch = subprocess.run(
            ["git", "ls-remote", "--heads", str(self.remote), "feature-refspec"],
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertIn("feature-refspec", real_branch)


class PushTargetTests(FlowPushTestCase):
    """The push always targets `origin/<branch>`; safety decisions must be
    based on that actual target, never an unrelated configured upstream."""

    def test_new_branch_no_remote_ref_pushes_with_dash_u(self):
        self._feature_branch()
        flowctl = self._complete_task()
        decision = self.push(flowctl)
        self.assertEqual(decision.action, "pushed")
        remote_heads = subprocess.run(
            ["git", "ls-remote", "--heads", str(self.remote), "feature-x"],
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertIn("feature-x", remote_heads)

    def test_local_ahead_of_remote_branch_pushes(self):
        self._feature_branch()
        flowctl = self._complete_task()
        first = self.push(flowctl)
        self.assertEqual(first.action, "pushed")
        _add_task_file(self.repo, "fn-1.2")
        self._fake_flowctl({"fn-1.1": "done", "fn-1.2": "done"})
        decision = self.push(self.flowctl_path)
        self.assertEqual(decision.action, "pushed")

    def test_local_equals_remote_means_nothing_to_push(self):
        self._feature_branch()
        flowctl = self._complete_task()
        first = self.push(flowctl)
        self.assertEqual(first.action, "pushed")
        decision = self.push(flowctl)
        self.assertEqual(decision.action, "skipped")

    def test_local_behind_remote_refuses(self):
        self._feature_branch()
        flowctl = self._complete_task()
        self.assertEqual(self.push(flowctl).action, "pushed")
        _git(self.repo, "reset", "-q", "--hard", "HEAD~1")
        decision = self.push(flowctl)
        self.assertEqual(decision.action, "skipped")

    def test_diverged_remote_refuses_without_repair(self):
        self._feature_branch()
        flowctl = self._complete_task()
        self.assertEqual(self.push(flowctl).action, "pushed")
        (self.repo / "extra.txt").write_text("changed locally\n")
        _git(self.repo, "add", "extra.txt")
        _git(self.repo, "commit", "-q", "--amend", "-m", "amended")
        decision = self.push(flowctl)
        self.assertEqual(decision.action, "skipped")
        self.assertIn("ancestor", decision.detail)

    def test_upstream_on_a_different_remote_is_ignored_target_is_origin(self):
        """`@{u}` may track a `fork` remote, but the push always goes to
        `origin`."""
        self._feature_branch()
        flowctl = self._complete_task()
        fork = self.root / "fork.git"
        _init_bare_remote(fork, self.repo, default_branch="main", remote_name="fork")
        _git(self.repo, "push", "-q", "-u", "fork", "feature-x")  # sets @{u} to fork/feature-x
        decision = self.push(flowctl)
        self.assertEqual(decision.action, "pushed")
        origin_heads = subprocess.run(
            ["git", "ls-remote", "--heads", str(self.remote), "feature-x"],
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertIn("feature-x", origin_heads)

    def test_no_configured_upstream_still_resolves_against_origin(self):
        self._feature_branch()
        flowctl = self._complete_task()
        self.assertEqual(self.push(flowctl).action, "pushed")
        _git(self.repo, "branch", "--unset-upstream")
        _add_task_file(self.repo, "fn-1.2")
        self._fake_flowctl({"fn-1.1": "done", "fn-1.2": "done"})
        decision = self.push(self.flowctl_path)
        self.assertEqual(decision.action, "pushed")

    def test_stale_tracking_branch_after_remote_branch_deleted_is_treated_as_new(self):
        self._feature_branch()
        flowctl = self._complete_task()
        self.assertEqual(self.push(flowctl).action, "pushed")
        _git(self.repo, "push", "-q", "origin", "--delete", "feature-x")
        decision = self.push(flowctl)
        self.assertEqual(decision.action, "pushed")


# -- basic guard chain --------------------------------------------------------

class PushGuardTests(FlowPushTestCase):
    def test_auto_push_disabled_for_this_launch_means_no_push(self):
        self._feature_branch()
        flowctl = self._complete_task()
        decision = flow_push.guarded_push(self.repo, auto_push_enabled=False, flowctl=flowctl)
        self.assertEqual(decision.action, "skipped")

    def test_non_flow_next_workspace_means_no_push(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _init_repo(repo)
            _add_task_file(repo, "fn-1.1")
            flowctl = _write_fake_flowctl(Path(tmp) / "flowctl", {"fn-1.1": "done"})
            decision = flow_push.guarded_push(repo, True, flowctl)
            self.assertEqual(decision.action, "skipped")
            self.assertIn("not a Flow-Next workspace", decision.detail)

    def test_detached_head_means_no_push(self):
        self._feature_branch()
        flowctl = self._complete_task()
        _git(self.repo, "checkout", "-q", "--detach")
        decision = self.push(flowctl)
        self.assertEqual(decision.action, "skipped")

    def test_main_branch_never_pushed(self):
        _add_task_file(self.repo, "fn-1.1")
        flowctl = self._fake_flowctl({"fn-1.1": "done"})
        decision = self.push(flowctl)
        self.assertEqual(decision.action, "skipped")
        self.assertIn("protected branch", decision.detail)

    def test_master_branch_never_pushed(self):
        _git(self.repo, "branch", "-m", "master")
        _add_task_file(self.repo, "fn-1.1")
        flowctl = self._fake_flowctl({"fn-1.1": "done"})
        decision = self.push(flowctl)
        self.assertEqual(decision.action, "skipped")

    def test_dirty_worktree_means_no_push(self):
        self._feature_branch()
        flowctl = self._complete_task()
        (self.repo / "dirty.txt").write_text("dirty\n")
        decision = self.push(flowctl)
        self.assertEqual(decision.action, "skipped")
        self.assertIn("dirty", decision.detail)

    def test_dirty_index_means_no_push(self):
        self._feature_branch()
        flowctl = self._complete_task()
        (self.repo / "staged.txt").write_text("staged\n")
        _git(self.repo, "add", "staged.txt")
        decision = self.push(flowctl)
        self.assertEqual(decision.action, "skipped")

    def test_diverged_branch_is_refused_before_ever_attempting_a_push(self):
        self._feature_branch()
        flowctl = self._complete_task()
        self.assertEqual(self.push(flowctl).action, "pushed")
        (self.repo / "extra.txt").write_text("changed locally\n")
        _git(self.repo, "add", "extra.txt")
        _git(self.repo, "commit", "-q", "--amend", "-m", "amended")
        # push_preflight itself refuses -- git push never even runs here.
        decision = self.push(flowctl)
        self.assertEqual(decision.action, "skipped")

    def test_real_push_rejection_by_remote_is_retained_and_never_forced(self):
        """A real `git push` that reaches the remote and is rejected there
        (a rejecting `pre-receive` hook on a real bare remote) -- exercises
        the actual final `git push -u origin HEAD` invocation, not a mock."""
        pre_receive = self.remote / "hooks" / "pre-receive"
        pre_receive.write_text("#!/bin/sh\necho 'remote rejects all pushes' >&2\nexit 1\n")
        pre_receive.chmod(pre_receive.stat().st_mode | 0o111)

        self._feature_branch("feature-rejected")
        flowctl = self._complete_task()
        preflight = flow_push.push_preflight(self.repo, True, flowctl)
        self.assertEqual(preflight.action, "ready", "preflight must succeed so the real push actually runs")

        decision = self.push(flowctl)

        self.assertEqual(decision.action, "failed")
        self.assertIn("reject", decision.detail.lower())
        remote_sha_after = subprocess.run(
            ["git", "ls-remote", "--heads", str(self.remote), "feature-rejected"],
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertEqual(remote_sha_after, "", "the rejected push must not have created the branch")

    def test_push_timeout_is_reported_as_failed(self):
        self._feature_branch()
        flowctl = self._complete_task()
        preflight = flow_push.push_preflight(self.repo, True, flowctl)
        self.assertEqual(preflight.action, "ready")

        real_run = flow_push._run

        def fake_run(args, cwd=None, timeout=10):
            if "push" in args:
                raise subprocess.TimeoutExpired(cmd=args, timeout=timeout)
            return real_run(args, cwd=cwd, timeout=timeout)

        with unittest.mock.patch.object(flow_push, "_run", side_effect=fake_run):
            decision = self.push(flowctl)
        self.assertEqual(decision.action, "failed")
        self.assertIn("timed out", decision.detail)

    def test_stop_hook_survives_an_unexpected_exception_in_the_guard_chain(self):
        """run_stop_hook is the outermost safety net: even a genuinely
        unexpected exception anywhere in the guard chain must not escape
        it or produce a blocking hook decision."""
        self._feature_branch()
        with unittest.mock.patch.object(flow_push, "guarded_push", side_effect=RuntimeError("boom")), \
             unittest.mock.patch.dict(os.environ, {AUTO_PUSH_ENV: "1"}):
            decision = flow_push.run_stop_hook(json.dumps({"cwd": str(self.repo)}))
        self.assertEqual(decision.action, "failed")
        self.assertIn("boom", decision.detail)

    def test_no_code_path_constructs_force_flags(self):
        source = Path(flow_push.__file__).read_text()
        self.assertNotIn('"--force"', source)
        self.assertNotIn("'--force'", source)
        self.assertNotIn('"--force-with-lease"', source)
        self.assertNotIn("'--force-with-lease'", source)
        self.assertNotIn('"--tags"', source)


# -- provider adapters ---------------------------------------------------------

class ClaudeAdapterTests(FlowPushTestCase):
    def test_stop_hook_respects_auto_push_env(self):
        self._feature_branch()
        flowctl = self._complete_task()
        with unittest.mock.patch.dict(os.environ, {FLOWCTL_ENV: str(flowctl)}, clear=False):
            os.environ.pop(AUTO_PUSH_ENV, None)
            decision = flow_push.run_stop_hook(json.dumps({"cwd": str(self.repo)}))
        self.assertEqual(decision.action, "skipped")

    def test_stop_hook_pushes_when_env_is_set(self):
        self._feature_branch()
        flowctl = self._complete_task()
        with unittest.mock.patch.dict(os.environ, {AUTO_PUSH_ENV: "1", FLOWCTL_ENV: str(flowctl)}):
            decision = flow_push.run_stop_hook(json.dumps({"cwd": str(self.repo)}))
        self.assertEqual(decision.action, "pushed")

    def test_stop_hook_never_blocks_on_failed_push(self):
        self._feature_branch()
        _git(self.repo, "remote", "set-url", "origin", "/nonexistent/offline-remote.git")
        flowctl = self._complete_task()
        with unittest.mock.patch.dict(os.environ, {AUTO_PUSH_ENV: "1", FLOWCTL_ENV: str(flowctl)}):
            decision = flow_push.run_stop_hook(json.dumps({"cwd": str(self.repo)}))
        self.assertIn(decision.action, ("skipped", "failed"))

    def test_malformed_stop_payload_does_not_raise(self):
        decision = flow_push.run_stop_hook("not json")
        self.assertEqual(decision.action, "skipped")


class CodexAdapterTests(FlowPushTestCase):
    def test_non_turn_complete_event_is_a_noop(self):
        self._feature_branch()
        flowctl = self._complete_task()
        with unittest.mock.patch.dict(os.environ, {AUTO_PUSH_ENV: "1", FLOWCTL_ENV: str(flowctl)}):
            result = flow_push.run_codex_turn_complete_hook(
                json.dumps({"type": "something-else", "cwd": str(self.repo)}))
        self.assertIsNone(result)
        # Nothing was pushed: still exactly the two remote commits from setUp.
        remote = subprocess.run(
            ["git", "ls-remote", "--heads", str(self.remote)],
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertNotIn("feature-x", remote)

    def test_turn_complete_event_pushes(self):
        self._feature_branch()
        flowctl = self._complete_task()
        payload = json.dumps({"type": "agent-turn-complete", "cwd": str(self.repo)})
        with unittest.mock.patch.dict(os.environ, {AUTO_PUSH_ENV: "1", FLOWCTL_ENV: str(flowctl)}):
            decision = flow_push.run_codex_turn_complete_hook(payload)
        self.assertEqual(decision.action, "pushed")

    def test_malformed_payload_does_not_raise(self):
        result = flow_push.run_codex_turn_complete_hook("not json")
        self.assertIsNone(result)


class MultiWorkspaceIsolationTests(unittest.TestCase):
    """A completion event for one workspace must never inspect or push
    another. There is no global marker/state -- every reconciliation is
    scoped only by the workspace resolved from its own event `cwd`."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _flow_repo(self, name: str) -> tuple[Path, Path]:
        repo = self.root / name
        repo.mkdir()
        _init_repo(repo, branch="main")
        (repo / ".flow").mkdir()
        (repo / ".flow" / "config.json").write_text("{}")
        _git(repo, "add", ".flow")
        _git(repo, "commit", "-q", "-m", "flow-next marker")
        remote = self.root / f"{name}-remote.git"
        _init_bare_remote(remote, repo, default_branch="main")
        _git(repo, "push", "-q", "-u", "origin", "main")
        return repo, remote

    def test_completion_in_one_workspace_never_pushes_another(self):
        repo_a, remote_a = self._flow_repo("project-a")
        repo_b, remote_b = self._flow_repo("project-b")

        _git(repo_a, "checkout", "-q", "-b", "feature")
        _add_task_file(repo_a, "fn-a.1")
        _git(repo_b, "checkout", "-q", "-b", "feature")
        _add_task_file(repo_b, "fn-b.1")

        # One shared Flowctl only reports project-a's own task as done; a
        # project-b task id it was never told about defaults to "todo".
        flowctl = _write_fake_flowctl(self.root / "flowctl", {"fn-a.1": "done"})

        with unittest.mock.patch.dict(os.environ, {AUTO_PUSH_ENV: "1", FLOWCTL_ENV: str(flowctl)}):
            decision_a = flow_push.run_stop_hook(json.dumps({"cwd": str(repo_a)}))
            decision_b = flow_push.run_stop_hook(json.dumps({"cwd": str(repo_b)}))

        self.assertEqual(decision_a.action, "pushed")
        self.assertEqual(decision_b.action, "skipped")
        pushed_a = subprocess.run(
            ["git", "ls-remote", "--heads", str(remote_a), "feature"],
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertIn("feature", pushed_a)
        pushed_b = subprocess.run(
            ["git", "ls-remote", "--heads", str(remote_b), "feature"],
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertEqual(pushed_b, "", "project-b must never be pushed by project-a's completion")


# -- hook installation (Claude Stop only) -------------------------------------

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
            self.assertNotIn("PostToolUse", second["hooks"])

    def test_install_only_touches_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps({"hooks": {"Notification": [{"hooks": []}]}}))
            flow_push.install_flow_push_hooks(path)
            settings = json.loads(path.read_text())
            self.assertIn("Notification", settings["hooks"])
            self.assertIn("Stop", settings["hooks"])
            self.assertNotIn("PostToolUse", settings["hooks"])

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


# -- inherited Git configuration precedence (unchanged, must not regress) ----

class InheritedGitConfigContextTests(unittest.TestCase):
    """Preflight and the final push must resolve `origin` from *identical*
    effective Git configuration, whatever inherited GIT_CONFIG_* the process
    environment carries."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.flowctl_path = self.root / "flowctl"

    def _flow_repo(self, path: Path, branch: str) -> None:
        path.mkdir(parents=True, exist_ok=True)
        _init_repo(path, branch=branch)
        (path / ".flow").mkdir()
        (path / ".flow" / "config.json").write_text("{}")
        _git(path, "add", ".flow")
        _git(path, "commit", "-q", "-m", "flow-next marker")

    def _complete_task(self, repo: Path, task_id: str = "fn-1.1") -> Path:
        _add_task_file(repo, task_id)
        return _write_fake_flowctl(self.flowctl_path, {task_id: "done"})

    def _inherit(self, **env: str):
        patcher = unittest.mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_inherited_command_scope_pushurl_is_not_dropped_by_the_push(self):
        repo_a = self.root / "repo-A.git"
        repo_b = self.root / "repo-B.git"
        _git(self.root, "init", "-q", "--bare", "-b", "trunk", str(repo_a))
        _git(self.root, "init", "-q", "--bare", "-b", "main", str(repo_b))
        repo = self.root / "work"
        self._flow_repo(repo, branch="trunk")
        _git(repo, "remote", "add", "origin", str(repo_a))
        _git(repo, "push", "-q", "origin", "trunk")
        _git(repo, "push", "-q", str(repo_b), "trunk:main")
        flowctl = self._complete_task(repo)

        a_before = _git(repo_a, "rev-parse", "trunk").stdout.strip()
        head = _git(repo, "rev-parse", "HEAD").stdout.strip()
        self.assertNotEqual(a_before, head)

        self._inherit(
            GIT_CONFIG_COUNT="1",
            GIT_CONFIG_KEY_0="remote.origin.pushurl",
            GIT_CONFIG_VALUE_0=str(repo_b),
        )
        self.assertEqual(flow_push._effective_push_url(repo), str(repo_b))

        decision = flow_push.guarded_push(repo, True, flowctl)
        self.assertEqual(decision.action, "pushed")

        self.assertEqual(
            _git(repo_a, "rev-parse", "trunk").stdout.strip(), a_before,
            "repo-A (never inspected by preflight) must not have received the push")
        self.assertEqual(
            _git(repo_b, "rev-parse", "trunk").stdout.strip(), head,
            "the push must land in the exact repository preflight inspected")

    def test_feature_branch_lands_only_in_the_inspected_repository(self):
        fetch_remote = self.root / "fetch.git"
        push_remote = self.root / "push.git"
        _git(self.root, "init", "-q", "--bare", "-b", "main", str(fetch_remote))
        _git(self.root, "init", "-q", "--bare", "-b", "main", str(push_remote))
        repo = self.root / "work"
        self._flow_repo(repo, branch="main")
        _git(repo, "remote", "add", "origin", str(fetch_remote))
        _git(repo, "push", "-q", "origin", "main")
        _git(repo, "push", "-q", str(push_remote), "main")
        _git(repo, "checkout", "-q", "-b", "feature-inherited")
        flowctl = self._complete_task(repo)
        head = _git(repo, "rev-parse", "HEAD").stdout.strip()

        self._inherit(
            GIT_CONFIG_COUNT="1",
            GIT_CONFIG_KEY_0="remote.origin.pushurl",
            GIT_CONFIG_VALUE_0=str(push_remote),
        )
        self.assertEqual(flow_push._effective_push_url(repo), str(push_remote))

        decision = flow_push.guarded_push(repo, True, flowctl)
        self.assertEqual(decision.action, "pushed")

        self.assertEqual(
            _git(push_remote, "rev-parse", "feature-inherited").stdout.strip(), head)
        self.assertEqual(
            subprocess.run(
                ["git", "ls-remote", "--heads", str(fetch_remote), "feature-inherited"],
                capture_output=True, text=True, check=True).stdout,
            "", "the fetch-only repository must not have received the branch")

    def test_inherited_git_config_parameters_cannot_re_enable_follow_tags(self):
        remote = self.root / "remote.git"
        _git(self.root, "init", "-q", "--bare", "-b", "main", str(remote))
        repo = self.root / "work"
        self._flow_repo(repo, branch="main")
        _git(repo, "remote", "add", "origin", str(remote))
        _git(repo, "push", "-q", "origin", "main")
        _git(repo, "checkout", "-q", "-b", "feature-hostile-tags")
        flowctl = self._complete_task(repo)
        _git(repo, "tag", "-a", "v9", "-m", "release v9")

        self._inherit(GIT_CONFIG_PARAMETERS="'push.followTags=true'")
        self.assertEqual(
            _git(repo, "config", "--get", "push.followTags").stdout.strip(), "true")

        decision = flow_push.guarded_push(repo, True, flowctl)

        self.assertEqual(decision.action, "pushed")
        self.assertIn(
            "feature-hostile-tags",
            subprocess.run(
                ["git", "ls-remote", "--heads", str(remote), "feature-hostile-tags"],
                capture_output=True, text=True, check=True).stdout)
        self.assertEqual(
            subprocess.run(
                ["git", "ls-remote", "--tags", str(remote)],
                capture_output=True, text=True, check=True).stdout,
            "", "no tag may be pushed, whatever the inherited config says")

    def test_inherited_git_config_parameters_cannot_re_enable_recurse_submodules(self):
        sub_remote = self.root / "sub.git"
        sub_control_remote = self.root / "sub-control.git"
        control_remote = self.root / "control.git"
        remote = self.root / "remote.git"
        for bare in (sub_remote, sub_control_remote, control_remote, remote):
            _git(self.root, "init", "-q", "--bare", "-b", "main", str(bare))
        sub_work = self.root / "sub-work"
        sub_work.mkdir()
        _init_repo(sub_work, branch="main")
        _git(sub_work, "remote", "add", "origin", str(sub_remote))
        _git(sub_work, "push", "-q", "-u", "origin", "main")

        repo = self.root / "work"
        self._flow_repo(repo, branch="main")
        _git(repo, "remote", "add", "origin", str(remote))
        _git(repo, "remote", "add", "control", str(control_remote))
        _git(repo, "push", "-q", "origin", "main")
        _git(repo, "checkout", "-q", "-b", "feature-hostile-sub")
        subprocess.run(
            ["git", "-c", "protocol.file.allow=always", "submodule", "add", "-q",
             str(sub_remote), "subdir"],
            cwd=repo, check=True, capture_output=True, text=True)
        _git(repo, "commit", "-q", "-m", "add submodule")
        subdir = repo / "subdir"
        (subdir / "extra.txt").write_text("submodule change\n")
        _git(subdir, "add", "extra.txt")
        _git(subdir, "commit", "-q", "-m", "submodule change")
        _git(subdir, "checkout", "-q", "-B", "feature-hostile-sub")
        _git(repo, "add", "subdir")
        _git(repo, "commit", "-q", "-m", "bump submodule pointer")
        flowctl = self._complete_task(repo)

        self._inherit(
            GIT_CONFIG_PARAMETERS="'push.recurseSubmodules=on-demand' 'protocol.file.allow=always'")
        self.assertEqual(
            _git(repo, "config", "--get", "push.recurseSubmodules").stdout.strip(), "on-demand")

        decision = flow_push.guarded_push(repo, True, flowctl)

        self.assertEqual(decision.action, "pushed")
        self.assertIn(
            "feature-hostile-sub",
            subprocess.run(
                ["git", "ls-remote", "--heads", str(remote), "feature-hostile-sub"],
                capture_output=True, text=True, check=True).stdout)
        self.assertEqual(
            subprocess.run(
                ["git", "ls-remote", "--heads", str(sub_remote), "feature-hostile-sub"],
                capture_output=True, text=True, check=True).stdout,
            "", "the submodule's own repository must not have been pushed")

        # Control, deliberately *after* the assertion above: an otherwise
        # identical push without the neutralization really does recurse.
        _git(subdir, "remote", "add", "control", str(sub_control_remote))
        subprocess.run(
            ["git", "-c", "push.recurseSubmodules=on-demand",
             "-c", "protocol.file.allow=always", "push", "-q", "control", "HEAD"],
            cwd=repo, check=True, capture_output=True, text=True)
        self.assertNotEqual(
            subprocess.run(
                ["git", "ls-remote", "--heads", str(sub_control_remote), "feature-hostile-sub"],
                capture_output=True, text=True, check=True).stdout,
            "", "control: this topology really is reachable by submodule recursion, "
                "so the assertion above is not vacuous")

    def test_unrelated_inherited_config_survives_alongside_the_overrides(self):
        decoy = self.root / "decoy.git"
        real = self.root / "real.git"
        _git(self.root, "init", "-q", "--bare", "-b", "main", str(decoy))
        _git(self.root, "init", "-q", "--bare", "-b", "main", str(real))
        repo = self.root / "work"
        self._flow_repo(repo, branch="main")
        _git(repo, "remote", "add", "origin", str(decoy))
        _git(repo, "push", "-q", "origin", "main")
        _git(repo, "push", "-q", str(real), "main")
        _git(repo, "checkout", "-q", "-b", "feature-mixed")
        flowctl = self._complete_task(repo)
        _git(repo, "tag", "-a", "v8", "-m", "release v8")

        self._inherit(
            GIT_CONFIG_COUNT="2",
            GIT_CONFIG_KEY_0="remote.origin.pushurl",
            GIT_CONFIG_VALUE_0=str(real),
            GIT_CONFIG_KEY_1="push.followTags",
            GIT_CONFIG_VALUE_1="true",
        )

        decision = flow_push.guarded_push(repo, True, flowctl)

        self.assertEqual(decision.action, "pushed")
        self.assertIn(
            "feature-mixed",
            subprocess.run(
                ["git", "ls-remote", "--heads", str(real), "feature-mixed"],
                capture_output=True, text=True, check=True).stdout,
            "the unrelated inherited pushurl must still decide the destination")
        self.assertEqual(
            subprocess.run(
                ["git", "ls-remote", "--heads", str(decoy), "feature-mixed"],
                capture_output=True, text=True, check=True).stdout, "")
        self.assertEqual(
            subprocess.run(
                ["git", "ls-remote", "--tags", str(real)],
                capture_output=True, text=True, check=True).stdout,
            "", "the safety override must still win over the inherited one")


if __name__ == "__main__":
    unittest.main()
