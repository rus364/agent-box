"""A tracker-writing delegate reads its own sources; Git remains read-only."""
import fnmatch
import hashlib
import re
import subprocess
from pathlib import Path

from . import migrations
from .errors import AgentBoxError
from .tracker.core import root_task
from .workflow_state import lock


RULES = """Act as the sync executor. Use tracker CLI; never invoke workflow commands or the coordinator's ab-sync skill. Read AGENTS.md/CLAUDE.md and imports. Read source with agent-box tracker get ID --no-comments; inspect decisions, commits and referenced sections. Use tracker search with separate path/anchor/term substrings; follow relations, including unreferenced tasks. Search defaults to open; --include-done finds history. Respect recursive scope; use get --no-comments for plausible candidates, not every task; comments ID only for missing evidence. Never fetch task URLs directly.
Edit open tasks in scope yourself, including Backlog, Ready, In progress and In review. Only Done is read-only history; superseded text never blocks. Change only tracker descriptions/comments through agent-box tracker. Never change files, Git, lifecycle, assignees, priorities, relations or progress marks; never delegate.
Edit only a concrete stale assertion or a decision changing the task's scope/acceptance. No informational additions, Git/sync bookkeeping (commit hashes, sync-task IDs), or restated workflow instructions. Preserve intended requirements and unrelated text; unresolved contradictions need a decision, never weaker acceptance. No correction means no write.
With references enabled, repair missing/stale references needed to implement affected tasks; do not backfill unrelated tasks. Preserve the section's heading and location; if absent, add one under Context. Use path#heading plus purpose; verify sections and meaning, never line numbers or invented decisions.
Before each edit, reread the task; use agent-box tracker update ID --revision REV --description-file - with its full preserved description in a direct heredoc, without pipes or temporary files. Read back saved text; journal substantive corrections. Write in task language, or wholly in an assignment's Working language. On unknown-outcome, reread task/comments before any further write; never blindly retry.
Report in English: task IDs, saved/partial corrections, evidence, unresolved issues. End with one line: Sync: UPDATED, Sync: UNCHANGED, Sync: BLOCKED, or Sync: ERROR. BLOCKED needs a decision; ERROR means reading, writing or verification failed.
"""

PENDING = ("RUNNING", "NATIVE", "ERROR", "BLOCKED")


def record_path(root, task):
    from . import workflow
    ident = hashlib.sha256(task.id.encode()).hexdigest()[:24]
    return workflow.WORKFLOW_STATE / "plan-sync" / workflow.repository_key(root) / f"{ident}.json"


def last_result(root, task):
    try:
        return migrations.read(record_path(root, task), migrations.PLAN_SYNC)
    except (OSError, ValueError):
        return None


def save(root, task, result):
    path = record_path(root, task)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    migrations.write(path, result, migrations.PLAN_SYNC)


def managed_workflow_only(root, base, head):
    """Normalize verified owned blocks, preserving surrounding bytes and uncertain changes."""
    from .repo_binding import update_block
    versions = []
    for revision in (base, head):
        try:
            result = subprocess.run(["git", "cat-file", "blob", f"{revision}:AGENTS.md"],
                                    cwd=root, capture_output=True, timeout=30)
            if result.returncode:
                return False
            original = result.stdout.decode("utf-8")
            if "<!-- BEGIN AGENT-BOX workflow" not in original:
                return False
            versions.append(update_block(original, "", "workflow", root / "AGENTS.md"))
        except (AgentBoxError, OSError, UnicodeError, subprocess.TimeoutExpired):
            return False
    return versions[0] == versions[1]


def decision(root, task, settings, event):
    if event == "manual":
        return True, "manual"
    if settings.policy == "off" or event not in settings.events:
        return False, "automatic event disabled"
    if settings.policy == "always" or event != "after":
        return True, "selected event"
    from . import workflow
    saved = workflow.read_evidence(root, task)
    base, head = saved.get("base_head"), saved.get("head")
    if not base or not head:
        # Missing provenance cannot safely mean an unchanged source.
        return True, "source range unavailable; inspect Git history"
    paths = workflow.gw.git(root, "diff", "--name-only", base, head).splitlines()
    matched = [path for path in paths if any(fnmatch.fnmatchcase(path, pattern) for pattern in settings.paths)]
    if matched == ["AGENTS.md"] and managed_workflow_only(root, base, head):
        return False, "only managed workflow block changed"
    return bool(matched), "changed path matched" if matched else "no changed path matched"


def native_result(result):
    """Older native records have a profile route, while managed routes name a provider."""
    return bool(result and (result.get("backend") == "native"
                            or result.get("route", {}).get("native")))


def checked_result(result, config):
    """Status and recovery commands must not redisclose a previously refused report."""
    if result and not native_result(result) and isinstance(result.get("report"), str):
        from .text_delivery import check_size
        result = dict(result)
        if delivery := result.pop("report_delivery", None):
            result.update(report=delivery["text"], summary=delivery["summary"])
        if result.get("reconciliation"):
            result["reconciliation"] = dict(result["reconciliation"])
            if delivery := result["reconciliation"].pop("report_delivery", None):
                result["reconciliation"].update(report=delivery["text"], summary=delivery["summary"])
        size = len(result["report"].encode("utf-8"))
        size += len(result.get("reconciliation", {}).get("report", "").encode("utf-8"))
        check_size(size, config.workflow, "sync report (original saved)")
    return result


def prepare_report(root, config, result, *, reconciliation=False):
    """Only explicit completion/reconciliation may pay for a summary; reads reuse it."""
    if native_result(result):
        return
    from .text_summary import compact
    target = result["reconciliation"] if reconciliation else result
    budget = config.workflow.text_max_bytes
    if reconciliation:
        previous = result.get("report_delivery", {}).get("text", result.get("report", ""))
        budget -= len(previous.encode("utf-8"))
    report = target.get("report", "")
    delivery = target.get("report_delivery")
    if (delivery and delivery["summary"]["source_sha256"] == hashlib.sha256(report.encode("utf-8")).hexdigest()
            and len(delivery["text"].encode("utf-8")) <= budget):
        return
    protected = [line for line in report.splitlines() if re.fullmatch(r"(?:\*\*)?Sync: \w+(?:\*\*)?[ \t]*", line)]
    text, summary = compact(report, config, root, label="Plan sync report", budget=budget, protected=protected)
    if summary:
        summary_outcomes = [line for line in text.splitlines()
                            if re.fullmatch(r"(?:\*\*)?Sync: \w+(?:\*\*)?[ \t]*", line)]
        if summary_outcomes != protected:
            raise AgentBoxError("TEXT_SUMMARY_FAILED: Plan sync summary changed outcome lines; original retained")
        try:
            original_outcome = outcome(report, reconciliation=reconciliation)
        except AgentBoxError:
            # A malformed original is itself a recovery case; the new reconciliation owns the outcome.
            original_outcome = None
        if original_outcome:
            try:
                if outcome(text, reconciliation=reconciliation) != original_outcome:
                    raise AgentBoxError("outcome differs")
            except AgentBoxError as exc:
                raise AgentBoxError(f"TEXT_SUMMARY_FAILED: Plan sync summary changed its outcome; original retained: {exc}") from None
        target["report_delivery"] = {"text": text, "summary": summary}


def describe(root, task, config):
    from .workflow import execution
    result = {"policy": config.workflow.sync.policy, "events": config.workflow.sync.events,
              "scope": config.workflow.sync.scope, "references": config.workflow.sync.references,
              "execution": execution(config, "sync", config.workflow.effective_tier(task.tier), task.id),
              "command": f"agent-box workflow sync {task.id}", "last_result": checked_result(last_result(root, task), config)}
    prior = result["last_result"] or {}
    if prior.get("status") == "REQUIRED":
        result["next_step"] = (f"Run Plan sync through ab-sync: agent-box workflow sync {task.id} "
                               f"--event {prior.get('event', 'manual')}; do this before starting affected work")
    elif prior.get("status") in PENDING:
        result["next_step"] = (f"Reconcile Plan sync through ab-sync: stop/await its executor, reread live tasks, "
                               f"then agent-box workflow sync {task.id} --reconcile FILE; "
                               "use --result FILE for a completed native answer")
    return result


def schedule(root, binding, adapter, config, task, event):
    required, reason = decision(root, task, config.workflow.sync, event)
    if not required:
        return {"status": "SKIPPED", "reason": reason}
    from .workflow import gw
    result = {"status": "REQUIRED", "task": task.id, "head": gw.head_sha(root), "event": event,
            "command": f"agent-box workflow sync {task.id} --event {event}",
            "execution": describe(root, task, config)["execution"]}
    with lock(root, "work"):
        prior = last_result(root, task)
        if (event == "after" and prior and prior.get("event") == event and prior.get("head") == result["head"]
                and prior.get("status") in ("UPDATED", "UNCHANGED")):
            return checked_result(prior, config)
        # Do not hide an interrupted run behind a new automatic request.
        if prior and prior.get("status") in PENDING:
            return checked_result(prior, config)
        save(root, task, result)
    return result


def outcome(report, *, reconciliation=False):
    statuses = re.findall(r"^(?:\*\*)?Sync: (\w+)(?:\*\*)?[ \t]*$", report, re.M)
    allowed = ("UPDATED", "UNCHANGED", "BLOCKED", "ERROR") + (("REQUIRED",) if reconciliation else ())
    if (len(statuses) != 1 or statuses[0] not in allowed
            or report.rstrip().splitlines()[-1].strip("* \t") != f"Sync: {statuses[0]}"):
        raise AgentBoxError("Plan sync returned no single outcome; reread tasks before retrying")
    if reconciliation and not "\n".join(report.rstrip().splitlines()[:-1]).strip():
        raise AgentBoxError("Plan sync reconciliation requires evidence of checked writes and resolved/remaining blockers")
    return statuses[0]


def complete(root, task, result, report):
    result["report"] = report
    result["status"] = outcome(report)
    save(root, task, result)
    return result


def run(root, binding, adapter, config, task, event="manual", *, native=False, result_file=None, reconcile_file=None):
    from . import workflow
    settings = config.workflow.sync
    if reconcile_file is not None:
        with lock(root, "work"):
            result = last_result(root, task)
            if not result or result.get("status") not in PENDING:
                raise AgentBoxError("No pending Plan sync result to reconcile")
            report = Path(reconcile_file).read_text()
            status = outcome(report, reconciliation=True)
            # Retain the original head, report and diagnostics; recovery is not a new run.
            result["reconciliation"] = {"previous_status": result["status"], "report": report}
            result["status"] = status
            save(root, task, result)
            try:
                prepare_report(root, config, result)
                save(root, task, result)
                prepare_report(root, config, result, reconciliation=True)
            except (AgentBoxError, OSError) as exc:
                result.update(status="ERROR", reason=str(exc))
                save(root, task, result)
                raise AgentBoxError(f"Plan sync delivery failed; reconciliation evidence saved: {exc}") from None
            save(root, task, result)
            return checked_result(result, config)
    if result_file is not None:
        with lock(root, "work"):
            result = last_result(root, task)
            if not result or result.get("status") != "NATIVE":
                raise AgentBoxError("No pending native Plan sync assignment")
            if result["head"] != workflow.gw.head_sha(root):
                raise AgentBoxError("Repository head changed during Plan sync; inspect tasks before retrying")
            result["backend"] = "native"
            return complete(root, task, result, Path(result_file).read_text())
    required, reason = decision(root, task, settings, event)
    if not required:
        return {"task": task.id, "event": event, "status": "SKIPPED", "reason": reason}
    tier = config.workflow.effective_tier(task.tier)
    path, models = config.workflow.models_for("sync", tier)
    if not models:
        raise AgentBoxError(f"Configure {path} before Plan sync; it requires a separate agent")
    parent = root_task(task, adapter.get)
    if task.project not in set(adapter.subtree(binding.project, binding.recursive)):
        raise AgentBoxError("Plan sync source is outside the repository binding")
    target = (f"parent tree {parent.id}" if settings.scope == "parent" else
              f"project {binding.project}; recursive={str(binding.recursive).lower()}")
    prompt = (RULES + f"\nAssigned role: sync executor\nSource: {task.id}\nRead: agent-box tracker get {task.url or task.id} --no-comments\nEvent: {event}\nScope: {target}\n"
              f"References: {str(settings.references).lower()}\n")
    from .workflow_behavior import instructions
    prompt += instructions(config.workflow.behavior_version, "sync")
    guidance = workflow.repository_instructions(root, config.workflow)
    if guidance:
        prompt += f"\nRepository instructions (including imports; already supplied):\n{guidance}\n"
    if config.workflow.language:
        # The rules are also the native profile shared by every repository; only the value is this one's.
        prompt += f"Working language: {config.workflow.language}\n"
    saved = workflow.read_evidence(root, task)
    if saved.get("base_head") and saved.get("head"):
        prompt += f"Source commits: {saved['base_head']}..{saved['head']} (verify integration before propagation).\n"
    from . import text_delivery
    text_delivery.check_size(len(prompt.encode("utf-8")), config.workflow, "sync assignment")
    text_delivery.prepare(config)
    with lock(root, "work"):
        prior = last_result(root, task)
        if prior and prior.get("status") in PENDING:
            raise AgentBoxError(f"Plan sync {prior['status']}; stop/await its executor and inspect live tasks, "
                                f"then workflow sync {task.id} --reconcile FILE before retrying")
        result = {"task": task.id, "event": event, "head": workflow.gw.head_sha(root), "status": "RUNNING",
                  "backend": "native" if native else "managed"}
        if native:
            route = workflow.execution(config, "sync", tier, task.id)
            if not route.get("native"):
                raise AgentBoxError("No configured native Plan sync profile; use the managed command")
            artifact = text_delivery.publish(prompt, config)
            result.update(status="NATIVE", route=route)
            save(root, task, result)
            from .delegation import bootstrap
            return {**result, "message": bootstrap("sync", artifact), "input": artifact}
        selected = workflow.candidates(config, "sync", tier)
        save(root, task, result)
        try:
            output = workflow.execute(config.workflow, selected, "sync", root, prompt)
            report = output["report"]
            result.update(route=output["route"], usage=output["diagnostics"]["usage"])
            complete(root, task, result, report)
            prepare_report(root, config, result)
        except (AgentBoxError, OSError) as exc:
            result.update(status="ERROR", reason=str(exc))
            save(root, task, result)
            raise AgentBoxError(f"Plan sync failed; task writes may have completed. Inspect tasks before retrying: {exc}") from None
        save(root, task, result)
        return checked_result(result, config)
