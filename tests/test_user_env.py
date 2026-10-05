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
from agent_box.launch_options import parse
from agent_box.config import Config, EnvConfig, parse_env_spec


def _config(forward=(), assignments=None):
    config = Config()
    config.box.env = EnvConfig(forward=list(forward), set=dict(assignments or {}))
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


class InvocationEnvTests(unittest.TestCase):
    """Passing a variable for one launch, without editing the config file.

    Two spellings of the same request, because the obvious one is impossible:
    a variable set by a shell prefix (``FOO=bar agent-box codex``) looks,
    inside the process, exactly like one exported in a login profile, so
    honouring bare names would mean forwarding the entire host environment.
    ``--env`` states the intent in the arguments; ``AGENT_BOX_ENV_FOO`` states
    it in a namespace that belongs to agent-box, keeping the shell-prefix
    ergonomics.
    """

    def test_a_flag_sets_a_variable_for_this_launch(self):
        merged = Config().box.env.overlay([parse_env_spec("TZ=Europe/Moscow")], {})
        self.assertEqual(merged.set, {"TZ": "Europe/Moscow"})

    def test_a_bare_flag_name_forwards_by_name(self):
        """The secret-safe form: the value never enters the argv."""
        merged = Config().box.env.overlay([parse_env_spec("MY_TOKEN")], {})
        self.assertEqual(merged.forward, ["MY_TOKEN"])
        self.assertEqual(merged.set, {})

    def test_the_shell_prefix_namespace_is_honoured(self):
        merged = Config().box.env.overlay([], {"AGENT_BOX_ENV_DATABASE_URL": "postgres://x"})
        self.assertEqual(merged.set, {"DATABASE_URL": "postgres://x"})

    def test_an_unrelated_host_variable_is_never_picked_up(self):
        merged = Config().box.env.overlay([], {"DATABASE_URL": "postgres://x", "HOME": "/root"})
        self.assertEqual(merged.set, {})
        self.assertEqual(merged.forward, [])

    def test_a_flag_beats_the_shell_prefix(self):
        merged = Config().box.env.overlay(
            [parse_env_spec("TZ=UTC")], {"AGENT_BOX_ENV_TZ": "Europe/Moscow"})
        self.assertEqual(merged.set, {"TZ": "UTC"})

    def test_an_invocation_value_beats_the_stored_lists(self):
        config = _config(assignments={"TZ": "UTC"})
        merged = config.box.env.overlay([parse_env_spec("TZ=Europe/Moscow")], {})
        self.assertEqual(merged.set, {"TZ": "Europe/Moscow"})

    def test_forwarding_a_name_that_is_stored_as_an_assignment_replaces_it(self):
        """The two lists must never claim the same name at once."""
        config = _config(assignments={"TOKEN": "written-down"})
        merged = config.box.env.overlay([parse_env_spec("TOKEN")], {})
        self.assertEqual(merged.forward, ["TOKEN"])
        self.assertEqual(merged.set, {})

    def test_setting_a_name_that_is_stored_as_a_forward_replaces_it(self):
        config = _config(forward=["TOKEN"])
        merged = config.box.env.overlay([parse_env_spec("TOKEN=literal")], {})
        self.assertEqual(merged.forward, [])
        self.assertEqual(merged.set, {"TOKEN": "literal"})

    def test_the_overlay_leaves_the_stored_configuration_alone(self):
        config = _config(forward=["A"], assignments={"B": "1"})
        config.box.env.overlay([parse_env_spec("C=3")], {})
        self.assertEqual(config.box.env.forward, ["A"])
        self.assertEqual(config.box.env.set, {"B": "1"})

    def test_a_malformed_flag_is_rejected(self):
        for spec in ("=value", "not a name=1", "2LEADING=1", ""):
            with self.subTest(spec=spec):
                with self.assertRaises(ValueError):
                    parse_env_spec(spec)

    def test_a_malformed_namespace_entry_is_ignored(self):
        merged = Config().box.env.overlay([], {"AGENT_BOX_ENV_": "x", "AGENT_BOX_ENV_2BAD": "y"})
        self.assertEqual(merged.set, {})

    def test_the_request_channel_does_not_leak_into_the_session(self):
        """AGENT_BOX_ENV_FOO asks for FOO; the container gets FOO, not both."""
        config = Config()
        config.box.env = config.box.env.overlay([], {"AGENT_BOX_ENV_FOO": "bar"})
        flags = runtime.user_env(config)
        self.assertEqual(flags, ["--env=FOO=bar"])


class EnvArgumentParsingTests(unittest.TestCase):
    def test_the_flag_is_collected_before_the_agent(self):
        options, rest = parse(["--env", "TZ=UTC", "--env=MY_TOKEN", "codex"])
        self.assertEqual(options.env, [("TZ", "UTC"), ("MY_TOKEN", None)])
        self.assertEqual(rest, ["codex"])
        self.assertEqual(options.mounts, [])
        self.assertFalse(options.host)

    def test_a_flag_after_the_agent_belongs_to_the_agent(self):
        options, rest = parse(["codex", "--env", "theirs"])
        self.assertEqual(options.env, [])
        self.assertEqual(rest, ["codex", "--env", "theirs"])

    def test_it_combines_with_mounts_and_host_mode(self):
        options, rest = parse(["--mount", "/data", "--env", "TZ=UTC", "--host", "claude"])
        self.assertEqual((options.mounts, options.host, options.env, rest),
                         (["/data"], True, [("TZ", "UTC")], ["claude"]))


class EnvConfigSchemaTests(unittest.TestCase):
    """Everything here becomes a --env flag, so a malformed entry is dropped
    rather than carried to Podman, where it would define something else."""

    def test_defaults_are_empty(self):
        self.assertEqual(Config().box.env.forward, [])
        self.assertEqual(Config().box.env.set, {})

    def test_round_trips(self):
        config = _config(forward=["A"], assignments={"B": "2"})
        loaded = Config.from_dict(config.to_dict()).box.env
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
        config = Config.from_dict({"version": 1, "credentials": {"github": {}}})
        self.assertEqual(config.box.env.forward, [])


if __name__ == "__main__":
    unittest.main()
