"""What the container is allowed to see, as a function of agent-box config.

The rule encoded here: a host credential reaches the container only because
the user turned that switch on. These assertions run against the **generated
podman argv** rather than the helper functions, because that argv is what
actually determines exposure — an earlier version made the right decision in
`maybe_gh_token()` and then forwarded `GH_TOKEN`/`GITHUB_TOKEN` unconditionally
a few lines later, so a shell-exported token reached the container anyway.
Testing the decision function alone would not have caught that.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_box import runtime
from agent_box.config import Config
from agent_box.workspace import Workspace


def _argv(config, env=None, gh_token="gho_secrettoken"):
    """Full podman argv for one run, with `gh auth token` on the host faked."""
    tmp = tempfile.TemporaryDirectory()
    project = Path(tmp.name)
    base = {k: v for k, v in os.environ.items()
            if k not in ("GH_TOKEN", "GITHUB_TOKEN", "AGENT_BOX_GH_TOKEN",
                         "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "HTTPS_PROXY")}
    # KVM off: kvm_args() shells out to `podman info`, which the gh mock below
    # would otherwise answer, printing a confusing warning.
    base["AGENT_BOX_KVM"] = "0"
    base.update(env or {})
    try:
        state = project / "state"
        with mock.patch.dict(os.environ, base, clear=True), \
             mock.patch.object(runtime, "STATE_ROOT", state), \
             mock.patch.object(runtime, "BUILD_STATE", state / "build"), \
             mock.patch.object(runtime, "NOTIFY_STATE_ROOT", state / "notify"), \
             mock.patch.object(runtime, "NOTIFY_CREDENTIALS", state / "runtime/notify.json"), \
             mock.patch.object(runtime.shutil, "which", return_value="/usr/bin/gh"), \
             mock.patch.object(runtime.subprocess, "run",
                               return_value=mock.Mock(returncode=0, stdout=gh_token + "\n")):
            return runtime.container_argv(Workspace(project, project), [], project, ["claude"], config)
    finally:
        tmp.cleanup()


def _env_flags(argv):
    return [a for a in argv if a.startswith("--env=")]


class GitHubCredentialGatingTests(unittest.TestCase):
    def test_authenticated_host_alone_forwards_nothing(self):
        flags = _env_flags(_argv(Config()))
        self.assertFalse([f for f in flags if "GH_TOKEN" in f or "GITHUB_TOKEN" in f])

    def test_exported_gh_token_is_withheld_when_the_switch_is_off(self):
        """The regression this file exists for: a token in the launching
        shell is not consent to give it to an agent container."""
        argv = _argv(Config(), env={"GH_TOKEN": "gho_from_shell"})
        self.assertNotIn("--env=GH_TOKEN", argv)
        self.assertNotIn("gho_from_shell", " ".join(argv))

    def test_exported_github_token_is_withheld_when_the_switch_is_off(self):
        argv = _argv(Config(), env={"GITHUB_TOKEN": "ghp_from_shell"})
        self.assertNotIn("--env=GITHUB_TOKEN", argv)
        self.assertNotIn("ghp_from_shell", " ".join(argv))

    def test_enabled_forwards_the_host_derived_token(self):
        config = Config()
        config.github.gh_token = True
        argv = _argv(config)
        self.assertIn("--env=GH_TOKEN", argv)
        self.assertNotIn("gho_secrettoken", " ".join(argv))

    def test_enabled_forwards_an_exported_token_by_name_only(self):
        config = Config()
        config.github.gh_token = True
        argv = _argv(config, env={"GH_TOKEN": "gho_from_shell"})
        self.assertIn("--env=GH_TOKEN", argv)
        self.assertNotIn("gho_from_shell", " ".join(argv))

    def test_override_1_allows_for_one_run(self):
        argv = _argv(Config(), env={"AGENT_BOX_GH_TOKEN": "1"})
        self.assertIn("--env=GH_TOKEN", argv)
        self.assertNotIn("gho_secrettoken", " ".join(argv))

    def test_override_0_denies_even_when_configured_on(self):
        config = Config()
        config.github.gh_token = True
        argv = _argv(config, env={"AGENT_BOX_GH_TOKEN": "0", "GH_TOKEN": "gho_from_shell"})
        self.assertFalse([f for f in _env_flags(argv) if "GH_TOKEN" in f])

    def test_no_host_token_available_forwards_nothing(self):
        config = Config()
        config.github.gh_token = True
        self.assertFalse([f for f in _env_flags(_argv(config, gh_token="")) if "GH_TOKEN" in f])


class ProviderEnvTests(unittest.TestCase):
    def test_api_keys_withheld_by_default(self):
        argv = _argv(Config(), env={"ANTHROPIC_API_KEY": "sk-test"})
        self.assertNotIn("--env=ANTHROPIC_API_KEY", argv)
        self.assertNotIn("sk-test", " ".join(argv))

    def test_api_keys_forwarded_when_enabled(self):
        config = Config()
        config.providers.api_keys = True
        argv = _argv(config, env={"ANTHROPIC_API_KEY": "sk-test"})
        self.assertIn("--env=ANTHROPIC_API_KEY", argv)
        # By name, never by value: the argv is world-readable through /proc.
        self.assertNotIn("sk-test", " ".join(argv))

    def test_transport_settings_forwarded_by_default(self):
        argv = _argv(Config(), env={"HTTPS_PROXY": "http://proxy:3128"})
        self.assertIn("--env=HTTPS_PROXY", argv)

    def test_transport_settings_withheld_when_disabled(self):
        config = Config()
        config.providers.transport = False
        argv = _argv(config, env={"HTTPS_PROXY": "http://proxy:3128"})
        self.assertNotIn("--env=HTTPS_PROXY", argv)


class GitSeedingTests(unittest.TestCase):
    """git config exposure: identity only by default, whole file opt-in."""

    def _seed(self, config, git_output="Someone"):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        home = root / "home"
        (home / ".config/gh").mkdir(parents=True)
        (home / ".config/gh/hosts.yml").write_text("github.com:\n  oauth_token: gho_secret\n")
        (home / ".gitconfig").write_text(
            "[user]\n\tname = Someone\n\temail = a@b.c\n"
            "[credential]\n\thelper = store\n"
            '[http "https://internal/"]\n\textraHeader = Authorization: Bearer sekrit\n'
        )
        project = root / "project"
        project.mkdir()
        state = root / "state"
        with mock.patch.object(runtime, "STATE_ROOT", state), \
             mock.patch.object(runtime, "BUILD_STATE", state / "build"), \
             mock.patch.object(runtime, "NOTIFY_STATE_ROOT", state / "notify"), \
             mock.patch.object(runtime, "NOTIFY_CREDENTIALS", state / "runtime/notify.json"), \
             mock.patch.object(runtime, "HOST_HOME", home), \
             mock.patch.object(runtime.subprocess, "run",
                               return_value=mock.Mock(returncode=0, stdout=git_output + "\n")):
            runtime.build_mounts(Workspace(project, project), [], root, config)
        return state

    def test_default_seeds_identity_only(self):
        text = (self._seed(Config()) / "git/config").read_text()
        self.assertIn("Someone", text)
        self.assertNotIn("credential", text)
        self.assertNotIn("sekrit", text)

    def test_full_copy_is_opt_in_and_brings_everything(self):
        config = Config()
        config.git.full_config = True
        text = (self._seed(config) / "git/config").read_text()
        self.assertIn("sekrit", text)
        self.assertIn("helper = store", text)

    def test_both_off_leaves_an_empty_directory(self):
        config = Config()
        config.git.identity = False
        state = self._seed(config)
        self.assertTrue((state / "git").is_dir())
        self.assertEqual(list((state / "git").iterdir()), [])

    def test_identity_seeding_survives_an_unset_host_key(self):
        state = self._seed(Config(), git_output="")
        self.assertEqual(list((state / "git").iterdir()), [])

    def test_gh_config_not_copied_by_default(self):
        state = self._seed(Config())
        self.assertTrue((state / "gh").is_dir(), "the directory is still mounted")
        self.assertEqual(list((state / "gh").iterdir()), [], "but carries no host credentials")

    def test_gh_config_copied_when_enabled(self):
        config = Config()
        config.github.gh_config = True
        self.assertIn("gho_secret", (self._seed(config) / "gh/hosts.yml").read_text())


class NotificationCredentialMountTests(unittest.TestCase):
    """The container gets a generated two-key file, never config.json — and
    only while notifications are actually usable."""

    def _mounts(self, config):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        home = root / "home"
        home.mkdir()
        project = root / "project"
        project.mkdir()
        state = root / "state"
        config_file = root / "config.json"
        config_file.write_text('{"notifications": {"telegram_token": "123:SECRETTOKEN"}}')
        with mock.patch.object(runtime, "STATE_ROOT", state), \
             mock.patch.object(runtime, "BUILD_STATE", state / "build"), \
             mock.patch.object(runtime, "NOTIFY_STATE_ROOT", state / "notify"), \
             mock.patch.object(runtime, "NOTIFY_CREDENTIALS", state / "runtime/notify.json"), \
             mock.patch.object(runtime, "CONTAINER_NOTIFY_CREDENTIALS",
                               home / ".local/share/agent-box/runtime/notify.json"), \
             mock.patch.object(runtime, "HOST_HOME", home), \
             mock.patch.object(runtime.subprocess, "run",
                               return_value=mock.Mock(returncode=0, stdout="")):
            return runtime.build_mounts(Workspace(project, project), [], root, config), state

    def test_config_json_is_never_mounted(self):
        flags, _ = self._mounts(Config())
        self.assertFalse([f for f in flags if "config.json" in f])

    def test_disabled_notifications_expose_no_token_file(self):
        flags, state = self._mounts(Config())
        self.assertFalse([f for f in flags if "notify.json" in f])
        self.assertFalse((state / "runtime/notify.json").exists())

    def test_enabled_notifications_mount_the_credential_file_read_only(self):
        config = Config()
        config.notifications.enabled = True
        config.notifications.telegram_token = "123:SECRETTOKEN"
        config.notifications.telegram_chat_id = "555"
        flags, state = self._mounts(config)
        mount = next(f for f in flags if "notify.json" in f)
        self.assertIn("ro=true", mount)
        contents = (state / "runtime/notify.json").read_text()
        self.assertIn("123:SECRETTOKEN", contents)
        # Only the two keys the hook needs; no host-integration switches.
        self.assertNotIn("gh_token", contents)
        self.assertNotIn("telegram_channel", contents)

    def test_shell_or_channel_owner_can_skip_the_notification_credential(self):
        config = Config()
        config.notifications.enabled = True
        config.notifications.telegram_token = "123:SECRETTOKEN"
        config.notifications.telegram_chat_id = "555"
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        state = root / "state"
        home = root / "home"
        home.mkdir()
        project = root / "project"
        project.mkdir()
        with mock.patch.object(runtime, "STATE_ROOT", state), \
             mock.patch.object(runtime, "BUILD_STATE", state / "build"), \
             mock.patch.object(runtime, "NOTIFY_STATE_ROOT", state / "notify"), \
             mock.patch.object(runtime, "NOTIFY_CREDENTIALS", state / "runtime/notify.json"), \
             mock.patch.object(runtime, "HOST_HOME", home), \
             mock.patch.object(runtime.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="")):
            flags = runtime.build_mounts(Workspace(project, project), [], root, config,
                                         mount_notifications=False)
        self.assertFalse([flag for flag in flags if "notify.json" in flag])
        self.assertFalse((state / "runtime/notify.json").exists())

    def test_turning_notifications_off_removes_a_previously_written_file(self):
        config = Config()
        config.notifications.enabled = True
        config.notifications.telegram_token = "123:SECRETTOKEN"
        config.notifications.telegram_chat_id = "555"
        flags, state = self._mounts(config)
        self.assertTrue((state / "runtime/notify.json").exists())
        credentials = state / "runtime/notify.json"
        runtime.notify_module.sync_credentials(Config(), credentials)
        self.assertFalse(credentials.exists())


class BunCacheTests(unittest.TestCase):
    def test_bun_cache_points_into_the_isolated_cache(self):
        bun = [a for a in _argv(Config()) if a.startswith("--env=BUN_INSTALL_CACHE_DIR=")]
        self.assertEqual(len(bun), 1)
        self.assertTrue(bun[0].endswith("/bun"))


if __name__ == "__main__":
    unittest.main()
