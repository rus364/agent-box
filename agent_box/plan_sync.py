"""A tracker-writing delegate reads its own sources; Git remains read-only."""
import fnmatch
import hashlib
import json
import re
import subprocess
from pathlib import Path

from .atomicio import atomic_write_text
from .errors import AgentBoxError
from .tracker.core import root_task
from .workflow_state import lock


RULES = """Synchronize tracker tasks. Read AGENTS.md/CLAUDE.md and imports. Read the source with agent-box tracker get ID --no-comments; inspect its accepted decisions, commit range and referenced sections yourself. Discover candidates with tracker search: separate substring queries for changed paths, anchors and terms; follow source relations, including unreferenced tasks. Search defaults to open; --include-done finds history. Respect recursive scope. Scope bounds discovery, not a full-task audit: get --no-comments only plausible candidates; fetch comments ID only for missing decision evidence. Never fetch task URLs directly.
Only tracker descriptions/comments may change through agent-box tracker. Never change files, Git, lifecycle, assignees, priorities, relations or progress marks; do not delegate. Done and other active tasks are read-only; affected unwritable tasks are BLOCKED.
Edit only a concrete stale assertion or a decision changing the task's scope/acceptance. No informational additions, Git/sync bookkeeping (commit hashes, sync-task IDs), or restated workflow instructions. Preserve intended requirements and unrelated text; unresolved contradictions need a decision, never weaker acceptance. No correction means no write.
With references enabled, repair missing/stale references needed to implement affected tasks; do not backfill unrelated tasks. Preserve the section's heading, language and location; if absent, add a task-language section under Context. Use path#heading plus purpose; verify sections and meaning, never line numbers or invented decisions.
Before each edit, reread the task; use agent-box tracker update ID --revision REV --description-file - with its full preserved description in a direct heredoc, without pipes or temporary files. Read back saved text; journal substantive corrections in task language. On unknown-outcome, reread task/comments before any further write; never blindly retry.
Return concise English: task IDs, exact mismatch/correction and evidence, unresolved issues. End with exactly one line: Sync: UPDATED, Sync: UNCHANGED, or Sync: BLOCKED. Partial work or inaccessible sources is BLOCKED.
"""


def record_path(root, task):
    from . import workflow
    ident = hashlib.sha256(task.id.encode()).hexdigest()[:24]
    return workflow.WORKFLOW_STATE / "plan-sync" / workflow.repository_key(root) / f"{ident}.json"


def last_result(root, task):
    try:
        result = json.loads(record_path(root, task).read_text())
        return result if isinstance(result, dict) else None
    except (OSError, ValueError):
        return None


def save(root, task, result):
    path = record_path(root, task)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write_text(path, json.dumps(result, ensure_ascii=False, indent=2) + "\n", 0o600)


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


def describe(root, task, config):
    from .workflow import execution
    result = {"policy": config.workflow.sync.policy, "events": config.workflow.sync.events,
              "scope": config.workflow.sync.scope, "references": config.workflow.sync.references,
              "execution": execution(config, "sync", config.workflow.effective_tier(task.tier), task.id),
              "command": f"agent-box workflow sync {task.id}", "last_result": last_result(root, task)}
    prior = result["last_result"] or {}
    if prior.get("status") in ("REQUIRED", "RUNNING", "NATIVE", "ERROR", "BLOCKED"):
        action = "Run" if prior["status"] == "REQUIRED" else "Reconcile the pending or partial result before retrying"
        result["next_step"] = (f"{action} Plan sync through ab-sync: agent-box workflow sync {task.id} "
                               f"--event {prior.get('event', 'manual')}; do this before starting affected work")
    return result


def schedule(root, binding, adapter, config, task, event):
    required, reason = decision(root, task, config.workflow.sync, event)
    if not required:
        return {"status": "SKIPPED", "reason": reason}
    from .workflow import gw
    prior = last_result(root, task)
    if (event == "after" and prior and prior.get("event") == event and prior.get("head") == gw.head_sha(root)
            and prior.get("status") in ("UPDATED", "UNCHANGED")):
        return prior
    result = {"status": "REQUIRED", "task": task.id, "head": gw.head_sha(root), "event": event,
            "command": f"agent-box workflow sync {task.id} --event {event}",
            "execution": describe(root, task, config)["execution"]}
    with lock(root, "work"):
        # Do not hide an interrupted run behind a new automatic request.
        if prior and prior.get("status") in ("NATIVE", "RUNNING", "ERROR", "BLOCKED"):
            return prior
        save(root, task, result)
    return result


def complete(root, task, result, report):
    statuses = re.findall(r"^(?:\*\*)?Sync: (UPDATED|UNCHANGED|BLOCKED)(?:\*\*)?[ \t]*$", report, re.M)
    if len(statuses) != 1 or report.rstrip().splitlines()[-1].strip("* \t") != f"Sync: {statuses[0]}":
        raise AgentBoxError("Plan sync returned no single outcome; reread tasks before retrying")
    result.update(status=statuses[0], report=report)
    save(root, task, result)
    return result


def run(root, binding, adapter, config, task, event="manual", *, native=False, result_file=None):
    from . import workflow
    settings = config.workflow.sync
    if result_file is not None:
        with lock(root, "work"):
            result = last_result(root, task)
            if not result or result.get("status") != "NATIVE":
                raise AgentBoxError("No pending native Plan sync assignment")
            if result["head"] != workflow.gw.head_sha(root):
                raise AgentBoxError("Repository head changed during Plan sync; inspect tasks before retrying")
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
    prompt = (f"Source: {task.id}\nRead: agent-box tracker get {task.url or task.id} --no-comments\nEvent: {event}\nScope: {target}\n"
              f"References: {str(settings.references).lower()}\n")
    saved = workflow.read_evidence(root, task)
    if saved.get("base_head") and saved.get("head"):
        prompt += f"Source commits: {saved['base_head']}..{saved['head']} (verify integration before propagation).\n"
    with lock(root, "work"):
        result = {"task": task.id, "event": event, "head": workflow.gw.head_sha(root), "status": "RUNNING"}
        if native:
            route = workflow.execution(config, "sync", tier, task.id)
            if not route.get("native"):
                raise AgentBoxError("No configured native Plan sync profile; use the managed command")
            result.update(status="NATIVE", route=route)
            save(root, task, result)
            return {**result, "message": prompt}
        selected = workflow.candidates(config, "sync", tier)
        save(root, task, result)
        try:
            output = workflow.execute(config.workflow, selected, "sync", root, RULES + "\n" + prompt)
            report = output["report"]
            result.update(route=output["route"], usage=output["diagnostics"]["usage"])
            complete(root, task, result, report)
        except (AgentBoxError, OSError) as exc:
            result.update(status="ERROR", reason=str(exc))
            save(root, task, result)
            raise AgentBoxError(f"Plan sync failed; task writes may have completed. Inspect tasks before retrying: {exc}") from None
        save(root, task, result)
        return result
