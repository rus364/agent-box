"""External adoption edges are best effort, never a reason to rewrite backup.

Only the live graph determines preservation. Journal comparison below avoids
reposting an identical explanation; it never decides whether a relation exists.
"""
from dataclasses import replace

from . import vikunja_api as api
from .core import TrackerError, blocking_reasons


AUTOMATIC = {"related", "duplicateof", "duplicates", "precedes", "follows", "blocked"}
REASONS = {
    "parenttask": "Parent and subtask must belong to the same project in agent-box.",
    "subtask": "Parent and subtask must belong to the same project in agent-box.",
    "blocking": "Adding staging as another blocker would change the external task's workflow.",
    "copiedfrom": "Changing historical copy provenance needs a human decision.",
    "copiedto": "Changing historical copy provenance needs a human decision.",
}


def contains(task, kind, other):
    return str(other) in {str(t["id"]) for t in (task.get("related_tasks") or {}).get(kind, [])}


def preserve(adapter, relations, apply=False):
    client, results = adapter.client, []
    for relation in relations:
        source, target, kind, other = (relation[key] for key in ("task", "staging", "kind", "other"))
        row = {"task": source, "staging": target, "kind": kind, "other": other}
        reason = REASONS.get(kind, "Unsupported relation type.") if kind not in AUTOMATIC else ""
        current = client.get(f"/tasks/{target}") if target else None
        if current and contains(current, kind, other):
            row["status"] = "preserved"
        elif not target:
            row["status"] = "skipped" if reason else "planned"
        else:
            row["status"] = "skipped" if reason else "missing"
            if not reason:
                try:
                    client.get(f"/tasks/{other}")
                except api.APIError as exc:
                    reason = str(exc)
                    client.get(f"/tasks/{target}")
                if not reason and kind == "blocked":
                    task = adapter.get(target)
                    proposed = replace(task, blockers=[*task.blockers, other])
                    reasons = blocking_reasons(proposed, adapter.get)
                    reason = "; ".join(item for item in reasons if "cycle" in item or "cannot be verified" in item)
                if not reason and apply:
                    try:
                        if kind == "blocked":
                            # The regular adapter owns dependency cycle checks.
                            adapter.relate(target, "blocked-by", other)
                        else:
                            client.post(f"/tasks/{target}/relations",
                                        {"other_task_id": int(other), "relation_kind": kind})
                    except TrackerError as exc:
                        reason = str(exc)
                    # A lost POST response may still have committed. Failure to
                    # read staging is fatal, unlike rejection of one relation.
                    if contains(client.get(f"/tasks/{target}"), kind, other):
                        row["status"], reason = "preserved", ""
                    elif not reason:
                        reason = "The provider did not confirm the relation."
            if row["status"] != "preserved" and apply:
                explanation = reason or "This external relation could not be preserved automatically."
                message = (f"Adoption: source task #{source} has a {kind} relation to task #{other}. "
                           f"{explanation} The original relation is unchanged. "
                           "Check access and add the relation manually, agree on a suitable relation type, "
                           "or leave it on the original task. Resolve hierarchy or workflow changes with the project owners.")
                try:
                    if not any((comment.get("comment") or "").strip() == message for comment in adapter.comments(target)):
                        adapter.comment(target, message)
                except TrackerError as exc:
                    row["comment_warning"] = str(exc)
                    client.get(f"/tasks/{target}")
        if row["status"] != "preserved":
            row["warning"] = reason or "External relation is not yet present on staging."
        elif kind in {"blocking", "blocked"}:
            row["warning"] = "External dependency preserved; its completion remains governed by the live tasks."
        elif kind in {"precedes", "follows"}:
            row["warning"] = "Ordering preserved; agent-box does not enforce this relation as a blocker."
        results.append(row)
    return results
