"""Registry and Helm logins reach a box only as chosen, each from its own source.

Forwarding copies live credentials, so the risks are the ones tested here:
an unchosen login leaking into the box, one source's entries landing in
another's file, a stale copy outliving its switch, a credential in Podman's
argv, and a renewed host login never reaching a running box.
"""
import json
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

from agent_box import launch_options, registries, runtime
from agent_box.config import Config
from agent_box.workspace import Workspace

REPOSITORIES = """\
apiVersion: ""
generated: "2026-01-01T00:00:00Z"
repositories:
- caFile: ""
  certFile: ""
  insecure_skip_tls_verify: false
  keyFile: ""
  name: private
  pass_credentials_all: true
  password: "p\\"a#ss\\x41"
  url: https://user:secret@charts.example/private
  username: robot
- name: public
  password: ''
  url: https://charts.example/public  # a comment
  username: 'it''s'
"""


def registry_config(hosts=(), repositories=(), enabled=True):
    config = Config()
    config.credentials.registries.enabled = enabled
    config.credentials.registries.hosts = list(hosts)
    config.credentials.registries.helm_repositories = list(repositories)
    return config


class IsolatedSources(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.runtime_auth = self.root / "run/containers/auth.json"
        self.config_auth = self.root / "config/containers/auth.json"
        self.helm_auth = self.root / "config/helm/registry/config.json"
        self.repositories = self.root / "config/helm/repositories.yaml"
        for patcher in (
                mock.patch.object(registries, "CONTAINERS_AUTH_FILES", (self.runtime_auth, self.config_auth)),
                mock.patch.object(registries, "HELM_REGISTRY_CONFIG", self.helm_auth),
                mock.patch.object(registries, "HELM_REPOSITORY_CONFIG", self.repositories),
                mock.patch.object(registries, "REGISTRY_RUNTIME_DIR", self.root / "state")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value if isinstance(value, str) else json.dumps(value))

    def rendered(self, name, workspace="ws"):
        return json.loads((registries.runtime_dir(workspace) / name).read_text())


class ConfigTests(unittest.TestCase):
    def test_nothing_is_forwarded_until_asked_for(self):
        registries_config = Config().credentials.registries
        self.assertEqual((registries_config.enabled, registries_config.hosts, registries_config.helm_repositories),
                         (False, [], []))
        self.assertFalse(registries_config.active)

    def test_round_trips_and_drops_entries_no_registry_could_match(self):
        loaded = Config.from_dict({"credentials": {"registries": {
            "enabled": True, "hosts": ["cr.yandex", " cr.yandex ", "", "two words", 7],
            "helm_repositories": ["private"]}}}).credentials.registries
        self.assertEqual((loaded.enabled, loaded.hosts, loaded.helm_repositories), (True, ["cr.yandex"], ["private"]))
        self.assertEqual(Config.from_dict(Config().to_dict()).credentials.registries.to_dict(),
                         Config().credentials.registries.to_dict())


class FilteringTests(IsolatedSources):
    def test_only_chosen_hosts_reach_each_tool_from_its_own_source(self):
        """Helm and podman keep different logins; merging them would hand each the other's."""
        self.write(self.runtime_auth, {"auths": {"cr.yandex": {"auth": "runtime"},
                                                 "cr.yandex/folder/app": {"auth": "scoped"},
                                                 "other.example": {"auth": "unchosen"}}})
        self.write(self.config_auth, {"auths": {"cr.yandex": {"auth": "stale"}, "registry.b": {"auth": "saved"}}})
        self.write(self.helm_auth, {"auths": {"https://registry.b/v2/": {"auth": "helm"},
                                              "other.example": {"auth": "unchosen"}}})
        problems = registries.sync(registry_config(["cr.yandex", "registry.b"]), "ws")
        self.assertEqual(problems, [])
        self.assertEqual(self.rendered("auth.json"), {"auths": {
            "cr.yandex": {"auth": "runtime"}, "cr.yandex/folder/app": {"auth": "scoped"},
            "registry.b": {"auth": "saved"}}})
        self.assertEqual(self.rendered("helm-registry.json"), {"auths": {"https://registry.b/v2/": {"auth": "helm"}}})
        self.assertFalse((registries.runtime_dir("ws") / "helm-repositories.yaml").exists())

    def test_chosen_helm_repositories_keep_their_credentials_and_types(self):
        self.write(self.repositories, REPOSITORIES)
        self.assertEqual(registries.sync(registry_config(repositories=["private"]), "ws"), [])
        rendered = self.rendered("helm-repositories.yaml")
        self.assertEqual([entry["name"] for entry in rendered["repositories"]], ["private"])
        entry = rendered["repositories"][0]
        self.assertEqual((entry["password"], entry["username"], entry["insecure_skip_tls_verify"],
                          entry["pass_credentials_all"]), ('p"a#ssA', "robot", False, True))
        self.assertFalse((registries.runtime_dir("ws") / "auth.json").exists())

    def test_quoted_and_commented_values_read_as_helm_wrote_them(self):
        public = {entry["name"]: entry for entry in registries.parse_repositories(REPOSITORIES)}["public"]
        self.assertEqual((public["password"], public["username"], public["url"]),
                         ("", "it's", "https://charts.example/public"))

    def test_yaml_beyond_what_helm_writes_is_refused_rather_than_half_read(self):
        self.write(self.repositories, "repositories:\n- name: private\n  password: |\n    secret\n")
        problems = registries.sync(registry_config(repositories=["private"]), "ws")
        self.assertEqual(len(problems), 1)
        self.assertIn("line 3", problems[0])
        self.assertEqual(self.rendered("helm-repositories.yaml")["repositories"], [])

    def test_rendered_copies_are_private_to_the_user(self):
        self.write(self.runtime_auth, {"auths": {"cr.yandex": {"auth": "runtime"}}})
        registries.sync(registry_config(["cr.yandex"]), "ws")
        directory = registries.runtime_dir("ws")
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual({path.stat().st_mode & 0o777 for path in directory.iterdir()}, {0o600})

    def test_a_renewed_host_login_replaces_the_copy_inside_the_mounted_directory(self):
        """The box mounts the directory, so only a replace within it reaches a running box."""
        self.write(self.runtime_auth, {"auths": {"cr.yandex": {"auth": "old"}}})
        registries.sync(registry_config(["cr.yandex"]), "ws")
        directory = registries.runtime_dir("ws").stat().st_ino
        self.write(self.runtime_auth, {"auths": {"cr.yandex": {"auth": "renewed"}}})
        registries.sync(registry_config(["cr.yandex"]), "ws")
        self.assertEqual(self.rendered("auth.json"), {"auths": {"cr.yandex": {"auth": "renewed"}}})
        self.assertEqual(registries.runtime_dir("ws").stat().st_ino, directory)

    def test_turning_forwarding_off_removes_the_copies(self):
        self.write(self.runtime_auth, {"auths": {"cr.yandex": {"auth": "runtime"}}})
        registries.sync(registry_config(["cr.yandex"]), "ws")
        for config in (registry_config(["cr.yandex"], enabled=False), registry_config()):
            registries.sync(registry_config(["cr.yandex"]), "ws")
            self.assertEqual(registries.sync(config, "ws"), [])
            self.assertFalse(registries.runtime_dir("ws").exists())
            self.assertEqual((registries.mounts(config, "ws"), registries.container_env(config)), ([], []))


class DiagnosticTests(IsolatedSources):
    def test_what_cannot_be_forwarded_is_reported_without_failing_the_launch(self):
        self.write(self.runtime_auth, {"auths": {"docker.io": {}}, "credsStore": "secretservice",
                                       "credHelpers": {"cr.yandex": "yc"}})
        self.write(self.helm_auth, "{not json")
        self.write(self.repositories, REPOSITORIES)
        problems = registries.sync(registry_config(["cr.yandex", "docker.io", "gone.example"], ["missing"]), "ws")
        self.assertEqual(len(problems), 5, problems)
        text = "\n".join(problems)
        self.assertIn(str(self.helm_auth), text)
        self.assertIn("Registry cr.yandex is logged in through a credential helper", text)
        self.assertIn("Registry docker.io is logged in through a credential helper", text)
        self.assertIn("Registry gone.example has no stored login", text)
        self.assertIn("Helm repository missing is not in", text)
        self.assertEqual(self.rendered("auth.json"), {"auths": {}})

    def test_missing_sources_forward_nothing_and_say_so(self):
        problems = registries.sync(registry_config(["cr.yandex"], ["private"]), "ws")
        self.assertEqual([problem.split(" ", 2)[:2] for problem in problems],
                         [["Registry", "cr.yandex"], ["Helm", "repository"]])


class MenuTests(IsolatedSources):
    def test_choices_show_where_each_login_lives_and_keep_vanished_choices_visible(self):
        self.write(self.runtime_auth, {"auths": {"cr.yandex": {"auth": "SECRET-A"}}, "credHelpers": {"gcr.io": "gcloud"}})
        self.write(self.helm_auth, {"auths": {"cr.yandex": {"auth": "SECRET-B"}, "registry.b": {"auth": "SECRET-C"}}})
        self.write(self.repositories, REPOSITORIES)
        hosts, host_details, problems = registries.choices("hosts", ["old.example", "cr.yandex"])
        self.assertEqual(hosts, ["cr.yandex", "gcr.io", "registry.b", "old.example"])
        self.assertIn(str(self.runtime_auth), host_details[0])
        self.assertIn(str(self.helm_auth), host_details[0])
        self.assertIn("credential helper, not forwarded", host_details[1])
        self.assertIn("no longer found", host_details[3])
        names, name_details, _ = registries.choices("helm_repositories", [])
        self.assertEqual(names, ["private", "public"])
        self.assertIn("https://charts.example/private", name_details[0])
        shown = "\n".join([*host_details, *name_details, *problems])
        for secret in ("SECRET", "secret@", "user:", 'p\\"a'):
            self.assertNotIn(secret, shown)

    def test_an_unreadable_source_is_named_rather_than_looking_empty(self):
        self.write(self.config_auth, "[]")
        _, _, problems = registries.choices("hosts", [])
        self.assertEqual(len(problems), 1)
        self.assertIn(str(self.config_auth), problems[0])


class BoxTests(IsolatedSources):
    def test_the_box_gets_paths_and_a_read_only_directory_never_a_credential(self):
        self.write(self.runtime_auth, {"auths": {"cr.yandex": {"auth": "SECRET"}}})
        config = registry_config(["cr.yandex"], ["private"])
        workspace = Workspace(Path("/project"), Path("/project"))
        state = runtime.workspace_runtime_state(workspace)
        args = runtime.container_lifetime_args(workspace, [], Path("/checkout"), config, runtime_state=state)
        target = registries.CONTAINER_REGISTRY_RUNTIME_DIR
        self.assertIn(f"type=bind,src={registries.runtime_dir(state.root.name)},dst={target},ro=true", args)
        for name, file in (("REGISTRY_AUTH_FILE", "auth.json"), ("HELM_REGISTRY_CONFIG", "helm-registry.json"),
                           ("HELM_REPOSITORY_CONFIG", "helm-repositories.yaml")):
            self.assertIn(f"--env={name}={target / file}", args)
        self.assertFalse(any("SECRET" in arg for arg in args))
        plain = runtime.container_lifetime_args(workspace, [], Path("/checkout"), Config(), runtime_state=state)
        self.assertFalse(any(str(target) in arg for arg in plain))

    def test_a_launch_refused_by_a_running_box_leaves_its_logins_alone(self):
        """--no-registries against a running box with logins must not delete them under it."""
        self.write(self.runtime_auth, {"auths": {"cr.yandex": {"auth": "runtime"}}})
        registries.sync(registry_config(["cr.yandex"]), "ws")
        spec = mock.Mock(creation_config=registry_config(enabled=False), runtime_state=mock.Mock(root=Path("/state/ws")))
        with mock.patch.object(runtime, "workspace_transition_lock", side_effect=lambda _workspace: nullcontext()), \
             mock.patch.object(runtime, "confirm_k3s"), \
             mock.patch.object(runtime, "ensure_workspace_container",
                               side_effect=runtime.AgentBoxError("incompatible")):
            with self.assertRaises(runtime.AgentBoxError):
                runtime.launch_in_workspace_container(spec, mock.Mock(), ["bash"], [])
        self.assertEqual(self.rendered("auth.json"), {"auths": {"cr.yandex": {"auth": "runtime"}}})


class SessionSwitchTests(unittest.TestCase):
    def test_the_session_flag_overrides_the_saved_switch_both_ways(self):
        for argv, saved, expected in ((["--registries"], False, True), (["--no-registries"], True, False)):
            options, rest = launch_options.parse([*argv, "claude"])
            self.assertEqual(rest, ["claude"])
            config = registry_config(["cr.yandex"], enabled=saved)
            launch_options.apply(config, options.overrides)
            self.assertIs(config.credentials.registries.enabled, expected)
            self.assertEqual(config.credentials.registries.hosts, ["cr.yandex"])


if __name__ == "__main__":
    unittest.main()
