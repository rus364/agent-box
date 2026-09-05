"""Container tracker networking preserves TLS names and rewrites loopback."""
import unittest
from unittest import mock
from agent_box import runtime

def _locality(local: bool):
    """Pin :func:`runtime.is_host_local` so the suite never consults DNS."""
    return mock.patch.object(runtime, "is_host_local", lambda host: local)


class ContainerUrlTests(unittest.TestCase):
    """Which addresses get rewritten, and which are left exactly alone."""

    def test_loopback_ip_becomes_the_container_host_alias(self):
        self.assertEqual(
            runtime.container_tracker_url("http://127.0.0.1:3456"),
            "http://host.containers.internal:3456")

    def test_localhost_becomes_the_container_host_alias(self):
        self.assertEqual(
            runtime.container_tracker_url("http://localhost:3456"),
            "http://host.containers.internal:3456")

    def test_ipv6_loopback_becomes_the_container_host_alias(self):
        self.assertEqual(
            runtime.container_tracker_url("http://[::1]:3456"),
            "http://host.containers.internal:3456")

    def test_scheme_port_and_path_survive_the_rewrite(self):
        self.assertEqual(
            runtime.container_tracker_url("https://127.0.0.1:8443/vikunja/api"),
            "https://host.containers.internal:8443/vikunja/api")

    def test_an_absent_port_stays_absent(self):
        self.assertEqual(
            runtime.container_tracker_url("http://localhost"),
            "http://host.containers.internal")

    def test_a_host_local_ip_literal_is_rewritten(self):
        with _locality(True):
            self.assertEqual(
                runtime.container_tracker_url("http://192.168.1.203:3456"),
                "http://host.containers.internal:3456")

    def test_a_dns_name_keeps_its_url_even_when_it_is_this_machine(self):
        """TLS and virtual-host routing depend on the name surviving."""
        with _locality(True):
            self.assertEqual(
                runtime.container_tracker_url("https://pm.example.org/api"),
                "https://pm.example.org/api")

    def test_a_remote_server_is_returned_unchanged(self):
        with _locality(False):
            for url in ("https://tasks.example.com", "http://10.1.2.3:3456"):
                with self.subTest(url=url):
                    self.assertEqual(runtime.container_tracker_url(url), url)

    def test_a_malformed_authority_is_handed_through(self):
        self.assertEqual(
            runtime.container_tracker_url("http://127.0.0.1:not-a-port"),
            "http://127.0.0.1:not-a-port")

    def test_an_empty_url_stays_empty(self):
        self.assertEqual(runtime.container_tracker_url("   "), "")

class HostAliasTests(unittest.TestCase):
    def test_a_host_local_dns_name_is_mapped(self):
        with _locality(True):
            self.assertEqual(
                runtime.tracker_host_alias("http://pm.example.org:3456"), "pm.example.org")

    def test_a_remote_dns_name_is_not_mapped(self):
        with _locality(False):
            self.assertEqual(runtime.tracker_host_alias("https://tasks.example.com"), "")

    def test_addresses_and_loopback_names_are_not_mapped(self):
        """Those are the rewrite's job, not the host mapping's."""
        with _locality(True):
            for url in ("http://127.0.0.1:3456", "http://localhost", "http://[::1]:3456"):
                with self.subTest(url=url):
                    self.assertEqual(runtime.tracker_host_alias(url), "")

    def test_unusable_input_maps_nothing(self):
        self.assertEqual(runtime.tracker_host_alias("   "), "")
        self.assertEqual(runtime.tracker_host_alias("http://pm.example.org:not-a-port"), "")


class TrackerExposureTests(unittest.TestCase):
    def test_rendered_artifact_is_private_and_disabled_tracker_reveals_no_connections(self):
        import json
        import tempfile
        from pathlib import Path
        from agent_box.config import Config, TrackerConnection
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "runtime"
            config = Config()
            config.workflow.enabled = True
            config.tracker.connections["vikunja"] = TrackerConnection("http://localhost:3456", "secret")
            runtime.sync_tracker_runtime(config, target)
            rendered = json.loads((target / "config.json").read_text())
            self.assertEqual(rendered["tracker"]["connections"], {})
            self.assertNotIn("secret", (target / "config.json").read_text())
            config.tracker.enabled = True
            runtime.sync_tracker_runtime(config, target)
            rendered = json.loads((target / "config.json").read_text())
            self.assertEqual(rendered["tracker_urls"]["vikunja"], "http://host.containers.internal:3456")
            self.assertEqual((target / "config.json").stat().st_mode & 0o777, 0o600)
            self.assertEqual(target.stat().st_mode & 0o777, 0o700)
            self.assertNotIn("notifications", rendered)
            self.assertNotIn("env", rendered)
