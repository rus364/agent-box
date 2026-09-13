"""Task completion evidence follows content and requirements, not optimistic flags."""
import tempfile
import unittest
from pathlib import Path
import subprocess

from agent_box.tracker.core import Task


class WorkflowGitTests(unittest.TestCase):
    def test_review_is_invalidated_by_requirement_changes(self):
        from agent_box.workflow import scope
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for args in (["init", "-b", "main"], ["config", "user.name", "Test"], ["config", "user.email", "test@example.invalid"],
                         ["commit", "--allow-empty", "-m", "initial"]):
                subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
            task = Task("1", "1", "Task", description="- [ ] requirement")
            original = scope(root, "main", [task])
            task.description = "- [x] requirement"
            self.assertEqual(scope(root, "main", [task]), original)
            task.description = "- [ ] new requirement"
            self.assertNotEqual(scope(root, "main", [task]), original)

    def test_auto_review_uses_diff_and_labels_not_priority(self):
        from agent_box.workflow import review_required
        from agent_box.config import ReviewConfig
        config = ReviewConfig()
        task = Task("1", "1", "Task", priority=5)
        self.assertFalse(review_required(config, [task], ["readme.md"], 1))
        self.assertTrue(review_required(config, [task], ["src/auth/token.py"], 1))
        self.assertTrue(review_required(config, [task], ["readme.md"], 200))

    def test_native_worker_report_accepts_its_code_changes_but_reviewer_report_does_not(self):
        from unittest.mock import Mock, patch
        from agent_box import workflow
        from agent_box.config import Config
        from agent_box.repo_binding import Binding
        import json
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            for args in (["init", "-b", "main"], ["config", "user.name", "Test"], ["config", "user.email", "test@example.invalid"],
                         ["commit", "--allow-empty", "-m", "initial"], ["switch", "-c", "ab/task"]):
                subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
            task = Task("1", "1", "Task", description="## Acceptance\n- [ ] requirement", state="Doing", assignees=["1"], labels=["ab:tier:1"])
            config = Config()
            config.workflow.enabled = True
            adapter = Mock()
            adapter.get.return_value = task
            adapter.identity.return_value = {"id": 1}
            route = {"fast": False, "provider": "claude", "tier": 1, "role": "worker", "model": "exact-model", "effort": "high", "fallbacks": []}
            info = {"branch": "ab/task", "base": "main", "writer": "codex"}
            report = Path(temporary) / "report.json"
            report.write_text(json.dumps({"status": "completed", "summary": "Implemented", "evidence": ["Changed code"]}))
            brief = Path(temporary) / "review-brief.md"
            brief.write_text("Changes/reasons: code change. Risks: values. Validation: static only; no live proof.\n")
            with patch.object(workflow, "effective_config", return_value=config), patch.object(workflow, "repository_root", return_value=root), \
                 patch.object(Binding, "load", return_value=Binding("vikunja", "https://tasks.example", "1")), \
                 patch.object(workflow, "make_adapter", return_value=adapter), \
                 patch.object(workflow, "resolve", return_value=(task, info, info, "main")), \
                 patch.object(workflow, "route", return_value=route), patch.object(workflow, "WORKFLOW_STATE", Path(temporary) / "state"):
                prepared = workflow.dispatch(workflow.parser().parse_args(["prepare", "1", "--primary", "codex", "--reason", "Git fixture", "--brief", str(brief)]))
                (root / "change.py").write_text("value = 1\n")
                result = workflow.dispatch(workflow.parser().parse_args(["report", "1", "--role", "worker", "--primary", "codex", "--provider", "claude", "--model", "exact-model", "--effort", "high", "--fast", "off", "--file", str(report), "--scope", prepared["scope"], "--handoff", prepared["handoff"]]))
                self.assertEqual(result["route"]["provider"], "claude")
                # The writer is local evidence; nothing about it reaches the tracker.
                self.assertEqual(workflow.read_evidence(root, task)["writer"], "claude")
                adapter.comment.assert_not_called()
                workflow.dispatch(workflow.parser().parse_args(["writer", "1", "--provider", "codex", "--reason", "Coordinator fix"]))
                self.assertEqual(workflow.read_evidence(root, task)["writer"], "codex")
                writer = workflow.read_evidence(root, task)["writer"]
                from agent_box.routing import select
                config.workflow.review.tiers["1"] = {provider: {"fast": False, "model": provider + "-model", "effort": "high"} for provider in ("codex", "claude")}
                self.assertEqual(select(config.workflow, "reviewer", 1, "codex", {}, writer=writer)["provider"], "claude")
                subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
                subprocess.run(["git", "commit", "-m", "change"], cwd=root, check=True, capture_output=True)
                prepared = workflow.dispatch(workflow.parser().parse_args([
                    "prepare", "1", "--role", "reviewer", "--primary", "codex", "--brief", str(brief)]))
                (root / "change.py").write_text("value = 2\n")
                from agent_box.errors import AgentBoxError
                with self.assertRaises(AgentBoxError):
                    workflow.dispatch(workflow.parser().parse_args(["report", "1", "--role", "reviewer", "--primary", "codex", "--provider", "claude", "--model", "exact-model", "--effort", "high", "--fast", "off", "--file", str(report), "--scope", prepared["scope"], "--handoff", prepared["handoff"]]))

    def test_origin_identity_follows_the_url_rewrite_a_push_would_follow(self):
        """`insteadOf` makes `remote.origin.url` and the real destination differ.

        The base branch, a task branch's live state and the GitHub repository are
        all read out of origin, so they have to resolve the same rewrite that the
        push itself resolves; reading the configuration key would not.
        """
        from agent_box import git_workflow as git
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for args in (["init", "-b", "main"],
                         ["remote", "add", "origin", "insteadof:owner/repository.git"],
                         ["config", "url.git@github.com:.insteadOf", "insteadof:"]):
                subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
            self.assertEqual(git.effective_push_url(root), "git@github.com:owner/repository.git")
            self.assertEqual(git.github_origin(root), "owner/repository")
            subprocess.run(["git", "config", "url.git@example.invalid:.pushInsteadOf", "insteadof:"],
                           cwd=root, check=True, capture_output=True)
            self.assertEqual(git.effective_push_url(root), "git@example.invalid:owner/repository.git")

    def test_parent_push_reuses_live_remote_guards_and_does_not_publish_tags(self):
        from unittest.mock import patch
        from agent_box import git_workflow as git
        from agent_box.errors import AgentBoxError
        with tempfile.TemporaryDirectory() as temporary:
            remote, root = Path(temporary) / "remote.git", Path(temporary) / "repo"
            subprocess.run(["git", "init", "--bare", "-b", "main", str(remote)], check=True, capture_output=True)
            subprocess.run(["git", "init", "-b", "main", str(root)], check=True, capture_output=True)
            for args in (("config", "user.name", "Test"), ("config", "user.email", "test@example.invalid"),
                         ("commit", "--allow-empty", "-m", "initial"), ("remote", "add", "origin", str(remote)),
                         ("push", "origin", "main"), ("switch", "-c", "ab/parent"),
                         ("commit", "--allow-empty", "-m", "change"), ("tag", "-a", "private-tag", "-m", "private"),
                         ("config", "push.followTags", "true")):
                git.git(root, *args)
            with patch.object(git, "github_origin", return_value="owner/repository"):
                git.push_parent(root, "ab/parent", "main")
                self.assertEqual(git.git(remote, "rev-parse", "refs/heads/ab/parent"), git.head_sha(root))
                self.assertEqual(git.git(remote, "tag", "--list"), "")
                (root / "uncommitted").write_text("user work")
                with self.assertRaises(AgentBoxError):
                    git.push_parent(root, "ab/parent", "main")
                (root / "uncommitted").unlink()
                git.git(root, "switch", "main")
                with self.assertRaises(AgentBoxError):
                    git.push_parent(root, "main", "main")
