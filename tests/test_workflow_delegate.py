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
from agent_box.config import BalanceConfig
from agent_box.errors import AgentBoxError
from agent_box.tracker.core import Task
from test_external_config import external_config
from workflow_fixtures import BINDING, WorkflowCase, candidate, repository, routing_session

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

    def test_balanced_delegates_consume_one_turn_for_each_native_or_managed_launch(self):
        """A native turn must advance the same rotation so the next run returns to the other CLI."""
        self.config.workflow.balance = BalanceConfig("weighted", {"claude": 1, "codex": 1})
        for role in ("worker", "scout"):
            models = [{**candidate("claude", "first", "high"), "speed": "standard"},
                      candidate("codex", "second", "high")]
            if role == "worker":
                self.config.workflow.worker.tiers["1"] = models
            else:
                self.config.workflow.scout.models = models
            with self.subTest(role=role), routing_session("codex"), \
                    patch.object(workflow, "balance_vendor", wraps=workflow.balance_vendor) as turn, \
                    patch.object(workflow, "run", return_value=ANSWER) as run:
                first = self.delegate(role)
                opened = self.delegate(role)
                third = self.delegate(role)
                self.assertEqual(turn.call_count, 3)
                self.assertEqual(run.call_count, 2)
            self.assertEqual((first["route"]["provider"], third["route"]["provider"]), ("claude", "claude"))
            self.assertEqual(opened["launch"], "native")
            self.assertEqual(opened["profile"], "ab-worker-1" if role == "worker" else "ab-scout")
            self.assertEqual((opened["route"]["provider"], opened["route"]["model"]), ("codex", "second"))
            self.assertNotIn("native_limitation", opened["route"])
            self.assertIn(opened["input"]["path"], opened["message"])

    def test_balanced_delegates_preserve_explicit_speed_in_a_managed_launch(self):
        """A matching CLI must not inherit session speed when the selected candidate requests standard speed."""
        self.config.workflow.balance = BalanceConfig("weighted", {"codex": 1})
        for role in ("worker", "scout"):
            self.config.workflow.role(role).models = [candidate("claude", "first", "high"),
                {**candidate("codex", "standard", "high"), "speed": "standard"}]
            with self.subTest(role=role), routing_session("codex"), \
                    patch.object(workflow, "run", return_value=ANSWER) as run:
                result = self.delegate(role)
            self.assertEqual(result["route"]["speed"], "standard")
            self.assertEqual(run.call_count, 1)

    def test_sole_weighted_delegate_uses_native_only_when_its_session_is_detected(self):
        """A weighted vendor behind the opener can use its profile, but an unknown session must run managed."""
        self.config.workflow.balance = BalanceConfig("weighted", {"claude": 0, "codex": 1})
        for role in ("worker", "scout"):
            self.config.workflow.role(role).models = [candidate("claude", "first", "high"),
                                                       candidate("codex", "weighted", "high")]
            for provider in ("codex", None):
                with self.subTest(role=role, provider=provider), routing_session(provider), \
                        patch.object(workflow, "balance_vendor", side_effect=AssertionError("sole vendor spent a counter")), \
                        patch.object(workflow, "run", return_value=ANSWER) as run:
                    result = self.delegate(role)
                self.assertEqual(result["route"]["model"], "weighted")
                self.assertEqual(result.get("launch"), "native" if provider else None)
                self.assertEqual(run.call_count, 0 if provider else 1)

    def test_balanced_delegate_preserves_reserve_selection_over_the_native_profile(self):
        """A later model of the session CLI must run managed when its installed profile names the exhausted model."""
        self.config.workflow.balance = BalanceConfig("weighted", {"codex": 1})
        exhausted = {"windows": [{"remaining": 0, "reset_at": 4102444800, "models": ["profile"]}]}
        for role in ("worker", "scout"):
            self.config.workflow.role(role).models = [candidate("claude", "first", "high"),
                                                       candidate("codex", "profile", "high"),
                                                       candidate("codex", "reserve", "high")]
            with self.subTest(role=role), routing_session("codex"), \
                    patch.object(workflow, "usage_snapshot", return_value=exhausted), \
                    patch.object(workflow, "run", return_value=ANSWER) as run:
                result = self.delegate(role)
            self.assertEqual(result["route"]["model"], "reserve")
            self.assertEqual(run.call_count, 1)
            self.assertNotIn("launch", result)

    def test_balanced_external_pick_cannot_use_a_same_model_native_profile(self):
        """Matching model, effort and CLI must not discard an explicitly selected external API identity."""
        self.config.agents.external_apis = external_config().agents.external_apis
        self.config.workflow.balance = BalanceConfig("weighted", {"deepseek": 1})
        for role in ("worker", "scout"):
            spec = candidate("codex", "external-small", "high")
            self.config.workflow.role(role).models = [spec, {**spec, "api_provider": "deepseek"}]
            with self.subTest(role=role), routing_session("codex"), \
                    patch.object(workflow, "run", return_value=ANSWER) as run:
                result = self.delegate(role)
            self.assertEqual(result["route"]["api_provider"], "deepseek")
            self.assertEqual(run.call_count, 1)
            self.assertNotIn("launch", result)

    def test_balanced_delegate_refuses_explicit_native_without_consuming_a_turn(self):
        """Explicit native cannot bypass selection, even when the calling CLI is undetected."""
        self.config.workflow.balance = BalanceConfig("weighted", {"claude": 1, "codex": 1})
        for role in ("worker", "scout"):
            self.config.workflow.role(role).models = [candidate("claude", "first", "high"),
                                                       candidate("codex", "second", "high")]
            with self.subTest(role=role), routing_session(None), \
                    patch.object(workflow, "candidates", side_effect=AssertionError("selection consumed")):
                with self.assertRaisesRegex(AgentBoxError, f"run agent-box workflow delegate 1 --role {role} "
                                                          "--brief FILE --output reference$"):
                    self.run_workflow("delegate", "1", "--role", role, "--brief", str(self.review_brief), "--native")

    def test_balanced_delegate_cli_prints_a_native_handoff_as_json(self):
        """The plain command can return a handoff instead of the Markdown response its stdout path expects."""
        self.config.workflow.balance = BalanceConfig("weighted", {"claude": 1})
        with routing_session("claude"), patch.object(workflow, "run") as run, \
                contextlib.redirect_stdout(io.StringIO()) as stdout, \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            code = workflow.main(["delegate", "1", "--role", "scout", "--brief", str(self.review_brief)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["profile"], "ab-scout")
        self.assertEqual(json.loads(stdout.getvalue())["launch"], "native")
        self.assertEqual(stderr.getvalue(), "")
        run.assert_not_called()

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

    def test_worker_quota_failure_with_a_clean_tree_tries_the_next_candidate(self):
        """A quota failure without edits can use the next candidate without risking unfinished work."""
        self.config.workflow.worker.models.append(candidate("codex", "worker-fallback", "high"))
        failure = provider_runner.ProviderFailure("Quota exhausted", "quota", stage="execution")
        self.assertTrue(gw._worktree_clean(self.root))
        with patch.object(workflow, "run", side_effect=[failure, ANSWER]) as run:
            result = self.delegate("worker")
        self.assertEqual([call.args[0]["model"] for call in run.call_args_list], ["worker-model", "worker-fallback"])
        self.assertEqual(run.call_args_list[0].args[3], run.call_args_list[1].args[3])
        self.assertEqual((result["route"]["model"], result["response"]), ("worker-fallback", ANSWER))
        self.assertEqual(result["failures"][0]["category"], "quota")

    def test_worker_quota_failure_with_edits_stops_without_running_a_fallback(self):
        """Even a quota error can follow partial edits; another worker must not restart over those files."""
        self.config.workflow.worker.models.append(candidate("codex", "worker-fallback", "high"))
        changed = self.root / "change.py"

        def provider(*args, **kwargs):
            changed.write_text("unfinished = True\n")
            raise provider_runner.ProviderFailure("Quota exhausted", "quota", stage="execution")

        with patch.object(workflow, "run", side_effect=provider) as run, \
             self.assertRaisesRegex(workflow.ProviderFailures, "Worker left changes before failure") as caught:
            self.delegate("worker")
        run.assert_called_once()
        self.assertEqual(changed.read_text(), "unfinished = True\n")
        self.assertEqual(caught.exception.failures[0]["category"], "quota")

    def test_clean_workers_still_stop_on_failures_other_than_quota_or_unavailability(self):
        """A clean tree alone does not authorize retrying authentication, settings or execution errors."""
        self.config.workflow.worker.models.append(candidate("codex", "worker-fallback", "high"))
        for category in ("authentication", "timeout", "provider-error", "unsupported", "invalid-report"):
            failure = provider_runner.ProviderFailure("Worker failed", category, stage="execution")
            with self.subTest(category=category), patch.object(workflow, "run", side_effect=failure) as run, \
                 self.assertRaisesRegex(workflow.ProviderFailures, "reconcile files and task progress"):
                self.delegate("worker")
            run.assert_called_once()
            self.assertTrue(gw._worktree_clean(self.root))

    def test_refusal_before_launch_still_names_its_reason_and_diagnostic(self):
        """Settings refused before a process starts still name why and where the private diagnostic is."""
        self.config.workflow.scout.models = [candidate("claude", "claude-opus-5", "high") | {"speed": "fast"}]
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
        The fixture runs from a Claude session, so only a claude profile is its native route.
        """
        self.config.workflow.worker.tiers["1"] = [candidate("codex", "worker-tier1", "high")]
        self.config.workflow.scout.models = []
        for use_tiers, worker in ((False, {"mode": "delegate", "models": "workflow.worker.models",
                                           "native": {"claude": "ab-worker"}, "for_this_session": "native"}),
                                  (True, {"mode": "delegate", "models": "workflow.worker.tiers.1",
                                          "native": {"codex": "ab-worker-1"}, "for_this_session": "managed"})):
            self.config.workflow.use_tiers = use_tiers
            with self.subTest(use_tiers=use_tiers):
                execution = self.run_workflow("inspect", "1")["execution"]
                self.assertEqual(execution["worker"], {
                    **worker, "managed": "agent-box workflow delegate 1 --role worker --brief FILE --output reference"})
                self.assertEqual(execution["scout"], {"mode": "self", "models": "workflow.scout.models"})
        self.config.workflow.use_tiers = False
        self.config.workflow.worker.models = [{**candidate("claude", "worker-model", "high"), "api_provider": "deepseek"}]
        self.config.workflow.scout.models = [candidate("codex", "scout-model", "high"),
                                             candidate("claude", "scout-fallback", "low")]
        execution = self.run_workflow("inspect", "1")["execution"]
        self.assertEqual((execution["worker"]["native"], execution["scout"]["native"]), ({}, {"codex": "ab-scout"}))

    def test_a_native_assignment_is_refused_where_the_sessions_route_is_managed(self):
        """The scout list opens with codex, so a Claude session's same-named profile is the fallback."""
        inspected = self.run_workflow("inspect", "1")
        self.assertEqual((inspected["session_provider"], inspected["execution"]["scout"]["for_this_session"]),
                         ("claude", "managed"))
        with self.assertRaisesRegex(AgentBoxError, r"managed for this claude session.*"
                                                   r"run agent-box workflow delegate 1 --role scout"):
            self.run_workflow("delegate", "1", "--role", "scout", "--brief", str(self.review_brief), "--native")
        with self.assertRaisesRegex(AgentBoxError, "pass it with --native"):
            self.run_workflow("delegate", "1", "--role", "scout", "--brief", str(self.review_brief),
                              "--provider", "codex")
        self.run_as("codex")
        opened = self.run_workflow("delegate", "1", "--role", "scout", "--brief", str(self.review_brief), "--native")
        self.assertEqual(opened["route"]["native"], {"codex": "ab-scout"})

    def test_inspect_exposes_the_root_native_wait_policy(self):
        """The coordinator can follow the global switch without passing it to delegates."""
        self.config.agents.codex.native_agent_wait.enabled = False
        self.config.agents.codex.native_agent_wait.interval_seconds = 900
        summary = self.run_workflow("inspect", "1")["configuration"]
        self.assertEqual(summary["native_agent_wait"], {"enabled": False, "interval_seconds": 900})

    def test_reviewer_native_route_preserves_candidate_order_and_effective_tier(self):
        """A matching session uses the first candidate's profile, never a later fallback."""
        self.config.workflow.review.models = [candidate("codex", "gpt-5.6-luna", "max"),
                                              candidate("claude", "fallback", "high")]
        self.config.workflow.review.tiers["1"] = [candidate("claude", "tier-reviewer", "high")]
        for tiered, native, source, session in (
                (False, {"codex": "ab-reviewer"}, "workflow.review.models", "managed"),
                (True, {"claude": "ab-reviewer-1"}, "workflow.review.tiers.1", "native")):
            self.config.workflow.use_tiers = tiered
            with self.subTest(tiered=tiered):
                self.assertEqual(self.run_workflow("inspect", "1")["execution"]["reviewer"],
                                 {"mode": "delegate", "models": source, "native": native,
                                  "for_this_session": session, "managed": "agent-box workflow review 1"})
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

    def test_explicit_speed_requires_managed_for_every_role(self):
        """Native agents inherit speed; explicit requests require their own process."""
        for role in ('worker', 'scout', 'reviewer'):
            for provider, model in (('claude', 'claude-opus-5'), ('codex', 'gpt-5.6-luna')):
                for speed in ('inherit', 'standard', 'fast'):
                    with self.subTest(role=role, provider=provider, speed=speed):
                        self.config.workflow.role(role).models = [{**candidate(provider, model, 'high'), 'speed': speed}]
                        route = self.run_workflow('inspect', '1')['execution'][role]
                        native = {provider: 'ab-' + role} if speed == 'inherit' else {}
                        self.assertEqual(route['native'], native)
                        if speed != 'inherit':
                            self.assertIn('managed CLI to request ' + speed, route['native_limitation'])
                        self.assertIn('agent-box workflow', route['managed'])

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
        self.assertEqual(self.run_workflow("finish", "1")["state"], "In review")
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
                profile = body("claude", role, 1, {"speed": "inherit", "model": "m", "effort": "high"})
                prompt = delegation.delegate_prompt(role, Task("1", "1", "Task"), "Brief.", "", behavior_version="v2")
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
        command = provider_runner.build_command({"speed": "inherit", "provider": "claude", "model": "m", "effort": "high"},
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


class SessionProviderTests(unittest.TestCase):
    def test_the_session_is_read_from_each_clis_own_marker_and_never_guessed(self):
        """CODEX_HOME is set in every box, so only CLAUDECODE and CODEX_THREAD_ID name the calling CLI.

        Both markers mean one CLI runs inside the other; naming either could pick
        the wrong route, so the coordinator must say which with --provider.
        """
        import os
        cases = (({"CLAUDECODE": "1"}, "claude"), ({"CODEX_THREAD_ID": "t"}, "codex"),
                 ({"CLAUDECODE": "1", "CODEX_THREAD_ID": "t"}, None), ({"CODEX_HOME": "/x"}, None))
        for markers, expected in cases:
            with self.subTest(markers=markers):
                environment = {key: value for key, value in os.environ.items()
                               if key not in ("CLAUDECODE", "CODEX_THREAD_ID")}
                with patch.dict(os.environ, {**environment, **markers}, clear=True):
                    self.assertEqual(workflow.session_provider(), expected)
