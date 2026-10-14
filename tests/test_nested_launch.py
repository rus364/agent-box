"""Launches started inside a workspace box, which the session snapshot ties
to the outer box's settings while Podman nests them inside it."""
import json
import os
import unittest
from pathlib import Path
from unittest import mock

from agent_box import paths, runtime
from agent_box.config import Config, effective_config
from agent_box.launch_options import LaunchOptions
from test_k3s_runtime import K3sRuntimeTest, k3s_config


class NestedLaunchTests(K3sRuntimeTest):
    """A launch inside a workspace box inherits the outer box's ``box.k3s``
    and ``box.links`` through its session snapshot, but both live on the host:
    every nested launch pulled the k3s image and failed on sysfs, and with k3s
    off it failed on the outer broker's socket directory."""

    def launch(self, inside: bool, config: Config | None = None, project: dict | None = None,
               options: LaunchOptions | None = None):
        captured = {}

        def fake_launch(spec, workspace, command, extra_env, process_env=None):
            # Box creation prepares or starts a broker only for creation_config's links.
            captured.update(argv=spec.create_argv, env=extra_env, links=spec.creation_config.box.links.enabled)
            return 0

        with mock.patch.object(runtime, "inside_box", return_value=inside), \
                mock.patch("agent_box.project_config.load", return_value=project or {}), \
                mock.patch.object(runtime.host_podman, "require"), \
                mock.patch.object(runtime, "resolve_workspace", return_value=self.workspace), \
                mock.patch.object(runtime, "load_config", return_value=config or k3s_config()), \
                mock.patch.object(runtime, "image_exists", return_value=True), \
                mock.patch.object(runtime, "image_tool_drift", return_value=([], [])), \
                mock.patch.object(runtime, "inspect_workspace_container", return_value=None), \
                mock.patch.multiple(runtime.k3s, ensure=mock.DEFAULT, api_port=mock.DEFAULT) as sidecar, \
                mock.patch.object(runtime, "launch_in_workspace_container", side_effect=fake_launch), \
                mock.patch.object(runtime, "warn") as warn, \
                self.assertRaises(SystemExit):
            sidecar["api_port"].return_value = 41234
            runtime.runtime_main(["doctor"], Path.cwd(), options)
        target = Path(next(item.split("=", 1)[1] for item in captured["env"]
                           if item.startswith("AGENT_BOX_RUNTIME_CONFIG=")))
        source, destination, _ = runtime.tracker_mounts(Config(), runtime.workspace_runtime_state(self.workspace))[0]
        snapshot = source / target.relative_to(destination)
        return captured, sidecar["ensure"], [call.args[0] for call in warn.call_args_list], snapshot

    def test_a_nested_box_gets_no_cluster_of_its_own_and_says_so(self):
        captured, ensure, warnings, snapshot = self.launch(inside=True)
        ensure.assert_not_called()
        self.assertFalse(any("pasta" in arg or "KUBECONFIG" in arg or str(self.state) in arg
                             for arg in captured["argv"]))
        self.assertIn("AGENT_BOX_K3S=nested", captured["env"])
        self.assertTrue(any("gets no cluster of its own" in warning for warning in warnings))
        # A launch nested one level deeper must not inherit the cluster either.
        self.assertFalse(json.loads(snapshot.read_text())["box"]["k3s"]["enabled"])

    def test_the_nested_session_reads_k3s_off_even_under_an_inherited_box_policy(self):
        """A session that joined a k3s box carries that box's policy, which the
        snapshot keeps and a nested session reapplies over its configuration."""
        config = k3s_config()
        with mock.patch.object(runtime.k3s, "api_port", return_value=41234), \
                mock.patch.object(runtime, "image_tool_groups", return_value=None):
            config._box_policy = runtime.workspace_box_policy(
                runtime.workspace_container_spec(self.workspace, [], Path.cwd(), k3s_config()))
        _, _, _, snapshot = self.launch(inside=True, config=config)
        with mock.patch.dict(os.environ, {"AGENT_BOX_RUNTIME_CONFIG": str(snapshot)}), \
                mock.patch("agent_box.project_config.load", return_value={}):
            self.assertFalse(effective_config().box.k3s.enabled)

    def test_a_project_that_switched_k3s_off_still_hands_on_a_k3s_free_snapshot(self):
        """The snapshot renders from the layer beneath the project, where the
        global setting still has the cluster on."""
        _, ensure, warnings, snapshot = self.launch(inside=True, project={"box": {"k3s": {"enabled": False}}})
        ensure.assert_not_called()
        self.assertFalse(any("gets no cluster of its own" in warning for warning in warnings))
        with mock.patch.dict(os.environ, {"AGENT_BOX_RUNTIME_CONFIG": str(snapshot)}):
            self.assertFalse(effective_config(include_project=False).box.k3s.enabled)

    def test_doctor_in_a_joined_nested_box_still_names_the_nested_case(self):
        """A nested box created earlier recorded k3s off, so the launch sees it
        off; doctor must not then advise turning box.k3s on."""
        captured, _, warnings, _ = self.launch(inside=True, config=Config())
        self.assertIn("AGENT_BOX_K3S=nested", captured["env"])
        self.assertFalse(any("gets no cluster of its own" in warning for warning in warnings))

    def test_a_host_launch_still_starts_and_wires_the_cluster(self):
        captured, ensure, warnings, snapshot = self.launch(inside=False)
        ensure.assert_called_once()
        self.assertIn("--network=pasta:-T,41234", captured["argv"])
        self.assertIn("AGENT_BOX_K3S=1", captured["env"])
        self.assertFalse(any("gets no cluster of its own" in warning for warning in warnings))
        self.assertTrue(json.loads(snapshot.read_text())["box"]["k3s"]["enabled"])

    def test_doctor_names_the_nested_case(self):
        self.assertIn("none in a nested box", runtime.DOCTOR_SCRIPT)


    def test_a_nested_box_gets_no_link_broker_and_says_so(self):
        config = Config()
        config.box.links.enabled = True
        captured, _, warnings, snapshot = self.launch(inside=True, config=config)
        self.assertFalse(captured["links"])
        self.assertFalse(any(runtime.links.SOCKET_ENV in arg for arg in captured["argv"]))
        self.assertTrue(any("gets no host link broker" in warning for warning in warnings))
        self.assertFalse(json.loads(snapshot.read_text())["box"]["links"]["enabled"])

    def test_a_host_launch_still_prepares_the_link_broker(self):
        config = Config()
        config.box.links.enabled = True
        captured, _, warnings, _ = self.launch(inside=False, config=config)
        self.assertTrue(captured["links"])
        self.assertTrue(any(runtime.links.SOCKET_ENV in arg for arg in captured["argv"]))
        self.assertFalse(any("gets no host link broker" in warning for warning in warnings))


    def test_an_explicit_links_flag_does_not_conflict_with_a_running_nested_box(self):
        """A running nested box records links off; comparing --links with that
        policy would refuse every later nested launch that passes it."""
        _, _, warnings, snapshot = self.launch(inside=True, config=Config(),
                                               options=LaunchOptions(overrides={"links": True}))
        admitted = runtime.existing_workspace_spec.call_args.args[1]
        self.assertIs(admitted.overrides["links"], False)
        self.assertTrue(any("gets no host link broker" in warning for warning in warnings))
        self.assertFalse(json.loads(snapshot.read_text())["launch"]["links"])

    def test_an_inherited_links_flag_is_not_replayed_in_the_nested_session(self):
        """The snapshot replays the session's launch flags over the box policy."""
        config = Config()
        config._launch = {"links": True}
        _, _, _, snapshot = self.launch(inside=True, config=config)
        with mock.patch.dict(os.environ, {"AGENT_BOX_RUNTIME_CONFIG": str(snapshot)}), \
                mock.patch("agent_box.project_config.load", return_value={}):
            self.assertFalse(effective_config().box.links.enabled)


class NestedLinksDirectoryTests(unittest.TestCase):
    def test_a_nested_launcher_never_addresses_the_outer_broker(self):
        """Podman mounts the outer broker's socket directory at the host path,
        so a nested stop there would send the outer broker its stop request."""
        with mock.patch.object(paths.Path, "is_file", return_value=False):
            host = paths.links_runtime_root()
        with mock.patch.object(paths.Path, "is_file", return_value=True):
            nested = paths.links_runtime_root()
        self.assertEqual(host, Path("/tmp") / f"agent-box-links-{os.getuid()}")
        self.assertEqual(nested.parent, host.parent)
        self.assertNotEqual(nested, host)
        self.assertTrue(nested.name.startswith(host.name + "-"))


if __name__ == "__main__":
    unittest.main()
