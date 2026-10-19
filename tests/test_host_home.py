"""Regression tests: ``$HOME`` must never override the passwd home.

``Path.home()`` and ``Path.expanduser()`` both consult ``$HOME`` first. Every
place agent-box derived a host path from them was therefore steerable by a
shell variable: the build identity baked into the image, the container's
runtime ``HOME``, every bind mount's source, and the state/config directory
defaults. A "poisoned" ``$HOME`` (an inherited value from a service manager, a
sudo invocation, or a deliberately hostile environment) would silently
relocate all of it while the passwd entry -- what ``--userns=keep-id`` maps
back to on the host -- stayed put.
"""
import importlib
import os
import pwd
import tempfile
import unittest
from pathlib import Path
from unittest import mock

POISON = "/tmp/agent-box-poisoned-home"


def real_home() -> Path:
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def reload_paths(env):
    """Reload agent_box.paths under ``env``.

    Only ``paths`` and ``runtime`` are ever reloaded: both export constants
    that are computed at import time. ``agent_box.identity`` is deliberately
    *not* reloaded -- that would rebind its ``HostIdentity`` class and make
    isinstance checks in other test modules fail against a stale class object.
    """
    from agent_box import paths
    with mock.patch.dict(os.environ, env, clear=True):
        importlib.reload(paths)
        return paths


class PasswdHomeTests(unittest.TestCase):
    def tearDown(self):
        from agent_box import paths, runtime
        importlib.reload(paths)
        importlib.reload(runtime)

    def test_passwd_home_ignores_env_home(self):
        from agent_box.paths import passwd_home
        with mock.patch.dict(os.environ, {"HOME": POISON}):
            self.assertEqual(passwd_home(), real_home())

    def test_host_home_ignores_env_home(self):
        paths = reload_paths({"HOME": POISON, "PATH": os.environ.get("PATH", "")})
        self.assertEqual(paths.HOST_HOME, real_home())
        self.assertNotIn(POISON, str(paths.STATE_ROOT))
        self.assertNotIn(POISON, str(paths.CONFIG_ROOT))
        self.assertNotIn(POISON, str(paths.CLAUDE_HOME))
        self.assertNotIn(POISON, str(paths.CODEX_HOME))
        self.assertNotIn(POISON, str(paths.XDG_CACHE_HOME))

    def test_build_identity_home_ignores_env_home(self):
        from agent_box import identity
        with mock.patch.dict(os.environ, {"HOME": POISON}):
            detected = identity.detect_host_identity()
        self.assertEqual(detected.home, real_home())

    def test_build_args_carry_the_passwd_home(self):
        from agent_box import identity, runtime
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            token = Path(tmp) / "tools-refresh"
            token.write_text("tok\n")
            with mock.patch.dict(os.environ, {"HOME": POISON}), \
                 mock.patch.object(runtime, "TOOLS_REFRESH_FILE", token), \
                 mock.patch.object(runtime.image_build, "build", lambda *args: (calls.append(args[4]) or 0, "image-id")), \
                 mock.patch.object(runtime, "BUILD_STATE", Path(tmp)), \
                 mock.patch.object(runtime, "ensure_state_dirs", lambda: None):
                runtime.build_image(runtime.PACKAGE_ROOT, False, False,
                                    identity=identity.detect_host_identity())
        self.assertIn(f"USER_HOME={real_home()}", calls[0])
        self.assertNotIn(POISON, " ".join(calls[0]))

    def test_tilde_in_a_setting_expands_to_the_passwd_home(self):
        paths = reload_paths({"HOME": POISON, "CLAUDE_CONFIG_DIR": "~/elsewhere"})
        self.assertEqual(paths.CLAUDE_HOME, real_home() / "elsewhere")

    def test_container_home_and_mounts_ignore_env_home(self):
        from agent_box import paths, runtime
        from agent_box.workspace import Workspace
        with tempfile.TemporaryDirectory() as state:
            with mock.patch.dict(os.environ, {"HOME": POISON, "PATH": os.environ.get("PATH", ""),
                                               "AGENT_BOX_STATE_DIR": state}):
                importlib.reload(paths)
                importlib.reload(runtime)
                with tempfile.TemporaryDirectory() as project, mock.patch.object(
                    runtime, "prepare_container_lifetime_state",
                ):
                    workspace = Workspace(Path(project), Path(project))
                    argv = runtime.container_argv(workspace, [], Path(project), ["claude"])
        self.assertIn(f"--env=HOME={real_home()}", argv)
        self.assertNotIn(POISON, " ".join(argv))

    def test_non_absolute_passwd_home_falls_back_to_root_not_env_home(self):
        from agent_box.paths import passwd_home
        entry = pwd.getpwuid(os.getuid())
        broken = type(entry)((entry.pw_name, entry.pw_passwd, entry.pw_uid, entry.pw_gid,
                              entry.pw_gecos, "relative/home", entry.pw_shell))
        with mock.patch("pwd.getpwuid", return_value=broken), \
             mock.patch.dict(os.environ, {"HOME": POISON}):
            self.assertEqual(passwd_home(), Path("/"))

    def test_missing_passwd_entry_falls_back_to_root_not_env_home(self):
        from agent_box.paths import passwd_home
        with mock.patch("pwd.getpwuid", side_effect=KeyError()), \
             mock.patch.dict(os.environ, {"HOME": POISON}):
            self.assertEqual(passwd_home(), Path("/"))


class ClaudeConfigDirTests(unittest.TestCase):
    """A relocated Claude config directory must reach the container.

    ``CLAUDE_HOME`` was already bind-mounted at its host path, but nothing
    told ``claude`` *inside* the container to look there, so it fell back to
    ``$HOME/.claude`` -- a directory with none of the host's config in it.
    """

    def tearDown(self):
        from agent_box import paths, runtime
        importlib.reload(paths)
        importlib.reload(runtime)

    def _argv(self, env, project):
        from agent_box import paths, runtime
        from agent_box.workspace import Workspace
        full = dict(os.environ)
        full.update(env)
        full["AGENT_BOX_STATE_DIR"] = str(Path(project).parent / "state")
        with mock.patch.dict(os.environ, full, clear=True):
            importlib.reload(paths)
            importlib.reload(runtime)
            workspace = Workspace(Path(project), Path(project))
            with mock.patch.object(runtime, "prepare_container_lifetime_state"):
                return runtime.container_argv(workspace, [], Path(project), ["claude"])

    def test_custom_claude_config_dir_is_forwarded_and_mounted(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "claude-config"
            project = Path(tmp) / "project"
            project.mkdir()
            argv = self._argv({"CLAUDE_CONFIG_DIR": str(config_dir)}, project)
        self.assertIn(f"--env=CLAUDE_CONFIG_DIR={config_dir}", argv)
        self.assertTrue(
            any(f"src={config_dir},dst={config_dir}" in a for a in argv),
            "the relocated config directory must also be bind-mounted at its host path",
        )

    def test_unset_claude_config_dir_is_not_forwarded(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"}
            env["AGENT_BOX_STATE_DIR"] = str(Path(tmp) / "state")
            from agent_box import paths, runtime
            from agent_box.workspace import Workspace
            with mock.patch.dict(os.environ, env, clear=True):
                importlib.reload(paths)
                importlib.reload(runtime)
                workspace = Workspace(project, project)
                with mock.patch.object(runtime, "prepare_container_lifetime_state"):
                    argv = runtime.container_argv(workspace, [], project, ["claude"])
        self.assertFalse([a for a in argv if a.startswith("--env=CLAUDE_CONFIG_DIR")])


if __name__ == "__main__":
    unittest.main()
