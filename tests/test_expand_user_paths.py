"""Poisoned-``$HOME`` regression tests for every remaining tilde expansion.

``paths.HOST_HOME`` is passwd-derived, but the settings that expand ``~``
themselves are security- or correctness-relevant and must agree with it:

* ``AGENT_BOX_WORKSPACE_ROOT`` decides which host tree is mounted read-write.
* ``--mount`` adds an arbitrary host path to the container.

A ``$HOME`` that a shell, a service manager, or a hostile caller set could
otherwise steer either of them, even though everything else already ignores
it. These tests pin both to the passwd home.
"""
import os
import pwd
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_box.mounts import MountError, parse_mount_arg
from agent_box.paths import expand_user
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
        with mock.patch.dict(os.environ, {"HOME": POISON}):
            workspace = resolve_workspace(cwd=real_home(), override="~")
        self.assertEqual(workspace.host_root, real_home().resolve())


class MountArgTests(unittest.TestCase):
    def test_tilde_mount_resolves_against_passwd_home(self):
        with mock.patch.dict(os.environ, {"HOME": POISON}):
            spec = parse_mount_arg("~")
        self.assertEqual(spec.path, real_home().resolve())
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
        with mock.patch.dict(os.environ, {"HOME": POISON}):
            spec = parse_mount_arg("~:ro")
        self.assertTrue(spec.read_only)
        self.assertEqual(spec.path, real_home().resolve())


if __name__ == "__main__":
    unittest.main()
