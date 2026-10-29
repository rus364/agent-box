"""Regression tests for the Path('').expanduser() truthiness bug.

``Path("")`` normalizes to ``Path(".")``, which is truthy, so a naive
``Path(os.environ.get(name, "")).expanduser() or default`` never falls back
for an unset/empty environment variable -- it silently resolves to the
current working directory. This previously caused agent-box to write real
state (synced Claude skills) into whatever directory it was invoked from.
"""
import os
import runpy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_box.paths import env_path
from agent_box import paths, runtime


class EnvPathTests(unittest.TestCase):
    def test_unset_uses_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGENT_BOX_TEST_VAR", None)
            self.assertEqual(env_path("AGENT_BOX_TEST_VAR", Path("/default")), Path("/default"))

    def test_empty_string_uses_default(self):
        with mock.patch.dict(os.environ, {"AGENT_BOX_TEST_VAR": ""}):
            self.assertEqual(env_path("AGENT_BOX_TEST_VAR", Path("/default")), Path("/default"))

    def test_whitespace_only_uses_default(self):
        with mock.patch.dict(os.environ, {"AGENT_BOX_TEST_VAR": "   "}):
            self.assertEqual(env_path("AGENT_BOX_TEST_VAR", Path("/default")), Path("/default"))

    def test_explicit_absolute_value_used(self):
        with mock.patch.dict(os.environ, {"AGENT_BOX_TEST_VAR": "/explicit/path"}):
            self.assertEqual(env_path("AGENT_BOX_TEST_VAR", Path("/default")), Path("/explicit/path"))

    def test_explicit_relative_value_expanded_as_given(self):
        with mock.patch.dict(os.environ, {"AGENT_BOX_TEST_VAR": "relative/path"}):
            self.assertEqual(env_path("AGENT_BOX_TEST_VAR", Path("/default")), Path("relative/path"))

    def test_tilde_is_expanded(self):
        with mock.patch.dict(os.environ, {"AGENT_BOX_TEST_VAR": "~/somewhere"}):
            result = env_path("AGENT_BOX_TEST_VAR", Path("/default"))
            self.assertFalse(str(result).startswith("~"))
            self.assertTrue(str(result).endswith("/somewhere"))

    def test_never_resolves_to_cwd_for_empty_value(self):
        # The actual bug: confirm the result is never Path('.') when a
        # module was reloaded from within some arbitrary cwd.
        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                with mock.patch.dict(os.environ, {"AGENT_BOX_TEST_VAR": ""}):
                    result = env_path("AGENT_BOX_TEST_VAR", Path("/default"))
            finally:
                os.chdir(old_cwd)
            self.assertNotEqual(result, Path(tmp))
            self.assertEqual(result, Path("/default"))


class GlabConfigPathTests(unittest.TestCase):
    """Copy the same user config glab selects without trusting the shell's HOME."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name) / "home"
        self.default = self.home / ".config/glab-cli"
        self.default.mkdir(parents=True)
        for patcher in (mock.patch.object(paths, "HOST_HOME", self.home),
                        mock.patch.dict(os.environ, {"HOME": "/poison"}, clear=True)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_default_uses_passwd_home(self):
        self.assertEqual(paths.glab_config_dir(), self.default)

    def test_explicit_override_wins_even_when_default_exists(self):
        (self.default / "config.yml").touch()
        with mock.patch.dict(os.environ, {"GLAB_CONFIG_DIR": "~/custom-glab", "XDG_CONFIG_HOME": "/xdg"}):
            self.assertEqual(paths.glab_config_dir(), self.home / "custom-glab")

    def test_default_file_precedes_xdg_but_empty_directory_does_not(self):
        with mock.patch.dict(os.environ, {"GLAB_CONFIG_DIR": "", "XDG_CONFIG_HOME": "/xdg"}):
            self.assertEqual(paths.glab_config_dir(), Path("/xdg/glab-cli"))
            (self.default / "config.yml").touch()
            self.assertEqual(paths.glab_config_dir(), self.default)


class ModuleLevelPathDefaultsTests(unittest.TestCase):
    """Import-time globals must reflect env_path, not the old broken pattern."""

    def test_state_root_is_not_cwd_when_env_unset(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                env = dict(os.environ)
                env.pop("AGENT_BOX_STATE_DIR", None)
                with mock.patch.dict(os.environ, env, clear=True):
                    import importlib
                    from agent_box import paths as paths_module
                    importlib.reload(paths_module)
                    try:
                        self.assertNotEqual(paths_module.STATE_ROOT, Path(tmp))
                        self.assertNotEqual(paths_module.CLAUDE_HOME, Path(tmp))
                        self.assertNotEqual(paths_module.CODEX_HOME, Path(tmp))
                    finally:
                        importlib.reload(paths_module)  # restore real environment
            finally:
                os.chdir(old_cwd)


class NestedLauncherStateTests(unittest.TestCase):
    def load_paths(self, namespace, overrides=None, container=True):
        environment = {"XDG_DATA_HOME": "/shared/data", "AGENT_BOX_WORKFLOW_STATE": "/shared/workflow"}
        environment.update(overrides or {})
        with mock.patch.dict(os.environ, environment, clear=True), \
             mock.patch.object(Path, "is_file", return_value=container), \
             mock.patch.object(Path, "stat", return_value=mock.Mock(st_ino=namespace)):
            return runpy.run_path(str(Path(paths.__file__)))

    def test_container_build_and_runtime_avoid_shared_mounts_but_hooks_keep_them(self):
        """Creating a root-owned bind parent or a read-only policy mount must not block a nested launch."""
        values = self.load_paths(123)
        root = values["STATE_ROOT"]
        self.assertEqual(root, values["HOST_HOME"] / ".local/share/agent-box-launcher-123")
        for key in ("BUILD_STATE", "HOST_SESSIONS_ROOT", "TRACKER_RUNTIME_DIR", "POLICY_RUNTIME_DIR"):
            self.assertTrue(values[key].is_relative_to(root), key)
        self.assertEqual(values["NOTIFY_STATE_ROOT"], Path("/shared/data/agent-box/notify"))
        self.assertEqual(values["NOTIFY_CREDENTIALS"], Path("/shared/data/agent-box/runtime/notify.json"))
        self.assertEqual(values["WORKFLOW_STATE"], Path("/shared/workflow"))

    def test_namespace_is_stable_within_a_box_and_distinct_from_its_parent(self):
        """The same workspace path must not alias a mounted outer lifecycle directory."""
        self.assertEqual(self.load_paths(123)["STATE_ROOT"], self.load_paths(123)["STATE_ROOT"])
        self.assertNotEqual(self.load_paths(123)["STATE_ROOT"], self.load_paths(456)["STATE_ROOT"])

    def test_host_defaults_and_explicit_test_isolation_are_preserved(self):
        self.assertEqual(self.load_paths(123, container=False)["STATE_ROOT"], Path("/shared/data/agent-box"))
        values = self.load_paths(123, {"AGENT_BOX_STATE_DIR": "/isolated"})
        self.assertEqual(values["STATE_ROOT"], Path("/isolated"))
        self.assertEqual(values["NOTIFY_STATE_ROOT"], Path("/isolated/notify"))

    @unittest.skipIf(os.geteuid() == 0, "DAC denial requires an unprivileged test user")
    def test_state_creation_succeeds_beside_an_unwritable_shared_mount_parent(self):
        """The old build path must fail while the selected private path initializes normally."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            shared = home / ".local/share/agent-box"
            (shared / "notify").mkdir(parents=True)
            with mock.patch.object(paths.pwd, "getpwuid", return_value=mock.Mock(pw_dir=str(home))):
                values = self.load_paths(123, {"XDG_DATA_HOME": str(home / ".local/share"),
                                               "XDG_CACHE_HOME": str(home / ".cache")})
            names = ("STATE_ROOT", "BUILD_STATE", "NOTIFY_STATE_ROOT", "CODEX_HOME",
                     "CLAUDE_HOME", "XDG_CACHE_HOME")
            shared.chmod(0o555)
            try:
                with mock.patch.multiple(runtime, **{name: values[name] for name in names}):
                    with mock.patch.object(runtime, "BUILD_STATE", shared / "build"):
                        with self.assertRaises(PermissionError):
                            runtime.ensure_state_dirs()
                    runtime.ensure_state_dirs()
                self.assertTrue(values["BUILD_STATE"].is_dir())
                self.assertTrue((values["STATE_ROOT"] / "runtime/workspaces").is_dir())
                self.assertFalse((shared / "build").exists())
                self.assertEqual(shared.stat().st_mode & 0o777, 0o555)
            finally:
                shared.chmod(0o755)


if __name__ == "__main__":
    unittest.main()
