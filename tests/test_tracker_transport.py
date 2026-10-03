"""Lost tracker replies must not invite a duplicate write or look like bad input."""
import contextlib
import http.client
import io
import json
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock, Mock, patch

from agent_box import workflow
from agent_box.tracker import cli
from agent_box.tracker.vikunja_api import APIError, VikunjaClient


class TransportTests(unittest.TestCase):
    def diagnostic(self, command, operation):
        output = io.StringIO()
        redirect = contextlib.redirect_stderr if command is cli else contextlib.redirect_stdout
        with patch.object(command, 'dispatch', side_effect=lambda _: operation()), redirect(output):
            self.assertEqual(command.main(['status'] if command is cli else ['inspect', '1']), 2)
        return json.loads(output.getvalue())

    def test_accepted_write_with_lost_reply_reports_unknown_without_repeating(self):
        """A timeout before headers or in the body can follow a committed POST."""
        for body_started in (False, True):
            with self.subTest(body_started=body_started):
                release = threading.Event()
                received = []

                class Handler(BaseHTTPRequestHandler):
                    def log_message(self, *args):
                        pass

                    def do_POST(self):
                        received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                        if body_started:
                            self.send_response(200)
                            self.send_header('Content-Length', '100')
                            self.end_headers()
                            self.wfile.write(b'{')
                            self.wfile.flush()
                        release.wait(2)

                server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
                thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
                thread.start()
                try:
                    client = VikunjaClient(f'http://127.0.0.1:{server.server_port}', 'fixture-secret', timeout=0.1)
                    result = self.diagnostic(cli, lambda: client.post('/tasks', {'title': 'fixture'}))
                    self.assertEqual(received, [{'title': 'fixture'}])
                    self.assertEqual((result['category'], result['retryable']), ('unknown-outcome', False))
                    self.assertIn('reread', result['error'])
                finally:
                    release.set()
                    server.shutdown()
                    server.server_close()
                    thread.join()

    def test_network_failures_distinguish_read_retry_from_unknown_write(self):
        """Exception wrapping and response phase cannot change retry safety or leak the token."""
        failures = (TimeoutError('fixture-secret timeout'), ConnectionResetError('fixture-secret reset'),
                    urllib.error.URLError(TimeoutError('fixture-secret timeout')),
                    http.client.RemoteDisconnected('fixture-secret disconnected'),
                    http.client.IncompleteRead(b'fixture-secret', 20))
        for method in ('GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE'):
            for failure in failures:
                for phase in ('open', 'read'):
                    with self.subTest(method=method, failure=type(failure).__name__, phase=phase):
                        opener = Mock()
                        if phase == 'open':
                            opener.side_effect = failure
                        else:
                            response = MagicMock()
                            response.__enter__.return_value = response
                            response.headers = {}
                            response.read.side_effect = failure
                            opener.return_value = response
                        client = VikunjaClient('https://example.invalid', 'fixture-secret', opener=opener)
                        with self.assertRaises(APIError) as caught:
                            client.request(method, '/tasks')
                        read = method in ('GET', 'HEAD')
                        self.assertEqual(caught.exception.category, 'transport' if read else 'unknown-outcome')
                        self.assertEqual(caught.exception.retryable, read)
                        self.assertNotIn('fixture-secret', str(caught.exception))
                        self.assertEqual(opener.call_count, 1)

    def test_http_status_does_not_advertise_repeating_writes(self):
        """A gateway/server failure may follow a write; explicit denials retain their meaning."""
        for method in ('GET', 'POST'):
            for status in (401, 403, 404, 408, 409, 412, 422, 429, 500, 502, 503, 504):
                with self.subTest(method=method, status=status):
                    body = io.BytesIO(b'{"detail":"fixture-secret failed","code":123}')
                    error = urllib.error.HTTPError('https://example.invalid', status, 'failed', {}, body)
                    self.addCleanup(error.close)
                    opener = Mock(side_effect=error)
                    client = VikunjaClient('https://example.invalid', 'fixture-secret', opener=opener)
                    with self.assertRaises(APIError) as caught:
                        client.request(method, '/tasks')
                    found = caught.exception
                    self.assertEqual((found.status, found.code), (status, 123))
                    expected = ('unknown-outcome' if method == 'POST' and (status == 408 or status >= 500) else
                                'authentication' if status in (401, 403) else
                                'conflict' if status in (409, 412) else
                                'transport' if status in (408, 429, 500, 502, 503, 504) else 'provider')
                    self.assertEqual(found.category, expected)
                    self.assertEqual(found.retryable, method == 'GET' and status in (408, 429, 500, 502, 503, 504))
                    self.assertNotIn('fixture-secret', str(found))
                    self.assertTrue(body.closed)
                    self.assertEqual(opener.call_count, 1)

    def test_unreadable_error_body_keeps_received_status(self):
        """A truncated denial must not become an unknown write or bad input."""
        for status, category in ((401, 'authentication'), (409, 'conflict'), (503, 'unknown-outcome')):
            with self.subTest(status=status):
                body = Mock()
                body.read.side_effect = TimeoutError('fixture-secret timeout')
                error = urllib.error.HTTPError('https://example.invalid', status, 'fixture-secret failed', {}, body)
                self.addCleanup(error.close)
                client = VikunjaClient('https://example.invalid', 'fixture-secret', opener=Mock(side_effect=error))
                with self.assertRaises(APIError) as caught:
                    client.post('/tasks', {})
                self.assertEqual((caught.exception.category, caught.exception.retryable), (category, False))
                self.assertNotIn('fixture-secret', str(caught.exception))
                body.close.assert_called()

    def test_invalid_success_payload_is_not_bad_command_input(self):
        """A write can succeed before malformed JSON prevents receipt of its identity."""
        for method in ('GET', 'POST'):
            for payload in (b'{', b'\xff'):
                with self.subTest(method=method, payload=payload):
                    opener = Mock(return_value=io.BytesIO(payload))
                    client = VikunjaClient('https://example.invalid', 'fixture-secret', opener=opener)
                    with self.assertRaises(APIError) as caught:
                        client.request(method, '/tasks')
                    self.assertEqual(caught.exception.category, 'provider' if method == 'GET' else 'unknown-outcome')
                    self.assertFalse(caught.exception.retryable)
                    self.assertEqual(opener.call_count, 1)

    def test_cli_and_workflow_preserve_transport_diagnostics(self):
        """Both entrypoints must expose the same safe recovery after a failed request."""
        for command in (cli, workflow):
            for method in ('GET', 'POST'):
                with self.subTest(command=command.__name__, method=method):
                    client = VikunjaClient('https://example.invalid', 'fixture-secret',
                                          opener=Mock(side_effect=TimeoutError('timeout')))
                    result = self.diagnostic(command, lambda: client.request(method, '/tasks'))
                    self.assertEqual(result['category'], 'transport' if method == 'GET' else 'unknown-outcome')
                    self.assertEqual(result['retryable'], method == 'GET')
                    if method == 'GET':
                        self.assertIn('Retry only the failed read', result['error'])
        result = self.diagnostic(cli, Mock(side_effect=FileNotFoundError('missing description')))
        self.assertEqual(result['category'], 'input')

    def test_failed_verification_read_does_not_invite_repeating_the_whole_command(self):
        """A successful write followed by a failed GET must remain a single write."""
        opener = Mock(side_effect=[io.BytesIO(b'{"id":7}'), TimeoutError('timeout')])
        client = VikunjaClient('https://example.invalid', 'fixture-secret', opener=opener)

        def create_then_read():
            created = client.post('/tasks', {'title': 'fixture'})
            return client.get(f"/tasks/{created['id']}")

        result = self.diagnostic(cli, create_then_read)
        self.assertEqual([call.args[0].method for call in opener.call_args_list], ['POST', 'GET'])
        self.assertTrue(result['retryable'])
        self.assertIn('Retry only the failed read', result['error'])
        self.assertIn('may have completed earlier writes', result['error'])
