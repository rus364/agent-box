"""Opt-in real MCP check of the managed host browser container; no models or external sites."""
import json
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent_box import browser  # noqa: E402


class Page(BaseHTTPRequestHandler):
    def do_GET(self):
        body = (b'<h1>before</h1><button onclick="document.querySelector(\'h1\').textContent='
                b"'after'\">Change</button>")
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class Client:
    def __init__(self, command, workspace, stderr):
        self.process = subprocess.Popen(command, cwd=workspace, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=stderr)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        self.buffer, self.sequence = b'', 0

    def send(self, message):
        self.process.stdin.write(json.dumps({'jsonrpc': '2.0', **message}).encode() + b'\n')
        self.process.stdin.flush()

    def request(self, method, params):
        self.sequence += 1
        self.send({'id': self.sequence, 'method': method, 'params': params})
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if b'\n' not in self.buffer:
                if not self.selector.select(max(0, deadline - time.monotonic())):
                    break
                chunk = self.process.stdout.read1(65536)
                if not chunk:
                    raise RuntimeError(f'MCP closed during {method}')
                self.buffer += chunk
                continue
            line, self.buffer = self.buffer.split(b'\n', 1)
            message = json.loads(line)
            if message.get('id') == self.sequence:
                if message.get('error'):
                    raise RuntimeError(str(message['error']))
                result = message['result']
                if result.get('isError'):
                    raise RuntimeError(text(result))
                return result
        raise TimeoutError(f'MCP timed out during {method}')

    def tool(self, name, **arguments):
        return self.request('tools/call', {'name': name, 'arguments': arguments})

    def close(self):
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.selector.close()
        self.process.stdin.close()
        self.process.stdout.close()


def text(result):
    return '\n'.join(block.get('text', '') for block in result.get('content', []))


def main():
    with tempfile.TemporaryDirectory(prefix='agent-box-browser-smoke-') as tmp, \
            tempfile.TemporaryFile() as stderr:
        root = Path(tmp)
        workspace, session = root / 'project', root / 'session'
        workspace.mkdir()
        output = session / 'output'
        output.mkdir(parents=True)
        server = HTTPServer(('127.0.0.1', 0), Page)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = None
        try:
            browser.STATE_ROOT = root / "runtime"
            command = browser.prepare(session, workspace, workspace, workspace="smoke-test")
            client = Client(command, workspace, stderr)
            client.request('initialize', {'protocolVersion': '2024-11-05', 'capabilities': {},
                                         'clientInfo': {'name': 'agent-box-smoke', 'version': '1'}})
            client.send({'method': 'notifications/initialized'})
            tools = {tool['name']: tool for tool in client.request('tools/list', {})['tools']}
            client.tool('browser_navigate', url=f'http://127.0.0.1:{server.server_port}/')
            # A selector avoids coupling this check to generated snapshot reference IDs.
            click = tools['browser_click']['inputSchema']['properties']
            if 'target' in click:
                client.tool('browser_click', target='button')
            else:
                raise RuntimeError('This smoke needs browser_click selector support')
            result = client.tool('browser_snapshot')
            snapshot = text(result)
            if 'heading "after"' not in snapshot:
                snapshots = list(output.glob('*.yml'))
                if snapshots:
                    snapshot += max(snapshots, key=lambda p: p.stat().st_mtime_ns).read_text()
            assert 'heading "after"' in snapshot, snapshot
            arguments = {'type': 'png'}
            if 'scale' in tools['browser_take_screenshot']['inputSchema']['properties']:
                arguments['scale'] = 'css'
            client.tool('browser_take_screenshot', **arguments)
            unnamed = list(output.glob('*.png'))
            assert unnamed and all(p.stat().st_size > 0 for p in unnamed)
            assert not list(workspace.glob('*.png')), 'Unnamed screenshot entered the project'
            client.tool('browser_take_screenshot', filename='named.png', **arguments)
            assert (workspace / 'named.png').stat().st_size > 0
            assert not (output / 'named.png').exists(), 'Explicit filename semantics changed'
            print('Browser MCP passed: local navigation, click, snapshot; unnamed output is '
                  'session-scoped and explicit relative filename is workspace-relative')
        except Exception:
            stderr.seek(0)
            print(stderr.read()[-4096:].decode(errors='replace'), file=sys.stderr)
            raise
        finally:
            if client:
                client.close()
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()


if __name__ == '__main__':
    main()
