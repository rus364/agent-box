import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_box import runtime
from agent_box.errors import AgentBoxError
from agent_box.mounts import MountSpec
from agent_box.workspace import Workspace


class WorkspaceContainerIdentityTests(unittest.TestCase):
    def test_canonical_workspace_has_a_stable_collision_safe_container_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = Workspace(root / "a" / "project", root / "a" / "project")
            second = Workspace(root / "b" / "project", root / "b" / "project")
            first.host_root.mkdir(parents=True)
            second.host_root.mkdir(parents=True)

            first_name = runtime.workspace_container_name(first)
            self.assertEqual(first_name, runtime.workspace_container_name(first))
            self.assertNotEqual(first_name, runtime.workspace_container_name(second))
            self.assertTrue(first_name.startswith("agent-box-project-"))
            self.assertLessEqual(len(first_name), 63)

    def test_weird_workspace_name_is_safe_for_podman(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "weird name_[]!"
            root.mkdir()
            name = runtime.workspace_container_name(Workspace(root, root))
        self.assertRegex(name, r"^[a-z0-9][a-z0-9_.-]*$")


class WorkspaceContainerLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        state = Path(self.tmp.name) / "state"
        for patch in (
            mock.patch.object(runtime, "STATE_ROOT", state),
            mock.patch.object(runtime, "BUILD_STATE", state / "build"),
            mock.patch.object(runtime, "NOTIFY_STATE_ROOT", state / "notify"),
            mock.patch.object(runtime, "NOTIFY_CREDENTIALS", state / "runtime/notify.json"),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        root = Path(self.tmp.name) / "project"
        root.mkdir()
        self.workspace = Workspace(root, root)
        self.config = runtime.Config()
        self.script_dir = Path(__file__).parents[1]
        self.spec = runtime.workspace_container_spec(self.workspace, [], self.script_dir, self.config)

    def _container(self, state="running", config_hash=None):
        return {"State": {"Status": state}, "Config": {"Labels": {
            runtime.WORKSPACE_CONTAINER_LABEL: "true",
            runtime.WORKSPACE_CONTAINER_WORKSPACE_LABEL: self.spec.workspace_id,
            runtime.WORKSPACE_CONTAINER_SCHEMA_LABEL: runtime.WORKSPACE_CONTAINER_SCHEMA,
            runtime.WORKSPACE_CONTAINER_CONFIG_LABEL: config_hash or self.spec.config_hash,
        }}}

    def test_first_launch_creates_then_starts_one_workspace_container(self):
        with mock.patch.object(runtime, "inspect_workspace_container", return_value=None), \
             mock.patch.object(runtime.subprocess, "run", return_value=mock.Mock(returncode=0, stderr="")) as run:
            runtime.ensure_workspace_container(self.spec)
        self.assertEqual(run.call_args_list[0].args[0], self.spec.create_argv)
        self.assertEqual(run.call_args_list[1].args[0], ["podman", "start", self.spec.name])
        self.assertIn("--init", self.spec.create_argv)
        self.assertIn("--rm", self.spec.create_argv)
        self.assertEqual(tuple(self.spec.create_argv[-2:]), runtime.KEEPER_COMMAND)

    def test_running_workspace_is_reused_without_create_or_start(self):
        with mock.patch.object(runtime, "inspect_workspace_container", return_value=self._container()), \
             mock.patch.object(runtime.subprocess, "run") as run:
            runtime.ensure_workspace_container(self.spec)
        run.assert_not_called()

    def test_stopped_workspace_is_removed_and_recreated(self):
        with mock.patch.object(runtime, "inspect_workspace_container", return_value=self._container("exited")), \
             mock.patch.object(runtime.subprocess, "run", return_value=mock.Mock(returncode=0, stderr="")) as run:
            runtime.ensure_workspace_container(self.spec)
        self.assertEqual(run.call_args_list[0].args[0], ["podman", "rm", "-f", self.spec.name])
        self.assertEqual(run.call_args_list[1].args[0], self.spec.create_argv)

    def test_explicit_incompatible_mounts_refuse_a_live_box(self):
        extra = Path(self.tmp.name) / "extra"
        extra.mkdir()
        incompatible = runtime.workspace_container_spec(
            self.workspace, [MountSpec(extra, False)], self.script_dir, self.config,
        )
        with mock.patch.object(runtime, "inspect_workspace_container", return_value=self._container()), \
             self.assertRaisesRegex(AgentBoxError, "different mount"):
            runtime.ensure_workspace_container(incompatible)

    def test_omitted_mounts_attach_to_an_existing_configured_box(self):
        with mock.patch.object(runtime, "inspect_workspace_container", return_value=self._container(config_hash="older")), \
             mock.patch.object(runtime.subprocess, "run") as run:
            runtime.ensure_workspace_container(self.spec)
        run.assert_not_called()

    def test_equivalent_explicit_mount_order_has_one_configuration_identity(self):
        first = Path(self.tmp.name) / "first"
        second = Path(self.tmp.name) / "second"
        first.mkdir()
        second.mkdir()
        forward = runtime.workspace_container_spec(
            self.workspace, [MountSpec(first, False), MountSpec(second, True)], self.script_dir, self.config,
        )
        reverse = runtime.workspace_container_spec(
            self.workspace, [MountSpec(second, True), MountSpec(first, False)], self.script_dir, self.config,
        )
        self.assertEqual(forward.config_hash, reverse.config_hash)
        self.assertEqual(forward.mounts_hash, reverse.mounts_hash)

    def test_daemon_workload_keeps_container_after_launcher_exits(self):
        with mock.patch.object(runtime, "inspect_workspace_container", return_value=self._container()), \
             mock.patch.object(runtime, "container_has_workloads", return_value=True), \
             mock.patch.object(runtime.subprocess, "run") as run:
            runtime.cleanup_workspace_container_if_idle(self.spec)
        run.assert_not_called()

    def test_idle_container_is_removed(self):
        with mock.patch.object(runtime, "inspect_workspace_container", return_value=self._container()), \
             mock.patch.object(runtime, "container_has_workloads", return_value=False), \
             mock.patch.object(runtime.subprocess, "run") as run:
            runtime.cleanup_workspace_container_if_idle(self.spec)
        self.assertEqual(run.call_args.args[0], ["podman", "rm", "-f", self.spec.name])

    def test_process_table_filters_only_init_and_keeper(self):
        self.assertFalse(runtime.parse_top_workloads("PID PPID COMMAND\n1 0 catatonit\n2 1 bash keeper.sh\n"))
        self.assertEqual(
            runtime.parse_top_workloads("PID PPID COMMAND\n1 0 catatonit\n2 1 bash keeper.sh\n17 1 codex app-server\n"),
            [(17, "codex app-server")],
        )


if __name__ == "__main__":
    unittest.main()
