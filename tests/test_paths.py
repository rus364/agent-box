"""Regression tests for the Path('').expanduser() truthiness bug.

``Path("")`` normalizes to ``Path(".")``, which is truthy, so a naive
``Path(os.environ.get(name, "")).expanduser() or default`` never falls back
for an unset/empty environment variable -- it silently resolves to the
current working directory. This previously caused agent-box to write real
state (synced Claude skills) into whatever directory it was invoked from.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_box.paths import env_path


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


if __name__ == "__main__":
    unittest.main()
