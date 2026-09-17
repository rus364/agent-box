import contextlib
import io
import json
import os
import unittest
import unittest.mock

from agent_box import notify as notify_module
from agent_box import usage as usage_module
from agent_box.cli import notify_command, usage_command
from agent_box.errors import AgentBoxError
from agent_box.launch_options import LaunchOptions, parse, parse_spec


class LaunchOptionParsingTests(unittest.TestCase):
    def test_agent_arguments_pass_through_untouched(self):
        """Everything after the agent is the agent's, even spellings agent-box also uses."""
        options, rest = parse(["claude", "--model", "opus", "--host", "--mount", "/x", "--review", "off"])
        self.assertEqual(options, LaunchOptions())
        self.assertEqual(rest, ["claude", "--model", "opus", "--host", "--mount", "/x", "--review", "off"])

    def test_double_dash_ends_options_early(self):
        options, rest = parse(["--host", "--", "--review", "always"])
        self.assertTrue(options.host)
        self.assertEqual(rest, ["--review", "always"])

    def test_space_and_equals_forms_are_one_option(self):
        for argv in (["--mount", "/has spaces", "--review", "always"], ["--mount=/has spaces", "--review=always"]):
            with self.subTest(argv=argv):
                options, rest = parse([*argv, "codex"])
                self.assertEqual((options.mounts, options.overrides, rest),
                                 (["/has spaces"], {"review": "always"}, ["codex"]))

    def test_launch_options_shape_this_launch_and_are_not_session_overrides(self):
        """Mounts, env, tmux and KVM describe how to start, so the snapshot never carries them."""
        options, rest = parse(["--host", "--workspace", "/w", "--mount", "/a", "--mount", "/b:ro",
                               "--env", "TZ=UTC", "--env=TOKEN", "--no-tmux", "--no-kvm", "codex"])
        self.assertEqual(options, LaunchOptions(True, "/w", ["/a", "/b:ro"], [("TZ", "UTC"), ("TOKEN", None)],
                                                False, False, {}))
        self.assertEqual(rest, ["codex"])

    def test_session_options_become_overrides_in_one_normalized_form(self):
        options, _ = parse(["--worker", "codex:gpt-5.5:high",
                            "--reviewer", "claude@deepseek:deepseek-v4", "--rounds", "5", "--no-push",
                            "--gh-token", "--no-notify", "--tracker", "HTTPS://Tasks.Example.org/", "--project", "12"])
        self.assertEqual(options.overrides, {
            "worker": "codex:gpt-5.5:high", "reviewer": "claude@deepseek:deepseek-v4",
            "rounds": 5, "push": False, "gh-token": True, "notify": False,
            "tracker": "https://tasks.example.org", "project": "12"})

    def test_invalid_or_repeated_options_fail_before_anything_starts(self):
        """A typo must not start an agent with the intended setting silently missing."""
        for argv in (["--review", "sometimes"], ["--rounds", "0"],
                     ["--reviewer", "gpt:x"], ["--reviewer", "claude"], ["--reviewer", "claude::high"], ["--scout", "codex@Bad:m"],
                     ["--push=yes"], ["--review", "off", "--review", "auto"],
                     ["--mount"], ["--env", "1BAD"], ["--tracker", "ftp://tasks.example.org"]):
            with self.subTest(argv=argv), self.assertRaises(AgentBoxError):
                parse(argv)

    def test_the_first_foreign_option_starts_the_default_agents_arguments(self):
        """`agent-box --host --model x` passes --model to the default agent, so unknown flags end ours."""
        options, rest = parse(["--no-push", "--model", "x", "--review", "off"])
        self.assertEqual((options.overrides, rest), ({"push": False}, ["--model", "x", "--review", "off"]))

    def test_help_stays_a_command(self):
        for flag in ("--help", "-h"):
            self.assertEqual(parse([flag]), (LaunchOptions(), [flag]))

    def test_an_effort_suffix_is_optional_and_a_model_may_contain_colons(self):
        """Only a known effort is split off, so provider model IDs such as `name:free` survive."""
        self.assertEqual(parse_spec("--reviewer", "claude:opus:high"),
                         [{"provider": "claude", "model": "opus", "effort": "high", "fast": False, "api_provider": ""}])
        self.assertEqual(parse_spec("--worker", "codex@openrouter:deepseek/v4:free"),
                         [{"provider": "codex", "model": "deepseek/v4:free", "effort": "default", "fast": False, "api_provider": "openrouter"}])


class LaunchOptionDispatchTests(unittest.TestCase):
    def run_main(self, argv):
        from agent_box import cli, project_config, runtime
        with unittest.mock.patch.object(project_config, "load", return_value={}), \
             unittest.mock.patch("sys.argv", ["agent-box", *argv]), \
             unittest.mock.patch.object(runtime, "host_main", return_value=0) as host, \
             unittest.mock.patch.object(runtime, "runtime_main") as box, \
             unittest.mock.patch.object(runtime, "attach_main", return_value=0) as attach, \
             unittest.mock.patch.object(runtime, "host_session_main", return_value=0), \
             unittest.mock.patch.object(cli, "config_show") as show, \
             unittest.mock.patch.object(cli, "die", side_effect=SystemExit(1)) as die:
            try:
                cli.main()
            except SystemExit:
                pass
        return die, (host, box, attach, show)

    def test_options_that_cannot_apply_are_refused_instead_of_ignored(self):
        for argv, message in ((["--host", "--mount", "/x", "claude"], "--mount needs a container"),
                              (["--host", "--browser"], "--[no-]browser needs a container"),
                              (["--host", "--no-kvm", "codex"], "--no-kvm needs a container"),
                              (["--review", "always", "config", "show"], "not valid before config"),
                              (["--mount", "/x", "attach"], "not valid before attach")):
            with self.subTest(argv=argv):
                die, calls = self.run_main(argv)
                self.assertIn(message, die.call_args.args[0])
                for call in calls:
                    call.assert_not_called()

    def test_workspace_reaches_attach(self):
        die, (_, _, attach, _) = self.run_main(["--workspace", "/w", "attach"])
        die.assert_not_called()
        attach.assert_called_once_with([], "/w")

    def test_help_lists_every_option_and_command(self):
        """The option table renders the help, so nothing can be accepted without being documented."""
        from agent_box import cli
        from agent_box.launch_options import OPTIONS, spelling
        with contextlib.redirect_stdout(io.StringIO()) as output:
            cli.usage_text()
        for option in OPTIONS:
            self.assertIn(spelling(option), output.getvalue())
        for command in cli.NON_RUNTIME - {"-h", "--help"}:
            self.assertRegex(output.getvalue(), rf"\b{command}\b")


class HostSessionDispatchTests(unittest.TestCase):
    def test_host_attach_and_stop_never_dispatch_to_container_commands(self):
        """The explicit mode must survive CLI dispatch without touching Podman."""
        from agent_box import cli, project_config, runtime
        for command, args in (("attach", ["codex-123"]), ("stop", [])):
            with unittest.mock.patch.object(project_config, "load", return_value={}), \
                 unittest.mock.patch("sys.argv", ["agent-box", "--host", command, *args]), \
                 unittest.mock.patch.object(runtime, "host_session_main", return_value=7) as host, \
                 unittest.mock.patch.object(runtime, "attach_main") as attach, \
                 unittest.mock.patch.object(runtime, "stop_main") as stop, \
                 self.assertRaises(SystemExit) as result:
                cli.main()
            self.assertEqual(result.exception.code, 7)
            host.assert_called_once_with(args, stop=command == "stop", workspace_root="")
            attach.assert_not_called()
            stop.assert_not_called()

    def test_host_without_an_agent_is_left_to_the_default_agent(self):
        """A bare --host is a launch, not an error; the host launch resolves the default."""
        from agent_box import cli, project_config, runtime
        for argv, agent, rest in ((["--host"], None, []), (["--host", "--model", "x"], None, ["--model", "x"]),
                                  (["--host", "codex", "exec"], "codex", ["exec"])):
            with self.subTest(argv=argv), \
                 unittest.mock.patch.object(project_config, "load", return_value={}), \
                 unittest.mock.patch("sys.argv", ["agent-box", *argv]), \
                 unittest.mock.patch.object(runtime, "host_main", return_value=0) as host, \
                 self.assertRaises(SystemExit):
                cli.main()
            self.assertEqual(host.call_args.args[:2], (agent, rest))
            self.assertTrue(host.call_args.args[2].host)

    def test_host_refuses_container_only_commands(self):
        from agent_box import cli, project_config, runtime
        for command in ("shell", "doctor"):
            with self.subTest(command=command), \
                 unittest.mock.patch.object(project_config, "load", return_value={}), \
                 unittest.mock.patch("sys.argv", ["agent-box", "--host", command]), \
                 unittest.mock.patch.object(runtime, "host_main") as host, \
                 unittest.mock.patch.object(cli, "die", side_effect=SystemExit(1)) as die, \
                 self.assertRaises(SystemExit):
                cli.main()
            host.assert_not_called()
            self.assertIn("container-only", die.call_args.args[0])




class NotifyCodexDispatchTests(unittest.TestCase):
    """Codex supports exactly one `notify` program, so `agent-box notify
    codex` must drive both outbound notifications and the Flow-Next
    auto-push turn-complete adapter from the same one payload read."""

    def test_payload_argument_reaches_notify(self):
        payload = json.dumps({"type": "agent-turn-complete", "cwd": "/work/project"})
        with unittest.mock.patch.object(notify_module, "notify_codex") as notify_codex:
            notify_command(["codex", payload])
        notify_codex.assert_called_once_with(payload)

    def test_stdin_payload_reaches_notify(self):
        payload = json.dumps({"type": "agent-turn-complete", "cwd": "/work/project"})
        with unittest.mock.patch.object(notify_module, "notify_codex") as notify_codex, \
             unittest.mock.patch("sys.stdin") as stdin:
            stdin.read.return_value = payload
            notify_command(["codex"])
        notify_codex.assert_called_once_with(payload)

    def test_too_many_payload_arguments_dies(self):
        with self.assertRaises(SystemExit):
            notify_command(["codex", "a", "b"])


class UsageCommandColorTests(unittest.TestCase):
    """Colour separates segments, so only the combined line carries it."""

    def _run(self, args: list[str], custom: str = "") -> str:
        out = io.StringIO()
        with (
            unittest.mock.patch.dict(os.environ, {}, clear=True),
            unittest.mock.patch.object(
                usage_module, "claude_usage", return_value="88/59 · 2h29m/4d15h"),
            unittest.mock.patch.object(
                usage_module, "codex_usage", return_value="91/17 · 2h11m/4d9h"),
            unittest.mock.patch.object(usage_module, "custom_usage", return_value=custom),
            contextlib.redirect_stdout(out),
        ):
            usage_command(args)
        return out.getvalue().rstrip("\n")

    def test_single_provider_output_is_never_colored(self):
        for args in (["claude"], ["codex"]):
            with self.subTest(args=args):
                self.assertNotIn("\x1b[", self._run(args))

    def test_refresh_flags_allow_default_provider_and_any_order(self):
        """Fresh machine output must not require an otherwise optional provider."""
        for args, providers in ((["--json", "--no-cache"], ["codex", "claude"]),
                                (["--no-cache", "codex", "--json"], ["codex"])):
            with self.subTest(args=args), contextlib.redirect_stdout(io.StringIO()), \
                    unittest.mock.patch.object(usage_module, "usage_snapshot", return_value={"fresh": True}) as snapshot:
                usage_command(args)
                self.assertEqual(snapshot.call_args_list,
                                 [unittest.mock.call(p, no_cache=True) for p in providers])

    def test_single_provider_prints_only_the_numbers(self):
        self.assertEqual(self._run(["claude"]), "88/59 · 2h29m/4d15h")

    def test_combined_line_colors_every_segment(self):
        self.assertEqual(self._run(["all"]), usage_module.STATUSLINE_SEPARATOR.join([
            f"\x1b[{usage_module.CLAUDE_COLOR}m88/59 · 2h29m/4d15h{usage_module.COLOR_OFF}",
            f"\x1b[{usage_module.CODEX_COLOR}m91/17 · 2h11m/4d9h{usage_module.COLOR_OFF}",
        ]))

    def test_configured_command_adds_its_own_hue_after_codex(self):
        """Without a provider name, only a distinct hue tells the extra segment apart."""
        self.assertEqual(self._run(["all"], custom="$4.20"), usage_module.STATUSLINE_SEPARATOR.join([
            f"\x1b[{usage_module.CLAUDE_COLOR}m88/59 · 2h29m/4d15h{usage_module.COLOR_OFF}",
            f"\x1b[{usage_module.CODEX_COLOR}m91/17 · 2h11m/4d9h{usage_module.COLOR_OFF}",
            f"\x1b[{usage_module.CUSTOM_COLOR}m$4.20{usage_module.COLOR_OFF}",
        ]))
        self.assertNotIn(usage_module.CUSTOM_COLOR, (usage_module.CONTEXT_COLOR, usage_module.CLAUDE_COLOR,
                                                     usage_module.CODEX_COLOR))

    def test_every_provider_rejects_arguments(self):
        for args in (["claude", "--statusline"], ["codex", "-x"], ["all", "--statusline"]):
            with (
                self.subTest(args=args),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                self._run(args)

    def test_no_color_disables_the_combined_line_too(self):
        out = io.StringIO()
        with (
            unittest.mock.patch.dict(os.environ, {"NO_COLOR": "1"}, clear=True),
            unittest.mock.patch.object(usage_module, "claude_usage", return_value="a"),
            unittest.mock.patch.object(usage_module, "codex_usage", return_value="b"),
            unittest.mock.patch.object(usage_module, "custom_usage", return_value=""),
            contextlib.redirect_stdout(out),
        ):
            usage_command(["all"])
        self.assertEqual(out.getvalue().rstrip("\n"), "a │ b")


if __name__ == "__main__":
    unittest.main()


class PathStatusTests(unittest.TestCase):
    """Silence would read as success, so an unfinished PATH setup has to speak.

    ``config`` and ``config apply`` both end here. The two unfinished states
    need different advice: a directory was chosen but the shell has not picked
    it up yet, or no directory was ever chosen at all.
    """

    def status(self, bin_dir, kind):
        from pathlib import Path
        from agent_box import pathsetup
        from agent_box.cli import path_status
        from agent_box.config import Config
        config = Config()
        config.install.bin_dir = bin_dir
        found = Path(bin_dir or "/nowhere") / "agent-box"
        with unittest.mock.patch.object(
                pathsetup, "link_state", return_value=pathsetup.LinkState(kind, found)):
            return path_status(config)

    def test_a_working_link_says_nothing(self):
        self.assertEqual(self.status("/home/somebody/.local/bin", "ours"), [])

    def test_a_chosen_directory_the_shell_has_not_picked_up_gets_the_path_line(self):
        lines = self.status("/home/somebody/.local/bin", "missing")
        self.assertIn("not on PATH yet in this shell", lines[0])
        self.assertTrue(any("export PATH=" in line for line in lines))

    def test_no_chosen_directory_gets_the_commands_to_finish_by_hand(self):
        lines = self.status("", "missing")
        self.assertIn("agent-box is not on PATH", lines[0])
        self.assertTrue(any(line.strip().startswith("ln -s") for line in lines))
