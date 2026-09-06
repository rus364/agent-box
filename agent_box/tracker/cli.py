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
from .audit import audit
from .core import STATES, Task, TrackerError, checkboxes, claim, ready_tasks, set_checkbox
from .registry import make_adapter


def text_file(path):
    return sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")


def emit(value):
    print(json.dumps(value.to_dict() if isinstance(value, Task) else value, ensure_ascii=False, indent=2))


def parser():
    p = argparse.ArgumentParser(prog="agent-box tracker")
    p.add_argument("--provider", help="Override the configured provider")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    cmd = sub.add_parser("audit")
    cmd.add_argument("--project", help="Audit boards before a binding exists")
    cmd.add_argument("--recursive", action="store_true")
    projects = sub.add_parser("projects")
    projects.add_argument("query", nargs="?", default="")
    for name in ("search", "ready", "next"):
        cmd = sub.add_parser(name)
        if name == "search":
            cmd.add_argument("query", nargs="?", default="")
        cmd.add_argument("--project")
        cmd.add_argument("--recursive", action="store_true")
    for name in ("get", "comments", "checklist", "claim", "update", "state", "check", "comment", "tier", "relation"):
        cmd = sub.add_parser(name)
        cmd.add_argument("task")
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
    cmd = sub.add_parser("create")
    cmd.add_argument("--project")
    cmd.add_argument("--title", required=True)
    cmd.add_argument("--description-file", required=True)
    cmd.add_argument("--parent")
    cmd.add_argument("--tier", type=int, choices=(1, 2, 3))
    cmd = sub.add_parser("setup")
    cmd.add_argument("--project", required=True)
    cmd.add_argument("--recursive", action="store_true",
                     help="Include every project under the root project in the workflow")
    cmd.add_argument("--apply", action="store_true", help="Save binding and managed repository instructions")
    cmd = sub.add_parser("provider")
    cmd.add_argument("provider", choices=("vikunja",))
    cmd.add_argument("arguments", nargs=argparse.REMAINDER)
    return p


def context(provider=None, require_binding=False):
    binding = None
    try:
        root = repository_root()
    except AgentBoxError:
        root = None
    if root and ((root / ".agent-box.json").exists() or (root / ".agent-box.json").is_symlink()):
        binding = Binding.load(root)
    if require_binding and binding is None:
        raise TrackerError("This operation needs a repository binding; run ab-setup")
    return make_adapter(binding, provider), binding, root


def dispatch(args):
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
    adapter, binding, root = context(args.provider, args.command in ("ready", "next", "claim", "state"))
    if args.command == "status":
        user = adapter.identity()
        return {"provider": args.provider or (binding.provider if binding else effective_config().tracker.provider),
                "user": {"id": str(user["id"]), "username": user.get("username")},
                "capabilities": sorted(adapter.capabilities), "binding": asdict(binding) if binding else None,
                "lifecycle": adapter.lifecycle_report() if binding else []}
    if args.command == "audit":
        # The bound subtree is the scope: a task outside it has no lifecycle
        # state to audit, so auditing one would report every card as stray.
        # Boards are audited without a binding because they are repaired first.
        if binding is None:
            if not args.project:
                raise TrackerError("Pass --project to audit boards, or run ab-setup to bind this repository")
            return audit(None, lambda task_id: None,
                         adapter.lifecycle_report(adapter.subtree(args.project, args.recursive)))
        if args.project and str(adapter.project(args.project)["id"]) != binding.project:
            raise TrackerError(f"This repository is bound to project {binding.project}; "
                               "the audit runs on the bound subtree")
        tasks = adapter.search(binding.project, "", binding.recursive)
        index = {task.id: task for task in tasks}
        return audit(tasks, lambda task_id: index.get(task_id) or adapter.get(task_id),
                     adapter.lifecycle_report())
    if args.command == "projects":
        return [{"id": str(p["id"]), "title": p["title"], "parent": str(p.get("parent_project_id") or "")}
                for p in adapter.projects() if args.query.casefold() in p["title"].casefold()]
    if args.command in ("search", "ready", "next", "create"):
        project = args.project or (binding.project if binding else None)
        if not project:
            raise TrackerError("Resolve a project first: pass --project or run ab-setup")
        if args.command == "create":
            task = adapter.create(str(adapter.project(project)["id"]), args.title,
                                  text_file(args.description_file), args.parent)
            return adapter.set_tier(task.id, args.tier) if args.tier else task
        tasks = adapter.search(project, getattr(args, "query", ""), args.recursive or bool(binding and binding.recursive))
        if args.command in ("ready", "next"):
            tasks = ready_tasks(tasks, adapter.get, str(adapter.identity()["id"]))
        results = [task.to_dict(full=False) for task in tasks]
        return (results[0] if results else None) if args.command == "next" else results
    task = adapter.get(args.task)
    if args.command == "get":
        return {**task.to_dict(), "comments": adapter.comments(task.id)}
    if args.command == "comments":
        return adapter.comments(task.id)
    if args.command == "checklist":
        return [{key: item[key] for key in ("index", "checked", "text")} for item in checkboxes(task.description)]
    if args.command == "claim":
        from ..workflow_state import lock
        with lock(root, "claim"):
            return claim(adapter, task.id, args.resume)
    if args.command == "update":
        fields = {name: value for name in ("title", "priority") if (value := getattr(args, name)) is not None}
        if args.description_file:
            fields["description"] = text_file(args.description_file)
        if not fields:
            raise TrackerError("No changes requested")
        return adapter.update(task, fields)
    if args.command == "state":
        if args.state == "Done":
            raise TrackerError("Use agent-box workflow finish to verify integration or merge before Done")
        if args.state == "Doing":
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
