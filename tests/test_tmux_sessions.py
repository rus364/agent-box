import contextlib
import io
import os
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

from agent_box import runtime, sessions
from agent_box.errors import AgentBoxError
from agent_box.workspace import Workspace


class TmuxDecisionTests(unittest.TestCase):
    def _tty(self, value=True):
        return mock.patch.multiple(
            runtime.sys,
            stdin=mock.Mock(isatty=mock.Mock(return_value=value)),
            stdout=mock.Mock(isatty=mock.Mock(return_value=value)),
        )

    def test_interactive_agent_session_is_detachable(self):
        with self._tty():
            for agent in ("claude", "codex", "shell"):
                self.assertTrue(runtime.tmux_enabled(agent), agent)

    def test_diagnostic_command_is_never_wrapped(self):
        with self._tty():
            self.assertFalse(runtime.tmux_enabled("doctor"))

    def test_piped_run_is_never_wrapped(self):
        """`claude -p ...` and CI must keep writing straight to the caller."""
        with self._tty(False):
            self.assertFalse(runtime.tmux_enabled("claude"))

    def test_no_tmux_disables_wrapping(self):
        with self._tty():
            self.assertFalse(runtime.tmux_enabled("claude", allowed=False))


class TmuxSessionCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = runtime.WorkspaceRuntimeState(
            root=Path(self.tmp.name), git=Path(self.tmp.name) / "git", gh=Path(self.tmp.name) / "gh",
        )

    def test_session_is_per_launch_not_per_agent(self):
        """Two `agent-box claude` runs stay two independent Claude sessions."""
        session = runtime.tmux_session("claude", self.state)
        self.assertEqual(session.name, f"claude-{os.getpid()}")
        self.assertEqual(session.status_path.parent, self.state.root / "sessions")
        self.assertTrue(session.status_path.parent.is_dir())

    def test_wrapped_command_survives_the_client_and_keeps_arguments_verbatim(self):
        session = runtime.tmux_session("claude", self.state)
        argv = session.wrap(["claude", "--model", "opus", "a prompt with spaces"])
        self.assertEqual(argv[0], "tmux")
        self.assertIn("-L", argv)
        self.assertIn(sessions.TMUX_SOCKET, argv)
        self.assertIn(str(sessions.TMUX_CONFIG_PATH), argv)
        self.assertIn("new-session", argv)
        self.assertEqual(argv[argv.index("-s") + 1], session.name)
        # tmux passes the trailing argv through without re-splitting it, so
        # the agent's own arguments must remain individual argv entries.
        self.assertEqual(argv[-4:], ["claude", "--model", "opus", "a prompt with spaces"])
        self.assertIn(str(session.status_path), argv)

    def test_recorded_status_is_the_agents_own_exit_code(self):
        session = runtime.tmux_session("claude", self.state)
        session.status_path.write_text("7\n")
        self.assertEqual(session.finish("box", 0), 7)
        # Reading consumes it: nothing is left to mislead a later launch.
        self.assertFalse(session.status_path.exists())

    def test_failed_session_says_its_output_went_with_the_terminal(self):
        """A pane dies with its session, so a failure to read is unrecoverable.

        Reporting only the exit status is how a dead external Codex launch
        looked like a bare '[exited]' with the reason already gone.
        """
        session = runtime.tmux_session("codex", self.state)
        session.status_path.write_text("1\n")
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(session.finish("box", 0), 1)
        self.assertIn("--no-tmux", stderr.getvalue())

    def test_a_clean_session_end_is_reported_only_by_its_status(self):
        session = runtime.tmux_session("claude", self.state)
        session.status_path.write_text("0\n")
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(session.finish("box", 0), 0)
        self.assertEqual(stderr.getvalue(), "")

    def test_detached_session_reports_zero_and_says_how_to_return(self):
        session = runtime.tmux_session("claude", self.state)
        with mock.patch.object(runtime, "tmux_session_exists", return_value=True):
            self.assertEqual(session.finish("box", 0), 0)

    def test_image_without_tmux_is_named_as_the_problem(self):
        session = runtime.tmux_session("claude", self.state)
        with mock.patch.object(runtime, "warn") as warn:
            self.assertEqual(session.finish("box", 127), 127)
        self.assertIn("build", warn.call_args[0][0])


class TmuxSessionListingTests(unittest.TestCase):
    def _run(self, returncode=0, stdout="", stderr=""):
        return mock.patch.object(
            sessions.subprocess, "run",
            return_value=mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr),
        )

    def test_sessions_are_listed_by_name(self):
        """Names only: tmux still calls a dead terminal's ghost client attached."""
        with self._run(stdout="claude-11\ncodex-22\n"):
            self.assertEqual(sessions.tmux_sessions("box"), ["claude-11", "codex-22"])

    def test_box_without_a_server_has_no_sessions(self):
        with self._run(returncode=1, stderr="no server running on /tmp/tmux-1000/agent-box"):
            self.assertEqual(sessions.tmux_sessions("box"), [])

    def test_missing_tmux_binary_asks_for_a_rebuild(self):
        with self._run(returncode=127, stderr='exec: "tmux": executable file not found in $PATH'):
            with self.assertRaises(AgentBoxError) as caught:
                sessions.tmux_sessions("box")
        self.assertIn("build", str(caught.exception))

    def test_operational_failure_is_reported_not_swallowed(self):
        with self._run(returncode=125, stderr="container is not running"):
            with self.assertRaises(AgentBoxError):
                sessions.tmux_sessions("box")


class ExistingSessionNoticeTests(unittest.TestCase):
    def test_session_left_by_a_closed_terminal_is_announced(self):
        """The ghost client makes such a session look attached, so the notice
        must not filter on attachment or it misses its only real case."""
        with mock.patch.object(runtime, "inspect_workspace_container", return_value={"State": {"Status": "running"}}), \
             mock.patch.object(runtime, "tmux_sessions", return_value=["claude-9"]), \
             mock.patch.object(runtime, "print") as printed:
            runtime.announce_existing_sessions("box")
        self.assertIn("claude-9", printed.call_args[0][0])

    def test_first_launch_in_a_workspace_says_nothing(self):
        with mock.patch.object(runtime, "inspect_workspace_container", return_value=None), \
             mock.patch.object(runtime, "print") as printed:
            runtime.announce_existing_sessions("box")
        printed.assert_not_called()

    def test_a_notice_never_blocks_a_launch(self):
        with mock.patch.object(runtime, "inspect_workspace_container", side_effect=AgentBoxError("podman broke")):
            runtime.announce_existing_sessions("box")


class SessionChoiceTests(unittest.TestCase):
    def test_single_session_needs_no_name(self):
        self.assertEqual(runtime.choose_session(["claude-1"], None), "claude-1")

    def test_named_session_is_honoured(self):
        self.assertEqual(
            runtime.choose_session(["claude-1", "codex-2"], "codex-2"), "codex-2")

    def test_unknown_name_is_refused(self):
        with self.assertRaises(AgentBoxError):
            runtime.choose_session(["claude-1"], "codex-2")

    def test_ambiguous_box_lists_what_it_holds(self):
        with self.assertRaises(AgentBoxError) as caught:
            runtime.choose_session(["claude-1", "codex-2"], None)
        message = str(caught.exception)
        self.assertIn("claude-1", message)
        self.assertIn("codex-2", message)

    def test_empty_box_says_there_is_nothing_to_attach_to(self):
        with self.assertRaises(AgentBoxError):
            runtime.choose_session([], None)


class AttachAndStopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name) / "project"
        root.mkdir()
        self.workspace = Workspace(root, root)
        self.name = runtime.workspace_container_name(self.workspace)
        for patch in (
            mock.patch.object(runtime.host_podman, "require"),
            mock.patch.object(runtime, "resolve_workspace", return_value=self.workspace),
            mock.patch.object(runtime, "workspace_transition_lock", return_value=nullcontext()),
            mock.patch.object(runtime, "STATE_ROOT", Path(self.tmp.name) / "state"),
            # stop also stops the workspace's k3s sidecar; keep real Podman out of reach.
            mock.patch.multiple(runtime.k3s, stop=mock.Mock(return_value=False), remove=mock.Mock()),
            mock.patch.object(runtime, "effective_config", return_value=runtime.Config()),
            mock.patch.multiple(
                runtime.sys,
                stdin=mock.Mock(isatty=mock.Mock(return_value=True)),
                stdout=mock.Mock(isatty=mock.Mock(return_value=True)),
            ),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def test_attach_evicts_the_ghost_client_of_a_dead_terminal(self):
        """Refresh host link policy before a resumed CLI can request a browser over SSH."""
        events = []
        with mock.patch.object(runtime, "inspect_workspace_container", return_value={"State": {"Status": "running"}}), \
             mock.patch.object(runtime, "tmux_sessions", return_value=["claude-9"]), \
             mock.patch.object(runtime, "cleanup_workspace_container_if_idle"), \
             mock.patch.object(runtime.links, "reattach", side_effect=lambda *a: events.append("refresh")) as refresh, \
             mock.patch.object(runtime.subprocess, "Popen") as popen:
            popen.return_value = mock.Mock(wait=mock.Mock(return_value=0))
            popen.side_effect = lambda *a, **k: (events.append("attach"), popen.return_value)[1]
            self.assertEqual(runtime.attach_main([]), 0)
        self.assertEqual(events, ["refresh", "attach"])
        refresh.assert_called_once_with(runtime.workspace_runtime_state(self.workspace).root.name, self.name)
        argv = popen.call_args[0][0]
        self.assertEqual(argv[:2], ["podman", "exec"])
        self.assertIn("--tty", argv)
        self.assertIn("attach-session", argv)
        # Without -d the client left behind by the closed terminal keeps the
        # session to itself: its pty is still open, so it never sees EOF.
        self.assertIn("-d", argv[argv.index("attach-session"):])
        self.assertEqual(argv[-1], "claude-9")

    def test_attach_refuses_when_no_box_is_running(self):
        with mock.patch.object(runtime, "inspect_workspace_container", return_value=None):
            with self.assertRaises(AgentBoxError):
                runtime.attach_main([])

    def test_attach_needs_a_terminal(self):
        with mock.patch.object(runtime.sys, "stdout", mock.Mock(isatty=mock.Mock(return_value=False))):
            with self.assertRaises(AgentBoxError):
                runtime.attach_main([])

    def test_stop_removes_the_box_and_its_session_records(self):
        sessions = runtime.workspace_runtime_state(self.workspace).root / "sessions"
        sessions.mkdir(parents=True)
        (sessions / "claude-9.status").write_text("0")
        with mock.patch.object(runtime, "inspect_workspace_container", return_value={"State": {"Status": "running"}}), \
             mock.patch.object(runtime, "tmux_sessions", return_value=["claude-9"]), \
             mock.patch.object(runtime, "remove_workspace_container_then_confirm_absent") as remove:
            self.assertEqual(runtime.stop_main([]), 0)
        remove.assert_called_once_with(self.name)
        self.assertFalse(sessions.exists())

    def test_stop_without_a_box_is_not_an_error(self):
        with mock.patch.object(runtime, "inspect_workspace_container", return_value=None), \
             mock.patch.object(runtime, "remove_workspace_container_then_confirm_absent") as remove:
            self.assertEqual(runtime.stop_main([]), 0)
        remove.assert_not_called()


class TmuxConfigTests(unittest.TestCase):
    def test_shipped_configuration_keeps_a_detached_session_alive(self):
        config = (Path(__file__).parents[1] / "agent_box" / "tmux.conf").read_text()
        self.assertIn("destroy-unattached off", config)
        # The agent draws a full-screen TUI; tmux must not take a row for
        # itself or sit on the escape sequences the agent reads.
        self.assertIn("status off", config)
        self.assertIn("escape-time 0", config)


if __name__ == "__main__":
    unittest.main()
