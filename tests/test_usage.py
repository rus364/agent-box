import unittest

from agent_box import usage
from agent_box.errors import AgentBoxError


class UsageFormattingTests(unittest.TestCase):
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

    def test_parse_epoch_handles_z_suffix(self):
        self.assertIsNotNone(usage.parse_epoch("2024-01-01T00:00:00Z"))
        self.assertIsNone(usage.parse_epoch("not-a-date"))


if __name__ == "__main__":
    unittest.main()
