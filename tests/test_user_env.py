"""Arbitrary environment variables the user asks agent-box to pass through.

The fixed ``providers`` lists cover what the *agents* need. This covers what a
*project* needs, which agent-box cannot enumerate: a registry endpoint, a
database URL, a license key. Two mechanisms, and the difference between them
is a security one -- a forwarded name never puts its value in the Podman argv,
where ``/proc`` exposes it to every process on the host, while an explicit
assignment does and is therefore for settings rather than secrets.
"""
import os
import unittest
from unittest import mock

from agent_box import runtime
from agent_box.config import Config, EnvConfig


def _config(forward=(), assignments=None):
    config = Config()
    config.env = EnvConfig(forward=list(forward), set=dict(assignments or {}))
    return config


class UserEnvTests(unittest.TestCase):
    def test_nothing_configured_forwards_nothing(self):
        self.assertEqual(runtime.user_env(Config()), [])

    def test_a_forwarded_name_is_passed_by_name_only(self):
        """The value must not reach the argv, which /proc makes readable."""
        with mock.patch.dict(os.environ, {"MY_TOKEN": "s3cret"}):
            flags = runtime.user_env(_config(forward=["MY_TOKEN"]))
        self.assertEqual(flags, ["--env=MY_TOKEN"])

    def test_a_forwarded_name_that_is_unset_is_skipped(self):
        """An empty variable and an absent one mean different things."""
        environ = {k: v for k, v in os.environ.items() if k != "NOT_SET_ANYWHERE"}
        with mock.patch.dict(os.environ, environ, clear=True):
            self.assertEqual(runtime.user_env(_config(forward=["NOT_SET_ANYWHERE"])), [])

    def test_an_explicit_assignment_carries_its_value(self):
        self.assertEqual(
            runtime.user_env(_config(assignments={"TZ": "Europe/Moscow"})),
            ["--env=TZ=Europe/Moscow"])

    def test_an_explicit_assignment_wins_over_a_forward(self):
        """The config file is a statement, not a suggestion the shell overrides."""
        with mock.patch.dict(os.environ, {"TZ": "UTC"}):
            flags = runtime.user_env(_config(forward=["TZ"], assignments={"TZ": "Europe/Moscow"}))
        self.assertEqual(flags, ["--env=TZ=Europe/Moscow"])

    def test_assignments_are_ordered_so_the_argv_is_reproducible(self):
        flags = runtime.user_env(_config(assignments={"B": "2", "A": "1"}))
        self.assertEqual(flags, ["--env=A=1", "--env=B=2"])

    def test_the_flags_reach_a_launch(self):
        """Per-exec, so a value changed in the shell needs no new box."""
        config = _config(forward=["MY_TOKEN"], assignments={"TZ": "Europe/Moscow"})
        with mock.patch.dict(os.environ, {"MY_TOKEN": "s3cret"}):
            pairs, env = runtime.invocation_env(config, "claude")
        self.assertIn("MY_TOKEN", pairs)
        self.assertIn("TZ=Europe/Moscow", pairs)
        self.assertEqual(env["MY_TOKEN"], "s3cret")

    def test_a_forwarded_secret_never_appears_in_the_launch_arguments(self):
        config = _config(forward=["MY_TOKEN"])
        with mock.patch.dict(os.environ, {"MY_TOKEN": "s3cret"}):
            pairs, _ = runtime.invocation_env(config, "claude")
        self.assertNotIn("s3cret", " ".join(pairs))


class EnvConfigSchemaTests(unittest.TestCase):
    """Everything here becomes a --env flag, so a malformed entry is dropped
    rather than carried to Podman, where it would define something else."""

    def test_defaults_are_empty(self):
        self.assertEqual(Config().env.forward, [])
        self.assertEqual(Config().env.set, {})

    def test_round_trips(self):
        config = _config(forward=["A"], assignments={"B": "2"})
        loaded = Config.from_dict(config.to_dict()).env
        self.assertEqual(loaded.forward, ["A"])
        self.assertEqual(loaded.set, {"B": "2"})

    def test_names_that_are_not_variable_names_are_dropped(self):
        loaded = EnvConfig.from_dict({
            "forward": ["OK", "not ok", "WITH=EQUALS", "2LEADING", "", 17],
            "set": {"FINE": "x", "bad name": "y", "": "z"},
        })
        self.assertEqual(loaded.forward, ["OK"])
        self.assertEqual(loaded.set, {"FINE": "x"})

    def test_non_string_values_are_stringified_or_dropped(self):
        loaded = EnvConfig.from_dict({"set": {"PORT": 8080, "NESTED": {"no": 1}}})
        self.assertEqual(loaded.set, {"PORT": "8080"})

    def test_a_malformed_section_loads_as_empty(self):
        loaded = EnvConfig.from_dict({"forward": "OK", "set": ["A=1"]})
        self.assertEqual(loaded.forward, [])
        self.assertEqual(loaded.set, {})

    def test_a_config_file_without_the_section_loads(self):
        config = Config.from_dict({"version": 1, "github": {}})
        self.assertEqual(config.env.forward, [])


if __name__ == "__main__":
    unittest.main()
