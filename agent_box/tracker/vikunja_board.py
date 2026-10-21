"""Reviewed board intent is temporary; native columns remain the lifecycle authority.

The agent supplies meaning, not the executor. A saved plan binds that decision
to one observed board and never authorizes deleting columns or moving cards.
"""
import copy
import json
import math
from urllib.parse import urlencode

from ..repo_binding import server_identity
from . import vikunja_api as api
from .core import STATES, TrackerError


VIEW_FIELDS = ("id", "title", "view_kind", "position", "filter", "bucket_configuration_mode",
               "bucket_configuration", "default_bucket_id", "done_bucket_id", "updated")
COLUMN_FIELDS = ("id", "title", "position", "limit", "count", "updated")


def _ref(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).isdigit() or int(value) < 1:
        raise TrackerError("Board IDs must be positive integers")
    return str(int(value))


def _snapshot(view, columns):
    if not isinstance(view, dict) or not isinstance(columns, list):
        raise TrackerError("Board snapshot needs a view and columns")
    _ref(view.get("id"))
    result = []
    for column in columns:
        if not isinstance(column, dict) or not isinstance(column.get("title"), str):
            raise TrackerError("Board columns need IDs, titles and numeric positions")
        _ref(column.get("id"))
        position = column.get("position")
        if isinstance(position, bool) or not isinstance(position, (int, float)) or not math.isfinite(position):
            raise TrackerError("Board columns need finite numeric positions")
        result.append({key: column.get(key) for key in COLUMN_FIELDS})
    if len({row["id"] for row in result}) != len(result):
        raise TrackerError("Board snapshot repeats a column ID")
    return copy.deepcopy({"view": {key: view.get(key) for key in VIEW_FIELDS},
                          "columns": sorted(result, key=lambda row: (row["position"], int(row["id"])))})


def inspect(adapter, project):
    """Bucket metadata suffices for a proposal; task-bearing board reads are opt-in elsewhere."""
    project = _ref(project)
    resolved = adapter.refresh_lifecycle(project)
    snapshot = _snapshot(resolved["view"], resolved["buckets"]) if resolved["view"] else {"view": None, "columns": []}
    return {"project": project, **snapshot, "states": resolved["states"], "problems": resolved["problems"]}


def _mapping(value):
    if not isinstance(value, dict) or set(value) != set(STATES):
        raise TrackerError("Mapping must name all five lifecycle states; use a bucket ID or null to create")
    result = {state: _ref(value[state]) if value[state] is not None else None for state in STATES}
    ids = [ident for ident in result.values() if ident is not None]
    if len(set(ids)) != len(ids):
        raise TrackerError("One column cannot represent several lifecycle states")
    return result


def _changes(before, mapping):
    if not isinstance(before, dict) or set(before) != {"view", "columns"}:
        raise TrackerError("Plan needs the original board snapshot")
    snapshot = _snapshot(before["view"], before["columns"])
    if snapshot != before:
        raise TrackerError("Plan snapshot is malformed; regenerate the plan")
    if snapshot["view"]["bucket_configuration_mode"] != "manual":
        raise TrackerError("Board plans require one manually configured kanban view", "configuration")
    columns = {str(row["id"]): row for row in snapshot["columns"]}
    if any(ident is not None and ident not in columns for ident in mapping.values()):
        raise TrackerError("Mapping contains a bucket outside the selected view")
    desired = [(mapping[state], state, state) for state in STATES]
    # Extra columns retain their IDs, titles and relative order, after the lifecycle.
    desired += [(str(row["id"]), row["title"], None) for row in snapshot["columns"]
                if str(row["id"]) not in mapping.values()]
    titles = [title.casefold() for _, title, _ in desired]
    for state in STATES:
        if titles.count(state.casefold()) != 1:
            raise TrackerError(f"Plan would duplicate {state}; choose its existing column or resolve the conflict first")
    rows = []
    for index, (ident, title, state) in enumerate(desired, 1):
        original = columns.get(ident)
        rows.append({"id": ident, "state": state, "from": original["title"] if original else None,
                     "title": title, "position": index * 100,
                     "action": "create" if original is None else "rename" if title != original["title"] else "keep",
                     "reposition": original is not None and original["position"] != index * 100})
    return {"columns": rows,
            "default_bucket": {"from": snapshot["view"]["default_bucket_id"], "to": "Backlog"},
            "done_bucket": {"from": snapshot["view"]["done_bucket_id"], "to": "Done"}}


def build(adapter, project, mapping):
    observed = inspect(adapter, project)
    before = {key: observed[key] for key in ("view", "columns")}
    mapping = _mapping(mapping)
    return {"format_version": 1, "provider": "vikunja", "server": server_identity(adapter.server),
            "project": observed["project"], "before": before, "mapping": mapping,
            "changes": _changes(before, mapping)}


def _achieved(current, plan):
    """A completed plan can be reread safely, including its newly allocated IDs."""
    columns = {str(row["id"]): row for row in current["columns"]}
    original_ids = {str(row["id"]) for row in plan["before"]["columns"]}
    ids, states = set(), {}
    for row in plan["changes"]["columns"]:
        ident = row["id"]
        if ident is None:
            candidates = [key for key, col in columns.items()
                          if key not in original_ids and col["title"] == row["title"]]
            if len(candidates) != 1:
                return False
            ident = candidates[0]
        column = columns.get(ident)
        if not column or column["title"] != row["title"] or column["position"] != row["position"]:
            return False
        ids.add(ident)
        if row["state"]:
            states[row["state"]] = int(ident)
    view = current["view"]
    return (set(columns) == ids and view is not None and view["id"] == plan["before"]["view"]["id"]
            and view["bucket_configuration_mode"] == "manual"
            and view["default_bucket_id"] == states["Backlog"] and view["done_bucket_id"] == states["Done"])


def execute(adapter, project, plan, apply=False):
    if not isinstance(plan, dict) or type(plan.get("format_version", 1)) is not int or plan.get("format_version", 1) != 1:
        raise TrackerError("Unsupported board plan format_version; use a matching agent-box version")
    if set(plan) - {"format_version", "provider", "server", "project", "before", "mapping", "changes"}:
        raise TrackerError("Board plan contains unsupported operations")
    if (plan.get("provider") != "vikunja" or plan.get("server") != server_identity(adapter.server)
            or plan.get("project") != _ref(project)):
        raise TrackerError("Board plan belongs to another provider, server or project", "conflict")
    mapping = _mapping(plan.get("mapping"))
    changes = _changes(plan.get("before"), mapping)
    if changes != plan.get("changes"):
        raise TrackerError("Board plan changes differ from its mapping; regenerate and review the plan")
    observed = inspect(adapter, project)
    current = {key: observed[key] for key in ("view", "columns")}
    if _achieved(current, plan):
        return {"project": _ref(project), "applied": False, "status": "already-applied", "changes": changes}
    if current != plan["before"]:
        raise TrackerError("Board changed since this plan; inspect it and review a fresh plan before applying", "conflict")
    if not apply:
        return {"project": _ref(project), "applied": False, "status": "preview", "changes": changes}
    view = current["view"]
    ids = dict(mapping)
    originals = {str(row["id"]): row for row in current["columns"]}
    for row in changes["columns"]:
        if row["id"] is None:
            created = api.create_bucket(adapter.client, project, view, row["title"], row["position"])
            if not isinstance(created, dict) or not created.get("id"):
                raise TrackerError("Created column not confirmed; inspect the board before retrying", "unknown-outcome")
            ids[row["state"]] = _ref(created["id"])
        elif row["action"] == "rename" or row["reposition"]:
            api.update_bucket(adapter.client, project, view, originals[row["id"]], row["title"], row["position"])
    fields = {key: int(ids[state]) for key, state in (("default_bucket_id", "Backlog"), ("done_bucket_id", "Done"))
              if view.get(key) != int(ids[state])}
    if fields:
        adapter.client.patch(f"/projects/{int(project)}/views/{int(view['id'])}", fields)
    final = inspect(adapter, project)
    if not _achieved(final, plan):
        raise TrackerError("Board plan result not confirmed; inspect the board before retrying", "conflict")
    return {"project": _ref(project), "applied": True, "status": "applied", "changes": changes,
            "states": final["states"]}


def _read(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise TrackerError(f"Board JSON repeats key {key!r}")
            result[key] = value
        return result
    try:
        return json.loads(path.read_text(), object_pairs_hook=unique)
    except (OSError, ValueError) as exc:
        raise TrackerError(f"Cannot read board JSON: {exc}") from None


def sample_bucket(adapter, project, bucket, limit=3):
    """One explicitly requested page, never the unpaginated whole-board endpoint."""
    if type(limit) is not int or not 1 <= limit <= 10:
        raise TrackerError("Board sample limit must be between 1 and 10")
    observed = inspect(adapter, project)
    bucket = _ref(bucket)
    if not observed["view"] or bucket not in {str(row["id"]) for row in observed["columns"]}:
        raise TrackerError("Sample bucket is outside the selected board")
    query = urlencode({"filter": f"bucket_id = {bucket}", "page": 1, "per_page": limit, "format": "markdown"})
    rows = api._items(adapter.client.get(f"/projects/{int(project)}/views/{observed['view']['id']}/tasks?{query}"))
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise TrackerError("Invalid board sample response", "provider")
    return {"project": _ref(project), "view": observed["view"]["id"], "bucket": bucket,
            "tasks": [{key: row.get(key) for key in ("id", "title", "description", "done")}
                      for row in rows[:limit]]}


def run(adapter, projects, *, mapping=None, plan=None, sample=None, limit=3, apply=False):
    """Setup and staging use identical execution after their own scope resolution."""
    if sum(bool(value) for value in (mapping, plan, sample)) > 1 or apply and not plan:
        raise TrackerError("Use --mapping FILE to build a plan, then --plan FILE [--apply] after user confirmation")
    if mapping or plan or sample:
        if len(projects) != 1:
            raise TrackerError("Select one project for a board plan or sample")
        if sample:
            return sample_bucket(adapter, projects[0], sample, limit)
        return (build(adapter, projects[0], _read(mapping)) if mapping
                else execute(adapter, projects[0], _read(plan), apply))
    return [inspect(adapter, project) for project in projects]
