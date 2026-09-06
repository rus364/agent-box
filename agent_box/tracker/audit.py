"""What an existing project must fix before the workflow can run inside it.

Only mechanical facts live here. Judgement - whether a title says what the task
is, whether acceptance is the right acceptance - belongs to the coordinator that
reads this report, because a rule that guesses at meaning would quietly rewrite
work nobody asked it to touch.

Re-running the audit is also how adoption resumes: whatever still appears in the
report is still undone, so no adoption state is stored anywhere.
"""
from __future__ import annotations

from .core import TrackerError, blocking_reasons, checkboxes

# Warnings and recommendations for the user. They are returned rather than
# printed so the coordinator can render them in the user's own language.
NOTES = (
    "Adoption rewrites tracker data only: task titles, descriptions, lifecycle "
    "columns, relations and agent-box tier labels. It never writes repository "
    "files and never implements anything a task asks for.",
    "Take a snapshot first; it is the only way back. Every change is applied "
    "task by task and the tracker keeps no undo of its own.",
    "Name the project exactly as your tracker names it - a numeric ID or a full "
    "project path. Nothing is guessed from a partial title.",
    "A project with subprojects is adopted with the whole subtree, because every "
    "project under the root takes part in the workflow.",
    "Labels the project already uses belong to the user. Only labels starting "
    "with 'ab:' are agent-box's to add, change or remove.",
    "A description may carry an import marker that identifies the task for a "
    "later re-import. Rewriting the text around it is fine; dropping it is not.",
    "Existing meaning is preserved. A rewrite makes a task's goal, acceptance "
    "and validation explicit; it does not decide the work differently.",
)


def task_problems(task, get):
    """Everything mechanically wrong with one task, as stable codes."""
    problems = []

    def report(code, message):
        problems.append({"code": code, "message": message})

    if task.state == "Unknown":
        report("no-state", "sits in no lifecycle column of its project board")
    try:
        if task.tier is None:
            report("no-tier", "carries no ab:tier label, so it cannot be delegated")
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
    if task.parent:
        try:
            if get(task.parent).project != task.project:
                report("split-parent", f"has parent {task.parent} in another project")
        except (KeyError, TrackerError) as exc:
            report("broken-parent", f"has an unreadable parent {task.parent}: {exc}")
    for reason in blocking_reasons(task, get):
        # A dependency that is merely unfinished is the workflow working; only a
        # relation that cannot hold at all is a defect to fix during adoption.
        if "cycle" in reason or "cannot be verified" in reason:
            report("broken-dependency", reason)
    return problems


def audit(tasks, get, lifecycle=()):
    """The whole adoptable scope: board problems, task problems, and their counts."""
    duplicates = {}
    for task in tasks:
        duplicates.setdefault(task.title.strip().casefold(), []).append(task.id)
    results, counts = [], {}
    for task in sorted(tasks, key=lambda item: int(item.id) if item.id.isdigit() else 0):
        problems = task_problems(task, get)
        same = duplicates[task.title.strip().casefold()]
        if len(same) > 1:
            problems.append({"code": "duplicate-title",
                             "message": "shares its title with " + ", ".join(i for i in same if i != task.id)})
        for problem in problems:
            counts[problem["code"]] = counts.get(problem["code"], 0) + 1
        results.append({"id": task.id, "project": task.project, "title": task.title,
                        "state": task.state, "problems": problems})
    return {"notes": list(NOTES), "lifecycle": list(lifecycle),
            "boards": [entry for entry in lifecycle if entry["problems"]],
            "tasks": results, "counts": counts,
            "clean": not counts and not any(entry["problems"] for entry in lifecycle)}
