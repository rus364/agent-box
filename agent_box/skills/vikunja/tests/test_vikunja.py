import contextlib
import csv
import importlib.util
import io
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "vikunja.py"
SPEC = importlib.util.spec_from_file_location("vikunja", MODULE_PATH)
vikunja = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(vikunja)


FIELDS = [
    "Title", "Description", "Priority", "Labels", "Project", "Done",
    "Plan ID", "Kind", "Depends On", "Source Tasks", "Order",
]


def write_plan(path, rows):
    with Path(path).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def row(external_id="M01-01", depends="", project="Root / Child", title="Task"):
    return {
        "Title": title,
        "Description": "Meaningful description",
        "Priority": "4",
        "Labels": "migration,architecture",
        "Project": project,
        "Done": "false",
        "Plan ID": external_id,
        "Kind": "decision",
        "Depends On": depends,
        "Source Tasks": "Old #1",
        "Order": "1",
    }


class ConfigTests(unittest.TestCase):
    def test_environment_overrides_files_and_token_is_stripped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.json").write_text('{"url":"http://file:3456/"}', encoding="utf-8")
            (root / "token").write_text("tk_file\n", encoding="utf-8")
            config = vikunja.Config.load(
                env={"VIKUNJA_URL": "http://env:3456/", "VIKUNJA_TOKEN": "tk_env"},
                config_dir=root,
            )
            self.assertEqual(config.url, "http://env:3456")
            self.assertEqual(config.token, "tk_env")

    def test_missing_configuration_has_actionable_error(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(vikunja.ConfigError, "VIKUNJA_URL"):
                vikunja.Config.load(env={}, config_dir=Path(directory))

    def test_secret_redaction_never_returns_full_short_or_long_token(self):
        self.assertEqual(vikunja.redact_secret("tk_1234567890abcdef"), "tk_...cdef")
        self.assertEqual(vikunja.redact_secret("abc"), "***")
        self.assertNotIn("123456", vikunja.redact_secret("tk_1234567890abcdef"))


class _APIHandler(BaseHTTPRequestHandler):
    requests = []

    def log_message(self, *_args):
        pass

    def do_GET(self):
        type(self).requests.append((self.command, self.path, self.headers.get("Authorization")))
        if self.path.startswith("/api/v2/authfail"):
            self.send_response(401)
            self.send_header("Content-Type", "application/problem+json")
            self.end_headers()
            self.wfile.write(b'{"title":"Unauthorized","detail":"bad token"}')
            return
        if self.path.startswith("/api/v2/fail"):
            self.send_response(422)
            self.send_header("Content-Type", "application/problem+json")
            self.end_headers()
            self.wfile.write(b'{"title":"Invalid","detail":"bad value","code":123}')
            return
        if self.path.startswith("/api/v2/items"):
            page = 2 if "page=2" in self.path else 1
            body = {"items": [{"id": page}], "page": page, "per_page": 1,
                    "total": 2, "total_pages": 2}
            encoded = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            return
        if self.path.startswith("/api/v2/virtual"):
            body = {"items": [{"id": -2}, {"id": 1}], "page": 1, "per_page": 50,
                    "total": 1, "total_pages": 1}
            encoded = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            return
        if self.path.startswith("/api/v2/tasks/7?"):
            body = {"id": 7, "title": "Complete", "description": "body",
                    "related_tasks": {"blocking": [{"id": 8}]}}
            encoded = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            return
        if self.path.startswith("/api/v2/tasks/7/comments"):
            body = {"items": [{"id": 1, "comment": "one"}], "page": 1,
                    "per_page": 1, "total": 1, "total_pages": 1}
            encoded = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        type(self).requests.append((self.command, self.path, self.headers.get("Authorization")))
        if self.path == "/api/v2/tasks/7/labels":
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length))
            encoded = json.dumps({"label_id": body["label_id"]}).encode()
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            return
        self.send_response(404)
        self.end_headers()


class ClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _APIHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        _APIHandler.requests = []
        self.client = vikunja.VikunjaClient(self.url, "tk_test_secret", per_page=1)

    def test_pagination_reads_every_page_without_silent_truncation(self):
        self.assertEqual(self.client.paginate("/items"), [{"id": 1}, {"id": 2}])
        self.assertEqual(len(_APIHandler.requests), 2)

    def test_pagination_accepts_documented_synthetic_items_beyond_total(self):
        self.assertEqual(self.client.paginate("/virtual"), [{"id": -2}, {"id": 1}])

    def test_authentication_failure_is_classified_without_token_leak(self):
        with self.assertRaises(vikunja.AuthenticationError) as caught:
            self.client.get("/authfail")
        self.assertNotIn("tk_test_secret", str(caught.exception))

    def test_api_problem_is_actionable_and_preserves_status_and_code(self):
        with self.assertRaises(vikunja.APIError) as caught:
            self.client.get("/fail")
        self.assertEqual(caught.exception.status, 422)
        self.assertEqual(caught.exception.code, 123)
        self.assertIn("bad value", str(caught.exception))

    def test_api_error_response_is_closed_after_reading_the_problem(self):
        stream = io.BytesIO(b'{"title":"Invalid","detail":"bad value"}')
        error = vikunja.urllib.error.HTTPError(
            self.url + "/api/v2/fail", 422, "invalid", {}, stream
        )

        def raise_error(*_args, **_kwargs):
            raise error

        client = vikunja.VikunjaClient(self.url, "tk_test_secret", opener=raise_error)

        with self.assertRaises(vikunja.APIError):
            client.get("/fail")

        self.assertTrue(stream.closed)

    def test_label_attachment_uses_v2_post_contract(self):
        self.assertEqual(vikunja.attach_label(self.client, 7, 9), {"label_id": 9})
        self.assertEqual(_APIHandler.requests[-1][0:2], ("POST", "/api/v2/tasks/7/labels"))

    def test_complete_task_inspection_reads_task_and_all_comments(self):
        result = vikunja.get_task_complete(self.client, 7)
        self.assertEqual(result["id"], 7)
        self.assertEqual(result["comments"], [{"id": 1, "comment": "one"}])
        paths = [request[1] for request in _APIHandler.requests]
        self.assertTrue(any(path.startswith("/api/v2/tasks/7?") for path in paths))
        self.assertTrue(any(path.startswith("/api/v2/tasks/7/comments?") for path in paths))


class ResolutionTests(unittest.TestCase):
    def setUp(self):
        self.projects = [
            {"id": 10, "title": "Root", "parent_project_id": 0},
            {"id": 11, "title": "Child", "parent_project_id": 10},
            {"id": 12, "title": "Child", "parent_project_id": 0},
            {"id": 13, "title": "Leaf", "parent_project_id": 11},
        ]

    def test_project_resolves_by_stable_id_or_exact_path(self):
        index = vikunja.ProjectIndex(self.projects)
        self.assertEqual(index.resolve("13")["id"], 13)
        self.assertEqual(index.resolve("Root / Child / Leaf")["id"], 13)

    def test_ambiguous_project_title_fails(self):
        with self.assertRaisesRegex(vikunja.AmbiguousMatch, "Child"):
            vikunja.ProjectIndex(self.projects).resolve("Child")

    def test_ambiguous_task_title_fails_but_numeric_id_wins(self):
        tasks = [{"id": 1, "title": "Same"}, {"id": 2, "title": "Same"}]
        index = vikunja.TaskIndex(tasks)
        self.assertEqual(index.resolve("2")["id"], 2)
        with self.assertRaises(vikunja.AmbiguousMatch):
            index.resolve("Same")

    def test_project_children_excludes_the_parent_and_grandchildren_by_default(self):
        self.assertEqual(vikunja.project_children(self.projects, "Root"), [
            {"id": 11, "parent_project_id": 10, "path": "Root / Child", "title": "Child"},
        ])

    def test_recursive_project_children_returns_the_whole_subtree_without_parent(self):
        self.assertEqual(
            [project["id"] for project in
             vikunja.project_children(self.projects, "Root", recursive=True)],
            [11, 13],
        )


class PlanTests(unittest.TestCase):
    def test_multiline_csv_description_preserves_physical_line_breaks(self):
        planned = row()
        planned["Description"] = (
            "Goal\nKeep this paragraph separate.\n\n"
            "Work\n- First item\n- Second item"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.csv"
            write_plan(path, [planned])

            parsed = vikunja.parse_plan(path).tasks[0]

        self.assertEqual(parsed.description, planned["Description"])
        self.assertIn("Goal  \nKeep this paragraph separate.", parsed.desired_description())
        self.assertIn("Work  \n- First item  \n- Second item", parsed.desired_description())

    def test_csv_parsing_preserves_semantics_and_dependency_direction(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.csv"
            first = row("M01-01", "")
            second = row("M01-02", "M01-01", title="Second")
            second["Order"] = "2"
            write_plan(path, [first, second])
            plan = vikunja.parse_plan(path)
            self.assertEqual(plan.tasks[1].external_id, "M01-02")
            self.assertEqual(plan.tasks[1].depends_on, ("M01-01",))
            self.assertEqual(plan.relation_edges(), (("M01-01", "blocking", "M01-02"),))

    def test_invalid_external_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.csv"
            write_plan(path, [row("has spaces")])
            with self.assertRaisesRegex(vikunja.PlanValidationError, "external ID"):
                vikunja.parse_plan(path)

    def test_duplicate_external_ids_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.csv"
            a, b = row("M01-01"), row("M01-01", title="Another")
            b["Order"] = "2"
            write_plan(path, [a, b])
            with self.assertRaisesRegex(vikunja.PlanValidationError, "duplicate"):
                vikunja.parse_plan(path)

    def test_self_dependency_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.csv"
            write_plan(path, [row("M01-01", "M01-01")])
            with self.assertRaisesRegex(vikunja.PlanValidationError, "itself"):
                vikunja.parse_plan(path)

    def test_dependency_outside_the_plan_is_an_external_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.csv"
            write_plan(path, [row("M01-01", "M99-99")])
            plan = vikunja.parse_plan(path)
            self.assertEqual(plan.external_references(), ("M99-99",))

    def test_unresolvable_external_reference_fails_at_import_not_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.csv"
            write_plan(path, [row("M01-01", "M99-99", project="Root")])
            plan = vikunja.parse_plan(path)
            inventory = vikunja.Inventory([{"id": 1, "title": "Root", "parent_project_id": 0}], [], [])
            with self.assertRaisesRegex(vikunja.ImportConflict, "M99-99"):
                vikunja.build_import_actions(plan, inventory, require_root=True)

    def test_external_reference_resolves_against_an_existing_marked_task(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.csv"
            write_plan(path, [row("M01-01", "M99-99", project="Root")])
            plan = vikunja.parse_plan(path)
            existing = {"id": 7, "title": "Older", "project_id": 1,
                        "description": "Body\n\nVikunja external ID: M99-99"}
            inventory = vikunja.Inventory(
                [{"id": 1, "title": "Root", "parent_project_id": 0}], [existing], []
            )
            actions = vikunja.build_import_actions(plan, inventory, require_root=True)
            relations = [a for a in actions if a.entity == "relation"]
            self.assertEqual(
                [(a.kind, a.key) for a in relations], [("create", "M99-99:blocking:M01-01")]
            )

    def test_dependency_cycle_is_rejected_with_cycle_members(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.csv"
            a, b = row("A", "B"), row("B", "A", title="B")
            b["Order"] = "2"
            write_plan(path, [a, b])
            with self.assertRaisesRegex(vikunja.PlanValidationError, "cycle.*A.*B"):
                vikunja.parse_plan(path)


class StateAndPlanningTests(unittest.TestCase):
    def test_state_mapping_recovers_task_when_server_stripped_marker(self):
        inventory = vikunja.Inventory(
            projects=[{"id": 1, "title": "Root", "parent_project_id": 0},
                      {"id": 2, "title": "Child", "parent_project_id": 1}],
            tasks=[{"id": 42, "title": "Task", "project_id": 2,
                    "description": "Meaningful description", "priority": 4,
                    "done": False, "labels": None, "related_tasks": {}}],
            labels=[],
        )
        plan = vikunja.Plan(tasks=(vikunja.PlanTask.from_row(row()),), source=Path("x"), digest="d")
        actions = vikunja.build_import_actions(
            plan, inventory, require_root=True, state_task_ids={"M01-01": 42}
        )
        task_action = next(action for action in actions if action.entity == "task")
        self.assertEqual(task_action.kind, "update")
        self.assertEqual(task_action.details["id"], 42)
        self.assertIn("Vikunja external ID: M01-01", task_action.details["changes"]["description"])

    def test_text_external_marker_round_trips_without_html_comment(self):
        description = vikunja.with_external_marker("Body", "M03-04")
        self.assertEqual(description, "Body\n\nVikunja external ID: M03-04")
        self.assertEqual(vikunja.external_id_from_description(description), "M03-04")

    def test_description_equivalence_ignores_markdown_round_trip_but_requires_identity(self):
        source = "Цель\nТекст\n\nPlan ID\nA"
        actual = "Цель  \nТекст\n\nPlan ID  \nA\n\nVikunja external ID: A"
        self.assertTrue(vikunja.description_matches(actual, source, "A"))
        self.assertFalse(vikunja.description_matches(source, source, "A"))
        self.assertFalse(vikunja.description_matches(
            source + "\n\n\\[vikunja-external-id: A]", source, "A"
        ))

    def test_mapping_persists_and_reverse_resolution_survives_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            state = vikunja.ImportState(path, source_digest="abc")
            state.record_task("M01-01", 42, "created")
            loaded = vikunja.ImportState(path, source_digest="abc")
            self.assertEqual(loaded.task_id("M01-01"), 42)
            self.assertEqual(loaded.external_id(42), "M01-01")

    def test_source_change_refuses_to_reuse_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            vikunja.ImportState(path, source_digest="abc").record_task("A", 1, "created")
            with self.assertRaisesRegex(vikunja.ImportConflict, "digest"):
                vikunja.ImportState(path, source_digest="different")

    def test_interrupted_import_resume_does_not_plan_duplicate_task(self):
        inventory = vikunja.Inventory(
            projects=[{"id": 1, "title": "Root", "parent_project_id": 0},
                      {"id": 2, "title": "Child", "parent_project_id": 1}],
            tasks=[{"id": 42, "title": "Task", "project_id": 2,
                    "description": vikunja.with_external_marker("Meaningful description", "M01-01"),
                    "priority": 4, "done": False, "labels": []}],
            labels=[],
        )
        plan = vikunja.Plan(tasks=(vikunja.PlanTask.from_row(row()),), source=Path("x"), digest="d")
        actions = vikunja.build_import_actions(plan, inventory, require_root=True)
        self.assertEqual([a.kind for a in actions if a.entity == "task"], ["reuse"])

    def test_title_match_without_external_marker_is_a_conflict_not_reuse(self):
        inventory = vikunja.Inventory(
            projects=[{"id": 1, "title": "Root", "parent_project_id": 0},
                      {"id": 2, "title": "Child", "parent_project_id": 1}],
            tasks=[{"id": 42, "title": "Task", "project_id": 2, "description": "unrelated"}],
            labels=[],
        )
        plan = vikunja.Plan(tasks=(vikunja.PlanTask.from_row(row()),), source=Path("x"), digest="d")
        with self.assertRaisesRegex(vikunja.ImportConflict, "title"):
            vikunja.build_import_actions(plan, inventory, require_root=True)

    def test_missing_required_root_is_a_conflict_not_a_create(self):
        plan = vikunja.Plan(tasks=(vikunja.PlanTask.from_row(row()),), source=Path("x"), digest="d")
        with self.assertRaisesRegex(vikunja.ImportConflict, "root.*Root"):
            vikunja.build_import_actions(plan, vikunja.Inventory([], [], []), require_root=True)

    def test_dry_run_executor_performs_no_writes(self):
        actions = [vikunja.Action("create", "task", "M01-01", {"project_id": 2})]
        transport = vikunja.RecordingWriter()
        report = vikunja.execute_actions(actions, transport, apply=False)
        self.assertEqual(transport.calls, [])
        self.assertEqual(report["planned"], 1)

    def test_existing_blocking_relation_is_reused_in_correct_direction(self):
        projects = [{"id": 1, "title": "Root", "parent_project_id": 0},
                    {"id": 2, "title": "Child", "parent_project_id": 1}]
        a = row("A", "", title="A")
        b = row("B", "A", title="B"); b["Order"] = "2"
        tasks = [
            {"id": 10, "title": "A", "project_id": 2, "priority": 4, "done": False,
             "description": vikunja.with_external_marker("Meaningful description", "A"),
             "labels": [], "related_tasks": {"blocking": [{"id": 11}]}},
            {"id": 11, "title": "B", "project_id": 2, "priority": 4, "done": False,
             "description": vikunja.with_external_marker("Meaningful description", "B"),
             "labels": [], "related_tasks": {"blocked": [{"id": 10}]}},
        ]
        plan = vikunja.Plan(tasks=(vikunja.PlanTask.from_row(a), vikunja.PlanTask.from_row(b)), source=Path("x"), digest="d")
        actions = vikunja.build_import_actions(plan, vikunja.Inventory(projects, tasks, []), require_root=True)
        relation = [action for action in actions if action.entity == "relation"]
        self.assertEqual([(action.kind, action.key) for action in relation], [("reuse", "A:blocking:B")])

    def test_stable_json_is_sorted_compact_and_newline_terminated(self):
        self.assertEqual(vikunja.stable_json({"z": 1, "a": [2]}), '{"a":[2],"z":1}\n')

    def test_bulk_update_rejects_duplicate_targets_and_read_only_fields(self):
        tasks = [{"id": 1, "title": "One", "description": ""}]
        with self.assertRaisesRegex(vikunja.ImportConflict, "duplicate"):
            vikunja.plan_bulk_updates([{"task": "1", "changes": {"done": True}},
                                       {"task": "1", "changes": {"priority": 5}}], tasks)
        with self.assertRaisesRegex(vikunja.ImportConflict, "not writable"):
            vikunja.plan_bulk_updates([{"task": "1", "changes": {"id": 8}}], tasks)


class _MemoryVikunja:
    def __init__(self):
        self.projects = [{"id": 1, "title": "Root", "parent_project_id": 0}]
        self.tasks = []
        self.labels = []
        self.users = [{"id": 9, "username": "operator"}]
        self.views = {1: [{"id": 50, "title": "Kanban", "view_kind": "kanban",
                           "bucket_configuration_mode": "manual"}]}
        self.buckets = {50: [{"id": 60, "title": "Doing", "project_view_id": 50}]}
        self.writes = []

    def paginate(self, path, _params=None):
        if path == "/projects": return json.loads(json.dumps(self.projects))
        if path == "/tasks": return json.loads(json.dumps(self.tasks))
        if path == "/labels": return json.loads(json.dumps(self.labels))
        if path == "/users":
            query = (_params or {}).get("q", "")
            return [dict(u) for u in self.users if query in u["username"]]
        raise AssertionError(path)

    def get(self, path):
        if path.endswith("/views"):
            return json.loads(json.dumps(self.views[int(path.split("/")[2])]))
        if path.endswith("/buckets"):
            return json.loads(json.dumps(self.buckets[int(path.split("/")[4])]))
        raise AssertionError(path)

    def put(self, path, body):
        self.writes.append(("PUT", path, body))
        if path.endswith("/assignees/bulk"):
            task_id = int(path.split("/")[2])
            task = next(item for item in self.tasks if item["id"] == task_id)
            by_id = {u["id"]: u for u in self.users}
            task["assignees"] = [dict(by_id[a["id"]]) for a in body["assignees"]]
            return dict(task)
        if "/buckets/" in path and path.endswith("/tasks"):
            task_id = int(body["task_id"])
            task = next(item for item in self.tasks if item["id"] == task_id)
            view_id = int(path.split("/")[4])
            bucket = next(b for b in self.buckets[view_id] if b["id"] == int(path.split("/")[6]))
            task["bucket_id"] = 0
            task["buckets"] = [dict(bucket)]
            return dict(task)
        raise AssertionError(path)

    def post(self, path, body):
        self.writes.append(("POST", path, body))
        if path.startswith("/projects?"):
            item = {"id": len(self.projects) + 1, "title": body["title"],
                    "parent_project_id": body.get("parent_project_id", 0)}
            self.projects.append(item); return dict(item)
        if path.startswith("/projects/") and "/tasks?" in path:
            project_id = int(path.split("/")[2])
            item = {"id": 100 + len(self.tasks), "project_id": project_id,
                    "labels": [], "related_tasks": {}, **body}
            self.tasks.append(item); return dict(item)
        if path.startswith("/labels?"):
            item = {"id": 200 + len(self.labels), "title": body["title"]}
            self.labels.append(item); return dict(item)
        if path.endswith("/labels"):
            task_id = int(path.split("/")[2]); label_id = body["label_id"]
            task = next(item for item in self.tasks if item["id"] == task_id)
            label = next(item for item in self.labels if item["id"] == label_id)
            task["labels"].append(dict(label)); return {"label_id": label_id}
        if path.endswith("/relations"):
            task_id = int(path.split("/")[2]); other_id = body["other_task_id"]
            source = next(item for item in self.tasks if item["id"] == task_id)
            target = next(item for item in self.tasks if item["id"] == other_id)
            source["related_tasks"].setdefault(body["relation_kind"], []).append({"id": other_id})
            inverse = {"blocking": "blocked", "blocked": "blocking",
                       "parenttask": "subtask", "subtask": "parenttask"}.get(
                           body["relation_kind"], "related")
            target["related_tasks"].setdefault(inverse, []).append({"id": task_id})
            return {"task_id": task_id, **body}
        raise AssertionError(path)

    def patch(self, path, body, markdown=False):
        self.writes.append(("PATCH", path, body, markdown))
        task_id = int(path.split("/")[2])
        task = next(item for item in self.tasks if item["id"] == task_id)
        task.update(body); return dict(task)


class ImportIntegrationTests(unittest.TestCase):
    def test_apply_tolerates_unrelated_tasks_with_null_labels(self):
        plan = vikunja.Plan((vikunja.PlanTask.from_row(row()),), Path("plan.csv"), "null-labels")
        service = _MemoryVikunja()
        service.tasks.append({
            "id": 9, "project_id": 1, "title": "Unrelated", "description": "",
            "priority": 0, "done": False, "labels": None, "related_tasks": {},
        })
        with tempfile.TemporaryDirectory() as directory:
            report = vikunja.Importer(service, plan, Path(directory)).apply()
        self.assertTrue(report["verification"]["ok"], report["verification"])

    def test_explicit_root_creation_allows_import_into_an_empty_instance(self):
        plan = vikunja.Plan((vikunja.PlanTask.from_row(row()),), Path("plan.csv"), "new-root")
        service = _MemoryVikunja()
        service.projects.clear()
        with tempfile.TemporaryDirectory() as directory:
            report = vikunja.Importer(
                service, plan, Path(directory), allow_create_root=True
            ).apply()
        self.assertEqual(report["projects_created"], 2)
        self.assertEqual(report["tasks_created"], 1)
        self.assertTrue(report["verification"]["ok"], report["verification"])

    def test_apply_then_resume_creates_no_duplicates_and_verifies_relations(self):
        first = row("A", "", title="First")
        second = row("B", "A", title="Second"); second["Order"] = "2"
        plan = vikunja.Plan((vikunja.PlanTask.from_row(first), vikunja.PlanTask.from_row(second)), Path("plan.csv"), "digest")
        service = _MemoryVikunja()
        with tempfile.TemporaryDirectory() as directory:
            report = vikunja.Importer(service, plan, Path(directory)).apply()
            self.assertEqual(report["projects_created"], 1)
            self.assertEqual(report["tasks_created"], 2)
            self.assertEqual(report["relations_created"], 1)
            self.assertTrue(report["verification"]["ok"], report["verification"])
            task_count, project_count = len(service.tasks), len(service.projects)
            resumed = vikunja.Importer(service, plan, Path(directory)).apply()
            self.assertEqual((len(service.tasks), len(service.projects)), (task_count, project_count))
            self.assertEqual(resumed["tasks_reused"], 2)
            self.assertEqual(resumed["relations_reused"], 1)

    def test_importer_dry_run_performs_no_writes(self):
        plan = vikunja.Plan((vikunja.PlanTask.from_row(row()),), Path("plan.csv"), "digest")
        service = _MemoryVikunja()
        with tempfile.TemporaryDirectory() as directory:
            report = vikunja.Importer(service, plan, Path(directory)).dry_run()
        self.assertTrue(report["dry_run"])
        self.assertEqual(service.writes, [])


class KanbanTests(unittest.TestCase):
    class _Board:
        def __init__(self, views, buckets):
            self.views = views
            self.buckets = buckets
            self.writes = []

        def get(self, path):
            if path.endswith("/views"):
                return {"items": json.loads(json.dumps(self.views))}
            return {"items": json.loads(json.dumps(self.buckets))}

        def put(self, path, body):
            self.writes.append((path, body))
            return {}

    def _service(self, views=None, buckets=None):
        return self._Board(
            views if views is not None else [
                {"id": 7, "title": "List", "view_kind": "list"},
                {"id": 8, "title": "Kanban", "view_kind": "kanban",
                 "bucket_configuration_mode": "manual"},
            ],
            buckets if buckets is not None else [
                {"id": 60, "title": "To-Do"}, {"id": 61, "title": "Doing"},
            ],
        )

    def test_kanban_view_is_selected_among_other_view_kinds(self):
        self.assertEqual(vikunja.resolve_kanban_view(self._service(), 1)["id"], 8)

    def test_project_without_a_kanban_view_is_named_not_guessed(self):
        service = self._service(views=[{"id": 7, "title": "List", "view_kind": "list"}])
        with self.assertRaisesRegex(vikunja.NotFound, "no kanban view"):
            vikunja.resolve_kanban_view(service, 1)

    def test_several_kanban_views_are_ambiguous_until_one_is_named(self):
        service = self._service(views=[
            {"id": 8, "title": "Board", "view_kind": "kanban"},
            {"id": 9, "title": "Triage", "view_kind": "kanban"},
        ])
        with self.assertRaisesRegex(vikunja.AmbiguousMatch, "--view"):
            vikunja.resolve_kanban_view(service, 1)
        self.assertEqual(vikunja.resolve_kanban_view(service, 1, "Triage")["id"], 9)
        self.assertEqual(vikunja.resolve_kanban_view(service, 1, "8")["id"], 8)

    def test_bucket_resolves_by_title_or_id_and_missing_names_the_alternatives(self):
        buckets = self._service().buckets
        self.assertEqual(vikunja.resolve_bucket(buckets, "Doing")["id"], 61)
        self.assertEqual(vikunja.resolve_bucket(buckets, "60")["title"], "To-Do")
        with self.assertRaisesRegex(vikunja.NotFound, "To-Do, Doing|Doing, To-Do"):
            vikunja.resolve_bucket(buckets, "Nowhere")

    def test_filter_configured_board_refuses_a_hand_move_with_a_reason(self):
        view = {"id": 8, "view_kind": "kanban", "bucket_configuration_mode": "filter"}
        with self.assertRaisesRegex(vikunja.VikunjaError, "filters"):
            vikunja.place_in_bucket(self._service(), 1, view, {"id": 60}, 42)

    def test_placing_a_task_uses_the_documented_url_shape(self):
        service = self._service()
        view = vikunja.resolve_kanban_view(service, 1)
        vikunja.place_in_bucket(service, 1, view, {"id": 61}, 42)
        self.assertEqual(service.writes,
                         [("/projects/1/views/8/buckets/61/tasks", {"task_id": 42})])

    def test_column_comes_from_the_expanded_buckets_not_the_always_zero_bucket_id(self):
        task = {"bucket_id": 0, "buckets": [{"id": 61, "title": "Doing"}]}
        self.assertEqual(vikunja.task_bucket_titles(task), ["Doing"])
        self.assertEqual(vikunja.task_bucket_titles({"bucket_id": 61}), [])


class RenderingTests(unittest.TestCase):
    def test_execution_overview_contains_no_description_or_checklist_content(self):
        inventory = vikunja.Inventory(
            projects=[{"id": 1, "title": "Rollout", "parent_project_id": 0}],
            tasks=[{
                "id": 11, "index": 1, "project_id": 1, "title": "Network",
                "description": "Secret context\n\n- [ ] Private implementation step",
                "done": False,
            }], labels=[])

        graph = vikunja.build_execution_graph(inventory, "Rollout")

        rendered = vikunja.stable_json(graph)
        self.assertNotIn("description", rendered)
        self.assertNotIn("Private implementation step", rendered)
        self.assertNotIn("first_unchecked", graph["selected"])

    def test_next_task_uses_open_blockers(self):
        inventory = vikunja.Inventory(
            projects=[{"id": 1, "title": "Rollout", "parent_project_id": 0}],
            tasks=[
                {"id": 10, "index": 1, "project_id": 1, "title": "Preflight",
                 "done": True, "related_tasks": {"blocking": [{"id": 11}]}},
                {"id": 11, "index": 2, "project_id": 1, "title": "Network",
                 "done": False, "priority": 3, "buckets": [{"title": "To-Do"}],
                 "description": "- [x] Compare rules\n- [ ] Create the network\n- [ ] Check state",
                 "related_tasks": {
                     "blocked": [{"id": 10}], "blocking": [{"id": 12}]
                 }},
                {"id": 12, "index": 3, "project_id": 1, "title": "Cluster",
                 "done": False, "priority": 5,
                 "description": "- [ ] Create master",
                 "related_tasks": {"blocked": [{"id": 11}]}},
            ], labels=[])

        graph = vikunja.build_execution_graph(inventory, "Rollout")

        self.assertEqual(graph["selected"]["id"], 11)
        self.assertEqual([task["id"] for task in graph["ready"]], [11])
        self.assertEqual(graph["blocked"][0]["blocked_by"][0]["id"], 11)
        self.assertEqual(graph["edges"], [
            {"from": 10, "from_done": True, "to": 11},
            {"from": 11, "from_done": False, "to": 12},
        ])

    def test_next_task_prefers_an_active_column_then_priority_and_index(self):
        inventory = vikunja.Inventory(
            projects=[{"id": 1, "title": "Rollout", "parent_project_id": 0}],
            tasks=[
                {"id": 11, "index": 2, "project_id": 1, "title": "Earlier",
                 "done": False, "priority": 5, "buckets": [{"title": "To-Do"}]},
                {"id": 13, "index": 4, "project_id": 1, "title": "Current",
                 "done": False, "priority": 1, "buckets": [{"title": "Doing"}]},
            ], labels=[])

        graph = vikunja.build_execution_graph(inventory, "Rollout")

        self.assertEqual(graph["selected"]["id"], 13)
        self.assertEqual([task["id"] for task in graph["ready"]], [13, 11])

        inventory.tasks[1]["buckets"] = [{"title": "To-Do"}]
        inventory.tasks.append(
            {"id": 14, "index": 1, "project_id": 1, "title": "Same priority, earlier",
             "done": False, "priority": 5, "buckets": [{"title": "To-Do"}]}
        )
        graph = vikunja.build_execution_graph(inventory, "Rollout")
        self.assertEqual([task["id"] for task in graph["ready"]], [14, 11, 13])

    def test_an_open_cross_project_blocker_is_honoured_but_not_added_as_project_work(self):
        inventory = vikunja.Inventory(
            projects=[
                {"id": 1, "title": "Rollout", "parent_project_id": 0},
                {"id": 2, "title": "Security", "parent_project_id": 0},
            ],
            tasks=[
                {"id": 20, "index": 1, "project_id": 2, "title": "Approve CIDRs",
                 "done": False, "related_tasks": {"blocking": [{"id": 11}]}},
                {"id": 11, "index": 1, "project_id": 1, "title": "Create network",
                 "done": False, "related_tasks": {"blocked": [{"id": 20}]}}
            ], labels=[])

        graph = vikunja.build_execution_graph(inventory, "Rollout")

        self.assertIsNone(graph["selected"])
        self.assertEqual(graph["summary"], {"blocked": 1, "done": 0, "open": 1, "ready": 0})
        self.assertEqual(graph["blocked"][0]["blocked_by"][0]["project"], "Security")

    def test_mermaid_graph_marks_selected_ready_blocked_and_done_tasks(self):
        graph = {
            "project": {"id": 1, "path": "Rollout"},
            "selected": {"id": 11},
            "tasks": [
                {"id": 10, "title": "Done", "state": "done"},
                {"id": 11, "title": "Next", "state": "ready"},
                {"id": 12, "title": "Wait", "state": "blocked"},
            ],
            "edges": [
                {"from": 10, "from_done": True, "to": 11},
                {"from": 11, "from_done": False, "to": 12},
            ],
        }

        text = vikunja.render_execution_mermaid(graph)

        self.assertIn('t10["10 · Done"] --> t11["11 · Next"]', text)
        self.assertIn('class t11 selected', text)
        self.assertIn('class t12 blocked', text)

    def test_task_detail_keeps_the_body_and_names_the_column(self):
        text = vikunja.render_task_detail({
            "id": 5, "title": "T", "priority": 3, "done": False,
            "description": "Goal\n\nVikunja external ID: M01-01",
            "labels": [{"title": "migration"}], "buckets": [{"title": "Doing"}],
            "due_date": vikunja.EMPTY_TIMESTAMP,
        }, "Root / Child")
        self.assertIn("Goal", text)
        self.assertIn("Doing", text)
        self.assertIn("M01-01", text)
        self.assertIn("Root / Child", text)
        self.assertNotIn(vikunja.EMPTY_TIMESTAMP, text)

    def test_relations_render_as_lines_not_embedded_task_objects(self):
        text = vikunja.render_relations(
            {"blocked": [{"id": 9, "title": "Other", "priority": 2, "done": True}]})
        self.assertIn("blocked", text)
        self.assertIn("Other", text)
        self.assertNotIn("description", text)
        self.assertEqual(vikunja.render_relations({}), "(no relations)")

    def test_board_shows_every_column_including_the_empty_ones(self):
        text = vikunja.render_board({
            "project": "Root", "view": {"id": 8, "title": "Kanban"},
            "columns": [
                {"id": 60, "title": "To-Do", "limit": 0,
                 "tasks": [{"id": 5, "title": "T", "priority": 3, "done": False}]},
                {"id": 61, "title": "Doing", "limit": 2, "tasks": []},
            ],
        })
        self.assertIn("To-Do  1", text)
        self.assertIn("Doing  0/2", text)
        self.assertIn("(empty)", text)


class CLITests(unittest.TestCase):
    def test_project_children_parses_parent_scope_and_recursion(self):
        args = vikunja.build_parser().parse_args([
            "projects", "children", "Root / Parent", "--recursive"
        ])
        self.assertEqual(
            (args.command, args.operation, args.project, args.recursive),
            ("projects", "children", "Root / Parent", True),
        )

    def test_next_and_graph_commands_parse_project_scope_and_output_mode(self):
        parser = vikunja.build_parser()
        args = parser.parse_args(["tasks", "next", "--project", "Rollout", "--recursive"])
        self.assertEqual(
            (args.command, args.operation, args.project, args.recursive),
            ("tasks", "next", "Rollout", True),
        )
        args = parser.parse_args([
            "graph", "--project", "Rollout", "--recursive", "--format", "mermaid"
        ])
        self.assertEqual(
            (args.command, args.project, args.recursive, args.format),
            ("graph", "Rollout", True, "mermaid"),
        )

    def test_failed_readback_verification_returns_nonzero(self):
        self.assertEqual(
            vikunja.result_exit_code({"verification": {"ok": False, "errors": ["drift"]}}),
            3,
        )
        self.assertEqual(vikunja.result_exit_code({"verification": {"ok": True}}), 0)

    def test_standalone_verify_drift_is_visible_in_the_exit_code(self):
        self.assertEqual(vikunja.result_exit_code({"ok": False, "errors": ["bucket differs"]}), 3)
        self.assertEqual(vikunja.result_exit_code({"ok": True, "errors": []}), 0)
        self.assertEqual(vikunja.result_exit_code({"ok": False}), 0)

    def test_board_commands_parse_with_and_without_an_explicit_view(self):
        parser = vikunja.build_parser()
        args = parser.parse_args(["board", "move", "M01-01", "Doing"])
        self.assertEqual((args.command, args.operation, args.task, args.bucket, args.view),
                         ("board", "move", "M01-01", "Doing", None))
        args = parser.parse_args(["board", "show", "Root", "--view", "Triage"])
        self.assertEqual((args.operation, args.project, args.view), ("show", "Root", "Triage"))

    def test_local_validation_needs_neither_url_nor_token(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = root / "plan.csv"
            write_plan(plan, [row()])
            stdout, stderr = io.StringIO(), io.StringIO()
            old_out, old_err = vikunja.sys.stdout, vikunja.sys.stderr
            old_env = dict(os.environ)
            try:
                vikunja.sys.stdout, vikunja.sys.stderr = stdout, stderr
                os.environ.pop("VIKUNJA_URL", None)
                os.environ.pop("VIKUNJA_TOKEN", None)
                code = vikunja.main(["--config-dir", str(root / "missing"), "--json",
                                     "import", "validate", str(plan)])
            finally:
                vikunja.sys.stdout, vikunja.sys.stderr = old_out, old_err
                os.environ.clear(); os.environ.update(old_env)
            self.assertEqual(code, 0, stderr.getvalue())
            self.assertEqual(json.loads(stdout.getvalue())["valid"], True)


if __name__ == "__main__":
    unittest.main()


def json_plan(path, entries):
    Path(path).write_text(json.dumps({"tasks": entries}, ensure_ascii=False), encoding="utf-8")


class PlanFormatTests(unittest.TestCase):
    def test_json_plan_carries_multiline_prose_without_csv_quoting(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            json_plan(path, [{
                "id": "M01-01", "title": "Task", "project": "Root / Child",
                "description": "Line one\n\n- bullet, with a comma\n- \"quoted\"",
                "priority": 4, "labels": ["migration", "decision"],
            }])
            task = vikunja.parse_plan(path).tasks[0]
            self.assertEqual(task.description, 'Line one\n\n- bullet, with a comma\n- "quoted"')
            self.assertEqual(task.labels, ("migration", "decision"))
            self.assertEqual(task.priority, 4)

    def test_json_and_csv_plans_produce_the_same_task(self):
        with tempfile.TemporaryDirectory() as directory:
            csv_path, json_path = Path(directory) / "p.csv", Path(directory) / "p.json"
            write_plan(csv_path, [row("M01-01", "", title="Same")])
            json_plan(json_path, [{
                "id": "M01-01", "title": "Same", "project": "Root / Child",
                "description": "Meaningful description", "priority": 4,
                "labels": ["migration", "architecture"], "done": False,
                "kind": "decision", "source_tasks": "Old #1", "order": 1,
            }])
            self.assertEqual(
                vikunja.parse_plan(csv_path).tasks[0].as_dict(),
                vikunja.parse_plan(json_path).tasks[0].as_dict(),
            )

    def test_unknown_json_keys_are_rejected_with_the_known_set(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            json_plan(path, [{"id": "A", "title": "T", "project": "Root",
                              "description": "D", "priorty": 3}])
            with self.assertRaisesRegex(vikunja.PlanValidationError, "priorty"):
                vikunja.parse_plan(path)

    def test_minimal_csv_needs_only_the_four_required_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.csv"
            path.write_text(
                "Title,Description,Project,Plan ID\nOne,Body,Root,A\nTwo,Body,Root,B\n",
                encoding="utf-8",
            )
            plan = vikunja.parse_plan(path)
            self.assertEqual([t.external_id for t in plan.tasks], ["A", "B"])
            self.assertEqual([t.order for t in plan.tasks], [1, 2])
            self.assertEqual(plan.tasks[0].priority, 0)
            self.assertFalse(plan.tasks[0].done)

    def test_repeated_order_values_are_allowed_and_ordering_stays_deterministic(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.csv"
            path.write_text(
                "Title,Description,Project,Plan ID,Order\nOne,B,Root,B,7\nTwo,B,Root,A,7\n",
                encoding="utf-8",
            )
            self.assertEqual([t.external_id for t in vikunja.parse_plan(path).tasks], ["A", "B"])

    def test_unknown_csv_column_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.csv"
            path.write_text("Title,Description,Project,Plan ID,Nope\nA,B,Root,X,1\n", encoding="utf-8")
            with self.assertRaisesRegex(vikunja.PlanValidationError, "Nope"):
                vikunja.parse_plan(path)

    def test_optional_scalars_are_validated_and_blank_stays_unmanaged(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            json_plan(path, [{"id": "A", "title": "T", "project": "Root", "description": "D",
                              "due_date": "2026-09-02", "percent_done": 0.5, "hex_color": "#AABBCC"}])
            task = vikunja.parse_plan(path).tasks[0]
            self.assertEqual(task.fields["due_date"], "2026-09-02T00:00:00Z")
            self.assertEqual(task.fields["hex_color"], "aabbcc")
            self.assertNotIn("start_date", task.fields)
            self.assertFalse(task.manages_assignees())
            json_plan(path, [{"id": "A", "title": "T", "project": "Root", "description": "D",
                              "percent_done": 50}])
            with self.assertRaisesRegex(vikunja.PlanValidationError, "between 0 and 1"):
                vikunja.parse_plan(path)

    def test_empty_assignee_list_clears_while_absence_leaves_it_alone(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            json_plan(path, [{"id": "A", "title": "T", "project": "Root",
                              "description": "D", "assignees": []}])
            task = vikunja.parse_plan(path).tasks[0]
            self.assertTrue(task.manages_assignees())
            self.assertEqual(task.assignees, ())

    def test_parent_edge_is_posted_on_the_child_and_cycles_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            json_plan(path, [
                {"id": "P", "title": "Parent", "project": "Root", "description": "D"},
                {"id": "C", "title": "Child", "project": "Root", "description": "D", "parent": "P"},
            ])
            self.assertIn(("C", "parenttask", "P"), vikunja.parse_plan(path).relation_edges())
            json_plan(path, [
                {"id": "P", "title": "Parent", "project": "Root", "description": "D", "parent": "C"},
                {"id": "C", "title": "Child", "project": "Root", "description": "D", "parent": "P"},
            ])
            with self.assertRaisesRegex(vikunja.PlanValidationError, "cycle"):
                vikunja.parse_plan(path)


class StaleBindingTests(unittest.TestCase):
    def test_dropped_dependency_and_label_are_reported_not_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            json_plan(path, [
                {"id": "A", "title": "A", "project": "Root", "description": "D"},
                {"id": "B", "title": "B", "project": "Root", "description": "D", "labels": ["keep"]},
            ])
            plan = vikunja.parse_plan(path)
            tasks = [
                {"id": 1, "title": "A", "project_id": 1, "labels": [],
                 "description": "D\n\nVikunja external ID: A", "related_tasks": {}},
                {"id": 2, "title": "B", "project_id": 1,
                 "labels": [{"id": 5, "title": "keep"}, {"id": 6, "title": "gone"}],
                 "description": "D\n\nVikunja external ID: B",
                 "related_tasks": {"blocked": [{"id": 1}]}},
            ]
            inventory = vikunja.Inventory(
                [{"id": 1, "title": "Root", "parent_project_id": 0}], tasks, []
            )
            stale = vikunja.find_stale_bindings(plan, inventory)
            self.assertEqual(stale["relations"], [{"kind": "blocked", "other": "A", "task": "B"}])
            self.assertEqual(stale["labels"], [{"label": "gone", "task": "B"}])
            actions = vikunja.build_import_actions(plan, inventory, require_root=True)
            self.assertFalse([a for a in actions if a.kind == "delete"])


class TextOptionTests(unittest.TestCase):
    def test_inline_and_file_are_mutually_exclusive_and_file_wins_when_alone(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "body.md"
            path.write_text("from file\n", encoding="utf-8")
            self.assertEqual(vikunja.read_text_option(None, path, "description"), "from file\n")
            self.assertEqual(vikunja.read_text_option("inline", None, "description"), "inline")
            self.assertIsNone(vikunja.read_text_option(None, None, "description"))
            with self.assertRaisesRegex(vikunja.VikunjaError, "not both"):
                vikunja.read_text_option("inline", path, "description")

    def test_append_keeps_the_external_marker_last(self):
        current = "Body\n\nVikunja external ID: M01-01"
        updated = vikunja.append_to_description(current, "- extra bullet")
        self.assertTrue(updated.rstrip().endswith("Vikunja external ID: M01-01"))
        self.assertIn("- extra bullet", updated)
        self.assertEqual(vikunja.external_id_from_description(updated), "M01-01")
        self.assertEqual(updated.count("Vikunja external ID"), 1)

    def test_append_on_an_unmarked_description_adds_no_marker(self):
        self.assertEqual(vikunja.append_to_description("Body", "More"), "Body\n\nMore")


class RichFieldImportTests(unittest.TestCase):
    def _plan(self, directory, entries):
        path = Path(directory) / "plan.json"
        json_plan(path, entries)
        return vikunja.parse_plan(path)

    def test_assignees_buckets_dates_and_parent_apply_once_and_then_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = self._plan(directory, [
                {"id": "P", "title": "Parent", "project": "Root", "description": "D"},
                {"id": "C", "title": "Child", "project": "Root", "description": "D",
                 "parent": "P", "assignees": ["operator"], "bucket": "Doing",
                 "due_date": "2026-09-10", "percent_done": 0.25, "hex_color": "aabbcc"},
            ])
            service = _MemoryVikunja()
            report = vikunja.Importer(service, plan, Path(directory)).apply()
            self.assertEqual(report["tasks_created"], 2)
            self.assertEqual(report["assignees_set"], 1)
            self.assertEqual(report["buckets_set"], 1)
            self.assertEqual(report["relations_created"], 1)
            self.assertTrue(report["verification"]["ok"], report["verification"])
            child = next(t for t in service.tasks if t["title"] == "Child")
            self.assertEqual(child["due_date"], "2026-09-10T00:00:00Z")
            self.assertEqual(child["percent_done"], 0.25)
            self.assertEqual(child["hex_color"], "aabbcc")
            self.assertEqual([b["title"] for b in child["buckets"]], ["Doing"])
            self.assertEqual([u["username"] for u in child["assignees"]], ["operator"])
            self.assertIn("parenttask", child["related_tasks"])
            resumed = vikunja.Importer(service, plan, Path(directory)).apply()
            self.assertEqual(resumed["tasks_reused"], 2)
            self.assertEqual(resumed["assignees_reused"], 1)
            self.assertEqual(resumed["relations_reused"], 1)
            self.assertEqual(resumed["buckets_reused"], 1)
            self.assertEqual(resumed["buckets_set"], 0)
            self.assertFalse([w for w in service.writes
                              if w[0] == "PUT" and "/buckets/" in w[1]][1:])

    def test_unknown_assignee_fails_closed_before_a_partial_assignment(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = self._plan(directory, [{"id": "A", "title": "T", "project": "Root",
                                           "description": "D", "assignees": ["ghost"]}])
            service = _MemoryVikunja()
            with self.assertRaisesRegex(vikunja.ImportConflict, "ghost"):
                vikunja.Importer(service, plan, Path(directory)).apply()
            self.assertFalse([w for w in service.writes if w[0] == "PUT"])

    def test_missing_bucket_names_the_available_buckets(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = self._plan(directory, [{"id": "A", "title": "T", "project": "Root",
                                           "description": "D", "bucket": "Nowhere"}])
            with self.assertRaisesRegex(vikunja.ImportConflict, "Doing"):
                vikunja.Importer(_MemoryVikunja(), plan, Path(directory)).apply()


class TaskFieldOptionTests(unittest.TestCase):
    def _args(self, argv):
        return vikunja.build_parser().parse_args(argv)

    def test_update_accepts_every_writable_scalar_in_one_call(self):
        args = self._args([
            "tasks", "update", "42", "--title", "T", "--priority", "3", "--done", "true",
            "--due-date", "2026-09-10T00:00:00Z", "--percent-done", "0.5",
            "--hex-color", "aabbcc", "--is-favorite", "false",
        ])
        self.assertEqual(vikunja.task_field_changes(args), {
            "done": True, "due_date": "2026-09-10T00:00:00Z", "hex_color": "aabbcc",
            "is_favorite": False, "percent_done": 0.5, "priority": 3, "title": "T",
        })
        self.assertEqual(set(vikunja.task_field_changes(args)) - vikunja.BULK_WRITABLE_TASK_FIELDS, set())

    def test_out_of_range_values_are_refused_before_any_request(self):
        for argv, message in (
            (["tasks", "update", "1", "--priority", "9"], "between 0 and 5"),
            (["tasks", "update", "1", "--percent-done", "50"], "between 0 and 1"),
            (["tasks", "update", "1", "--done", "yes"], "true or false"),
        ):
            with self.subTest(argv=argv):
                if "--done" in argv:
                    with self.assertRaises(SystemExit), \
                            contextlib.redirect_stderr(io.StringIO()):
                        self._args(argv)
                    continue
                with self.assertRaisesRegex(vikunja.VikunjaError, message):
                    vikunja.task_field_changes(self._args(argv))

    def test_clearing_a_date_sends_the_null_timestamp(self):
        args = self._args(["tasks", "update", "1", "--due-date", "-"])
        self.assertEqual(vikunja.task_field_changes(args)["due_date"], vikunja.NULL_TIMESTAMP)

    def test_delete_commands_require_an_explicit_force_flag(self):
        for argv in (["tasks", "delete", "1"], ["projects", "delete", "Root"],
                     ["labels", "delete", "migration"]):
            with self.subTest(argv=argv):
                self.assertFalse(self._args(argv).force)
                self.assertTrue(self._args(argv + ["--force"]).force)


class BucketInViewTests(unittest.TestCase):
    TASK = {"buckets": [{"id": 60, "title": "Doing", "project_view_id": 8},
                        {"id": 90, "title": "Doing", "project_view_id": 9}]}

    def test_the_column_is_read_from_the_view_being_moved_on(self):
        self.assertEqual(vikunja.task_bucket_in_view(self.TASK, 9)["id"], 90)
        self.assertIsNone(vikunja.task_bucket_in_view(self.TASK, 11))
        self.assertIsNone(vikunja.task_bucket_in_view({}, 8))
