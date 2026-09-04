#!/usr/bin/env python3
"""Deterministic, dependency-free Vikunja API v2 CLI."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


API_PREFIX = "/api/v2"
DEFAULT_CONFIG_DIR = Path("~/.config/vikunja").expanduser()
DEFAULT_STATE_DIR = Path("~/.local/state/vikunja").expanduser()
PLAN_FIELDS = (
    "Title", "Description", "Priority", "Labels", "Project", "Done",
    "Plan ID", "Kind", "Depends On", "Source Tasks", "Order",
)
PLAN_OPTIONAL_FIELDS = (
    "Due Date", "Start Date", "End Date", "Percent Done", "Hex Color",
    "Assignees", "Bucket", "Parent",
)
PLAN_KNOWN_FIELDS = PLAN_FIELDS + PLAN_OPTIONAL_FIELDS
PLAN_REQUIRED_FIELDS = ("Title", "Description", "Project", "Plan ID")
PLAN_REQUIRED_VALUES = ("Title", "Description", "Project", "Plan ID")
UNMANAGED = ""
CLEARED = "-"
HEX_COLOR_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")
NULL_TIMESTAMP = "0001-01-01T00:00:00Z"
EXTERNAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
CURRENT_MARKER_RE = re.compile(r"(?mi)^Vikunja external ID:\s*([^\s]+)\s*$")
MARKER_RES = (
    re.compile(r"<!--\s*vikunja-external-id:\s*([^\s]+)\s*-->"),
    re.compile(r"\\?\[vikunja-external-id:\s*([^\]\\\s]+)\\?\]"),
    CURRENT_MARKER_RE,
)
RELATION_KINDS = {
    "subtask", "parenttask", "related", "duplicateof", "duplicates",
    "blocking", "blocked", "precedes", "follows", "copiedfrom", "copiedto",
}
ACTIVE_BUCKET_TITLES = {"doing", "in progress", "in-progress", "в работе"}


class VikunjaError(Exception):
    """Base error with a concise user-facing message."""


class ConfigError(VikunjaError):
    pass


class APIError(VikunjaError):
    def __init__(self, status, detail, code=None):
        self.status = status
        self.code = code
        suffix = f" (code {code})" if code is not None else ""
        super().__init__(f"Vikunja API HTTP {status}: {detail}{suffix}")


class AuthenticationError(APIError):
    pass


class NotFound(VikunjaError):
    pass


class AmbiguousMatch(VikunjaError):
    pass


class PlanValidationError(VikunjaError):
    pass


class ImportConflict(VikunjaError):
    pass


def read_text_option(inline, path, option):
    """Text from --X or --X-file (- reads stdin); at most one of the two."""
    if inline is not None and path is not None:
        raise VikunjaError(f"pass either --{option} or --{option}-file, not both")
    if path is None:
        return inline
    if str(path) == "-":
        return sys.stdin.read()
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise VikunjaError(f"cannot read {path}: {exc}") from None


def append_to_description(current, addition, external_id=None):
    """Append prose while keeping the external-ID footer last."""
    marker = external_id or external_id_from_description(current)
    body = current or ""
    if marker:
        for pattern in MARKER_RES:
            body = pattern.sub("", body)
    body = body.rstrip() + "\n\n" + addition.strip()
    return with_external_marker(body, marker) if marker else body.strip()


def _boolean_option(value, option):
    if value in ("true", "false"):
        return value == "true"
    raise VikunjaError(f"--{option} must be true or false")


def task_field_changes(args):
    """Every writable scalar the API exposes, from one update invocation."""
    changes = {}
    for option, key in (("title", "title"), ("hex_color", "hex_color")):
        value = getattr(args, option, None)
        if value is not None:
            changes[key] = value
    description = read_text_option(args.description, args.description_file, "description")
    if description is not None:
        changes["description"] = description
    if getattr(args, "priority", None) is not None:
        if args.priority not in range(0, 6):
            raise VikunjaError("--priority must be between 0 and 5")
        changes["priority"] = args.priority
    if getattr(args, "percent_done", None) is not None:
        if not 0.0 <= args.percent_done <= 1.0:
            raise VikunjaError("--percent-done must be between 0 and 1 (0.5 means 50%)")
        changes["percent_done"] = args.percent_done
    for option, key in (("done", "done"), ("is_favorite", "is_favorite")):
        value = getattr(args, option, None)
        if value is not None:
            changes[key] = _boolean_option(value, option.replace("_", "-"))
    for option, key in (("due_date", "due_date"), ("start_date", "start_date"),
                        ("end_date", "end_date")):
        value = getattr(args, option, None)
        if value is not None:
            changes[key] = NULL_TIMESTAMP if value in ("", CLEARED) else value
    return changes


def redact_secret(value):
    value = (value or "").strip()
    if len(value) < 9:
        return "***"
    prefix = "tk_" if value.startswith("tk_") else value[:2]
    return f"{prefix}...{value[-4:]}"


class Config:
    def __init__(self, url, token, config_dir=DEFAULT_CONFIG_DIR):
        self.url = url.rstrip("/")
        self.token = token.strip()
        self.config_dir = Path(config_dir)

    @classmethod
    def load(cls, env=None, config_dir=None):
        env = os.environ if env is None else env
        config_dir = Path(config_dir or DEFAULT_CONFIG_DIR).expanduser()
        file_config = {}
        config_path = config_dir / "config.json"
        if config_path.exists():
            try:
                file_config = json.loads(config_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ConfigError(f"Cannot read {config_path}: {exc}") from exc
        url = (env.get("VIKUNJA_URL") or file_config.get("url") or "").strip()
        token = (env.get("VIKUNJA_TOKEN") or "").strip()
        token_path = config_dir / "token"
        if not token and token_path.exists():
            try:
                token = token_path.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise ConfigError(f"Cannot read {token_path}: {exc}") from exc
        if not url:
            raise ConfigError(f"Set VIKUNJA_URL or add url to {config_path}")
        if not token:
            raise ConfigError(f"Set VIKUNJA_TOKEN or create {token_path} with mode 0600")
        return cls(url, token, config_dir)


def stable_json(value, pretty=False):
    if pretty:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


class VikunjaClient:
    def __init__(self, url, token, per_page=1000, timeout=30, opener=None):
        self.url = url.rstrip("/")
        self.token = token
        self.per_page = per_page
        self.timeout = timeout
        self.opener = opener or urllib.request.urlopen

    def request(self, method, path, body=None, headers=None):
        if not path.startswith("/"):
            path = "/" + path
        url = self.url + API_PREFIX + path
        request_headers = {
            "Accept": "application/json",
            "Authorization": "Bearer " + self.token,
            "User-Agent": "vikunja-skill/1",
        }
        request_headers.update(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(url, data=data, method=method, headers=request_headers)
        try:
            with self.opener(request, timeout=self.timeout) as response:
                payload = response.read()
                return None if not payload else json.loads(payload.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read().decode("utf-8", "replace")
            finally:
                exc.close()
            try:
                problem = json.loads(raw)
            except json.JSONDecodeError:
                problem = {}
            detail = problem.get("detail") or problem.get("message") or problem.get("title") or exc.reason
            error_type = AuthenticationError if exc.code in (401, 403) else APIError
            raise error_type(exc.code, detail, problem.get("code")) from None
        except urllib.error.URLError as exc:
            raise APIError(0, f"cannot connect to {self.url}: {exc.reason}") from None

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, body):
        return self.request("POST", path, body)

    def put(self, path, body):
        return self.request("PUT", path, body)

    def patch(self, path, body, markdown=False):
        headers = {"Content-Type": "application/merge-patch+json"}
        if markdown:
            headers["X-Vikunja-Format"] = "markdown"
        return self.request("PATCH", path, body, headers)

    def delete(self, path):
        return self.request("DELETE", path)

    def paginate(self, path, params=None):
        params = dict(params or {})
        params["per_page"] = self.per_page
        page = 1
        items = []
        expected_total = None
        while True:
            params["page"] = page
            separator = "&" if "?" in path else "?"
            encoded = urllib.parse.urlencode(params, doseq=True)
            response = self.get(path + separator + encoded)
            if not isinstance(response, dict) or not isinstance(response.get("items"), list):
                raise APIError(200, "paginated response did not contain an items array")
            items.extend(response["items"])
            expected_total = response.get("total", expected_total)
            total_pages = response.get("total_pages")
            if total_pages is not None:
                if page >= total_pages:
                    break
            elif len(response["items"]) < self.per_page:
                break
            page += 1
        if expected_total is not None and len(items) < expected_total:
            raise APIError(200, f"pagination truncated: received {len(items)} of {expected_total} items")
        return items


def _normalize_path(value):
    parts = tuple(part.strip() for part in value.split("/") if part.strip())
    if not parts:
        raise ValueError("empty project path")
    return parts


class ProjectIndex:
    def __init__(self, projects):
        self.projects = list(projects)
        self.by_id = {int(p["id"]): p for p in self.projects}

    def path_for(self, project):
        parts = []
        seen = set()
        current = project
        while current:
            current_id = int(current["id"])
            if current_id in seen:
                raise ImportConflict(f"project hierarchy cycle at id {current_id}")
            seen.add(current_id)
            parts.append(current["title"])
            parent_id = int(current.get("parent_project_id") or 0)
            current = self.by_id.get(parent_id)
        return " / ".join(reversed(parts))

    def resolve(self, reference):
        text = str(reference).strip()
        if re.fullmatch(r"-?\d+", text):
            found = self.by_id.get(int(text))
            if not found:
                raise NotFound(f"project id {text} was not found")
            return found
        if "/" in text:
            matches = [p for p in self.projects if self.path_for(p) == " / ".join(_normalize_path(text))]
        else:
            matches = [p for p in self.projects if p.get("title") == text]
        if not matches:
            raise NotFound(f"project {text!r} was not found")
        if len(matches) > 1:
            paths = ", ".join(f"{self.path_for(p)} (id {p['id']})" for p in matches)
            raise AmbiguousMatch(f"project {text!r} is ambiguous: {paths}")
        return matches[0]

    def by_exact_path(self, path):
        canonical = " / ".join(_normalize_path(path))
        matches = [p for p in self.projects if self.path_for(p) == canonical]
        if len(matches) > 1:
            raise AmbiguousMatch(f"project path {canonical!r} matched multiple projects")
        return matches[0] if matches else None


class TaskIndex:
    def __init__(self, tasks):
        self.tasks = list(tasks)
        self.by_id = {int(task["id"]): task for task in self.tasks}

    def resolve(self, reference):
        text = str(reference).strip()
        if text.isdigit():
            found = self.by_id.get(int(text))
            if not found:
                raise NotFound(f"task id {text} was not found")
            return found
        external = [t for t in self.tasks if external_id_from_description(t.get("description", "")) == text]
        if len(external) == 1:
            return external[0]
        if len(external) > 1:
            raise AmbiguousMatch(f"external task id {text!r} is duplicated")
        matches = [task for task in self.tasks if task.get("title") == text]
        if not matches:
            raise NotFound(f"task {text!r} was not found")
        if len(matches) > 1:
            ids = ", ".join(str(task["id"]) for task in matches)
            raise AmbiguousMatch(f"task title {text!r} is ambiguous; matching ids: {ids}")
        return matches[0]


def external_id_from_description(description):
    for pattern in MARKER_RES:
        match = pattern.search(description or "")
        if match:
            return match.group(1)
    return None


def with_external_marker(description, external_id):
    description = description or ""
    for pattern in MARKER_RES:
        description = pattern.sub("", description)
    lines = description.rstrip().splitlines()
    prepared = []
    in_fence = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
        next_nonblank = index + 1 < len(lines) and bool(lines[index + 1].strip())
        if stripped and next_nonblank and not in_fence and not line.endswith("  "):
            line += "  "
        prepared.append(line)
    body = "\n".join(prepared).rstrip()
    return f"{body}\n\nVikunja external ID: {external_id}"


def _normalized_description_text(description):
    text = description or ""
    for pattern in MARKER_RES:
        text = pattern.sub("", text)
    text = re.sub(r"\\([\\`*_[\]{}()#+\-.!])", r"\1", text)
    text = text.replace("  \n", "\n").replace("\\\n", "\n")
    return re.sub(r"\s+", " ", text).strip()


def description_matches(actual, source, external_id):
    marker = CURRENT_MARKER_RE.search(actual or "")
    return (
        marker is not None
        and marker.group(1) == external_id
        and _normalized_description_text(actual) == _normalized_description_text(source)
    )


def _cell(row, field):
    return (row.get(field) or "").strip()


def _plan_timestamp(external_id, field, value):
    """Optional date cell: blank leaves the field unmanaged, "-" clears it."""
    if value == UNMANAGED:
        return None
    if value == CLEARED:
        return NULL_TIMESTAMP
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?(Z|[+-]\d{2}:\d{2})?)?", value):
        raise PlanValidationError(
            f"{external_id}: {field} must be ISO-8601 (2026-09-02 or 2026-09-02T15:04:05Z), "
            f'blank to leave unmanaged, or "-" to clear'
        )
    if len(value) == 10:
        value += "T00:00:00Z"
    return value.replace(" ", "T")


class PlanTask:
    def __init__(self, title, description, priority, labels, project_path, done,
                 external_id, kind, depends_on, source_tasks, order,
                 fields=None, assignees=None, bucket=UNMANAGED, parent=None):
        self.title = title
        self.description = description
        self.priority = priority
        self.labels = tuple(labels)
        self.project_path = project_path
        self.done = done
        self.external_id = external_id
        self.kind = kind
        self.depends_on = tuple(depends_on)
        self.source_tasks = source_tasks
        self.order = order
        self.fields = dict(fields or {})
        self.assignees = None if assignees is None else tuple(assignees)
        self.bucket = bucket
        self.parent = parent

    @classmethod
    def from_row(cls, row, default_order=1):
        external_id = _cell(row, "Plan ID")
        if not EXTERNAL_ID_RE.fullmatch(external_id):
            raise PlanValidationError(f"invalid external ID {external_id!r}")
        priority_text = _cell(row, "Priority") or "0"
        try:
            priority = int(priority_text)
        except ValueError:
            raise PlanValidationError(f"{external_id}: priority must be an integer") from None
        if priority not in range(0, 6):
            raise PlanValidationError(f"{external_id}: priority must be between 0 and 5")
        done_text = (_cell(row, "Done") or "false").lower()
        if done_text not in ("true", "false"):
            raise PlanValidationError(f"{external_id}: Done must be true or false")
        order_text = _cell(row, "Order")
        if order_text:
            try:
                order = int(order_text)
            except ValueError:
                raise PlanValidationError(f"{external_id}: Order must be an integer") from None
        else:
            order = default_order
        try:
            project_path = " / ".join(_normalize_path(row["Project"]))
        except ValueError:
            raise PlanValidationError(f"{external_id}: Project must not be empty") from None
        labels = tuple(dict.fromkeys(x.strip() for x in _cell(row, "Labels").split(",") if x.strip()))
        depends = tuple(dict.fromkeys(x.strip() for x in _cell(row, "Depends On").split(",") if x.strip()))

        fields = {}
        for column, key in (("Due Date", "due_date"), ("Start Date", "start_date"),
                            ("End Date", "end_date")):
            stamp = _plan_timestamp(external_id, column, _cell(row, column))
            if stamp is not None:
                fields[key] = stamp
        percent = _cell(row, "Percent Done")
        if percent:
            try:
                value = float(percent)
            except ValueError:
                raise PlanValidationError(f"{external_id}: Percent Done must be a number") from None
            if not 0.0 <= value <= 1.0:
                raise PlanValidationError(
                    f"{external_id}: Percent Done must be between 0 and 1 (0.5 means 50%)"
                )
            fields["percent_done"] = value
        color = _cell(row, "Hex Color")
        if color:
            if color == CLEARED:
                fields["hex_color"] = ""
            elif HEX_COLOR_RE.fullmatch(color):
                fields["hex_color"] = color.lstrip("#").lower()
            else:
                raise PlanValidationError(f"{external_id}: Hex Color must be six hex digits")

        assignee_cell = _cell(row, "Assignees")
        if assignee_cell == CLEARED:
            assignees = ()
        elif assignee_cell:
            assignees = tuple(dict.fromkeys(x.strip() for x in assignee_cell.split(",") if x.strip()))
        else:
            assignees = None
        parent = _cell(row, "Parent") or None
        if parent and not EXTERNAL_ID_RE.fullmatch(parent):
            raise PlanValidationError(f"{external_id}: invalid Parent reference {parent!r}")
        if parent == external_id:
            raise PlanValidationError(f"{external_id} is its own parent")

        return cls(
            _cell(row, "Title"), row["Description"].rstrip(), priority, labels,
            project_path, done_text == "true", external_id, _cell(row, "Kind"),
            depends, _cell(row, "Source Tasks"), order,
            fields=fields, assignees=assignees, bucket=_cell(row, "Bucket"), parent=parent,
        )

    def manages_assignees(self):
        return self.assignees is not None

    def desired_description(self):
        return with_external_marker(self.description, self.external_id)

    def as_dict(self):
        value = {
            "depends_on": list(self.depends_on), "description": self.description,
            "done": self.done, "external_id": self.external_id, "kind": self.kind,
            "labels": list(self.labels), "order": self.order, "priority": self.priority,
            "project": self.project_path, "source_tasks": self.source_tasks,
            "title": self.title,
        }
        if self.fields:
            value["fields"] = dict(self.fields)
        if self.assignees is not None:
            value["assignees"] = list(self.assignees)
        if self.bucket:
            value["bucket"] = self.bucket
        if self.parent:
            value["parent"] = self.parent
        return value


class Plan:
    def __init__(self, tasks, source, digest):
        self.tasks = tuple(tasks)
        self.source = Path(source)
        self.digest = digest

    def relation_edges(self):
        """Edges as (base task, relation kind, other task).

        `A Depends On B` is posted on B as `blocking` A. `A Parent P` is posted
        on A as `parenttask` P, and Vikunja records the inverse `subtask` on P.
        """
        edges = []
        for task in self.tasks:
            for dependency in task.depends_on:
                edges.append((dependency, "blocking", task.external_id))
            if task.parent:
                edges.append((task.external_id, "parenttask", task.parent))
        return tuple(edges)

    def external_references(self):
        """Referenced plan IDs that no row defines; they must already exist."""
        known = {task.external_id for task in self.tasks}
        referenced = set()
        for task in self.tasks:
            referenced.update(task.depends_on)
            if task.parent:
                referenced.add(task.parent)
        return tuple(sorted(referenced - known))

    def as_dict(self):
        return {
            "digest": self.digest,
            "external_references": list(self.external_references()),
            "projects": sorted({task.project_path for task in self.tasks}),
            "relation_edges": [list(edge) for edge in self.relation_edges()],
            "source": str(self.source),
            "tasks": [task.as_dict() for task in sorted(self.tasks, key=lambda item: item.order)],
        }


def _find_cycle(tasks_by_id):
    visiting = set()
    visited = set()
    stack = []

    def visit(node):
        if node in visiting:
            start = stack.index(node)
            return stack[start:] + [node]
        if node in visited:
            return None
        visiting.add(node)
        stack.append(node)
        task = tasks_by_id[node]
        successors = list(task.depends_on)
        if task.parent:
            successors.append(task.parent)
        for dependency in successors:
            if dependency not in tasks_by_id:
                continue
            cycle = visit(dependency)
            if cycle:
                return cycle
        stack.pop()
        visiting.remove(node)
        visited.add(node)
        return None

    for node in tasks_by_id:
        cycle = visit(node)
        if cycle:
            return cycle
    return None


JSON_PLAN_KEYS = {
    "id": "Plan ID", "title": "Title", "description": "Description",
    "priority": "Priority", "labels": "Labels", "project": "Project",
    "done": "Done", "kind": "Kind", "depends_on": "Depends On",
    "source_tasks": "Source Tasks", "order": "Order", "due_date": "Due Date",
    "start_date": "Start Date", "end_date": "End Date",
    "percent_done": "Percent Done", "hex_color": "Hex Color",
    "assignees": "Assignees", "bucket": "Bucket", "parent": "Parent",
}


def _json_cell(key, value):
    """Render one JSON plan value into the shared row grammar."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        if key in ("assignees",) and not value:
            return CLEARED
        for item in value:
            if not isinstance(item, str):
                raise PlanValidationError(f"{key} must be a list of strings")
        return ",".join(item.strip() for item in value)
    if isinstance(value, (int, float)):
        return repr(value) if isinstance(value, float) else str(value)
    if isinstance(value, str):
        return value
    raise PlanValidationError(f"{key} has unsupported type {type(value).__name__}")


def _rows_from_json(document):
    if isinstance(document, dict):
        entries = document.get("tasks")
        unknown_top = sorted(set(document) - {"tasks"})
        if unknown_top:
            raise PlanValidationError(f"unknown top-level keys: {', '.join(unknown_top)}")
    else:
        entries = document
    if not isinstance(entries, list):
        raise PlanValidationError('JSON plan must be a list of tasks or {"tasks": [...]}')
    rows = []
    for position, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise PlanValidationError(f"JSON task {position} must be an object")
        unknown = sorted(set(entry) - set(JSON_PLAN_KEYS))
        if unknown:
            raise PlanValidationError(
                f"JSON task {position} has unknown keys: {', '.join(unknown)}; "
                f"known keys are {', '.join(sorted(JSON_PLAN_KEYS))}"
            )
        row = {column: "" for column in PLAN_KNOWN_FIELDS}
        for key, value in entry.items():
            row[JSON_PLAN_KEYS[key]] = _json_cell(key, value)
        rows.append(row)
    return rows


def parse_plan(path):
    path = Path(path)
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if path.suffix.lower() == ".json":
        try:
            document = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PlanValidationError(f"{path}: invalid JSON plan: {exc}") from None
        return _finish_plan(_rows_from_json(document), path, digest)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise PlanValidationError(f"{path}: CSV must be UTF-8: {exc}") from None
    # Passing splitlines() discards the physical newlines inside quoted CSV
    # fields, causing csv.reader to concatenate every line of a description.
    reader = csv.DictReader(io.StringIO(text, newline=""))
    columns = list(reader.fieldnames or [])
    unknown = [name for name in columns if name not in PLAN_KNOWN_FIELDS]
    if unknown:
        raise PlanValidationError(
            f"unknown CSV columns: {', '.join(unknown)}; "
            f"known columns are {', '.join(PLAN_KNOWN_FIELDS)}"
        )
    duplicates = sorted({name for name in columns if columns.count(name) > 1})
    if duplicates:
        raise PlanValidationError(f"duplicate CSV columns: {', '.join(duplicates)}")
    missing_columns = [name for name in PLAN_REQUIRED_FIELDS if name not in columns]
    if missing_columns:
        raise PlanValidationError(f"CSV is missing required columns: {', '.join(missing_columns)}")
    rows = []
    for number, csv_row in enumerate(reader, start=2):
        if None in csv_row:
            raise PlanValidationError(f"CSV row {number} has extra columns")
        rows.append(csv_row)
    return _finish_plan(rows, path, digest)


def _finish_plan(rows, path, digest):
    tasks = []
    for number, row in enumerate(rows, start=1):
        blank = [field for field in PLAN_REQUIRED_VALUES
                 if not (row.get(field) or "").strip()]
        if blank:
            raise PlanValidationError(f"plan entry {number} has blank fields: {', '.join(blank)}")
        tasks.append(PlanTask.from_row(row, default_order=number))
    if not tasks:
        raise PlanValidationError("plan contains no tasks")
    by_id = {}
    for task in tasks:
        if task.external_id in by_id:
            raise PlanValidationError(f"duplicate external ID {task.external_id}")
        by_id[task.external_id] = task
    seen_edges = set()
    for task in tasks:
        for dependency in task.depends_on:
            if dependency == task.external_id:
                raise PlanValidationError(f"{task.external_id} depends on itself")
            edge = (dependency, task.external_id)
            if edge in seen_edges:
                raise PlanValidationError(f"duplicate dependency edge {dependency} -> {task.external_id}")
            seen_edges.add(edge)
    cycle = _find_cycle(by_id)
    if cycle:
        raise PlanValidationError("dependency cycle: " + " -> ".join(cycle))
    tasks.sort(key=lambda item: (item.order, item.external_id))
    return Plan(tasks, path.resolve(), digest)


class Inventory:
    def __init__(self, projects, tasks, labels):
        self.projects = list(projects)
        self.tasks = list(tasks)
        self.labels = list(labels)


class Action:
    def __init__(self, kind, entity, key, details=None):
        self.kind = kind
        self.entity = entity
        self.key = str(key)
        self.details = details or {}

    def as_dict(self):
        return {"details": self.details, "entity": self.entity, "key": self.key, "kind": self.kind}


def _tasks_by_external(inventory, state_task_ids=None):
    tasks_by_external = {}
    for task in inventory.tasks:
        external_id = external_id_from_description(task.get("description", ""))
        if external_id:
            if external_id in tasks_by_external:
                raise ImportConflict(f"external ID {external_id!r} appears on multiple Vikunja tasks")
            tasks_by_external[external_id] = task
    tasks_by_id = {int(task["id"]): task for task in inventory.tasks}
    for external_id, task_id in (state_task_ids or {}).items():
        task = tasks_by_id.get(int(task_id))
        if task is None:
            raise ImportConflict(
                f"import state maps {external_id!r} to missing Vikunja task id {task_id}"
            )
        marked_id = external_id_from_description(task.get("description", ""))
        if marked_id and marked_id != external_id:
            raise ImportConflict(
                f"import state maps {external_id!r} to task {task_id}, which is marked {marked_id!r}"
            )
        existing = tasks_by_external.get(external_id)
        if existing is not None and int(existing["id"]) != int(task_id):
            raise ImportConflict(
                f"external ID {external_id!r} conflicts between import state and task marker"
            )
        tasks_by_external[external_id] = task
    return tasks_by_external


def build_import_actions(plan, inventory, require_root=True, state_task_ids=None):
    actions = []
    project_index = ProjectIndex(inventory.projects)
    desired_paths = sorted({task.project_path for task in plan.tasks}, key=lambda p: (p.count("/"), p))
    roots = sorted({_normalize_path(path)[0] for path in desired_paths})
    if len(roots) != 1:
        raise ImportConflict(f"plan must have exactly one root project, got: {', '.join(roots)}")
    root = roots[0]
    root_matches = [p for p in inventory.projects if p["title"] == root and not int(p.get("parent_project_id") or 0)]
    if require_root and not root_matches:
        raise ImportConflict(f"required existing root project {root!r} was not found")
    if len(root_matches) > 1:
        raise ImportConflict(f"root project {root!r} is ambiguous: {[p['id'] for p in root_matches]}")

    known_paths = {project_index.path_for(p): p for p in inventory.projects}
    for path in desired_paths:
        parts = _normalize_path(path)
        for depth in range(1, len(parts) + 1):
            partial = " / ".join(parts[:depth])
            if partial in known_paths:
                if not any(a.entity == "project" and a.key == partial for a in actions):
                    actions.append(Action("reuse", "project", partial, {"id": known_paths[partial]["id"]}))
            elif depth == 1 and require_root:
                raise ImportConflict(f"required existing root project {partial!r} was not found")
            elif not any(a.entity == "project" and a.key == partial for a in actions):
                actions.append(Action("create", "project", partial, {"parent_path": " / ".join(parts[:depth - 1])}))

    tasks_by_external = _tasks_by_external(inventory, state_task_ids)
    project_tasks = {}
    for task in inventory.tasks:
        project_tasks.setdefault(int(task.get("project_id") or 0), []).append(task)

    unresolved = [ref for ref in plan.external_references() if ref not in tasks_by_external]
    if unresolved:
        raise ImportConflict(
            "referenced plan IDs are neither in this plan nor on an existing task: "
            + ", ".join(unresolved)
        )

    for task in sorted(plan.tasks, key=lambda item: item.order):
        existing = tasks_by_external.get(task.external_id)
        desired_project = known_paths.get(task.project_path)
        if existing:
            changes = {}
            expected = {
                "title": task.title, "description": task.desired_description(),
                "priority": task.priority, "done": task.done,
            }
            expected.update(task.fields)
            if desired_project:
                expected["project_id"] = desired_project["id"]
            for key, value in expected.items():
                if key == "description":
                    differs = not description_matches(
                        existing.get("description", ""), task.description, task.external_id
                    )
                elif key in ("due_date", "start_date", "end_date"):
                    differs = _timestamp_text(existing.get(key)) != _timestamp_text(value)
                elif key == "percent_done":
                    differs = float(existing.get(key) or 0.0) != float(value)
                elif key == "hex_color":
                    differs = (existing.get(key) or "").lstrip("#").lower() != value
                else:
                    differs = existing.get(key) != value
                if differs:
                    changes[key] = value
            kind = "update" if changes else "reuse"
            actions.append(Action(kind, "task", task.external_id,
                                  {"id": existing["id"], "project_path": task.project_path, "changes": changes}))
        else:
            if desired_project:
                collisions = [candidate for candidate in project_tasks.get(int(desired_project["id"]), [])
                              if candidate.get("title") == task.title]
                if collisions:
                    raise ImportConflict(
                        f"task title {task.title!r} already exists in {task.project_path} without "
                        f"external ID {task.external_id}; refusing title-based reuse"
                    )
            details = {
                "project_path": task.project_path, "title": task.title,
                "description": task.desired_description(), "priority": task.priority,
                "done": task.done, "order": task.order,
            }
            details.update(task.fields)
            actions.append(Action("create", "task", task.external_id, details))

    for task in sorted(plan.tasks, key=lambda item: item.order):
        existing = tasks_by_external.get(task.external_id)
        if task.manages_assignees():
            current = sorted(
                (u.get("username") or "") for u in ((existing or {}).get("assignees") or [])
            )
            desired = sorted(task.assignees)
            kind = "reuse" if existing and current == desired else "set"
            actions.append(Action(kind, "assignee", task.external_id,
                                  {"usernames": desired, "current": current}))
        if task.bucket:
            current = task_bucket_titles(existing or {})
            kind = "reuse" if existing and task.bucket in current else "set"
            actions.append(Action(kind, "bucket", task.external_id,
                                  {"title": task.bucket, "current": current}))

    labels_by_title = {label["title"]: label for label in inventory.labels}
    for title in sorted({label for task in plan.tasks for label in task.labels}):
        if title in labels_by_title:
            actions.append(Action("reuse", "label", title, {"id": labels_by_title[title]["id"]}))
        else:
            actions.append(Action("create", "label", title))
    relation_keys = existing_relation_keys(inventory.tasks)
    for base, relation_kind, other in plan.relation_edges():
        source = tasks_by_external.get(base)
        target = tasks_by_external.get(other)
        kind = "reuse" if source and target and (
            int(source["id"]), relation_kind, int(target["id"])
        ) in relation_keys else "create"
        actions.append(Action(kind, "relation", f"{base}:{relation_kind}:{other}", {
            "from_external_id": base, "relation_kind": relation_kind,
            "to_external_id": other,
        }))
    return actions


def _timestamp_text(value):
    text = str(value or "").strip()
    return "" if not text or text.startswith("0001-01-01") else text


MANAGED_RELATION_KINDS = ("blocked", "parenttask")


def find_stale_bindings(plan, inventory, state_task_ids=None):
    """Report relations and labels present in Vikunja but absent from the plan.

    Never applied. The importer only adds; this makes what it cannot remove
    visible instead of leaving a silently stale edge behind.
    """
    tasks_by_external = _tasks_by_external(inventory, state_task_ids)
    id_to_external = {int(t["id"]): e for e, t in tasks_by_external.items()}
    desired = set()
    for base, kind, other in plan.relation_edges():
        desired.add((base, kind, other))
        inverse = {"blocking": "blocked", "blocked": "blocking",
                   "parenttask": "subtask", "subtask": "parenttask"}.get(kind)
        if inverse:
            desired.add((other, inverse, base))
    stale_relations = []
    stale_labels = []
    for task in plan.tasks:
        existing = tasks_by_external.get(task.external_id)
        if not existing:
            continue
        related = existing.get("related_tasks") or {}
        for kind in MANAGED_RELATION_KINDS:
            for other in related.get(kind) or []:
                other_external = id_to_external.get(int(other["id"]))
                if other_external is None:
                    continue
                if (task.external_id, kind, other_external) not in desired:
                    stale_relations.append({
                        "kind": kind, "other": other_external, "task": task.external_id,
                    })
        attached = {label["title"] for label in (existing.get("labels") or [])}
        for title in sorted(attached - set(task.labels)):
            stale_labels.append({"label": title, "task": task.external_id})
    return {
        "labels": sorted(stale_labels, key=lambda item: (item["task"], item["label"])),
        "relations": sorted(
            stale_relations, key=lambda item: (item["task"], item["kind"], item["other"])
        ),
    }


class RecordingWriter:
    def __init__(self):
        self.calls = []

    def execute(self, action):
        self.calls.append(action)


def attach_label(client, task_id, label_id):
    return client.post(f"/tasks/{int(task_id)}/labels", {"label_id": int(label_id)})


def _items(response):
    """List endpoints answer either a bare array or a paging envelope around one."""
    if isinstance(response, dict):
        items = response.get("items")
        return items if isinstance(items, list) else response
    return response


def _view_label(view):
    return f"{view.get('title') or 'kanban'} (id {view['id']})"


def resolve_kanban_view(client, project_id, reference=None):
    """The kanban view whose buckets are the project's board.

    A project may carry several kanban views, so picking the first one silently
    would move cards on an arbitrary board. Ambiguity is an error here, exactly
    as it is for project and task titles.
    """
    views = [v for v in _items(client.get(f"/projects/{int(project_id)}/views"))
             if v.get("view_kind") == "kanban"]
    if not views:
        raise NotFound(f"project id {project_id} has no kanban view")
    if reference is not None:
        text = str(reference).strip()
        matches = [v for v in views if str(v["id"]) == text or v.get("title") == text]
        if not matches:
            available = ", ".join(_view_label(v) for v in views)
            raise NotFound(f"kanban view {text!r} was not found; available: {available}")
        if len(matches) > 1:
            raise AmbiguousMatch(f"kanban view {text!r} is ambiguous")
        return matches[0]
    if len(views) > 1:
        available = ", ".join(_view_label(v) for v in views)
        raise AmbiguousMatch(
            f"project id {project_id} has several kanban views ({available}); pass --view"
        )
    return views[0]


def view_buckets(client, project_id, view_id):
    return _items(client.get(f"/projects/{int(project_id)}/views/{int(view_id)}/buckets"))


def board_columns(client, project_id, view_id):
    """Buckets with their tasks in one request, rather than a task query per column."""
    return _items(client.get(
        f"/projects/{int(project_id)}/views/{int(view_id)}/buckets/tasks?format=markdown"
    ))


def resolve_bucket(buckets, reference, missing=NotFound):
    text = str(reference).strip()
    matches = [b for b in buckets if str(b.get("id")) == text]
    if not matches:
        matches = [b for b in buckets if b.get("title") == text]
    if not matches:
        available = ", ".join(sorted(str(b.get("title")) for b in buckets))
        raise missing(f"bucket {text!r} not found; available: {available}")
    if len(matches) > 1:
        raise AmbiguousMatch(f"bucket {text!r} is ambiguous")
    return matches[0]


def place_in_bucket(client, project_id, view, bucket, task_id):
    """PUT the task into the bucket. Idempotent server-side; callers skip no-op moves."""
    if view.get("bucket_configuration_mode") == "filter":
        raise VikunjaError(
            f"kanban view {_view_label(view)} builds its buckets from filters; "
            f"a card moves there by changing the task fields the filters match, not by hand"
        )
    return client.put(
        f"/projects/{int(project_id)}/views/{int(view['id'])}"
        f"/buckets/{int(bucket['id'])}/tasks",
        {"task_id": int(task_id)},
    )


def task_bucket_in_view(task, view_id):
    """The task's bucket in one specific view, or None.

    A task carries one bucket entry per kanban view, so comparing titles across
    all of them would call a move a no-op when the card sits in a same-named
    column of a different board.
    """
    for bucket in (task.get("buckets") or []):
        if int(bucket.get("project_view_id") or 0) == int(view_id):
            return bucket
    return None


def task_bucket_titles(task):
    """The columns a task sits in, one per kanban view.

    `bucket_id` on a task is always 0 on API v2: the placement lives in the
    expanded `buckets` array instead, one entry per kanban view of the project.
    """
    return sorted({str(b.get("title")) for b in (task.get("buckets") or []) if b.get("title")})


def get_task_complete(client, task_id):
    query = urllib.parse.urlencode([
        ("format", "markdown"), ("expand", "subtasks"), ("expand", "buckets"),
        ("expand", "reactions"), ("expand", "comment_count"),
        ("expand", "time_entries_count"), ("expand", "is_unread"),
    ])
    task = client.get(f"/tasks/{int(task_id)}?{query}")
    task["comments"] = client.paginate(
        f"/tasks/{int(task_id)}/comments", {"format": "markdown"}
    )
    return task


BULK_WRITABLE_TASK_FIELDS = {
    "title", "description", "done", "priority", "project_id", "due_date",
    "start_date", "end_date", "percent_done", "hex_color", "is_favorite",
}


def plan_bulk_updates(specification, tasks):
    if not isinstance(specification, list) or not specification:
        raise ImportConflict("bulk update file must contain a non-empty JSON array")
    index = TaskIndex(tasks)
    actions = []
    seen = set()
    for position, item in enumerate(specification, start=1):
        if not isinstance(item, dict) or set(item) != {"task", "changes"}:
            raise ImportConflict(f"bulk item {position} must contain only task and changes")
        task = index.resolve(item["task"])
        task_id = int(task["id"])
        if task_id in seen:
            raise ImportConflict(f"duplicate bulk target task id {task_id}")
        seen.add(task_id)
        changes = item["changes"]
        if not isinstance(changes, dict) or not changes:
            raise ImportConflict(f"bulk item {position} changes must be a non-empty object")
        forbidden = sorted(set(changes) - BULK_WRITABLE_TASK_FIELDS)
        if forbidden:
            raise ImportConflict(f"bulk fields are not writable: {', '.join(forbidden)}")
        actions.append(Action("update", "task", task_id, {"changes": changes, "title": task.get("title", "")}))
    return actions


def execute_actions(actions, writer, apply=False):
    actions = list(actions)
    if apply:
        for action in actions:
            writer.execute(action)
    return {
        "applied": len(actions) if apply else 0,
        "dry_run": not apply,
        "planned": len(actions),
    }


class ImportState:
    def __init__(self, path, source_digest):
        self.path = Path(path)
        self.source_digest = source_digest
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
            if self.data.get("source_digest") != source_digest:
                raise ImportConflict(
                    f"import source digest changed for {self.path}; use a new state file or restore the source"
                )
        else:
            self.data = {"source_digest": source_digest, "projects": {}, "tasks": {}, "relations": {}}

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=self.path.name + ".", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(stable_json(self.data, pretty=True))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def record_project(self, path, project_id, disposition):
        self.data["projects"][path] = {"id": int(project_id), "disposition": disposition}
        self._save()

    def record_task(self, external_id, task_id, disposition):
        self.data["tasks"][external_id] = {"id": int(task_id), "disposition": disposition}
        self._save()

    def record_relation(self, key, disposition):
        self.data["relations"][key] = {"disposition": disposition}
        self._save()

    def task_id(self, external_id):
        value = self.data["tasks"].get(external_id)
        return value["id"] if value else None

    def external_id(self, task_id):
        matches = [external for external, value in self.data["tasks"].items() if value["id"] == int(task_id)]
        if len(matches) > 1:
            raise ImportConflict(f"Vikunja task id {task_id} maps to multiple external IDs")
        return matches[0] if matches else None


def projects_from_client(client):
    return client.paginate("/projects", {"format": "markdown", "is_archived": "true"})


def inventory_from_client(client):
    projects = projects_from_client(client)
    tasks = client.paginate("/tasks", {"format": "markdown", "expand": ["buckets"]})
    labels = client.paginate("/labels")
    return Inventory(projects, tasks, labels)


def collect_tasks(client, query=None, project=None, recursive=False, filter_expr=None):
    """List or search tasks server-side.

    The API matches `q` as a case-insensitive substring of title or description;
    comments are never searched. Project scope and `filter_expr` are combined
    into one filter expression so the server, not this process, does the work.
    """
    clauses = []
    if filter_expr:
        clauses.append(f"({filter_expr})")
    if project:
        projects = projects_from_client(client)
        resolved = ProjectIndex(projects).resolve(project)
        ids = (sorted(descendants(projects, resolved["id"])) if recursive
               else [int(resolved["id"])])
        clauses.append("project_id in " + ", ".join(str(i) for i in ids))
    params = {"format": "markdown", "expand": ["buckets"]}
    if clauses:
        params["filter"] = " && ".join(clauses)
    if query:
        params["q"] = query
    return sorted(client.paginate("/tasks", params), key=lambda t: int(t["id"]))


ACTION_COUNT_SUFFIX = {"reuse": "reused", "create": "created", "update": "updated",
                       "set": "set", "delete": "deleted"}


def summarize_actions(actions):
    counts = {}
    for action in actions:
        suffix = ACTION_COUNT_SUFFIX.get(action.kind, action.kind)
        counts[f"{action.entity}s_{suffix}"] = counts.get(f"{action.entity}s_{suffix}", 0) + 1
    return {
        "actions": [action.as_dict() for action in actions],
        "counts": dict(sorted(counts.items())),
        "dry_run": True,
    }


def _state_path(plan, state_dir):
    return Path(state_dir) / "imports" / f"{plan.digest}.json"


class Importer:
    def __init__(self, client, plan, state_dir=DEFAULT_STATE_DIR, allow_create_root=False):
        self.client = client
        self.plan = plan
        self.state = ImportState(_state_path(plan, state_dir), plan.digest)
        self.allow_create_root = allow_create_root
        self._kanban_cache = {}

    def _state_task_ids(self):
        return {
            external_id: value["id"]
            for external_id, value in self.state.data["tasks"].items()
        }

    def dry_run(self):
        inventory = inventory_from_client(self.client)
        state_task_ids = self._state_task_ids()
        result = summarize_actions(build_import_actions(
            self.plan, inventory, not self.allow_create_root, state_task_ids=state_task_ids,
        ))
        result["stale"] = find_stale_bindings(self.plan, inventory, state_task_ids)
        return result

    def apply(self):
        inventory = inventory_from_client(self.client)
        state_task_ids = self._state_task_ids()
        actions = build_import_actions(
            self.plan, inventory, not self.allow_create_root, state_task_ids=state_task_ids,
        )
        stale = find_stale_bindings(self.plan, inventory, state_task_ids)
        projects = ProjectIndex(inventory.projects)
        path_to_id = {projects.path_for(p): int(p["id"]) for p in inventory.projects}
        report = {"projects_created": 0, "projects_reused": 0, "tasks_created": 0,
                  "tasks_reused": 0, "tasks_updated": 0, "labels_created": 0,
                  "labels_reused": 0, "relations_created": 0, "relations_reused": 0,
                  "assignees_set": 0, "assignees_reused": 0, "buckets_set": 0,
                  "buckets_reused": 0}

        for action in [a for a in actions if a.entity == "project"]:
            if action.kind == "reuse":
                project_id = int(action.details["id"])
                report["projects_reused"] += 1
            else:
                parent_id = path_to_id.get(action.details["parent_path"], 0)
                title = _normalize_path(action.key)[-1]
                created = self.client.post("/projects?format=markdown", {
                    "title": title, "parent_project_id": parent_id,
                })
                project_id = int(created["id"])
                path_to_id[action.key] = project_id
                report["projects_created"] += 1
            self.state.record_project(action.key, project_id, action.kind)

        plan_by_id = {task.external_id: task for task in self.plan.tasks}
        task_ids = {}
        for action in [a for a in actions if a.entity == "task"]:
            task = plan_by_id[action.key]
            if action.kind == "create":
                body = {
                    "title": task.title, "description": task.desired_description(),
                    "priority": task.priority, "done": task.done,
                }
                body.update(task.fields)
                created = self.client.post(
                    f"/projects/{path_to_id[task.project_path]}/tasks?format=markdown", body
                )
                task_id = int(created["id"])
                report["tasks_created"] += 1
            else:
                task_id = int(action.details["id"])
                if action.kind == "update":
                    changes = dict(action.details["changes"])
                    if "description" in changes:
                        self.client.patch(f"/tasks/{task_id}", changes, markdown=True)
                    else:
                        self.client.patch(f"/tasks/{task_id}", changes)
                    report["tasks_updated"] += 1
                else:
                    report["tasks_reused"] += 1
            task_ids[action.key] = task_id
            self.state.record_task(action.key, task_id, action.kind)

        labels = {label["title"]: int(label["id"]) for label in inventory.labels}
        for title in sorted({label for task in self.plan.tasks for label in task.labels}):
            if title in labels:
                report["labels_reused"] += 1
            else:
                created = self.client.post("/labels?format=markdown", {"title": title})
                labels[title] = int(created["id"])
                report["labels_created"] += 1
        current = {
            int(t["id"]): {int(label["id"]) for label in (t.get("labels") or [])}
            for t in inventory.tasks
        }
        for task in self.plan.tasks:
            task_id = task_ids[task.external_id]
            attached = current.get(task_id, set())
            for title in task.labels:
                label_id = labels[title]
                if label_id not in attached:
                    attach_label(self.client, task_id, label_id)

        for action in [a for a in actions if a.entity == "assignee"]:
            if action.kind == "reuse":
                report["assignees_reused"] += 1
                continue
            users = [self._resolve_user(name) for name in action.details["usernames"]]
            self.client.put(f"/tasks/{task_ids[action.key]}/assignees/bulk",
                            {"assignees": [{"id": user_id} for user_id in users]})
            report["assignees_set"] += 1

        for action in [a for a in actions if a.entity == "bucket"]:
            if action.kind == "reuse":
                report["buckets_reused"] += 1
                continue
            task = plan_by_id[action.key]
            self._move_to_bucket(
                task_ids[action.key], path_to_id[task.project_path], action.details["title"]
            )
            report["buckets_set"] += 1

        refreshed = inventory_from_client(self.client)
        resolved = dict(task_ids)
        for external_id, existing in _tasks_by_external(refreshed).items():
            resolved.setdefault(external_id, int(existing["id"]))
        relations = existing_relation_keys(refreshed.tasks)
        for base, kind, other in self.plan.relation_edges():
            key = f"{base}:{kind}:{other}"
            numeric_key = (resolved[base], kind, resolved[other])
            if numeric_key in relations:
                report["relations_reused"] += 1
                self.state.record_relation(key, "reuse")
                continue
            self.client.post(f"/tasks/{resolved[base]}/relations", {
                "other_task_id": resolved[other], "relation_kind": kind,
            })
            report["relations_created"] += 1
            self.state.record_relation(key, "create")
        report["stale"] = stale
        report["verification"] = verify_plan(self.client, self.plan)
        return report

    def _resolve_user(self, username):
        found = self.client.paginate("/users", {"q": username})
        exact = [user for user in found if user.get("username") == username]
        if not exact:
            raise ImportConflict(f"assignee {username!r} did not match any Vikunja user")
        if len(exact) > 1:
            raise AmbiguousMatch(f"assignee {username!r} matched multiple users")
        return int(exact[0]["id"])

    def _kanban(self, project_id):
        cached = self._kanban_cache.get(int(project_id))
        if cached is None:
            try:
                view = resolve_kanban_view(self.client, project_id)
            except NotFound as exc:
                raise ImportConflict(f"{exc} for buckets") from None
            cached = (view, view_buckets(self.client, project_id, view["id"]))
            self._kanban_cache[int(project_id)] = cached
        return cached

    def _move_to_bucket(self, task_id, project_id, bucket_title):
        view, buckets = self._kanban(project_id)
        bucket = resolve_bucket(buckets, bucket_title, missing=ImportConflict)
        return place_in_bucket(self.client, project_id, view, bucket, task_id)


def existing_relation_keys(tasks):
    keys = set()
    for task in tasks:
        task_id = int(task["id"])
        related = task.get("related_tasks") or {}
        for kind, others in related.items():
            for other in others or []:
                keys.add((task_id, kind, int(other["id"])))
    return keys


def verify_plan(client, plan):
    inventory = inventory_from_client(client)
    project_index = ProjectIndex(inventory.projects)
    paths = {project_index.path_for(p): p for p in inventory.projects}
    errors = []
    root = _normalize_path(plan.tasks[0].project_path)[0]
    roots = [p for p in inventory.projects if p["title"] == root and not int(p.get("parent_project_id") or 0)]
    if len(roots) != 1:
        errors.append(f"expected one root {root!r}, found {len(roots)}")
    for path in sorted({task.project_path for task in plan.tasks}):
        if path not in paths:
            errors.append(f"missing project path {path}")
    by_external = {}
    for task in inventory.tasks:
        external = external_id_from_description(task.get("description", ""))
        if external:
            by_external.setdefault(external, []).append(task)
    for desired in plan.tasks:
        matches = by_external.get(desired.external_id, [])
        if len(matches) != 1:
            errors.append(f"external ID {desired.external_id} resolves to {len(matches)} tasks")
            continue
        actual = matches[0]
        for field, expected in (("title", desired.title), ("priority", desired.priority), ("done", desired.done)):
            if actual.get(field) != expected:
                errors.append(f"{desired.external_id}: {field} differs")
        if not description_matches(
            actual.get("description", ""), desired.description, desired.external_id
        ):
            errors.append(f"{desired.external_id}: description differs")
        actual_labels = {label["title"] for label in (actual.get("labels") or [])}
        if actual_labels != set(desired.labels):
            errors.append(f"{desired.external_id}: labels differ")
        if desired.bucket and desired.bucket not in task_bucket_titles(actual):
            errors.append(f"{desired.external_id}: bucket differs")
    relations = existing_relation_keys(inventory.tasks)
    unique = {external: matches[0] for external, matches in by_external.items() if len(matches) == 1}
    for dependency, kind, blocked in plan.relation_edges():
        if dependency in unique and blocked in unique:
            key = (int(unique[dependency]["id"]), kind, int(unique[blocked]["id"]))
            if key not in relations:
                errors.append(f"missing relation {dependency} {kind} {blocked}")
    return {"errors": errors, "ok": not errors, "tasks_expected": len(plan.tasks),
            "relations_expected": len(plan.relation_edges())}


def validation_summary(plan):
    """Local validation result. External references are resolved only on import."""
    return {
        "digest": plan.digest,
        "external_references": list(plan.external_references()),
        "projects": len({t.project_path for t in plan.tasks}),
        "relations": len(plan.relation_edges()),
        "tasks": len(plan.tasks),
        "valid": True,
    }


def project_tree(projects):
    index = ProjectIndex(projects)
    return [{"id": p["id"], "parent_project_id": p.get("parent_project_id", 0),
             "path": index.path_for(p), "title": p["title"]}
            for p in sorted(projects, key=lambda item: index.path_for(item))]


def descendants(projects, root_id):
    wanted = {int(root_id)}
    changed = True
    while changed:
        changed = False
        for project in projects:
            if int(project.get("parent_project_id") or 0) in wanted and int(project["id"]) not in wanted:
                wanted.add(int(project["id"]))
                changed = True
    return wanted


def project_children(projects, parent_ref, recursive=False):
    index = ProjectIndex(projects)
    parent = index.resolve(parent_ref)
    parent_id = int(parent["id"])
    if recursive:
        child_ids = descendants(projects, parent_id) - {parent_id}
    else:
        child_ids = {int(project["id"]) for project in projects
                     if int(project.get("parent_project_id") or 0) == parent_id}
    children = [project for project in projects if int(project["id"]) in child_ids]
    return [{"id": project["id"],
             "parent_project_id": project.get("parent_project_id", 0),
             "path": index.path_for(project), "title": project["title"]}
            for project in sorted(children, key=index.path_for)]


def make_snapshot(client, project_ref=None, recursive=False):
    inventory = inventory_from_client(client)
    projects = inventory.projects
    tasks = inventory.tasks
    if project_ref is not None:
        project = ProjectIndex(projects).resolve(project_ref)
        ids = descendants(projects, project["id"]) if recursive else {int(project["id"])}
        projects = [p for p in projects if int(p["id"]) in ids]
        tasks = [t for t in tasks if int(t.get("project_id") or 0) in ids]
    return {
        "projects": project_tree(projects),
        "tasks": sorted(tasks, key=lambda task: int(task["id"])),
    }


def _execution_edges(tasks):
    """Unique blocking edges, normalized to blocker -> blocked task."""
    edges = {}
    for task in tasks:
        task_id = int(task["id"])
        related = task.get("related_tasks") or {}
        for other in related.get("blocked") or []:
            edges[(int(other["id"]), task_id)] = other
        for other in related.get("blocking") or []:
            edges.setdefault((task_id, int(other["id"])), task)
    return edges


def _task_project_path(task, project_index):
    project = project_index.by_id.get(int(task.get("project_id") or 0))
    return project_index.path_for(project) if project else None


def _task_summary(task, state, project_index, blocked_by=()):
    return {
        "blocked_by": list(blocked_by),
        "columns": task_bucket_titles(task),
        "external_id": external_id_from_description(task.get("description", "")),
        "id": int(task["id"]),
        "index": int(task.get("index") or 0),
        "priority": int(task.get("priority") or 0),
        "project": _task_project_path(task, project_index),
        "state": state,
        "title": task.get("title", ""),
    }


def _ready_order(task):
    columns = {title.casefold() for title in task_bucket_titles(task)}
    active_rank = 0 if columns & ACTIVE_BUCKET_TITLES else 1
    index = int(task.get("index") or 0)
    return (active_rank, -int(task.get("priority") or 0), index or sys.maxsize, int(task["id"]))


def build_execution_graph(inventory, project_ref, recursive=False):
    """Build the task-level execution graph and select the next actionable task.

    Only unfinished `blocked`/`blocking` relations gate execution. Descriptions
    and their local checklist items are deliberately omitted from the result.
    """
    project_index = ProjectIndex(inventory.projects)
    project = project_index.resolve(project_ref)
    project_ids = (descendants(inventory.projects, project["id"])
                   if recursive else {int(project["id"])})
    scoped = [task for task in inventory.tasks
              if int(task.get("project_id") or 0) in project_ids]
    all_by_id = {int(task["id"]): task for task in inventory.tasks}
    raw_edges = _execution_edges(inventory.tasks)
    scoped_ids = {int(task["id"]) for task in scoped}
    relevant_edges = {edge: fallback for edge, fallback in raw_edges.items()
                      if edge[1] in scoped_ids}

    blockers = {task_id: [] for task_id in scoped_ids}
    for (source_id, target_id), fallback in relevant_edges.items():
        source = all_by_id.get(source_id, fallback)
        if not bool(source.get("done")):
            blockers[target_id].append(source)

    done, ready_tasks, blocked_tasks = [], [], []
    for task in scoped:
        task_id = int(task["id"])
        if bool(task.get("done")):
            done.append(_task_summary(task, "done", project_index))
            continue
        open_blockers = sorted(blockers[task_id], key=lambda item: int(item["id"]))
        if not open_blockers:
            ready_tasks.append(task)
            continue
        blocker_summaries = [{
            "done": bool(other.get("done")),
            "external_id": external_id_from_description(other.get("description", "")),
            "id": int(other["id"]),
            "project": _task_project_path(other, project_index),
            "title": other.get("title", ""),
        } for other in open_blockers]
        blocked_tasks.append(_task_summary(task, "blocked", project_index, blocker_summaries))

    ready_tasks.sort(key=_ready_order)
    ready = [_task_summary(task, "ready", project_index) for task in ready_tasks]
    done.sort(key=lambda task: (task["index"] or sys.maxsize, task["id"]))
    blocked_tasks.sort(key=lambda task: (task["index"] or sys.maxsize, task["id"]))
    task_summaries = sorted(done + ready + blocked_tasks,
                            key=lambda task: (task["index"] or sys.maxsize, task["id"]))
    edges = []
    for source_id, target_id in sorted(relevant_edges):
        source = all_by_id.get(source_id, relevant_edges[(source_id, target_id)])
        edges.append({"from": source_id, "from_done": bool(source.get("done")), "to": target_id})
    open_count = len(ready) + len(blocked_tasks)
    return {
        "blocked": blocked_tasks,
        "done": done,
        "edges": edges,
        "project": {"id": int(project["id"]), "path": project_index.path_for(project)},
        "ready": ready,
        "selected": ready[0] if ready else None,
        "summary": {"blocked": len(blocked_tasks), "done": len(done),
                    "open": open_count, "ready": len(ready)},
        "tasks": task_summaries,
    }


class Rendered:
    """A result whose JSON payload and human text are not the same shape.

    Commands that exist to be read -- a task, a board, a relation set -- deserve
    a layout, while --json must still emit the untouched payload.
    """

    def __init__(self, value, text):
        self.value = value
        self.text = text


EMPTY_TIMESTAMP = "0001-01-01T00:00:00Z"


def _present(value):
    return value not in (None, "", 0, EMPTY_TIMESTAMP, [], {})


def render_task_line(task, bucket=True):
    marker = "✓" if task.get("done") else "·"
    column = ""
    if bucket:
        titles = task_bucket_titles(task)
        column = f" [{', '.join(titles)}]" if titles else ""
    return f"{task['id']:>5} {marker} P{task.get('priority', 0)}{column} {task['title']}"


def render_task_detail(task, project_path=None):
    lines = [render_task_line(task, bucket=False)]
    external = external_id_from_description(task.get("description", ""))
    facts = [
        ("project", project_path),
        ("column", ", ".join(task_bucket_titles(task))),
        ("plan ID", external),
        ("labels", ", ".join(sorted(l["title"] for l in (task.get("labels") or [])))),
        ("assignees", ", ".join(sorted(u.get("username", "") for u in (task.get("assignees") or [])))),
        ("due", task.get("due_date")),
        ("start", task.get("start_date")),
        ("end", task.get("end_date")),
        ("percent done", task.get("percent_done")),
    ]
    for name, value in facts:
        if _present(value):
            lines.append(f"  {name:>13}  {value}")
    related = task.get("related_tasks") or {}
    for kind in sorted(related):
        others = ", ".join(str(other["id"]) for other in related[kind])
        lines.append(f"  {kind:>13}  {others}")
    description = (task.get("description") or "").strip()
    lines.append("")
    lines.append(description if description else "(no description)")
    comments = task.get("comments")
    if comments:
        lines.append("")
        lines.append(f"--- comments ({len(comments)}) ---")
        for comment in comments:
            author = (comment.get("author") or {}).get("username", "?")
            lines.append(f"[{comment.get('created', '')} {author}]")
            lines.append((comment.get("comment") or "").strip())
            lines.append("")
    return "\n".join(lines).rstrip()


def render_relations(related):
    if not related:
        return "(no relations)"
    lines = []
    for kind in sorted(related):
        lines.append(kind)
        for other in related[kind]:
            lines.append("  " + render_task_line(other, bucket=False))
    return "\n".join(lines)


def render_buckets(buckets):
    lines = []
    for bucket in buckets:
        size = str(bucket["count"])
        if bucket["limit"]:
            size += f"/{bucket['limit']}"
        lines.append(f"{bucket['id']:>5}  {bucket['title']}  {size}")
    return "\n".join(lines)


def render_board(board):
    lines = [f"{board['project']} · {board['view']['title']} (id {board['view']['id']})", ""]
    for column in board["columns"]:
        size = str(len(column["tasks"]))
        if column.get("limit"):
            size += f"/{column['limit']}"
        lines.append(f"{column['title']}  {size}")
        for task in column["tasks"]:
            lines.append("  " + render_task_line(task, bucket=False))
        if not column["tasks"]:
            lines.append("  (empty)")
        lines.append("")
    return "\n".join(lines).rstrip()


def _execution_task_line(task):
    external = f" [{task['external_id']}]" if task.get("external_id") else ""
    columns = f" · {', '.join(task.get('columns') or [])}" if task.get("columns") else ""
    return f"{task['id']}{external} · {task['title']}{columns}"


def render_next_task(graph):
    selected = graph.get("selected")
    if selected is None:
        if graph["summary"]["open"] == 0:
            return f"{graph['project']['path']}\nNo open tasks."
        return f"{graph['project']['path']}\nNo actionable task; all open tasks are blocked."
    lines = [graph["project"]["path"], "", "Next: " + _execution_task_line(selected)]
    if len(graph["ready"]) > 1:
        lines.extend(["", "Also ready:"])
        lines.extend("  " + _execution_task_line(task) for task in graph["ready"][1:])
    return "\n".join(lines)


def _mermaid_label(task):
    text = f"{task['id']} · {task['title']}"
    return text.replace("&", "&amp;").replace('"', "&quot;").replace("\n", " ")


def render_execution_mermaid(graph):
    tasks_by_id = {int(task["id"]): task for task in graph["tasks"]}
    lines = ["flowchart TD"]
    linked = set()
    for edge in graph["edges"]:
        source, target = int(edge["from"]), int(edge["to"])
        if source not in tasks_by_id:
            continue
        linked.update((source, target))
        lines.append(
            f'  t{source}["{_mermaid_label(tasks_by_id[source])}"] --> '
            f't{target}["{_mermaid_label(tasks_by_id[target])}"]'
        )
    for task in graph["tasks"]:
        task_id = int(task["id"])
        if task_id not in linked:
            lines.append(f'  t{task_id}["{_mermaid_label(task)}"]')
    lines.extend([
        "  classDef selected fill:#d7f5dd,stroke:#188038,stroke-width:3px",
        "  classDef ready fill:#e8f0fe,stroke:#1a73e8",
        "  classDef blocked fill:#fce8e6,stroke:#c5221f",
        "  classDef done fill:#f1f3f4,stroke:#80868b,color:#5f6368",
    ])
    selected_id = int(graph["selected"]["id"]) if graph.get("selected") else None
    for task in graph["tasks"]:
        task_id = int(task["id"])
        state = "selected" if task_id == selected_id else task["state"]
        lines.append(f"  class t{task_id} {state}")
    return "\n".join(lines)


def render_execution_graph(graph):
    lines = [graph["project"]["path"], ""]
    for heading, key in (("Ready", "ready"), ("Blocked", "blocked"), ("Done", "done")):
        lines.append(f"{heading} ({len(graph[key])})")
        for task in graph[key]:
            marker = "*" if graph.get("selected") and task["id"] == graph["selected"]["id"] else "-"
            line = f"  {marker} " + _execution_task_line(task)
            if task.get("blocked_by"):
                line += " · blocked by " + ", ".join(
                    str(blocker["id"]) for blocker in task["blocked_by"])
            lines.append(line)
        if not graph[key]:
            lines.append("  (none)")
        lines.append("")
    return "\n".join(lines).rstrip()


def render_human(value):
    if isinstance(value, list):
        return "\n".join(render_human(item) for item in value)
    if isinstance(value, dict):
        if "path" in value and "id" in value:
            return f"{value['id']:>5}  {value['path']}"
        if "title" in value and "id" in value:
            return render_task_line(value)
        return stable_json(value, pretty=True).rstrip()
    return str(value)


def result_exit_code(result):
    if isinstance(result, Rendered):
        result = result.value
    if not isinstance(result, dict):
        return 0
    verification = result.get("verification")
    if not isinstance(verification, dict):
        verification = result if "ok" in result and "errors" in result else None
    if isinstance(verification, dict) and verification.get("ok") is False:
        return 3
    return 0


def _add_output(parser):
    parser.add_argument("--json", action="store_true", help="emit stable machine-readable JSON")


def build_parser():
    parser = argparse.ArgumentParser(prog="vikunja", description=__doc__)
    parser.add_argument("--config-dir", type=Path, default=DEFAULT_CONFIG_DIR)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    _add_output(parser)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")

    projects = commands.add_parser("projects").add_subparsers(dest="operation", required=True)
    for name in ("list", "tree", "search"):
        p = projects.add_parser(name)
        if name == "search": p.add_argument("query")
    p = projects.add_parser("children"); p.add_argument("project"); p.add_argument("--recursive", action="store_true")
    p = projects.add_parser("get"); p.add_argument("project")
    p = projects.add_parser("create"); p.add_argument("title"); p.add_argument("--parent"); p.add_argument("--description"); p.add_argument("--description-file")
    p = projects.add_parser("update"); p.add_argument("project"); p.add_argument("--title"); p.add_argument("--description"); p.add_argument("--description-file"); p.add_argument("--archived", choices=("true", "false"))
    p = projects.add_parser("delete"); p.add_argument("project"); p.add_argument("--force", action="store_true")

    tasks = commands.add_parser("tasks").add_subparsers(dest="operation", required=True)
    for name in ("list", "search"):
        p = tasks.add_parser(name); p.add_argument("--project"); p.add_argument("--recursive", action="store_true")
        p.add_argument("--filter", help='server-side filter expression, e.g. \'done = false && priority >= 3\'')
        if name == "search": p.add_argument("query")
    p = tasks.add_parser("get"); p.add_argument("task"); p.add_argument("--field", help="print one raw field (e.g. description) instead of the whole task")
    p = tasks.add_parser("next"); p.add_argument("--project", required=True); p.add_argument("--recursive", action="store_true")

    def add_task_fields(parser_, include_project, include_title=True):
        if include_title:
            parser_.add_argument("--title")
        parser_.add_argument("--description")
        parser_.add_argument("--description-file", help="read the description from a file, or - for stdin")
        parser_.add_argument("--priority", type=int)
        parser_.add_argument("--done", choices=("true", "false"))
        parser_.add_argument("--due-date", help='ISO-8601 timestamp, or - to clear')
        parser_.add_argument("--start-date")
        parser_.add_argument("--end-date")
        parser_.add_argument("--percent-done", type=float, help="0..1, where 0.5 means 50%%")
        parser_.add_argument("--hex-color")
        parser_.add_argument("--is-favorite", choices=("true", "false"))
        if include_project:
            parser_.add_argument("--project")

    p = tasks.add_parser("create"); p.add_argument("--project", required=True); p.add_argument("--title", required=True); add_task_fields(p, include_project=False, include_title=False)
    p = tasks.add_parser("update"); p.add_argument("task"); add_task_fields(p, include_project=True)
    p.add_argument("--append-description", help="append prose, keeping the external-ID footer last")
    p.add_argument("--append-description-file")
    p = tasks.add_parser("delete"); p.add_argument("task"); p.add_argument("--force", action="store_true")
    p = tasks.add_parser("assign"); p.add_argument("task"); p.add_argument("usernames", nargs="*", help="the complete set of assignees; pass none to unassign everyone")
    for name in ("done", "reopen"):
        p = tasks.add_parser(name); p.add_argument("task")
    p = tasks.add_parser("move"); p.add_argument("task"); p.add_argument("--project", required=True)
    p = tasks.add_parser("bulk-update"); p.add_argument("file", type=Path)
    mode = p.add_mutually_exclusive_group(); mode.add_argument("--dry-run", action="store_true"); mode.add_argument("--apply", action="store_true")

    labels = commands.add_parser("labels").add_subparsers(dest="operation", required=True)
    labels.add_parser("list")
    p = labels.add_parser("add"); p.add_argument("task"); p.add_argument("label")
    p = labels.add_parser("remove"); p.add_argument("task"); p.add_argument("label")
    p = labels.add_parser("delete"); p.add_argument("label"); p.add_argument("--force", action="store_true")

    relations = commands.add_parser("relations").add_subparsers(dest="operation", required=True)
    p = relations.add_parser("list"); p.add_argument("task")
    p = relations.add_parser("add"); p.add_argument("task"); p.add_argument("kind", choices=sorted(RELATION_KINDS)); p.add_argument("other_task")
    p = relations.add_parser("remove"); p.add_argument("task"); p.add_argument("kind", choices=sorted(RELATION_KINDS)); p.add_argument("other_task")

    comments = commands.add_parser("comments").add_subparsers(dest="operation", required=True)
    p = comments.add_parser("list"); p.add_argument("task")
    p = comments.add_parser("add"); p.add_argument("task"); p.add_argument("comment", nargs="?"); p.add_argument("--comment-file")

    board = commands.add_parser("board").add_subparsers(dest="operation", required=True)
    for name in ("show", "buckets"):
        p = board.add_parser(name); p.add_argument("project")
        p.add_argument("--view", help="kanban view id or title, when the project has several")
    p = board.add_parser("move"); p.add_argument("task"); p.add_argument("bucket")
    p.add_argument("--view", help="kanban view id or title, when the project has several")

    p = commands.add_parser("snapshot"); p.add_argument("--project"); p.add_argument("--recursive", action="store_true")
    p = commands.add_parser("graph"); p.add_argument("--project", required=True); p.add_argument("--recursive", action="store_true"); p.add_argument("--format", choices=("human", "mermaid"), default="human")
    p = commands.add_parser("external").add_subparsers(dest="operation", required=True)
    r = p.add_parser("resolve"); r.add_argument("reference")

    imports = commands.add_parser("import").add_subparsers(dest="operation", required=True)
    for name in ("validate", "plan", "verify"):
        p = imports.add_parser(name); p.add_argument("csv", type=Path)
        if name == "plan":
            mode = p.add_mutually_exclusive_group()
            mode.add_argument("--dry-run", action="store_true")
            mode.add_argument("--apply", action="store_true")
            p.add_argument(
                "--create-root", action="store_true",
                help="explicitly allow creation when the plan root does not exist",
            )
    return parser


def _resolve_inventory(client):
    return inventory_from_client(client)


def dispatch(args, config):
    client = VikunjaClient(config.url, config.token)
    if args.command == "status":
        info = client.get("/info")
        user = client.get("/user")
        return {"api": "v2", "auth": "API token", "instance": config.url,
                "status": "OK", "token": redact_secret(config.token),
                "user": user.get("username"), "version": info.get("version")}
    if args.command == "import":
        plan = parse_plan(args.csv)
        if args.operation == "validate":
            return validation_summary(plan)
        importer = Importer(
            client, plan, args.state_dir,
            allow_create_root=getattr(args, "create_root", False),
        )
        if args.operation == "plan":
            return importer.apply() if args.apply else importer.dry_run()
        return verify_plan(client, plan)
    if args.command == "tasks" and args.operation in ("list", "search"):
        return collect_tasks(client, getattr(args, "query", None),
                             args.project, args.recursive, args.filter)
    inventory = _resolve_inventory(client)
    project_index = ProjectIndex(inventory.projects)
    task_index = TaskIndex(inventory.tasks)
    if args.command == "graph":
        graph = build_execution_graph(inventory, args.project, args.recursive)
        text = (render_execution_mermaid(graph) if args.format == "mermaid"
                else render_execution_graph(graph))
        return Rendered(graph, text)
    if args.command == "projects":
        if args.operation == "list": return project_tree(inventory.projects)
        if args.operation == "tree": return project_tree(inventory.projects)
        if args.operation == "search": return project_tree([p for p in inventory.projects if args.query.casefold() in p["title"].casefold()])
        if args.operation == "children": return project_children(
            inventory.projects, args.project, args.recursive)
        if args.operation == "get": return project_index.resolve(args.project)
        if args.operation == "create":
            parent_id = project_index.resolve(args.parent)["id"] if args.parent else 0
            body = {"title": args.title, "parent_project_id": parent_id}
            description = read_text_option(args.description, args.description_file, "description")
            if description is not None: body["description"] = description
            return client.post("/projects?format=markdown", body)
        if args.operation == "update":
            project = project_index.resolve(args.project); changes = {}
            if args.title is not None: changes["title"] = args.title
            description = read_text_option(args.description, args.description_file, "description")
            if description is not None: changes["description"] = description
            if args.archived is not None: changes["is_archived"] = args.archived == "true"
            if not changes: raise VikunjaError("no project changes requested")
            return client.patch(f"/projects/{project['id']}", changes, markdown="description" in changes)
        if args.operation == "delete":
            project = project_index.resolve(args.project)
            wanted = descendants(inventory.projects, project["id"])
            doomed = [t for t in inventory.tasks if int(t.get("project_id") or 0) in wanted]
            summary = {"child_projects": len(wanted) - 1, "id": int(project["id"]),
                       "path": project_index.path_for(project), "tasks": len(doomed)}
            if not args.force:
                raise VikunjaError(
                    f"refusing to delete project {summary['path']!r} with {summary['tasks']} task(s) "
                    f"and {summary['child_projects']} child project(s); pass --force to confirm"
                )
            client.delete(f"/projects/{project['id']}")
            return {"deleted": summary}
    if args.command == "board":
        if args.operation == "move":
            task = task_index.resolve(args.task)
            project = project_index.by_id.get(int(task.get("project_id") or 0))
            if project is None:
                raise NotFound(f"task {task['id']} belongs to no readable project")
        else:
            project = project_index.resolve(args.project)
        project_id = int(project["id"])
        view = resolve_kanban_view(client, project_id, args.view)
        view_summary = {"id": int(view["id"]), "title": view.get("title") or "Kanban"}
        if args.operation == "buckets":
            columns = [{"count": len(b.get("tasks") or []), "id": int(b["id"]),
                        "limit": int(b.get("limit") or 0), "title": b.get("title")}
                       for b in board_columns(client, project_id, view["id"])]
            return Rendered(columns, render_buckets(columns))
        if args.operation == "show":
            board_value = {
                "project": project_index.path_for(project),
                "view": view_summary,
                "columns": [{
                    "id": int(column["id"]),
                    "limit": int(column.get("limit") or 0),
                    "title": column.get("title"),
                    "tasks": [{"done": bool(t.get("done")), "id": int(t["id"]),
                               "priority": t.get("priority", 0), "title": t.get("title")}
                              for t in (column.get("tasks") or [])],
                } for column in board_columns(client, project_id, view["id"])],
            }
            return Rendered(board_value, render_board(board_value))
        buckets = view_buckets(client, project_id, view["id"])
        bucket = resolve_bucket(buckets, args.bucket)
        current = task_bucket_in_view(task, view["id"])
        if current is not None and int(current["id"]) == int(bucket["id"]):
            return {"bucket": bucket.get("title"), "moved": False, "task": int(task["id"]),
                    "view": view_summary}
        place_in_bucket(client, project_id, view, bucket, task["id"])
        moved = {"bucket": bucket.get("title"),
                 "from": current.get("title") if current else None,
                 "moved": True, "task": int(task["id"]), "view": view_summary}
        done_bucket = int(view.get("done_bucket_id") or 0)
        if done_bucket and done_bucket == int(bucket["id"]):
            moved["done"] = True
        elif done_bucket and current is not None and done_bucket == int(current["id"]):
            moved["done"] = False
        return moved

    if args.command == "tasks":
        if args.operation == "next":
            graph = build_execution_graph(inventory, args.project, args.recursive)
            return Rendered(graph, render_next_task(graph))
        if args.operation == "bulk-update":
            try:
                specification = json.loads(args.file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ImportConflict(f"cannot read bulk update file {args.file}: {exc}") from None
            actions = plan_bulk_updates(specification, inventory.tasks)
            if args.apply:
                for action in actions:
                    changes = action.details["changes"]
                    client.patch(f"/tasks/{action.key}", changes, markdown="description" in changes)
            result = summarize_actions(actions)
            result["dry_run"] = not args.apply
            result["applied"] = len(actions) if args.apply else 0
            return result
        if args.operation == "get":
            complete = get_task_complete(client, task_index.resolve(args.task)["id"])
            if args.field:
                if args.field not in complete:
                    raise VikunjaError(f"task has no field {args.field!r}")
                return complete[args.field]
            project = project_index.by_id.get(int(complete.get("project_id") or 0))
            return Rendered(complete, render_task_detail(
                complete, project_index.path_for(project) if project else None))
        if args.operation == "create":
            project = project_index.resolve(args.project)
            body = {"title": args.title}
            body.update(task_field_changes(args))
            body.setdefault("description", "")
            body.setdefault("priority", 0)
            return client.post(f"/projects/{project['id']}/tasks?format=markdown", body)
        task = task_index.resolve(args.task)
        if args.operation == "update":
            changes = task_field_changes(args)
            addition = read_text_option(
                args.append_description, args.append_description_file, "append-description"
            )
            if addition is not None:
                if "description" in changes:
                    raise VikunjaError("pass either a replacement description or an append, not both")
                changes["description"] = append_to_description(task.get("description", ""), addition)
            if args.project is not None:
                changes["project_id"] = project_index.resolve(args.project)["id"]
            if not changes: raise VikunjaError("no task changes requested")
            return client.patch(f"/tasks/{task['id']}", changes, markdown="description" in changes)
        if args.operation == "delete":
            summary = {"external_id": external_id_from_description(task.get("description", "")),
                       "id": int(task["id"]), "title": task.get("title", "")}
            if not args.force:
                raise VikunjaError(
                    f"refusing to delete task {summary['id']} ({summary['title']!r}); "
                    f"pass --force to confirm"
                )
            client.delete(f"/tasks/{task['id']}")
            return {"deleted": summary}
        if args.operation == "assign":
            users = []
            for name in args.usernames:
                found = client.paginate("/users", {"q": name})
                exact = [user for user in found if user.get("username") == name]
                if not exact: raise NotFound(f"user {name!r} was not found")
                if len(exact) > 1: raise AmbiguousMatch(f"user {name!r} is ambiguous")
                users.append({"id": int(exact[0]["id"])})
            return client.put(f"/tasks/{task['id']}/assignees/bulk", {"assignees": users})
        if args.operation == "done": return client.patch(f"/tasks/{task['id']}", {"done": True})
        if args.operation == "reopen": return client.patch(f"/tasks/{task['id']}", {"done": False})
        if args.operation == "move": return client.patch(f"/tasks/{task['id']}", {"project_id": project_index.resolve(args.project)["id"]})
    if args.command == "labels":
        if args.operation == "list": return inventory.labels
        if args.operation == "delete":
            matches = [l for l in inventory.labels if l["title"] == args.label or str(l["id"]) == args.label]
            if not matches: raise NotFound(f"label {args.label!r} was not found")
            if len(matches) > 1: raise AmbiguousMatch(f"label {args.label!r} is ambiguous")
            label = matches[0]
            used = [t for t in inventory.tasks
                    if any(int(x["id"]) == int(label["id"]) for x in (t.get("labels") or []))]
            summary = {"id": int(label["id"]), "tasks_labelled": len(used), "title": label["title"]}
            if not args.force:
                raise VikunjaError(
                    f"refusing to delete label {label['title']!r} used by {len(used)} task(s); "
                    f"pass --force to confirm"
                )
            client.delete(f"/labels/{label['id']}")
            return {"deleted": summary}
        task = task_index.resolve(args.task)
        matches = [label for label in inventory.labels if label["title"] == args.label or str(label["id"]) == args.label]
        if not matches: raise NotFound(f"label {args.label!r} was not found")
        if len(matches) > 1: raise AmbiguousMatch(f"label {args.label!r} is ambiguous")
        label = matches[0]
        if args.operation == "add": return attach_label(client, task["id"], label["id"])
        return client.delete(f"/tasks/{task['id']}/labels/{label['id']}")
    if args.command == "relations":
        task = task_index.resolve(args.task)
        if args.operation == "list":
            related = task.get("related_tasks") or {}
            return Rendered(related, render_relations(related))
        other = task_index.resolve(args.other_task)
        if args.operation == "add": return client.post(f"/tasks/{task['id']}/relations", {"other_task_id": other["id"], "relation_kind": args.kind})
        return client.delete(f"/tasks/{task['id']}/relations/{args.kind}/{other['id']}")
    if args.command == "comments":
        task = task_index.resolve(args.task)
        if args.operation == "list": return client.paginate(f"/tasks/{task['id']}/comments", {"format": "markdown"})
        comment = read_text_option(args.comment, args.comment_file, "comment")
        if comment is None: raise VikunjaError("pass a comment argument or --comment-file")
        return client.post(f"/tasks/{task['id']}/comments?format=markdown", {"comment": comment})
    if args.command == "snapshot": return make_snapshot(client, args.project, args.recursive)
    if args.command == "external":
        task = task_index.resolve(args.reference)
        return {"external_id": external_id_from_description(task.get("description", "")), "task_id": task["id"]}
    raise VikunjaError("unsupported command")


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "import" and args.operation == "validate":
            result = validation_summary(parse_plan(args.csv))
            if args.json:
                sys.stdout.write(stable_json(result))
            else:
                print(render_human(result))
            return 0
        config = Config.load(config_dir=args.config_dir)
        result = dispatch(args, config)
        if args.json:
            sys.stdout.write(stable_json(result.value if isinstance(result, Rendered) else result))
        elif isinstance(result, Rendered):
            print(result.text)
        else:
            print(render_human(result))
        return result_exit_code(result)
    except (VikunjaError, OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
