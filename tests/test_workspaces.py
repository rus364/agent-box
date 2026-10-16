"""agent-box workspaces: listing what each project keeps, and removing it."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest import mock

from agent_box import cloud, k3s, kubeconfigs, links, registries, runtime, workspaces
from agent_box.errors import AgentBoxError

CLUSTER, PODMAN, STATE_ONLY, UNKNOWN, NETWORK = ("2" * 16, "3" * 16, "4" * 16, "5" * 16, "6" * 16)


class FakePodman:
    """``ps``/``volume ls`` as JSON and ``unshare du``; nothing here deletes."""

    def __init__(self):
        self.containers, self.volumes, self.networks = [], [], []

    def __call__(self, argv, **_):
        args = argv[1:]
        ok = lambda out: CompletedProcess(argv, 0, out, "")  # noqa: E731
        if args[:2] == ["ps", "--all"]:
            label, _, value = args[2].removeprefix("--filter=label=").partition("=")
            return ok(json.dumps([c for c in self.containers
                                  if label in c["Labels"] and (not value or c["Labels"][label] == value)]))
        if args[:2] == ["network", "ls"]:
            return ok(json.dumps(self.networks))
        if args[:2] == ["volume", "ls"]:
            return ok(json.dumps(self.volumes))
        if args[:2] == ["unshare", "du"]:
            return ok("".join(f"{1_500_000_000 if 'podman' in p else 2048}\t{p}\n" for p in args[4:]))
        raise AssertionError(f"unexpected podman call {args}")


class WorkspacesTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        state = self.tmp / "state"
        self.podman = FakePodman()
        for patch in (
            mock.patch.object(runtime, "STATE_ROOT", state),
            mock.patch.object(runtime, "TRACKER_RUNTIME_DIR", state / "runtime/tracker"),
            mock.patch.object(runtime, "POLICY_RUNTIME_DIR", state / "runtime/policy"),
            mock.patch.object(runtime, "HOST_SESSIONS_ROOT", state / "runtime/host-sessions"),
            mock.patch.object(registries, "REGISTRY_RUNTIME_DIR", state / "runtime/registries"),
            mock.patch.object(kubeconfigs, "KUBECONFIG_RUNTIME_DIR", state / "runtime/kubeconfigs"),
            mock.patch.object(cloud, "CLOUD_RUNTIME_DIR", state / "runtime/cloud"),
            mock.patch.object(links, "LINKS_RUNTIME_DIR", self.tmp / "links"),
            mock.patch.object(workspaces.subprocess, "run", self.podman),
            mock.patch.object(runtime.host_podman, "require"),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.live = self.tmp / "live"
        self.live.mkdir()
        self.box = runtime.workspace_identity(runtime.Workspace(self.live, self.live))
        self.gone = self.tmp / "gone"

    def populate(self):
        """One workspace per source of evidence, so the listing must merge all of them."""
        path = runtime.WORKSPACE_PATH_LABEL
        self.podman.containers = [
            {"Names": ["agent-box-live-" + self.box], "State": "running",
             "Labels": {runtime.WORKSPACE_CONTAINER_LABEL: "true",
                        runtime.WORKSPACE_CONTAINER_WORKSPACE_LABEL: self.box, path: str(self.live)}},
            {"Names": [k3s.name(CLUSTER)], "State": "exited", "Labels": {k3s.LABEL: CLUSTER}},
        ]
        self.podman.volumes = [
            {"Name": k3s.name(CLUSTER), "Mountpoint": "/v/k3s", "Labels": {k3s.LABEL: CLUSTER, path: str(self.gone)}},
            {"Name": runtime.nested_podman_volume(PODMAN), "Mountpoint": "/v/podman",
             "Labels": {runtime.NESTED_PODMAN_LABEL: PODMAN, path: str(self.live)}},
            {"Name": "somebody-elses", "Mountpoint": "/v/other", "Labels": {}},
        ]
        self.podman.networks = [{"name": k3s.name(NETWORK), "labels": {k3s.LABEL: NETWORK}}]
        runtime.record_workspace_path(STATE_ONLY, str(self.gone / "scratch"))
        (runtime.workspace_state_root(UNKNOWN) / "git").mkdir(parents=True)
        (runtime.TRACKER_RUNTIME_DIR / "workspaces" / self.box).mkdir(parents=True)

    def entry(self, workspace_id):
        return next(item for item in workspaces.collect() if item.id == workspace_id)


class ListingTests(WorkspacesTestCase):
    def test_every_source_contributes_its_workspace_and_path(self):
        self.populate()
        items = {item.id: item for item in workspaces.collect()}
        self.assertEqual(set(items), {self.box, CLUSTER, PODMAN, STATE_ONLY, UNKNOWN, NETWORK},
                         "a volume agent-box did not label is not a workspace")
        self.assertEqual(items[NETWORK].cluster, "network", "a leftover network alone is still something to remove")
        self.assertEqual(items[self.box].path, str(self.live))
        self.assertEqual((items[CLUSTER].cluster, items[CLUSTER].path), ("exited", str(self.gone)))
        self.assertEqual(items[PODMAN].podman_volume, "/v/podman")
        self.assertEqual(items[STATE_ONLY].path, str(self.gone / "scratch"))
        self.assertEqual(items[UNKNOWN].path, "")
        self.assertTrue(items[self.box].state)

    def test_the_table_marks_vanished_and_unrecorded_directories_and_shows_sizes(self):
        self.populate()
        lines = workspaces.render(workspaces.collect()).splitlines()
        row = {line.split()[0]: line for line in lines[1:]}
        self.assertIn(f"{self.gone} (missing)", row[CLUSTER])
        self.assertIn("exited 2.0 KB", row[CLUSTER])
        self.assertIn("1.5 GB", row[PODMAN])
        self.assertIn("(unknown)", row[UNKNOWN])
        self.assertNotIn("missing", row[self.box])


class RemovalTests(WorkspacesTestCase):
    def setUp(self):
        super().setUp()
        self.removed = mock.Mock()
        for patch in (
            mock.patch.object(runtime, "remove_workspace_container_then_confirm_absent", self.removed.box),
            mock.patch.object(links, "stop", self.removed.links),
            mock.patch.object(k3s, "remove", self.removed.cluster),
            mock.patch.object(runtime, "remove_nested_podman_volume", self.removed.podman),
            mock.patch.object(runtime, "stop_host_sessions", self.removed.host_sessions),
            mock.patch.object(workspaces.shutil, "which", return_value="/usr/bin/tmux"),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.populate()

    def test_rm_by_path_deletes_the_box_cluster_podman_data_and_every_state_directory(self):
        self.assertEqual(workspaces.main(["rm", str(self.live), "--yes"]), 0)
        self.removed.box.assert_called_once_with("agent-box-live-" + self.box)
        self.removed.cluster.assert_called_once_with(self.box, runtime.workspace_state_root(self.box) / "k3s", data=True)
        self.removed.podman.assert_called_once_with(self.box)
        self.assertEqual([d for d in runtime.workspace_state_dirs(self.box) if d.exists()],
                         [runtime.workspace_state_root(self.box)])
        self.assertEqual([c.name for c in runtime.workspace_state_root(self.box).iterdir()],
                         [runtime.WORKSPACE_LOCK_FILE], "only the lock a waiting launch may hold stays")
        self.assertFalse(workspaces._has_state(runtime.workspace_state_root(self.box)), "a lone lock is nothing to list")

    def test_rm_by_id_reaches_a_workspace_whose_directory_nobody_recorded(self):
        workspaces.main(["rm", UNKNOWN, "--yes"])
        self.assertFalse(workspaces._has_state(runtime.workspace_state_root(UNKNOWN)))

    def test_rm_of_something_agent_box_does_not_keep_is_refused(self):
        with self.assertRaisesRegex(AgentBoxError, "keeps nothing"):
            workspaces.main(["rm", str(self.tmp / "elsewhere"), "--yes"])

    def test_prune_removes_exactly_the_workspaces_whose_directory_is_gone(self):
        """Unknown directories may still exist: guessing would delete a live project's state."""
        workspaces.main(["prune", "--yes"])
        self.assertEqual(sorted(c.args[0] for c in self.removed.podman.call_args_list), [CLUSTER, STATE_ONLY])
        self.assertTrue(runtime.workspace_state_root(UNKNOWN).exists())
        self.assertTrue((runtime.TRACKER_RUNTIME_DIR / "workspaces" / self.box).exists())

    def test_prune_ends_a_box_whose_directory_is_gone(self):
        """A detached session in a deleted project is exactly the leftover prune exists for."""
        self.podman.containers[0]["Labels"][runtime.WORKSPACE_PATH_LABEL] = str(self.gone / "running")
        workspaces.main(["prune", "--yes"])
        self.removed.box.assert_called_once_with("agent-box-live-" + self.box)

    def test_rm_ends_the_workspace_host_sessions(self):
        """A running session has consumed its env file and has no status yet: its directory is empty."""
        sessions = runtime.HOST_SESSIONS_ROOT / UNKNOWN
        sessions.mkdir(parents=True)
        workspaces.main(["rm", UNKNOWN, "--yes"])
        self.removed.host_sessions.assert_called_once_with(sessions)

    def test_host_sessions_that_cannot_be_stopped_make_removal_incomplete(self):
        sessions = runtime.HOST_SESSIONS_ROOT / UNKNOWN
        sessions.mkdir(parents=True)
        (sessions / "claude-0123456789ab.status").write_text("")
        with mock.patch.object(workspaces.shutil, "which", return_value=None), \
             mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(workspaces.main(["rm", UNKNOWN, "--yes"]), 1)
        self.assertIn("tmux not found", err.getvalue())
        self.assertTrue(sessions.exists(), "the record of sessions still running must not be deleted")

    def test_without_podman_host_state_is_still_listed_and_removed(self):
        """--host needs no Podman, and what its sessions leave must stay manageable."""
        def no_podman(argv, **_):
            raise AssertionError(f"podman called without podman: {argv}")
        with mock.patch.object(workspaces.shutil, "which", lambda name: None if name == "podman" else "/bin/" + name), \
             mock.patch.object(workspaces.subprocess, "run", no_podman):
            self.assertIn(STATE_ONLY, {item.id for item in workspaces.collect(podman=False)})
            self.assertEqual(workspaces.main(["rm", STATE_ONLY, "--yes"]), 0)
        self.removed.podman.assert_not_called()
        self.assertFalse((runtime.workspace_state_root(STATE_ONLY) / runtime.WORKSPACE_PATH_FILE).exists())

    def test_a_box_created_while_confirming_is_removed_too(self):
        def launch_meanwhile(items, assume_yes):
            self.podman.containers.append({"Names": ["late-box"], "State": "running", "Labels": {
                runtime.WORKSPACE_CONTAINER_LABEL: "true", runtime.WORKSPACE_CONTAINER_WORKSPACE_LABEL: STATE_ONLY}})
            return True
        with mock.patch.object(workspaces, "_confirm", launch_meanwhile):
            workspaces.main(["rm", STATE_ONLY])
        self.removed.box.assert_called_once_with("late-box")

    def test_a_workspace_left_with_only_a_cluster_network_can_be_removed(self):
        self.assertEqual(workspaces.main(["rm", NETWORK, "--yes"]), 0)
        self.removed.cluster.assert_called_once_with(NETWORK, runtime.workspace_state_root(NETWORK) / "k3s", data=True)

    def test_state_that_could_not_be_deleted_is_reported_not_hidden(self):
        (runtime.workspace_state_root(STATE_ONLY) / "gh").mkdir()
        sessions = runtime.HOST_SESSIONS_ROOT / STATE_ONLY
        sessions.mkdir(parents=True)
        with mock.patch.object(workspaces.shutil, "rmtree"), \
             mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertEqual(workspaces.main(["rm", STATE_ONLY, "--yes"]), 1)
        self.assertIn(str(runtime.workspace_state_root(STATE_ONLY)), err.getvalue())
        self.assertIn(str(sessions), err.getvalue(), "a session directory may hold an unconsumed credential file")

    def test_deleting_needs_a_terminal_or_yes(self):
        with mock.patch.object(workspaces.sys, "stdin", mock.Mock(isatty=mock.Mock(return_value=False))):
            with self.assertRaisesRegex(AgentBoxError, "--yes"):
                workspaces.main(["prune"])
        self.removed.podman.assert_not_called()

    def test_a_declined_confirmation_deletes_nothing(self):
        with mock.patch.object(workspaces.sys, "stdin", mock.Mock(isatty=mock.Mock(return_value=True))), \
             mock.patch("builtins.input", return_value="n"):
            self.assertEqual(workspaces.main(["rm", self.box]), 1)
        self.removed.box.assert_not_called()


class PathRecordTests(unittest.TestCase):
    def test_a_directory_name_that_is_not_utf8_survives_the_record(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(runtime, "STATE_ROOT", Path(tmp)):
            path = "/home/u/" + bytes([0xFF, 0x41]).decode("utf-8", "surrogateescape") + "\n"
            runtime.record_workspace_path(UNKNOWN, path)
            with mock.patch.object(workspaces.subprocess, "run", FakePodman()):
                self.assertEqual(next(item.path for item in workspaces.collect() if item.id == UNKNOWN), path)

    def test_a_box_names_its_project_on_the_container_and_beside_its_state(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(runtime, "STATE_ROOT", Path(tmp) / "state"), \
             mock.patch.object(runtime, "_nested_podman_volume_owner", return_value=None), \
             mock.patch.object(runtime, "inspect_workspace_container", return_value=None), \
             mock.patch.object(runtime, "prepare_container_lifetime_state"), \
             mock.patch.object(runtime, "image_tool_groups", return_value=None), \
             mock.patch.object(runtime.subprocess, "run", return_value=CompletedProcess([], 0, "", "")):
            root = Path(tmp) / "project"
            root.mkdir()
            workspace = runtime.Workspace(root, root)
            spec = runtime.workspace_container_spec(workspace, [], Path(__file__).parents[1], runtime.Config())
            runtime.ensure_workspace_container(spec)
            self.assertIn(f"--label={runtime.WORKSPACE_PATH_LABEL}={root.resolve()}", spec.create_argv)
            record = runtime.workspace_state_root(spec.workspace_id) / runtime.WORKSPACE_PATH_FILE
            self.assertEqual(record.read_text().strip(), str(root.resolve()))


if __name__ == "__main__":
    unittest.main()
