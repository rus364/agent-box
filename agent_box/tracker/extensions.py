"""Explicit product operations that do not belong to the portable task contract."""
import argparse
import json
from pathlib import Path

from ..atomicio import atomic_write_json
from ..config import effective_config
from ..repo_binding import server_identity
from . import vikunja_api as api
from .core import TrackerError


def parser():
    p = argparse.ArgumentParser(prog="agent-box tracker provider vikunja")
    sub = p.add_subparsers(dest="command", required=True)
    cmd = sub.add_parser("import")
    cmd.add_argument("operation", choices=("validate", "plan", "verify"))
    cmd.add_argument("file", type=Path)
    cmd.add_argument("--state-dir", type=Path, default=api.DEFAULT_STATE_DIR)
    cmd.add_argument("--apply", action="store_true")
    cmd.add_argument("--create-root", action="store_true")
    cmd = sub.add_parser("bulk-update")
    cmd.add_argument("file", type=Path)
    cmd.add_argument("--apply", action="store_true")
    cmd = sub.add_parser("board")
    cmd.add_argument("operation", choices=("show", "buckets", "move", "init"))
    cmd.add_argument("project")
    cmd.add_argument("--view")
    cmd.add_argument("--recursive", action="store_true",
                     help="Include every project under this one")
    cmd.add_argument("--apply", action="store_true", help="Create the missing lifecycle columns")
    cmd.add_argument("--task")
    cmd.add_argument("--bucket")
    cmd = sub.add_parser("projects")
    cmd.add_argument("operation", choices=("tree", "children", "create", "update", "delete"))
    cmd.add_argument("project", nargs="?")
    cmd.add_argument("--parent")
    cmd.add_argument("--title")
    cmd.add_argument("--description-file", type=Path)
    cmd.add_argument("--recursive", action="store_true")
    cmd.add_argument("--archived", choices=("true", "false"))
    cmd.add_argument("--force", action="store_true")
    cmd = sub.add_parser("labels")
    cmd.add_argument("operation", choices=("list", "create", "add", "remove", "delete"))
    cmd.add_argument("label", nargs="?")
    cmd.add_argument("--task")
    cmd.add_argument("--force", action="store_true")
    cmd = sub.add_parser("snapshot")
    cmd.add_argument("--project")
    cmd.add_argument("--recursive", action="store_true")
    cmd.add_argument("--out", type=Path, help="Write the snapshot to this file instead of stdout")
    cmd = sub.add_parser("restore")
    cmd.add_argument("file", type=Path)
    cmd.add_argument("--apply", action="store_true")
    cmd.add_argument("--allow-partial", action="store_true",
                     help="Restore everything that can be restored, leaving the blockers reported")
    cmd = sub.add_parser("external")
    cmd.add_argument("reference")
    cmd = sub.add_parser("task-fields")
    cmd.add_argument("task")
    cmd.add_argument("--file", type=Path, required=True)
    cmd = sub.add_parser("delete-task")
    cmd.add_argument("task")
    cmd.add_argument("--force", action="store_true")
    return p


def snapshot_identity(provider="vikunja"):
    """Which tracker a snapshot belongs to.

    Read from the configuration rather than the client, because a container
    reaches the same server through a rewritten URL; the binding compares
    identity the same way.
    """
    return {"provider": provider,
            "server": server_identity(effective_config().tracker.connection(provider).url)}


def run(adapter, arguments):
    args = parser().parse_args(arguments)
    operation = getattr(args, "operation", "")
    client = adapter.client
    if args.command == "import":
        plan = api.parse_plan(args.file)
        if operation == "validate":
            return api.validation_summary(plan)
        if operation == "verify":
            return api.verify_plan(client, plan)
        importer = api.Importer(client, plan, args.state_dir, allow_create_root=args.create_root)
        return importer.apply() if args.apply else importer.dry_run()
    if args.command == "snapshot":
        snapshot = api.make_snapshot(client, args.project, args.recursive, snapshot_identity())
        if not args.out:
            return snapshot
        # A snapshot is the only way back from adoption, so its path is part of
        # the result rather than something the caller has to remember.
        atomic_write_json(args.out, snapshot)
        return {"path": str(args.out.resolve()), "projects": len(snapshot["projects"]),
                "tasks": len(snapshot["tasks"])}
    if args.command == "restore":
        return api.restore(client, json.loads(args.file.read_text()),
                           snapshot_identity(), args.apply, args.allow_partial)
    if args.command == "external":
        task = api.TaskIndex(api.inventory_from_client(client).tasks).resolve(args.reference)
        return {"task_id": task["id"], "external_id": api.external_id_from_description(task.get("description", ""))}
    if args.command == "bulk-update":
        inventory = api.inventory_from_client(client)
        actions = api.plan_bulk_updates(json.loads(args.file.read_text()), inventory.tasks)
        if args.apply:
            for action in actions:
                current = client.get(f"/tasks/{action.key}")
                expected = next(task for task in inventory.tasks if str(task["id"]) == str(action.key))
                if current.get("updated") != expected.get("updated"):
                    raise TrackerError(f"Task {action.key} changed during bulk update", "conflict")
                changes = action.details["changes"]
                client.patch(f"/tasks/{action.key}", changes, markdown="description" in changes)
        return {**api.summarize_actions(actions), "dry_run": not args.apply, "applied": len(actions) if args.apply else 0}
    if args.command == "board":
        if operation == "init":
            return adapter.initialize_boards(adapter.subtree(args.project, args.recursive), args.apply)
        project = adapter.project(args.project)
        view = api.resolve_kanban_view(client, project["id"], args.view)
        if operation == "show":
            return {"project": project["id"], "view": view, "columns": api.board_columns(client, project["id"], view["id"])}
        buckets = api.view_buckets(client, project["id"], view["id"])
        if operation == "buckets":
            return buckets
        if not args.task or not args.bucket:
            raise TrackerError("Board move requires --task and --bucket")
        task = adapter.get(args.task)
        if task.project != str(project["id"]):
            raise TrackerError("Task is outside the selected project")
        bucket = api.resolve_bucket(buckets, args.bucket)
        api.place_in_bucket(client, project["id"], view, bucket, task.id)
        return adapter.get(task.id).to_dict()
    if args.command == "projects":
        projects = adapter.projects()
        if operation == "tree":
            return api.project_tree(projects)
        if operation == "children":
            return api.project_children(projects, args.project, args.recursive)
        if operation == "create":
            if not args.title:
                raise TrackerError("Project creation requires --title")
            body = {"title": args.title}
            if args.parent:
                body["parent_project_id"] = adapter.project(args.parent)["id"]
            if args.description_file:
                body["description"] = args.description_file.read_text()
            return client.post("/projects?format=markdown", body)
        project = api.ProjectIndex(projects).resolve(args.project or "")
        if operation == "delete":
            if not args.force:
                raise TrackerError("Project deletion includes its tasks and children; inspect a snapshot and pass --force")
            client.delete(f"/projects/{project['id']}")
            return {"deleted": project["id"]}
        fields = {}
        if args.title is not None:
            fields["title"] = args.title
        if args.description_file:
            fields["description"] = args.description_file.read_text()
        if args.archived is not None:
            fields["is_archived"] = args.archived == "true"
        if not fields:
            raise TrackerError("No project changes requested")
        client.get(f"/projects/{project['id']}")
        return client.patch(f"/projects/{project['id']}", fields, markdown="description" in fields)
    if args.command == "labels":
        labels = client.paginate("/labels")
        if operation == "list":
            return labels
        if not args.label:
            raise TrackerError("Label title or ID is required")
        if operation == "create":
            return client.post("/labels", {"title": args.label})
        matches = [label for label in labels if str(label["id"]) == args.label or label["title"] == args.label]
        if len(matches) != 1:
            raise TrackerError("Resolve one exact label first")
        label = matches[0]
        if operation == "delete":
            if not args.force:
                raise TrackerError("Label deletion affects all tasks; pass --force after inspection")
            client.delete(f"/labels/{label['id']}")
        elif not args.task:
            raise TrackerError("Pass --task")
        elif operation == "add":
            api.attach_label(client, adapter.get(args.task).id, label["id"])
        else:
            client.delete(f"/tasks/{int(args.task)}/labels/{label['id']}")
        return {"operation": operation, "label": label["id"]}
    task = adapter.get(args.task)
    if args.command == "delete-task":
        if not args.force:
            raise TrackerError("Inspect the task and pass --force to delete it")
        client.delete(f"/tasks/{task.id}")
        return {"deleted": task.id}
    fields = json.loads(args.file.read_text())
    if not isinstance(fields, dict) or fields.keys() - {"due_date", "start_date", "end_date", "percent_done", "hex_color", "is_favorite", "project_id"}:
        raise TrackerError("task-fields accepts only product-specific scheduling, color, favorite, and project fields")
    client.patch(f"/tasks/{task.id}", fields)
    return adapter.get(task.id).to_dict()
