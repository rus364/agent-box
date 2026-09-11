"""The public tracker command; provider extensions stay explicitly named."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from ..config import effective_config
from ..errors import AgentBoxError
from ..repo_binding import Binding, repository_root, write_setup
from .audit import audit_project
from .core import STATES, Task, TrackerError, checkboxes, claim, ready_tasks, set_checkbox
from .registry import make_adapter


def text_file(path):
    return sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")


def emit(value):
    print(json.dumps(value.to_dict() if isinstance(value, Task) else value, ensure_ascii=False, indent=2))


def parser():
    p = argparse.ArgumentParser(prog="agent-box tracker", description=(
        "Read and maintain tracker tasks. Output is JSON by default; no --json flag is needed. "
        "Use resolve for a user reference, then get ID for a full task."))
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
        cmd.add_argument("--project")
        cmd.add_argument("--recursive", action="store_true")
    for name in ("get", "comments", "checklist", "claim", "update", "state", "check", "comment", "tier", "relation"):
        description = {"get": "Read a full task, including requirements, relations and comments",
                       "comments": "Read task comments",
                       "checklist": "Read checklist indices and text",
                       "claim": "Claim an eligible task and enter Doing",
                       "update": "Update selected task fields",
                       "state": "Move a task to a lifecycle state",
                       "check": "Check or uncheck an exact checklist item",
                       "comment": "Append a human journal comment from a file or stdin",
                       "tier": "Set the task's execution tier",
                       "relation": "Add or remove a task relation"}[name]
        cmd = sub.add_parser(name, help=description, description=description + "; output is JSON.")
        cmd.add_argument("task", help="Canonical task ID returned by resolve/get/search")
        if name == "claim":
            cmd.add_argument("--resume", action="store_true")
        elif name == "state":
            cmd.add_argument("state", choices=STATES)
        elif name == "check":
            cmd.add_argument("index", type=int)
            cmd.add_argument("--text", required=True, help="Expected current checklist item text")
            cmd.add_argument("--undo", action="store_true")
        elif name == "comment":
            cmd.add_argument("--file", required=True, help="UTF-8 text file, or - for stdin")
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
    cmd.add_argument("--receipt", type=Path, required=True)
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
        from .adoption import staging_context
        if root is None:
            raise TrackerError("Adoption needs a repository")
        adapter, binding, root = staging_context(adoption, root)
        if provider and provider != binding.provider:
            raise TrackerError("Provider differs from the adoption receipt", "conflict")
        return adapter, binding, root
    if root and ((root / ".agent-box.json").exists() or (root / ".agent-box.json").is_symlink()):
        from ..project_config import load
        if "project" in load(root / ".agent-box.json").get("tracker", {}):
            binding = Binding.load(root)
    if require_binding and binding is None:
        raise TrackerError("This operation needs a repository binding; run ab-setup")
    return make_adapter(binding, provider), binding, root


def dispatch(args):
    if args.adoption and args.command not in {"audit", "status", "search", "resolve", "get", "comments", "checklist",
                                             "update", "tier", "relation", "comment", "check", "state"}:
        raise TrackerError("Staging adoption only reads and normalizes existing tasks")
    if args.command == "adopt":
        from . import adoption
        root = repository_root()
        if args.operation == "clone":
            if not args.project:
                raise TrackerError("Clone needs --project with an exact source ID or path")
            return adoption.start(root, args.project, args.receipt, args.provider, args.apply)
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
    adapter, binding, root = context(args.provider, args.command in ("ready", "next", "claim", "state"), args.adoption)
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
            task = adapter.create(str(adapter.project(project)["id"]), args.title,
                                  text_file(args.description_file), args.parent, priority=args.priority)
            return adapter.set_tier(task.id, args.tier) if args.tier else task
        tasks = adapter.search(project, getattr(args, "query", ""), args.recursive or bool(binding and binding.recursive))
        if args.command in ("ready", "next"):
            tasks = ready_tasks(tasks, adapter.get, str(adapter.identity()["id"]))
        results = [task.to_dict(full=False) for task in tasks]
        return (results[0] if results else None) if args.command == "next" else results
    task = adapter.get(args.task)
    if args.adoption and args.command in {"update", "tier", "relation", "comment", "check", "state"}:
        scope = set(adapter.subtree(binding.project, True))
        if task.project not in scope:
            raise TrackerError("Adoption can only change staging tasks", "conflict")
        if args.command == "relation" and adapter.get(args.other).project not in scope:
            raise TrackerError("Adoption relations must stay inside staging", "conflict")
    if args.command == "get":
        return {**task.to_dict(), "comments": adapter.comments(task.id)}
    if args.command == "comments":
        return adapter.comments(task.id)
    if args.command == "checklist":
        return [{key: item[key] for key in ("index", "checked", "text")} for item in checkboxes(task.description)]
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
        if args.state == "Done" and (not args.adoption or task.state != "Done"):
            raise TrackerError("Use agent-box workflow finish to verify integration or merge before Done")
        if args.state == "Doing" and not args.adoption:
            raise TrackerError("Use agent-box tracker claim to assign and start a task")
        return adapter.set_state(task, args.state)
    if args.command == "check":
        return adapter.update(task, {"description": set_checkbox(task.description, args.index, not args.undo, args.text)})
    if args.command == "comment":
        return adapter.comment(task.id, text_file(args.file))
    if args.command == "tier":
        return adapter.set_tier(task.id, args.tier)
    if args.command == "relation":
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
