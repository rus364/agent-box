"""Workflow-controlled deliveries refuse whole payloads before their consumer sees them."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import re
from unittest.mock import patch

from agent_box import plan_sync, provider_runner, text_delivery, text_summary, workflow
from agent_box.errors import AgentBoxError
from test_text_delivery import delivered_bytes
from workflow_fixtures import BINDING, WorkflowCase, answer, candidate, managed_runner


class WorkflowTextDeliveryTests(WorkflowCase):
    def setUp(self):
        super().setUp()
        # A smaller explicit user limit must remain effective when package defaults grow.
        self.config.workflow.text_max_bytes = 65536
        for role in ("worker", "scout", "sync"):
            self.config.workflow.role(role).models = [candidate("codex", "fixture", "high")]
        self.config.workflow.review.policy = "always"
        self.config.workflow.review.incremental = "off"
        self.adapter.subtree.return_value = ["1"]
        usage = patch.object(workflow, "usage_snapshot", return_value={})
        usage.start()
        self.addCleanup(usage.stop)

    def test_all_assignment_routes_refuse_before_launch_pages_or_round_reservation(self):
        """A limit on a final page alone would still expose oversized assignments and spend money."""
        self.config.workflow.text_max_bytes = 64
        for command in (("delegate", "1", "--role", "worker", "--brief", str(self.review_brief)),
                        ("delegate", "1", "--role", "scout", "--brief", str(self.review_brief)),
                        ("review", "1"), ("sync", "1")):
            for native in ((), ("--native",)):
                with self.subTest(command=command, native=native), patch.object(workflow, "run") as run:
                    with self.assertRaisesRegex(AgentBoxError, "TEXT_LIMIT_EXCEEDED.*tell the user"):
                        self.run_workflow(*command, *native)
                    run.assert_not_called()
                    self.assertFalse(text_delivery.WORKFLOW_TEXTS.exists())
                    self.assertFalse(workflow.read_review_state(self.root, BINDING, self.task).get("pending"))
                    self.assertIsNone(plan_sync.last_result(self.root, self.task))

    def test_review_references_count_with_the_assignment_instead_of_resetting_its_budget(self):
        """Individually acceptable evidence files can exceed the combined delivery budget."""
        self.config.workflow.text_max_bytes = 16000
        self.task.description = "T" * 9000
        (self.root / "AGENTS.md").write_text("B" * 9000)
        workflow.gw.git(self.root, "add", ".")
        workflow.gw.git(self.root, "commit", "-m", "required instructions")
        with patch.object(workflow, "run") as run:
            with self.assertRaisesRegex(AgentBoxError, "including evidence"):
                self.native_round("--brief", str(self.review_brief))
        run.assert_not_called()
        self.assertFalse(list(text_delivery.WORKFLOW_TEXTS.glob("*/page-*.txt")))

    def test_reference_mode_preserves_original_reports_and_default_inline_schema(self):
        """The opt-in changes delivery, never the captured report or successful default fields."""
        args = ("delegate", "1", "--role", "scout", "--brief", str(self.review_brief))
        for report in ("Done", "日本語🙂" * 600):
            with patch.object(workflow, "run", return_value=report):
                inline = self.run_workflow(*args)
                result = self.run_workflow(*args, "--output", "reference")
            self.assertEqual(inline["response"], report)
            self.assertNotIn("response_text", inline)
            self.assertEqual(Path(result["response_path"]).read_bytes(), report.encode())
            if len(report.encode()) > text_delivery.PAGE_BYTES:
                self.assertEqual(delivered_bytes(result["response_text"]), report.encode())
                self.assertNotIn(report, result["response"])
            else:
                self.assertEqual(result["response"], report)
                self.assertNotIn("response_text", result)

    def test_refused_managed_reports_are_saved_without_inline_prefix_or_readable_locator(self):
        """Refusing consumption must preserve diagnostics without rerunning the paid provider."""
        report = "SENSITIVE_REPORT_PREFIX" + "x" * 65536
        self.config.workflow.scout.models = []
        for mode in ("inline", "reference"):
            with patch.object(workflow, "run", return_value=report) as run, \
                    contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(io.StringIO()):
                code = workflow.main(["delegate", "1", "--role", "worker", "--brief", str(self.review_brief),
                                      "--output", mode])
            self.assertEqual(code, 2)
            self.assertIn("TEXT_LIMIT_EXCEEDED", stdout.getvalue())
            self.assertNotIn("SENSITIVE_REPORT_PREFIX", stdout.getvalue())
            self.assertNotIn("response_path", stdout.getvalue())
            run.assert_called_once()
        self.assertTrue(any(p.read_text() == report for p in workflow.WORKFLOW_STATE.rglob("*.md")))
        self.assertFalse(list(text_delivery.WORKFLOW_TEXTS.glob("*/page-*.txt")))

    def test_fallback_reuses_exact_prepared_input_and_preparation_prunes_expired_records(self):
        """Changing provider cannot reset a delivery's budget or postpone cleanup forever."""
        with patch.object(text_delivery.time, "time", return_value=0):
            old = text_delivery.publish("old", self.config)
        self.config.workflow.scout.models.append(candidate("claude", "fallback", "high"))
        failure = provider_runner.ProviderFailure("unavailable", "fixture missing")
        with patch.object(workflow, "run", side_effect=[failure, "Done"]) as run:
            self.run_workflow("delegate", "1", "--role", "scout", "--brief", str(self.review_brief))
        self.assertEqual(run.call_args_list[0].args[3], run.call_args_list[1].args[3])
        self.assertFalse(Path(old["path"]).exists())

    def test_managed_review_refusal_survives_result_lookup_without_destroying_the_receipt(self):
        """A fresh result and a recovery lookup must enforce the same consumption boundary."""
        report = "Verdict: PASS\n" + "x" * 65536
        self.config.workflow.scout.models = []
        with patch.object(workflow, "run", return_value=answer(report)):
            with self.assertRaisesRegex(AgentBoxError, "TEXT_LIMIT_EXCEEDED.*reviewer report"):
                self.run_workflow("review", "1")
        state = workflow.read_review_state(self.root, BINDING, self.task)
        self.assertEqual(state["used"], 1)
        self.assertEqual(state["results"][-1]["status"], "saved")
        with self.assertRaisesRegex(AgentBoxError, "TEXT_LIMIT_EXCEEDED"):
            self.run_workflow("review-result", "1")
        self.assertNotIn("review", workflow.read_evidence(self.root, self.task))
        self.config.workflow.text_max_bytes = 131072
        result = self.run_workflow("review-result", "1", "--reconcile")
        self.assertEqual(json.loads(Path(result["result_path"]).read_text())["output"]["report"]["text"], report)
        self.assertEqual(delivered_bytes(result["report_text"]), report.encode())
        self.assertEqual(result["reconciliation"]["status"], "applied")

    def test_sync_reference_and_overflow_keep_the_saved_report_and_partial_write_status(self):
        """Plan sync may have written tasks before output refusal; never disguise it as safe retry."""
        report = "日本語" * 700 + "\nSync: UNCHANGED\n"
        with patch.object(workflow, "run", return_value=report), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(workflow.main(["sync", "1", "--output", "reference"]), 0)
        result = json.loads(stdout.getvalue())
        self.assertEqual(delivered_bytes(result["report_text"]), report.encode())
        self.assertEqual(plan_sync.last_result(self.root, self.task)["report"], report)
        oversized = "x" * 65536 + "\nSync: UPDATED\n"
        self.config.workflow.scout.models = []
        with patch.object(workflow, "run", return_value=oversized):
            with self.assertRaisesRegex(AgentBoxError, "task writes may have completed.*TEXT_LIMIT_EXCEEDED"):
                self.run_workflow("sync", "1")
        saved = plan_sync.last_result(self.root, self.task)
        self.assertEqual(saved["status"], "ERROR")
        self.assertEqual(saved["report"], oversized)
        with self.assertRaisesRegex(AgentBoxError, "TEXT_LIMIT_EXCEEDED"):
            self.run_workflow("inspect", "1")

    def test_native_sync_answer_remains_direct_and_inspectable_above_the_managed_limit(self):
        """Recording an already delivered native answer must not turn success into an overflow."""
        report = "x" * 65536 + "\nSync: UPDATED\n"
        self.run_workflow("sync", "1", "--native")
        answer_file = self.root.parent / "sync-answer.md"
        answer_file.write_text(report)
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            code = workflow.main(["sync", "1", "--result", str(answer_file), "--output", "reference"])
        self.assertEqual(code, 0)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["report"], report)
        self.assertNotIn("report_text", result)
        for legacy in (False, True):
            if legacy:
                result.pop("backend")
                plan_sync.save(self.root, self.task, result)
            self.assertEqual(self.run_workflow("inspect", "1")["plan_sync"]["last_result"]["report"], report)

    def test_sync_reference_pages_reconciliation_without_changing_saved_evidence(self):
        """Recovery evidence needs the same bounded transport as an executor's original report."""
        original = "Provider stopped after partial task writes.\nSync: ERROR\n"
        recovery = "Reread affected tasks. " * 2700 + "\nSync: UPDATED\n"
        self.report.write_text(recovery)
        with patch.object(workflow, "run", return_value=original):
            self.run_workflow("sync", "1")
        with patch.object(workflow, "run") as run, contextlib.redirect_stdout(io.StringIO()) as stdout:
            code = workflow.main(["sync", "1", "--reconcile", str(self.report), "--output", "reference"])
        self.assertEqual(code, 0)
        run.assert_not_called()
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["report"], original)
        self.assertLess(len(stdout.getvalue().encode()), text_delivery.PAGE_BYTES)
        self.assertEqual(delivered_bytes(result["reconciliation"]["report_text"]), recovery.encode())
        saved = plan_sync.last_result(self.root, self.task)
        self.assertEqual((saved["report"], saved["reconciliation"]["report"]), (original, recovery))
        self.assertNotIn("report_text", saved["reconciliation"])

    def test_large_diff_is_never_injected_into_any_native_or_managed_role_assignment(self):
        """Agents choose source/diff ranges themselves; even an enormous diff needs only compact context."""
        marker = "DIFF_CONTENT_MUST_STAY_OUT_OF_ASSIGNMENTS"
        (self.root / "change.txt").write_text((marker + "\n") * 10000)
        workflow.gw.git(self.root, "add", ".")
        workflow.gw.git(self.root, "commit", "-m", "large separate diff")
        commands = [("delegate", "1", "--role", role, "--brief", str(self.review_brief))
                    for role in ("worker", "scout")] + [("review", "1"), ("sync", "1")]
        for command in commands:
            with self.subTest(command=command):
                native = self.run_workflow(*command, "--native")
                prompt = delivered_bytes(native["input"]).decode()
                self.assertNotIn(marker, prompt)
                self.assertLess(len(prompt.encode()), 65536)
                if command[0] == "review":
                    payload = json.JSONDecoder().raw_decode(prompt.split("Delegation handoff:\n", 1)[1])[0]
                    manifest = json.loads(Path(payload["manifest"]["path"]).read_text())
                    self.assertIn(marker, Path(manifest["artifacts"]["full.diff"]["path"]).read_text())
                    self.native_result()
                elif command[0] == "sync":
                    self.report.write_text("No drift.\nSync: UNCHANGED\n")
                    self.run_workflow("sync", "1", "--result", str(self.report))
                report = answer() if command[0] == "review" else "Done\nSync: UNCHANGED\n"
                with patch.object(workflow, "run", return_value=report) as run:
                    self.run_workflow(*command)
                run.assert_called_once()
                self.assertNotIn(marker, run.call_args.args[3])

    def test_mass_renames_keep_native_and_managed_assignments_bounded_without_losing_evidence(self):
        """A path inventory larger than the budget must not prevent a complete structural review."""
        package = self.root / "package"
        package.mkdir()
        for index in range(408):
            (package / f"module_{index:03d}_{'x' * 90}.py").write_text(f"value = {index}\n")
        instruction = "Preserve all public imports during relocation."
        (self.root / "AGENTS.md").write_text(instruction + "\n")
        (self.root / "settings.py").write_text("enabled = False\n")
        workflow.gw.git(self.root, "add", ".")
        workflow.gw.git(self.root, "commit", "-m", "original package")
        workflow.gw.git(self.root, "branch", "-f", "main", "HEAD")
        base = workflow.gw.head_sha(self.root)
        (self.root / "python").mkdir()
        package.rename(self.root / "python/package")
        (self.root / "settings.py").write_text("enabled = True\n")
        workflow.gw.git(self.root, "add", ".")
        workflow.gw.git(self.root, "commit", "-m", "move package and enable it")
        head = workflow.gw.head_sha(self.root)
        diff_range = f"{base}...{head}"
        inventory = workflow.gw.git(self.root, "diff", "--name-status", "--find-renames", diff_range, "--")
        diff = workflow.gw.git(self.root, "diff", "--find-renames", diff_range, "--")
        self.assertEqual(sum(line.startswith("R100\t") for line in inventory.splitlines()), 408)
        self.assertGreater(len(inventory.encode()), self.config.workflow.text_max_bytes)

        def check_prompt(prompt):
            self.assertLessEqual(len(prompt.encode()), 65536)
            self.assertIn("requirement", prompt)
            self.assertIn(instruction, prompt)
            self.assertIn("changed-paths.txt", prompt)
            self.assertNotIn("module_000_", prompt)
            self.assertNotIn("+enabled = True", prompt)
            payload = json.JSONDecoder().raw_decode(prompt.split("Delegation handoff:\n", 1)[1])[0]
            self.assertEqual(payload["boundaries"]["diff_range"], diff_range)
            self.assertIn("409 files changed", payload["change_summary"])
            manifest = json.loads(Path(payload["manifest"]["path"]).read_text())
            for name, expected in (("changed-paths.txt", inventory), ("full.diff", diff)):
                artifact = manifest["artifacts"][name]
                self.assertEqual(Path(artifact["path"]).read_bytes(), expected.encode())
                self.assertEqual(artifact["sha256"], hashlib.sha256(expected.encode()).hexdigest())

        opened = self.native_round()
        check_prompt(delivered_bytes(opened["input"]).decode())
        self.native_result()

        def provider(route, role, root, prompt, **kwargs):
            check_prompt(prompt)
            return answer()

        with patch.object(workflow, "run", side_effect=managed_runner(provider)) as run:
            self.assertEqual(self.run_workflow("review", "1")["verdict"], "PASS")
            run.assert_called_once()

    def test_mandatory_nested_instruction_imports_refuse_all_routes_before_paid_work(self):
        """Moving an instruction into an unchanged imported file cannot evade the aggregate input limit."""
        (self.root / "docs").mkdir()
        (self.root / "AGENTS.md").write_text("@docs/policy.md\n")
        (self.root / "docs/policy.md").write_text("@large.md\n")
        (self.root / "docs/large.md").write_text("x" * 65536)
        workflow.gw.git(self.root, "add", ".")
        workflow.gw.git(self.root, "commit", "-m", "mandatory imported instructions")
        for command in (("delegate", "1", "--role", "worker", "--brief", str(self.review_brief)),
                        ("delegate", "1", "--role", "scout", "--brief", str(self.review_brief)),
                        ("review", "1"), ("sync", "1")):
            for flags in ((), ("--native",)):
                with self.subTest(command=command, flags=flags), patch.object(workflow, "run") as run:
                    with self.assertRaisesRegex(AgentBoxError, "TEXT_LIMIT_EXCEEDED.*including imports"):
                        self.run_workflow(*command, *flags)
                    run.assert_not_called()
        self.assertFalse(list(text_delivery.WORKFLOW_TEXTS.glob("*/page-*.txt")))
        self.assertFalse(workflow.read_review_state(self.root, BINDING, self.task).get("pending"))

    def test_separate_path_and_diff_evidence_stays_bound_to_the_review_round(self):
        """On-demand evidence must not accept the original head after its paths or diff are altered."""
        for name in ("full.diff", "changed-paths.txt"):
            with self.subTest(artifact=name):
                opened = self.native_round()
                prompt = delivered_bytes(opened["input"]).decode()
                payload = json.JSONDecoder().raw_decode(prompt.split("Delegation handoff:\n", 1)[1])[0]
                manifest = json.loads(Path(payload["manifest"]["path"]).read_text())
                source = Path(manifest["artifacts"][name]["path"])
                source.chmod(0o600)
                source.write_text("Changed after dispatch")
                source.chmod(0o400)
                with self.assertRaisesRegex(AgentBoxError, "Review evidence changed"):
                    self.native_result()
                self.assertNotIn("review", workflow.read_evidence(self.root, self.task))

    def test_instruction_imports_preserve_bytes_skip_examples_and_bound_cycles_and_depth(self):
        """Only actual imports add mandatory context; aliases and examples cannot duplicate or invent it."""
        (self.root / "docs").mkdir()
        policy = self.root / "docs/policy file.md"
        raw = "日本語 policy\r\n@../AGENTS.md\r\n".encode()
        policy.write_bytes(raw)
        (self.root / "AGENTS.md").write_text(
            "@docs/policy\\ file.md\n`@missing-inline.md`\n```text\n@missing-fence.md\n```\n")
        (self.root / "CLAUDE.md").write_text("@AGENTS.md\n")
        workflow.gw.git(self.root, "add", ".")
        workflow.gw.git(self.root, "commit", "-m", "imported instruction fixture")
        evidence = workflow.repository_instruction_evidence(self.root, self.config.workflow)
        self.assertEqual(dict(evidence)["docs/policy file.md"].encode(), raw)
        self.assertEqual(len(evidence), 3)
        opened = self.native_round()
        prompt = delivered_bytes(opened["input"]).decode()
        self.assertIn(raw.decode(), prompt)
        self.native_result()
        for index in range(6):
            (self.root / f"chain{index}.md").write_text(f"@chain{index + 1}.md\n" if index < 5 else "end")
        (self.root / "AGENTS.md").write_text("@chain0.md\n")
        with self.assertRaisesRegex(AgentBoxError, "four hops"):
            workflow.repository_instruction_evidence(self.root, self.config.workflow)
        (self.root / "AGENTS.md").write_text("@missing.md\n")
        with self.assertRaisesRegex(AgentBoxError, "missing"):
            workflow.repository_instruction_evidence(self.root, self.config.workflow)
        (self.root / "missing.md").symlink_to(policy)
        with self.assertRaisesRegex(AgentBoxError, "symlinked"):
            workflow.repository_instruction_evidence(self.root, self.config.workflow)

    def test_scout_compacts_only_notes_and_preserves_native_managed_assignment_parity(self):
        """Large supplemental notes must not replace authoritative requirements or leak into the target prompt."""
        source = "Long supplemental notes. " * 4000
        self.review_brief.write_text(source)
        paths = []
        def summarize(route, role, root, prompt, **kwargs):
            self.assertEqual(role, "scout")
            first = Path(json.loads(re.search(r'starting at ("[^"]+")', prompt)[1]))
            paths.append(first.parent)
            artifact = {"path": str(first), "id": first.parent.name, "bytes": len(source.strip().encode()),
                        "pages": len(list(first.parent.glob("page-*.txt")))}
            self.assertEqual(delivered_bytes(artifact), source.strip().encode())
            self.assertNotIn(source.strip(), prompt)
            return "Scope: verify the documented boundary."
        with patch.object(workflow, "run", side_effect=summarize):
            native = self.run_workflow("delegate", "1", "--role", "worker", "--brief", str(self.review_brief), "--native")
        native_prompt = delivered_bytes(native["input"]).decode()
        self.assertIn(self.task.description, native_prompt)
        self.assertIn("Scout summary", native_prompt)
        def managed(route, role, root, prompt, **kwargs):
            if role == "scout":
                return summarize(route, role, root, prompt, **kwargs)
            self.assertEqual(prompt, native_prompt)
            return "Done"
        with patch.object(workflow, "run", side_effect=managed) as run:
            self.run_workflow("delegate", "1", "--role", "worker", "--brief", str(self.review_brief))
        self.assertEqual(run.call_count, 2)
        self.assertTrue(all(not path.exists() for path in paths))
        self.assertEqual(self.review_brief.read_text(), source)

    def test_report_summaries_keep_originals_and_recovery_reads_do_not_call_models(self):
        """A paid summary is saved once; review identity and Plan sync outcome still come from the original."""
        finding = "- [HIGH] change.py:1 — Preserve the boundary."
        report = "Verdict: NEEDS_WORK\n" + finding + "\n" + "x" * 70000
        summary = "Verdict: NEEDS_WORK\n" + finding + "\nConfirmed by source inspection."
        with patch.object(workflow, "run", side_effect=[answer(report), summary]) as run:
            result = self.run_workflow("review", "1")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(result["verdict"], "NEEDS_WORK")
        self.assertIn(finding, result["report"])
        self.assertEqual(json.loads(Path(result["result_path"]).read_text())["output"]["report"]["text"], report)
        with patch.object(workflow, "run") as run:
            repeated = self.run_workflow("review-result", "1")
        run.assert_not_called()
        self.assertEqual(repeated["report"], result["report"])
        raw_sync = "x" * 70000 + "\nSync: UPDATED\n"
        with patch.object(workflow, "run", side_effect=[raw_sync, "Saved corrections.\nSync: UPDATED\n"]):
            result = self.run_workflow("sync", "1")
        self.assertEqual(plan_sync.last_result(self.root, self.task)["report"], raw_sync)
        self.assertIn("Scout summary", result["report"])
        self.assertNotIn("report_delivery", result)
        with patch.object(workflow, "run") as run:
            self.assertEqual(self.run_workflow("inspect", "1")["plan_sync"]["last_result"]["report"], result["report"])
        run.assert_not_called()

    def test_summary_failure_never_recurses_or_weakens_required_outcomes(self):
        """An invalid or oversized scout answer stops once, keeping the original available to its owner."""
        source = "x" * 70000 + "\nSync: UPDATED\n"
        for answer_text in ("Sync: UNCHANGED", "x" * 70000, ""):
            with self.subTest(answer=answer_text[:30]), patch.object(workflow, "run", return_value=answer_text) as run:
                with self.assertRaisesRegex(AgentBoxError, "TEXT_SUMMARY_FAILED"):
                    text_summary.compact(source, self.config, self.root, label="fixture", protected=["Sync: UPDATED"])
                run.assert_called_once()

    def test_worker_and_scout_summaries_preserve_exact_findings_before_delivery(self):
        """A short summary may neither hide a real finding nor invent or change its identity."""
        finding = "- [MEDIUM] change.py:7 — Preserve this finding and its evidence."
        verdict = "Verdict: NEEDS_WORK"
        report = verdict + "\n" + finding + "\n" + "x" * 70000
        for role in ("worker", "scout"):
            args = ("delegate", "1", "--role", role, "--brief", str(self.review_brief), "--output", "reference")
            for summary in (verdict, verdict + "\n" + finding.replace(":7", ":8"),
                            verdict + "\n" + finding + "\n- [LOW] extra.py:1 — Invented.", finding):
                with self.subTest(role=role, summary=summary), patch.object(workflow, "run", side_effect=[report, summary]) as run:
                    with self.assertRaisesRegex(AgentBoxError, "TEXT_SUMMARY_FAILED"):
                        self.run_workflow(*args)
                self.assertEqual(run.call_count, 2)
            with patch.object(workflow, "run", side_effect=[report, verdict + "\n" + finding + "\nEvidence retained."]) as run:
                result = self.run_workflow(*args)
            self.assertEqual(run.call_count, 2)
            self.assertIn(finding, result["response"])
            self.assertEqual(Path(result["response_path"]).read_text(), report)

    def test_exhausted_review_budget_never_spends_a_scout_call_on_preparation(self):
        """Preparing a summary cannot spend provider budget when the target round is already forbidden."""
        workflow.save_review_state(self.root, BINDING, self.task, {"used": self.config.workflow.review.max_rounds})
        self.review_brief.write_text("x" * 70000)
        for flags in ((), ("--native",)):
            with self.subTest(flags=flags), patch.object(workflow, "run") as run:
                with self.assertRaisesRegex(AgentBoxError, "Review attempt budget exhausted"):
                    self.run_workflow("review", "1", "--brief", str(self.review_brief), *flags)
                run.assert_not_called()

    def test_summary_source_bound_and_role_behavior_apply_before_provider_work(self):
        """The cheap route has a finite source bound and receives this project's selected scout behavior."""
        with patch.object(workflow, "run") as run:
            with self.assertRaisesRegex(AgentBoxError, "scout input bound"):
                text_summary.compact("x" * (provider_runner.RESPONSE_LIMIT + 1), self.config, self.root, label="fixture")
        run.assert_not_called()
        for version in ("v1", "v2"):
            self.config.workflow.behavior_version = version
            with patch.object(workflow, "run", return_value="Short verified summary.") as run:
                text_summary.compact("x" * 70000, self.config, self.root, label="fixture")
            self.assertIn(f"Workflow behavior: {version} (scout)", run.call_args.args[3])

    def test_sync_summaries_preserve_formatted_outcomes_without_requiring_terminal_position(self):
        """Summaries may move an explicit outcome, but cannot hide or invent another marker."""
        marker = " \t`Sync: UPDATED` \t"
        original = "x" * 70000 + "\n" + marker
        summary = marker + "\nTask 2 correction saved and reread."
        result = {"report": original, "backend": "managed"}
        with patch.object(workflow, "run", return_value=summary) as run:
            plan_sync.prepare_report(self.root, self.config, result)
        run.assert_called_once()
        self.assertEqual(result["report"], original)
        delivered = result["report_delivery"]["text"]
        self.assertIn(marker, delivered.splitlines())
        self.assertEqual(plan_sync.outcome(delivered), "UPDATED")
        for invalid in (summary.replace(marker, "Sync: UPDATED"),
                        summary + "\n**Sync: ERROR", summary + "\nSync: UNKNOWN"):
            with self.subTest(summary=invalid), patch.object(workflow, "run", return_value=invalid):
                with self.assertRaisesRegex(AgentBoxError, "TEXT_SUMMARY_FAILED"):
                    plan_sync.prepare_report(self.root, self.config, {"report": original})

    def test_failed_sync_summary_keeps_reconciliation_retryable_and_preserves_malformed_original(self):
        """Recovery must survive a failed delivery without losing evidence or repeating tracker writes."""
        original = "x" * 70000
        with patch.object(workflow, "run", return_value=original):
            with self.assertRaisesRegex(AgentBoxError, "missing standalone Sync line"):
                self.run_workflow("sync", "1")
        self.report.write_text("Reread affected tasks; all required changes were saved.\nSync: UPDATED\n")
        with patch.object(workflow, "run", return_value=""):
            with self.assertRaisesRegex(AgentBoxError, "reconciliation evidence saved"):
                self.run_workflow("sync", "1", "--reconcile", str(self.report))
        saved = plan_sync.last_result(self.root, self.task)
        self.assertEqual((saved["status"], saved["report"]), ("ERROR", original))
        self.assertEqual(saved["reconciliation"]["report"], self.report.read_text())
        with patch.object(workflow, "run", return_value="The original contained no outcome marker.") as run:
            result = self.run_workflow("sync", "1", "--reconcile", str(self.report))
        run.assert_called_once()
        self.assertEqual(result["status"], "UPDATED")
        self.assertEqual(plan_sync.last_result(self.root, self.task)["report"], original)
