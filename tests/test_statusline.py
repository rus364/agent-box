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

    def test_uncommitted_diff_follows_the_dirty_marker(self):
        with mock.patch.object(statusline, "git_branch", return_value="main"), \
                mock.patch.object(statusline, "worktree_dirty", return_value=True), \
                mock.patch.object(statusline, "worktree_diff", return_value="+42/−7"):
            self.assertEqual(
                statusline.format_prompt(Path("/srv/work/project"), now="09:41"),
                "➜ [09:41] project git:(main) ✗ +42/−7",
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
        self.env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(self.repo)],
            check=True, capture_output=True, env=self.env)

    def test_branch_of_a_fresh_repository(self):
        self.assertEqual(statusline.git_branch(self.repo), "main")
        self.assertFalse(statusline.worktree_dirty(self.repo))

    def test_untracked_file_makes_the_worktree_dirty(self):
        (self.repo / "new.txt").write_text("x")
        self.assertTrue(statusline.worktree_dirty(self.repo))

    def _git(self, *args: str) -> None:
        subprocess.run(
            ["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
            check=True, capture_output=True, env=self.env)

    def test_diff_counts_uncommitted_tracked_lines_against_head(self):
        """The repository's own diff, which a commit clears; untracked files are left to ✗."""
        (self.repo / "a.txt").write_text("one\ntwo\nthree\n")
        self.assertEqual(statusline.worktree_diff(self.repo), "")  # no HEAD yet
        self._git("add", "a.txt")
        self._git("commit", "-q", "-m", "init")
        self.assertEqual(statusline.worktree_diff(self.repo), "")
        (self.repo / "a.txt").write_text("one\nTWO\nthree\nfour\n")
        (self.repo / "untracked.txt").write_text("x\n")
        self.assertEqual(statusline.worktree_diff(self.repo), "+2/−1")
        (self.repo / "a.txt").write_text("one\n")
        self.assertEqual(statusline.worktree_diff(self.repo), "+0/−2")

    def test_changes_without_line_counts_show_no_count(self):
        """A binary or empty change is left to ✗; a "+0/−0" count would read as a defect."""
        (self.repo / "a.bin").write_bytes(b"\x00\x01")
        (self.repo / "empty.txt").write_text("")
        self._git("add", ".")
        self._git("commit", "-q", "-m", "init")
        (self.repo / "a.bin").write_bytes(b"\x00\x02")
        self._git("rm", "-q", "empty.txt")
        self.assertTrue(statusline.worktree_dirty(self.repo))
        self.assertEqual(statusline.worktree_diff(self.repo), "")

    def test_diff_count_does_not_depend_on_the_callers_locale(self):
        """Host sessions keep the caller's locale, and git translates its --shortstat summary."""
        (self.repo / "a.txt").write_text("one\n")
        self._git("add", "a.txt")
        self._git("commit", "-q", "-m", "init")
        (self.repo / "a.txt").write_text("one\ntwo\n")
        with mock.patch.dict(os.environ, {"LANGUAGE": "de", "LC_ALL": "de_DE.UTF-8", "LANG": "de_DE.UTF-8"}):
            if "insertion" in statusline.git_output(self.repo, "diff", "--shortstat", "HEAD"):
                self.skipTest("git has no German translation installed here")
            self.assertEqual(statusline.worktree_diff(self.repo), "+1/−0")

    def test_outside_a_repository_there_is_no_branch(self):
        with tempfile.TemporaryDirectory() as plain:
            self.assertEqual(statusline.git_branch(Path(plain)), "")

    def test_a_git_failure_is_never_fatal(self):
        with mock.patch.object(
            statusline.subprocess, "run", side_effect=OSError("no git")
        ):
            self.assertEqual(statusline.git_output(self.repo, "status"), "")
            self.assertEqual(statusline.git_branch(self.repo), "")
            self.assertEqual(statusline.worktree_diff(self.repo), "")

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
        # 2100-01-01 00:00 UTC: always in the future, rendered in UTC.
        "prompt_cache": {"caching_observed": True, "warm": True, "expires_at": 4_102_444_800},
        "cost": {"total_lines_added": 156, "total_lines_removed": 23},
    })
    CONTEXT = "ctx 31% · 62k/200k · cache →00:00 · +156/−23"

    def _render(self, payload: str, env: dict[str, str], custom: str = "") -> str:
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(usage.time, "localtime", time.gmtime),
            mock.patch.object(statusline, "git_branch", return_value="main"),
            mock.patch.object(statusline, "worktree_dirty", return_value=True),
            mock.patch.object(statusline, "worktree_diff", return_value="+42/−7"),
            mock.patch.object(usage, "claude_usage", return_value="88/59 · 2h29m/4d15h"),
            mock.patch.object(usage, "codex_usage", return_value="91/17 · 2h11m/4d9h"),
            mock.patch.object(usage, "custom_usage", return_value=custom),
        ):
            return statusline.render(payload, now="09:41")

    def test_configured_command_segment_ends_the_line(self):
        self.assertEqual(
            self._render(self.PAYLOAD, {"NO_COLOR": "1"}, custom="$4.20"),
            f"➜ [09:41] project git:(main) ✗ +42/−7 │ {self.CONTEXT}"
            " │ 88/59 · 2h29m/4d15h │ 91/17 · 2h11m/4d9h │ $4.20",
        )

    def test_complete_line(self):
        self.assertEqual(
            self._render(self.PAYLOAD, {"NO_COLOR": "1"}),
            f"➜ [09:41] project git:(main) ✗ +42/−7 │ {self.CONTEXT}"
            " │ 88/59 · 2h29m/4d15h │ 91/17 · 2h11m/4d9h",
        )

    def test_render_disables_credential_refresh(self):
        """Repeated status-line callbacks must not rotate account tokens."""
        with mock.patch.object(usage, "usage_snapshot", return_value={"windows": []}) as snapshot:
            self._render_without_usage_stubs()
        self.assertIn(mock.call("claude", allow_refresh=False), snapshot.call_args_list)

    def _render_without_usage_stubs(self):
        with mock.patch.object(statusline, "git_branch", return_value="main"), \
                mock.patch.object(statusline, "worktree_dirty", return_value=False):
            return statusline.render(self.PAYLOAD, now="09:41")

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
        self.assertIn("88/59", line)

    def test_unusable_usage_degrades_to_placeholders(self):
        with (
            mock.patch.dict(os.environ, {"NO_COLOR": "1"}, clear=True),
            mock.patch.object(usage.time, "localtime", time.gmtime),
            mock.patch.object(statusline, "git_branch", return_value=""),
            mock.patch.object(usage, "claude_usage", side_effect=RuntimeError("offline")),
            mock.patch.object(usage, "codex_usage", side_effect=RuntimeError("offline")),
            mock.patch.object(usage, "custom_usage", return_value=""),
        ):
            line = statusline.render(self.PAYLOAD, now="09:41")
        self.assertEqual(
            line,
            f"➜ [09:41] project │ {self.CONTEXT}"
            f" │ {usage.PLACEHOLDER_USAGE} │ {usage.PLACEHOLDER_USAGE}",
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
        self.assertIn("88/66 · ", line)

    def test_unparseable_payload_still_renders_a_line(self):
        with (
            mock.patch.dict(os.environ, {"NO_COLOR": "1"}, clear=True),
            mock.patch.object(statusline, "git_branch", return_value=""),
            mock.patch.object(usage, "claude_usage", return_value="a"),
            mock.patch.object(usage, "codex_usage", return_value="b"),
        ):
            self.assertIn(" │ a │ b", statusline.render("not json", now="09:41"))


if __name__ == "__main__":
    unittest.main()
