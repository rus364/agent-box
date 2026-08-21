"""What the container is allowed to see, as a function of agent-box config.

The rule these tests encode: a host credential reaches the container only
because the user turned that switch on. The specific regression guarded here
is the old behaviour where being logged in to ``gh`` on the host was treated
as consent to hand every disposable container a live GitHub token.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_box import runtime
from agent_box.config import Config
from agent_box.workspace import Workspace


class GhTokenPassthroughTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        for name in ("GH_TOKEN", "GITHUB_TOKEN", "AGENT_BOX_GH_TOKEN"):
            os.environ.pop(name, None)
        self.addCleanup(self.env.stop)
        self.gh = mock.patch.object(
            runtime.subprocess, "run",
            return_value=mock.Mock(returncode=0, stdout="gho_secrettoken\n"))
        self.gh.start()
        self.addCleanup(self.gh.stop)
        self.which = mock.patch.object(runtime.shutil, "which", return_value="/usr/bin/gh")
        self.which.start()
        self.addCleanup(self.which.stop)

    def test_authenticated_host_alone_does_not_forward_a_token(self):
        runtime.maybe_gh_token(Config())
        self.assertNotIn("GH_TOKEN", os.environ)

    def test_enabled_in_config_forwards_the_token(self):
        config = Config()
        config.github.gh_token = True
        runtime.maybe_gh_token(config)
        self.assertEqual(os.environ.get("GH_TOKEN"), "gho_secrettoken")

    def test_env_override_withholds_from_a_configured_host(self):
        config = Config()
        config.github.gh_token = True
        os.environ["AGENT_BOX_GH_TOKEN"] = "0"
        runtime.maybe_gh_token(config)
        self.assertNotIn("GH_TOKEN", os.environ)

    def test_env_override_forwards_from_an_unconfigured_host(self):
        os.environ["AGENT_BOX_GH_TOKEN"] = "1"
        runtime.maybe_gh_token(Config())
        self.assertEqual(os.environ.get("GH_TOKEN"), "gho_secrettoken")

    def test_an_existing_token_in_the_shell_is_never_overwritten(self):
        config = Config()
        config.github.gh_token = True
        os.environ["GH_TOKEN"] = "user_supplied"
        runtime.maybe_gh_token(config)
        self.assertEqual(os.environ["GH_TOKEN"], "user_supplied")


class SeededConfigDirTests(unittest.TestCase):
    """git/gh config directories are always mounted (so both tools work) but
    only populated from the host when the user opted in."""

    def _seed(self, config):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = Path(tmp.name) / "state"
        home = Path(tmp.name) / "home"
        (home / ".config/gh").mkdir(parents=True)
        (home / ".config/gh/hosts.yml").write_text("github.com:\n  oauth_token: gho_secret\n")
        (home / ".gitconfig").write_text("[user]\n\tname = Someone\n")
        project = Path(tmp.name) / "project"
        project.mkdir()
        with mock.patch.object(runtime, "STATE_ROOT", state), \
             mock.patch.object(runtime, "BUILD_STATE", state / "build"), \
             mock.patch.object(runtime, "NOTIFY_STATE_ROOT", state / "notify"), \
             mock.patch.object(runtime, "HOST_HOME", home):
            runtime.build_mounts(Workspace(project, project), [], Path(tmp.name), config)
        return state

    def test_gh_config_not_copied_by_default(self):
        state = self._seed(Config())
        self.assertTrue((state / "gh").is_dir(), "the directory is still mounted")
        self.assertEqual(list((state / "gh").iterdir()), [], "but carries no host credentials")

    def test_gh_config_copied_when_enabled(self):
        config = Config()
        config.github.gh_config = True
        state = self._seed(config)
        self.assertIn("gho_secret", (state / "gh/hosts.yml").read_text())

    def test_git_config_copied_by_default(self):
        state = self._seed(Config())
        self.assertIn("Someone", (state / "git/config").read_text())

    def test_git_config_withheld_when_disabled(self):
        config = Config()
        config.git.config = False
        state = self._seed(config)
        self.assertEqual(list((state / "git").iterdir()), [])


class ProviderEnvTests(unittest.TestCase):
    def _argv(self, config):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        project = Path(tmp.name)
        return runtime.container_argv(Workspace(project, project), [], project, ["claude"], config)

    def test_provider_variables_forwarded_by_default(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}):
            self.assertIn("--env=ANTHROPIC_API_KEY", self._argv(Config()))

    def test_provider_variables_withheld_when_disabled(self):
        config = Config()
        config.providers.env = False
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}):
            self.assertNotIn("--env=ANTHROPIC_API_KEY", self._argv(config))

    def test_only_the_name_is_passed_never_the_value(self):
        """podman --env=NAME forwards from the caller's environment; the
        secret must never appear in the argv (it is visible in `ps`)."""
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}):
            self.assertNotIn("sk-test", " ".join(self._argv(Config())))


class BunCacheTests(unittest.TestCase):
    def test_bun_cache_points_into_the_isolated_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            argv = runtime.container_argv(Workspace(project, project), [], project, ["claude"], Config())
            bun = [a for a in argv if a.startswith("--env=BUN_INSTALL_CACHE_DIR=")]
            self.assertEqual(len(bun), 1)
            self.assertTrue(bun[0].endswith("/bun"))


if __name__ == "__main__":
    unittest.main()


class ConfigMountTests(unittest.TestCase):
    """The notification hooks run inside the container and read the config
    from the default XDG path, because agent-box there gets no
    AGENT_BOX_CONFIG_DIR. A host that relocates its config directory must
    still have the file land where the container looks."""

    def test_relocated_config_is_mounted_at_the_default_container_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            config_file = root / "elsewhere" / "config.json"
            config_file.parent.mkdir()
            config_file.write_text("{}")
            project = root / "project"
            project.mkdir()
            with mock.patch.object(runtime, "STATE_ROOT", root / "state"), \
                 mock.patch.object(runtime, "BUILD_STATE", root / "state/build"), \
                 mock.patch.object(runtime, "NOTIFY_STATE_ROOT", root / "state/notify"), \
                 mock.patch.object(runtime, "HOST_HOME", home), \
                 mock.patch.object(runtime, "CONFIG_FILE", config_file):
                flags = runtime.build_mounts(Workspace(project, project), [], root, Config())
            mount = next(f for f in flags if "config.json" in f)
            self.assertIn(f"src={config_file}", mount)
            self.assertIn(f"dst={home}/.config/agent-box/config.json", mount)
            self.assertIn("ro=true", mount)
