"""Staging adoption switches the binding only after audit and both root renames.

The receipt names an operation, not a saved tracker state. Every continuation
reads the actual projects and binding. An interruption leaves usable projects;
there is no compensating deletion or tracker restoration.
"""
from dataclasses import asdict, replace
from pathlib import Path

from ..atomicio import atomic_write_json, read_json
from ..paths import WORKFLOW_STATE
from ..repo_binding import (BINDING_FILE, Binding, server_identity, setup_content,
                            write_binding, write_setup)
from .audit import audit_project
from .core import TrackerError, relation_key
from .registry import make_adapter


def current_binding(root):
    from ..project_config import load
    return Binding.load(root) if "project" in load(root / BINDING_FILE).get("tracker", {}) else None


def in_container():
    return Path("/run/.containerenv").is_file()


def receipts(root):
    """Receipts live in workflow state: it is mounted into the box, so they survive a recreated container."""
    from ..workflow import repository_key
    return WORKFLOW_STATE / "adoptions" / repository_key(root)


def check_receipt(path):
    """A box's own filesystem is discarded with the box, taking the only clone mapping with it."""
    if in_container() and WORKFLOW_STATE.resolve() not in path.resolve().parents:
        raise TrackerError(f"Inside a workspace box keep the adoption receipt under {WORKFLOW_STATE}; "
                           f"omit --receipt to use {WORKFLOW_STATE / 'adoptions'}")
    return path


def locate(root):
    found = sorted(receipts(root).glob("*.json"))
    if len(found) != 1:
        raise TrackerError(f"Found {len(found)} adoption receipts in {receipts(root)}; "
                           + ("pass --receipt PATH" if found else "run tracker adopt clone first"))
    return found[0]


def save(path, receipt):
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise TrackerError(f"Refusing receipt path {path}")
    atomic_write_json(path, receipt, 0o600)


def load(path, root):
    if path.is_symlink():
        raise TrackerError(f"Refusing receipt symlink {path}")
    receipt = read_json(path)
    try:
        binding = Binding(**receipt["source"])
        binding.validate()
        copies = receipt["clone"]["projects"]
        if not isinstance(copies, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                               for k, v in copies.items()):
            raise ValueError("clone.projects must map source ID strings to new ID strings")
        tasks = receipt["clone"].get("tasks", {})
        if (not isinstance(tasks, dict) or any(not isinstance(k, str) or not k.isdigit()
                                               or not isinstance(v, str) or not v.isdigit()
                                               for k, v in tasks.items())
                or len(set(tasks.values())) != len(tasks)):
            raise ValueError("clone.tasks must map source task ID strings to distinct staging task ID strings")
        removed = receipt["clone"].get("removed", [])
        if (not isinstance(removed, list) or len(set(removed)) != len(removed)
                or any(not isinstance(ident, str) or ident not in copies or ident == binding.project
                       for ident in removed)):
            raise ValueError("clone.removed must list distinct copied source subprojects")
        if removed and "tasks" not in receipt["clone"]:
            raise ValueError("clone.removed needs the clone.tasks mapping saved before the first removal")
        removals = receipt["clone"].get("relation_removals", [])
        if not isinstance(removals, list):
            raise ValueError("clone.relation_removals must be a list")
        for edge in removals:
            if (not isinstance(edge, list) or len(edge) != 3
                    or any(not isinstance(value, str) or not value for value in edge)
                    or edge != list(relation_key(*edge))):
                raise ValueError("clone.relation_removals must contain canonical task/kind/other triples")
        for field in ("repository", "original_title", "staging_title", "backup_title", "parent"):
            if not isinstance(receipt[field], str):
                raise ValueError(f"{field} must be a string")
        if receipt["previous_binding"] is not None:
            Binding(**receipt["previous_binding"]).validate()
        if not binding.recursive:
            raise ValueError("adoption must cover the complete project tree")
    except (KeyError, TypeError, ValueError) as exc:
        raise TrackerError(f"Invalid adoption receipt {path}: {exc}") from None
    if receipt.get("repository") != str(root.resolve()):
        raise TrackerError("Adoption receipt belongs to another repository", "conflict")
    adapter = make_adapter(binding)
    if not {"clone_project_tree", "rename_project"} <= adapter.capabilities:
        raise TrackerError("Provider does not support staging adoption", "unsupported")
    return receipt, binding, adapter


def staging_context(path, root):
    receipt, source, adapter = load(path, root)
    copies = receipt["clone"].get("projects", {})
    target = copies.get(source.project)
    if not target or receipt["clone"].get("pending"):
        raise TrackerError("Clone is incomplete; inspect or resume it before normalization", "conflict")
    if set(copies) != set(adapter.subtree(source.project, True)):
        raise TrackerError("Clone does not cover the complete source tree", "conflict")
    if set(copies.values()) & set(copies):
        raise TrackerError("Staging and source projects overlap", "conflict")
    # A removed copy may still exist when its delete was interrupted; rerunning
    # delete-project, which needs this context, is how it goes.
    gone = {copies[ident] for ident in receipt["clone"].get("removed", [])}
    if set(adapter.subtree(target, True)) - gone != set(copies.values()) - gone:
        raise TrackerError("Staging tree differs from the clone mapping", "conflict")
    if adapter.project(target)["title"] != receipt["staging_title"]:
        raise TrackerError("Staging was renamed; inspect cutover before normalizing", "conflict")
    if current_binding(root) != (Binding(**receipt["previous_binding"]) if receipt["previous_binding"] else None):
        raise TrackerError("Repository binding changed; inspect cutover before normalizing", "conflict")
    binding = replace(source, project=target)
    return make_adapter(binding), binding, root


def relate(root, path, adapter, task, kind, other, remove=False):
    """Persist exact edit intent before the API call so interrupted retries are safe."""
    receipt, _, _ = load(path, root)
    key = list(relation_key(task.id, kind, other))
    removals = receipt["clone"].setdefault("relation_removals", [])
    if remove and key not in removals:
        removals.append(key)
        save(path, receipt)
    elif not remove and key in removals:
        removals.remove(key)
        save(path, receipt)
    return adapter.relate(task.id, kind, other, remove)


def freeze_tasks(root, path):
    """Save the source -> staging task mapping before the first move changes a visible number.

    Until then the copy is verified by pairing numbers inside each project pair;
    a moved task gets a new number, and only a mapping saved while the numbers
    still pair up keeps cutover able to prove nothing was lost.
    """
    receipt, source, adapter = load(path, root)
    if "tasks" in receipt["clone"]:
        return
    copied = adapter.clone_project_tree(source.project, receipt["staging_title"], receipt["clone"], lambda _: None)
    # Saving the mapping ends clone resumption, so an unfinished copy must be
    # finished by clone first; afterwards only cutover's own report could say so.
    problems = [*copied["errors"], *copied["incomplete"]] + (
        ["clone is missing source projects"] if copied["missing_projects"] else [])
    if problems:
        raise TrackerError("Staging is not a complete, paired copy of its source, so a task mapping cannot be "
                           "saved before a move; finish or inspect the clone first: " + "; ".join(problems),
                           "conflict")
    receipt["clone"]["tasks"] = copied["tasks"]
    save(path, receipt)


def delete_project(root, path, adapter, project):
    """Record an emptied staging subproject in clone.removed before deleting it.

    Staging is checked against clone.projects, so a copy deleted without a
    record reads as a lost one and blocks cutover; its tasks, moved out first,
    are verified through the task mapping saved here if no move saved it yet.
    """
    def checkpoint(staging):
        # Only after the adapter's checks: a refused delete leaves the receipt as it was.
        freeze_tasks(root, path)
        receipt, _, _ = load(path, root)
        ident = next((source for source, copy in receipt["clone"]["projects"].items() if copy == staging), None)
        if ident is None:
            raise TrackerError(f"Project {staging} is not a staging copy", "conflict")
        removed = receipt["clone"].setdefault("removed", [])
        if ident not in removed:
            removed.append(ident)
            save(path, receipt)
    return adapter.delete_project(project, checkpoint)


def working_language():
    from ..config import effective_config
    return effective_config().workflow.language


def start(root, project, path=None, provider=None, apply=False):
    root = root.resolve()
    if path is None:
        from ..config import effective_config
        adapter = make_adapter(provider=provider or effective_config().tracker.provider)
        path = receipts(root) / f"{adapter.project(project)['id']}.json"
    path = path.resolve() if not path.is_symlink() else path
    if path in {root / name for name in (BINDING_FILE, "AGENTS.md", "CLAUDE.md")} or root / ".git" in path.parents:
        raise TrackerError("Keep the adoption receipt outside repository control files")
    if path.exists() or path.is_symlink():
        receipt, source, adapter = load(path, root)
        if str(adapter.project(project)["id"]) != source.project or (provider and provider != source.provider):
            raise TrackerError("Receipt names another source project", "conflict")
        if adapter.project(source.project)["title"] != receipt["original_title"]:
            raise TrackerError("Source was renamed; inspect cutover instead of cloning again", "conflict")
    else:
        from ..config import effective_config
        config = effective_config()
        provider = provider or config.tracker.provider
        adapter = make_adapter(provider=provider)
        if not {"clone_project_tree", "rename_project"} <= adapter.capabilities:
            raise TrackerError("Provider does not support staging adoption", "unsupported")
        original = adapter.project(project)
        source = Binding(provider, server_identity(config.tracker.connection(provider).url), str(original["id"]), True)
        previous = current_binding(root)
        if previous and (previous.provider, server_identity(previous.server), previous.project) != (
                source.provider, source.server, source.project):
            raise TrackerError("Repository is bound to another source project", "conflict")
        receipt = {"repository": str(root), "source": asdict(source),
                   "previous_binding": asdict(previous) if previous else None,
                   "original_title": original["title"], "parent": str(original.get("parent") or ""),
                   "staging_title": f"{original['title']} — staging {source.project}",
                   "backup_title": f"{original['title']} — backup {source.project}", "clone": {"projects": {}}}
    setup_content(root, source)
    for sibling in adapter.projects():
        if str(sibling.get("parent") or "") == receipt["parent"] and str(sibling["id"]) not in receipt["clone"]["projects"].values():
            if sibling["title"] in (receipt["staging_title"], receipt["backup_title"]):
                raise TrackerError(f"Adoption name already exists on project {sibling['id']}; inspect that project", "conflict")

    def checkpoint(progress):
        receipt["clone"] = progress
        save(path, receipt)

    # Preview includes provider checks before either a receipt or tracker write.
    result = adapter.clone_project_tree(source.project, receipt["staging_title"], receipt["clone"], checkpoint)
    if apply:
        if result["errors"]:
            raise TrackerError("; ".join(result["errors"]), "conflict")
        checkpoint(receipt["clone"])
        result = adapter.clone_project_tree(source.project, receipt["staging_title"], receipt["clone"], checkpoint, True)
    return {**result, "receipt": str(path), "source": source.project,
            "staging_title": receipt["staging_title"], "backup_title": receipt["backup_title"]}


def cutover(root, path, apply=False):
    if not apply:
        return check_cutover(root, path)
    from ..workflow_state import lock
    # A staging move started after the journal check could be interrupted once
    # the roots are renamed, when no --adoption rerun can finish it any more.
    with lock(root, "move"):
        return check_cutover(root, path, True)


def check_cutover(root, path, apply=False):
    receipt, source, adapter = load(path, root)
    target = receipt["clone"].get("projects", {}).get(source.project)
    if not target:
        raise TrackerError("Receipt has no staging root", "conflict")
    binding = replace(source, project=target)
    previous = Binding(**receipt["previous_binding"]) if receipt["previous_binding"] else None
    current = current_binding(root)
    old, new = adapter.project(source.project), adapter.project(target)
    expected = (receipt["backup_title"], receipt["original_title"])
    actual = (old["title"], new["title"])
    if current == binding and actual == expected:
        copied = adapter.clone_project_tree(source.project, receipt["staging_title"], receipt["clone"], lambda _: None)
        return {"complete": True, "source": source.project, "project": target, "binding": asdict(binding),
                "clone": copied}
    if current != previous:
        raise TrackerError("Repository binding differs from the expected pre-cutover binding", "conflict")
    allowed = {(receipt["original_title"], receipt["staging_title"]),
               (receipt["backup_title"], receipt["staging_title"]), expected}
    if actual not in allowed or any(str(p.get("parent") or "") != receipt["parent"] for p in (old, new)):
        raise TrackerError("Project names or parents changed outside this cutover; inspect by ID", "conflict")
    if any(str(p["id"]) not in {source.project, target} and p["title"] in expected
           for p in adapter.projects()
           if str(p.get("parent") or "") == receipt["parent"]):
        raise TrackerError("Cutover names conflict with another sibling project", "conflict")
    copied = adapter.clone_project_tree(source.project, receipt["staging_title"], receipt["clone"], lambda _: None)
    errors = [*copied["errors"], *copied["incomplete"]]
    if copied["missing_projects"]:
        errors.append("Clone is missing source projects")
    report = audit_project(make_adapter(binding), target, language=working_language())
    if not report["clean"]:
        errors.append("Staging audit has structural errors; resolve them before cutover")
    from .cli import move_journal
    if move_journal(root).exists():
        # After the rename no --adoption move can run, and the journal is the
        # only record of the column an interrupted staging move left.
        errors.append(f"An interrupted move is unfinished ({move_journal(root)}); rerun it before cutover, "
                      "with tracker --adoption when it moved staging tasks")
    setup_content(root, binding)
    result = {"source": source.project, "project": target, "names": list(expected),
              "binding": asdict(binding), "audit": report, "clone": copied,
              "errors": errors, "ok": not errors, "dry_run": not apply}
    if not apply:
        return result
    if errors:
        raise TrackerError("; ".join(errors), "conflict")
    copied["external_relations"] = adapter.preserve_external_relations(copied["external_relations"], True)
    report = audit_project(make_adapter(binding), target, language=working_language())
    result["audit"] = report
    if not report["clean"]:
        raise TrackerError("Staging audit has structural errors; resolve them before cutover", "conflict")
    write_setup(root, binding, instructions_only=True)
    adapter.rename_project(source.project, expected[0])
    adapter.rename_project(target, expected[1])
    if current_binding(root) != previous:
        raise TrackerError("Binding changed during cutover; project names are ready, inspect binding", "conflict")
    write_binding(root, binding)
    if current_binding(root) != binding:
        raise TrackerError("Binding switch was not confirmed", "conflict")
    return {**result, "complete": True}
