"""Remote startup must never cost a working local CLI or another live session."""
import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_box import remote_control as rc, runtime
from agent_box.config import Config
from agent_box.paths import LOCAL_LAUNCHER


class RemoteControlTests(unittest.TestCase):
    def test_nested_launch_snapshot_preserves_remote_choices_without_other_host_integrations(self):
        """A new nested launcher must see its captured opt-in settings."""
        config = Config()
        config.claude.remote_control = True
        config.codex.remote_control = True
        config.claude.telegram_channel = True
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            runtime.sync_tracker_runtime(config, directory, policy=True)
            data = json.loads((directory / 'config.json').read_text())
            loaded = Config.from_dict(data)
            self.assertTrue(loaded.claude.remote_control)
            self.assertTrue(loaded.codex.remote_control)
            self.assertEqual(data['claude'], {'remote_control': True})

    def test_off_preserves_exact_native_command_for_both_launch_modes(self):
        """Disabled RC must not impose server limitations on profiles or arguments."""
        config = Config.from_dict(Config().to_dict())
        for provider in ('claude', 'codex'):
            self.assertFalse(getattr(config, provider).remote_control)
            command = [provider, '-p', 'custom', '--', 'prompt']
            for container in (False, True):
                self.assertEqual(runtime.remote_control_command(command, provider, config, container=container), command)
        config.codex.remote_control = True
        self.assertTrue(Config.from_dict(config.to_dict()).codex.remote_control)
        self.assertFalse(Config.from_dict(config.to_dict()).claude.remote_control)

    def test_managed_and_redirected_calls_never_enable_rc(self):
        """Delegated providers retain their own route, even inside an RC session."""
        with mock.patch.object(runtime.sys.stdin, 'isatty', return_value=True), \
             mock.patch.object(runtime.sys.stdout, 'isatty', return_value=True):
            for provider, args in [('claude', ['-p', 'prompt']), ('claude', ['--bg']),
                                   ('claude', ['auth', 'status']), ('codex', ['exec', 'prompt']),
                                   ('codex', ['remote-control', 'start']), ('codex', ['--help'])]:
                self.assertFalse(runtime.remote_control_invocation(provider, args))
        with mock.patch.object(runtime.sys.stdin, 'isatty', return_value=False):
            self.assertFalse(runtime.remote_control_invocation('codex', []))

    def test_claude_prompt_integrations_and_separator_keep_their_arguments(self):
        """Optional RC names must not swallow the prompt or variadic MCP values."""
        command = ['claude', 'prompt', '--channels', 'plugin:telegram', '--mcp-config', 'one', 'two', '--', 'literal']
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(rc.claude_command(command), [*command[:-2], '--remote-control', '--', 'literal'])
            named = ['claude', '--remote-control', 'named', 'prompt']
            self.assertEqual(rc.claude_command(named), named)

    def test_claude_settings_resume_and_custom_auth_preserve_native_behavior(self):
        """Do not rewrite settings or take control away from an existing conversation."""
        with mock.patch.dict(os.environ, {}, clear=True):
            for args in (['--settings', '{"env":{"TOKEN":"private"}}'], ['--resume', 'id'], ['--continue']):
                with self.assertRaises(rc.Unavailable):
                    rc.claude_command(['claude', *args])
        with mock.patch.dict(os.environ, {'ANTHROPIC_BASE_URL': 'https://custom.invalid'}, clear=True):
            with self.assertRaises(rc.Unavailable):
                rc.claude_command(['claude'])

    def test_claude_auth_capability_failures_keep_original_command(self):
        """A configured checkbox must not turn a usable local CLI into a login failure."""
        help_result = subprocess.CompletedProcess([], 0, b'--remote-control [name]')
        accounts = ({'loggedIn': False, 'authMethod': 'none'},
                    {'loggedIn': True, 'authMethod': 'api_key', 'apiProvider': 'firstParty'},
                    {'loggedIn': True, 'authMethod': 'claude.ai', 'apiProvider': 'firstParty'})
        for account in accounts:
            with self.subTest(account=account), mock.patch.dict(os.environ, {}, clear=True), \
                 mock.patch.object(rc.subprocess, 'run', side_effect=[help_result, subprocess.CompletedProcess([], 0, json.dumps(account).encode())]), \
                 mock.patch.object(rc, 'wait_for_client', return_value=23) as launch, \
                 contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(rc.main(['claude', 'claude', 'prompt']), 23)
                expected = ['claude', 'prompt']
                if account.get('authMethod') == 'claude.ai':
                    expected.append('--remote-control')
                launch.assert_called_once_with(expected)

    def test_server_receives_mcp_agent_and_model_overrides_with_explicit_cwd(self):
        """The TUI's narrow remote config transport must not drop integrations."""
        with tempfile.TemporaryDirectory() as temp:
            command = ['codex', '-c', 'mcp_servers.browser.command="playwright"',
                       '-c', 'agents.worker.model="worker-model"', '--model', 'chosen',
                       '-c', 'model="lower-priority"', '--enable', 'multi_agent',
                       '--dangerously-bypass-approvals-and-sandbox', '--cd', temp,
                       '--image', 'image.png', 'prompt', '--', '--literal']
            server, client, cwd = rc.codex_commands(command)
            self.assertEqual(cwd, Path(temp))
            self.assertIn('mcp_servers.browser.command="playwright"', server)
            self.assertIn('agents.worker.model="worker-model"', server)
            self.assertGreater(server.index('model="chosen"'), server.index('model="lower-priority"'))
            self.assertIn('approval_policy="never"', server)
            self.assertIn('features."multi_agent"=true', server)
            self.assertEqual(client[-4:], ['--cd', temp, '--', '--literal'])
            self.assertIn('image.png', client)

    def test_incompatible_codex_options_keep_original_launch(self):
        """Profiles and unsupported CLI features must remain available without RC."""
        for args in (['-p', 'provider'], ['--profile=work'], ['--remote', 'unix:///user'],
                     ['--oss'], ['--future-flag'], ['resume', 'id'], ['--worktree'], ['--add-dir', '/data']):
            with self.subTest(args=args), mock.patch.object(rc, 'wait_for_client', return_value=23) as run, \
                 contextlib.redirect_stderr(io.StringIO()) as output:
                command = ['codex', *args]
                self.assertEqual(rc.run_codex(command), 23)
                run.assert_called_once_with(command)
                self.assertIn('ordinary launch', output.getvalue())


FAKE_CODEX = r'''
import base64, hashlib, json, os, signal, socket, sys, time
from pathlib import Path
home = Path(os.environ['CODEX_HOME'])
args = sys.argv[1:]
def event(kind, **values):
    with (home / 'events').open('a') as stream:
        stream.write(json.dumps(dict(kind=kind, pid=os.getpid(), args=args, **values)) + '\n')
def response(message):
    method = message.get('method')
    if method == 'initialized': return None
    if method == 'initialize': result = {'userAgent':'fake'}
    elif method == 'config/read': result = {'config': {'model_provider':os.environ.get('RC_TEST_PROVIDER', 'openai')}}
    elif method == 'account/read': result = {'account': {'type':os.environ.get('RC_TEST_ACCOUNT', 'chatgpt')}}
    elif method == 'remoteControl/status/read': result = {'status':'connected'}
    elif method == 'remoteControl/pairing/start':
        if os.environ.get('RC_TEST_PAIR_FAIL'): return {'id':message['id'], 'error':{'message':'secret-not-for-output'}}
        result = {'manualPairingCode':'TEST-CODE'}
    else: return {'id':message['id'], 'error':{'message':'secret-not-for-output'}}
    return {'id':message['id'], 'result':result}
if 'app-server' in args:
    if '--stdio' in args:
        event('preflight', disabled=os.environ.get('CODEX_INTERNAL_APP_SERVER_REMOTE_CONTROL_DISABLED'))
        for line in sys.stdin:
            result = response(json.loads(line))
            if result is not None: print(json.dumps(result), flush=True)
    else:
        path = args[args.index('--listen') + 1].removeprefix('unix://')
        event('server', path=path)
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(path); server.listen()
            while True:
                connection, _ = server.accept()
                with connection, connection.makefile('rb') as stream:
                    request = b''
                    while not request.endswith(b'\r\n\r\n'): request += stream.read(1)
                    key = next(line.split(b':',1)[1].strip() for line in request.split(b'\r\n') if line.startswith(b'Sec-WebSocket-Key:'))
                    accept = base64.b64encode(hashlib.sha1(key+b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest())
                    connection.sendall(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: '+accept+b'\r\n\r\n')
                    while True:
                        header = stream.read(2)
                        if not header: break
                        size = header[1] & 127
                        if size in (126,127): size = int.from_bytes(stream.read(2 if size == 126 else 8),'big')
                        mask = stream.read(4)
                        data = bytes(value ^ mask[index%4] for index,value in enumerate(stream.read(size)))
                        result = response(json.loads(data))
                        if result is not None:
                            payload = json.dumps(result).encode()
                            length = bytes([len(payload)]) if len(payload)<126 else b'\x7e'+len(payload).to_bytes(2,'big')
                            connection.sendall(b'\x81'+length+payload)

else:
    event('client', remote='--remote' in args)
    if os.environ.get('RC_TEST_WAIT') == '1':
        while True: time.sleep(.1)
    sys.exit(23)
'''


class CodexLifecycleTests(unittest.TestCase):
    """Exercise real process/socket/lock lifetimes with a credential-free provider."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.fake = self.home / 'codex'
        self.fake.write_text('#!' + sys.executable + '\n' + FAKE_CODEX)
        self.fake.chmod(0o700)
        self.environment = dict(os.environ, CODEX_HOME=str(self.home),
                                AGENT_BOX_STATE_DIR=str(self.home / 'state'))
        for key in list(self.environment):
            if key.startswith('RC_TEST_') or key == 'OPENAI_BASE_URL':
                del self.environment[key]
        self.processes = []
        self.addCleanup(self.cleanup_processes)

    def cleanup_processes(self):
        for process in self.processes:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=6)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            process.stdout.close()
            process.stderr.close()
        for event in self.events():
            if event['kind'] in {'server', 'client'}:
                try:
                    command = Path(f"/proc/{event['pid']}/cmdline").read_bytes()
                    if os.fsencode(self.fake) in command.split(b'\0'):
                        os.kill(event['pid'], signal.SIGKILL)
                except (ProcessLookupError, FileNotFoundError):
                    pass

    def events(self):
        try:
            return [json.loads(line) for line in (self.home / 'events').read_text().splitlines()]
        except FileNotFoundError:
            return []

    def launch(self, *args, **env):
        process = subprocess.Popen([sys.executable, str(LOCAL_LAUNCHER), '_session', 'codex', str(self.fake), *args],
                                   env=dict(self.environment, **env), stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True)
        self.processes.append(process)
        return process

    def wait_event(self, kind):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            matches = [event for event in self.events() if event['kind'] == kind]
            if matches:
                return matches[-1]
            time.sleep(.02)
        self.fail(f'No {kind} event: {self.events()}')

    def assert_gone(self, pid):
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_exit_cleans_private_server_socket_and_releases_slot(self):
        """A normal terminal exit must not keep the workspace box alive."""
        process = self.launch('-c', 'mcp_servers.browser.command="fake-browser"', 'prompt')
        self.assertEqual(process.wait(timeout=10), 23)
        server = self.wait_event('server')
        self.assert_gone(server['pid'])
        self.assertFalse(Path(server['path']).parent.exists())
        self.assertIn('mcp_servers.browser.command="fake-browser"', server['args'])
        self.assertTrue(self.wait_event('client')['remote'])
        self.assertEqual(self.wait_event('preflight')['disabled'], '1')
        self.assertEqual(self.launch().wait(timeout=10), 23)
        self.assertEqual(len([e for e in self.events() if e['kind'] == 'server']), 2)

    def test_second_session_falls_back_and_does_not_stop_the_first(self):
        """One slot is shared by all launchers using the same Codex home."""
        first = self.launch(RC_TEST_WAIT='1')
        first_client = self.wait_event('client')
        first_server = self.wait_event('server')
        second = self.launch('-p', 'custom')
        self.assertEqual(second.wait(timeout=10), 23)
        third = self.launch('another prompt')
        self.assertEqual(third.wait(timeout=10), 23)
        self.assertIn(b'another agent-box', third.stderr.read())
        self.assertIsNone(first.poll())
        os.kill(first_client['pid'], 0)
        os.kill(first_server['pid'], 0)
        first.terminate()
        self.assertEqual(first.wait(timeout=10), 128 + signal.SIGTERM)
        self.assert_gone(first_client['pid'])
        self.assert_gone(first_server['pid'])

    def test_api_auth_and_custom_provider_never_start_remote_server(self):
        """Provider selection must not be silently replaced with OpenAI for RC."""
        for env in ({'RC_TEST_ACCOUNT':'apiKey'}, {'RC_TEST_PROVIDER':'deepseek'}):
            process = self.launch('prompt', **env)
            self.assertEqual(process.wait(timeout=10), 23)
            self.assertFalse(self.wait_event('client')['remote'])
        self.assertFalse(any(e['kind'] == 'server' for e in self.events()))

    def test_pairing_failure_cleans_server_and_launches_locally_without_private_errors(self):
        """Cloud enrollment failure must not expose logs or cost a local session."""
        process = self.launch('prompt', RC_TEST_PAIR_FAIL='1')
        self.assertEqual(process.wait(timeout=10), 23)
        server = self.wait_event('server')
        self.assert_gone(server['pid'])
        self.assertFalse(Path(server['path']).parent.exists())
        self.assertFalse(self.wait_event('client')['remote'])
        stderr = process.stderr.read()
        self.assertIn(b'ordinary launch', stderr)
        self.assertNotIn(b'secret-not-for-output', stderr)

    def test_server_death_ends_only_its_terminal_without_replaying_prompt(self):
        """Restarting the original command after a live server crash could duplicate work."""
        process = self.launch('do work', RC_TEST_WAIT='1')
        client = self.wait_event('client')
        server = self.wait_event('server')
        os.kill(server['pid'], signal.SIGKILL)
        self.assertEqual(process.wait(timeout=10), 1)
        self.assert_gone(client['pid'])
        self.assertEqual(len([e for e in self.events() if e['kind'] == 'client']), 1)
