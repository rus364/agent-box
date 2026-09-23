"""The k3s sidecar as seen from the box and the commands around it.

The box reaches its cluster only through pasta's loopback forward and a
read-only kubeconfig, so both belong to the box's identity: a recreated
sidecar publishes a new port and must recreate the box. A switched-off
cluster must leave the box exactly as it was and cost no Podman calls, and
the lifetime setting decides whether a cluster outlives its box.
"""
import io
import tempfile
import unittest
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from agent_box import cli, runtime
from agent_box.config import Config
from agent_box.errors import AgentBoxError
from agent_box.workspace import Workspace
from test_runtime import IsolatedRuntimeTest


def k3s_config(**settings) -> Config:
    config = Config()
    config.tools.k3s.enabled = True
    for key, value in settings.items():
        setattr(config.tools.k3s, key, value)
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
    def prepare(self, *, box_running, outdated):
        box = {"State": {"Status": "running"}} if box_running else None
        with mock.patch.object(runtime, "workspace_transition_lock", return_value=nullcontext()), \
                mock.patch.object(runtime, "inspect_workspace_container", return_value=box), \
                mock.patch.multiple(runtime.k3s, is_outdated=mock.Mock(return_value=outdated),
                                    ensure=mock.DEFAULT) as sidecar:
            runtime.prepare_k3s(k3s_config(), self.workspace, runtime.workspace_runtime_state(self.workspace))
        return sidecar["ensure"]

    def test_changed_settings_never_pull_the_cluster_from_under_a_running_box(self):
        with self.assertRaisesRegex(AgentBoxError, "agent-box stop"):
            self.prepare(box_running=True, outdated=True)

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

    def test_changed_settings_apply_when_no_box_runs_and_unchanged_ones_always(self):
        for box_running, outdated in ((False, True), (True, False)):
            with self.subTest(box_running=box_running, outdated=outdated):
                self.prepare(box_running=box_running, outdated=outdated).assert_called_once()


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
        config.tools.k3s.persistent_data = False
        self.release(config)
        settings = runtime.k3s.stop.call_args.args[1]
        self.assertTrue(settings.persistent_data)


class StopCommandTests(K3sRuntimeTest):
    def setUp(self):
        super().setUp()
        for patch in (
            mock.patch.object(runtime.shutil, "which", return_value="/usr/bin/podman"),
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
        for config, pulls in ((Config(), 0), (k3s_config(), 1)):
            with self.subTest(pulls=pulls), \
                    mock.patch.object(runtime.subprocess, "call", return_value=0), \
                    mock.patch.object(runtime, "detect_host_identity"), \
                    mock.patch.object(runtime, "nested_podman_build_args", return_value=[]), \
                    mock.patch.object(runtime.k3s, "pull") as pull:
                self.assertEqual(runtime.build_image(Path.cwd(), False, False, config=config), 0)
            self.assertEqual(pull.call_count, pulls)

    def test_a_cluster_without_kubectl_in_the_image_is_reported(self):
        config = k3s_config()
        config.tools.groups["kubernetes"] = False
        self.assertEqual(len(runtime.k3s_warnings(config)), 1)
        self.assertEqual(runtime.k3s_warnings(k3s_config()), [])

    def test_config_show_carries_the_warning(self):
        """``config show`` is where a user checks a setting before launching."""
        config = k3s_config()
        config.tools.groups["kubernetes"] = False
        with mock.patch.object(cli, "effective_config", return_value=config), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
            cli.config_show()
        self.assertIn("no kubectl or helm", err.getvalue())


class HostModeTests(K3sRuntimeTest):
    def test_host_sessions_never_start_or_wire_the_cluster(self):
        """A host agent keeps the user's own kube contexts; nothing may redirect it."""
        for agent in ("claude", "codex"):
            with self.subTest(agent=agent), \
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
            self.assertNotIn("KUBECONFIG", str(call.call_args))
            self.assertIn("--host sessions get no cluster", err.getvalue())


if __name__ == "__main__":
    unittest.main()
