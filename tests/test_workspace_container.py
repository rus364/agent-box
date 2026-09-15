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

    def test_different_workspaces_render_distinct_git_and_gh_snapshot_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first_root = root / "first"
            second_root = root / "second"
            first_root.mkdir()
            second_root.mkdir()
            config = runtime.Config()
            script_dir = Path(__file__).parents[1]
            first = runtime.workspace_container_spec(
                Workspace(first_root, first_root), [], script_dir, config,
            )
            second = runtime.workspace_container_spec(
                Workspace(second_root, second_root), [], script_dir, config,
            )

            def source_for(spec, destination):
                mount = next(arg for arg in spec.create_argv if f"dst={destination}" in arg)
                return mount.split("src=", 1)[1].split(",", 1)[0]

            self.assertNotEqual(
                source_for(first, runtime.HOST_HOME / ".config/git"),
                source_for(second, runtime.HOST_HOME / ".config/git"),
            )
            self.assertNotEqual(
                source_for(first, runtime.HOST_HOME / ".config/gh"),
                source_for(second, runtime.HOST_HOME / ".config/gh"),
            )


class WorkspaceContainerLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        state = Path(self.tmp.name) / "state"
        for patch in (
            mock.patch.object(runtime, "STATE_ROOT", state),
            mock.patch.object(runtime, "BUILD_STATE", state / "build"),
            mock.patch.object(runtime, "NOTIFY_STATE_ROOT", state / "notify"),
            mock.patch.object(runtime, "TRACKER_RUNTIME_DIR", state / "runtime/tracker"),
            mock.patch.object(runtime, "POLICY_RUNTIME_DIR", state / "runtime/policy"),
            mock.patch.object(runtime, "WORKFLOW_STATE", state / "workflow"),
            mock.patch.object(runtime, "CLAUDE_HOME", state / "claude"),
            mock.patch.object(runtime, "CODEX_HOME", state / "codex"),
            mock.patch.object(runtime, "XDG_CACHE_HOME", state / "cache"),
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

    def test_confirmed_absence_permits_snapshot_reseed_and_creation(self):
        with mock.patch.object(runtime, "workspace_container_exists", return_value=False), \
             mock.patch.object(runtime, "prepare_container_lifetime_state") as prepare, \
             mock.patch.object(runtime.subprocess, "run", return_value=mock.Mock(returncode=0, stderr="")) as run:
            runtime.ensure_workspace_container(self.spec)

        prepare.assert_called_once_with(
            self.spec.creation_config, mount_notifications=False, runtime_state=self.spec.runtime_state,
        )
        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [self.spec.create_argv, ["podman", "start", self.spec.name]],
        )

    def test_container_exists_operational_failure_fails_closed_before_reseed(self):
        with mock.patch.object(
            runtime.subprocess, "run", return_value=mock.Mock(returncode=125, stderr="storage failure"),
        ) as run, mock.patch.object(runtime, "prepare_container_lifetime_state") as prepare:
            with self.assertRaisesRegex(AgentBoxError, "could not determine workspace container state"):
                runtime.ensure_workspace_container(self.spec)

        prepare.assert_not_called()
        self.assertEqual([call.args[0] for call in run.call_args_list], [
            ["podman", "container", "exists", self.spec.name],
        ])

    def test_inspect_operational_failure_after_exists_fails_closed(self):
        responses = [
            mock.Mock(returncode=0, stdout="", stderr=""),
            mock.Mock(returncode=125, stdout="", stderr="inspect failed"),
            mock.Mock(returncode=0, stdout="", stderr=""),
        ]
        with mock.patch.object(runtime.subprocess, "run", side_effect=responses) as run, \
             mock.patch.object(runtime, "prepare_container_lifetime_state") as prepare:
            with self.assertRaisesRegex(AgentBoxError, "could not inspect workspace container"):
                runtime.ensure_workspace_container(self.spec)

        prepare.assert_not_called()
        self.assertEqual([call.args[0] for call in run.call_args_list], [
            ["podman", "container", "exists", self.spec.name],
            ["podman", "container", "inspect", self.spec.name],
            ["podman", "container", "exists", self.spec.name],
        ])

    def test_container_disappearing_between_exists_and_inspect_is_rechecked_before_creation(self):
        responses = [
            mock.Mock(returncode=0, stdout="", stderr=""),
            mock.Mock(returncode=125, stdout="", stderr="no such container"),
            mock.Mock(returncode=1, stdout="", stderr=""),
            mock.Mock(returncode=0, stdout="", stderr=""),
            mock.Mock(returncode=0, stdout="", stderr=""),
        ]
        with mock.patch.object(runtime.subprocess, "run", side_effect=responses) as run, \
             mock.patch.object(runtime, "prepare_container_lifetime_state") as prepare:
            runtime.ensure_workspace_container(self.spec)

        prepare.assert_called_once()
        self.assertEqual([call.args[0] for call in run.call_args_list], [
            ["podman", "container", "exists", self.spec.name],
            ["podman", "container", "inspect", self.spec.name],
            ["podman", "container", "exists", self.spec.name],
            self.spec.create_argv,
            ["podman", "start", self.spec.name],
        ])

    def test_cleanup_query_failure_leaves_container_untouched(self):
        with mock.patch.object(runtime, "inspect_workspace_container", side_effect=AgentBoxError("query failed")), \
             mock.patch.object(runtime.subprocess, "run") as run:
            with self.assertRaisesRegex(AgentBoxError, "query failed"):
                runtime.cleanup_workspace_container_if_idle(self.spec.name)
        run.assert_not_called()

    def test_stale_container_must_be_confirmed_absent_before_snapshot_reseed(self):
        stale = self._container("exited")
        with mock.patch.object(runtime, "inspect_workspace_container", side_effect=[stale, stale]), \
             mock.patch.object(runtime, "prepare_container_lifetime_state") as prepare, \
             mock.patch.object(runtime.subprocess, "run", return_value=mock.Mock(returncode=0, stderr="")) as run:
            with self.assertRaisesRegex(AgentBoxError, "container still exists after removal"):
                runtime.ensure_workspace_container(self.spec)

        prepare.assert_not_called()
        self.assertEqual([call.args[0] for call in run.call_args_list], [
            ["podman", "rm", "-f", self.spec.name],
        ])

    def test_ambiguous_keeper_query_refuses_to_admit_work_into_a_dying_box(self):
        with mock.patch.object(runtime, "inspect_workspace_container", return_value=self._container()), \
             mock.patch.object(runtime, "container_has_keeper", return_value=None), \
             mock.patch.object(runtime, "prepare_container_lifetime_state") as prepare, \
             mock.patch.object(runtime.subprocess, "run") as run:
            with self.assertRaisesRegex(AgentBoxError, "could not determine workspace keeper state"):
                runtime.ensure_workspace_container(self.spec)

        prepare.assert_not_called()
        run.assert_not_called()

    def test_exiting_keeper_rechecks_until_old_box_is_confirmed_absent(self):
        with mock.patch.object(runtime, "inspect_workspace_container", return_value=None), \
             mock.patch.object(runtime, "container_has_keeper", return_value=False), \
             mock.patch.object(runtime.time, "monotonic", side_effect=[0, 0]), \
             mock.patch.object(runtime.time, "sleep"):
            self.assertIsNone(runtime._wait_for_exiting_keeper(self.spec.name, self._container()))

    def test_spec_calculation_preserves_existing_git_and_gh_source_directory_inodes(self):
        """Catches reseeding a live bind source while merely attaching."""
        git_source = self.spec.runtime_state.git
        gh_source = self.spec.runtime_state.gh
        git_source.mkdir(parents=True, exist_ok=True)
        gh_source.mkdir(parents=True, exist_ok=True)
        (git_source / "config").write_text("[user]\n\tname = live identity\n")
        (gh_source / "hosts.yml").write_text("github.com:\n")
        before = (git_source.stat().st_ino, gh_source.stat().st_ino)

        runtime.workspace_container_spec(self.workspace, [], self.script_dir, self.config)

        self.assertEqual((git_source.stat().st_ino, gh_source.stat().st_ino), before)
        self.assertIn("live identity", (git_source / "config").read_text())
        self.assertTrue((gh_source / "hosts.yml").is_file())

    def test_preparing_second_workspace_preserves_first_snapshot_inode_and_contents(self):
        first_state = self.spec.runtime_state
        second_root = Path(self.tmp.name) / "second-project"
        second_root.mkdir()
        second_spec = runtime.workspace_container_spec(
            Workspace(second_root, second_root), [], self.script_dir, self.config,
        )
        runtime.prepare_container_lifetime_state(
            self.config, mount_notifications=False, runtime_state=first_state,
        )
        (first_state.git / "config").write_text("[user]\n\tname = workspace-a\n")
        (first_state.gh / "hosts.yml").write_text("workspace-a\n")
        before = (first_state.git.stat().st_ino, first_state.gh.stat().st_ino)

        runtime.prepare_container_lifetime_state(
            self.config, mount_notifications=False, runtime_state=second_spec.runtime_state,
        )

        self.assertEqual((first_state.git.stat().st_ino, first_state.gh.stat().st_ino), before)
        self.assertIn("workspace-a", (first_state.git / "config").read_text())
        self.assertEqual((first_state.gh / "hosts.yml").read_text(), "workspace-a\n")
        self.assertNotEqual(first_state.git, second_spec.runtime_state.git)
        self.assertNotEqual(first_state.gh, second_spec.runtime_state.gh)

    def test_preparing_recreated_workspace_reseeds_its_own_snapshot(self):
        config = runtime.Config()
        config.git.identity = False
        state = self.spec.runtime_state
        runtime.prepare_container_lifetime_state(
            config, mount_notifications=False, runtime_state=state,
        )
        (state.git / "old-box").write_text("stale")
        (state.gh / "old-box").write_text("stale")

        runtime.prepare_container_lifetime_state(
            config, mount_notifications=False, runtime_state=state,
        )

        self.assertFalse((state.git / "old-box").exists())
        self.assertFalse((state.gh / "old-box").exists())

    def test_running_workspace_is_reused_without_create_or_start(self):
        with mock.patch.object(runtime, "inspect_workspace_container", return_value=self._container()), \
             mock.patch.object(runtime.subprocess, "run") as run:
            runtime.ensure_workspace_container(self.spec)
        run.assert_not_called()

    def test_stopped_workspace_is_removed_and_recreated(self):
        with mock.patch.object(runtime, "inspect_workspace_container", side_effect=[self._container("exited"), None]), \
             mock.patch.object(runtime.subprocess, "run", return_value=mock.Mock(returncode=0, stderr="")) as run:
            runtime.ensure_workspace_container(self.spec)
        podman_calls = [call.args[0] for call in run.call_args_list if call.args[0][0] == "podman"]
        self.assertEqual(podman_calls[:2], [["podman", "rm", "-f", self.spec.name], self.spec.create_argv])

    def test_stopped_incompatible_workspace_is_removed_and_recreated(self):
        with mock.patch.object(runtime, "inspect_workspace_container", side_effect=[self._container("exited", base_hash="old"), None]), \
             mock.patch.object(runtime.subprocess, "run", return_value=mock.Mock(returncode=0, stderr="")) as run:
            runtime.ensure_workspace_container(self.spec)
        podman_calls = [call.args[0] for call in run.call_args_list if call.args[0][0] == "podman"]
        self.assertEqual(podman_calls[:2], [["podman", "rm", "-f", self.spec.name], self.spec.create_argv])

    def test_stopped_prior_schema_workspace_is_recreated(self):
        stale = self._container("exited")
        stale["Config"]["Labels"][runtime.WORKSPACE_CONTAINER_SCHEMA_LABEL] = "1"
        with mock.patch.object(runtime, "inspect_workspace_container", side_effect=[stale, None]), \
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

    def test_audio_change_is_a_running_base_configuration_mismatch(self):
        """A running box cannot gain or lose a bind mount, so flipping audio
        must recreate it rather than report sound that is not there."""
        with mock.patch.object(runtime, "audio_args", return_value=[]):
            without_audio = runtime.workspace_container_spec(self.workspace, [], self.script_dir, self.config)
        with mock.patch.object(runtime, "audio_args", return_value=["--env=PULSE_SERVER=unix:/run/user/1/pulse/native"]):
            with_audio = runtime.workspace_container_spec(self.workspace, [], self.script_dir, self.config)
        self.assertNotEqual(without_audio.base_config_hash, with_audio.base_config_hash)
        with mock.patch.object(runtime, "inspect_workspace_container", return_value=self._container(base_hash=without_audio.base_config_hash)):
            with self.assertRaisesRegex(AgentBoxError, "incompatible container-level configuration"):
                runtime.ensure_workspace_container(with_audio)

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
            runtime.cleanup_workspace_container_if_idle(self.spec.name)
        run.assert_not_called()

    def test_idle_container_is_removed(self):
        with mock.patch.object(runtime, "inspect_workspace_container", side_effect=[self._container(), None]), \
             mock.patch.object(runtime, "container_has_workloads", return_value=False), \
             mock.patch.object(runtime.subprocess, "run") as run:
            runtime.cleanup_workspace_container_if_idle(self.spec.name)
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

    def test_podman_pause_process_does_not_keep_the_box_alive(self):
        """One nested `podman` call leaves `catatonit -P` holding its user
        namespace for good; counting it would keep the box up forever."""
        self.assertFalse(runtime.parse_top_workloads(
            "PID PPID COMMAND\n1 0 catatonit\n2 1 bash keeper.sh\n540 1 catatonit -P\n"
        ))

    def test_nested_container_beside_the_pause_process_is_still_a_workload(self):
        """Only the pause-mode argv is exempt: a running inner container, even
        one using catatonit as its init, is real work."""
        self.assertEqual(runtime.parse_top_workloads(
            "PID PPID COMMAND\n1 0 catatonit\n2 1 bash keeper.sh\n540 1 catatonit -P\n"
            "600 1 /usr/bin/conmon --api-version 1\n601 600 catatonit -- sleep 60\n"
        ), [(600, "/usr/bin/conmon --api-version 1"), (601, "catatonit -- sleep 60")])

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
