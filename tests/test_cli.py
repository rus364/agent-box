import contextlib
import io
import json
import os
import unittest
import unittest.mock

from agent_box import notify as notify_module
from agent_box import usage as usage_module
from agent_box.cli import notify_command, parse_global_args, usage_command


class ParseGlobalArgsTests(unittest.TestCase):
    def test_no_options_passes_through(self):
        mounts, host, _env, rest = parse_global_args(["claude", "--model", "opus"])
        self.assertEqual(mounts, [])
        self.assertFalse(host)
        self.assertEqual(rest, ["claude", "--model", "opus"])

    def test_single_mount_space_form(self):
        mounts, host, _env, rest = parse_global_args(["--mount", "/foo", "claude"])
        self.assertEqual(mounts, ["/foo"])
        self.assertFalse(host)
        self.assertEqual(rest, ["claude"])

    def test_mount_equals_form(self):
        mounts, host, _env, rest = parse_global_args(["--mount=/foo", "claude"])
        self.assertEqual(mounts, ["/foo"])
        self.assertEqual(rest, ["claude"])

    def test_multiple_mounts(self):
        mounts, host, _env, rest = parse_global_args(["--mount", "/a", "--mount", "/b:ro", "codex"])
        self.assertEqual(mounts, ["/a", "/b:ro"])
        self.assertEqual(rest, ["codex"])

    def test_provider_own_model_flag_never_consumed(self):
        # Claude/Codex's own flags must never be misread as agent-box's.
        mounts, host, _env, rest = parse_global_args(["claude", "-p", "do something", "--model", "opus"])
        self.assertEqual(mounts, [])
        self.assertEqual(rest, ["claude", "-p", "do something", "--model", "opus"])

    def test_double_dash_stops_global_parsing(self):
        mounts, host, _env, rest = parse_global_args(["--", "claude", "--mount", "notours"])
        self.assertEqual(mounts, [])
        self.assertEqual(rest, ["claude", "--mount", "notours"])

    def test_mount_after_subcommand_is_not_consumed_as_ours(self):
        # Grammar: our options must precede the subcommand.
        mounts, host, _env, rest = parse_global_args(["claude", "--mount", "/foo"])
        self.assertEqual(mounts, [])
        self.assertEqual(rest, ["claude", "--mount", "/foo"])

    def test_bare_run_no_args(self):
        mounts, host, _env, rest = parse_global_args([])
        self.assertEqual(mounts, [])
        self.assertFalse(host)
        self.assertEqual(rest, [])

    def test_path_with_spaces_stays_one_token(self):
        mounts, host, _env, rest = parse_global_args(["--mount", "/has spaces/dir", "claude"])
        self.assertEqual(mounts, ["/has spaces/dir"])

    def test_host_flag_recognized(self):
        mounts, host, _env, rest = parse_global_args(["--host", "claude", "--model", "opus"])
        self.assertTrue(host)
        self.assertEqual(mounts, [])
        self.assertEqual(rest, ["claude", "--model", "opus"])

    def test_host_flag_after_subcommand_is_not_consumed_as_ours(self):
        mounts, host, _env, rest = parse_global_args(["claude", "--host"])
        self.assertFalse(host)
        self.assertEqual(rest, ["claude", "--host"])

    def test_host_and_mount_combine(self):
        mounts, host, _env, rest = parse_global_args(["--mount", "/foo", "--host", "codex"])
        self.assertEqual(mounts, ["/foo"])
        self.assertTrue(host)
        self.assertEqual(rest, ["codex"])


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
            host.assert_called_once_with(args, stop=command == "stop")
            attach.assert_not_called()
            stop.assert_not_called()




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

    def _run(self, args: list[str]) -> str:
        out = io.StringIO()
        with (
            unittest.mock.patch.dict(os.environ, {}, clear=True),
            unittest.mock.patch.object(
                usage_module, "claude_usage", return_value="88/59 · 2h29m/4d15h"),
            unittest.mock.patch.object(
                usage_module, "codex_usage", return_value="91/17 · 2h11m/4d9h"),
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
