"""The parent's cloud review: one request per head, and a verdict only for that head.

The fixtures below are the answers this repository's own bots published on a
probe pull request on 2026-09-17, with the commit replaced by the test's head.
They are kept verbatim in shape because reading them is the whole feature: the
checklist lines, the badge image and the abbreviated commit are exactly what a
parser has to survive.
"""
import datetime
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_box import git_workflow as gw
from agent_box import github_review
from agent_box import workflow
from agent_box.errors import AgentBoxError
from workflow_fixtures import BINDING


CLAUDE_PASS = """**Claude finished @rus364's task in 32s** —— [View job](https://github.com/o/r/actions/runs/1)

---
### Review: commit `{short}`

- [x] Gather context (PR purpose, diff, repo guidance)
- [x] Review commit {head} against stated purpose

**Scope check.** The diff is exactly what the pull request describes, and the term it
introduces is the one the rest of the documentation already uses.

Reviewed-Commit: {head}
Verdict: PASS"""

CLAUDE_NEEDS_WORK = """**Claude finished @rus364's task in 33s** —— [View job](https://github.com/o/r/actions/runs/2)

---
### Review: commit `{short}`

- [x] Gather context (PR purpose, diff, AGENTS.md guidance)

AGENTS.md is explicit on this point, and this edit does what that rule forbids.

- [CRITICAL] agent_box/paths.py:37 — `HOST_HOME` now falls back to `os.environ.get("HOME")`
  before the passwd-derived path, so a poisoned environment can redirect bind mounts.

Reviewed-Commit: {head}
Verdict: NEEDS_WORK"""

CODEX_NO_FINDINGS = """Codex Review: Didn't find any major issues. Another round soon, please!

**Reviewed commit:** `{short10}`

<details> <summary>ℹ️ About Codex in GitHub</summary>
<br/>

If Codex has suggestions, it will comment; otherwise it will react with 👍.

</details>"""

CODEX_REVIEW = """### 💡 Codex Review

Here are some automated review suggestions for this pull request.

**Reviewed commit:** `{short10}`
"""

CODEX_FINDING = """**<sub><sub>![P1 Badge](https://img.shields.io/badge/P1-orange?style=flat)</sub></sub>  Restore the passwd-derived host home**

When `$HOME` differs from the passwd entry, this assignment trusts the environment and
redirects every `HOST_HOME`-derived default. Keep `HOST_HOME` derived exclusively from
`passwd_home()`.

Useful? React with 👍 / 👎."""

CODEX_SUMMARY = """<!-- codex-pull-request-review-summary -->
## Codex Review Summary

| Review | Status | Commit | Review trigger |
| --- | --- | --- | --- |
| 📝 **Code Review** | ✅ **Completed** | `{short}` | Manual request |
"""


def when(seconds_ago=0):
    """GitHub timestamps are absolute, and the wait budget measures from now."""
    moment = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=seconds_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def bot(login, body, created=None, identifier=1):
    created = created or when()
    return {"id": identifier, "user": {"login": login, "type": "Bot"}, "body": body,
            "created_at": created, "updated_at": created,
            "html_url": f"https://github.com/o/r/pull/9#issuecomment-{identifier}"}


class CloudReviewTests(unittest.TestCase):
    """A reviewer that answers where it runs still spends one round and one verdict."""

    def setUp(self):
        from test_local_workflow import LocalLandTests
        LocalLandTests.setUp(self)
        self.config.workflow.merge_mode = "github"
        self.config.workflow.review.policy = "always"
        self.config.workflow.review.github_provider = "claude"
        self.config.github.gh_token = True
        self.action = self.root / ".github/workflows/claude.yml"
        self.action.parent.mkdir(parents=True, exist_ok=True)
        self.comments = []
        self.reviews = []
        self.inline = []
        self.commit_action("uses: anthropics/claude-code-action@v1\n")

    def commit_action(self, source=None):
        """The Action is part of the reviewed head, so changing it moves the head."""
        self.action.write_text(source) if source is not None else self.action.unlink()
        gw.git(self.root, "add", "-A", ".github")
        gw.git(self.root, "commit", "-m", "claude action")
        self.head = gw.head_sha(self.root)
        self.pr = {"number": 9, "url": "https://github.com/o/r/pull/9", "state": "OPEN",
                   "headRefOid": self.head, "headRefName": "ab/7-parent", "baseRefName": "main"}
        self.saved = {**workflow.evidence(self.root, self.parent, workflow.scope(self.root, "main"), "main"),
                      "validation": {"command": ["true"], "exit": 0}}
        workflow.save_evidence(self.root, self.parent, self.saved)
        self.comments.clear()

    def paginate(self, root, repo, path):
        if path.startswith("issues/"):
            return self.comments
        return self.reviews if path.startswith("pulls/9/reviews") else self.inline

    def review(self, *flags):
        from agent_box import github_workflow as github
        from test_local_workflow import LocalLandTests
        self.requests = []

        def command(*args, **kwargs):
            # The body file lives in a temporary directory that is gone by the assertion.
            if args[1:3] == ("pr", "comment"):
                self.requests.append(Path(args[-1]).read_text())

        with patch.object(github.gw, "github_origin", return_value="o/r"), \
             patch.object(github, "find_pr", return_value=9), \
             patch.object(github, "view", return_value=self.pr), \
             patch.object(github_review, "paginate", side_effect=self.paginate), \
             patch.object(github, "gh", side_effect=command):
            return LocalLandTests.dispatch(self, ["review", "7", *flags])

    def published(self):
        """The request body this run posted, if it posted one."""
        return self.requests[-1] if self.requests else None

    def answer(self, body, *, login="claude[bot]", created=None):
        self.comments.append(bot(login, body.format(head=self.head, short=self.head[:7],
                                                    short10=self.head[:10]), created, len(self.comments) + 1))

    def request_comment(self, created=None):
        created = created or when(60)
        return {"id": 100, "user": {"login": "rus364", "type": "User"}, "created_at": created,
                "updated_at": created, "body": github_review.request_body("claude", self.head),
                "html_url": "https://github.com/o/r/pull/9#issuecomment-100"}

    def test_the_first_run_publishes_one_request_and_later_runs_publish_none(self):
        """A rerun is how the answer is read, so it must never ask a second time."""
        opened = self.review()
        body = self.published()
        self.assertIn("@claude review", body)
        self.assertIn(self.head, body)
        self.assertEqual(opened["requested_head"], self.head)
        self.assertEqual(opened["review_budget"]["used"], 0)
        self.comments.append({**self.request_comment(), "body": body})
        waiting = self.review()
        self.assertIsNone(self.published())
        self.assertEqual(waiting["round"], opened["round"])
        self.assertIn("no answer", waiting["outcome"])
        self.assertNotIn("review", workflow.read_evidence(self.root, self.parent))

    def test_claude_answers_in_the_reviewer_form_and_spends_one_round(self):
        """Claude is asked for the canonical answer, so its comment is read as a verdict."""
        self.review()
        self.comments.append(self.request_comment())
        self.answer(CLAUDE_PASS)
        result = self.review()
        self.assertEqual(result["verdict"], "PASS")
        self.assertEqual(result["findings"], {})
        self.assertEqual(result["reconciliation"]["status"], "applied")
        self.assertEqual(result["review_budget"]["used"], 1)
        self.assertEqual(result["route"]["provider"], "claude")
        self.assertEqual(workflow.read_evidence(self.root, self.parent)["review"]["verdict"], "PASS")

    def test_claude_findings_keep_their_own_severities(self):
        """The reviewer's checklist lines are not findings, and its one finding is."""
        self.review()
        self.comments.append(self.request_comment())
        self.answer(CLAUDE_NEEDS_WORK)
        result = self.review()
        self.assertEqual(result["verdict"], "NEEDS_WORK")
        self.assertEqual(result["findings"], {"CRITICAL": 1})

    def test_codex_priorities_become_findings_of_the_commit_they_annotate(self):
        """Codex answers in its own form: a review of the commit, with priorities inline."""
        self.config.workflow.review.github_provider = "codex"
        self.review()
        request = {**self.request_comment(), "body": github_review.request_body("codex", self.head)}
        self.comments.append(request)
        self.reviews.append({"user": {"login": "chatgpt-codex-connector[bot]", "type": "Bot"},
                             "state": "COMMENTED", "commit_id": self.head,
                             "submitted_at": when(),
                             "body": CODEX_REVIEW.format(short10=self.head[:10]),
                             "html_url": "https://github.com/o/r/pull/9#pullrequestreview-1"})
        self.inline.append({"user": {"login": "chatgpt-codex-connector[bot]", "type": "Bot"},
                            "commit_id": self.head, "path": "agent_box/paths.py", "line": 37,
                            "body": CODEX_FINDING})
        result = self.review()
        self.assertEqual(result["verdict"], "NEEDS_WORK")
        self.assertEqual(result["findings"], {"HIGH": 1})
        text = Path(result["result_path"]).read_text()
        self.assertIn("[HIGH] agent_box/paths.py:37", text)
        self.assertIn("Restore the passwd-derived host home", text)

    def test_codex_without_findings_is_a_pass_for_the_commit_it_names(self):
        """Nothing attached to the head it reviewed is the whole no-findings signal."""
        self.config.workflow.review.github_provider = "codex"
        self.review()
        self.comments.append({**self.request_comment(), "body": github_review.request_body("codex", self.head)})
        self.answer(CODEX_SUMMARY, login="chatgpt-codex-connector[bot]")
        self.answer(CODEX_NO_FINDINGS, login="chatgpt-codex-connector[bot]")
        result = self.review()
        self.assertEqual(result["verdict"], "PASS")
        self.assertEqual(result["findings"], {})

    def test_an_answer_about_another_commit_is_not_this_head_s_verdict(self):
        """A bot answering the previous push must not accept what was pushed after it."""
        self.review()
        self.comments.append(self.request_comment())
        other = "0" * 40
        self.comments.append(bot("claude[bot]", CLAUDE_PASS.format(head=other, short=other[:7]),
                                 when(), 7))
        self.assertIn("no answer", self.review()["outcome"])
        self.assertNotIn("review", workflow.read_evidence(self.root, self.parent))

    def test_an_answer_about_this_head_without_a_verdict_ends_the_round_at_once(self):
        """A finished answer that names no single verdict will not improve by waiting."""
        self.review()
        self.comments.append(self.request_comment())
        self.answer(CLAUDE_PASS + "\n\nVerdict: NEEDS_WORK")
        with self.assertRaisesRegex(AgentBoxError, "no single Verdict line"):
            self.review()

    def test_an_answer_published_before_the_request_is_not_its_answer(self):
        """A repository that reviews every push already answered; this head was asked again."""
        self.review()
        self.comments.append(self.request_comment(created=when()))
        self.answer(CLAUDE_PASS, created=when(60))
        self.assertIn("no answer", self.review()["outcome"])

    def test_silence_past_the_wait_budget_ends_the_round_and_asks_the_user(self):
        """Absence of an answer is never approval, and the coordinator must not decide alone."""
        self.config.workflow.review.wait_seconds = 1
        opened = self.review()
        self.comments.append(self.request_comment(created=when(3600)))
        with self.assertRaisesRegex(AgentBoxError, "published no verdict"):
            self.review()
        state = workflow.read_review_state(self.root, BINDING, self.parent)
        self.assertIsNone(state.get("pending"))
        self.assertEqual(state["failures"], 1)
        # The request stays published, so a retry opens a new round and reads the
        # same head's answer without asking the bot a second time.
        self.answer(CLAUDE_PASS)
        reopened = self.review()
        self.assertNotEqual(reopened["round"], opened["round"])
        self.assertIsNone(self.published())
        self.assertEqual(self.review()["verdict"], "PASS")

    def test_a_pinned_model_is_refused_before_anything_is_published(self):
        """Codex cannot pin a model, and a Claude Action pins it in the workflow, not in a comment."""
        self.config.workflow.review.github_model = "opus"
        self.config.workflow.review.github_provider = "codex"
        with self.assertRaisesRegex(AgentBoxError, "cannot pin model"):
            self.review()
        self.assertIsNone(self.published())
        self.config.workflow.review.github_provider = "claude"
        with self.assertRaisesRegex(AgentBoxError, "claude_args with --model opus"):
            self.review()
        self.commit_action("uses: anthropics/claude-code-action@v1\n  claude_args: '--model opus'\n")
        self.assertEqual(self.review()["provider"], "claude")
        self.commit_action()
        with self.assertRaisesRegex(AgentBoxError, "Claude GitHub Action"):
            self.review()

    def test_an_unpublished_head_has_no_reviewer_and_a_missing_pull_request_names_ab_pr(self):
        """The bot reads GitHub, so a head GitHub does not have cannot be the one reviewed."""
        from agent_box import github_workflow as github
        from test_local_workflow import LocalLandTests
        self.pr = {**self.pr, "headRefOid": "0" * 40}
        with self.assertRaisesRegex(AgentBoxError, "publish this head"):
            self.review()
        with patch.object(github.gw, "github_origin", return_value="o/r"), \
             patch.object(github, "find_pr", return_value=None), \
             patch.object(github, "gh"):
            with self.assertRaisesRegex(AgentBoxError, "ab-pr"):
                LocalLandTests.dispatch(self, ["review", "7"])

    def test_a_disabled_github_integration_refuses_before_any_call(self):
        """The bot lives on GitHub; without that integration there is nothing to ask."""
        self.config.github.gh_token = False
        with self.assertRaisesRegex(AgentBoxError, "Enable the GitHub integration"):
            self.review()
        self.assertIsNone(self.published())

    def test_the_local_route_still_answers_when_the_user_asks_for_it(self):
        """A bot that never answers must leave the user a way through, not a dead end."""
        opened = self.review("--native")
        self.assertIn("message", opened)
        self.assertIsNone(self.published())


class CloudRouteTests(unittest.TestCase):
    """Where the final review happens changes what finishing the parent requires."""

    setUp = CloudReviewTests.setUp
    commit_action = CloudReviewTests.commit_action

    def dispatch(self, arguments):
        from test_local_workflow import LocalLandTests
        return LocalLandTests.dispatch(self, arguments)

    def test_finishing_a_parent_leaves_its_final_review_to_the_pull_request(self):
        """The reviewer reads a pull request that does not exist before finish."""
        self.parent.state = "Doing"
        result = self.dispatch(["finish", "7"])
        self.assertEqual(result["state"], "Review")
        self.assertIn("workflow review 7", result["next"])
        self.adapter.set_state.assert_called_once_with(self.parent, "Review")

    def test_a_local_route_still_requires_the_local_verdict_before_review(self):
        """Only a cloud provider moves the parent's final review out of the local round."""
        self.config.workflow.review.github_provider = "auto"
        self.parent.state = "Doing"
        with self.assertRaisesRegex(AgentBoxError, "fresh independent PASS"):
            self.dispatch(["finish", "7"])
        self.adapter.set_state.assert_not_called()

    def test_inspect_names_the_route_that_will_answer(self):
        """The coordinator must not guess where a verdict comes from."""
        self.assertEqual(self.dispatch(["inspect", "7"])["review"]["route"],
                         "claude in the parent pull request")
        self.config.workflow.merge_mode = "local"
        self.assertEqual(self.dispatch(["inspect", "7"])["review"]["route"], "local reviewer")

    def test_a_subtask_keeps_its_local_round_in_either_mode(self):
        """Only the parent has a pull request; a subtask is reviewed where it is built."""
        from agent_box.tracker.core import Task
        child = Task("8", "1", "Child", state="Doing", assignees=["1"], parent="7")
        self.assertFalse(workflow.cloud_review(self.config, child, self.parent))
        self.assertTrue(workflow.cloud_review(self.config, self.parent, self.parent))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
