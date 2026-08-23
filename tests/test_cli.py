import json
import unittest
import unittest.mock

from agent_box import flow_push
from agent_box import notify as notify_module
from agent_box.cli import flow_next_command, notify_command, parse_global_args


class ParseGlobalArgsTests(unittest.TestCase):
    def test_no_options_passes_through(self):
        mounts, host, rest = parse_global_args(["claude", "--model", "opus"])
        self.assertEqual(mounts, [])
        self.assertFalse(host)
        self.assertEqual(rest, ["claude", "--model", "opus"])

    def test_single_mount_space_form(self):
        mounts, host, rest = parse_global_args(["--mount", "/foo", "claude"])
        self.assertEqual(mounts, ["/foo"])
        self.assertFalse(host)
        self.assertEqual(rest, ["claude"])

    def test_mount_equals_form(self):
        mounts, host, rest = parse_global_args(["--mount=/foo", "claude"])
        self.assertEqual(mounts, ["/foo"])
        self.assertEqual(rest, ["claude"])

    def test_multiple_mounts(self):
        mounts, host, rest = parse_global_args(["--mount", "/a", "--mount", "/b:ro", "codex"])
        self.assertEqual(mounts, ["/a", "/b:ro"])
        self.assertEqual(rest, ["codex"])

    def test_provider_own_model_flag_never_consumed(self):
        # Claude/Codex's own flags must never be misread as agent-box's.
        mounts, host, rest = parse_global_args(["claude", "-p", "do something", "--model", "opus"])
        self.assertEqual(mounts, [])
        self.assertEqual(rest, ["claude", "-p", "do something", "--model", "opus"])

    def test_double_dash_stops_global_parsing(self):
        mounts, host, rest = parse_global_args(["--", "claude", "--mount", "notours"])
        self.assertEqual(mounts, [])
        self.assertEqual(rest, ["claude", "--mount", "notours"])

    def test_mount_after_subcommand_is_not_consumed_as_ours(self):
        # Grammar: our options must precede the subcommand.
        mounts, host, rest = parse_global_args(["claude", "--mount", "/foo"])
        self.assertEqual(mounts, [])
        self.assertEqual(rest, ["claude", "--mount", "/foo"])

    def test_bare_run_no_args(self):
        mounts, host, rest = parse_global_args([])
        self.assertEqual(mounts, [])
        self.assertFalse(host)
        self.assertEqual(rest, [])

    def test_path_with_spaces_stays_one_token(self):
        mounts, host, rest = parse_global_args(["--mount", "/has spaces/dir", "claude"])
        self.assertEqual(mounts, ["/has spaces/dir"])

    def test_host_flag_recognized(self):
        mounts, host, rest = parse_global_args(["--host", "claude", "--model", "opus"])
        self.assertTrue(host)
        self.assertEqual(mounts, [])
        self.assertEqual(rest, ["claude", "--model", "opus"])

    def test_host_flag_after_subcommand_is_not_consumed_as_ours(self):
        mounts, host, rest = parse_global_args(["claude", "--host"])
        self.assertFalse(host)
        self.assertEqual(rest, ["claude", "--host"])

    def test_host_and_mount_combine(self):
        mounts, host, rest = parse_global_args(["--mount", "/foo", "--host", "codex"])
        self.assertEqual(mounts, ["/foo"])
        self.assertTrue(host)
        self.assertEqual(rest, ["codex"])


class FlowNextDispatchTests(unittest.TestCase):
    """`flow_next_command` is Claude's Stop hook entry point only now --
    the PostToolUse/`flowctl done` detector was removed along with the
    marker mechanism it drove."""

    def test_stop_dispatches_to_run_stop_hook(self):
        with unittest.mock.patch.object(flow_push, "run_stop_hook") as run_stop_hook, \
             unittest.mock.patch("sys.stdin") as stdin:
            stdin.read.return_value = '{"cwd": "/work"}'
            flow_next_command(["stop"])
        run_stop_hook.assert_called_once_with('{"cwd": "/work"}')

    def test_post_tool_use_is_no_longer_a_valid_action(self):
        with self.assertRaises(SystemExit):
            flow_next_command(["post-tool-use"])

    def test_unknown_action_dies(self):
        with self.assertRaises(SystemExit):
            flow_next_command(["bogus"])


class NotifyCodexDispatchTests(unittest.TestCase):
    """Codex supports exactly one `notify` program, so `agent-box notify
    codex` must drive both outbound notifications and the Flow-Next
    auto-push turn-complete adapter from the same one payload read."""

    def test_payload_argument_reaches_both_notify_and_flow_push(self):
        payload = json.dumps({"type": "agent-turn-complete", "cwd": "/work/project"})
        with unittest.mock.patch.object(notify_module, "notify_codex") as notify_codex, \
             unittest.mock.patch.object(flow_push, "run_codex_turn_complete_hook") as run_hook:
            notify_command(["codex", payload])
        notify_codex.assert_called_once_with(payload)
        run_hook.assert_called_once_with(payload)

    def test_stdin_payload_reaches_both_notify_and_flow_push(self):
        payload = json.dumps({"type": "agent-turn-complete", "cwd": "/work/project"})
        with unittest.mock.patch.object(notify_module, "notify_codex") as notify_codex, \
             unittest.mock.patch.object(flow_push, "run_codex_turn_complete_hook") as run_hook, \
             unittest.mock.patch("sys.stdin") as stdin:
            stdin.read.return_value = payload
            notify_command(["codex"])
        notify_codex.assert_called_once_with(payload)
        run_hook.assert_called_once_with(payload)

    def test_too_many_payload_arguments_dies(self):
        with self.assertRaises(SystemExit):
            notify_command(["codex", "a", "b"])


if __name__ == "__main__":
    unittest.main()
