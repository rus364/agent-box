"""Temporary texts preserve bytes without turning paging into an unlimited context feed."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_box import config, project_config, runtime, text_delivery, workflow_state
from agent_box.errors import AgentBoxError


def delivered_bytes(artifact):
    """Follow the same page chain a reader sees, including a final unterminated line."""
    path = Path(artifact["path"])
    result = b""
    for _ in range(artifact["pages"]):
        page = path.read_bytes()
        header, body = page.split(b"--- TEXT ---\n", 1)
        match = re.search(rb"bytes (\d+)\.\.(\d+) of (\d+)", header)
        start, end, total = map(int, match.groups())
        assert start == len(result) and total == artifact["bytes"]
        part = body[:end - start]
        part.decode("utf-8")
        result += part
        footer = body[end - start:].decode()
        assert len(page) <= text_delivery.PAGE_BYTES + text_delivery.METADATA_BYTES
        if "\nNext: read sibling " in footer:
            path = path.parent / json.loads(footer.split("\nNext: read sibling ", 1)[1])
        else:
            assert f"END OF TEXT {artifact['id']}: {len(result)} bytes" in footer
            break
    else:
        raise AssertionError("Page chain has no final marker")
    assert len(result) == artifact["bytes"]
    return result


class TextStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = self.root / "state/texts"
        self.config = config.Config()
        self.config.workflow.enabled = True
        self.config._session_container = False
        for target, name, value in ((text_delivery, "WORKFLOW_TEXTS", self.store),
                                     (workflow_state, "WORKFLOW_STATE", self.store.parent)):
            item = patch.object(target, name, value)
            item.start()
            self.addCleanup(item.stop)

    def test_pages_preserve_empty_unicode_crlf_and_unterminated_long_lines(self):
        """Paging must preserve every source byte, including content that line-count reads lose."""
        cases = ("", "last line", "a\r\nb\r\nlast", "x" * 16000,
                 ('Текст 日本語 🙂 \\ "quoted" `code` $()\r\n' * 400) + "FINAL")
        for text in cases:
            for page_bytes in (256, 4096):
                with self.subTest(size=len(text), page_bytes=page_bytes):
                    artifact = text_delivery.publish(text, self.config, page_bytes=page_bytes)
                    self.assertEqual(delivered_bytes(artifact), text.encode())
                    self.assertEqual(text_delivery.locate(artifact["id"], self.config), artifact)

    def test_total_limit_applies_before_publication_and_does_not_reset_per_page(self):
        """Many individually small pages may still constitute one refused assignment."""
        self.config.workflow.text_max_bytes = 1024
        for size in (1023, 1024):
            artifact = text_delivery.publish("a" * size, self.config, page_bytes=256)
            self.assertEqual(len(delivered_bytes(artifact)), size)
        before = set(self.store.iterdir())
        with self.assertRaisesRegex(AgentBoxError, "TEXT_LIMIT_EXCEEDED.*1025.*1024.*tell the user"):
            text_delivery.publish("a" * 1025, self.config, page_bytes=256)
        self.assertEqual(set(self.store.iterdir()), before)
        with self.assertRaisesRegex(AgentBoxError, "TEXT_LIMIT_EXCEEDED"):
            text_delivery.publish("🙂" * 257, self.config)

    def test_locator_reads_are_repeatable_and_need_no_write_permission_or_cleanup(self):
        """A read-only delegate must be able to retry a tool read without destroying the source."""
        artifact = text_delivery.publish("repeatable", self.config)
        directory = Path(artifact["path"]).parent
        before = {p: p.stat().st_mtime_ns for p in directory.iterdir()}
        directory.chmod(0o500)
        self.addCleanup(directory.chmod, 0o700)
        with patch.object(text_delivery, "lock") as lock, patch.object(text_delivery, "cleanup") as cleanup:
            for _ in range(2):
                self.assertEqual(text_delivery.locate(artifact["id"], self.config), artifact)
                self.assertEqual(delivered_bytes(artifact), b"repeatable")
        lock.assert_not_called()
        cleanup.assert_not_called()
        self.assertEqual({p: p.stat().st_mtime_ns for p in directory.iterdir()}, before)

    def test_lower_limit_refuses_an_older_record_before_reading_its_pages(self):
        """A newly issued locator must not keep a larger budget from an older session."""
        artifact = text_delivery.publish("a" * 2048, self.config)
        self.config.workflow.text_max_bytes = 1024
        with patch.object(text_delivery, "page_files") as pages, self.assertRaisesRegex(AgentBoxError, "TEXT_LIMIT_EXCEEDED"):
            text_delivery.locate(artifact["id"], self.config)
        pages.assert_not_called()

    def test_expiry_is_fixed_at_creation_and_cleanup_preserves_unrelated_content(self):
        """Another session's retention choice cannot reinterpret existing records or prune by count."""
        with patch.object(text_delivery.time, "time", return_value=100):
            old = text_delivery.publish("old", self.config)
            self.config.workflow.text_retention_days = 30
            newer = text_delivery.publish("new", self.config)
        outside = self.store.parent / "review-inputs/receipt.txt"
        outside.parent.mkdir()
        outside.write_text("preserve")
        unrelated = self.store / "user.txt"
        unrelated.write_text("preserve")
        when = 100 + 8 * 86400
        with patch.object(text_delivery.time, "time", return_value=when):
            with self.assertRaisesRegex(AgentBoxError, "expired"):
                text_delivery.locate(old["id"], self.config)
            self.assertTrue(Path(old["path"]).exists())
            text_delivery.publish("cleanup trigger", self.config)
            self.assertEqual(delivered_bytes(text_delivery.locate(newer["id"], self.config)), b"new")
        self.assertFalse(Path(old["path"]).exists())
        self.assertEqual(outside.read_text(), "preserve")
        self.assertEqual(unrelated.read_text(), "preserve")

    def test_future_metadata_unknown_files_and_symlinks_are_never_cleanup_targets(self):
        """Ownership uncertainty must preserve files even when their names resemble old records."""
        artifacts = [text_delivery.publish(str(n), self.config) for n in range(3)]
        future, unknown, linked = [Path(a["path"]).parent for a in artifacts]
        path = future / "record.json"
        data = json.loads(path.read_text()); data["format_version"] = 99
        path.chmod(0o600); path.write_text(json.dumps(data))
        (unknown / "user-owned").write_text("preserve")
        page = Path(artifacts[2]["path"]); page.unlink(); page.symlink_to(unknown / "user-owned")
        with self.assertRaisesRegex(AgentBoxError, "Unsupported.*format"):
            text_delivery.locate(artifacts[0]["id"], self.config)
        text_delivery.cleanup(now=max(a["expires_at"] for a in artifacts) + 1)
        self.assertTrue(all(p.exists() for p in (future, unknown, linked)))
        self.assertEqual((unknown / "user-owned").read_text(), "preserve")

    def test_missing_invalid_ids_and_damaged_pages_fail_without_returning_a_prefix(self):
        """A valid first page cannot conceal a missing or modified remainder."""
        for ident in ("../outside", "", "A" * 16, "a" * 16):
            with self.assertRaises(AgentBoxError):
                text_delivery.locate(ident, self.config)
        artifact = text_delivery.publish("x" * 8192, self.config)
        last = Path(artifact["path"]).with_name("page-000002.txt")
        last.chmod(0o600); last.write_text("shortened")
        with self.assertRaisesRegex(AgentBoxError, "changed or incomplete pages"):
            text_delivery.locate(artifact["id"], self.config)
        last.unlink()
        with self.assertRaisesRegex(AgentBoxError, "missing or invalid pages"):
            text_delivery.locate(artifact["id"], self.config)

    def test_concurrent_publishers_and_id_collision_preserve_every_original(self):
        """Independent writers must never reuse a readable ID or lose an earlier unexpired text."""
        first = text_delivery.publish("first", self.config)
        with patch.object(text_delivery.secrets, "token_hex", side_effect=[first["id"], "f" * 16]):
            second = text_delivery.publish("second", self.config)
        self.assertEqual(delivered_bytes(first), b"first")
        self.assertNotEqual(first["id"], second["id"])
        with patch.object(text_delivery.time, "time", return_value=0):
            expired = text_delivery.publish("expired", self.config)
        def concurrent(n):
            artifact = text_delivery.publish(str(n), self.config)
            self.assertEqual(text_delivery.locate(first["id"], self.config), first)
            text_delivery.prepare(self.config)
            return artifact
        with ThreadPoolExecutor(max_workers=4) as pool:
            artifacts = list(pool.map(concurrent, range(12)))
        self.assertFalse(Path(expired["path"]).exists())
        self.assertEqual(len({a["id"] for a in artifacts}), 12)
        for n, artifact in enumerate(artifacts):
            self.assertEqual(delivered_bytes(text_delivery.locate(artifact["id"], self.config)), str(n).encode())

    def test_failed_write_publishes_nothing_and_can_be_retried(self):
        """An atomic directory rename prevents readers seeing a half-written page chain."""
        write = text_delivery.atomic_write_bytes
        calls = []
        def fail(path, data, mode):
            calls.append(path)
            if len(calls) == 2:
                raise OSError("fixture disk full")
            return write(path, data, mode)
        with patch.object(text_delivery, "atomic_write_bytes", side_effect=fail), self.assertRaisesRegex(OSError, "disk full"):
            text_delivery.publish("source", self.config)
        self.assertEqual(list(self.store.iterdir()), [])
        self.assertEqual(delivered_bytes(text_delivery.publish("retry", self.config)), b"retry")

    def test_disabled_or_unmounted_container_never_creates_private_fallback_storage(self):
        """A locator in a container-only directory would break host/native delivery."""
        self.config.workflow.enabled = False
        with self.assertRaisesRegex(AgentBoxError, "requires"):
            text_delivery.publish("source", self.config)
        self.config.workflow.enabled = True
        self.config._session_container = True
        with patch.object(Path, "read_text", return_value=""), self.assertRaisesRegex(AgentBoxError, "not mounted"):
            text_delivery.publish("source", self.config)
        self.assertFalse(self.store.exists())

    def test_shared_records_remain_readable_under_a_different_mount_destination(self):
        """Absolute next-page paths would bind shared contents to the publisher's namespace."""
        artifact = text_delivery.publish("跨环境" * 3000, self.config)
        mounted = self.root / "container-mount"
        mounted.symlink_to(self.store.parent, target_is_directory=True)
        with patch.object(text_delivery, "WORKFLOW_TEXTS", mounted / "texts"):
            located = text_delivery.locate(artifact["id"], self.config)
            self.assertNotEqual(located["path"], artifact["path"])
            self.assertEqual(located["sha256"], artifact["sha256"])
            self.assertEqual(delivered_bytes(located), delivered_bytes(artifact))

    def cli(self, *args, data=None):
        snapshot = self.root / "session.json"
        snapshot.write_text(json.dumps({**self.config.to_dict(), "container": False}))
        env = {**os.environ, "AGENT_BOX_RUNTIME_CONFIG": str(snapshot), "AGENT_BOX_WORKFLOW_STATE": str(self.store.parent)}
        return subprocess.run([sys.executable, "-m", "agent_box", "text", *args], input=data, capture_output=True,
                              cwd=self.root, env=env, timeout=15)

    def test_cli_uses_stdin_and_ids_with_actionable_overflow_and_utf8_errors(self):
        """Exercise the public parser, configuration and output instead of just the store helper."""
        raw = ('日本語\r\n' * 100 + 'final').encode()
        result = self.cli("write", data=raw)
        self.assertEqual(result.returncode, 0, result.stderr)
        artifact = json.loads(result.stdout)
        self.assertEqual(delivered_bytes(artifact), raw)
        lookup = self.cli("locate", artifact["id"])
        self.assertEqual(lookup.returncode, 0, lookup.stdout)
        self.assertEqual(json.loads(lookup.stdout), artifact)
        self.config.workflow.text_max_bytes = 32
        refused = self.cli("write", data=b"x" * 100)
        self.assertEqual(refused.returncode, 2)
        self.assertIn("at least 33", json.loads(refused.stdout)["error"])
        self.assertEqual(self.cli("write", data=b"\xff").returncode, 2)
        self.assertEqual(self.cli("write", str(self.root / "user-path"), data=b"x").returncode, 2)


class TextSettingsTests(unittest.TestCase):
    def test_defaults_explicit_values_and_host_container_snapshots_keep_the_global_limits(self):
        """A project may choose behavior, but cannot expand shared storage budgets or retention."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for container in (False, True):
                settings = config.Config()
                self.assertEqual((settings.workflow.text_max_bytes, settings.workflow.text_retention_days), (262144, 7))
                settings.workflow.enabled = True
                settings.workflow.text_max_bytes = 32768
                settings.workflow.text_retention_days = 14
                runtime.sync_tracker_runtime(settings, root, container=container)
                with patch.dict(os.environ, {"AGENT_BOX_RUNTIME_CONFIG": str(root / "config.json")}), \
                        patch.object(project_config, "load", return_value={}):
                    loaded = config.effective_config()
                self.assertEqual((loaded.workflow.text_max_bytes, loaded.workflow.text_retention_days), (32768, 14))
                self.assertEqual(loaded._project_base._session_container, container)
        for name in ("text_max_bytes", "text_retention_days"):
            with self.assertRaises(AgentBoxError):
                project_config.validate({"workflow": {name: 99}}, Path(".agent-box.json"))
            for value in (0, -1, True, None, "7", 1.5):
                loaded = config.Config.from_dict({"workflow": {name: value}})
                self.assertIn(f"workflow.{name} must be a positive integer", loaded.workflow.validate())

    def test_direct_upgrades_adopt_defaults_without_rewriting_published_fixtures(self):
        """No migration is invented for additive settings or the unpublished text layout."""
        fixtures = Path(__file__).parent / "fixtures/compatibility"
        for path in (fixtures / "1.0.0/config.json", fixtures / "1.1.0/browser-config.json",
                     fixtures / "2.0.0/permission-config.json"):
            before = path.read_bytes()
            loaded = config.load_config(path)
            self.assertEqual((loaded.workflow.text_max_bytes, loaded.workflow.text_retention_days), (262144, 7))
            self.assertEqual(path.read_bytes(), before)
