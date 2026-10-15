"""GitHub-specific tracker commands that do not belong to the portable task contract."""
import argparse

from .core import TrackerError


def parser():
    p = argparse.ArgumentParser(prog="agent-box tracker provider github")
    sub = p.add_subparsers(dest="command", required=True)
    cmd = sub.add_parser("board", help="Show or repair a project's Status and Priority options",
                         description="init plans the Status options Backlog, Ready, In progress, In review, Done and "
                                     "the Priority options Immediate, Urgent, High, Medium, Low; --apply writes the plan. "
                                     "Renames keep option IDs, so cards keep their values.")
    cmd.add_argument("operation", choices=("show", "init"))
    cmd.add_argument("project", help="owner/number or the project's URL")
    cmd.add_argument("--apply", action="store_true", help="Write the planned options")
    cmd = sub.add_parser("drafts", help="List or convert draft items, which are not tasks until they are issues",
                         description="list shows the bound board's draft items; convert ITEM turns one into an issue "
                                     "of this repository on the same card, keeping its column.")
    cmd.add_argument("operation", choices=("list", "convert"))
    cmd.add_argument("item", nargs="?", help="Draft item ID printed by drafts list")
    return p


def run(adapter, arguments):
    args = parser().parse_args(arguments)
    if args.command == "drafts":
        if args.operation == "list":
            return adapter.drafts(adapter.bound_project()["id"])
        if not args.item:
            raise TrackerError("Name the draft item to convert")
        return adapter.convert_draft(args.item).to_dict()
    if args.operation == "show":
        return adapter.lifecycle_report([args.project])[0]
    return adapter.initialize_boards([args.project], args.apply)[0]
