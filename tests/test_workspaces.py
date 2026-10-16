"""agent-box workspaces: listing what each project keeps, and removing it."""
import json
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest import mock

from agent_box import cloud, k3s, kubeconfigs, links, registries, runtime, workspaces
from agent_box.errors import AgentBoxError

CLUSTER, PODMAN, STATE_ONLY, UNKNOWN = ("2" * 16, "3" * 16, "4" * 16, "5" * 16)


class FakePodman:
    """``ps``/``volume ls`` as JSON and ``unshare du``; nothing here deletes."""

    def __init__(self):
        self.containers, self.volumes = [], []

    def __call__(self, argv, **_):
        args = argv[1:]
        ok = lambda out: CompletedProcess(argv, 0, out, "")  # noqa: E731
        if args[:2] == ["ps", "--all"]:
            label = args[2].removeprefix("--filter=label=").split("=")[0]
            return ok(json.dumps([c for c in self.containers if label in c["Labels"]]))
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
        runtime.record_workspace_path(STATE_ONLY, str(self.gone / "scratch"))
        runtime.workspace_state_root(UNKNOWN).mkdir(parents=True)
        (runtime.TRACKER_RUNTIME_DIR / "workspaces" / self.box).mkdir(parents=True)

    def entry(self, workspace_id):
        return next(item for item in workspaces.collect() if item.id == workspace_id)


class ListingTests(WorkspacesTestCase):
    def test_every_source_contributes_its_workspace_and_path(self):
        self.populate()
        items = {item.id: item for item in workspaces.collect()}
        self.assertEqual(set(items), {self.box, CLUSTER, PODMAN, STATE_ONLY, UNKNOWN},
                         "a volume agent-box did not label is not a workspace")
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
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.populate()

    def test_rm_by_path_deletes_the_box_cluster_podman_data_and_every_state_directory(self):
        self.assertEqual(workspaces.main(["rm", str(self.live), "--yes"]), 0)
        self.removed.box.assert_called_once_with("agent-box-live-" + self.box)
        self.removed.cluster.assert_called_once_with(self.box, runtime.workspace_state_root(self.box) / "k3s", data=True)
        self.removed.podman.assert_called_once_with(self.box)
        self.assertEqual([d for d in runtime.workspace_state_dirs(self.box) if d.exists()], [])

    def test_rm_by_id_reaches_a_workspace_whose_directory_nobody_recorded(self):
        workspaces.main(["rm", UNKNOWN, "--yes"])
        self.assertFalse(runtime.workspace_state_root(UNKNOWN).exists())

    def test_rm_of_something_agent_box_does_not_keep_is_refused(self):
        with self.assertRaisesRegex(AgentBoxError, "keeps nothing"):
            workspaces.main(["rm", str(self.tmp / "elsewhere"), "--yes"])

    def test_prune_removes_exactly_the_workspaces_whose_directory_is_gone(self):
        """Unknown directories may still exist: guessing would delete a live project's state."""
        workspaces.main(["prune", "--yes"])
        self.assertEqual(sorted(c.args[0] for c in self.removed.podman.call_args_list), [CLUSTER, STATE_ONLY])
        self.assertTrue(runtime.workspace_state_root(UNKNOWN).exists())
        self.assertTrue((runtime.TRACKER_RUNTIME_DIR / "workspaces" / self.box).exists())

    def test_prune_leaves_a_running_box_to_an_explicit_rm(self):
        self.podman.containers[0]["Labels"][runtime.WORKSPACE_PATH_LABEL] = str(self.gone / "running")
        workspaces.main(["prune", "--yes"])
        self.removed.box.assert_not_called()

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
            path = "/home/u/" + bytes([0xFF, 0x41]).decode("utf-8", "surrogateescape")
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
