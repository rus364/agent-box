import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_box import statusline
from agent_box import usage


def strip_color(text: str) -> str:
    result: list[str] = []
    index = 0
    while index < len(text):
        if text.startswith("\x1b[", index):
            end = text.find("m", index)
            index = len(text) if end < 0 else end + 1
            continue
        result.append(text[index])
        index += 1
    return "".join(result)


class PromptSegmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.no_color = mock.patch.dict(os.environ, {"NO_COLOR": "1"}, clear=True)
        self.no_color.start()
        self.addCleanup(self.no_color.stop)

    def test_home_directory_is_shown_as_a_tilde(self):
        with mock.patch.object(statusline, "git_branch", return_value=""):
            self.assertEqual(
                statusline.format_prompt(statusline.HOST_HOME, now="09:41"),
                "➜ [09:41] ~",
            )

    def test_directory_is_the_trailing_component(self):
        with mock.patch.object(statusline, "git_branch", return_value=""):
            self.assertEqual(
                statusline.format_prompt(Path("/srv/work/project"), now="09:41"),
                "➜ [09:41] project",
            )

    def test_virtual_env_is_named_before_the_directory(self):
        with mock.patch.object(statusline, "git_branch", return_value=""):
            self.assertEqual(
                statusline.format_prompt(
                    Path("/srv/work/project"), now="09:41",
                    virtual_env="/srv/work/project/.venv",
                ),
                "➜ [09:41] (.venv) project",
            )

    def test_clean_and_dirty_git_segments(self):
        for dirty, expected in ((False, "git:(main)"), (True, "git:(main) ✗")):
            with self.subTest(dirty=dirty), (
                mock.patch.object(statusline, "git_branch", return_value="main")
            ), mock.patch.object(statusline, "worktree_dirty", return_value=dirty):
                self.assertEqual(
                    statusline.format_prompt(Path("/srv/work/project"), now="09:41"),
                    f"➜ [09:41] project {expected}",
                )

    def test_no_git_segment_outside_a_repository(self):
        with mock.patch.object(statusline, "git_branch", return_value=""):
            self.assertNotIn(
                "git:", statusline.format_prompt(Path("/srv/work/project"), now="09:41"))


class GitProbeTests(unittest.TestCase):
    """The real git calls, against a real repository."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(self.repo)],
            check=True, capture_output=True, env=env)

    def test_branch_of_a_fresh_repository(self):
        self.assertEqual(statusline.git_branch(self.repo), "main")
        self.assertFalse(statusline.worktree_dirty(self.repo))

    def test_untracked_file_makes_the_worktree_dirty(self):
        (self.repo / "new.txt").write_text("x")
        self.assertTrue(statusline.worktree_dirty(self.repo))

    def test_outside_a_repository_there_is_no_branch(self):
        with tempfile.TemporaryDirectory() as plain:
            self.assertEqual(statusline.git_branch(Path(plain)), "")

    def test_a_git_failure_is_never_fatal(self):
        with mock.patch.object(
            statusline.subprocess, "run", side_effect=OSError("no git")
        ):
            self.assertEqual(statusline.git_output(self.repo, "status"), "")
            self.assertEqual(statusline.git_branch(self.repo), "")

    def test_a_slow_repository_loses_its_segment_instead_of_stalling(self):
        with mock.patch.object(
            statusline.subprocess, "run",
            side_effect=subprocess.TimeoutExpired("git", statusline.GIT_TIMEOUT),
        ):
            self.assertEqual(statusline.git_branch(self.repo), "")


class PayloadTests(unittest.TestCase):
    def test_workspace_directory_wins_over_cwd(self):
        self.assertEqual(
            statusline.payload_cwd({"workspace": {"current_dir": "/a"}, "cwd": "/b"}),
            Path("/a"),
        )

    def test_cwd_is_the_fallback_claude_code_sometimes_sends(self):
        self.assertEqual(statusline.payload_cwd({"cwd": "/b"}), Path("/b"))

    def test_an_empty_payload_still_resolves_a_directory(self):
        self.assertEqual(statusline.payload_cwd({}), Path.cwd())


class RenderTests(unittest.TestCase):
    PAYLOAD = json.dumps({
        "workspace": {"current_dir": "/srv/work/project"},
        "context_window": {
            "total_input_tokens": 62_385, "context_window_size": 200_000,
            "used_percentage": 31,
        },
    })

    def _render(self, payload: str, env: dict[str, str]) -> str:
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(statusline, "git_branch", return_value="main"),
            mock.patch.object(statusline, "worktree_dirty", return_value=True),
            mock.patch.object(usage, "claude_usage", return_value="88/59 · 2h29m/4d15h"),
            mock.patch.object(usage, "codex_usage", return_value="91/17 · 2h11m/4d9h"),
        ):
            return statusline.render(payload, now="09:41")

    def test_complete_line(self):
        self.assertEqual(
            self._render(self.PAYLOAD, {"NO_COLOR": "1"}),
            "➜ [09:41] project git:(main) ✗ │ ctx 31% · 62k/200k"
            " │ Claude 88/59 · 2h29m/4d15h │ Codex 91/17 · 2h11m/4d9h",
        )

    def test_colored_line_carries_the_same_text(self):
        colored = self._render(self.PAYLOAD, {})
        self.assertIn("\x1b[", colored)
        self.assertEqual(strip_color(colored), self._render(self.PAYLOAD, {"NO_COLOR": "1"}))

    def test_color_never_clears_the_dim_claude_code_applies(self):
        self.assertNotIn("\x1b[0m", self._render(self.PAYLOAD, {}))

    def test_context_segment_is_absent_before_the_first_assistant_message(self):
        line = self._render(json.dumps(
            {"workspace": {"current_dir": "/srv/work/project"}}), {"NO_COLOR": "1"})
        self.assertNotIn("ctx", line)
        self.assertIn("Claude 88/59", line)

    def test_unusable_usage_degrades_to_placeholders(self):
        with (
            mock.patch.dict(os.environ, {"NO_COLOR": "1"}, clear=True),
            mock.patch.object(statusline, "git_branch", return_value=""),
            mock.patch.object(usage, "claude_usage", side_effect=RuntimeError("offline")),
            mock.patch.object(usage, "codex_usage", side_effect=RuntimeError("offline")),
        ):
            line = statusline.render(self.PAYLOAD, now="09:41")
        self.assertEqual(
            line,
            f"➜ [09:41] project │ ctx 31% · 62k/200k"
            f" │ Claude {usage.PLACEHOLDER_USAGE} │ Codex {usage.PLACEHOLDER_USAGE}",
        )

    def test_session_rate_limits_are_preferred_over_the_cached_usage(self):
        """The payload already carries the session's limits, so no HTTP call."""
        payload = json.dumps({
            "workspace": {"current_dir": "/srv/work/project"},
            "rate_limits": {
                "five_hour": {"used_percentage": 12, "resets_at": time.time() + 3600},
                "seven_day": {"used_percentage": 34, "resets_at": time.time() + 86_400},
            },
        })
        with (
            mock.patch.dict(os.environ, {"NO_COLOR": "1"}, clear=True),
            mock.patch.object(statusline, "git_branch", return_value=""),
            mock.patch.object(usage, "atomic_write_json"),
            mock.patch.object(usage, "claude_usage", side_effect=AssertionError("no fallback")),
            mock.patch.object(usage, "codex_usage", return_value="b"),
        ):
            line = statusline.render(payload, now="09:41")
        self.assertIn("Claude 88/66 · ", line)

    def test_unparseable_payload_still_renders_a_line(self):
        with (
            mock.patch.dict(os.environ, {"NO_COLOR": "1"}, clear=True),
            mock.patch.object(statusline, "git_branch", return_value=""),
            mock.patch.object(usage, "claude_usage", return_value="a"),
            mock.patch.object(usage, "codex_usage", return_value="b"),
        ):
            self.assertIn("Claude a", statusline.render("not json", now="09:41"))


if __name__ == "__main__":
    unittest.main()
