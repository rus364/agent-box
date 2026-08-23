import tempfile
import unittest
from contextlib import nullcontext
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
            mock.patch.object(runtime, "container_has_keeper", return_value=True),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        root = Path(self.tmp.name) / "project"
        root.mkdir()
        self.workspace = Workspace(root, root)
        self.config = runtime.Config()
        self.script_dir = Path(__file__).parents[1]
        self.spec = runtime.workspace_container_spec(self.workspace, [], self.script_dir, self.config)

    def _container(self, state="running", base_hash=None, extra_mounts_hash=None):
        return {"State": {"Status": state}, "Config": {"Labels": {
            runtime.WORKSPACE_CONTAINER_LABEL: "true",
            runtime.WORKSPACE_CONTAINER_WORKSPACE_LABEL: self.spec.workspace_id,
            runtime.WORKSPACE_CONTAINER_SCHEMA_LABEL: runtime.WORKSPACE_CONTAINER_SCHEMA,
            runtime.WORKSPACE_CONTAINER_BASE_CONFIG_LABEL: base_hash or self.spec.base_config_hash,
            runtime.WORKSPACE_CONTAINER_EXTRA_MOUNTS_LABEL: extra_mounts_hash or self.spec.extra_mounts_hash,
        }}}

    def test_first_launch_creates_then_starts_one_workspace_container(self):
        with mock.patch.object(runtime, "inspect_workspace_container", return_value=None), \
             mock.patch.object(runtime.subprocess, "run", return_value=mock.Mock(returncode=0, stderr="")) as run:
            runtime.ensure_workspace_container(self.spec)
        podman_calls = [call.args[0] for call in run.call_args_list if call.args[0][0] == "podman"]
        self.assertEqual(podman_calls, [self.spec.create_argv, ["podman", "start", self.spec.name]])
        self.assertIn("--init", self.spec.create_argv)
        self.assertIn("--rm", self.spec.create_argv)
        self.assertEqual(self.spec.create_argv[-3], "bash")
        self.assertEqual(self.spec.create_argv[-2], str(runtime.KEEPER_PATH))

    def test_spec_calculation_preserves_existing_git_and_gh_source_directory_inodes(self):
        """Catches reseeding a live bind source while merely attaching."""
        git_source = runtime.STATE_ROOT / "git"
        gh_source = runtime.STATE_ROOT / "gh"
        git_source.mkdir(parents=True, exist_ok=True)
        gh_source.mkdir(parents=True, exist_ok=True)
        (git_source / "config").write_text("[user]\n\tname = live identity\n")
        (gh_source / "hosts.yml").write_text("github.com:\n")
        before = (git_source.stat().st_ino, gh_source.stat().st_ino)

        runtime.workspace_container_spec(self.workspace, [], self.script_dir, self.config)

        self.assertEqual((git_source.stat().st_ino, gh_source.stat().st_ino), before)
        self.assertIn("live identity", (git_source / "config").read_text())
        self.assertTrue((gh_source / "hosts.yml").is_file())

    def test_running_workspace_is_reused_without_create_or_start(self):
        with mock.patch.object(runtime, "inspect_workspace_container", return_value=self._container()), \
             mock.patch.object(runtime.subprocess, "run") as run:
            runtime.ensure_workspace_container(self.spec)
        run.assert_not_called()

    def test_stopped_workspace_is_removed_and_recreated(self):
        with mock.patch.object(runtime, "inspect_workspace_container", return_value=self._container("exited")), \
             mock.patch.object(runtime.subprocess, "run", return_value=mock.Mock(returncode=0, stderr="")) as run:
            runtime.ensure_workspace_container(self.spec)
        podman_calls = [call.args[0] for call in run.call_args_list if call.args[0][0] == "podman"]
        self.assertEqual(podman_calls[:2], [["podman", "rm", "-f", self.spec.name], self.spec.create_argv])

    def test_stopped_incompatible_workspace_is_removed_and_recreated(self):
        with mock.patch.object(runtime, "inspect_workspace_container", return_value=self._container("exited", base_hash="old")), \
             mock.patch.object(runtime.subprocess, "run", return_value=mock.Mock(returncode=0, stderr="")) as run:
            runtime.ensure_workspace_container(self.spec)
        podman_calls = [call.args[0] for call in run.call_args_list if call.args[0][0] == "podman"]
        self.assertEqual(podman_calls[:2], [["podman", "rm", "-f", self.spec.name], self.spec.create_argv])

    def test_stopped_prior_schema_workspace_is_recreated(self):
        stale = self._container("exited")
        stale["Config"]["Labels"][runtime.WORKSPACE_CONTAINER_SCHEMA_LABEL] = "1"
        with mock.patch.object(runtime, "inspect_workspace_container", return_value=stale), \
             mock.patch.object(runtime.subprocess, "run", return_value=mock.Mock(returncode=0, stderr="")) as run:
            runtime.ensure_workspace_container(self.spec)
        self.assertEqual(run.call_args_list[0].args[0], ["podman", "rm", "-f", self.spec.name])

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
        with mock.patch.object(runtime, "inspect_workspace_container", return_value=self._container(extra_mounts_hash="older")), \
             mock.patch.object(runtime.subprocess, "run") as run:
            runtime.ensure_workspace_container(self.spec)
        run.assert_not_called()

    def test_parent_extra_mount_and_omitted_mount_have_the_same_base_configuration(self):
        parent = Path(self.tmp.name) / "code"
        project = parent / "project"
        project.mkdir(parents=True)
        workspace = Workspace(project, project)
        mounted = runtime.workspace_container_spec(
            workspace, [MountSpec(parent, False)], self.script_dir, self.config,
        )
        omitted = runtime.workspace_container_spec(workspace, [], self.script_dir, self.config)
        self.assertEqual(mounted.base_config_hash, omitted.base_config_hash)
        self.assertNotEqual(mounted.extra_mounts_hash, omitted.extra_mounts_hash)
        running = {"State": {"Status": "running"}, "Config": {"Labels": {
            runtime.WORKSPACE_CONTAINER_LABEL: "true",
            runtime.WORKSPACE_CONTAINER_WORKSPACE_LABEL: mounted.workspace_id,
            runtime.WORKSPACE_CONTAINER_SCHEMA_LABEL: runtime.WORKSPACE_CONTAINER_SCHEMA,
            runtime.WORKSPACE_CONTAINER_BASE_CONFIG_LABEL: mounted.base_config_hash,
            runtime.WORKSPACE_CONTAINER_EXTRA_MOUNTS_LABEL: mounted.extra_mounts_hash,
        }}}
        with mock.patch.object(runtime, "inspect_workspace_container", return_value=running), \
             mock.patch.object(runtime.subprocess, "run") as run:
            runtime.ensure_workspace_container(omitted)
        run.assert_not_called()

    def test_equivalent_explicit_parent_mount_has_one_identity(self):
        parent = Path(self.tmp.name) / "code"
        project = parent / "project"
        project.mkdir(parents=True)
        workspace = Workspace(project, project)
        first = runtime.workspace_container_spec(
            workspace, [MountSpec(parent, False)], self.script_dir, self.config,
        )
        second = runtime.workspace_container_spec(
            workspace, [MountSpec(parent, False)], self.script_dir, self.config,
        )
        self.assertEqual(first.base_config_hash, second.base_config_hash)
        self.assertEqual(first.extra_mounts_hash, second.extra_mounts_hash)

    def test_child_extra_mount_does_not_change_base_configuration(self):
        project = Path(self.tmp.name) / "project-with-child"
        child = project / "nested"
        child.mkdir(parents=True)
        workspace = Workspace(project, project)
        child_spec = runtime.workspace_container_spec(
            workspace, [MountSpec(child, False)], self.script_dir, self.config,
        )
        plain_spec = runtime.workspace_container_spec(workspace, [], self.script_dir, self.config)
        self.assertEqual(child_spec.base_config_hash, plain_spec.base_config_hash)
        self.assertNotEqual(child_spec.extra_mounts_hash, plain_spec.extra_mounts_hash)

    def test_explicit_ro_mount_does_not_join_live_rw_mount(self):
        extra = Path(self.tmp.name) / "extra-mode"
        extra.mkdir()
        rw = runtime.workspace_container_spec(
            self.workspace, [MountSpec(extra, False)], self.script_dir, self.config,
        )
        ro = runtime.workspace_container_spec(
            self.workspace, [MountSpec(extra, True)], self.script_dir, self.config,
        )
        running = {"State": {"Status": "running"}, "Config": {"Labels": {
            runtime.WORKSPACE_CONTAINER_LABEL: "true",
            runtime.WORKSPACE_CONTAINER_WORKSPACE_LABEL: rw.workspace_id,
            runtime.WORKSPACE_CONTAINER_SCHEMA_LABEL: runtime.WORKSPACE_CONTAINER_SCHEMA,
            runtime.WORKSPACE_CONTAINER_BASE_CONFIG_LABEL: rw.base_config_hash,
            runtime.WORKSPACE_CONTAINER_EXTRA_MOUNTS_LABEL: rw.extra_mounts_hash,
        }}}
        with mock.patch.object(runtime, "inspect_workspace_container", return_value=running):
            with self.assertRaisesRegex(AgentBoxError, "different mount configuration"):
                runtime.ensure_workspace_container(ro)

    def test_running_box_is_not_reseeded_while_attaching(self):
        with mock.patch.object(runtime, "inspect_workspace_container", return_value=self._container()), \
             mock.patch.object(runtime, "prepare_container_lifetime_state") as prepare:
            runtime.ensure_workspace_container(self.spec)
        prepare.assert_not_called()

    def test_running_box_rejects_a_different_base_configuration(self):
        with mock.patch.object(runtime, "inspect_workspace_container", return_value=self._container(base_hash="other")):
            with self.assertRaisesRegex(AgentBoxError, "incompatible container-level configuration"):
                runtime.ensure_workspace_container(self.spec)

    def test_codex_home_change_is_a_running_base_configuration_mismatch(self):
        first = Path(self.tmp.name) / "codex-a"
        second = Path(self.tmp.name) / "codex-b"
        first.mkdir()
        second.mkdir()
        with mock.patch.object(runtime, "CODEX_HOME", first):
            old_spec = runtime.workspace_container_spec(self.workspace, [], self.script_dir, self.config)
        with mock.patch.object(runtime, "CODEX_HOME", second):
            new_spec = runtime.workspace_container_spec(self.workspace, [], self.script_dir, self.config)
        self.assertNotEqual(old_spec.base_config_hash, new_spec.base_config_hash)
        with mock.patch.object(runtime, "inspect_workspace_container", return_value=self._container(base_hash=old_spec.base_config_hash)):
            with self.assertRaisesRegex(AgentBoxError, "incompatible container-level configuration"):
                runtime.ensure_workspace_container(new_spec)

    def test_claude_home_change_is_a_running_base_configuration_mismatch(self):
        first = Path(self.tmp.name) / "claude-a"
        second = Path(self.tmp.name) / "claude-b"
        first.mkdir()
        second.mkdir()
        with mock.patch.object(runtime, "CLAUDE_HOME", first):
            old_spec = runtime.workspace_container_spec(self.workspace, [], self.script_dir, self.config)
        with mock.patch.object(runtime, "CLAUDE_HOME", second):
            new_spec = runtime.workspace_container_spec(self.workspace, [], self.script_dir, self.config)
        self.assertNotEqual(old_spec.base_config_hash, new_spec.base_config_hash)
        with mock.patch.object(runtime, "inspect_workspace_container", return_value=self._container(base_hash=old_spec.base_config_hash)):
            with self.assertRaisesRegex(AgentBoxError, "incompatible container-level configuration"):
                runtime.ensure_workspace_container(new_spec)

    def test_kvm_change_is_a_running_base_configuration_mismatch(self):
        with mock.patch.object(runtime, "kvm_args", return_value=[]):
            without_kvm = runtime.workspace_container_spec(self.workspace, [], self.script_dir, self.config)
        with mock.patch.object(runtime, "kvm_args", return_value=["--device=/dev/kvm"]):
            with_kvm = runtime.workspace_container_spec(self.workspace, [], self.script_dir, self.config)
        self.assertNotEqual(without_kvm.base_config_hash, with_kvm.base_config_hash)
        with mock.patch.object(runtime, "inspect_workspace_container", return_value=self._container(base_hash=without_kvm.base_config_hash)):
            with self.assertRaisesRegex(AgentBoxError, "incompatible container-level configuration"):
                runtime.ensure_workspace_container(with_kvm)

    def test_image_change_is_a_running_base_configuration_mismatch(self):
        with mock.patch.object(runtime, "IMAGE", "localhost/agent-box:old"):
            old_spec = runtime.workspace_container_spec(self.workspace, [], self.script_dir, self.config)
        with mock.patch.object(runtime, "IMAGE", "localhost/agent-box:new"):
            new_spec = runtime.workspace_container_spec(self.workspace, [], self.script_dir, self.config)
        self.assertNotEqual(old_spec.base_config_hash, new_spec.base_config_hash)
        with mock.patch.object(runtime, "inspect_workspace_container", return_value=self._container(base_hash=old_spec.base_config_hash)):
            with self.assertRaisesRegex(AgentBoxError, "incompatible container-level configuration"):
                runtime.ensure_workspace_container(new_spec)

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
        self.assertEqual(forward.base_config_hash, reverse.base_config_hash)
        self.assertEqual(forward.extra_mounts_hash, reverse.extra_mounts_hash)

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

    def test_keeper_child_is_not_a_workload(self):
        self.assertFalse(runtime.parse_top_workloads(
            "PID PPID COMMAND\n1 0 catatonit\n2 1 bash keeper.sh\n3 2 sleep 1\n"
        ))

    def test_exec_barrier_waits_for_a_real_workload_not_a_keeper_child(self):
        process = mock.Mock()
        process.poll.side_effect = [None, None]
        process.wait.return_value = 0
        with mock.patch.object(runtime, "workspace_transition_lock", side_effect=lambda _workspace: nullcontext()), \
             mock.patch.object(runtime, "ensure_workspace_container"), \
             mock.patch.object(runtime.subprocess, "Popen", return_value=process) as popen, \
             mock.patch.object(runtime, "container_has_workloads", side_effect=[False, True]) as workloads, \
             mock.patch.object(runtime, "cleanup_workspace_container_if_idle"):
            self.assertEqual(runtime.launch_in_workspace_container(self.spec, self.workspace, ["bash"], []), 0)
        self.assertEqual(workloads.call_count, 2)
        popen.assert_called_once()

    def test_exec_rc_125_is_not_retried_after_admission(self):
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.return_value = 125
        with mock.patch.object(runtime, "workspace_transition_lock", side_effect=lambda _workspace: nullcontext()), \
             mock.patch.object(runtime, "ensure_workspace_container"), \
             mock.patch.object(runtime.subprocess, "Popen", return_value=process) as popen, \
             mock.patch.object(runtime, "container_has_workloads", return_value=True), \
             mock.patch.object(runtime, "cleanup_workspace_container_if_idle"):
            self.assertEqual(runtime.launch_in_workspace_container(self.spec, self.workspace, ["mutating-command"], []), 125)
        popen.assert_called_once()


if __name__ == "__main__":
    unittest.main()
