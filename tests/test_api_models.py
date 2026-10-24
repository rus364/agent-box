"""Provider model lists are advice: parsed from each documented shape, and never a way to leak the key."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

from agent_box import api_models

SAMPLES = Path(__file__).parent / 'api_model_samples'
KEY = 'sk-private-model-list-key'


def sample(name):
    return json.loads((SAMPLES / name).read_text())


class Server:
    """A loopback API: ``routes`` maps a request path to ``(status, body)`` or a callable."""

    def __init__(self, routes):
        self.routes, self.requests = routes, []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.requests.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
                route = outer.routes.get(self.path, (404, {}))
                status, body, *headers = route(outer) if callable(route) else route
                payload = json.dumps(body).encode()
                self.send_response(status)
                for name, value in (headers[0] if headers else {}).items():
                    self.send_header(name, value)
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                try:
                    self.wfile.write(payload)
                except BrokenPipeError:
                    pass  # the client gave up first, as a timeout test intends

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.httpd.server_port}'
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def provider(codex='', claude='', key=KEY):
    return {'api_key': key, 'codex': {'url': codex}, 'claude': {'url': claude}}


class ModelFieldTests(unittest.TestCase):
    def test_each_documented_list_shape_fills_only_the_fields_it_states(self):
        """A field the API does not state must stay manual rather than receive an invented value."""
        deepseek = {item['id']: api_models.model_fields(item) for item in sample('deepseek.json')['data']}
        self.assertEqual(deepseek['deepseek-flash'], {
            'display_name': 'DeepSeek-V4.1-Flash', 'context_window': 1048576, 'max_output_tokens': 393216, 'input_modalities': ['text', 'image'],
            'supported_efforts': ['low', 'high', 'max'], 'effort': 'high'})
        self.assertEqual(deepseek['deepseek-v4-pro']['input_modalities'], ['text'])
        openrouter = {item['id']: api_models.model_fields(item) for item in sample('openrouter.json')['data']}
        self.assertEqual(openrouter['deepseek/deepseek-v4.1-flash']['supported_efforts'], ['low', 'high', 'max'])
        self.assertEqual(openrouter['openai/gpt-5.6-luna']['effort'], 'medium')
        # OpenRouter's "file" input has no agent-box counterpart.
        self.assertEqual(openrouter['anthropic/claude-opus-5']['input_modalities'], ['text', 'image'])
        plain = openrouter['unbiased/pareto-26.10-preview']
        self.assertNotIn('supported_efforts', plain)
        self.assertNotIn('effort', plain)
        self.assertIn('context_window', plain)
        anthropic = [api_models.model_fields(item) for page in ('anthropic-page1.json', 'anthropic-page2.json')
                     for item in sample(page)['data']]
        self.assertEqual(anthropic[0], {
            'display_name': 'Claude Opus 5', 'context_window': 1000000, 'max_output_tokens': 128000,
            'input_modalities': ['text', 'image'],
            'supported_efforts': ['low', 'medium', 'high', 'xhigh', 'max'], 'effort': 'default'})
        # Haiku states it takes no effort: a stale declaration must not survive an update.
        self.assertEqual((anthropic[1]['supported_efforts'], anthropic[1]['effort']), ([], 'default'))
        self.assertEqual(api_models.model_fields({'id': 'm', 'effort': {'supported_levels': ['turbo']}}),
                         {'supported_efforts': [], 'effort': 'default'})
        self.assertEqual([api_models.model_fields(item) for item in sample('openai.json')['data']], [{}, {}])

    def test_routing_alias_does_not_advertise_one_models_limits_or_capabilities(self):
        """A router's metadata is not a guarantee about the model chosen on the next call."""
        for model in ('openrouter/free', 'openrouter/auto'):
            with self.subTest(model=model):
                fields = api_models.model_fields({
                    'id': model, 'name': 'Router', 'context_length': 200000,
                    'top_provider': {'max_completion_tokens': 8192},
                    'architecture': {'input_modalities': ['text', 'image']},
                    'reasoning': {'supported_efforts': ['high'], 'default_effort': 'high'},
                    'supported_parameters': ['tools', 'reasoning', 'parallel_tool_calls']})
                self.assertEqual(fields, {'display_name': 'Router'})

    def test_output_limits_require_positive_integers_and_tools_do_not_imply_parallel_calls(self):
        """An output ceiling is usable, but a parameter name does not establish client tool support."""
        fields = api_models.model_fields({'id': 'vendor/model', 'context_length': 65536,
            'top_provider': {'max_completion_tokens': 4096},
            'supported_parameters': ['tools', 'reasoning', 'parallel_tool_calls'],
            'architecture': {'input_modalities': ['text', 'audio'], 'output_modalities': ['audio']}})
        self.assertEqual(fields, {'context_window': 65536, 'max_output_tokens': 4096,
                                  'input_modalities': ['text']})
        for value in (None, True, -1, 0, '4096', 4096.0):
            with self.subTest(value=value):
                self.assertEqual(api_models.model_fields({'id': 'm', 'max_output_tokens': value,
                    'top_provider': {'max_completion_tokens': value}}), {})


class LoadTests(unittest.TestCase):
    def serve(self, routes):
        server = Server(routes)
        self.addCleanup(server.close)
        return server

    def test_codex_url_is_asked_first_and_claude_url_is_the_fallback(self):
        """DeepSeek's Anthropic URL has no list, so the OpenAI-compatible one leads; either serves both CLIs."""
        server = self.serve({'/openai/models': (200, sample('deepseek.json'))})
        models, error = api_models.load(provider(server.url + '/openai', server.url + '/anthropic'))
        self.assertIsNone(error)
        self.assertEqual(list(models), ['deepseek-flash', 'deepseek-v4-pro'])
        self.assertEqual([path for path, _ in server.requests], ['/openai/models'])
        self.assertEqual(server.requests[0][1]['authorization'], 'Bearer ' + KEY)

        server = self.serve({
            '/anthropic/v1/models?limit=1000': (200, sample('anthropic-page1.json')),
            '/anthropic/v1/models?limit=1000&after_id=claude-opus-5': (200, sample('anthropic-page2.json'))})
        models, error = api_models.load(provider(server.url + '/openai', server.url + '/anthropic'))
        self.assertIsNone(error)
        self.assertEqual(list(models), ['claude-opus-5', 'claude-haiku-4-5'])
        anthropic = [headers for path, headers in server.requests if path.startswith('/anthropic')]
        self.assertTrue(all(h['x-api-key'] == KEY and h['anthropic-version'] for h in anthropic))
        self.assertTrue(all('authorization' not in h for h in anthropic))

    def test_pages_follow_the_response_cursor_whichever_url_answered(self):
        """A Claude URL may answer in OpenRouter's shape; stopping after its first page would hide models."""
        full = sample('openrouter.json')
        routes = {'/api/v1/models?offset=2': (200, dict(full, data=full['data'][2:]))}
        server = self.serve(routes)
        routes['/api/v1/models?limit=1000'] = (200, dict(full, data=full['data'][:2],
                                                          links={'next': server.url + '/api/v1/models?offset=2'}))
        models, error = api_models.load(provider(claude=server.url + '/api'))
        self.assertIsNone(error)
        self.assertEqual(list(models), [item['id'] for item in full['data']])

    def test_a_list_cut_short_is_reported_not_shown_as_whole(self):
        """Models past a foreign next link, a missing cursor or the page bound would be falsely marked not listed."""
        first = dict(sample('openrouter.json'), data=sample('openrouter.json')['data'][:2])
        routes = {}
        server = self.serve(routes)
        routes['/api/v1/models'] = (200, dict(first, links={'next': 'http://localhost:1/api/v1/models?offset=2'}))
        models, error = api_models.load(provider(server.url + '/api/v1'))
        self.assertEqual(models, {})
        self.assertIn('another scheme or host', error)
        self.assertEqual(len(server.requests), 1)
        # Same host, other scheme: following it could send the key in plaintext.
        routes['/api/v1/models'] = (200, dict(first, links={'next': server.url.replace('http:', 'https:') + '/api/v1/models'}))
        models, error = api_models.load(provider(server.url + '/api/v1'))
        self.assertEqual(models, {})
        self.assertIn('another scheme or host', error)
        routes['/anthropic/v1/models?limit=1000'] = (200, dict(sample('anthropic-page1.json'), last_id=None))
        models, error = api_models.load(provider(claude=server.url + '/anthropic'))
        self.assertEqual(models, {})
        self.assertIn('without a cursor', error)
        routes['/api/v1/models'] = (200, dict(first, links={'next': server.url + '/api/v1/models'}))
        with patch.object(api_models, 'PAGES', 3):
            models, error = api_models.load(provider(server.url + '/api/v1'))
        self.assertEqual(models, {})
        self.assertIn('more than 3 pages', error)

    def test_failures_become_a_diagnostic_that_never_carries_the_key(self):
        """The menu shows this text; a key in it, in argv or after a redirect would be a disclosure."""
        def slow(server):
            time.sleep(0.5)
            return 200, sample('openai.json')
        server = self.serve({
            '/broken/models': (500, {'error': KEY}),
            '/moved/models': (302, {}, {'Location': '/elsewhere/models'}),
            '/elsewhere/models': (200, sample('openai.json')),
            '/html/models': (200, ['not', 'a', 'list']),
            '/slow/models': slow})
        with patch('subprocess.Popen', side_effect=AssertionError('a process would expose argv')), \
                patch.object(api_models, 'TIMEOUT', 0.1):
            for base, reason in (('/broken', 'HTTP 500'), ('/moved', 'HTTP 302'),
                                 ('/html', 'not a model list'), ('/slow', 'network error or timeout')):
                with self.subTest(base=base):
                    models, error = api_models.load(provider(server.url + base))
                    self.assertEqual(models, {})
                    self.assertIn(reason, error)
                    self.assertNotIn(KEY, error)
        self.assertNotIn('/elsewhere/models', [path for path, _ in server.requests])
        self.assertIn('API key', api_models.load(provider(server.url + '/broken', key=' '))[1])
        self.assertIn('URL', api_models.load(provider())[1])
        # The menu asks before save-time validation, so a draft URL may be malformed.
        self.assertIn('malformed URL', api_models.load(provider('http://[::1'))[1])


if __name__ == '__main__':
    unittest.main()
