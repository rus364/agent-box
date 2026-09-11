"""Host sessions must retain each launch's environment without owning the host."""
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from unittest import mock

from agent_box import runtime, sessions
from agent_box.config import Config
from agent_box.workspace import Workspace
from test_runtime import IsolatedRuntimeTest


class HostLaunchDecisionTests(IsolatedRuntimeTest):
    def test_current_codex_management_commands_preserve_direct_dispatch(self):
        """Management commands verified from local CLI help must not create sessions."""
        cases = [[name] for name in ('agents', 'plugin', 'remote-control', 'update', 'doctor',
                                    'queue', 'archive', 'delete', 'migrate-rollouts', 'unarchive', 'exec-server')]
        cases += [['--local-provider', 'ollama', 'doctor'], ['--remote', 'unix://example', 'doctor'],
                  ['--remote-auth-token-env', 'TEST_NAME', 'doctor'], ['resume', '--help']]
        with mock.patch.dict(os.environ, {'PATH': os.defpath}, clear=True), \
             mock.patch.object(runtime, 'resolve_workspace', return_value=Workspace(Path('/tmp'), Path('/tmp'))), \
             mock.patch.object(runtime, 'launch_config', return_value=Config()), \
             mock.patch.object(runtime.shutil, 'which', return_value='/bin/codex'), \
             mock.patch.object(runtime, 'tmux_enabled', return_value=True), \
             mock.patch.object(runtime, 'host_tmux_launch') as wrapped, \
             mock.patch.object(runtime.subprocess, 'call', return_value=0) as direct:
            for args in cases:
                with self.subTest(args=args):
                    runtime.host_main('codex', args)
                    self.assertEqual(wrapped.call_count, 0)
                    self.assertEqual(direct.call_args.args[0], ['/bin/codex', *args])

    def test_host_selection_errors_name_the_host(self):
        """Host attach must not send the user looking for a workspace container."""
        from agent_box.errors import AgentBoxError
        with mock.patch.object(runtime.shutil, 'which', return_value='/usr/bin/tmux'), \
             mock.patch.object(runtime.sys, 'stdin', mock.Mock(isatty=lambda: True)), \
             mock.patch.object(runtime.sys, 'stdout', mock.Mock(isatty=lambda: True)), \
             mock.patch.object(runtime, 'resolved_workspace', return_value=Workspace(Path('/tmp'), Path('/tmp'))):
            for names, args in (([], []), (['one'], ['missing']), (['one', 'two'], [])):
                with mock.patch.object(runtime, 'tmux_sessions', return_value=names), \
                     self.assertRaisesRegex(AgentBoxError, 'host'):
                    runtime.host_session_main(args)

    def test_batch_and_management_stay_direct_with_a_terminal(self):
        """A TTY alone does not make print/exec or CLI management interactive."""
        for agent, argv in (
            ('claude', ['-p', 'hello']), ('claude', ['--print=hello']),
            ('claude', ['--version']), ('claude', ['plugin', 'list']),
            ('claude', ['import', 'codex', '--dry-run']), ('claude', ['import', 'codex', '--yes']),
            ('claude', ['--model', 'opus', 'import', 'codex', '--dry-run']),
            ('claude', ['--effort', 'low', 'import', 'codex', '--dry-run']),
            ('claude', ['--effort', 'low', 'import', 'codex', '--yes=digest']),
            ('codex', ['exec', 'hello']), ('codex', ['--model', 'foo', 'exec', 'hello']),
            ('codex', ['mcp', 'list']), ('codex', ['--help']),
        ):
            with self.subTest(agent=agent, argv=argv):
                self.assertFalse(runtime.host_interactive_invocation(agent, argv))
        for agent, argv in (('claude', []), ('codex', []), ('codex', ['--model', 'exec']),
                            ('codex', ['resume']), ('claude', ['--', '-p'])):
            self.assertTrue(runtime.host_interactive_invocation(agent, argv))

    def test_missing_tmux_preserves_arguments_and_native_permissions(self):
        """An optional dependency must not prevent or change the direct launch."""
        with mock.patch.object(runtime, 'resolve_workspace', return_value=Workspace(Path('/tmp'), Path('/tmp'))), \
             mock.patch.object(runtime, 'launch_config', return_value=Config()), \
             mock.patch.object(runtime.shutil, 'which', side_effect=lambda name: '/bin/codex' if name == 'codex' else None), \
             mock.patch.object(runtime, 'tmux_enabled', return_value=True), \
             mock.patch.object(runtime.subprocess, 'call', return_value=7) as call, \
             mock.patch.object(runtime, 'warn') as warn:
            self.assertEqual(runtime.host_main('codex', ['hello']), 7)
        self.assertEqual(call.call_args.args[0], ['/bin/codex', 'hello'])
        self.assertIn('starting directly', warn.call_args.args[0])

    def test_tmux_error_never_retries_the_agent_directly(self):
        """The tmux client may fail after starting the agent; a retry duplicates work."""
        with mock.patch.object(runtime, 'resolve_workspace', return_value=Workspace(Path('/tmp'), Path('/tmp'))), \
             mock.patch.object(runtime, 'launch_config', return_value=Config()), \
             mock.patch.object(runtime.shutil, 'which', return_value='/bin/tmux'), \
             mock.patch.object(runtime, 'tmux_enabled', return_value=True), \
             mock.patch.object(runtime, 'host_tmux_launch', return_value=1), \
             mock.patch.object(runtime.subprocess, 'call') as direct:
            self.assertEqual(runtime.host_main('claude', []), 1)
        direct.assert_not_called()

    def test_launch_environment_is_private_and_absent_from_argv(self):
        """Secrets must travel via a consumed private file, not tmux arguments."""
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(runtime, 'HOST_SESSIONS_ROOT', Path(tmp)), \
             mock.patch.object(runtime, 'tmux_sessions', return_value=[]), \
             mock.patch.object(runtime, 'tmux_session_exists', return_value=False):
            def launch(argv, **kwargs):
                payload = next(Path(tmp).rglob('*.env.json'))
                self.assertEqual(payload.stat().st_mode & 0o777, 0o600)
                self.assertEqual(json.loads(payload.read_text())['TOKEN'], 'private test value')
                self.assertEqual(os.fsencode(json.loads(payload.read_text())['BYTE_VALUE']), b'\xff')
                self.assertNotIn('private test value', str(argv))
                self.assertNotIn('TOKEN', kwargs['env'])
                return 1
            with mock.patch.object(runtime.subprocess, 'call', side_effect=launch):
                runtime.host_tmux_launch(Workspace(Path(tmp), Path(tmp)), 'codex',
                                         ['/bin/false'], {'TOKEN': 'private test value',
                                                         'BYTE_VALUE': os.fsdecode(b'\xff')}, '/usr/bin/tmux')
            self.assertFalse(list(Path(tmp).rglob('*.env.json')))

    def test_host_recorder_uses_shell_from_the_callers_path(self):
        """The sanitized server PATH must not hide an otherwise installed shell."""
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(runtime, 'HOST_SESSIONS_ROOT', Path(tmp)), \
             mock.patch.object(runtime.shutil, 'which', return_value='/custom/bin/bash'), \
             mock.patch.object(runtime, 'tmux_sessions', return_value=[]), \
             mock.patch.object(runtime, 'tmux_session_exists', return_value=False):
            def launch(argv, **kwargs):
                self.assertIn('/custom/bin/bash', argv)
                return 1
            with mock.patch.object(runtime.subprocess, 'call', side_effect=launch):
                runtime.host_tmux_launch(Workspace(Path(tmp), Path(tmp)), 'codex',
                                         ['/bin/false'], {'PATH': '/custom/bin'}, '/usr/bin/tmux')

    def test_missing_host_recorder_shell_is_actionable_before_start(self):
        """The optional wrapper must explain its dependency without starting work."""
        from agent_box.errors import AgentBoxError
        with mock.patch.object(runtime.shutil, 'which', return_value=None), \
             mock.patch.object(runtime.subprocess, 'call') as launch, \
             self.assertRaisesRegex(AgentBoxError, 'bash on PATH.*AGENT_BOX_TMUX=0'):
            runtime.host_tmux_launch(Workspace(Path('/tmp'), Path('/tmp')), 'codex',
                                     ['/bin/false'], {}, '/usr/bin/tmux')
        self.assertEqual(launch.call_count, 0)


@unittest.skipUnless(shutil.which('tmux'), 'native tmux is not installed')
class NativeHostSessionsTests(unittest.TestCase):
    """Real PTYs/tmux; only temporary state, synthetic agents and private servers."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ab-native-host-')
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'
        self.projects = [self.root / 'one', self.root / 'two']
        for path in self.projects:
            path.mkdir()
        self.clients = []
        self.patch = mock.patch.object(runtime, 'HOST_SESSIONS_ROOT', self.state)
        self.patch.start()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for project in self.projects:
            subprocess.run(sessions.tmux_argv('kill-server', host_root=self.session_root(project)),
                           capture_output=True, env=sessions.host_tmux_env())
        for pid, fd in self.clients:
            try:
                os.kill(pid, signal.SIGTERM)
                os.waitpid(pid, 0)
            except (ProcessLookupError, ChildProcessError):
                pass
            os.close(fd)
        self.patch.stop()
        self.tmp.cleanup()

    def session_root(self, project):
        return self.state / runtime.workspace_identity(Workspace(project, project))

    def spawn(self, project, *, output=None, environment=None, attach=False):
        pid, fd = pty.fork()
        if pid == 0:
            try:
                os.environ.pop('TMUX', None)
                os.environ['TERM'] = 'xterm-256color'
                os.chdir(project)
                if attach:
                    runtime.resolve_workspace = lambda: Workspace(project, project)
                    rc = runtime.host_session_main([])
                else:
                    script = ('import json,os,sys; from pathlib import Path; '
                              'Path(sys.argv[1]).write_text(json.dumps(dict(os.environ))); '
                              'print("ready",flush=True); sys.exit(int(input()))')
                    rc = runtime.host_tmux_launch(
                        Workspace(project, project), 'codex',
                        [sys.executable, '-c', script, str(output)], environment, shutil.which('tmux'))
                os._exit(rc)
            except BaseException:
                import traceback
                traceback.print_exc()
                os._exit(99)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 100, 0, 0))
        self.clients.append((pid, fd))
        return pid, fd

    def until(self, predicate):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if predicate():
                return
            ready, _, _ = select.select([fd for _, fd in self.clients], [], [], 0.02)
            for fd in ready:
                try:
                    os.read(fd, 65536)
                except OSError:
                    pass
        self.fail('native tmux did not reach the expected state within 10 seconds')

    def wait_client(self, client):
        pid, fd = client
        result = []
        def exited():
            found, status = os.waitpid(pid, os.WNOHANG)
            if found:
                result.append(os.waitstatus_to_exitcode(status))
            return bool(found)
        self.until(exited)
        self.clients.remove(client)
        os.close(fd)
        return result[0]

    def test_detach_environment_reattach_exit_and_project_stop(self):
        """Reuse a live server without stale secrets, then stop only its project."""
        project, other = self.projects
        first_output = self.root / 'first.json'
        first = self.spawn(project, output=first_output,
                           environment={'PATH': os.defpath, 'TERM': 'xterm', 'OLD_ONLY': 'old', 'VALUE': 'first'})
        self.until(first_output.exists)
        os.write(first[1], b'\x1cd')
        self.assertEqual(self.wait_client(first), 0)
        self.assertEqual(len(sessions.tmux_sessions(None, host_root=self.session_root(project))), 1)

        second_output = self.root / 'second.json'
        second = self.spawn(project, output=second_output,
                            environment={'PATH': os.defpath, 'TERM': 'xterm', 'VALUE': 'second secret',
                                         os.fsdecode(b'KEY-\xff'): os.fsdecode(b'\xfe')})
        self.until(second_output.exists)
        actual = json.loads(second_output.read_text())
        self.assertEqual(actual['VALUE'], 'second secret')
        self.assertEqual(os.fsencode(actual[os.fsdecode(b'KEY-\xff')]), b'\xfe')
        self.assertNotIn('OLD_ONLY', actual)
        self.assertIn('TMUX_PANE', actual)
        self.assertFalse(list(self.session_root(project).glob('*.env.json')))
        os.write(second[1], b'7\n')
        self.assertEqual(self.wait_client(second), 7)

        attached = self.spawn(project, attach=True)
        self.until(lambda: subprocess.run(
            sessions.tmux_argv('list-clients', '-F', '#{client_pid}', host_root=self.session_root(project)),
            capture_output=True, text=True, env=sessions.host_tmux_env()).stdout.strip())
        os.write(attached[1], b'9\n')
        self.assertEqual(self.wait_client(attached), 9)

        live = []
        for index, location in enumerate(self.projects):
            output = self.root / f'live-{index}.json'
            client = self.spawn(location, output=output, environment={'PATH': os.defpath, 'TERM': 'xterm'})
            self.until(output.exists)
            live.append(client)
        with mock.patch.object(runtime, 'resolved_workspace', return_value=Workspace(project, project)):
            self.assertEqual(runtime.host_session_main([], stop=True), 0)
        self.assertIn(self.wait_client(live[0]), (0, 1))
        self.assertEqual(len(sessions.tmux_sessions(None, host_root=self.session_root(other))), 1)
        os.write(live[1][1], b'0\n')
        self.assertEqual(self.wait_client(live[1]), 0)

    def test_closed_terminal_preserves_the_agent_for_a_new_client(self):
        """Closing the real PTY must not kill the detached pane process."""
        project = self.projects[0]
        output = self.root / 'closed.json'
        client = self.spawn(project, output=output, environment={'PATH': os.defpath, 'TERM': 'xterm'})
        self.until(output.exists)
        self.clients.remove(client)
        os.close(client[1])
        self.until(lambda: os.waitpid(client[0], os.WNOHANG)[0])
        self.assertEqual(len(sessions.tmux_sessions(None, host_root=self.session_root(project))), 1)
        attached = self.spawn(project, attach=True)
        self.until(lambda: subprocess.run(
            sessions.tmux_argv('list-clients', '-F', '#{client_pid}', host_root=self.session_root(project)),
            capture_output=True, text=True, env=sessions.host_tmux_env()).stdout.strip())
        os.write(attached[1], b'0\n')
        self.assertEqual(self.wait_client(attached), 0)

    def test_posix_byte_paths_work_for_project_and_session_state(self):
        """Normal filesystem bytes must survive identity hashing and tmux argv."""
        project = self.root / os.fsdecode(b'project-\xff')
        project.mkdir()
        self.projects[0] = project
        self.state = self.root / os.fsdecode(b'state-\xfe')
        output = self.root / 'byte-path.json'
        with mock.patch.object(runtime, 'HOST_SESSIONS_ROOT', self.state):
            client = self.spawn(project, output=output, environment={'PATH': os.defpath, 'TERM': 'xterm'})
            self.until(output.exists)
            os.write(client[1], b'0\n')
            self.assertEqual(self.wait_client(client), 0)

    def test_recorder_shell_survives_an_empty_sanitized_path(self):
        """Use the caller's installed Bash even when the server PATH cannot find it."""
        project = self.projects[0]
        empty = self.root / 'empty-bin'
        empty.mkdir()
        output = self.root / 'shell-path.json'
        with mock.patch.object(runtime.os, 'defpath', str(empty)):
            client = self.spawn(project, output=output, environment={'TERM': 'xterm'})
            self.until(output.exists)
            os.write(client[1], b'0\n')
            self.assertEqual(self.wait_client(client), 0)

    def test_socket_discovery_survives_a_changed_tmux_tmpdir(self):
        """A later shell must find and stop the same project's existing server."""
        project = self.projects[0]
        original, changed = self.root / 'tmp-a', self.root / 'tmp-b'
        original.mkdir()
        changed.mkdir()
        output = self.root / 'tmpdir.json'
        try:
            with mock.patch.dict(os.environ, {'TMUX_TMPDIR': str(original)}):
                client = self.spawn(project, output=output, environment={'PATH': os.defpath, 'TERM': 'xterm'})
                self.until(output.exists)
                os.write(client[1], b'\x1cd')
                self.assertEqual(self.wait_client(client), 0)
            with mock.patch.dict(os.environ, {'TMUX_TMPDIR': str(changed)}):
                self.assertEqual(len(sessions.tmux_sessions(None, host_root=self.session_root(project))), 1)
                attached = self.spawn(project, attach=True)
                self.until(lambda: subprocess.run(
                    sessions.tmux_argv('list-clients', '-F', '#{client_pid}', host_root=self.session_root(project)),
                    capture_output=True, text=True, env=sessions.host_tmux_env()).stdout.strip())
                os.write(attached[1], b'\x1cd')
                self.assertEqual(self.wait_client(attached), 0)
                with mock.patch.object(runtime, 'resolved_workspace', return_value=Workspace(project, project)):
                    self.assertEqual(runtime.host_session_main([], stop=True), 0)
            self.assertEqual(sessions.tmux_sessions(None, host_root=self.session_root(project)), [])
        finally:
            with mock.patch.dict(os.environ, {'TMUX_TMPDIR': str(original)}):
                subprocess.run(sessions.tmux_argv('kill-server', host_root=self.session_root(project)),
                               capture_output=True, env=sessions.host_tmux_env())
