"""Network choices must not silently widen access or change a neighbour's box."""
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent_box import config_menu, launch_options, networking, project_config, runtime
from agent_box.config import Config, NetworkConfig, apply_box_policy, effective_config
from agent_box.errors import AgentBoxError


class NetworkingTests(unittest.TestCase):
    def test_old_publications_preserve_loopback_tcp_and_ipv6_udp_is_explicit(self):
        """Extending syntax must never reinterpret existing saved host ports as LAN binds."""
        self.assertEqual(networking.parse_port("8080:80").publish(), "127.0.0.1:8080:80")
        self.assertEqual(networking.parse_port("[::1]:5353:53/udp").publish(), "[::1]:5353:53/udp")
        for value in ("0:80", "65536:80", "80:0", "80:80/sctp", "localhost:80:80", "[127.0.0.1]:80:80",
                      "8080-8082:80-82", "[fe80::1%eth0]:80:80"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                networking.parse_port(value)

    def test_projects_can_publish_locally_but_cannot_authorize_host_or_lan_access(self):
        """Both edits and untrusted checked-in settings use this same project validator."""
        for section in ("network", "k3s"):
            project_config.validate({"box": {section: {"ports": ["8080:80", "[::1]:5353:53/udp"]}}}, Path("project.json"))
            with self.assertRaisesRegex(AgentBoxError, "global/CLI only"):
                project_config.validate({"box": {section: {"ports": ["0.0.0.0:8080:80"]}}}, Path("project.json"))
        for field, value in (("mode", "host"), ("host_ports", ["5432:15432"])):
            with self.assertRaisesRegex(AgentBoxError, "not a project field"):
                project_config.validate({"box": {"network": {field: value}}}, Path("project.json"))

    def test_publication_conflicts_account_for_protocol_and_wildcard_overlap(self):
        """Two targets cannot silently compete for one host endpoint; TCP and UDP can coexist."""
        config = Config()
        config.box.k3s.enabled = True
        config.box.k3s.ports = ["8080:80"]
        config.box.network.ports = ["8080:3000/udp"]
        self.assertFalse(config.box.validate())
        for value in ("8080:3000", "0.0.0.0:8080:3000", "[::]:8080:3000"):
            config.box.network.ports = [value]
            self.assertIn("overlap", " ".join(config.box.validate()))
        with self.assertRaises(ValueError):
            networking.ports(["8080:80", "127.0.0.1:8080:81/tcp"])

    def test_cli_replaces_lists_and_clear_flags_cannot_hide_set_flags(self):
        """One-launch requests replace defaults instead of accumulating unexpected exposure."""
        options, rest = launch_options.parse(["--publish", "8080:80", "--publish=5353:53/udp",
                                             "--host-port", "5432:15432", "codex"])
        saved = NetworkConfig(ports=["9999:99"])
        chosen = runtime.requested_network(saved, options)
        self.assertEqual(chosen.ports, ["8080:80", "5353:53/udp"])
        self.assertEqual(chosen.host_ports, ["5432:15432"])
        self.assertEqual(saved.ports, ["9999:99"])
        self.assertEqual(rest, ["codex"])
        self.assertFalse(options.overrides)
        for args in (["--no-publish", "--publish=80:80"], ["--publish=80:80", "--no-publish"],
                     ["--host-port=80:80", "--no-host-ports"], ["--no-host-ports", "--host-port=80:80"]):
            with self.subTest(args=args), self.assertRaises(AgentBoxError):
                launch_options.parse(args)
        cleared, _ = launch_options.parse(["--no-publish", "--no-host-ports", "--network=default"])
        self.assertEqual(runtime.requested_network(NetworkConfig("host"), cleared), NetworkConfig())
        self.assertTrue(all(option.container for option in options.given()))

    def test_k3s_and_selected_forwards_use_one_network_with_correct_direction(self):
        """The API forward must coexist with user TCP/UDP forwards, never replace them."""
        config = Config()
        config.box.network.host_ports = ["5432:15432", "5353:15353/udp", "6379:16379"]
        args = runtime.network_arguments(config, 24443)
        self.assertEqual(args, ["--network=pasta:-T,24443,-T,15432:5432,-T,16379:6379,-U,15353:5353"])
        config.box.network.ports = ["8080:24443"]
        with self.assertRaisesRegex(AgentBoxError, "reserved"):
            runtime.network_arguments(config, 24443)
        config.box.network.ports = ["8080:15432"]
        self.assertIn("same workspace port", " ".join(config.box.validate()))

    def test_host_mode_keeps_sidecar_ports_but_refuses_workspace_mappings(self):
        """Host mode cannot silently discard mappings; k3s remains a separate network."""
        config = Config()
        config.box.network.mode = "host"
        config.box.k3s.enabled = True
        config.box.k3s.ports = ["8080:80"]
        self.assertEqual(runtime.network_arguments(config, 24443), ["--network=host"])
        for attr in ("ports", "host_ports"):
            setattr(config.box.network, attr, ["8081:80"])
            with self.assertRaisesRegex(AgentBoxError, "clear.*--no-publish"):
                runtime.network_arguments(config, 24443)
            setattr(config.box.network, attr, [])

    def test_host_forwarding_rejects_an_unsupported_backend_without_changing_it(self):
        """No invisible backend/network switch should follow a selected host listener."""
        config = Config()
        with mock.patch.object(runtime.nested_podman, "host_info", return_value={"rootlessNetworkCmd": "slirp4netns"}) as info:
            runtime.prepare_network(config)
            info.assert_not_called()
            config.box.network.host_ports = ["5432:15432"]
            with self.assertRaisesRegex(AgentBoxError, "requires.*pasta"):
                runtime.prepare_network(config)

    def test_actual_namespace_publications_and_forwards_must_match_policy(self):
        """Labels alone cannot attest that inspected networking has the requested boundary."""
        network = NetworkConfig(ports=["8080:80"], host_ports=["5432:15432"])
        current = {"HostConfig": {"NetworkMode": "pasta", "PortBindings": {
            "80/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8080"}]}},
                   "Config": {"CreateCommand": networking.arguments(network)}}
        self.assertIsNone(networking.inspection_problem(network, current))
        for change in ("mode", "port", "forward"):
            altered = copy.deepcopy(current)
            if change == "mode":
                altered["HostConfig"]["NetworkMode"] = "host"
            elif change == "port":
                altered["HostConfig"]["PortBindings"]["80/tcp"][0]["HostIp"] = "0.0.0.0"
            else:
                altered["Config"]["CreateCommand"] = []
            self.assertIsNotNone(networking.inspection_problem(network, altered))

    def test_legacy_policy_defaults_do_not_inherit_new_global_host_access(self):
        """Missing additive fields in published v1 policies mean the original safe defaults."""
        config = Config()
        config.box.network = NetworkConfig("host")
        apply_box_policy(config, {"format_version": 1, "workflow_mount": False, "box": {}})
        self.assertEqual(config.box.network, NetworkConfig())
        with self.assertRaisesRegex(AgentBoxError, "future|newer|schema"):
            apply_box_policy(config, {"format_version": 999, "workflow_mount": False})

    def test_session_snapshot_keeps_actual_cli_network_after_project_changes(self):
        """Config show in the box must describe its namespace, not newly edited future settings."""
        options, _ = launch_options.parse(["--publish=8080:80"])
        base = Config()
        with mock.patch.object(runtime, "runtime_config_base", return_value=base), \
             mock.patch.object(project_config, "load", return_value={"box": {"network": {"ports": ["9000:90"]}}}):
            chosen = runtime.launch_config(options)
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            runtime.sync_tracker_runtime(chosen, directory, policy=True)
            with mock.patch.dict(os.environ, {"AGENT_BOX_RUNTIME_CONFIG": str(directory / "config.json")}), \
                 mock.patch.object(project_config, "load", return_value={"box": {"network": {"ports": ["9999:99"]}}}):
                self.assertEqual(effective_config().box.network.ports, ["8080:80"])
        self.assertEqual(base.box.network.ports, [])

    def test_network_diagnostics_show_directions_and_explicit_exposure(self):
        """Host-first notation needs a directional preview, and LAN binds need a visible scope."""
        self.assertIn("workspace localhost:15432 -> host localhost:5432/tcp",
                      networking.port_summary(["5432:15432"], "host"))
        line = networking.port_summary(["[::]:8080:80/udp"], "workspace")[0]
        self.assertIn("Accessible from other computers: [::]:8080", line)
        self.assertIn("workspace:80/udp", line)
        self.assertIn("listen on [::]:80", line)

    def test_inner_snapshot_replaces_inherited_outer_network_policy(self):
        """An explicit isolated inner launch must never regain the outer box's host namespace choice."""
        base = Config()
        outer = {"format_version": 1, "workflow_mount": False,
                 "box": {"network": {"mode": "host"}, "tools": {}}}
        apply_box_policy(base, outer)
        options, _ = launch_options.parse(["--network=default"])
        with mock.patch.object(runtime, "runtime_config_base", return_value=base), \
             mock.patch.object(project_config, "load", return_value={}):
            inner = runtime.launch_config(options)
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            runtime.sync_tracker_runtime(inner, directory, policy=True)
            with mock.patch.dict(os.environ, {"AGENT_BOX_RUNTIME_CONFIG": str(directory / "config.json")}), \
                 mock.patch.object(project_config, "load", return_value={}):
                self.assertEqual(effective_config().box.network.mode, "default")
                self.assertEqual(runtime.launch_config().box.network.mode, "default")

    def test_menu_moves_the_k3s_editor_without_changing_its_storage_key(self):
        """A second saved publication list would drift from the actual sidecar settings."""
        config = Config()
        self.assertIn("ports", config_menu.menu_keys(config.box.k3s.to_dict(), ("box", "k3s")))
        self.assertNotIn("k3s", config.box.network.to_dict())
        self.assertIn("network", config_menu.menu_keys(config.box.to_dict(), ("box",)))


if __name__ == "__main__":
    unittest.main()
