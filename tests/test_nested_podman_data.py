"""The nested Podman volume: who owns it, which project it names, and how long it lives."""
import dataclasses
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from subprocess import CompletedProcess
from unittest import mock

from agent_box import project_config, runtime
from agent_box.config import Config
from agent_box.errors import AgentBoxError
from agent_box.workspace import Workspace


class FakeVolumes:
    """Podman's volume commands over a dict of name -> labels; anything else succeeds."""

    def __init__(self):
        self.volumes: dict[str, dict[str, str]] = {}
        self.calls: list[list[str]] = []

    def __call__(self, argv, **_):
        args = argv[1:]
        self.calls.append(args)
        ok = lambda out="": CompletedProcess(argv, 0, out, "")  # noqa: E731
        match args:
            case ["volume", "exists", name]:
                return ok() if name in self.volumes else CompletedProcess(argv, 1, "", "")
            case ["volume", "inspect", "--format", template, name]:
                label = template.split('"')[1]
                return ok(self.volumes[name].get(label, "") + "\n")
            case ["volume", "create", *labels, name]:
                self.volumes[name] = dict(label.removeprefix("--label=").split("=", 1) for label in labels)
                return ok()
            case ["volume", "rm", name]:
                del self.volumes[name]
                return ok()
        return ok()


class NestedPodmanDataTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "project"
        root.mkdir()
        self.workspace = Workspace(root, root)
        self.id = runtime.workspace_identity(self.workspace)
        self.volume = runtime.nested_podman_volume(self.id)
        self.podman = FakeVolumes()
        state = Path(tmp.name) / "state"
        for patch in (
            mock.patch.object(runtime.subprocess, "run", self.podman),
            mock.patch.object(runtime, "STATE_ROOT", state),
            mock.patch.object(runtime, "POLICY_RUNTIME_DIR", state / "runtime/policy"),
            mock.patch.object(runtime, "TRACKER_RUNTIME_DIR", state / "runtime/tracker"),
            mock.patch.object(runtime, "inside_box", return_value=False),
            mock.patch.object(runtime, "image_tool_groups", return_value=None),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def config(self, *, persistent=False, podman=True):
        config = Config()
        config.box.podman.persistent_data = persistent
        config.box.tools.groups["podman"] = podman
        return config

    def own_volume(self, **extra):
        self.podman.volumes[self.volume] = {runtime.NESTED_PODMAN_LABEL: self.id, **extra}


class SettingTests(unittest.TestCase):
    def test_data_is_disposable_unless_asked_for(self):
        """Kept data piled up unseen; keeping it must be a choice."""
        self.assertFalse(Config().box.podman.persistent_data)
        self.assertTrue(Config.from_dict({"box": {"podman": {"persistent_data": True}}}).box.podman.persistent_data)

    def test_a_project_may_keep_or_drop_its_own_data(self):
        self.assertIs(project_config.project_field(("box", "podman", "persistent_data")), bool)

class SharedPolicyTests(NestedPodmanDataTestCase):
    def test_a_joining_session_keeps_what_the_box_was_created_to_keep(self):
        """The last session to leave decides; a joiner reading a default would delete kept data."""
        created = self.config(persistent=True, podman=False)
        spec = runtime.workspace_container_spec(self.workspace, [], Path(__file__).parents[1], created)
        joined = runtime.apply_box_policy(Config(), runtime.workspace_box_policy(spec))
        self.assertTrue(joined.box.podman.persistent_data)

    def test_a_session_snapshot_carries_the_setting(self):
        config = self.config(persistent=True, podman=False)
        state = runtime.workspace_runtime_state(self.workspace)
        path = runtime.session_runtime(config, runtime_state=state)
        with mock.patch.dict(runtime.os.environ, {"AGENT_BOX_RUNTIME_CONFIG": str(path)}):
            self.assertTrue(runtime.effective_config().box.podman.persistent_data)


class OwnershipTests(NestedPodmanDataTestCase):
    def test_the_volume_names_its_owner_and_its_project(self):
        """The id is a one-way hash: without the path label nothing can say whose data it is."""
        runtime.ensure_nested_podman_volume(self.id, "/home/u/project")
        self.assertEqual(self.podman.volumes[self.volume],
                         {runtime.NESTED_PODMAN_LABEL: self.id, runtime.WORKSPACE_PATH_LABEL: "/home/u/project"})

    def test_an_existing_own_volume_is_reused(self):
        self.own_volume(marker="kept")
        runtime.ensure_nested_podman_volume(self.id, "/p")
        self.assertEqual(self.podman.volumes[self.volume]["marker"], "kept")

    def test_a_volume_agent_box_did_not_create_is_neither_used_nor_deleted(self):
        self.podman.volumes[self.volume] = {}
        with self.assertRaisesRegex(AgentBoxError, "did not create"):
            runtime.ensure_nested_podman_volume(self.id, "/p")
        with self.assertRaisesRegex(AgentBoxError, "did not create"):
            runtime.remove_nested_podman_volume(self.id)
        self.assertIn(self.volume, self.podman.volumes)

    def test_a_failing_podman_is_not_absent_data(self):
        """Reading a broken Podman as "no volume" would report data deleted that is still there."""
        self.own_volume()
        with mock.patch.object(runtime.subprocess, "run",
                               return_value=CompletedProcess([], 125, "", "cannot connect to Podman")):
            with self.assertRaisesRegex(AgentBoxError, "cannot connect"):
                runtime.remove_nested_podman_volume(self.id)

    def test_removing_absent_data_is_not_an_error(self):
        self.assertFalse(runtime.remove_nested_podman_volume(self.id))


class BoxCreationTests(NestedPodmanDataTestCase):
    def create_box(self, config):
        spec = runtime.workspace_container_spec(self.workspace, [], Path(__file__).parents[1], Config())
        # The spec renders nested Podman's host checks; only its creation config matters here.
        spec = dataclasses.replace(spec, creation_config=config)
        with mock.patch.object(runtime, "inspect_workspace_container", return_value=None), \
             mock.patch.object(runtime, "prepare_container_lifetime_state"):
            runtime.ensure_workspace_container(spec)
        return spec

    def test_a_new_box_starts_without_the_data_a_gone_box_left(self):
        """A box that ended on its own leaves its volume; disposable means the next box never sees it."""
        self.own_volume(marker="old")
        spec = self.create_box(self.config())
        self.assertNotIn("marker", self.podman.volumes[self.volume])
        self.assertEqual(self.podman.volumes[self.volume][runtime.WORKSPACE_PATH_LABEL], spec.workspace_path)
        volume_created = next(i for i, call in enumerate(self.podman.calls) if call[:2] == ["volume", "create"])
        self.assertLess(volume_created, self.podman.calls.index(spec.create_argv[1:]),
                        "the volume must exist, labelled, before --mount could create it bare")

    def test_kept_data_reaches_the_next_box(self):
        self.own_volume(marker="old")
        self.create_box(self.config(persistent=True))
        self.assertEqual(self.podman.volumes[self.volume]["marker"], "old")

    def test_a_box_without_the_group_gets_no_volume(self):
        self.create_box(self.config(podman=False))
        self.assertEqual(self.podman.volumes, {})


class ReleaseTests(NestedPodmanDataTestCase):
    def release(self, config, box):
        with mock.patch.object(runtime, "inspect_workspace_container", return_value=box):
            runtime.release_nested_podman(self.workspace, config)

    def test_disposable_data_goes_once_the_box_is_gone(self):
        self.own_volume()
        self.release(self.config(), None)
        self.assertEqual(self.podman.volumes, {})

    def test_a_present_box_keeps_its_volume(self):
        """An exited box still mounts it; the next box's creation decides."""
        self.own_volume()
        self.release(self.config(), {"State": {"Status": "exited"}})
        self.assertIn(self.volume, self.podman.volumes)

    def test_kept_data_survives_the_box(self):
        self.own_volume()
        self.release(self.config(persistent=True), None)
        self.assertIn(self.volume, self.podman.volumes)


class StopTests(NestedPodmanDataTestCase):
    def stop(self, config, *args):
        with mock.patch.object(runtime.host_podman, "require"), \
             mock.patch.object(runtime, "resolve_workspace", return_value=self.workspace), \
             mock.patch.object(runtime, "workspace_transition_lock", return_value=nullcontext()), \
             mock.patch.object(runtime, "inspect_workspace_container", return_value=None), \
             mock.patch.object(runtime, "effective_config", return_value=config), \
             mock.patch.object(runtime.links, "stop"):
            return runtime.stop_main(list(args))

    def test_stop_deletes_disposable_data(self):
        self.own_volume()
        self.stop(self.config())
        self.assertEqual(self.podman.volumes, {})

    def test_stop_keeps_data_the_configuration_keeps(self):
        self.own_volume()
        self.stop(self.config(persistent=True))
        self.assertIn(self.volume, self.podman.volumes)

    def test_podman_data_flag_deletes_kept_data(self):
        self.own_volume()
        self.stop(self.config(persistent=True), "--podman-data")
        self.assertEqual(self.podman.volumes, {})

    def test_an_unknown_flag_names_both_accepted_ones(self):
        with self.assertRaisesRegex(AgentBoxError, "--cluster-data and --podman-data"):
            self.stop(self.config(), "--data")


if __name__ == "__main__":
    unittest.main()
