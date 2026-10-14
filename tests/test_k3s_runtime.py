"""The k3s sidecar as seen from the box and the commands around it.

The box reaches its cluster only through pasta's loopback forward and a
read-only kubeconfig, so both belong to the box's identity: a recreated
sidecar publishes a new port and must recreate the box. A switched-off
cluster must leave the box exactly as it was and cost no Podman calls, and
the lifetime setting decides whether a cluster outlives its box.
"""
import io
import json
import os
import tempfile
import unittest
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from agent_box import cli, runtime
from agent_box.config import Config, effective_config
from agent_box.errors import AgentBoxError
from agent_box.workspace import Workspace
from test_runtime import IsolatedRuntimeTest


def k3s_config(**settings) -> Config:
    config = Config()
    config.box.k3s.enabled = True
    for key, value in settings.items():
        setattr(config.box.k3s, key, value)
    return config


class K3sRuntimeTest(IsolatedRuntimeTest):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "project"
        root.mkdir()
        self.workspace = Workspace(root, root)
        self.state = runtime.k3s_state_dir(runtime.workspace_runtime_state(self.workspace))


class BoxIdentityTests(K3sRuntimeTest):
    def spec(self, config, port=41234):
        with mock.patch.object(runtime.k3s, "api_port", return_value=port):
            return runtime.workspace_container_spec(self.workspace, [], Path.cwd(), config)

    def test_the_box_reaches_the_cluster_through_loopback_and_a_read_only_kubeconfig(self):
        argv = self.spec(k3s_config()).create_argv
        self.assertIn("--network=pasta:-T,41234", argv)
        self.assertIn(f"--env=KUBECONFIG={self.state / 'kubeconfig'}", argv)
        self.assertIn(f"type=bind,src={self.state},dst={self.state},ro=true", argv)

    def test_a_switched_off_cluster_leaves_the_box_untouched_and_asks_podman_nothing(self):
        with mock.patch.object(runtime.k3s, "api_port") as port:
            argv = runtime.workspace_container_spec(self.workspace, [], Path.cwd(), Config()).create_argv
        port.assert_not_called()
        self.assertFalse(any("pasta" in arg or "KUBECONFIG" in arg or str(self.state) in arg for arg in argv))

    def test_a_new_sidecar_port_recreates_the_box(self):
        """A box still forwarding the old port would hold a dead kubeconfig."""
        self.assertNotEqual(self.spec(k3s_config(), 41234).base_config_hash,
                            self.spec(k3s_config(), 41235).base_config_hash)

    def test_lifetime_and_readiness_settings_do_not_recreate_the_box(self):
        self.assertEqual(self.spec(k3s_config()).base_config_hash,
                         self.spec(k3s_config(lifetime="box", ready_timeout=900)).base_config_hash)


class PrepareTests(K3sRuntimeTest):
    def prepare(self, *, box_running):
        box = {"State": {"Status": "running"}} if box_running else None
        with mock.patch.object(runtime, "workspace_transition_lock", return_value=nullcontext()), \
                mock.patch.object(runtime, "inspect_workspace_container", return_value=box), \
                mock.patch.object(runtime.k3s, "ensure") as ensure:
            runtime.prepare_k3s(k3s_config(), self.workspace, runtime.workspace_runtime_state(self.workspace))
        return ensure

    def test_creation_race_never_reconfigures_the_cluster_of_the_winning_box(self):
        """Matching settings still belong to another box's creation policy."""
        with mock.patch.object(runtime, "workspace_transition_lock", return_value=nullcontext()), \
             mock.patch.object(runtime, "inspect_workspace_container", return_value={"State": {"Status": "running"}}), \
             mock.patch.object(runtime.k3s, "ensure") as ensure, \
             self.assertRaisesRegex(AgentBoxError, "appeared.*again"):
            runtime.prepare_k3s(k3s_config(), self.workspace, runtime.workspace_runtime_state(self.workspace))
        ensure.assert_not_called()

    def test_a_box_bound_cluster_left_by_a_box_that_ended_on_its_own_restarts_clean(self):
        """With persistent_data off the new box would otherwise inherit the old cluster."""
        self.state.mkdir(parents=True)
        exited = {"State": {"Status": "exited"}}
        for lifetime, box, stops in (("box", None, 1), ("box", exited, 1), ("persistent", None, 0)):
            with self.subTest(lifetime=lifetime, box=box), \
                    mock.patch.object(runtime, "workspace_transition_lock", return_value=nullcontext()), \
                    mock.patch.object(runtime, "inspect_workspace_container", return_value=box), \
                    mock.patch.multiple(runtime.k3s, ensure=mock.DEFAULT, stop=mock.DEFAULT) as sidecar, \
                    redirect_stderr(io.StringIO()):
                runtime.prepare_k3s(k3s_config(lifetime=lifetime, persistent_data=False), self.workspace,
                                    runtime.workspace_runtime_state(self.workspace))
            self.assertEqual(sidecar["stop"].call_count, stops)
            sidecar["ensure"].assert_called_once()

    def test_cluster_settings_apply_when_no_box_runs(self):
        self.prepare(box_running=False).assert_called_once()


class CreationCheckTests(K3sRuntimeTest):
    """An ``agent-box stop`` between preparing the sidecar and creating the box
    must not leave a box forwarding to a cluster that is gone."""

    def confirm(self, *, status="running", port=41234, config=None):
        with mock.patch.object(runtime.k3s, "api_port", return_value=41234):
            spec = runtime.workspace_container_spec(self.workspace, [], Path.cwd(), config or k3s_config())
        sidecar = {"State": {"Status": status}} if status else None
        with mock.patch.multiple(runtime.k3s, inspect=mock.Mock(return_value=sidecar),
                                 api_port=mock.Mock(return_value=port)):
            runtime.confirm_k3s(spec)

    def test_the_prepared_cluster_is_accepted(self):
        self.confirm()

    def test_a_stopped_removed_or_recreated_cluster_refuses_the_box(self):
        for status, port in (("exited", 41234), (None, 41234), ("running", 41235)):
            with self.subTest(status=status, port=port), self.assertRaisesRegex(AgentBoxError, "run the command again"):
                self.confirm(status=status, port=port)

    def test_a_switched_off_cluster_asks_podman_nothing(self):
        with mock.patch.object(runtime.k3s, "inspect") as inspect:
            runtime.confirm_k3s(runtime.workspace_container_spec(self.workspace, [], Path.cwd(), Config()))
        inspect.assert_not_called()


class ReleaseTests(K3sRuntimeTest):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.multiple(runtime.k3s, stop=mock.Mock(return_value=True))
        patcher.start()
        self.addCleanup(patcher.stop)

    def release(self, config, *, box=None):
        with mock.patch.object(runtime, "inspect_workspace_container", return_value=box), \
                redirect_stderr(io.StringIO()):
            runtime.release_k3s(self.workspace, config)

    def started_once(self):
        self.state.mkdir(parents=True)
        (self.state / "kubeconfig").write_text("x")

    def test_a_workspace_that_never_ran_a_cluster_costs_no_podman_call(self):
        with mock.patch.object(runtime, "inspect_workspace_container") as inspect:
            runtime.release_k3s(self.workspace, Config())
        inspect.assert_not_called()
        runtime.k3s.stop.assert_not_called()

    def test_a_persistent_cluster_outlives_its_box(self):
        self.started_once()
        self.release(k3s_config())
        runtime.k3s.stop.assert_not_called()

    def test_a_box_bound_cluster_stops_once_the_box_is_gone(self):
        self.started_once()
        self.release(k3s_config(lifetime="box"), box={"State": {"Status": "running"}})
        runtime.k3s.stop.assert_not_called()
        self.release(k3s_config(lifetime="box"))
        runtime.k3s.stop.assert_called_once()

    def test_an_exited_box_left_by_a_reboot_does_not_hold_a_box_bound_cluster(self):
        self.started_once()
        self.release(k3s_config(lifetime="box"), box={"State": {"Status": "exited"}})
        runtime.k3s.stop.assert_called_once()

    def test_a_start_that_timed_out_still_stops_once_switched_off(self):
        """A readiness timeout leaves a running sidecar and no kubeconfig."""
        self.state.mkdir(parents=True)
        self.release(Config())
        runtime.k3s.stop.assert_called_once()

    def test_a_cluster_switched_off_stops_before_a_new_box_but_keeps_its_data(self):
        """Even a throwaway cluster: switching it off is not asking for its data to go."""
        self.started_once()
        config = Config()
        config.box.k3s.persistent_data = False
        self.release(config)
        settings = runtime.k3s.stop.call_args.args[1]
        self.assertTrue(settings.persistent_data)


class StopCommandTests(K3sRuntimeTest):
    def setUp(self):
        super().setUp()
        for patch in (
            mock.patch.object(runtime.host_podman, "require"),
            mock.patch.object(runtime, "resolve_workspace", return_value=self.workspace),
            mock.patch.object(runtime, "workspace_transition_lock", return_value=nullcontext()),
            mock.patch.object(runtime, "inspect_workspace_container", return_value=None),
            mock.patch.multiple(runtime.k3s, stop=mock.Mock(return_value=True), remove=mock.Mock()),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def stop(self, *args, config=None):
        with mock.patch.object(runtime, "effective_config", return_value=config or Config()), \
                redirect_stderr(io.StringIO()) as err:
            self.assertEqual(runtime.stop_main(list(args)), 0)
        return err.getvalue()

    def test_stop_stops_the_cluster_whatever_its_lifetime_and_keeps_its_data(self):
        self.state.mkdir(parents=True)
        output = self.stop(config=k3s_config())
        runtime.k3s.stop.assert_called_once()
        runtime.k3s.remove.assert_not_called()
        self.assertIn("stopped the k3s cluster", output)

    def test_a_workspace_that_never_ran_a_cluster_asks_podman_nothing_about_one(self):
        self.stop()
        runtime.k3s.stop.assert_not_called()

    def test_cluster_data_deletes_the_cluster_even_without_a_box(self):
        output = self.stop("--cluster-data")
        runtime.k3s.remove.assert_called_once()
        self.assertTrue(runtime.k3s.remove.call_args.kwargs["data"])
        self.assertIn("and its data", output)

    def test_an_unknown_argument_is_refused(self):
        with self.assertRaisesRegex(AgentBoxError, "--cluster-data"):
            runtime.stop_main(["--everything"])


class BuildAndWarningTests(K3sRuntimeTest):
    def test_build_fetches_the_sidecar_image_only_when_the_cluster_is_on(self):
        """Inside a box the cluster belongs to the outer box, so the image would be dead weight."""
        for config, inside, pulls in ((Config(), False, 0), (k3s_config(), False, 1), (k3s_config(), True, 0)):
            with self.subTest(pulls=pulls, inside=inside), \
                    mock.patch.object(runtime, "inside_box", return_value=inside), \
                    mock.patch.object(runtime.subprocess, "call", return_value=0), \
                    mock.patch.object(runtime, "detect_host_identity"), \
                    mock.patch.object(runtime, "nested_podman_build_args", return_value=[]), \
                    mock.patch.object(runtime.k3s, "pull") as pull:
                self.assertEqual(runtime.build_image(Path.cwd(), False, False, config=config), 0)
            self.assertEqual(pull.call_count, pulls)

    def test_a_cluster_without_kubectl_in_the_image_is_reported(self):
        config = k3s_config()
        config.box.tools.groups["kubernetes"] = False
        self.assertEqual(len(runtime.k3s_warnings(config)), 1)
        self.assertEqual(runtime.k3s_warnings(k3s_config()), [])

    def test_config_show_carries_the_warning(self):
        """``config show`` is where a user checks a setting before launching."""
        config = k3s_config()
        config.box.tools.groups["kubernetes"] = False
        with mock.patch.object(cli, "effective_config", return_value=config), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
            cli.config_show()
        self.assertIn("no kubectl or helm", err.getvalue())


class NestedLaunchTests(K3sRuntimeTest):
    """A launch inside a workspace box inherits the outer box's ``box.k3s``
    through its session snapshot, but a sidecar cannot start in nested Podman:
    every nested launch pulled the k3s image and then failed on sysfs."""

    def launch(self, inside: bool, config: Config | None = None, project: dict | None = None):
        captured = {}

        def fake_launch(spec, workspace, command, extra_env, process_env=None):
            captured.update(argv=spec.create_argv, env=extra_env)
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
            runtime.runtime_main(["doctor"], Path.cwd())
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


class HostModeTests(K3sRuntimeTest):
    def test_host_sessions_never_start_or_wire_the_cluster(self):
        """A host agent keeps the user's own kube contexts; nothing may redirect it.

        The user's KUBECONFIG, or its absence, reaches the agent unchanged; the
        suite may itself run inside a box whose session sets one.
        """
        for agent, kubeconfig in (("claude", None), ("codex", None), ("claude", "/home/user/.kube/config")):
            environ = {name: value for name, value in os.environ.items() if name != "KUBECONFIG"}
            if kubeconfig:
                environ["KUBECONFIG"] = kubeconfig
            with self.subTest(agent=agent, kubeconfig=kubeconfig), \
                    mock.patch.dict(os.environ, environ, clear=True), \
                    mock.patch.object(runtime.shutil, "which", return_value=f"/usr/bin/{agent}"), \
                    mock.patch.object(runtime, "resolve_workspace", return_value=self.workspace), \
                    mock.patch.object(runtime, "load_config", return_value=k3s_config()), \
                    mock.patch.object(runtime.claude_settings, "reconcile_claude_settings"), \
                    mock.patch.multiple(runtime.k3s, ensure=mock.DEFAULT, stop=mock.DEFAULT,
                                        api_port=mock.DEFAULT) as sidecar, \
                    mock.patch.object(runtime.subprocess, "call", return_value=0) as call, \
                    redirect_stderr(io.StringIO()) as err:
                runtime.host_main(agent, ["--version"])
            self.assertEqual([m.call_count for m in sidecar.values()], [0, 0, 0])
            # Compare the one variable: the whole environment would print its secrets on failure.
            self.assertEqual(call.call_args.kwargs["env"].get("KUBECONFIG"), kubeconfig)
            self.assertIn("--host sessions get no cluster", err.getvalue())


if __name__ == "__main__":
    unittest.main()
