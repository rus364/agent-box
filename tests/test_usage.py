import os
import unittest
from unittest import mock

from agent_box import usage
from agent_box.errors import AgentBoxError


class UsageFormattingTests(unittest.TestCase):
    def test_structured_usage_preserves_model_limits(self):
        result = usage.usage_windows("claude", {
            "seven_day": {"utilization": 40, "resets_at": 2000},
            "seven_day_sonnet": {"utilization": 90, "resets_at": 3000},
        }, observed_at=1000)
        self.assertEqual(result[1]["models"], ["sonnet"])
        self.assertEqual(result[1]["remaining"], 10)

    def test_no_cache_failure_never_reports_cached_data_as_fresh(self):
        import json
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "cache"
            cache.mkdir()
            auth = Path(tmp) / "auth.json"
            auth.write_text(json.dumps({"tokens": {"access_token": "secret"}}))
            (cache / "codex").mkdir()
            (cache / "codex/usage.json").write_text(json.dumps({"rate_limit": {
                "primary_window": {"used_percent": 100, "limit_window_seconds": 18000,
                                   "reset_at": 2000000000}}}))
            with mock.patch.object(usage, "XDG_CACHE_HOME", cache), \
                 mock.patch.dict(os.environ, {"CODEX_AUTH_PATH": str(auth)}), \
                 mock.patch.object(usage, "http_json", side_effect=OSError("offline")):
                result = usage.usage_snapshot("codex", no_cache=True)
            self.assertFalse(result["fresh"])
            self.assertIn("error", result)
            self.assertEqual(result["windows"][0]["remaining"], 0)

    def test_compact_time(self):
        self.assertEqual(usage.compact_time(3660), "1h1m")
        self.assertEqual(usage.compact_time(176400), "2d1h")
        self.assertEqual(usage.compact_time(59), "0m")

    def test_codex_usage_windows(self):
        data = {"rate_limit": {
            "primary_window": {"used_percent": 25, "limit_window_seconds": 18000, "reset_after_seconds": 3660},
            "secondary_window": {"used_percent": 40, "limit_window_seconds": 604800, "reset_after_seconds": 176400},
        }}
        self.assertEqual(usage.format_codex_usage(data), "75/60 · 1h1m/2d1h")

    def test_codex_usage_missing_windows_raises(self):
        with self.assertRaises(AgentBoxError):
            usage.format_codex_usage({"rate_limit": {}})

    def test_claude_usage_windows(self):
        now = 1_800_000_000
        data = {"rate_limits": {
            "five_hour": {"used_percentage": 25, "resets_at": now + 3660},
            "seven_day": {"used_percentage": 40, "resets_at": now + 176400},
        }}
        self.assertEqual(usage.format_claude_usage(data, now), "75/60 · 1h1m/2d1h")

    def test_claude_usage_normalizes_legacy_shape(self):
        now = 1_800_000_000
        data = {"five_hour": {"utilization": 10, "resets_at": now + 60}, "seven_day": None}
        normalized = usage.normalize_claude_usage(data)
        self.assertIn("five_hour", normalized["rate_limits"])

    def test_claude_usage_expired_window_shows_placeholder(self):
        now = 1_800_000_000
        data = {"rate_limits": {"five_hour": {"used_percentage": 10, "resets_at": now - 5}, "seven_day": None}}
        self.assertEqual(usage.format_claude_usage(data, now), "—/— · —/—")

    def test_compact_tokens(self):
        self.assertEqual(usage.compact_tokens(842), "842")
        self.assertEqual(usage.compact_tokens(62_385), "62k")
        self.assertEqual(usage.compact_tokens(200_000), "200k")
        self.assertEqual(usage.compact_tokens(1_000_000), "1.0M")

    CONTEXT_PAYLOAD = {"context_window": {
        "total_input_tokens": 62_385, "context_window_size": 200_000,
        "used_percentage": 31, "remaining_percentage": 69,
    }}

    def test_context_window_segment_is_plain(self):
        # Colour is the caller's decision: only the combined line has segments
        # to tell apart, so the formatter never applies it.
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                usage.format_context_window(self.CONTEXT_PAYLOAD), "ctx 31% · 62k/200k")

    def test_color_ends_without_clearing_the_dim_status_line(self):
        # Bold and foreground are turned off individually. A full reset would
        # also drop the dim attribute Claude Code wraps the status line in,
        # brightening everything printed after this segment.
        with mock.patch.dict(os.environ, {}, clear=True):
            colored = usage.colorize("x", usage.CLAUDE_COLOR)
        self.assertEqual(colored, f"\x1b[{usage.CLAUDE_COLOR}mx{usage.COLOR_OFF}")
        self.assertNotIn("\x1b[0m", colored)
        self.assertEqual(usage.COLOR_OFF, "\x1b[22;39m")

    def test_no_color_env_disables_color_everywhere(self):
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            self.assertEqual(usage.colorize("Codex 1/2", usage.CODEX_COLOR), "Codex 1/2")

    def test_context_window_absent_before_the_first_assistant_message(self):
        for payload in (
            {},
            {"context_window": None},
            {"context_window": {"context_window_size": 200_000, "used_percentage": None}},
            {"context_window": {"used_percentage": 0, "context_window_size": 0}},
            # Infinity parses from JSON but would raise in compact_tokens.
            {"context_window": {
                "used_percentage": 5, "context_window_size": float("inf"),
                "total_input_tokens": 1}},
        ):
            with self.subTest(payload=payload):
                self.assertEqual(usage.format_context_window(payload), "")

    #: 2025-02-01 17:00:00 UTC; tests render it in UTC.
    EXPIRES_AT = 1_738_429_200

    def _cache(self, **cache) -> str:
        with mock.patch.object(usage.time, "localtime", usage.time.gmtime):
            return usage.format_context_window(
                {"prompt_cache": {"caching_observed": True, **cache}},
                now=self.EXPIRES_AT - 600)

    def test_warm_cache_shows_the_clock_time_it_goes_cold(self):
        # A countdown would freeze while the session idles, because Claude
        # Code redraws only on session events; a clock time stays true.
        self.assertEqual(self._cache(warm=True, expires_at=self.EXPIRES_AT), "cache →17:00")

    def test_cold_cache_names_the_tokens_the_next_request_recaches(self):
        self.assertEqual(
            self._cache(warm=False, expires_at=None, recache_tokens_if_cold=45_000),
            "cache cold · 45k")
        self.assertEqual(self._cache(warm=False, recache_tokens_if_cold=None), "cache cold")
        # An unusable token count drops only the suffix, never the whole line.
        self.assertEqual(
            self._cache(warm=False, recache_tokens_if_cold=float("inf")), "cache cold")

    def test_a_stale_warm_payload_past_its_expiry_is_cold(self):
        self.assertEqual(
            self._cache(warm=True, expires_at=self.EXPIRES_AT - 601), "cache cold")

    def test_cache_part_absent_without_observed_caching_or_usable_fields(self):
        for cache in (
            {"caching_observed": False},
            {"warm": True},
            {"warm": "yes", "expires_at": self.EXPIRES_AT},
            # Beyond the platform's localtime range: must not abort the line.
            {"warm": True, "expires_at": float("inf")},
        ):
            with self.subTest(cache=cache):
                self.assertEqual(self._cache(**cache), "")
        self.assertEqual(usage.format_context_window({"prompt_cache": None}), "")

    def test_session_line_counts_come_from_the_payload_cost(self):
        self.assertEqual(
            usage.format_context_window(
                {"cost": {"total_lines_added": 156, "total_lines_removed": 23}}),
            "+156/−23")
        for cost in ({"total_lines_added": 0, "total_lines_removed": 0}, {}, None):
            with self.subTest(cost=cost):
                self.assertEqual(usage.format_context_window({"cost": cost}), "")

    def test_one_unusable_part_does_not_hide_the_others(self):
        payload = {
            **self.CONTEXT_PAYLOAD,
            "prompt_cache": {"caching_observed": True, "warm": True},
            "cost": {"total_lines_added": 5, "total_lines_removed": 2},
        }
        self.assertEqual(usage.format_context_window(payload), "ctx 31% · 62k/200k · +5/−2")

    def test_join_segments_drops_empty_ones(self):
        self.assertEqual(usage.join_segments("", "Claude a", "Codex b"), "Claude a │ Codex b")
        self.assertEqual(usage.join_segments("ctx 3%", "Claude a"), "ctx 3% │ Claude a")

    def test_parse_epoch_handles_z_suffix(self):
        self.assertIsNotNone(usage.parse_epoch("2024-01-01T00:00:00Z"))
        self.assertIsNone(usage.parse_epoch("not-a-date"))


class CustomUsageTests(unittest.TestCase):
    """The user's `statusline.command` runs on status-line redraws, so it must stay cheap and inert."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        cache = mock.patch.object(usage, "CUSTOM_USAGE_CACHE", self.root / "cache/usage-command.json")
        cache.start()
        self.addCleanup(cache.stop)

    def _custom(self, command):
        with mock.patch.object(usage, "configured_custom_command", return_value=command):
            return usage.custom_usage()

    def test_no_command_adds_no_segment_and_runs_nothing(self):
        with mock.patch.object(usage.subprocess, "Popen") as popen:
            self.assertEqual(self._custom(""), "")
        popen.assert_not_called()

    def test_first_non_empty_line_is_the_segment(self):
        self.assertEqual(self._custom("printf '\\n  $4.20  \\nsecond\\n'"), "$4.20")

    def test_failure_or_silence_shows_a_placeholder_instead_of_breaking_the_line(self):
        for command in ("echo partial; exit 3", "true", "exec /nonexistent/binary"):
            with self.subTest(command=command):
                self.assertEqual(self._custom(command), usage.CUSTOM_PLACEHOLDER)

    def test_timeout_kills_children_that_keep_the_output_pipe_open(self):
        """Killing only `sh` would leave `sleep` holding stdout and the redraw waiting on it."""
        import time
        started = time.monotonic()
        with mock.patch.object(usage, "CUSTOM_TIMEOUT_SECONDS", 0.3):
            self.assertEqual(self._custom("sleep 30; echo late"), usage.CUSTOM_PLACEHOLDER)
        self.assertLess(time.monotonic() - started, 5)

    def test_escape_sequences_are_removed_and_long_output_is_clipped(self):
        self.assertEqual(self._custom("printf '\\033[2J\\033[31mred\\033[0m\\n'"), "[2J[31mred[0m")
        clipped = self._custom(f"printf '{'x' * 100}'")
        self.assertEqual(len(clipped), usage.CUSTOM_MAX_WIDTH)
        self.assertTrue(clipped.endswith("…"))

    def test_cache_serves_the_same_command_but_never_another_one(self):
        runs = self.root / "runs"
        first = f"echo run >> {runs}; echo one"
        self.assertEqual(self._custom(first), "one")
        self.assertEqual(self._custom(first), "one")
        self.assertEqual(runs.read_text().count("run"), 1)
        self.assertEqual(self._custom(f"echo run >> {runs}; echo two"), "two")
        self.assertNotIn("echo", usage.CUSTOM_USAGE_CACHE.read_text(), "command text must not be cached")

    def test_failures_are_cached_so_a_slow_command_does_not_stall_every_redraw(self):
        runs = self.root / "runs"
        command = f"echo run >> {runs}; exit 1"
        for _ in range(2):
            self.assertEqual(self._custom(command), usage.CUSTOM_PLACEHOLDER)
        self.assertEqual(runs.read_text().count("run"), 1)

    def test_box_session_reads_the_command_from_its_snapshot(self):
        """Claude's status line runs in the box, where the saved config is not mounted."""
        from agent_box import runtime
        from agent_box.config import Config
        config = Config()
        config.agents.claude.statusline.command = "echo balance"
        runtime.sync_tracker_runtime(config, self.root / "session", policy=True)
        snapshot = self.root / "session/config.json"
        with mock.patch.dict(os.environ, {"AGENT_BOX_RUNTIME_CONFIG": str(snapshot)}, clear=True):
            self.assertEqual(usage.configured_custom_command(), "echo balance")


if __name__ == "__main__":
    unittest.main()
