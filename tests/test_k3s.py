"""The k3s sidecar: its configuration, and how agent-box keeps one cluster per workspace.

The cluster holds the user's deployments and volumes across sessions, so the
lifecycle's promises are the point: a changed definition recreates the
container but never its data, stopping keeps data unless the user chose a
throwaway cluster, and removing what is already gone is not an error. The
box reaches the API only through the rendered kubeconfig, so that file has to
point at the published loopback port and stay private.
"""
import json
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest import mock

from agent_box import k3s, runtime
from agent_box.config import Config, K3sConfig, ToolsConfig, load_config, save_config
from agent_box.errors import AgentBoxError

WID = "0123456789abcdef"
STATE, ROOT = Path("/state/k3s"), Path("/project")
NAME = f"agent-box-k3s-{WID}"
KUBECONFIG = """apiVersion: v1
clusters:
- cluster:
    certificate-authority-data: Q0E=
    server: https://127.0.0.1:6443
  name: default
contexts:
- context:
    cluster: default
    user: default
  name: default
current-context: default
kind: Config
users:
- name: default
  user:
    client-certificate-data: Q0VSVA==
"""


class FakePodman:
    """Just enough Podman state for the sidecar lifecycle."""

    def __init__(self, *, ready=True, dies=False):
        self.container = None  # {"running": bool, "spec": str}
        self.networks, self.volumes = set(), set()
        self.ready, self.dies = ready, dies
        self.calls = []
        self.controllers = ["cpuset", "cpu", "io", "memory", "pids"]
        self.owner = WID  # the io.agent-box.k3s label of every existing object
        self.broken = False
        self.probe_hangs = False
        self.probe_timeouts = []
        self.inspect_timeouts = []

    def __call__(self, argv, **_):
        assert argv[0] == "podman"
        args = argv[1:]
        self.calls.append(args)
        ok = lambda out="": CompletedProcess(argv, 0, out, "")  # noqa: E731
        fail = CompletedProcess(argv, 1, "", "no such object")
        if self.broken:
            return CompletedProcess(argv, 125, "", "cannot connect to Podman")
        match args:
            case ["container", "exists", _]:
                self.inspect_timeouts.append(_.get("timeout"))
                return ok() if self.container is not None else fail
            case ["container", "inspect", _]:
                return ok(json.dumps([{"State": {"Status": "running" if self.container["running"] else "exited"},
                                       "Config": {"Labels": {k3s.LABEL: self.owner,
                                                             k3s.SPEC_LABEL: self.container["spec"]}}}]))
            case [("network" | "volume"), "inspect", _, _]:
                return ok(self.owner + "\n")
            case ["info", _]:
                return ok(json.dumps({"host": {"cgroupVersion": "v2", "cgroupControllers": self.controllers}}))
            case ["image", "exists", _]:
                return ok()
            case [("network" | "volume") as kind, "exists", resource]:
                return ok() if resource in getattr(self, kind + "s") else fail
            case [("network" | "volume") as kind, "create", *_, resource]:
                getattr(self, kind + "s").add(resource)
                return ok()
            case [("network" | "volume") as kind, "rm", resource]:
                getattr(self, kind + "s").discard(resource)
                return ok()
            case ["create", *rest]:
                spec = next(a.split("=", 2)[2] for a in rest if a.startswith(f"--label={k3s.SPEC_LABEL}="))
                self.container = {"running": False, "spec": spec}
                return ok()
            case ["start", _]:
                self.container["running"] = not self.dies
                return ok()
            case ["stop", *_]:
                self.container["running"] = False
                return ok()
            case ["rm", *_]:
                self.container = None
                return ok()
            case ["exec", _, "kubectl", "get", "nodes", *_]:
                self.probe_timeouts.append(_["timeout"])
                if self.probe_hangs:
                    raise k3s.subprocess.TimeoutExpired(argv, _["timeout"])
                return ok("True" if self.ready else "False")
            case ["exec", _, "kubectl", "get", "--raw=/readyz"]:
                return ok("ok")
            case ["exec", _, "cat", _]:
                return ok(KUBECONFIG)
            case [("ps" | "network" | "volume"), *_] if any(a.startswith("--filter=label=") for a in args):
                names = [NAME] if self.container else []
                return ok("\n".join(sorted({*names, *self.networks, *self.volumes})))
            case ["port", *_]:
                return ok("127.0.0.1:41234\n")
            case ["logs", *_]:
                return ok("E0923 kubelet failed: open /dev/kmsg\n")
        raise AssertionError(f"unexpected podman call {args}")

    def did(self, *prefix):
        return [call for call in self.calls if tuple(call[:len(prefix)]) == prefix]


class SidecarTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name) / "k3s"
        self.root = Path(tmp.name) / "project"
        self.root.mkdir()
        self.podman = FakePodman()
        patcher = mock.patch.object(k3s.subprocess, "run", side_effect=lambda *a, **k: self.podman(*a, **k))
        patcher.start()
        self.addCleanup(patcher.stop)
        sleep = mock.patch.object(k3s.time, "sleep")
        sleep.start()
        self.addCleanup(sleep.stop)


class ConfigTests(unittest.TestCase):
    def test_the_cluster_is_off_by_default_and_a_saved_file_stays_sparse(self):
        """The agent becomes cluster-admin, so the cluster must be asked for."""
        self.assertFalse(Config().tools.k3s.enabled)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            config = Config()
            config.tools.k3s.enabled = True
            config.tools.k3s.lifetime = "box"
            save_config(config, path)
            self.assertEqual(json.loads(path.read_text())["tools"], {"k3s": {"enabled": True, "lifetime": "box"}})
            self.assertEqual(load_config(path).tools.k3s, config.tools.k3s)

    def test_a_non_boolean_does_not_enable_the_cluster(self):
        self.assertFalse(ToolsConfig.from_dict({"k3s": {"enabled": "yes"}}).k3s.enabled)
        self.assertFalse(ToolsConfig.from_dict({"k3s": "on"}).k3s.enabled)

    def test_an_unknown_lifetime_or_malformed_version_is_reported(self):
        """A typo must not silently run the pinned release or a different lifetime."""
        problems = ToolsConfig.from_dict({"k3s": {"lifetime": "forever", "version": "1.33"}}).validate()
        self.assertEqual(len(problems), 2)
        self.assertEqual(ToolsConfig.from_dict({"k3s": {"version": "v1.35.0-rc1-k3s1"}}).validate(), [])
        self.assertTrue(ToolsConfig.from_dict({"k3s": {"lifetime": ""}}).validate())

    def test_the_cluster_never_reaches_the_image_build_or_its_label(self):
        """k3s builds nothing into the image, so it must not be reported as drift."""
        config = Config()
        config.tools.k3s.enabled = True
        args = " ".join(runtime.tool_build_args(config))
        self.assertNotIn("K3S", args)
        self.assertNotIn("k3s", args)
        self.assertNotIn("k3s", config.tools.enabled_groups())
        with mock.patch.object(runtime, "image_tool_groups", return_value=config.tools.enabled_groups()):
            self.assertEqual(runtime.image_tool_drift(config), ([], []))


class DefinitionTests(unittest.TestCase):
    def test_components_and_version_change_the_definition(self):
        """Each of these changes what runs, so each must recreate the sidecar."""
        base = K3sConfig(enabled=True)
        variants = [K3sConfig(enabled=True, traefik=True), K3sConfig(enabled=True, metrics_server=True),
                    K3sConfig(enabled=True, version="v1.33.5-k3s1")]
        hashes = {k3s.spec_hash(WID, config, STATE, ROOT) for config in [base, *variants]}
        self.assertEqual(len(hashes), 4)
        self.assertIn("--disable=traefik,metrics-server", k3s.server_args(base))
        self.assertNotIn("--disable=metrics-server", k3s.server_args(K3sConfig(traefik=True, metrics_server=True)))

    def test_lifetime_and_data_policy_do_not_recreate_a_running_cluster(self):
        base = K3sConfig(enabled=True)
        self.assertEqual(k3s.spec_hash(WID, base, STATE, ROOT), k3s.spec_hash(WID, K3sConfig(
            enabled=True, lifetime="box", persistent_data=False, ready_timeout=900), STATE, ROOT))

    def test_the_pinned_release_is_addressed_by_digest(self):
        self.assertIn("@sha256:", k3s.image_ref(K3sConfig()))
        self.assertEqual(k3s.image_ref(K3sConfig(version="v1.33.5-k3s1")), "docker.io/rancher/k3s:v1.33.5-k3s1")

    def test_the_api_is_published_on_host_loopback_only(self):
        """Publishing on every address would hand the LAN a cluster-admin endpoint's door."""
        published = [arg for arg in k3s.container_args(WID, K3sConfig(), STATE, ROOT) if arg.startswith("--publish")]
        self.assertEqual(published, ["--publish=127.0.0.1::6443/tcp"])



class ClusterSettingsTests(unittest.TestCase):
    """The submenu's cluster settings: each shapes only the sidecar, and safely."""

    def test_defaults_keep_the_recipe_the_first_release_shipped(self):
        """A user who never opens the submenu must not have their cluster recreated."""
        args = k3s.container_args(WID, K3sConfig(), STATE, ROOT)
        self.assertIn("--disable=traefik,metrics-server", args)
        self.assertEqual([a for a in args if a.startswith(("--cpus", "--memory", "--disable-network-policy"))], [])
        self.assertEqual([a for a in args if a.startswith("--mount=type=bind")], [])
        self.assertEqual(args[-1], "--kube-proxy-arg=conntrack-max-per-core=0")

    def test_every_sidecar_setting_changes_the_definition_but_the_wait_does_not(self):
        variants = [dict(servicelb=False), dict(local_storage=False), dict(network_policy=False),
                    dict(cpus="2"), dict(memory="4g"), dict(ports=["8080:80"]),
                    dict(registry_mirrors={"docker.io": ["https://mirror.example"]}),
                    dict(registry_mirrors={"docker.io": ["https://other.example"]}),
                    dict(extra_args=["--cluster-cidr=10.60.0.0/16"])]
        hashes = {k3s.spec_hash(WID, K3sConfig(**v), STATE, ROOT) for v in [{}, *variants]}
        self.assertEqual(len(hashes), len(variants) + 1)
        self.assertEqual(k3s.spec_hash(WID, K3sConfig(), STATE, ROOT),
                         k3s.spec_hash(WID, K3sConfig(ready_timeout=900), STATE, ROOT))

    def test_components_map_to_k3s_flags_and_extra_args_come_last(self):
        args = k3s.server_args(K3sConfig(traefik=True, metrics_server=True, servicelb=False, local_storage=False,
                                         network_policy=False, extra_args=["--node-label=x=y"]))
        self.assertIn("--disable=servicelb,local-storage", args)
        self.assertIn("--disable-network-policy", args)
        self.assertEqual(args[-1], "--node-label=x=y")

    def test_ports_are_published_on_host_loopback_only(self):
        args = k3s.container_args(WID, K3sConfig(ports=["8080:80", "8443:443"]), STATE, ROOT)
        self.assertEqual([a for a in args if a.startswith("--publish")], [
            "--publish=127.0.0.1::6443/tcp", "--publish=127.0.0.1:8080:80", "--publish=127.0.0.1:8443:443"])

    def test_malformed_settings_are_named_not_dropped(self):
        for field, value in (("cpus", "0"), ("cpus", "two"), ("memory", "4x"), ("memory", "0g"),
                             ("ports", ["80"]), ("ports", ["70000:80"]), ("ports", ["80:80", "80:81"]),
                             ("ports", "8080:80"), ("manifests", "../deploy"), ("manifests", "/etc"),
                             ("registry_mirrors", {"docker.io": []}),
                             ("registry_mirrors", {"docker.io": ["https://user:secret@mirror.example"]}),
                             ("registry_mirrors", {"docker.io": ["ftp://mirror.example"]}),
                             ("extra_args", ["cluster-cidr=x"]), ("ready_timeout", 5), ("ready_timeout", 30.5)):
            with self.subTest(field=field, value=value):
                problems = K3sConfig.from_dict({field: value}).validate()
                self.assertEqual(len(problems), 1)
                self.assertIn(f"tools.k3s.{field}", problems[0])

    def test_extended_settings_round_trip_sparsely(self):
        config = Config()
        config.tools.k3s.ports = ["8080:80"]
        config.tools.k3s.registry_mirrors = {"docker.io": ["https://mirror.example"]}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            save_config(config, path)
            self.assertEqual(json.loads(path.read_text())["tools"]["k3s"], {
                "ports": ["8080:80"], "registry_mirrors": {"docker.io": ["https://mirror.example"]}})
            self.assertEqual(load_config(path).tools.k3s, config.tools.k3s)

    def test_a_project_may_set_mirrors_but_never_credentials(self):
        from agent_box import project_config
        path = Path("/project/.agent-box.json")
        project_config.validate({"tools": {"k3s": {"registry_mirrors": {"docker.io": ["https://m.example"]}}}}, path)
        with self.assertRaisesRegex(AgentBoxError, "registry_mirrors"):
            project_config.validate({"tools": {"k3s": {"registry_mirrors": {
                "docker.io": ["https://user:secret@m.example"]}}}}, path)

    def test_a_mount_path_podman_would_split_is_refused(self):
        with self.assertRaisesRegex(AgentBoxError, "without a comma"):
            k3s.container_args(WID, K3sConfig(registry_mirrors={"docker.io": ["https://m.example"]}),
                               Path("/state,ro=false/k3s"), ROOT)

    def test_registries_yaml_lists_each_mirror_as_an_endpoint(self):
        text = k3s.registries_yaml(K3sConfig(registry_mirrors={"docker.io": ["https://m.example"]}))
        self.assertEqual(json.loads(text), {"mirrors": {"docker.io": {"endpoint": ["https://m.example"]}}})
        self.assertEqual(k3s.registries_yaml(K3sConfig()), "")


class ManifestsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "project"
        (self.root / "deploy").mkdir(parents=True)
        self.outside = Path(tmp.name) / "outside"
        self.outside.mkdir()

    def test_the_directory_is_mounted_read_only_beside_k3s_own_manifests(self):
        args = k3s.container_args(WID, K3sConfig(manifests="deploy"), STATE, self.root)
        self.assertIn(f"--mount=type=bind,src={(self.root / 'deploy').resolve()},"
                      f"dst={k3s.DATA_DIR}/server/manifests/agent-box,ro=true", args)

    def test_a_symlink_out_of_the_project_is_refused(self):
        """The mount must never hand the sidecar an arbitrary host directory."""
        (self.root / "escape").symlink_to(self.outside)
        with self.assertRaisesRegex(AgentBoxError, "outside the project"):
            k3s.container_args(WID, K3sConfig(manifests="escape"), STATE, self.root)

    def test_a_missing_directory_is_an_error(self):
        with self.assertRaisesRegex(AgentBoxError, "not a directory"):
            k3s.container_args(WID, K3sConfig(manifests="nope"), STATE, self.root)

class EnsureTests(SidecarTestCase):
    def test_first_start_creates_network_volume_and_container_and_renders_kubeconfig(self):
        port = k3s.ensure(WID, K3sConfig(enabled=True), self.state, self.root)
        self.assertEqual(port, 41234)
        self.assertEqual((self.podman.networks, self.podman.volumes), ({NAME}, {NAME}))
        self.assertTrue(self.podman.container["running"])
        path = k3s.kubeconfig_path(self.state)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        text = path.read_text()
        self.assertIn("server: https://127.0.0.1:41234\n", text)
        self.assertIn("current-context: agent-box\n", text)
        self.assertNotIn("default", text)
        self.assertIn("client-certificate-data: Q0VSVA==", text)

    def test_a_running_cluster_is_reused(self):
        k3s.ensure(WID, K3sConfig(enabled=True), self.state, self.root)
        self.podman.calls.clear()
        k3s.ensure(WID, K3sConfig(enabled=True), self.state, self.root)
        self.assertEqual(self.podman.did("create") + self.podman.did("start") + self.podman.did("rm"), [])

    def test_a_stopped_cluster_is_started_not_recreated(self):
        k3s.ensure(WID, K3sConfig(enabled=True), self.state, self.root)
        self.podman.container["running"] = False
        self.podman.calls.clear()
        k3s.ensure(WID, K3sConfig(enabled=True), self.state, self.root)
        self.assertEqual(len(self.podman.did("start")), 1)
        self.assertEqual(self.podman.did("create"), [])

    def test_a_changed_definition_recreates_the_container_on_the_same_volume(self):
        """Turning on Traefik must not cost the user their cluster's data."""
        k3s.ensure(WID, K3sConfig(enabled=True), self.state, self.root)
        self.podman.calls.clear()
        k3s.ensure(WID, K3sConfig(enabled=True, traefik=True), self.state, self.root)
        self.assertEqual(len(self.podman.did("rm")), 1)
        self.assertEqual(len(self.podman.did("create")), 1)
        self.assertEqual(self.podman.did("volume", "rm") + self.podman.did("volume", "create"), [])
        self.assertEqual(self.podman.volumes, {NAME})

    def test_outdated_means_a_sidecar_exists_with_other_settings(self):
        self.assertFalse(k3s.is_outdated(WID, K3sConfig(enabled=True), self.state, self.root))
        k3s.ensure(WID, K3sConfig(enabled=True), self.state, self.root)
        self.assertFalse(k3s.is_outdated(WID, K3sConfig(enabled=True, lifetime="box"), self.state, self.root))
        self.assertTrue(k3s.is_outdated(WID, K3sConfig(enabled=True, traefik=True), self.state, self.root))

    def test_a_host_without_delegated_controllers_is_refused_before_anything_is_created(self):
        self.podman.controllers = ["memory", "pids"]
        with self.assertRaisesRegex(AgentBoxError, "missing: cpu, cpuset, io"):
            k3s.ensure(WID, K3sConfig(enabled=True), self.state, self.root)
        self.assertEqual((self.podman.container, self.podman.networks, self.podman.volumes), (None, set(), set()))

    def test_waiting_is_bounded_and_the_failure_shows_the_sidecar_log(self):
        self.podman.ready = False
        with mock.patch.object(k3s.time, "monotonic", side_effect=[0, 0, 30]), \
                self.assertRaisesRegex(AgentBoxError, r"not ready within 30s(.|\n)*open /dev/kmsg"):
            k3s.ensure(WID, K3sConfig(enabled=True, ready_timeout=30), self.state, self.root)

    def test_a_hanging_probe_is_cut_off_so_the_deadline_still_holds(self):
        self.podman.probe_hangs = True
        with mock.patch.object(k3s.time, "monotonic", side_effect=[0, 25, 30]), \
                self.assertRaisesRegex(AgentBoxError, "not ready within 30s"):
            k3s.ensure(WID, K3sConfig(enabled=True, ready_timeout=30), self.state, self.root)
        self.assertEqual(self.podman.probe_timeouts, [5])
        self.assertEqual(self.podman.inspect_timeouts[-1], 5)

    def test_same_named_objects_agent_box_did_not_create_are_never_touched(self):
        """A user's own container, network or volume must not be reused or deleted."""
        self.podman.networks.add(NAME)
        self.podman.owner = ""
        with self.assertRaisesRegex(AgentBoxError, "not created by agent-box"):
            k3s.ensure(WID, K3sConfig(enabled=True), self.state, self.root)
        self.assertEqual(self.podman.did("network", "rm") + self.podman.did("create"), [])
        self.podman.container = {"running": True, "spec": "other"}
        for action in (lambda: k3s.remove(WID, self.state, data=True),
                       lambda: k3s.stop(WID, K3sConfig(enabled=True), self.state)):
            with self.assertRaisesRegex(AgentBoxError, "not created by agent-box"):
                action()
        self.assertEqual(self.podman.did("rm") + self.podman.did("stop"), [])

    def test_a_failing_podman_is_an_error_not_an_absent_cluster(self):
        """Reporting "absent" would let stop claim success while the cluster runs on."""
        self.podman.broken = True
        for action in (lambda: k3s.status(WID), lambda: k3s.stop(WID, K3sConfig(enabled=True), self.state),
                       k3s.all_workspaces):
            with self.assertRaisesRegex(AgentBoxError, "cannot connect to Podman"):
                action()

    def test_invalid_settings_are_refused_before_podman_is_touched(self):
        with self.assertRaisesRegex(AgentBoxError, "tools.k3s.memory"):
            k3s.ensure(WID, K3sConfig(enabled=True, memory="lots"), self.state, self.root)
        self.assertEqual(self.podman.calls, [])

    def test_mirrors_are_rendered_before_the_sidecar_that_mounts_them_is_created(self):
        config = K3sConfig(enabled=True, registry_mirrors={"docker.io": ["https://m.example"]})
        k3s.ensure(WID, config, self.state, self.root)
        self.assertIn("https://m.example", (self.state / k3s.REGISTRIES_FILE).read_text())
        k3s.remove(WID, self.state, data=True)
        self.assertFalse((self.state / k3s.REGISTRIES_FILE).exists())

    def test_a_sidecar_that_dies_fails_at_once_instead_of_waiting_out_the_timeout(self):
        self.podman.ready, self.podman.dies = False, True
        with self.assertRaisesRegex(AgentBoxError, "stopped while starting"):
            k3s.ensure(WID, K3sConfig(enabled=True, ready_timeout=1800), self.state, self.root)


class StopAndRemoveTests(SidecarTestCase):
    def test_stop_keeps_the_data_of_a_persistent_cluster(self):
        k3s.ensure(WID, K3sConfig(enabled=True), self.state, self.root)
        self.assertTrue(k3s.stop(WID, K3sConfig(enabled=True), self.state))
        self.assertEqual(k3s.status(WID), "stopped")
        self.assertEqual(self.podman.volumes, {NAME})
        self.assertTrue(k3s.kubeconfig_path(self.state).exists())

    def test_stop_of_a_throwaway_cluster_deletes_its_data_and_credential(self):
        config = K3sConfig(enabled=True, persistent_data=False)
        k3s.ensure(WID, config, self.state, self.root)
        k3s.stop(WID, config, self.state)
        self.assertEqual((self.podman.container, self.podman.networks, self.podman.volumes), (None, set(), set()))
        self.assertFalse(k3s.kubeconfig_path(self.state).exists())

    def test_remove_without_data_keeps_the_volume(self):
        k3s.ensure(WID, K3sConfig(enabled=True), self.state, self.root)
        k3s.remove(WID, self.state, data=False)
        self.assertEqual((self.podman.container, self.podman.networks, self.podman.volumes), (None, set(), {NAME}))

    def test_absent_objects_are_not_an_error(self):
        self.assertFalse(k3s.stop(WID, K3sConfig(enabled=True), self.state))
        k3s.remove(WID, self.state, data=True)
        self.assertEqual(k3s.status(WID), "absent")

    def test_uninstall_finds_every_workspace_that_still_owns_something(self):
        k3s.ensure(WID, K3sConfig(enabled=True), self.state, self.root)
        k3s.remove(WID, self.state, data=False)
        self.assertEqual(k3s.all_workspaces(), [WID])

    def test_status_distinguishes_a_ready_api(self):
        k3s.ensure(WID, K3sConfig(enabled=True), self.state, self.root)
        self.assertEqual(k3s.status(WID), "ready")


if __name__ == "__main__":
    unittest.main()
