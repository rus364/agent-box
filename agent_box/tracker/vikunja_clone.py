"""Native project duplication, with only the gaps needed to copy a tree.

Vikunja copies task contents and files itself. We connect the resulting projects
and preserve external edges where possible, including their provider-managed
inverses. Source objects stay intact. A lost duplicate POST needs inspection.
"""
from . import vikunja_api as api
from .core import TrackerError


def indexed(tasks):
    result = {}
    for task in tasks:
        index = task.get("index")
        if not isinstance(index, int) or index <= 0 or index in result:
            raise TrackerError("Native clone requires unique positive task indexes", "conflict")
        result[index] = task
    return result


def inspect(adapter, source, title, progress):
    root = adapter.project(source)
    if not title.strip() or len(title) > 250:
        raise TrackerError("Staging title must contain 1-250 characters")
    if int(root["id"]) <= 0 or root.get("is_archived"):
        raise TrackerError("Adoption requires an unarchived, real project")
    if root.get("max_permission", 0) < 1:
        raise TrackerError("Cutover needs write access to the source root", "authentication")
    scope = set(adapter.subtree(source, True))
    projects = {str(p["id"]): p for p in adapter.projects() if str(p["id"]) in scope}
    copies = progress.get("projects", {})
    if set(copies) - projects.keys() or len(set(copies.values())) != len(copies):
        raise TrackerError("Clone project mapping no longer matches the source tree", "conflict")
    if set(copies.values()) & projects.keys():
        raise TrackerError("A staging project cannot be a source project", "conflict")
    # Once staging tasks move between projects their visible numbers no longer
    # pair up, so the mapping saved before the first move replaces the pairing.
    frozen = progress.get("tasks")
    tasks, task_map, errors, staging = {}, {}, [], set()
    for ident, project in projects.items():
        if ident not in copies and len(project["title"] + " - duplicate") > 250:
            errors.append(f"Source project {ident} title is too long for native duplicate")
        if project.get("is_archived"):
            errors.append(f"Source project {ident} is archived; do not unarchive it during adoption")
        rows = api.collect_tasks(adapter.client, project=ident)
        tasks.update({str(t["id"]): t for t in rows})
        original = indexed(rows)
        if ident not in copies:
            continue
        target = adapter.project(copies[ident])
        expected_parent = (root.get("parent_project_id") or 0) if ident == str(root["id"]) else copies.get(
            str(project.get("parent_project_id")))
        if expected_parent is None or str(target.get("parent_project_id") or 0) != str(expected_parent):
            errors.append(f"Copy {target['id']} has a different parent; inspect it before continuing")
        copied = api.collect_tasks(adapter.client, project=str(target["id"]))
        if frozen is not None:
            staging.update(str(row["id"]) for row in copied)
            continue
        duplicate = indexed(copied)
        if original.keys() != duplicate.keys():
            errors.append(f"Task indexes differ between projects {ident} and {target['id']}")
        for index in original.keys() & duplicate.keys():
            task_map[str(original[index]["id"])] = str(duplicate[index]["id"])
    if frozen is not None:
        task_map = {ident: target for ident, target in frozen.items() if ident in tasks}
        for ident in sorted(tasks.keys() - frozen.keys(), key=int):
            errors.append(f"Source task {ident} has no staging copy in the recorded task mapping")
        for ident in sorted(set(frozen.values()) - staging, key=int):
            errors.append(f"Staging task {ident} from the recorded task mapping is not in staging")
        for ident in sorted(staging - set(frozen.values()), key=int):
            errors.append(f"Staging task {ident} is not a copy of any source task")
    if str(source) in copies:
        actual = set(adapter.subtree(copies[str(source)], True))
        if actual != set(copies.values()):
            errors.append("Staging contains projects outside the recorded clone mapping")
    edges = api.relation_edges(tasks.values())
    external = []
    for base, kind, other in sorted(edges):
        if base in tasks and other in tasks:
            continue
        if base not in tasks:
            base, kind, other = other, api.RELATION_INVERSE.get(kind, kind), base
        external.append({"task": base, "staging": task_map.get(base), "kind": kind, "other": other})
    return {"root": root, "source_projects": projects, "source_tasks": tasks,
            "task_map": task_map, "edges": edges, "errors": errors,
            "external_relations": external,
            "missing_projects": sorted(projects.keys() - copies.keys())}


def clone(adapter, source, title, progress, checkpoint, apply=False):
    if apply and (progress.get("relation_removals") or "tasks" in progress):
        raise TrackerError("Resume normalization or cutover after relation edits or moves; do not rerun clone",
                           "conflict")
    source = str(adapter.project(source)["id"])
    report = inspect(adapter, source, title, progress)
    copies = progress.setdefault("projects", {})
    pending = progress.get("pending")
    if pending:
        report["errors"].append(
            f"Duplicate of source project {pending} has an uncertain result. Inspect the provider; "
            "record its new project ID in clone.projects and clear clone.pending only after verification.")
    if apply and report["errors"]:
        raise TrackerError("; ".join(report["errors"]), "conflict")
    if apply:
        remaining = set(report["missing_projects"])
        while remaining:
            eligible = [ident for ident in sorted(remaining, key=int)
                        if ident == source or str(report["source_projects"][ident].get("parent_project_id")) in copies]
            if not eligible:
                raise TrackerError("Source project hierarchy contains a cycle", "conflict")
            for ident in eligible:
                parent = report["source_projects"][ident].get("parent_project_id") or 0
                parent = parent if ident == source else int(copies[str(parent)])
                progress["pending"] = ident
                checkpoint(progress)
                created = adapter.client.post(f"/projects/{ident}/duplicate",
                                              {"parent_project_id": parent, "duplicate_shares": True})
                copies[ident] = str(created["duplicated_project"]["id"])
                progress.pop("pending")
                checkpoint(progress)
                remaining.remove(ident)
        # A mapping is saved before renames, so a failed rename never repeats a clone.
        for ident, target in copies.items():
            adapter.rename_project(target, title if ident == source else report["source_projects"][ident]["title"])
        report = inspect(adapter, source, title, progress)
        if report["errors"]:
            raise TrackerError("; ".join(report["errors"]), "conflict")
        mapping = report["task_map"]
        for ident, target in mapping.items():
            original = report["source_tasks"][ident]
            current = adapter.client.get(f"/tasks/{target}")
            users = {str(u["id"]) for u in original.get("assignees") or []}
            present = {str(u["id"]) for u in current.get("assignees") or []}
            # Native duplicate checks assignees before copying direct shares.
            # At this point those grants exist; only missing assignments are added.
            for user in sorted(users - present):
                adapter.client.post(f"/tasks/{target}/assignees", {"user_id": int(user)})
            reread = adapter.client.get(f"/tasks/{target}")
            if {str(u["id"]) for u in reread.get("assignees") or []} != users:
                raise TrackerError(f"Clone did not preserve assignees for task {ident}", "conflict")
        for base, kind, other in sorted(report["edges"]):
            if base not in mapping or other not in mapping:
                continue
            target, related = mapping[base], mapping[other]
            current = adapter.client.get(f"/tasks/{target}")
            existing = {str(t["id"]) for t in (current.get("related_tasks") or {}).get(kind, [])}
            if related not in existing:
                adapter.client.post(f"/tasks/{target}/relations",
                                    {"other_task_id": int(related), "relation_kind": kind})
                current = adapter.client.get(f"/tasks/{target}")
                if related not in {str(t["id"]) for t in (current.get("related_tasks") or {}).get(kind, [])}:
                    raise TrackerError(f"Clone relation {base} {kind} {other} was not confirmed", "conflict")
    incomplete = copy_problems(adapter.client, report, progress.get("relation_removals", []))
    if apply and incomplete:
        raise TrackerError("; ".join(incomplete), "conflict")
    return {"project": copies.get(source), "projects": dict(copies), "tasks": report["task_map"],
            "incomplete": incomplete,
            "missing_projects": report["missing_projects"], "errors": report["errors"],
            "relation_removals": progress.get("relation_removals", []),
            "external_relations": adapter.preserve_external_relations(report["external_relations"],
                                                                      apply and not report["errors"]),
            "dry_run": not apply,
            "notes": ["Native shares are copied immediately; link share URLs change. Labels remain shared.",
                      "Webhooks and complete history/subscriptions are not migrated. Inspect filters and embedded links.",
                      "Do not rerun clone after normalization; resume normalization by auditing the staging scope."]}


def copy_problems(client, report, relation_removals=()):
    """A saved project mapping alone cannot prove a failed copy finished.

Check preserved data again before cutover; allow added journal comments and text
normalization and explicitly requested internal relation removals, but never
silently lose other source relations, files or assignments.
"""
    removed = set()
    for base, kind, other in relation_removals:
        native = api.TASK_RELATION_KINDS[kind]
        removed.add((base, native, other))
        removed.add((other, api.RELATION_INVERSE[native], base))
    mapping, problems, targets = report["task_map"], [], {}
    for ident, target in mapping.items():
        original = report["source_tasks"][ident]
        current = targets[target] = client.get(f"/tasks/{target}")
        users = {str(u["id"]) for u in original.get("assignees") or []}
        if not users <= {str(u["id"]) for u in current.get("assignees") or []}:
            problems.append(f"Clone task {target} is missing source task {ident} assignees")
        for entity in ("comments", "attachments"):
            before = client.paginate(f"/tasks/{ident}/{entity}")
            after = client.paginate(f"/tasks/{target}/{entity}")
            if len(before) > len(after):
                problems.append(f"Clone task {target} is missing source task {ident} {entity}")
    for base, kind, other in sorted(report["edges"]):
        if base in mapping and other in mapping:
            existing = (targets[mapping[base]].get("related_tasks") or {}).get(kind, [])
            if ((mapping[base], kind, mapping[other]) not in removed
                    and mapping[other] not in {str(t["id"]) for t in existing}):
                problems.append(f"Clone is missing source relation {base} {kind} {other}")
    return problems
