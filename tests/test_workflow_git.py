"""Task completion evidence follows content and requirements, not optimistic flags."""
import tempfile
import unittest
from pathlib import Path
import subprocess

from agent_box.tracker.core import Task


class WorkflowGitTests(unittest.TestCase):
    def test_scope_follows_code_and_base_not_requirements(self):
        """A card edit keeps acceptance; a commit or a moved base is new code to validate and review."""
        from agent_box.workflow import scope
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for args in (["init", "-b", "main"], ["config", "user.name", "Test"], ["config", "user.email", "test@example.invalid"],
                         ["commit", "--allow-empty", "-m", "initial"], ["switch", "-c", "ab/1-task"]):
                subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
            original = scope(root, "main")
            subprocess.run(["git", "commit", "--allow-empty", "-m", "work"], cwd=root, check=True, capture_output=True)
            worked = scope(root, "main")
            self.assertNotEqual(worked, original)
            subprocess.run(["git", "commit", "--allow-empty", "-m", "base moves"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "branch", "-f", "main", "HEAD"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "reset", "--hard", "HEAD~1"], cwd=root, check=True, capture_output=True)
            self.assertNotEqual(scope(root, "main"), worked)

    def test_auto_review_uses_diff_and_labels_not_priority(self):
        from agent_box.workflow import review_required
        from agent_box.config import ReviewConfig
        config = ReviewConfig()
        task = Task("1", "1", "Task", priority=5)
        self.assertFalse(review_required(config, [task], ["readme.md"], 1))
        self.assertTrue(review_required(config, [task], ["src/auth/token.py"], 1))
        self.assertTrue(review_required(config, [task], ["readme.md"], 200))

    def test_ignored_paths_leave_trigger_counts_but_never_hide_sensitive_paths(self):
        """A documentation-only change skips auto review; a broad ignore must not silence
        a sensitive file, a binary it does not cover, or the always policy."""
        from agent_box import git_workflow as gw
        from agent_box.workflow import diff_stats, review_reasons, review_required
        from agent_box.config import ReviewConfig
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for args in (["init", "-b", "main"], ["config", "user.name", "Test"], ["config", "user.email", "test@example.invalid"],
                         ["commit", "--allow-empty", "-m", "initial"], ["switch", "-c", "ab/1-task"]):
                subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
            (root / "docs").mkdir()
            (root / "docs" / "guide.txt").write_text("guide\n" * 300)
            (root / "readme.md").write_text("readme\n")
            (root / "logo.png").write_bytes(b"\x00\x01binary")
            gw.git(root, "add", "-A")
            gw.git(root, "commit", "-m", "docs")
            task = Task("1", "1", "Task")
            ignored = ReviewConfig(max_files=1, ignored_paths=["*.md", "docs/**", "**/*.png"])
            self.assertEqual(diff_stats(root, "main", ignored), ([], 0))
            self.assertFalse(review_required(ignored, [task], [], 0))
            default = ReviewConfig(max_files=1)
            self.assertTrue(review_required(default, [task], *diff_stats(root, "main", default)))
            ignored.policy = "always"
            self.assertTrue(review_required(ignored, [task], *diff_stats(root, "main", ignored)))
            ignored.policy = "auto"
            (root / "agent_box" / "skills" / "ab-work").mkdir(parents=True)
            (root / "agent_box" / "skills" / "ab-work" / "SKILL.md").write_text("rule\n")
            gw.git(root, "add", "-A")
            gw.git(root, "commit", "-m", "skill")
            files, lines = diff_stats(root, "main", ignored)
            self.assertEqual((files, lines), (["agent_box/skills/ab-work/SKILL.md"], 1))
            self.assertIn("sensitive path agent_box/skills/ab-work/SKILL.md matches **/skills/**",
                          review_reasons(ignored, [task], files, lines))

    def test_ignored_paths_match_real_names_of_renames_and_non_ascii_paths(self):
        """Git's display field joins renames as `old => new` and quotes non-ASCII paths;
        matching it would drop code renamed out of docs and never ignore `docs/планы.md`.
        A file moved out of a sensitive path into an ignored one must still trigger review."""
        from agent_box import git_workflow as gw
        from agent_box.workflow import diff_stats, review_reasons
        from agent_box.config import ReviewConfig
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for args in (["init", "-b", "main"], ["config", "user.name", "Test"], ["config", "user.email", "test@example.invalid"]):
                subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
            (root / "docs").mkdir()
            (root / "docs" / "guide.md").write_text("".join(f"line {number}\n" for number in range(50)))
            (root / "docs" / "notes.md").write_text("".join(f"note {number}\n" for number in range(50)))
            (root / "agent_box" / "api").mkdir(parents=True)
            (root / "agent_box" / "api" / "handler.py").write_text("".join(f"handle_{number} = {number}\n" for number in range(50)))
            gw.git(root, "add", "-A")
            gw.git(root, "commit", "-m", "docs")
            gw.git(root, "switch", "-c", "ab/1-task")
            gw.git(root, "mv", "docs/guide.md", "tool.py")
            gw.git(root, "mv", "docs/notes.md", "docs/archive.md")
            gw.git(root, "mv", "agent_box/api/handler.py", "docs/handler.py")
            (root / "docs" / "планы.md").write_text("plan\n")
            gw.git(root, "add", "-A")
            gw.git(root, "commit", "-m", "rename")
            config = ReviewConfig(ignored_paths=["docs/**"])
            files, lines = diff_stats(root, "main", config)
            self.assertCountEqual(files, ["tool.py", "agent_box/api/handler.py"])
            self.assertEqual(lines, 0)
            self.assertIn("sensitive path agent_box/api/handler.py matches **/api/**",
                          review_reasons(config, [Task("1", "1", "Task")], files, lines))

    def test_reviewer_report_is_refused_once_working_files_change(self):
        """A reviewer reads a committed scope; an uncommitted edit blocks applying its verdict."""
        from unittest.mock import Mock, patch
        from agent_box import workflow
        from agent_box.config import Config
        from agent_box.repo_binding import Binding
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            for args in (["init", "-b", "main"], ["config", "user.name", "Test"], ["config", "user.email", "test@example.invalid"],
                         ["commit", "--allow-empty", "-m", "initial"], ["switch", "-c", "ab/task"]):
                subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
            task = Task("1", "1", "Task", description="## Acceptance\n- [ ] requirement", state="Doing", assignees=["1"], labels=["ab:tier:1"])
            config = Config()
            config.workflow.enabled = True
            config.workflow.review.policy = "always"
            config.workflow.use_tiers = True
            adapter = Mock()
            adapter.get.return_value = task
            adapter.identity.return_value = {"id": 1}
            route = {"fast": False, "provider": "claude", "tier": 1, "role": "reviewer", "model": "exact-model", "effort": "high", "fallbacks": []}
            info = {"branch": "ab/task", "base": "main"}
            report = Path(temporary) / "report.md"
            report.write_text("Verdict: PASS\n")
            brief = Path(temporary) / "review-brief.md"
            brief.write_text("Changes/reasons: code change. Risks: values. Validation: static only; no live proof.\n")
            (root / "change.py").write_text("value = 1\n")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "change"], cwd=root, check=True, capture_output=True)
            with patch.object(workflow, "effective_config", return_value=config), patch.object(workflow, "repository_root", return_value=root), \
                 patch.object(Binding, "load", return_value=Binding("vikunja", "https://tasks.example", "1")), \
                 patch.object(workflow, "make_adapter", return_value=adapter), \
                 patch.object(workflow, "resolve", return_value=(task, info, {}, "main")), \
                 patch.object(workflow, "candidates", return_value=route), patch.object(workflow, "WORKFLOW_STATE", Path(temporary) / "state"):
                workflow.dispatch(workflow.parser().parse_args(["review", "1", "--native", "--brief", str(brief)]))
                (root / "change.py").write_text("value = 2\n")
                result = workflow.dispatch(workflow.parser().parse_args([
                    "review", "1", "--result", str(report), "--provider", "claude", "--model", "exact-model"]))
                self.assertEqual(result["reconciliation"]["status"], "blocked")
                self.assertIn("working_tree", result["reconciliation"]["changes"])
                self.assertNotIn("review", workflow.read_evidence(root, task))
                adapter.comment.assert_not_called()

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
