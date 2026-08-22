import unittest

from agent_box.cli import parse_global_args


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


if __name__ == "__main__":
    unittest.main()
