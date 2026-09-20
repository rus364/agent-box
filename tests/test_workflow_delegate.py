"""Workers and scouts are launched, not tracked: the answer is Markdown and nothing binds it.

The prepare/descriptor/report ceremony bound a delegate's report to its scope,
HEAD and requirements; most of its refusals fired after a paid run, on edits
that changed nothing the delegate relied on. These tests protect the
replacement: pick the role's first available candidate, run it once in a fresh
managed process, return its answer, and keep nothing that later commands check.
"""
import contextlib
import io
import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_box import delegation, git_workflow as gw, provider_runner, workflow, workflow_state
from agent_box.agent_profiles import body
from agent_box.errors import AgentBoxError
from agent_box.tracker.core import Task
from workflow_fixtures import BINDING, WorkflowCase, candidate, repository

ANSWER = "## Done\nEdited change.py.\n\n## Evidence\n- unittest passed\n\n## Limitations\nNone."


class DelegateCommandTests(WorkflowCase):
    def setUp(self):
        super().setUp()
        self.config.workflow.worker.models = [candidate("claude", "worker-model", "high")]
        self.config.workflow.scout.models = [candidate("codex", "scout-model", "high"),
                                             candidate("claude", "scout-fallback", "low")]
        usage = patch.object(workflow, "usage_snapshot", return_value={})
        usage.start()
        self.addCleanup(usage.stop)

    def delegate(self, role, brief=None):
        return self.run_workflow("delegate", "1", "--role", role, "--brief", str(brief or self.review_brief))

    def test_scout_takes_the_first_candidate_and_skips_an_exhausted_window(self):
        """Order states the preference; a known exhausted subscription is passed over, not launched to fail."""
        exhausted = {"windows": [{"seconds": 18000, "remaining": 0, "reset_at": 4102444800}]}
        for usage, expected in (({}, "scout-model"), ({"codex": exhausted}, "scout-fallback")):
            with self.subTest(expected=expected), \
                 patch.object(workflow, "usage_snapshot", side_effect=lambda provider, **_: usage.get(provider, {})), \
                 patch.object(workflow, "run", return_value=ANSWER) as run:
                result = self.delegate("scout")
            self.assertEqual(run.call_args.args[0]["model"], expected)
            self.assertEqual(run.call_args.args[1], "scout")
            self.assertEqual(result["route"]["model"], expected)

    def test_the_task_tier_selects_a_worker_list_only_when_tiers_are_on_and_never_a_scouts(self):
        """The task is labelled tier 1; tiers off must run the role's own list, and scouts have no tier lists."""
        self.config.workflow.worker.tiers["1"] = [candidate("codex", "worker-tier1", "high")]
        for use_tiers, expected in ((False, "worker-model"), (True, "worker-tier1")):
            self.config.workflow.use_tiers = use_tiers
            with self.subTest(use_tiers=use_tiers), patch.object(workflow, "run", return_value=ANSWER) as run:
                self.delegate("worker")
                self.assertEqual(run.call_args.args[0]["model"], expected)
                self.delegate("scout")
                self.assertEqual((run.call_args.args[0]["model"], run.call_args.args[0]["tier"]), ("scout-model", None))

    def test_worker_answer_is_returned_as_markdown_and_saved_privately(self):
        """The coordinator reads the answer as written; the saved copy is private and nothing else is recorded."""
        before = workflow.read_evidence(self.root, self.task)

        def provider(route, role, root, prompt, **kwargs):
            self.assertEqual((route["model"], role), ("worker-model", "worker"))
            (Path(root) / "change.py").write_text("value = 1\n")
            return ANSWER

        with patch.object(workflow, "run", side_effect=provider):
            result = self.delegate("worker")
        self.assertEqual(result["response"], ANSWER)
        saved = Path(result["response_path"])
        self.assertEqual(saved.read_text(), ANSWER)
        self.assertEqual(stat.S_IMODE(saved.stat().st_mode), 0o600)
        self.assertEqual(workflow.read_evidence(self.root, self.task), before)
        self.assertFalse(workflow.review_budget_path(self.root, BINDING, self.task).exists())

    def test_command_prints_the_answer_and_puts_its_route_beside_it(self):
        with patch.object(workflow, "run", return_value=ANSWER), \
             contextlib.redirect_stdout(io.StringIO()) as stdout, \
             contextlib.redirect_stderr(io.StringIO()) as stderr:
            code = workflow.main(["delegate", "1", "--role", "scout", "--brief", str(self.review_brief)])
        self.assertEqual(code, 0)
        self.assertEqual(stdout.getvalue(), ANSWER + "\n")
        metadata = json.loads(stderr.getvalue())
        self.assertEqual(metadata["route"]["model"], "scout-model")
        self.assertTrue(Path(metadata["response_path"]).exists())

    def test_prompt_carries_the_brief_rules_and_repository_instructions(self):
        """Claude's managed run is in safe mode, so the repository's own rules travel in the prompt."""
        (self.root / "AGENTS.md").write_text("Never add a compatibility shim.\n")
        with patch.object(workflow, "run", return_value=ANSWER) as run:
            self.delegate("scout")
        prompt = run.call_args.args[3]
        self.assertIn(self.review_brief.read_text(), prompt)
        self.assertIn("Never add a compatibility shim.", prompt)
        self.assertIn("Read-only: change nothing", prompt)
        self.assertIn("## Done, ## Evidence, ## Limitations", prompt)

    def test_managed_scouts_beyond_the_limit_are_refused_before_launch(self):
        """Live managed scouts hold repository slot locks; one more than the limit does not start."""
        self.config.workflow.scout.limit = 2
        with contextlib.ExitStack() as held:
            for index in range(2):
                held.enter_context(workflow_state.lock(self.root, f"scout-{index}"))
            with patch.object(workflow, "run") as run, self.assertRaisesRegex(AgentBoxError, "All 2 local scout slots are busy"):
                self.delegate("scout")
            run.assert_not_called()
        with patch.object(workflow, "run", return_value=ANSWER):
            self.assertEqual(self.delegate("scout")["response"], ANSWER)

    def test_task_card_edits_while_a_delegate_runs_change_nothing(self):
        """Requirements, checkboxes and hierarchy may move during the run; the answer still arrives and is usable."""
        def provider(route, role, root, prompt, **kwargs):
            self.task.title = "Renamed while running"
            self.task.description = "## Acceptance\n- [x] requirement\n- [ ] added while running"
            self.task.children = ["2"]
            (Path(root) / "change.py").write_text("value = 1\n")
            return ANSWER

        with patch.object(workflow, "run", side_effect=provider):
            self.assertEqual(self.delegate("worker")["response"], ANSWER)
        self.task.children = []
        gw.git(self.root, "add", ".")
        gw.git(self.root, "commit", "-m", "coordinator commits the worker's edit")
        self.assertEqual(self.run_workflow("validate", "1", "--", "true")["validation"]["exit"], 0)

    def test_provider_failure_names_its_category_and_readable_diagnostic(self):
        failure = provider_runner.ProviderFailure("Quota exhausted", "quota")
        failure.diagnostic = {"provider": "codex", "model": "scout-model", "category": "quota",
                              "artifact": "provider-failures/fixture.json", "stdout_bytes": 7}
        fallback = provider_runner.ProviderFailure("Model missing", "model-unavailable")
        fallback.diagnostic = {"provider": "claude", "model": "scout-fallback", "artifact": None}
        with patch.object(workflow, "run", side_effect=[failure, fallback]), \
             contextlib.redirect_stdout(io.StringIO()) as stdout:
            code = workflow.main(["delegate", "1", "--role", "scout", "--brief", str(self.review_brief)])
        self.assertEqual(code, 2)
        output = json.loads(stdout.getvalue())
        self.assertIn("No provider produced a valid answer", output["error"])
        self.assertEqual(output["failures"][0], {
            "provider": "codex", "model": "scout-model", "category": "quota",
            "diagnostic": str(workflow.WORKFLOW_STATE / "provider-failures/fixture.json")})
        self.assertEqual(output["failures"][1]["category"], "model-unavailable")
        self.assertNotIn("diagnostic", output["failures"][1])

    def test_refusal_before_launch_still_names_its_reason_and_diagnostic(self):
        """Settings refused before a process starts still name why and where the private diagnostic is."""
        self.config.workflow.scout.models = [candidate("claude", "claude-opus-5", "high") | {"fast": True}]
        with patch.dict("os.environ", {"CLAUDE_CODE_DISABLE_FAST_MODE": "1"}), \
             patch.object(provider_runner.subprocess, "Popen") as popen, \
             contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(workflow.main(["delegate", "1", "--role", "scout", "--brief", str(self.review_brief)]), 2)
        popen.assert_not_called()
        failure = json.loads(stdout.getvalue())["failures"][0]
        self.assertEqual((failure["category"], failure["stage"]), ("unsupported", "preflight"))
        self.assertIn("CLAUDE_CODE_DISABLE_FAST_MODE", failure["reason"])
        artifact = json.loads(Path(failure["diagnostic"]).read_text())
        self.assertEqual(artifact["metadata"]["reason"], failure["reason"])
        self.assertEqual(artifact["streams"], {})

    def test_worker_needs_its_task_branch_and_a_configured_candidate(self):
        """A managed worker edits the checkout, so it must be the task's; with no candidate the coordinator works itself.

        Suggesting a native subagent instead would only rerun the session's own
        model in a fresh context that rereads everything.
        """
        gw.git(self.root, "switch", "main")
        with patch.object(workflow, "run") as run, self.assertRaisesRegex(AgentBoxError, "Switch to task branch ab/1-task"):
            self.delegate("worker")
        run.assert_not_called()
        gw.git(self.root, "switch", "ab/1-task")
        self.config.workflow.worker.models = []
        with patch.object(workflow, "run") as run, \
             self.assertRaisesRegex(AgentBoxError, "workflow.worker.models is empty, so the coordinator does this "
                                                   "worker work itself in its own session") as raised:
            self.delegate("worker")
        run.assert_not_called()
        self.assertNotIn("subagent", str(raised.exception))

    def test_inspect_names_how_each_assignment_runs(self):
        """Empty means the session itself; a list names its first candidate's native profile and the managed command.

        Only the provider opening the list gets a profile: another provider's
        profile runs a later fallback, which would silently skip the preferred
        model. A tier list selects the tier's profile only while tiers are on.
        """
        self.config.workflow.worker.tiers["1"] = [candidate("codex", "worker-tier1", "high")]
        self.config.workflow.scout.models = []
        for use_tiers, worker in ((False, {"mode": "delegate", "models": "workflow.worker.models",
                                           "native": {"claude": "ab-worker"}}),
                                  (True, {"mode": "delegate", "models": "workflow.worker.tiers.1",
                                          "native": {"codex": "ab-worker-1"}})):
            self.config.workflow.use_tiers = use_tiers
            with self.subTest(use_tiers=use_tiers):
                execution = self.run_workflow("inspect", "1")["execution"]
                self.assertEqual(execution["worker"], {
                    **worker, "managed": "agent-box workflow delegate 1 --role worker --brief FILE"})
                self.assertEqual(execution["scout"], {"mode": "self", "models": "workflow.scout.models"})
        self.config.workflow.use_tiers = False
        self.config.workflow.worker.models = [{**candidate("claude", "worker-model", "high"), "api_provider": "deepseek"}]
        self.config.workflow.scout.models = [candidate("codex", "scout-model", "high"),
                                             candidate("claude", "scout-fallback", "low")]
        execution = self.run_workflow("inspect", "1")["execution"]
        self.assertEqual((execution["worker"]["native"], execution["scout"]["native"]), ({}, {"codex": "ab-scout"}))

    def test_inspect_exposes_the_root_native_wait_policy(self):
        """The coordinator can follow the global switch without passing it to delegates."""
        self.config.codex.native_agent_wait.enabled = False
        self.config.codex.native_agent_wait.interval_seconds = 900
        summary = self.run_workflow("inspect", "1")["configuration"]
        self.assertEqual(summary["native_agent_wait"], {"enabled": False, "interval_seconds": 900})

    def test_reviewer_native_route_preserves_candidate_order_and_effective_tier(self):
        """A matching session uses the first candidate's profile, never a later fallback."""
        self.config.workflow.review.models = [candidate("codex", "gpt-5.6-luna", "max"),
                                              candidate("claude", "fallback", "high")]
        self.config.workflow.review.tiers["1"] = [candidate("claude", "tier-reviewer", "high")]
        for tiered, native, source in ((False, {"codex": "ab-reviewer"}, "workflow.review.models"),
                                      (True, {"claude": "ab-reviewer-1"}, "workflow.review.tiers.1")):
            self.config.workflow.use_tiers = tiered
            with self.subTest(tiered=tiered):
                self.assertEqual(self.run_workflow("inspect", "1")["execution"]["reviewer"],
                                 {"mode": "delegate", "models": source, "native": native,
                                  "managed": "agent-box workflow review 1"})
        self.config.workflow.use_tiers = False
        self.config.workflow.review.models[0]["api_provider"] = "deepseek"
        self.assertEqual(self.run_workflow("inspect", "1")["execution"]["reviewer"]["native"], {})

    def test_default_effort_reviewer_stays_managed_to_avoid_inherited_effort(self):
        """Omitting native effort can inherit the coordinator's override instead of the model default."""
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider):
                self.config.workflow.review.models = [candidate(provider, "review-model", "default")]
                route = self.run_workflow("inspect", "1")["execution"]["reviewer"]
                self.assertEqual(route["native"], {})
                self.assertEqual(route["managed"], "agent-box workflow review 1")

    def test_fast_review_uses_native_only_when_the_profile_can_pin_speed(self):
        """Claude profiles inherit speed; Codex profiles can express the configured fast request."""
        for provider, model, native in (("claude", "claude-opus-5", {}),
                                        ("codex", "gpt-5.6-luna", {"codex": "ab-reviewer"})):
            with self.subTest(provider=provider):
                self.config.workflow.review.models = [{**candidate(provider, model, "high"), "fast": True}]
                route = self.run_workflow("inspect", "1")["execution"]["reviewer"]
                self.assertEqual(route["native"], native)
                self.assertEqual(route["managed"], "agent-box workflow review 1")

    def test_empty_reviewer_list_never_suggests_self_review_or_overrides_cloud(self):
        """Independent native review remains required without candidates; cloud parents stay cloud."""
        self.config.workflow.review.models = []
        self.assertEqual(self.run_workflow("inspect", "1")["execution"]["reviewer"],
                         {"mode": "native", "models": "workflow.review.models"})
        self.config.workflow.merge_mode = "github"
        self.config.workflow.review.github_provider = "codex"
        self.assertEqual(self.run_workflow("inspect", "1")["execution"]["reviewer"],
                         {"mode": "cloud", "command": "agent-box workflow review 1"})

    def test_direct_work_needs_no_record_before_validation_review_and_finish(self):
        """The coordinator's own commit goes straight to validate, policy review and finish."""
        self.task.description = "## Acceptance\n- [x] requirement"
        self.config.workflow.review.policy = "off"
        (self.root / "change.py").write_text("value = 1\n")
        gw.git(self.root, "add", ".")
        gw.git(self.root, "commit", "-m", "direct change")
        self.run_workflow("validate", "1", "--", "true")
        self.assertEqual(self.run_workflow("review", "1")["review"]["verdict"], "SKIPPED")
        self.assertEqual(self.run_workflow("finish", "1")["state"], "Review")
        for removed in ("writer", "run"):
            with self.subTest(command=removed), contextlib.redirect_stderr(io.StringIO()), \
                 self.assertRaises(SystemExit):
                workflow.parser().parse_args([removed, "1"])


class DelegateContractTests(unittest.TestCase):
    """One worker contract, whichever backend runs the delegation.

    A delegation runs either as a native subagent of the coordinator's own
    session or as an agent-box-managed `codex exec` / `claude -p`. The native one
    inherits the coordinator's workspace, `.git` and credentials, so no
    per-delegate isolation is available to it; deliberately, none is imposed on
    the managed one either.
    """

    def test_the_native_profile_states_the_same_rules_as_the_managed_prompt(self):
        for role in ("worker", "scout"):
            with self.subTest(role=role):
                profile = body("claude", role, 1, {"fast": False, "model": "m", "effort": "high"})
                prompt = delegation.delegate_prompt(role, Task("1", "1", "Task"), "Brief.", "")
                self.assertIn(delegation.delegate_rules(role), profile)
                self.assertIn(delegation.delegate_rules(role), prompt)
                self.assertIn("never write to any of them", profile)

    def test_native_wait_policy_belongs_only_to_the_coordinator_skill(self):
        """Delegates do not need the coordinator's event-driven waiting policy."""
        skill = (Path(workflow.__file__).parent / "skills/ab-work/SKILL.md").read_text()
        self.assertIn("independent useful work", skill)
        self.assertIn("configuration.native_agent_wait", skill)
        self.assertIn("if it times out while agents still run", skill)
        self.assertIn("shorter waits", skill)
        for role in ("worker", "scout", "reviewer"):
            with self.subTest(role=role):
                self.assertNotIn("wait_agent", delegation.profile_instructions(role))

    def test_no_execution_backend_carries_a_private_tracker_restriction(self):
        """Symmetry is the point: an env-gated guard would apply to one path only."""
        for module in ("provider_runner", "workflow", "tracker/core", "tracker/cli"):
            source = (Path(workflow.__file__).parent / (module + ".py")).read_text()
            self.assertNotIn("AGENT_BOX_ROLE", source)

    def test_claude_delegation_keeps_the_isolated_safe_mode_context(self):
        """Safe mode is why the instructions must travel; it stays deliberately on."""
        command = provider_runner.build_command({"fast": False, "provider": "claude", "model": "m", "effort": "high"},
                                                "worker", Path("/tmp"), Path("/tmp/out"))
        self.assertIn("--safe-mode", command)


class RepositoryInstructionTests(unittest.TestCase):
    def test_instructions_are_complete_and_invalid_utf8_is_explicit(self):
        """Rules beyond the old 16 KiB cutoff must reach managed delegates."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "CLAUDE.md").write_text("@AGENTS.md\n")
            path = root / "AGENTS.md"
            text = "Repository rule.\n" * 2000 + "FINAL_MANDATORY_RULE\n"
            path.write_text(text, encoding="utf-8")
            guidance = workflow.repository_instructions(root)
            self.assertIn(text, guidance)
            self.assertIn("@AGENTS.md", guidance)
            self.assertEqual(guidance.count("FINAL_MANDATORY_RULE"), 1)
            path.write_bytes(b"invalid UTF-8: \xff")
            with self.assertRaisesRegex(AgentBoxError, "Cannot read repository instruction AGENTS.md"):
                workflow.repository_instructions(root)

    def test_a_symlinked_instruction_file_is_not_followed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = repository(directory)
            secret = Path(directory) / "secret.md"
            secret.write_text("private note")
            (root / "AGENTS.md").symlink_to(secret)
            self.assertNotIn("private note", workflow.repository_instructions(root))
            self.assertNotIn("AGENTS.md", workflow.repository_instruction_paths(root))


if __name__ == "__main__":
    unittest.main()
