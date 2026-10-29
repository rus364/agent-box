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
    project = Path(tmp.name) / "project"
    project.mkdir()
    base = {k: v for k, v in os.environ.items()
            if k not in ("GH_TOKEN", "GITHUB_TOKEN",
                         "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "HTTPS_PROXY")}
    base.update(env or {})
    try:
        state = Path(tmp.name) / "state"
        with mock.patch.dict(os.environ, base, clear=True), \
             mock.patch.object(runtime, "STATE_ROOT", state), \
             mock.patch.object(runtime, "BUILD_STATE", state / "build"), \
             mock.patch.object(runtime, "NOTIFY_STATE_ROOT", state / "notify"), \
             mock.patch.object(runtime, "TRACKER_RUNTIME_DIR", state / "runtime/tracker"), \
             mock.patch.object(runtime, "POLICY_RUNTIME_DIR", state / "runtime/policy"), \
             mock.patch.object(runtime, "WORKFLOW_STATE", state / "workflow"), \
             mock.patch.object(runtime, "CLAUDE_HOME", state / "claude"), \
             mock.patch.object(runtime, "CODEX_HOME", state / "codex"), \
             mock.patch.object(runtime, "XDG_CACHE_HOME", state / "cache"), \
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
        config.credentials.github.token = True
        argv = _argv(config)
        self.assertIn("--env=GH_TOKEN", argv)
        self.assertNotIn("gho_secrettoken", " ".join(argv))

    def test_enabled_forwards_an_exported_token_by_name_only(self):
        config = Config()
        config.credentials.github.token = True
        argv = _argv(config, env={"GH_TOKEN": "gho_from_shell"})
        self.assertIn("--env=GH_TOKEN", argv)
        self.assertNotIn("gho_from_shell", " ".join(argv))

    def test_launch_flag_allows_for_one_run(self):
        from agent_box import launch_options
        argv = _argv(launch_options.apply(Config(), {"gh-token": True}))
        self.assertIn("--env=GH_TOKEN", argv)
        self.assertNotIn("gho_secrettoken", " ".join(argv))

    def test_launch_flag_denies_even_when_configured_on(self):
        from agent_box import launch_options
        config = Config()
        config.credentials.github.token = True
        argv = _argv(launch_options.apply(config, {"gh-token": False}), env={"GH_TOKEN": "gho_from_shell"})
        self.assertFalse([f for f in _env_flags(argv) if "GH_TOKEN" in f])

    def test_no_host_token_available_forwards_nothing(self):
        config = Config()
        config.credentials.github.token = True
        self.assertFalse([f for f in _env_flags(_argv(config, gh_token="")) if "GH_TOKEN" in f])


class ProviderEnvTests(unittest.TestCase):
    def test_api_keys_withheld_by_default(self):
        argv = _argv(Config(), env={"ANTHROPIC_API_KEY": "sk-test"})
        self.assertNotIn("--env=ANTHROPIC_API_KEY", argv)
        self.assertNotIn("sk-test", " ".join(argv))

    def test_api_keys_forwarded_when_enabled(self):
        config = Config()
        config.agents.host_env.api_keys = True
        argv = _argv(config, env={"ANTHROPIC_API_KEY": "sk-test"})
        self.assertIn("--env=ANTHROPIC_API_KEY", argv)
        # By name, never by value: the argv is world-readable through /proc.
        self.assertNotIn("sk-test", " ".join(argv))

    def test_transport_settings_forwarded_by_default(self):
        argv = _argv(Config(), env={"HTTPS_PROXY": "http://proxy:3128"})
        self.assertIn("--env=HTTPS_PROXY", argv)

    def test_transport_settings_withheld_when_disabled(self):
        config = Config()
        config.agents.host_env.transport = False
        argv = _argv(config, env={"HTTPS_PROXY": "http://proxy:3128"})
        self.assertNotIn("--env=HTTPS_PROXY", argv)


class GitSeedingTests(unittest.TestCase):
    """git config exposure: identity only by default, whole file opt-in."""

    def _seed(self, config, git_output="Someone", *, glab_symlink=False):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        home = root / "home"
        (home / ".config/gh").mkdir(parents=True)
        (home / ".config/gh/hosts.yml").write_text("github.com:\n  oauth_token: gho_secret\n")
        glab_home = home / ".config/glab-cli"
        glab_home.mkdir(mode=0o755)
        (glab_home / "config.yml").write_text("hosts:\n  gitlab.example:\n    token: glpat_secret\n")
        if glab_symlink:
            dotfiles = home / "dotfiles"
            dotfiles.mkdir()
            (glab_home / "config.yml").rename(dotfiles / "glab.yml")
            (glab_home / "config.yml").symlink_to(dotfiles / "glab.yml")
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
             mock.patch.object(runtime, "TRACKER_RUNTIME_DIR", state / "runtime/tracker"), \
             mock.patch.object(runtime, "POLICY_RUNTIME_DIR", state / "runtime/policy"), \
             mock.patch.object(runtime, "WORKFLOW_STATE", state / "workflow"), \
             mock.patch.object(runtime, "CLAUDE_HOME", state / "claude"), \
             mock.patch.object(runtime, "CODEX_HOME", state / "codex"), \
             mock.patch.object(runtime, "XDG_CACHE_HOME", state / "cache"), \
             mock.patch.object(runtime, "NOTIFY_CREDENTIALS", state / "runtime/notify.json"), \
             mock.patch.object(runtime, "HOST_HOME", home), \
             mock.patch.object(runtime, "glab_config_dir", return_value=glab_home), \
             mock.patch.object(runtime.subprocess, "run",
                               return_value=mock.Mock(returncode=0, stdout=git_output + "\n")):
            runtime.prepare_container_lifetime_state(config, mount_notifications=False)
            runtime.build_mounts(Workspace(project, project), [], root, config)
        return state

    def test_default_seeds_identity_only(self):
        text = (self._seed(Config()) / "git/config").read_text()
        self.assertIn("Someone", text)
        self.assertNotIn("credential", text)
        self.assertNotIn("sekrit", text)

    def test_full_copy_is_opt_in_and_brings_everything(self):
        config = Config()
        config.credentials.git.full_config = True
        text = (self._seed(config) / "git/config").read_text()
        self.assertIn("sekrit", text)
        self.assertIn("helper = store", text)

    def test_both_off_leaves_an_empty_directory(self):
        config = Config()
        config.credentials.git.identity = False
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
        config.credentials.github.config = True
        self.assertIn("gho_secret", (self._seed(config) / "gh/hosts.yml").read_text())

    def test_glab_is_empty_until_explicitly_enabled(self):
        """An authenticated host must not give every new box GitLab credentials."""
        with mock.patch.object(runtime.glab_config, "snapshot_config") as snapshot:
            state = self._seed(Config())
        snapshot.assert_not_called()
        self.assertEqual(list((state / "glab").iterdir()), [])

    def test_glab_mount_receives_only_the_rendered_config_and_preserves_the_host(self):
        """Never copy the original config before replacing it: it may contain a rotating refresh token."""
        config = Config()
        config.credentials.gitlab.config = True
        with mock.patch.object(runtime.glab_config, "snapshot_config", return_value="rendered-access-only\n"):
            state = self._seed(config)
        self.assertEqual((state / "glab/config.yml").read_text(), "rendered-access-only\n")
        self.assertIn("glpat_secret", (state.parent / "home/.config/glab-cli/config.yml").read_text())
        self.assertEqual((state / "glab/config.yml").stat().st_mode & 0o777, 0o600)

    def test_glab_copy_is_private_and_host_files_are_not_writable_through_it(self):
        """The source directory's permissive mode must not leak the copied token."""
        config = Config()
        config.credentials.gitlab.config = True
        state = self._seed(config)
        copied = state / "glab/config.yml"
        original = state.parent / "home/.config/glab-cli/config.yml"
        self.assertIn("glpat_secret", copied.read_text())
        self.assertEqual((state / "glab").stat().st_mode & 0o777, 0o700)
        copied.write_text("host: changed.example\n")
        self.assertIn("glpat_secret", original.read_text())

    def test_symlinked_glab_config_becomes_an_independent_snapshot(self):
        """Dotfile-managed credentials must work without retaining a writable link to the host."""
        config = Config()
        config.credentials.gitlab.config = True
        state = self._seed(config, glab_symlink=True)
        source = state.parent / "home/.config/glab-cli"
        original = state.parent / "home/dotfiles/glab.yml"
        second = state / "second-glab"
        runtime.seed_state_dir(second, source)
        first_config, second_config = state / "glab/config.yml", second / "config.yml"
        self.assertFalse(first_config.is_symlink())
        self.assertFalse(second_config.is_symlink())
        first_config.write_text("host: changed.example\n")
        self.assertIn("glpat_secret", original.read_text())
        self.assertIn("glpat_secret", second_config.read_text())
        original.unlink()
        self.assertIn("glpat_secret", second_config.read_text())


class ConfigurationSnapshotTests(unittest.TestCase):
    def test_read_only_sources_become_private_writable_copies_without_losing_executable_helpers(self):
        """Managed dotfiles can be read-only; the box must edit its copies without changing the host."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, target = root / "source", root / "snapshot"
            nested = source / "nested"
            nested.mkdir(parents=True)
            settings = nested / "config.yml"
            settings.write_text("host: example.test\n")
            settings.chmod(0o400)
            helper = source / "helper"
            helper.write_text("#!/bin/sh\nexit 0\n")
            helper.chmod(0o500)
            (source / "config").write_text("old\n")
            (source / "config").chmod(0o400)
            override = root / "override"
            override.write_text("selected\n")
            nested.chmod(0o500)
            try:
                runtime.seed_state_dir(target, source, override)
                copied = target / "nested/config.yml"
                self.assertEqual(copied.stat().st_mode & 0o777, 0o600)
                self.assertEqual((target / "nested").stat().st_mode & 0o777, 0o700)
                self.assertEqual((target / "helper").stat().st_mode & 0o777, 0o700)
                self.assertEqual((target / "config").read_text(), "selected\n")
                copied.write_text("host: changed.test\n")
                self.assertEqual(settings.read_text(), "host: example.test\n")
                self.assertEqual(settings.stat().st_mode & 0o777, 0o400)
                self.assertEqual(nested.stat().st_mode & 0o777, 0o500)
            finally:
                nested.chmod(0o700)

    def test_dangling_links_do_not_block_copies_of_relative_file_and_directory_links(self):
        """Old CLI directories may contain stale links; valid dotfiles must still be materialized."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, dotfiles, target = root / "gh", root / "dotfiles", root / "snapshot"
            source.mkdir()
            dotfiles.mkdir()
            (dotfiles / "hosts.yml").write_text("github.com: {}\n")
            (source / "hosts.yml").symlink_to("../dotfiles/hosts.yml")
            (source / "aliases").symlink_to("../dotfiles", target_is_directory=True)
            (source / "obsolete").symlink_to("missing.yml")
            runtime.seed_state_dir(target, source)
            self.assertFalse((target / "hosts.yml").is_symlink())
            self.assertEqual((target / "hosts.yml").read_text(), "github.com: {}\n")
            self.assertFalse((target / "aliases").is_symlink())
            self.assertEqual((target / "aliases/hosts.yml").read_text(), "github.com: {}\n")
            self.assertFalse((target / "obsolete").is_symlink())
            self.assertFalse((target / "obsolete").exists())
            (target / "aliases/hosts.yml").write_text("changed\n")
            self.assertEqual((dotfiles / "hosts.yml").read_text(), "github.com: {}\n")


class GitLabEnvironmentTests(unittest.TestCase):
    def test_shell_config_path_and_tokens_do_not_escape_into_container_argv(self):
        """A copied config is selected by its container path, with no implicit token passthrough."""
        argv = _argv(Config(), env={"GLAB_CONFIG_DIR": "/host/glab",
                                    "GITLAB_TOKEN": "glpat_secret",
                                    "GITLAB_ACCESS_TOKEN": "access_secret", "OAUTH_TOKEN": "oauth_secret"})
        self.assertIn(f"--env=GLAB_CONFIG_DIR={runtime.HOST_HOME}/.config/glab-cli", argv)
        for secret in ("/host/glab", "glpat_secret", "access_secret", "oauth_secret"):
            self.assertNotIn(secret, " ".join(argv))
        for name in ("GITLAB_TOKEN", "GITLAB_ACCESS_TOKEN", "OAUTH_TOKEN"):
            self.assertNotIn(f"--env={name}", argv)


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
        config_file.write_text('{"host": {"notifications": {"telegram_token": "123:SECRETTOKEN"}}}')
        with mock.patch.object(runtime, "STATE_ROOT", state), \
             mock.patch.object(runtime, "BUILD_STATE", state / "build"), \
             mock.patch.object(runtime, "NOTIFY_STATE_ROOT", state / "notify"), \
             mock.patch.object(runtime, "TRACKER_RUNTIME_DIR", state / "runtime/tracker"), \
             mock.patch.object(runtime, "POLICY_RUNTIME_DIR", state / "runtime/policy"), \
             mock.patch.object(runtime, "WORKFLOW_STATE", state / "workflow"), \
             mock.patch.object(runtime, "CLAUDE_HOME", state / "claude"), \
             mock.patch.object(runtime, "CODEX_HOME", state / "codex"), \
             mock.patch.object(runtime, "XDG_CACHE_HOME", state / "cache"), \
             mock.patch.object(runtime, "NOTIFY_CREDENTIALS", state / "runtime/notify.json"), \
             mock.patch.object(runtime, "CONTAINER_NOTIFY_CREDENTIALS",
                               home / ".local/share/agent-box/runtime/notify.json"), \
             mock.patch.object(runtime, "HOST_HOME", home), \
             mock.patch.object(runtime.subprocess, "run",
                               return_value=mock.Mock(returncode=0, stdout="")):
            runtime.prepare_container_lifetime_state(config, mount_notifications=True)
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
        config.host.notifications.enabled = True
        config.host.notifications.telegram_token = "123:SECRETTOKEN"
        config.host.notifications.telegram_chat_id = "555"
        flags, state = self._mounts(config)
        mount = next(f for f in flags if "notify.json" in f)
        self.assertIn("ro=true", mount)
        contents = (state / "runtime/notify.json").read_text()
        self.assertIn("123:SECRETTOKEN", contents)
        # Only the two keys the hook needs; no host-integration switches.
        self.assertNotIn("gh_token", contents)
        self.assertNotIn("telegram_channel", contents)

    def test_callers_that_cannot_send_notifications_can_skip_the_credential(self):
        config = Config()
        config.host.notifications.enabled = True
        config.host.notifications.telegram_token = "123:SECRETTOKEN"
        config.host.notifications.telegram_chat_id = "555"
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
             mock.patch.object(runtime, "TRACKER_RUNTIME_DIR", state / "runtime/tracker"), \
             mock.patch.object(runtime, "POLICY_RUNTIME_DIR", state / "runtime/policy"), \
             mock.patch.object(runtime, "WORKFLOW_STATE", state / "workflow"), \
             mock.patch.object(runtime, "CLAUDE_HOME", state / "claude"), \
             mock.patch.object(runtime, "CODEX_HOME", state / "codex"), \
             mock.patch.object(runtime, "XDG_CACHE_HOME", state / "cache"), \
             mock.patch.object(runtime, "NOTIFY_CREDENTIALS", state / "runtime/notify.json"), \
             mock.patch.object(runtime, "HOST_HOME", home), \
             mock.patch.object(runtime.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="")):
            runtime.prepare_container_lifetime_state(config, mount_notifications=False)
            flags = runtime.build_mounts(Workspace(project, project), [], root, config,
                                         mount_notifications=False)
        self.assertFalse([flag for flag in flags if "notify.json" in flag])
        self.assertFalse((state / "runtime/notify.json").exists())

    def test_turning_notifications_off_removes_a_previously_written_file(self):
        config = Config()
        config.host.notifications.enabled = True
        config.host.notifications.telegram_token = "123:SECRETTOKEN"
        config.host.notifications.telegram_chat_id = "555"
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
