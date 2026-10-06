"""Kubeconfigs reach a box only as chosen, whole, unmerged and labelled honestly.

A kubeconfig usually reaches a real cluster, so the risks tested here are an
unchosen file leaking into the box, a copy that is not the user's file, a
stale copy outliving its switch or its source, a credential in Podman's argv
or on the menu, and a file that cannot authenticate in the box passing
silently for one that can.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

from agent_box import kubeconfigs, launch_options, runtime
from agent_box.config import Config
from agent_box.workspace import Workspace

STATIC = """\
apiVersion: v1
clusters:
- cluster:
    certificate-authority-data: CA-DATA
    server: https://static.example:6443
  name: static
contexts:
- context:
    cluster: static
    user: robot
  name: default
current-context: default
kind: Config
users:
- name: robot
  user:
    token: SECRET-TOKEN
"""

EXEC = """\
apiVersion: v1
kind: Config
contexts:
  - context:
      cluster: managed
      user: yc
    name: yc-managed
  - name: second
    context:
      cluster: managed
users:
  - name: yc
    user:
      exec:
        apiVersion: client.authentication.k8s.io/v1beta1
        args:
        - k8s
        - create-token
        - --profile
        - dixy
        - --token
        - SECRET-ARG
        command: /home/someone/yandex-cloud/bin/yc
"""


def kube_config(files=(), enabled=True):
    config = Config()
    config.credentials.kubernetes.enabled = enabled
    config.credentials.kubernetes.files = list(files)
    return config


class IsolatedKube(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.kube = self.root / "kube"
        self.kube.mkdir()
        for patcher in (mock.patch.object(kubeconfigs, "KUBECONFIG_DIR", self.kube),
                        mock.patch.object(kubeconfigs, "KUBECONFIG_RUNTIME_DIR", self.root / "state")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def write(self, name, value):
        path = self.kube / name
        if isinstance(value, bytes):
            path.write_bytes(value)
        else:
            path.write_text(value)
        return path

    def copy(self, name, workspace="ws"):
        return kubeconfigs.runtime_dir(workspace) / name


class ConfigTests(unittest.TestCase):
    def test_nothing_is_forwarded_until_asked_for(self):
        kubernetes = Config().credentials.kubernetes
        self.assertEqual((kubernetes.enabled, kubernetes.files, kubernetes.active), (False, [], False))

    def test_a_saved_name_cannot_reach_outside_kube(self):
        """A name with a slash or a dot path would copy some other readable file into the box."""
        loaded = Config.from_dict({"credentials": {"kubernetes": {
            "enabled": True, "files": ["dev config", "../.ssh/id_ed25519", "/etc/shadow", "..", ".", "", 3, "dev config"]}}})
        self.assertEqual(loaded.credentials.kubernetes.files, ["dev config"])
        self.assertEqual(Config.from_dict(Config().to_dict()).credentials.kubernetes.to_dict(),
                         Config().credentials.kubernetes.to_dict())


class DescribeTests(unittest.TestCase):
    def test_a_static_file_is_labelled_without_its_secret(self):
        summary = kubeconfigs.describe(STATIC)
        self.assertEqual((summary.kubeconfig, summary.contexts, summary.auth, summary.warnings),
                         (True, ["default"], ["token"], []))

    def test_an_exec_user_is_named_by_command_and_profile_and_warned_about(self):
        """Other exec arguments may carry a credential, so only the profile is shown."""
        summary = kubeconfigs.describe(EXEC)
        self.assertEqual(summary.contexts, ["yc-managed", "second"])
        self.assertEqual(summary.auth, ["exec: yc --profile=dixy"])
        self.assertEqual(len(summary.warnings), 1)
        self.assertIn("does not run inside the box", summary.warnings[0])
        self.assertNotIn("SECRET", repr(summary))

    def test_files_the_kubeconfig_points_at_are_reported(self):
        """A copy in another directory loses the host files it names; the box may not have them."""
        text = STATIC.replace("certificate-authority-data: CA-DATA", "certificate-authority: ca.crt").replace(
            "token: SECRET-TOKEN", "client-certificate: /certs/client.crt\n    client-key: /certs/client.key")
        summary = kubeconfigs.describe(text)
        self.assertEqual(summary.auth, ["client certificate"])
        self.assertEqual([warning.split(" ", 1)[0] for warning in summary.warnings],
                         ["certificate-authority", "client-certificate", "client-key"])

    def test_an_auth_provider_is_named_and_warned_about(self):
        text = STATIC.replace("token: SECRET-TOKEN", "auth-provider:\n      config:\n        id-token: SECRET\n      name: oidc")
        summary = kubeconfigs.describe(text)
        self.assertEqual(summary.auth, ["auth-provider: oidc"])
        self.assertIn("may not authenticate", summary.warnings[0])

    def test_other_yaml_is_not_offered_as_a_kubeconfig(self):
        self.assertFalse(kubeconfigs.describe("apiVersion: v1\nkind: Pod\nmetadata:\n  name: x\n").kubeconfig)


class DiscoveryTests(IsolatedKube):
    def test_only_kubeconfigs_at_the_top_of_kube_are_offered(self):
        self.write("prod.kubeconfig", STATIC)
        self.write("dev", EXEC)
        self.write("notes.txt", "kind: Pod\n")
        self.write(".hidden", STATIC)
        (self.kube / "cache").mkdir()
        (self.kube / "cache/config").write_text(STATIC)
        self.assertEqual(list(kubeconfigs.discover().files), ["dev", "prod.kubeconfig"])

    def test_choices_mark_each_file_and_keep_vanished_choices_visible(self):
        self.write("prod.kubeconfig", STATIC)
        self.write("dev", EXEC)
        options, details, marks, problems = kubeconfigs.choices(["gone", "dev"])
        self.assertEqual(options, ["dev", "prod.kubeconfig", "gone"])
        self.assertEqual(marks, ["exec: yc --profile=dixy", "token", "missing"])
        self.assertIn("does not run inside the box", details[0])
        self.assertIn("no longer found", details[2])
        self.assertNotIn("SECRET", "\n".join([*details, *marks, *problems]))

    def test_a_missing_kube_directory_offers_nothing(self):
        with mock.patch.object(kubeconfigs, "KUBECONFIG_DIR", self.root / "absent"):
            self.assertEqual(kubeconfigs.choices([]), ([], [], [], []))


class SyncTests(IsolatedKube):
    def test_only_chosen_files_are_copied_byte_for_byte_and_privately(self):
        exact = STATIC.encode() + b"# \xff not UTF-8\r\n"
        self.write("prod.kubeconfig", exact)
        self.write("other.kubeconfig", STATIC)
        self.assertEqual(kubeconfigs.sync(kube_config(["prod.kubeconfig"]), "ws"), [])
        directory = kubeconfigs.runtime_dir("ws")
        self.assertEqual([path.name for path in directory.iterdir()], ["prod.kubeconfig"])
        self.assertEqual(self.copy("prod.kubeconfig").read_bytes(), exact)
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.copy("prod.kubeconfig").stat().st_mode & 0o777, 0o600)

    def test_a_changed_host_file_replaces_the_copy_inside_the_mounted_directory(self):
        """The box mounts the directory, so only a replace within it reaches a running box."""
        self.write("prod.kubeconfig", STATIC)
        kubeconfigs.sync(kube_config(["prod.kubeconfig"]), "ws")
        directory = kubeconfigs.runtime_dir("ws").stat().st_ino
        self.write("prod.kubeconfig", STATIC.replace("SECRET-TOKEN", "RENEWED"))
        kubeconfigs.sync(kube_config(["prod.kubeconfig"]), "ws")
        self.assertIn("RENEWED", self.copy("prod.kubeconfig").read_text())
        self.assertEqual(kubeconfigs.runtime_dir("ws").stat().st_ino, directory)

    def test_unticked_and_vanished_files_leave_the_box(self):
        self.write("a", STATIC)
        self.write("b", STATIC)
        kubeconfigs.sync(kube_config(["a", "b"]), "ws")
        (self.kube / "b").unlink()
        problems = kubeconfigs.sync(kube_config(["b"]), "ws")
        self.assertEqual(list(kubeconfigs.runtime_dir("ws").iterdir()), [])
        self.assertEqual(len(problems), 1)
        self.assertIn("Kubeconfig b is not in", problems[0])

    @unittest.skipIf(os.geteuid() == 0, "root reads a file whatever its mode")
    def test_an_unreadable_file_is_reported_and_its_old_copy_withdrawn(self):
        path = self.write("a", STATIC)
        kubeconfigs.sync(kube_config(["a"]), "ws")
        path.chmod(0)
        self.addCleanup(path.chmod, 0o600)
        problems = kubeconfigs.sync(kube_config(["a"]), "ws")
        self.assertFalse(self.copy("a").exists())
        self.assertEqual(problems, [f"Kubeconfig {path}: unreadable (PermissionError); not forwarded"])

    def test_what_cannot_authenticate_in_the_box_is_reported_without_failing(self):
        self.write("dev", EXEC)
        problems = kubeconfigs.sync(kube_config(["dev"]), "ws")
        self.assertTrue(self.copy("dev").exists())
        self.assertEqual(len(problems), 1)
        self.assertTrue(problems[0].startswith("Kubeconfig dev: exec: yc --profile=dixy does not run"))

    def test_turning_forwarding_off_removes_the_copies(self):
        self.write("a", STATIC)
        for config in (kube_config(["a"], enabled=False), kube_config()):
            kubeconfigs.sync(kube_config(["a"]), "ws")
            self.assertEqual(kubeconfigs.sync(config, "ws"), [])
            self.assertFalse(kubeconfigs.runtime_dir("ws").exists())
            self.assertEqual((kubeconfigs.mounts(config, "ws"), kubeconfigs.container_env(config)), ([], []))


class SourceLocationTests(unittest.TestCase):
    def test_a_nested_launch_offers_the_copies_its_box_was_given(self):
        env = {**os.environ, "AGENT_BOX_KUBECONFIGS": "/copies"}
        shown = subprocess.run([sys.executable, "-c", "from agent_box import paths; print(paths.KUBECONFIG_DIR)"],
                               cwd=Path(kubeconfigs.__file__).parent.parent, env=env, capture_output=True,
                               text=True, check=True).stdout
        self.assertEqual(shown.strip(), "/copies")


class BoxTests(IsolatedKube):
    def test_the_box_gets_a_read_only_directory_and_its_path_never_kubeconfig_or_a_credential(self):
        """KUBECONFIG belongs to k3s; a merged list would resolve shared names to the wrong cluster."""
        self.write("prod.kubeconfig", STATIC)
        workspace = Workspace(Path("/project"), Path("/project"))
        state = runtime.workspace_runtime_state(workspace)
        args = runtime.container_lifetime_args(workspace, [], Path("/checkout"), kube_config(["prod.kubeconfig"]),
                                               runtime_state=state)
        target = kubeconfigs.CONTAINER_KUBECONFIG_RUNTIME_DIR
        self.assertIn(f"type=bind,src={kubeconfigs.runtime_dir(state.root.name)},dst={target},ro=true", args)
        self.assertIn(f"--env=AGENT_BOX_KUBECONFIGS={target}", args)
        self.assertFalse(any("SECRET" in arg or arg.startswith("--env=KUBECONFIG=") for arg in args))
        plain = runtime.container_lifetime_args(workspace, [], Path("/checkout"), Config(), runtime_state=state)
        self.assertFalse(any(str(target) in arg for arg in plain))

    def test_a_launch_refused_by_a_running_box_leaves_its_copies_alone(self):
        """--no-kubeconfigs against a running box with copies must not delete them under it."""
        self.write("a", STATIC)
        kubeconfigs.sync(kube_config(["a"]), "ws")
        spec = mock.Mock(creation_config=kube_config(enabled=False), runtime_state=mock.Mock(root=Path("/state/ws")))
        with mock.patch.object(runtime, "workspace_transition_lock", side_effect=lambda _workspace: nullcontext()), \
             mock.patch.object(runtime, "confirm_k3s"), \
             mock.patch.object(runtime, "ensure_workspace_container",
                               side_effect=runtime.AgentBoxError("incompatible")):
            with self.assertRaises(runtime.AgentBoxError):
                runtime.launch_in_workspace_container(spec, mock.Mock(), ["bash"], [])
        self.assertTrue(self.copy("a").exists())


class SessionSwitchTests(unittest.TestCase):
    def test_the_session_flag_overrides_the_saved_switch_both_ways(self):
        for argv, saved, expected in ((["--kubeconfigs"], False, True), (["--no-kubeconfigs"], True, False)):
            options, rest = launch_options.parse([*argv, "claude"])
            self.assertEqual(rest, ["claude"])
            config = kube_config(["a"], enabled=saved)
            launch_options.apply(config, options.overrides)
            self.assertIs(config.credentials.kubernetes.enabled, expected)
            self.assertEqual(config.credentials.kubernetes.files, ["a"])

    def test_turning_forwarding_on_with_nothing_chosen_says_what_is_missing(self):
        with self.assertRaisesRegex(launch_options.AgentBoxError, "--kubeconfigs has nothing to forward"):
            launch_options.apply(kube_config(enabled=False), {"kubeconfigs": True})
        launch_options.apply(kube_config(), {"kubeconfigs": False})


if __name__ == "__main__":
    unittest.main()
