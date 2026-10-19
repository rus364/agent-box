"""Skipped releases, failed writes and future data must not destroy saved choices."""
import json
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from agent_box import config, migrations, project_config
from agent_box.errors import AgentBoxError


def move_flag(data):
    data["enabled"] = data.pop("old_flag")
    return data


def nest_flag(data):
    data["feature"] = {"enabled": data.pop("enabled")}
    return data


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "settings.json"
        self.state = self.root / "migration-state"
        patcher = patch.object(migrations, "MIGRATION_STATE", self.state)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.schema = migrations.Schema("example", 3, {1: move_flag, 2: nest_flag})

    def test_skipped_releases_convert_a_copy_and_keep_false_and_empty_values(self):
        """One current package carries every step; reading cannot mutate the source."""
        old = {"old_flag": False, "items": [], "text": ""}
        self.assertEqual(self.schema.prepare(old), {"feature": {"enabled": False},
                         "items": [], "text": "", "format_version": 3})
        self.assertEqual(old, {"old_flag": False, "items": [], "text": ""})

    def test_explicit_upgrade_retains_exact_private_original_and_retry_is_a_noop(self):
        """Formatting and secrets in the original remain recoverable after commit."""
        original = b'{ "old_flag": false, "secret": "fixture-secret" }\n'
        self.path.write_bytes(original)
        self.assertTrue(migrations.upgrade(self.path, self.schema))
        backups = list(self.state.rglob("schema-*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
        written = self.path.read_bytes()
        self.assertFalse(migrations.upgrade(self.path, self.schema))
        self.assertEqual(self.path.read_bytes(), written)
        self.assertEqual(list(self.state.rglob("schema-*.json")), backups)

    def test_conversion_or_validation_failure_never_replaces_the_original(self):
        """A failed converter/validator cannot leave a partially advanced document."""
        self.path.write_text('{"old_flag": true}')
        original = self.path.read_bytes()
        def broken(data):
            data.clear()
            raise ValueError("fixture-secret")
        for schema, validate in ((migrations.Schema("example", 2, {1: broken}), None),
                                 (self.schema, lambda data: broken(data)),
                                 (migrations.Schema("example", 3, {1: move_flag}), None)):
            with self.subTest(schema=schema), self.assertRaises((AgentBoxError, ValueError)):
                migrations.upgrade(self.path, schema, validate=validate)
            self.assertEqual(self.path.read_bytes(), original)
        with self.assertRaisesRegex(AgentBoxError, "Cannot migrate") as caught:
            migrations.Schema("example", 2, {1: broken}).prepare({})
        self.assertNotIn("fixture-secret", str(caught.exception))

    def test_failed_final_write_keeps_source_and_retry_reuses_backup(self):
        """Interruption after backup is safe: the same migration can simply run again."""
        original = b'{"old_flag":true}'
        self.path.write_bytes(original)
        with patch.object(migrations, "atomic_write_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                migrations.upgrade(self.path, self.schema)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertTrue(migrations.upgrade(self.path, self.schema))
        self.assertEqual(len(list(self.state.rglob("schema-*.json"))), 1)

    def test_loading_then_saving_does_not_migrate_the_current_payload_twice(self):
        """Consumer APIs omit envelope metadata; writers still know their current format."""
        self.path.write_text('{"old_flag":false}')
        payload = migrations.read(self.path, self.schema)
        migrations.write(self.path, payload, self.schema)
        self.assertEqual(migrations.read(self.path, self.schema), payload)
        self.assertEqual(json.loads(self.path.read_text())["format_version"], 3)

    def test_future_or_malformed_versions_are_refused_before_read_and_overwrite(self):
        """An older binary must not interpret a future schema as empty defaults."""
        for version in (4, True, 0, -1, "1", 1.0, None):
            with self.subTest(version=version):
                self.path.write_text(json.dumps({"format_version": version}))
                original = self.path.read_bytes()
                with self.assertRaises(AgentBoxError):
                    migrations.read(self.path, self.schema)
                with self.assertRaises(AgentBoxError):
                    migrations.write(self.path, {"format_version": 3}, self.schema)
                self.assertEqual(self.path.read_bytes(), original)

    def test_an_external_edit_between_read_and_commit_is_not_overwritten(self):
        """Advisory locks cannot stop editors, so compare the source before commit."""
        self.path.write_text('{"old_flag":true}')
        original = self.path.read_bytes()
        self.path.write_text('{"old_flag":false}')
        with self.assertRaisesRegex(AgentBoxError, "changed"):
            migrations.write(self.path, json.loads(original), self.schema, expected=original)
        self.assertEqual(json.loads(self.path.read_text()), {"old_flag": False})

    def test_global_and_repository_configs_refuse_future_files_even_on_save(self):
        """Normal configuration entry points must use the format guard, not only the runner."""
        for load, save in ((config.load_config, lambda p: config.save_config(config.Config(), p)),
                           (project_config.load, lambda p: project_config.save(p, {}))):
            with self.subTest(load=load):
                self.path.write_text('{"version":999,"valuable":"preserve"}')
                original = self.path.read_bytes()
                with self.assertRaises(AgentBoxError):
                    load(self.path)
                with self.assertRaises(AgentBoxError):
                    save(self.path)
                self.assertEqual(self.path.read_bytes(), original)

    def test_global_config_migrates_before_parsing_and_only_persists_when_applied(self):
        """Renamed known fields must not disappear through Config.from_dict first."""
        def step(data):
            data["agents"] = {"default": data.pop("chosen_agent")}
            return data
        schema = migrations.Schema("global configuration", 2, {1: step}, key="version")
        self.path.write_text('{"version":1,"chosen_agent":"codex"}')
        original = self.path.read_bytes()
        with patch.object(migrations, "GLOBAL_CONFIG", schema):
            self.assertEqual(config.load_config(self.path).agents.default, "codex")
            self.assertEqual(self.path.read_bytes(), original)
            self.assertTrue(config.migrate_config(self.path))
        self.assertEqual(json.loads(self.path.read_text())["version"], 2)

    def test_public_baseline_fixtures_preserve_user_choices_and_project_inheritance(self):
        """Published examples remain loadable when future release schemas evolve."""
        fixtures = Path(__file__).parent / "fixtures" / "compatibility" / "1.0.0"
        saved = config.load_config(fixtures / "config.json")
        self.assertEqual(saved.agents.default, "codex")
        self.assertFalse(saved.box.tools.groups["analysis"])
        self.assertFalse(saved.credentials.github.token)
        self.assertEqual(saved.box.network, config.NetworkConfig())
        self.assertEqual(saved.box.k3s.ports, [])
        project = project_config.load(fixtures / "project.json")
        merged = project_config.apply(saved, project)
        self.assertFalse(merged.workflow.auto_push)
        self.assertEqual(merged.workflow.review.ignored_paths, [])
        self.assertEqual(merged.agents.default, "codex")

    def test_project_load_edit_save_converts_only_the_stored_old_format(self):
        """Proposed current overrides must not run an old field rename a second time."""
        def step(data):
            data["workflow"] = {"auto_push": data.pop("push")}
            return data
        schema = migrations.Schema("project configuration", 2, {1: step}, key="version")
        self.path.write_text('{"push":false}')
        with patch.object(migrations, "PROJECT_CONFIG", schema):
            data = project_config.load(self.path)
            project_config.validate(data, self.path)
            project_config.save(self.path, data)
            self.assertEqual(project_config.load(self.path), {"workflow": {"auto_push": False}})
            from agent_box import repo_binding
            self.path.rename(self.root / repo_binding.BINDING_FILE)
            binding = repo_binding.Binding("github", "https://github.com", "owner/2")
            bound = repo_binding.binding_document(self.root, binding)
            self.assertEqual(bound["workflow"], {"auto_push": False})
            self.assertEqual(bound["version"], 2)
            self.path = self.root / repo_binding.BINDING_FILE
        self.assertEqual(json.loads(self.path.read_text())["version"], 2)

    def test_durable_recovery_readers_refuse_future_state_instead_of_starting_over(self):
        """Discarding these records could duplicate external writes or reset review budgets."""
        from agent_box import plan_sync, workflow
        from agent_box.tracker import adoption
        from agent_box.tracker.vikunja_api import ImportState
        self.path.write_text('{"format_version":999,"used":4,"source_digest":"fixture"}')
        original = self.path.read_bytes()
        with patch.object(workflow, "evidence_path", return_value=self.path), \
                patch.object(workflow, "review_budget_path", return_value=self.path), \
                patch.object(plan_sync, "record_path", return_value=self.path):
            readers = (lambda: workflow.read_evidence(None, None),
                       lambda: workflow.read_review_state(None, None, None),
                       lambda: plan_sync.last_result(None, None),
                       lambda: adoption.load(self.path, self.root),
                       lambda: ImportState(self.path, "fixture"))
            for read in readers:
                with self.subTest(read=read), self.assertRaises(AgentBoxError):
                    read()
                self.assertEqual(self.path.read_bytes(), original)

    def test_future_workspace_policy_and_skill_markers_cannot_authorize_cleanup(self):
        """Unknown ownership/persistence semantics must retain both skill and volume data."""
        from agent_box import runtime
        (self.root / "box-fixture.json").write_text('{"format_version":999}')
        self.assertIsNone(runtime.last_box_config(SimpleNamespace(root=self.root), config.Config()))
        target = self.root / "skill"
        target.mkdir()
        (target / runtime.MANAGED_SKILL_MARKER).write_text('{"format_version":999,"digest":"x"}')
        content = target / "SKILL.md"
        content.write_text("user content")
        with self.assertRaises(AgentBoxError):
            runtime.managed_skill_digest(target)
        self.assertEqual(content.read_text(), "user content")

    def test_new_owned_markers_and_receipts_do_not_run_historical_converters(self):
        """Fresh producers stamp their current format even after its schema advances."""
        from agent_box import runtime, review_results
        def old_only(data):
            raise AssertionError("a new document is not historical data")
        schema = migrations.Schema("fixture", 2, {1: old_only})
        shipped = self.root / "shipped/fixture"
        shipped.mkdir(parents=True)
        (shipped / "SKILL.md").write_text("# fixture\n")
        installed = self.root / "installed"
        with patch.object(migrations, "SKILL_MARKER", schema), \
                patch.object(runtime, "MANAGED_SKILLS_SOURCE", shipped.parent), \
                patch.object(runtime, "AGENT_SKILLS_DIR", installed):
            runtime.install_managed_skill("fixture")
            self.assertTrue(runtime.managed_skill_digest(installed / "fixture"))
        with patch.object(migrations, "REVIEW_RECEIPT", schema):
            receipt = review_results.save(self.root / "state", self.root / "repo", "repository",
                {"descriptor": {"binding": "binding", "task": "task", "delegation": "round"}})
            value, _ = review_results.read(self.root / "state", "repository", receipt)
            self.assertEqual(value["format_version"], 2)

    def test_public_recovery_fixtures_keep_ids_budgets_and_opaque_fields(self):
        """Frozen durable payloads ensure migration cannot silently reset external history."""
        fixtures = Path(__file__).parent / "fixtures" / "compatibility" / "1.0.0"
        for name, schema in (("review-budget", migrations.REVIEW_BUDGET),
                             ("import-state", migrations.IMPORT_STATE)):
            path = fixtures / (name + ".json")
            original = json.loads(path.read_text())
            payload = migrations.read(path, schema)
            self.assertEqual(payload, {key: value for key, value in original.items() if key != "format_version"})
            migrations.write(self.path, payload, schema)
            self.assertEqual(migrations.read(self.path, schema), payload)


if __name__ == "__main__":
    unittest.main()
