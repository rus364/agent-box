"""Package updates reconcile once; old sessions and failed applications cannot mark success."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_box import cli, config, migrations, runtime, upgrades
from agent_box.errors import AgentBoxError


class AutomaticUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.package = self.root / "package"
        self.package.mkdir()
        (self.package / "__init__.py").write_text('"""release one"""\n')
        self.file = self.root / "config.json"
        self.marker = self.root / "applied-package.json"
        for obj, name, value in ((config, "CONFIG_FILE", self.file),
                                 (upgrades, "APPLIED_PACKAGE", self.marker),
                                 (upgrades, "PACKAGE_APPLY_LOCK", self.root / "apply.lock"),
                                 (upgrades, "PACKAGE_ROOT", self.package),
                                 (migrations, "MIGRATION_STATE", self.root / "backups")):
            patcher = patch.object(obj, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.real_in_session = upgrades.in_session
        patcher = patch.object(upgrades, "in_session", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(runtime, "apply_config", return_value=["applied fixture"])
        self.apply = patcher.start()
        self.addCleanup(patcher.stop)
        config.save_config(config.Config())

    def test_changed_package_applies_once_and_keeps_the_saved_config(self):
        """Integration-only releases need reconciliation even without a schema bump."""
        original = self.file.read_bytes()
        self.assertTrue(upgrades.reconcile())
        first_marker = self.marker.read_bytes()
        self.assertEqual(upgrades.reconcile(), [])
        self.assertEqual(self.apply.call_count, 1)
        (self.package / "__init__.py").write_text('"""release two"""\n')
        self.assertTrue(upgrades.reconcile())
        self.assertEqual(self.apply.call_count, 2)
        self.assertNotEqual(self.marker.read_bytes(), first_marker)
        self.assertEqual(self.file.read_bytes(), original)

    def test_failed_apply_retries_without_advancing_the_success_marker(self):
        """A partial integration write must not make the next launch skip recovery."""
        upgrades.reconcile()
        original = self.marker.read_bytes()
        (self.package / "__init__.py").write_text('"""release two"""\n')
        self.apply.side_effect = AgentBoxError("retryable apply failure")
        with self.assertRaises(AgentBoxError):
            upgrades.reconcile()
        self.assertEqual(self.marker.read_bytes(), original)
        self.apply.side_effect = None
        self.assertTrue(upgrades.reconcile())
        self.assertNotEqual(self.marker.read_bytes(), original)

    def test_concurrent_first_launches_apply_only_once(self):
        """The lock covers migration, integration writes and the success marker together."""
        def apply(saved):
            time.sleep(0.02)
            return []
        self.apply.side_effect = apply
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: upgrades.reconcile(), range(2)))
        self.assertEqual(self.apply.call_count, 1)
        self.assertEqual(sum(bool(value) for value in results), 1)

    def test_no_saved_host_config_or_a_rendered_session_does_not_apply(self):
        """Missing mounted settings are not defaults that can erase host integrations."""
        with patch.object(upgrades, "in_session", return_value=True):
            self.assertEqual(upgrades.reconcile(), [])
        self.file.unlink()
        self.assertEqual(upgrades.reconcile(), [])
        self.apply.assert_not_called()
        self.assertFalse(self.marker.exists())

    def test_session_detection_covers_host_sessions_with_a_visible_saved_file(self):
        """An older --host session must not downgrade freshly applied integrations."""
        with patch.dict(os.environ, {"AGENT_BOX_RUNTIME_CONFIG": "/fixture/snapshot"}):
            self.assertTrue(self.real_in_session())
        self.assertTrue(self.file.exists())

    def test_future_config_refuses_application_and_preserves_files(self):
        """Neither credentials nor an applied marker may be written from unknown settings."""
        self.file.write_text('{"version":999,"valuable":"keep"}')
        original = self.file.read_bytes()
        with self.assertRaises(AgentBoxError):
            upgrades.reconcile()
        self.apply.assert_not_called()
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.file.read_bytes(), original)

    def test_explicit_apply_reconciles_manual_changes_without_a_package_change(self):
        """Manual config edits keep the explicit apply escape hatch."""
        upgrades.reconcile()
        self.file.write_text(json.dumps({"version": 1, "agents": {"default": "codex"}}))
        self.assertEqual(upgrades.reconcile(), [])
        upgrades.reconcile(force=True)
        self.assertEqual(self.apply.call_args.args[0].agents.default, "codex")
        self.assertEqual(self.apply.call_count, 2)

    def test_uninstall_invalidation_allows_same_package_to_apply_again(self):
        """An installed-package digest cannot claim integrations removed by uninstall."""
        upgrades.reconcile()
        upgrades.forget()
        upgrades.reconcile()
        self.assertEqual(self.apply.call_count, 2)


class LaunchBoundaryTests(unittest.TestCase):
    def test_read_commands_do_not_apply_and_host_box_external_launches_do(self):
        """Only actual launch dispatches enter the post-upgrade boundary."""
        for args, expected in ((["doctor"], False), (["help"], False),
                               (["claude"], True), (["--host", "codex"], True),
                               (["ext", "fixture", "claude"], True),
                               (["--host", "ext", "fixture", "codex"], True)):
            with self.subTest(args=args), \
                    patch("sys.argv", ["agent-box", *args]), \
                    patch("agent_box.project_config.load", return_value={}), \
                    patch.object(upgrades, "before_launch") as apply, \
                    patch.object(runtime, "runtime_main"), \
                    patch.object(runtime, "host_main", return_value=0), \
                    patch.object(cli, "usage_text"):
                try:
                    cli.main()
                except SystemExit as exc:
                    self.assertEqual(exc.code, 0)
                self.assertEqual(apply.call_count, int(expected))
