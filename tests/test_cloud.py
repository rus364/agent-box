"""Cloud CLI profiles reach a box only as chosen, with their own sign-in and nothing else.

A profile reaches a whole cloud account, so the risks tested here are an
unchosen profile or its token leaking into the box, host history and logs
following it, a stale sign-in outliving its choice, a credential on the menu
or in Podman's argv, the host's own yc directory being touched, and
forwarded profiles without the yc tool passing silently for working ones.
"""
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_box import cloud, launch_options, runtime
from agent_box.config import Config
from agent_box.workspace import Workspace

CONFIG = """\
current: gb
profiles:
  dixy:
    federation-id: fed-id
    cloud-id: c1
  gb:
    subject-id: s1
    cloud-id: c2
  lenta:
    service-account-key:
      id: key-id
      private_key: |
        PLEASE DO NOT REMOVE THIS LINE!
        SECRET-KEY
    cloud-id: c3
"""


def cloud_config(profiles=(), enabled=True):
    config = Config()
    config.credentials.cloud.enabled = enabled
    config.credentials.cloud.yc = list(profiles)
    return config


class IsolatedYc(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.yc = self.root / "yandex-cloud"
        (self.yc / "credentials").mkdir(parents=True)
        (self.yc / "logs").mkdir()
        (self.yc / "config.yaml").write_text(CONFIG)
        (self.yc / "history.jsonl").write_text("SECRET-HISTORY\n")
        for name in ("dixy", "gb", "gb-rt"):
            (self.yc / "credentials" / name).write_text(f'{{"refresh_token": "SECRET-{name}"}}')
        for patcher in (mock.patch.object(cloud, "YC_CONFIG_DIR", self.yc),
                        mock.patch.object(cloud, "CLOUD_RUNTIME_DIR", self.root / "state")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def box(self, *parts):
        return cloud.runtime_dir("ws").joinpath(*parts)

    def host_files(self):
        return {path: path.read_bytes() for path in self.yc.rglob("*") if path.is_file()}


class ConfigTests(unittest.TestCase):
    def test_nothing_is_forwarded_until_asked_for(self):
        self.assertFalse(Config().credentials.cloud.active)
        self.assertFalse(cloud_config(["gb"], enabled=False).credentials.cloud.active)
        self.assertFalse(cloud_config([]).credentials.cloud.active)

    def test_a_profile_name_cannot_reach_outside_credentials(self):
        """The name becomes a path under credentials/; one that leaves it could copy any readable file."""
        loaded = Config.from_dict({"credentials": {"cloud": {"enabled": True,
                                                             "yc": {"profiles": ["gb", "../../.ssh/id_rsa", "..", "gb"]}}}})
        self.assertEqual(loaded.credentials.cloud.yc, ["gb"])
        self.assertEqual(loaded.to_dict()["credentials"]["cloud"], {"enabled": True, "yc": {"profiles": ["gb"]}, "aws": {"profiles": []}})


class ProfileTests(IsolatedYc):
    def test_each_profile_is_labelled_by_how_it_signs_in_without_a_value(self):
        """gb keeps its federation only in credentials/, as yc writes federated profiles it refreshed."""
        options, details, marks, problems = cloud.choices(["gone"])
        self.assertEqual(options, ["dixy", "gb", "lenta", "gone"])
        self.assertEqual(marks, ["federation", "federation", "service account key", "missing"])
        self.assertIn("refresh token is forwarded", details[0])
        self.assertIn("no longer in the host's yc config", details[3])
        self.assertEqual(problems, [])
        self.assertNotIn("SECRET", "\n".join([*details, *marks]))

    def test_a_host_without_yc_offers_nothing(self):
        (self.yc / "config.yaml").unlink()
        self.assertEqual(cloud.choices([]), ([], [], [], []))


class SyncTests(IsolatedYc):
    def test_only_chosen_profiles_and_their_sign_in_reach_the_box(self):
        """History, logs, other profiles' blocks and credentials, and a credentials file with no
        profile behind it all stay on the host; each chosen block is the host's, line for line."""
        before = self.host_files()
        self.assertEqual(cloud.sync(cloud_config(["lenta", "dixy"]), "ws"), [])
        rendered = self.box("config.yaml").read_text()
        self.assertEqual(rendered, "current: lenta\nprofiles:\n" + CONFIG[CONFIG.index("  lenta:"):]
                         + CONFIG[CONFIG.index("  dixy:"):CONFIG.index("  gb:")])
        self.assertEqual(sorted(path.name for path in self.box("credentials").iterdir()), ["dixy"])
        self.assertEqual(self.box("credentials/dixy").read_text(), (self.yc / "credentials/dixy").read_text())
        self.assertEqual(sorted(path.name for path in self.box().iterdir()), ["config.yaml", "credentials"])
        self.assertEqual(self.host_files(), before)

    def test_the_hosts_current_profile_stays_current_when_chosen(self):
        cloud.sync(cloud_config(["dixy", "gb"]), "ws")
        self.assertTrue(self.box("config.yaml").read_text().startswith("current: gb\n"))

    def test_copies_are_private_to_the_user(self):
        cloud.sync(cloud_config(["gb"]), "ws")
        for path, mode in ((self.box(), 0o700), (self.box("credentials"), 0o700),
                           (self.box("config.yaml"), 0o600), (self.box("credentials/gb"), 0o600)):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode, path)

    def test_an_unticked_or_vanished_profile_leaves_the_box_and_the_box_keeps_its_own_files(self):
        """yc in the box writes history and logs beside the copies; only forwarded sign-ins are managed."""
        cloud.sync(cloud_config(["dixy", "gb"]), "ws")
        self.box("history.jsonl").write_text("box history\n")
        (self.yc / "config.yaml").write_text(CONFIG.replace("  dixy:\n    federation-id: fed-id\n    cloud-id: c1\n", ""))
        problems = cloud.sync(cloud_config(["dixy", "gb"]), "ws")
        self.assertEqual([path.name for path in self.box("credentials").iterdir()], ["gb"])
        self.assertNotIn("dixy", self.box("config.yaml").read_text())
        self.assertEqual(self.box("history.jsonl").read_text(), "box history\n")
        self.assertEqual(len(problems), 1)
        self.assertIn("yc profile dixy is not in", problems[0])

    def test_links_the_box_plants_never_steer_a_launch_into_the_hosts_yc_directory(self):
        """The box writes its copy; links planted there must not lead the next launch's writes
        and deletions into the host's own yc directory, which would delete unchosen profiles'
        sign-ins on the host."""
        cloud.sync(cloud_config(["gb"]), "ws")
        before = self.host_files()
        import shutil
        shutil.rmtree(self.box("credentials"))
        self.box("credentials").symlink_to(self.yc / "credentials")
        self.box("config.yaml").unlink()
        self.box("config.yaml").symlink_to(self.yc / "config.yaml")
        self.assertEqual(cloud.sync(cloud_config(["gb"]), "ws"), [])
        self.assertEqual(self.host_files(), before)
        self.assertFalse(self.box("credentials").is_symlink() or self.box("config.yaml").is_symlink())
        self.assertEqual(sorted(path.name for path in self.box("credentials").iterdir()), ["gb"])
        self.box("credentials/gb").unlink()
        self.box("credentials/gb").symlink_to(self.yc / "credentials/dixy")
        self.box("credentials/dixy").mkdir()
        cloud.sync(cloud_config(["gb"]), "ws")
        self.assertEqual(self.host_files(), before)
        self.assertFalse(self.box("credentials/gb").is_symlink())
        self.assertEqual(sorted(path.name for path in self.box("credentials").iterdir()), ["gb"])

    def test_a_host_renewal_replaces_the_boxs_sign_in_at_the_next_launch(self):
        cloud.sync(cloud_config(["gb"]), "ws")
        self.box("credentials/gb").write_text("renewed in the box")
        (self.yc / "credentials/gb").write_text('{"refresh_token": "RENEWED-ON-HOST"}')
        cloud.sync(cloud_config(["gb"]), "ws")
        self.assertIn("RENEWED-ON-HOST", self.box("credentials/gb").read_text())

    def test_a_key_profile_drops_a_stale_federation_sign_in(self):
        """A profile switched from federation to a key would otherwise keep signing in as the old federation."""
        cloud.sync(cloud_config(["gb"]), "ws")
        (self.yc / "credentials/gb").unlink()
        self.assertEqual(cloud.sync(cloud_config(["gb"]), "ws"), [])
        self.assertFalse(self.box("credentials/gb").exists())

    @unittest.skipIf(os.geteuid() == 0, "root reads a file whatever its mode")
    def test_an_unreadable_sign_in_is_reported_and_its_old_copy_withdrawn(self):
        cloud.sync(cloud_config(["gb"]), "ws")
        source = self.yc / "credentials/gb"
        source.chmod(0)
        self.addCleanup(source.chmod, 0o600)
        problems = cloud.sync(cloud_config(["gb"]), "ws")
        self.assertFalse(self.box("credentials/gb").exists())
        self.assertEqual(problems, [f"yc profile gb: {source} unreadable (PermissionError); its sign-in not forwarded"])

    def test_turning_forwarding_off_removes_the_copies(self):
        for config in (cloud_config(["gb"], enabled=False), cloud_config()):
            cloud.sync(cloud_config(["gb"]), "ws")
            self.assertEqual(cloud.sync(config, "ws"), [])
            self.assertFalse(self.box().exists())
            self.assertEqual(cloud.mounts(config, "ws"), [])

    def test_a_host_without_profiles_says_so_instead_of_forwarding_nothing_silently(self):
        (self.yc / "config.yaml").unlink()
        problems = cloud.sync(cloud_config(["gb"]), "ws")
        self.assertEqual(len(problems), 1)
        self.assertIn("has no yc profiles", problems[0])
        self.assertFalse(self.box("config.yaml").exists())


class BoxTargetTests(unittest.TestCase):
    def test_the_copies_become_the_boxs_own_writable_yc_directory_and_never_argv(self):
        """yc keeps its renewed tokens, history and logs beside its config, so the mount is
        writable; the path is the host's, so yc and a nested launch find it where they look."""
        workspace = Workspace(Path("/project"), Path("/project"))
        state = runtime.workspace_runtime_state(workspace)
        args = runtime.container_lifetime_args(workspace, [], Path("/checkout"), cloud_config(["gb"]),
                                               runtime_state=state)
        target = runtime.HOST_HOME / ".config/yandex-cloud"
        self.assertEqual(cloud.YC_CONFIG_DIR, target)
        self.assertIn(f"type=bind,src={cloud.runtime_dir(state.root.name)},dst={target}", args)
        plain = runtime.container_lifetime_args(workspace, [], Path("/checkout"), Config(), runtime_state=state)
        self.assertFalse(any(f"dst={target}" in arg for arg in plain))


class ToolTests(unittest.TestCase):
    def test_forwarding_without_its_tool_group_is_named_at_launch(self):
        """config set or a one-session flag can leave the group off; the box would look set up and not be."""
        config = cloud_config(["gb"])
        config.credentials.kubernetes.enabled, config.credentials.kubernetes.files = True, ["prod"]
        config.box.tools.groups["kubernetes"] = False
        warnings = runtime.tool_warnings(config)
        self.assertEqual([warning.split(" need ", 1)[0] for warning in warnings],
                         ["Kubernetes configs", "Cloud CLI profiles (Yandex Cloud)"])
        self.assertIn("the yc tool group, which is off", warnings[1])
        self.assertIn("agent-box build", warnings[1])
        config.box.tools.groups["kubernetes"] = config.box.tools.groups["yc"] = True
        self.assertEqual(runtime.tool_warnings(config), [])
        self.assertEqual(runtime.tool_warnings(Config()), [])


class SessionSwitchTests(unittest.TestCase):
    def test_the_session_flag_overrides_the_saved_switch_both_ways(self):
        for argv, saved, expected in ((["--cloud-profiles"], False, True), (["--no-cloud-profiles"], True, False)):
            options, rest = launch_options.parse([*argv, "claude"])
            self.assertEqual(rest, ["claude"])
            config = cloud_config(["gb"], enabled=saved)
            launch_options.apply(config, options.overrides)
            self.assertIs(config.credentials.cloud.enabled, expected)
            self.assertEqual(config.credentials.cloud.yc, ["gb"])

    def test_turning_forwarding_on_with_nothing_chosen_says_what_is_missing(self):
        with self.assertRaisesRegex(launch_options.AgentBoxError, "--cloud-profiles has nothing to forward"):
            launch_options.apply(cloud_config(enabled=False), {"cloud-profiles": True})
        launch_options.apply(cloud_config(), {"cloud-profiles": False})


if __name__ == "__main__":
    unittest.main()
