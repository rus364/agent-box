"""Poisoned-``$HOME`` regression tests for every remaining tilde expansion.

``paths.HOST_HOME`` was made passwd-derived in an earlier pass, but four call
sites still expanded ``~`` with ``Path.expanduser()``, which reads ``$HOME``
first. Each of them is security- or correctness-relevant:

* ``AGENT_BOX_WORKSPACE_ROOT`` decides which host tree is mounted read-write.
* ``--mount`` adds an arbitrary host path to the container.
* ``telegram roots add`` writes the stored, canonical allowlist entry.
* Telegram project discovery reads that allowlist back to build the buttons a
  remote user picks from.

A ``$HOME`` that a shell, a service manager, or a hostile caller set could
therefore still steer any of them, even though everything else already ignored
it. These tests pin all four to the passwd home.
"""
import os
import pwd
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_box.cli import telegram_roots
from agent_box.config import RemoteControlConfig, TelegramConfig
from agent_box.mounts import MountError, parse_mount_arg
from agent_box.paths import expand_user
from agent_box.telegram.projects import list_projects, resolve_allowed_project
from agent_box.workspace import resolve_workspace

POISON = "/tmp/agent-box-poisoned-home"
# A directory name chosen so it cannot exist under the real passwd home; the
# poisoned-home cases need a path that resolves *only* under $HOME, so that a
# leak would be visible instead of accidentally hitting a real directory.
ONLY_UNDER_POISON = "agent-box-only-under-poisoned-home"


def real_home() -> Path:
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


class ExpandUserTests(unittest.TestCase):
    def test_bare_tilde(self):
        with mock.patch.dict(os.environ, {"HOME": POISON}):
            self.assertEqual(expand_user("~"), real_home())

    def test_tilde_slash(self):
        with mock.patch.dict(os.environ, {"HOME": POISON}):
            self.assertEqual(expand_user("~/code/app"), real_home() / "code/app")

    def test_absolute_path_untouched(self):
        with mock.patch.dict(os.environ, {"HOME": POISON}):
            self.assertEqual(expand_user("/srv/data"), Path("/srv/data"))

    def test_other_users_tilde_is_not_expanded(self):
        # ~otheruser is deliberately left alone rather than resolved through
        # the passwd database: agent-box only ever means "my home" by "~".
        self.assertEqual(expand_user("~root/x"), Path("~root/x"))


class WorkspaceRootTests(unittest.TestCase):
    def test_tilde_override_resolves_against_passwd_home(self):
        with mock.patch.dict(os.environ, {"HOME": POISON}):
            with self.assertRaises((NotADirectoryError, ValueError)) as ctx:
                resolve_workspace(cwd=Path("/tmp"), override="~/definitely-not-here")
            self.assertIn(str(real_home()), str(ctx.exception))
            self.assertNotIn(POISON, str(ctx.exception))

    def test_tilde_override_accepted_when_cwd_is_inside_the_real_home(self):
        with tempfile.TemporaryDirectory(dir=str(real_home())) as tmp:
            name = Path(tmp).name
            with mock.patch.dict(os.environ, {"HOME": POISON}):
                workspace = resolve_workspace(cwd=Path(tmp), override=f"~/{name}")
            self.assertEqual(workspace.host_root, Path(tmp).resolve())


class MountArgTests(unittest.TestCase):
    def test_tilde_mount_resolves_against_passwd_home(self):
        with tempfile.TemporaryDirectory(dir=str(real_home())) as tmp:
            name = Path(tmp).name
            with mock.patch.dict(os.environ, {"HOME": POISON}):
                spec = parse_mount_arg(f"~/{name}")
            self.assertEqual(spec.path, Path(tmp).resolve())
            self.assertNotIn(POISON, str(spec.path))

    def test_tilde_mount_under_poisoned_home_is_not_found(self):
        """The poisoned tree exists but must never be what ``~`` means."""
        with tempfile.TemporaryDirectory() as tmp:
            poisoned = Path(tmp) / "home"
            (poisoned / ONLY_UNDER_POISON).mkdir(parents=True)
            with mock.patch.dict(os.environ, {"HOME": str(poisoned)}):
                with self.assertRaises(MountError):
                    parse_mount_arg(f"~/{ONLY_UNDER_POISON}")

    def test_ro_suffix_still_parsed_with_tilde(self):
        with tempfile.TemporaryDirectory(dir=str(real_home())) as tmp:
            name = Path(tmp).name
            with mock.patch.dict(os.environ, {"HOME": POISON}):
                spec = parse_mount_arg(f"~/{name}:ro")
            self.assertTrue(spec.read_only)
            self.assertEqual(spec.path, Path(tmp).resolve())


class TelegramRootsTests(unittest.TestCase):
    def _config_with(self, tmp):
        path = Path(tmp) / "telegram.json"
        config = TelegramConfig(token="tok", remote_control=RemoteControlConfig())
        return path, config

    def test_roots_add_stores_the_passwd_expanded_path(self):
        with tempfile.TemporaryDirectory(dir=str(real_home())) as home_dir, \
             tempfile.TemporaryDirectory() as tmp:
            name = Path(home_dir).name
            path, config = self._config_with(tmp)
            saved = {}
            with mock.patch.dict(os.environ, {"HOME": POISON}), \
                 mock.patch("agent_box.cli._require_config", return_value=config), \
                 mock.patch("agent_box.cli.save_telegram_config",
                            side_effect=lambda cfg, *a, **kw: saved.update(cfg.to_dict())):
                telegram_roots(["add", f"~/{name}"])
            roots = saved["remote_control"]["workspace_roots"]
            self.assertEqual(roots, [str(Path(home_dir).resolve())])
            self.assertNotIn(POISON, roots[0])
            del path

    def test_roots_add_rejects_a_tilde_path_that_only_exists_under_poisoned_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            poisoned = Path(tmp) / "home"
            (poisoned / ONLY_UNDER_POISON).mkdir(parents=True)
            _path, config = self._config_with(tmp)
            with mock.patch.dict(os.environ, {"HOME": str(poisoned)}), \
                 mock.patch("agent_box.cli._require_config", return_value=config), \
                 mock.patch("agent_box.cli.save_telegram_config"):
                with self.assertRaises(SystemExit):
                    telegram_roots(["add", f"~/{ONLY_UNDER_POISON}"])


class ProjectDiscoveryTests(unittest.TestCase):
    def test_configured_tilde_root_expands_against_passwd_home(self):
        with tempfile.TemporaryDirectory(dir=str(real_home())) as home_dir:
            (Path(home_dir) / "proj").mkdir()
            name = Path(home_dir).name
            with mock.patch.dict(os.environ, {"HOME": POISON}):
                entries = list_projects([f"~/{name}"])
                allowed = resolve_allowed_project(Path(home_dir) / "proj", [f"~/{name}"])
            paths = [str(e.path) for e in entries]
            self.assertIn(str(Path(home_dir).resolve() / "proj"), paths)
            self.assertEqual(allowed, (Path(home_dir) / "proj").resolve())

    def test_poisoned_home_root_lists_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            poisoned = Path(tmp) / "home"
            secret = poisoned / ONLY_UNDER_POISON / "secret-project"
            secret.mkdir(parents=True)
            root = f"~/{ONLY_UNDER_POISON}"
            with mock.patch.dict(os.environ, {"HOME": str(poisoned)}):
                entries = list_projects([root])
                allowed = resolve_allowed_project(secret, [root])
            self.assertEqual(entries, [])
            self.assertIsNone(allowed)


class DiscoveryDepthTests(unittest.TestCase):
    """Documented behavior: a root plus its immediate children, nothing deeper."""

    def test_only_one_level_below_each_root_is_listed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "code"
            (root / "child" / "grandchild").mkdir(parents=True)
            names = {e.path.name for e in list_projects([str(root)])}
            self.assertIn("code", names)
            self.assertIn("child", names)
            self.assertNotIn("grandchild", names)

    def test_a_grandchild_is_still_selectable_if_named_directly(self):
        """Depth limits the *listing*, not what counts as inside a root."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "code"
            deep = root / "child" / "grandchild"
            deep.mkdir(parents=True)
            self.assertEqual(resolve_allowed_project(deep, [str(root)]), deep.resolve())


if __name__ == "__main__":
    unittest.main()
