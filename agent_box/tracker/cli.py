"""The public tracker command; provider extensions stay explicitly named."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from ..config import effective_config
from ..atomicio import atomic_write_json, read_json
from ..errors import AgentBoxError
from ..paths import WORKFLOW_STATE
from ..repo_binding import Binding, repository_root, write_setup
from .audit import audit_project
from .core import (NORMALIZED_LABEL, STATES, Task, TrackerError, checkboxes, claim, ready_tasks,
                   root_task, set_checkboxes)
from .registry import make_adapter


def text_file(path):
    return sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")


def emit(value):
    print(json.dumps(value.to_dict() if isinstance(value, Task) else value, ensure_ascii=False, indent=2))


def parser():
    p = argparse.ArgumentParser(prog="agent-box tracker", description=(
        "Read and maintain tracker tasks. Output is JSON by default; no --json flag is needed. "
        "get accepts a user reference directly; other task commands take the canonical ID it returns."))
    p.add_argument("--provider", help="Override the configured provider")
    p.add_argument("--adoption", type=Path, help="Use a receipt's staging scope without changing the repository binding")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="Show account, repository binding and lifecycle columns",
                   description="Show account, repository binding and lifecycle columns as JSON.")
    cmd = sub.add_parser("audit", help="Check project workflow structure without changes")
    cmd.add_argument("--project", help="Audit this project without saving a binding")
    cmd.add_argument("--recursive", action="store_true")
    projects = sub.add_parser("projects", help="List projects, optionally filtering their titles")
    projects.add_argument("query", nargs="?", default="")
    cmd = sub.add_parser("resolve", help="Resolve a visible number, exact title or task URL to a canonical ID")
    cmd.add_argument("reference", help="Visible number (21 or #21), exact title or task URL")
    cmd.add_argument("--title", action="store_true", help="Treat the reference as an exact title, including numeric titles")
    cmd.add_argument("--project", help="Narrow resolution to one project inside the binding")
    for name in ("search", "ready", "next"):
        description = {"search": "Find tasks; return compact summaries",
                       "ready": "List Ready tasks eligible for the current user",
                       "next": "Select the first eligible Ready task"}[name]
        cmd = sub.add_parser(name, help=description, description=description + "; output is JSON.")
        if name == "search":
            cmd.add_argument("query", nargs="?", default="")
            cmd.add_argument("--exclude-label", help="Exclude tasks carrying this exact label before output")
        cmd.add_argument("--project")
        cmd.add_argument("--recursive", action="store_true")
    for name in ("get", "comments", "checklist", "claim", "update", "state", "check", "comment", "tier", "relation", "normalized"):
        description = {"get": "Read a full task, including requirements, relations and comments",
                       "comments": "Read task comments",
                       "checklist": "Read the task revision and its checklist indices and text",
                       "claim": "Claim an eligible task and enter Doing",
                       "update": "Update selected task fields",
                       "state": "Move one or more tasks to a lifecycle state",
                       "check": "Check or uncheck checklist items in one write",
                       "comment": "Append a human journal comment from a file or stdin",
                       "tier": "Set the task's execution tier",
                       "normalized": "Mark completed normalization; --undo makes the task a candidate again",
                       "relation": "Add or remove a task relation"}[name]
        cmd = sub.add_parser(name, help=description, description=description + "; output is JSON.")
        several = {"state": "+", "normalized": "*"}
        cmd.add_argument("task", **({"nargs": several[name]} if name in several else {}),
                         help="Visible number, exact title, task URL or canonical ID" if name == "get"
                         else "Canonical task ID returned by resolve/get/search")
        if name == "claim":
            cmd.add_argument("--resume", action="store_true")
        elif name == "state":
            cmd.add_argument("state", choices=STATES)
            cmd.add_argument("--reason", help="Close tasks that were never implemented, journaling this reason")
        elif name == "check":
            cmd.add_argument("index", type=int, nargs="+")
            cmd.add_argument("--revision", required=True, help="Task revision printed by tracker checklist")
            cmd.add_argument("--undo", action="store_true")
        elif name == "comment":
            cmd.add_argument("--file", required=True, help="UTF-8 text file, or - for stdin")
        elif name == "normalized":
            cmd.add_argument("--undo", action="store_true")
            cmd.add_argument("--done", action="store_true",
                             help="Select every completed task in scope instead of naming tasks")
            cmd.add_argument("--project")
            cmd.add_argument("--recursive", action="store_true")
        elif name == "tier":
            cmd.add_argument("tier", type=int, choices=(1, 2, 3))
        elif name == "relation":
            cmd.add_argument("kind", choices=("parent", "child", "blocks", "blocked-by", "related", "duplicate"))
            cmd.add_argument("other")
            cmd.add_argument("--remove", action="store_true")
        elif name == "update":
            cmd.add_argument("--title")
            cmd.add_argument("--description-file")
            cmd.add_argument("--priority", type=int, choices=range(6))
    description = "Move tasks with all their subtasks into another bound project, keeping each lifecycle state"
    cmd = sub.add_parser("move", help=description, description=description + "; output is JSON.")
    cmd.add_argument("task", nargs="+", help="Canonical task ID returned by resolve/get/search")
    cmd.add_argument("--project", required=True, help="Target project inside the repository binding")
    cmd = sub.add_parser("create", help="Create a task from a title and description file")
    cmd.add_argument("--project")
    cmd.add_argument("--title", required=True)
    cmd.add_argument("--description-file", required=True)
    cmd.add_argument("--parent")
    cmd.add_argument("--priority", type=int, choices=range(6), default=0,
                     help="0 unset, 1 low, 2 medium, 3 high, 4 urgent, 5 immediate")
    cmd.add_argument("--tier", type=int, choices=(1, 2, 3))
    cmd = sub.add_parser("setup", help="Preview repository binding; save with --apply")
    cmd.add_argument("--project", required=True)
    cmd.add_argument("--recursive", action="store_true",
                     help="Include every project under the root project in the workflow")
    cmd.add_argument("--apply", action="store_true", help="Save binding and managed repository instructions")
    cmd = sub.add_parser("adopt", help="Clone, normalize or activate a staging project tree")
    cmd.add_argument("operation", choices=("clone", "boards", "cutover"))
    cmd.add_argument("--project", help="Exact source project for clone")
    cmd.add_argument("--receipt", type=Path,
                     help="Default in workflow state: clone uses <repository key>/<source ID>.json; "
                          "boards and cutover use the repository's only receipt")
    cmd.add_argument("--apply", action="store_true")
    cmd = sub.add_parser("provider", help="Run an explicitly provider-specific command")
    cmd.add_argument("provider", choices=("vikunja",))
    cmd.add_argument("arguments", nargs=argparse.REMAINDER)
    return p


def context(provider=None, require_binding=False, adoption=None):
    binding = None
    try:
        root = repository_root()
    except AgentBoxError:
        root = None
    if adoption:
        from .adoption import check_receipt, staging_context
        if root is None:
            raise TrackerError("Adoption needs a repository")
        adapter, binding, root = staging_context(check_receipt(adoption), root)
        if provider and provider != binding.provider:
            raise TrackerError("Provider differs from the adoption receipt", "conflict")
        return adapter, binding, root
    if root:
        binding = Binding.session(root)
    if require_binding and binding is None:
        raise TrackerError("This operation needs a repository binding; run ab-setup")
    return make_adapter(binding, provider), binding, root


def move_journal(root):
    """A move's pending states live in workflow state, beside the repository's other runs.

    After the project changes the card already sits in the default column, so
    this file is the only record of the column it left. It exists only while a
    move is unfinished; workflow state survives a recreated box.
    """
    from ..workflow import repository_key
    return WORKFLOW_STATE / "moves" / f"{repository_key(root)}.json"


def move(adapter, root, tasks, project):
    path = move_journal(root)
    journal = read_json(path) if path.exists() else {"project": project, "tasks": {}}
    if journal["project"] != project:
        raise TrackerError(f"An interrupted move of tasks {', '.join(journal['tasks'])} into project "
                           f"{journal['project']} is unfinished; rerun that move first", "conflict")

    def checkpoint(pending):
        if pending:
            atomic_write_json(path, {"project": project, "tasks": pending}, 0o600)
        else:
            path.unlink(missing_ok=True)

    return adapter.move(tasks, project, journal["tasks"], checkpoint)


def unintegrated(root, adapter, task):
    """Name the branch whose commits closing this task would abandon, if there is one.

    Closing without implementation is meant for work that was never built, so
    the only question Git has to answer is whether the task's branch still holds
    commits the base, or the parent branch it was cut from, does not contain.
    """
    from .. import git_workflow as gw
    try:
        parent = root_task(task, adapter.get)
        if not gw.has_origin(root):
            # Without a remote no other clone can hold this task's commits, and
            # no base can be derived either, so the question Git can still answer
            # is whether this checkout kept a branch of its own.
            left = gw.task_branches(root, task.id)
            return (f"Task {task.id} branch {', '.join(left)} exists in this checkout, which has no origin "
                    f"to judge it against; land or delete it before Done") if left else None
        targets = [gw.default_base(root)]
        if parent.id != task.id:
            targets.append(gw.task_branch(root, parent))
        targets = [name for name in targets if name]
        left = gw.abandoned_work(root, task.id, targets)
    except TrackerError:
        raise
    except AgentBoxError as exc:
        raise TrackerError(str(exc), "configuration") from None
    if left["unfetched"]:
        return (f"Origin has commits on {', '.join(left['unfetched'])} that this checkout has not fetched; "
                f"fetch origin before deciding that task {task.id} has nothing left to land")
    if left["unmerged"]:
        return (f"Task {task.id} branch {', '.join(left['unmerged'])} has commits that "
                f"{' and '.join(targets)} do not contain; run agent-box workflow finish {task.id}, "
                f"or land the work, before Done")
    return None


def dispatch(args):
    if args.adoption and args.command not in {"audit", "status", "search", "resolve", "get", "comments", "checklist",
                                             "update", "tier", "relation", "comment", "check", "state", "normalized"}:
        raise TrackerError("Staging adoption only reads and normalizes existing tasks")
    if args.command == "adopt":
        from . import adoption
        root = repository_root()
        receipt = args.receipt and adoption.check_receipt(args.receipt)
        if args.operation == "clone":
            if not args.project:
                raise TrackerError("Clone needs --project with an exact source ID or path")
            return adoption.start(root, args.project, receipt, args.provider, args.apply)
        args.receipt = receipt or adoption.locate(root)
        if args.provider:
            _, source, _ = adoption.load(args.receipt, root)
            if args.provider != source.provider:
                raise TrackerError("Provider differs from the adoption receipt", "conflict")
        if args.operation == "cutover":
            return adoption.cutover(root, args.receipt, args.apply)
        adapter, binding, _ = adoption.staging_context(args.receipt, root)
        return adapter.initialize_boards(adapter.subtree(binding.project, True), args.apply)
    if args.command == "provider":
        from .extensions import run
        adapter, _, _ = context(args.provider)
        return run(adapter, args.arguments)
    if args.command == "setup":
        project = make_adapter(provider=args.provider).project(args.project)
        binding = Binding(args.provider or effective_config().tracker.provider,
                          effective_config().tracker.connection(args.provider).url,
                          str(project["id"]), args.recursive)
        binding.validate()
        # Binding stores only the project; the boards are checked here so a
        # misconfigured column is reported at setup rather than mid-workflow.
        report = make_adapter(binding, args.provider).lifecycle_report()
        if not args.apply:
            return {"binding": asdict(binding), "lifecycle": report, "apply": False}
        broken = [entry for entry in report if entry["problems"]]
        if broken:
            raise TrackerError("Fix the board lifecycle first: " + "; ".join(
                f"project {entry['project']}: {', '.join(entry['problems'])}" for entry in broken),
                "configuration")
        return {"written": write_setup(repository_root(), binding), "binding": asdict(binding),
                "lifecycle": report}
    adapter, binding, root = context(args.provider, args.command in ("ready", "next", "claim", "state", "move"), args.adoption)
    if args.command == "status":
        user = adapter.identity()
        return {"provider": args.provider or (binding.provider if binding else effective_config().tracker.provider),
                "user": {"id": str(user["id"]), "username": user.get("username")},
                "capabilities": sorted(adapter.capabilities), "binding": asdict(binding) if binding else None,
                "lifecycle": adapter.lifecycle_report() if binding else []}
    if args.command == "audit":
        if args.project:
            config = effective_config()
            provider = args.provider or (binding.provider if binding else config.tracker.provider)
            selected = Binding(provider, config.tracker.connection(provider).url,
                               str(adapter.project(args.project)["id"]), args.recursive)
            adapter, binding = make_adapter(selected, provider), selected
        if binding is None:
            raise TrackerError("Pass --project to audit a project, or run ab-setup")
        return audit_project(adapter, binding.project, binding.recursive)
    if args.command == "projects":
        return [{"id": str(p["id"]), "title": p["title"], "parent": str(p.get("parent_project_id") or "")}
                for p in adapter.projects() if args.query.casefold() in p["title"].casefold()]
    if args.command == "resolve":
        if binding is None:
            raise TrackerError("Task resolution needs a repository binding; run ab-setup")
        project = binding.project
        recursive = binding.recursive
        if args.project:
            project = str(adapter.project(args.project)["id"])
            if project not in adapter.subtree(binding.project, binding.recursive):
                raise TrackerError("Resolution project is outside the repository binding")
            recursive = False
        return adapter.resolve(args.reference, project, recursive, title=args.title)
    if args.command in ("search", "ready", "next", "create"):
        project = args.project or (binding.project if binding else None)
        if not project:
            raise TrackerError("Resolve a project first: pass --project or run ab-setup")
        if args.command == "create":
            project = str(adapter.project(project)["id"])
            if binding and project not in adapter.subtree(binding.project, binding.recursive):
                raise TrackerError("Create the task inside the repository binding; an outside project "
                                   "stays readable and could not be maintained from here")
            task = adapter.create(project, args.title,
                                  text_file(args.description_file), args.parent, priority=args.priority)
            return adapter.set_tier(task.id, args.tier) if args.tier else task
        tasks = adapter.search(project, getattr(args, "query", ""), args.recursive or bool(binding and binding.recursive))
        if args.command == "search" and args.exclude_label:
            tasks = [task for task in tasks if args.exclude_label not in task.labels]
        if args.command in ("ready", "next"):
            tasks = ready_tasks(tasks, adapter.get, str(adapter.identity()["id"]))
        results = [task.to_dict(full=False) for task in tasks]
        return (results[0] if results else None) if args.command == "next" else results
    if args.command == "normalized" and bool(args.task) == args.done:
        raise TrackerError("Mark the tasks you name, or pass --done for every completed task in scope")
    if args.command == "normalized" and args.done:
        if binding is None:
            raise TrackerError("Marking a whole project needs a repository binding; run ab-setup")
        # Selecting a project by name must not reach past the binding: outside
        # projects stay readable, and this command writes to every task it finds.
        scope = set(adapter.subtree(binding.project, binding.recursive or bool(args.adoption)))
        project = str(adapter.project(args.project or binding.project)["id"])
        if project not in scope:
            raise TrackerError("Selected project is outside the repository binding")
        # --recursive widens the search inside the binding; it can never widen the
        # binding itself, so what the search finds beyond it is read and dropped.
        tasks = [found for found in adapter.search(project, "", args.recursive or binding.recursive)
                 if found.project in scope and found.state == "Done"
                 and (NORMALIZED_LABEL in found.labels) == args.undo]
    elif args.command == "get" and binding is not None:
        tasks = [adapter.resolve(args.task, binding.project, binding.recursive, canonical=True)]
    else:
        tasks = [adapter.get(reference) for reference in
                 (args.task if isinstance(args.task, list) else [args.task])]
    task = tasks[0] if tasks else None
    # One boundary for every task write: a project outside the binding stays
    # readable, and naming a task by ID is not a way past that.
    if binding and args.command in {"update", "tier", "relation", "comment", "check", "state", "normalized",
                                    "claim", "move"}:
        scope = set(adapter.subtree(binding.project, binding.recursive or bool(args.adoption)))
        outside = [found.id for found in tasks if found.project not in scope]
        if outside:
            raise TrackerError(("Adoption can only change staging tasks: " if args.adoption else
                                "Tasks outside the repository binding: ") + ", ".join(outside), "conflict")
        if args.command == "relation" and adapter.get(args.other).project not in scope:
            raise TrackerError("Adoption relations must stay inside staging" if args.adoption else
                               "Relations must stay inside the repository binding", "conflict")
    if args.command == "get":
        return {**task.to_dict(), "comments": adapter.comments(task.id)}
    if args.command == "comments":
        return adapter.comments(task.id)
    if args.command == "checklist":
        return {"task": task.id, "revision": task.revision,
                "items": [{key: item[key] for key in ("index", "checked", "text")}
                          for item in checkboxes(task.description)]}
    if args.command == "claim":
        from ..workflow_state import lock
        with lock(root, "claim"):
            task, _ = claim(adapter, task.id, args.resume)
            return task
    if args.command == "update":
        fields = {name: value for name in ("title", "priority") if (value := getattr(args, name)) is not None}
        if args.description_file:
            fields["description"] = text_file(args.description_file)
        if not fields:
            raise TrackerError("No changes requested")
        return adapter.update(task, fields)
    if args.command == "state":
        if args.reason is not None and (not args.reason.strip() or args.state != "Done"):
            raise TrackerError("A reason records closing a task without implementation; "
                               "it belongs to Done and must say something")
        if args.state == "Doing" and not args.adoption:
            raise TrackerError("Use agent-box tracker claim to assign and start a task")
        # Everything this command can see is judged before the first task moves:
        # a batch that stops halfway leaves the user reconstructing what it did.
        # Resolving each board here is what the move would do anyway, so a board
        # that cannot express the lifecycle stops the batch before it starts. It
        # is asked directly: a report would fold a provider outage into the same
        # answer as a misconfigured column and mislabel a retryable failure.
        for project in sorted({found.project for found in tasks}):
            adapter.board(project)
        for found in tasks if args.state == "Done" else []:
            if args.adoption:
                if found.state != "Done":
                    raise TrackerError("Staging adoption cannot mark unfinished work Done")
            elif not args.reason:
                raise TrackerError("Use agent-box workflow finish to verify integration or merge before Done, "
                                   "or pass --reason TEXT to close a task that was never implemented")
            elif reason := unintegrated(root, adapter, found):
                raise TrackerError(reason, "conflict")
        changed = []
        for found in tasks:
            # The journal entry goes first: a task closed without the reason it
            # was closed for is exactly what this command exists to avoid.
            if args.reason:
                adapter.comment(found.id, args.reason)
            # Compact rows: a batch answers with what moved, not with every body it read.
            changed.append(adapter.set_state(found, args.state).to_dict(full=False))
        return changed
    if args.command == "move":
        if "move" not in adapter.capabilities:
            raise TrackerError("This provider cannot move tasks between projects", "configuration")
        project = str(adapter.project(args.project)["id"])
        if project not in scope:
            raise TrackerError("Target project is outside the repository binding", "conflict")
        return move(adapter, root, tasks, project)
    if args.command == "check":
        if not task.revision:
            raise TrackerError("This provider returned no revision for the task, so an edit cannot prove it "
                               "read the current checklist", "configuration")
        if task.revision != args.revision:
            raise TrackerError("Task changed since that revision; reread tracker checklist before editing", "conflict")
        return adapter.update(task, {"description": set_checkboxes(task.description, args.index, not args.undo)})
    if args.command == "comment":
        return adapter.comment(task.id, text_file(args.file))
    if args.command == "normalized":
        return [adapter.set_normalized(found.id, not args.undo).to_dict(full=False) for found in tasks]
    if args.command == "tier":
        return adapter.set_tier(task.id, args.tier)
    if args.command == "relation":
        if args.adoption:
            from . import adoption
            return adoption.relate(root, args.adoption, adapter, task, args.kind, args.other, args.remove)
        return adapter.relate(task.id, args.kind, args.other, args.remove)
    raise TrackerError("Unsupported tracker operation")


def main(argv):
    try:
        result = dispatch(parser().parse_args(argv))
        emit(result)
        if isinstance(result, dict):
            verification = result.get("verification")
            if isinstance(verification, dict) and verification.get("ok") is False or result.get("ok") is False and "errors" in result:
                return 3
        return 0
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc), "category": "input"}), file=sys.stderr)
        return 2
    except TrackerError as exc:
        print(json.dumps({"error": str(exc), "category": exc.category, "retryable": exc.retryable}), file=sys.stderr)
        return 2
