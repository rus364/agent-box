"""What an existing project must fix before the workflow can run inside it.

Only mechanical facts live here. Judgement - whether a title says what the task
is, whether acceptance is the right acceptance - belongs to the coordinator that
reads this report, because a rule that guesses at meaning would quietly rewrite
work nobody asked it to touch.

Backlog can contain unresolved work. An audit must not pressure an adoption into
inventing requirements merely to make a report green.
"""
from __future__ import annotations

from .core import TrackerError, blocking_reasons, checkboxes

# Warnings and recommendations for the user. They are returned rather than
# printed so the coordinator can render them in the user's own language.
NOTES = (
    "Adoption normalizes a staging copy. The source stays intact; only after "
    "audit are the roots renamed and the repository binding switched. It never "
    "implements, decomposes or designs the work described by tasks.",
    "Name the project exactly as your tracker names it - a numeric ID or a full "
    "project path. Nothing is guessed from a partial title.",
    "A project with subprojects is adopted with the whole subtree, because every "
    "project under the root takes part in the workflow.",
    "Labels are shared objects. Change only staging task label attachments; "
    "never rename or delete global labels, including ab: labels.",
    "Preserve meaning and language. Extract only evidenced acceptance and "
    "checklists. Unanswered decisions stay in Backlog for ab-interview; planning "
    "and decomposition belong to ab-plan. Repair evidenced internal relations "
    "across the tree; a shared topic alone is not a dependency.",
)


def task_problems(task, get):
    """Everything mechanically wrong with one task, as stable codes."""
    problems = []

    def report(code, message):
        advisory = code in {"no-checklist", "early-progress", "early-assignee", "done-unfinished"}
        if code == "no-description" and task.state in {"Backlog", "Done"}:
            advisory = True
        problems.append({"code": code, "message": message,
                         "severity": "warning" if advisory else "error"})

    if task.state == "Unknown":
        report("no-state", "sits in no lifecycle column of its project board")
    try:
        # A task needs no tier; an explicit label must still be valid.
        task.tier
    except TrackerError as exc:
        report("bad-tier", str(exc))
    if not task.description.strip():
        report("no-description", "has no description")
    items = checkboxes(task.description)
    if not items:
        report("no-checklist", "records no checklist, so progress has no evidence")
    if task.state in ("Backlog", "Ready"):
        if any(item["checked"] for item in items):
            report("early-progress", "records checked progress but has not started")
        if task.assignees:
            report("early-assignee", "carries an assignee but has not started")
    if task.state == "Doing" and not task.assignees:
        report("unowned", "is in Doing with nobody assigned")
    if task.state == "Done" and any(not item["checked"] for item in items):
        report("done-unfinished", "is Done with unfinished checklist items")
    broken_parent = False
    if task.parent:
        try:
            if get(task.parent).project != task.project:
                report("split-parent", f"has parent {task.parent} in another project")
        except (KeyError, TrackerError) as exc:
            broken_parent = True
            report("broken-parent", f"has an unreadable parent {task.parent}: {exc}")
    # A parent in scope can pull an external child into workflow.tree even when
    # the child itself is outside the set of tasks being audited.
    for child in task.children:
        try:
            if get(child).project != task.project:
                report("split-child", f"has subtask {child} in another project")
        except (KeyError, TrackerError) as exc:
            report("broken-child", f"has an unreadable subtask {child}: {exc}")
    for reason in blocking_reasons(task, get):
        # A dependency that is merely unfinished is the workflow working; only a
        # relation that cannot hold at all is a defect to fix during adoption.
        # An unreadable parent already stops that walk, and it is one broken
        # relation, so it is not counted a second time as a broken dependency.
        if "cannot be verified" in reason and broken_parent:
            continue
        if "cycle" in reason or "cannot be verified" in reason:
            report("broken-dependency", reason)
    return problems


def audit(tasks, get, lifecycle=()):
    """Structural errors block cutover; preparation warnings remain visible."""
    duplicates = {}
    for task in tasks:
        duplicates.setdefault(task.title.strip().casefold(), []).append(task.id)
    results, counts = [], {}
    for task in sorted(tasks, key=lambda item: int(item.id) if item.id.isdigit() else 0):
        problems = task_problems(task, get)
        same = duplicates[task.title.strip().casefold()]
        if len(same) > 1:
            problems.append({"code": "duplicate-title",
                             "severity": "warning",
                             "message": "shares its title with " + ", ".join(i for i in same if i != task.id)})
        for problem in problems:
            counts[problem["code"]] = counts.get(problem["code"], 0) + 1
        results.append({"id": task.id, "project": task.project, "title": task.title,
                        "state": task.state, "problems": problems})
    errors = [dict(task=row["id"], **problem) for row in results for problem in row["problems"]
              if problem["severity"] == "error"]
    return {"notes": list(NOTES), "scope": "project-tree",
            "lifecycle": list(lifecycle),
            "boards": [entry for entry in lifecycle if entry["problems"]],
            "tasks": results, "counts": counts, "errors": errors,
            "clean": not errors and not any(entry["problems"] for entry in lifecycle)}


def audit_project(adapter, project, recursive=True):
    tasks = adapter.search(project, "", recursive)
    index = {task.id: task for task in tasks}
    return audit(tasks, lambda ident: index.get(ident) or adapter.get(ident),
                 adapter.lifecycle_report(adapter.subtree(project, recursive)))
