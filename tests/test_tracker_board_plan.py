"""The user chooses column meanings; execution preserves identities and refuses drift."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from agent_box.tracker import extensions, vikunja_board as plans
from agent_box.tracker.core import STATES, TrackerError
from agent_box.tracker import vikunja_api as api
from tracker_fixtures import adapter, board, task


class BoardPlanTests(unittest.TestCase):
    def setUp(self):
        data = board(10, 100, done_bucket=104,
                     titles=("Входящие", "Можно брать", "Делаем", "Проверка", "Готово", "On hold"))
        data["views"][0]["default_bucket_id"] = 102
        for row, position in zip(data["buckets"][10], (300, 100, 600, 200, 500, 400)):
            row.update(position=position, limit=3, count=1)
        self.instance, self.client = adapter(boards={1: data}, tasks={5: task(5, 1, 10, 102, description="Keep me")})
        self.mapping = dict(zip(STATES, range(100, 105)))

    def build(self):
        return plans.build(self.instance, "1", self.mapping)

    def test_discovery_exposes_only_metadata_and_does_not_guess_aliases(self):
        """Even embedded task payloads must not enter the coordinator's cheap first read."""
        self.client.boards[1]["buckets"][10][0]["tasks"] = [{"title": "secret task text"}]
        report = plans.inspect(self.instance, "1")
        self.assertNotIn("secret task text", json.dumps(report))
        self.assertEqual(report["states"], {"Done": "104"})
        self.assertTrue(all(row["count"] == 1 for row in report["columns"]))
        self.assertEqual(self.client.reads, ["/projects/1/views", "/projects/1/views/10/buckets"])
        self.assertEqual(self.client.writes, [])

    def test_explicit_localized_mapping_renames_in_place_and_orders_without_touching_cards(self):
        """Renaming changes native lifecycle meaning while keeping card data and WIP limits."""
        original_tasks = copy.deepcopy(self.client.tasks)
        plan = self.build()
        self.assertEqual(self.client.writes, [])
        preview = plans.execute(self.instance, "1", plan)
        self.assertEqual(preview["status"], "preview")
        self.assertEqual(self.client.writes, [])
        result = plans.execute(self.instance, "1", plan, apply=True)
        self.assertEqual(result["status"], "applied")
        columns = self.client.boards[1]["buckets"][10]
        self.assertEqual([row["title"] for row in columns], [*STATES, "On hold"])
        self.assertEqual([row["id"] for row in columns], list(range(100, 106)))
        self.assertTrue(all(row["limit"] == 3 for row in columns))
        self.assertEqual(self.client.tasks, original_tasks)
        self.assertEqual(self.client.boards[1]["views"][0]["default_bucket_id"], 100)
        self.assertEqual(self.instance.get("5").state, "In progress")
        self.client.writes.clear()
        self.assertEqual(plans.execute(self.instance, "1", plan, True)["status"], "already-applied")
        self.assertEqual(self.client.writes, [])

    def test_explicit_creation_can_be_repeated_without_duplicate_columns(self):
        """A plan with null allocations is recognized after all server IDs have been assigned."""
        self.mapping["Ready"] = None
        plan = self.build()
        plans.execute(self.instance, "1", plan, True)
        self.assertEqual(sum(path.endswith('/buckets') for path, _ in self.client.writes), 1)
        self.assertIn("Можно брать", [row["title"] for row in self.client.boards[1]["buckets"][10]])
        self.client.writes.clear()
        self.assertEqual(plans.execute(self.instance, "1", plan, True)["status"], "already-applied")
        self.assertEqual(self.client.writes, [])

    def test_existing_canonical_columns_are_not_duplicated_by_a_different_mapping(self):
        """A template leftover must not be renamed over a canonical column that will remain."""
        self.client.boards[1]["buckets"][10].append({"id": 106, "title": "Backlog", "position": 700})
        with self.assertRaisesRegex(TrackerError, "duplicate Backlog"):
            self.build()
        self.mapping["Backlog"] = 106
        plan = self.build()
        plans.execute(self.instance, "1", plan, True)
        self.assertIn("Входящие", [row["title"] for row in self.client.boards[1]["buckets"][10]])

    def test_invalid_or_foreign_mappings_refuse_before_writing(self):
        cases = [[], {}, {**self.mapping, "Other": 105}, {**self.mapping, "Ready": 100},
                 {**self.mapping, "Ready": 999}, {**self.mapping, "Ready": True},
                 {**self.mapping, "Ready": []}, {**self.mapping, "Ready": "-1"}]
        for mapping in cases:
            with self.subTest(mapping=mapping), self.assertRaises(TrackerError):
                plans.build(self.instance, "1", mapping)
        self.assertEqual(self.client.writes, [])

    def test_board_drift_including_counts_blocks_the_original_plan(self):
        """Confirmation names one observed board; subsequent edits require a new proposal."""
        for field, value in (("title", "Changed"), ("position", 999), ("limit", 4), ("count", 2)):
            with self.subTest(field=field):
                self.setUp()
                plan = self.build()
                self.client.boards[1]["buckets"][10][0][field] = value
                with self.assertRaisesRegex(TrackerError, "Board changed"):
                    plans.execute(self.instance, "1", plan, True)
                self.assertEqual(self.client.writes, [])

    def test_changed_view_identity_or_default_refuses_before_writes(self):
        plan = self.build()
        for field, value in (("default_bucket_id", 101), ("done_bucket_id", 100)):
            original = self.client.boards[1]["views"][0][field]
            self.client.boards[1]["views"][0][field] = value
            with self.assertRaisesRegex(TrackerError, "Board changed"):
                plans.execute(self.instance, "1", plan, True)
            self.client.boards[1]["views"][0][field] = original
        self.client.boards[1]["views"][0]["id"] = 20
        self.client.boards[1]["buckets"][20] = self.client.boards[1]["buckets"][10]
        with self.assertRaisesRegex(TrackerError, "Board changed"):
            plans.execute(self.instance, "1", plan, True)
        self.assertEqual(self.client.writes, [])

    def test_foreign_or_edited_plans_never_run_arbitrary_operations(self):
        plan = self.build()
        for key, value in (("server", "https://other.example"), ("provider", "github"),
                           ("project", "9"), ("changes", {}), ("delete", [100])):
            with self.subTest(key=key), self.assertRaises(TrackerError):
                plans.execute(self.instance, "1", {**plan, key: value}, True)
        self.assertEqual(self.client.writes, [])

    def test_future_plan_format_is_rejected_before_reading_payload_or_tracker(self):
        self.client.reads.clear()
        with self.assertRaisesRegex(TrackerError, "format_version"):
            plans.execute(self.instance, "1", {"format_version": 2}, True)
        self.assertEqual((self.client.reads, self.client.writes), ([], []))

    def test_ambiguous_or_filter_boards_stay_readable_but_cannot_apply(self):
        self.client.boards[1]["views"][0]["bucket_configuration_mode"] = "filter"
        self.assertIn("filters", "; ".join(plans.inspect(self.instance, "1")["problems"]))
        with self.assertRaisesRegex(TrackerError, "manually configured"):
            self.build()
        self.client.boards[1]["views"].append({"id": 11, "view_kind": "kanban"})
        self.assertIsNone(plans.inspect(self.instance, "1")["view"])
        with self.assertRaises(TrackerError):
            self.build()
        self.assertEqual(self.client.writes, [])

    def test_a_lost_create_reply_is_not_retried_with_the_old_plan(self):
        """Partial results require fresh inspection; a write timeout must not create twice."""
        self.mapping["Ready"] = None
        plan = self.build()
        post = self.client.post
        def lost(path, body):
            post(path, body)
            raise api.APIError(0, "reply lost", method="POST")
        self.client.post = lost
        with self.assertRaisesRegex(TrackerError, "outcome unknown"):
            plans.execute(self.instance, "1", plan, True)
        count = len(self.client.writes)
        with self.assertRaisesRegex(TrackerError, "Board changed"):
            plans.execute(self.instance, "1", plan, True)
        self.assertEqual(len(self.client.writes), count)

    def test_init_requires_a_saved_plan_and_reuses_it_after_confirmation(self):
        """CLI discovery and mapping construction are read-only even when mapping names are obvious."""
        with tempfile.TemporaryDirectory() as temporary:
            mapping_path = Path(temporary) / "mapping.json"
            plan_path = Path(temporary) / "plan.json"
            mapping_path.write_text(json.dumps(self.mapping))
            observed = extensions.run(self.instance, ["board", "init", "1"])
            self.assertEqual(observed[0]["project"], "1")
            plan = extensions.run(self.instance, ["board", "init", "1", "--mapping", str(mapping_path)])
            plan_path.write_text(json.dumps(plan))
            self.assertEqual(extensions.run(self.instance, ["board", "init", "1", "--plan", str(plan_path)])["status"],
                             "preview")
            with self.assertRaisesRegex(TrackerError, "--plan"):
                extensions.run(self.instance, ["board", "init", "1", "--apply"])
            with self.assertRaises(TrackerError):
                extensions.run(self.instance, ["board", "init", "1", "--mapping", str(mapping_path), "--apply"])
            with self.assertRaises(TrackerError):
                extensions.run(self.instance, ["board", "init", "1", "--mapping", str(mapping_path), "--recursive"])
            self.assertEqual(self.client.writes, [])
            self.assertFalse(any("/tasks" in path for path in self.client.reads))
            self.assertTrue(extensions.run(self.instance,
                                          ["board", "init", "1", "--plan", str(plan_path), "--apply"])["applied"])

    def test_duplicate_json_keys_are_not_silently_reinterpreted(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "mapping.json"
            path.write_text('{"Backlog":100,"Backlog":101}')
            with self.assertRaisesRegex(TrackerError, "repeats key"):
                plans.run(self.instance, ["1"], mapping=path)
        self.assertEqual(self.client.writes, [])

    def test_explicit_sample_reads_one_filtered_page_without_relations_or_comments(self):
        """Only an explicit sample may load bodies, and pagination must not read the whole board."""
        for ident in range(6, 15):
            self.client.tasks[ident] = task(ident, 1, 10, 102, description="Sample", comments=["Private journal"])
        result = extensions.run(self.instance, ["board", "sample", "1", "--bucket", "102", "--limit", "2"])
        self.assertEqual(len(result["tasks"]), 2)
        self.assertNotIn("Private journal", json.dumps(result))
        task_reads = [path for path in self.client.reads if "/tasks" in path]
        self.assertEqual(len(task_reads), 1)
        self.assertEqual(urlsplit(task_reads[0]).path, "/projects/1/views/10/tasks")
        self.assertEqual(parse_qs(urlsplit(task_reads[0]).query),
                         {"filter": ["bucket_id = 102"], "page": ["1"], "per_page": ["2"], "format": ["markdown"]})
        for bucket, limit in ((999, 3), (102, 0), (102, 11), (102, True)):
            with self.assertRaises(TrackerError):
                plans.sample_bucket(self.instance, "1", bucket, limit)
        self.assertEqual(self.client.writes, [])

    def test_an_empty_manual_board_can_allocate_the_whole_agreed_lifecycle(self):
        instance, client = adapter(boards={1: board(10, 100, titles=())})
        plan = plans.build(instance, "1", {state: None for state in STATES})
        self.assertTrue(plans.execute(instance, "1", plan, True)["applied"])
        self.assertEqual([row["title"] for row in client.boards[1]["buckets"][10]], list(STATES))
