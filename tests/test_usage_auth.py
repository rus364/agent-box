"""Usage refresh must neither rotate valid credentials nor corrupt a CLI login."""
import json
import http.client
import os
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from agent_box import usage, usage_auth
from agent_box.atomicio import read_json
from agent_box.errors import AgentBoxError


class UsageAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.auth = self.root / '.credentials.json'
        self.cache = self.root / 'cache.json'
        self.oauth = {'accessToken': 'old-access', 'refreshToken': 'old-refresh',
                      'expiresAt': (time.time() + 3600) * 1000,
                      'scopes': ['user:profile', 'user:inference'], 'subscriptionType': 'max'}
        self.save(self.oauth)
        self.live = {'five_hour': {'utilization': 10, 'resets_at': time.time() + 3600},
                     'seven_day': {'utilization': 20, 'resets_at': time.time() + 604800}}
        self.renewal = {'access_token': 'new-access', 'refresh_token': 'new-refresh', 'expires_in': 3600}
        self.enterContext(mock.patch.dict(os.environ, {'CLAUDE_CREDENTIALS_PATH': str(self.auth),
                                                      'CLAUDE_CODE_CUSTOM_OAUTH_URL': ''}))
        os.environ.pop('CLAUDE_CODE_CUSTOM_OAUTH_URL', None)
        self.enterContext(mock.patch.object(usage, 'CLAUDE_USAGE_CACHE', self.cache))

    def save(self, oauth):
        self.auth.write_text(json.dumps({'claudeAiOauth': oauth, 'unrelated': {'keep': True}}))

    def expire(self):
        self.oauth['expiresAt'] = (time.time() - 3600) * 1000
        self.save(self.oauth)

    def rejected(self, status=401):
        return urllib.error.HTTPError('https://example.invalid/SECRET', status, 'SECRET', {}, None)

    def snapshot(self):
        return usage.usage_snapshot('claude', no_cache=True)

    def test_no_cache_fetches_live_without_rotating_valid_token(self):
        """Ignoring usage cache must not mean ignoring token validity."""
        self.cache.write_text(json.dumps(self.live))
        before = self.auth.read_bytes()
        with mock.patch.object(usage, 'http_json', return_value=self.live) as http:
            result = self.snapshot()
        self.assertTrue(result['fresh'])
        self.assertEqual(result['source'], 'live')
        self.assertEqual(http.call_count, 1)
        self.assertIsNone(http.call_args.kwargs.get('data'))
        self.assertEqual(self.auth.read_bytes(), before)

    def test_valid_symlink_credentials_are_reused_without_refresh(self):
        """A read-only usage request must preserve working user-owned links."""
        target = self.root / 'credentials-target.json'
        self.auth.rename(target)
        self.auth.symlink_to(target)
        before = target.read_bytes()
        with mock.patch.object(usage, 'http_json', return_value=self.live) as http:
            self.assertTrue(self.snapshot()['fresh'])
        self.assertEqual(http.call_count, 1)
        self.assertIsNone(http.call_args.kwargs.get('data'))
        self.assertTrue(self.auth.is_symlink())
        self.assertEqual(target.read_bytes(), before)

    def test_refresh_never_replaces_a_credential_symlink(self):
        """Renewal must preserve links present initially or created during HTTP."""
        self.expire()
        target = self.root / 'credentials-target.json'
        before = self.auth.read_bytes()
        def link():
            self.auth.rename(target)
            self.auth.symlink_to(target)
        def response(*args):
            link()
            return self.renewal
        with self.assertRaisesRegex(AgentBoxError, 'symlink cannot be refreshed'):
            usage_auth.refresh(self.auth, self.oauth, response)
        self.assertTrue(self.auth.is_symlink())
        self.assertEqual(target.read_bytes(), before)
        with mock.patch.object(usage, 'http_json') as http:
            result = self.snapshot()
        self.assertFalse(result['fresh'])
        http.assert_not_called()
        self.assertTrue(self.auth.is_symlink())
        self.assertEqual(target.read_bytes(), before)

    def test_expired_token_refreshes_once_and_next_call_reuses_it(self):
        """Persist rotated tokens so repeated explicit reads do not refresh again."""
        self.expire()
        with mock.patch.object(usage, 'http_json', side_effect=[self.renewal, self.live, self.live]) as http:
            self.assertTrue(self.snapshot()['fresh'])
            self.assertTrue(self.snapshot()['fresh'])
        self.assertEqual(http.call_count, 3)
        body = json.loads(http.call_args_list[0].kwargs['data'])
        self.assertEqual(body['refresh_token'], 'old-refresh')
        self.assertEqual(body['scope'], 'user:profile user:inference')
        self.assertEqual(http.call_args_list[0].kwargs['headers']['User-Agent'], 'claude-cli (external, cli)')
        self.assertEqual(http.call_args_list[1].kwargs['headers']['Authorization'], 'Bearer new-access')
        stored = read_json(self.auth)
        self.assertEqual(stored['unrelated'], {'keep': True})
        self.assertEqual(stored['claudeAiOauth']['subscriptionType'], 'max')
        self.assertEqual(stored['claudeAiOauth']['refreshToken'], 'new-refresh')
        self.assertEqual(self.auth.stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.root / '.oauth_refresh.lock').exists())
        self.assertFalse((self.root / '.storage-write.lock').exists())

    def test_401_refreshes_and_retries_only_once(self):
        """A server rejection overrides the local expiry check without a loop."""
        with mock.patch.object(usage, 'http_json', side_effect=[self.rejected(), self.renewal, self.rejected()]) as http:
            result = self.snapshot()
        self.assertFalse(result['fresh'])
        self.assertIn('HTTP 401', result['error'])
        self.assertNotIn('SECRET', result['error'])
        self.assertEqual(http.call_count, 3)

    def test_401_after_expiry_refresh_does_not_rotate_twice(self):
        """Rejecting a newly renewed token must end the call, not start a refresh loop."""
        self.expire()
        with mock.patch.object(usage, 'http_json', side_effect=[self.renewal, self.rejected()]) as http:
            self.assertFalse(self.snapshot()['fresh'])
        self.assertEqual(http.call_count, 2)

    def test_non_auth_http_errors_never_refresh(self):
        """Rate limits and server failures cannot be repaired by rotating credentials."""
        for status in (403, 429, 500):
            with self.subTest(status=status), mock.patch.object(usage, 'http_json', side_effect=self.rejected(status)) as http:
                result = self.snapshot()
                self.assertIn(f'HTTP {status}', result['error'])
                self.assertEqual(http.call_count, 1)

    def test_failed_refresh_preserves_credentials_and_stale_cache(self):
        """An authorization failure must not destroy credentials or claim cached data is fresh."""
        self.expire()
        before = self.auth.read_bytes()
        self.cache.write_text(json.dumps(self.live))
        with mock.patch.object(usage, 'http_json', side_effect=self.rejected(400)) as http:
            result = self.snapshot()
        self.assertFalse(result['fresh'])
        self.assertEqual(result['source'], 'cache')
        self.assertIn('token refresh failed: HTTP 400', result['error'])
        self.assertEqual(http.call_count, 1)
        self.assertEqual(self.auth.read_bytes(), before)

    def test_bad_refresh_response_never_overwrites_credentials(self):
        """Malformed renewal data must not replace a recoverable account login."""
        self.expire()
        before = self.auth.read_bytes()
        for response in ({}, [], {**self.renewal, 'expires_in': float('nan')},
                         {**self.renewal, 'access_token': ''}):
            with self.subTest(response=response), mock.patch.object(usage, 'http_json', return_value=response):
                result = self.snapshot()
                self.assertFalse(result['fresh'])
                self.assertIn('invalid response', result['error'])
                self.assertEqual(self.auth.read_bytes(), before)

    def test_reread_adopts_another_process_token_without_refresh(self):
        """A sibling renewal must prevent a redundant rotation of its refresh token."""
        self.expire()
        new = {**self.oauth, 'accessToken': 'sibling', 'expiresAt': (time.time() + 3600) * 1000}
        self.save(new)
        with mock.patch.object(usage, 'refresh_request') as request:
            self.assertEqual(usage_auth.refresh(self.auth, self.oauth, request), new)
        request.assert_not_called()

    def test_login_during_refresh_is_preserved(self):
        """An in-flight renewal must not undo a newer account login."""
        self.expire()
        new = {**self.oauth, 'accessToken': 'sibling', 'refreshToken': 'sibling-refresh'}
        def response(*args):
            self.save(new)
            return self.renewal
        result = usage_auth.refresh(self.auth, self.oauth, response)
        self.assertEqual(result, new)
        self.assertEqual(read_json(self.auth)['claudeAiOauth'], new)

    def test_logout_during_refresh_is_not_undone(self):
        """A completed logout must not be resurrected by an earlier request."""
        self.expire()
        def response(*args):
            self.auth.unlink()
            return self.renewal
        with self.assertRaisesRegex(AgentBoxError, 'credentials missing'):
            usage_auth.refresh(self.auth, self.oauth, response)
        self.assertFalse(self.auth.exists())

    def test_unrelated_write_during_refresh_is_preserved(self):
        """Renewing one OAuth entry must preserve other credential-store changes."""
        self.expire()
        def response(*args):
            data = read_json(self.auth)
            data['unrelated'] = 'new settings'
            self.auth.write_text(json.dumps(data))
            return self.renewal
        usage_auth.refresh(self.auth, self.oauth, response)
        self.assertEqual(read_json(self.auth)['unrelated'], 'new settings')

    def test_contended_refresh_lock_never_posts_or_removes_foreign_lock(self):
        """A concurrent refresher owns both the rotation and its lock directory."""
        self.expire()
        path = self.root / '.oauth_refresh.lock'
        path.mkdir()
        with mock.patch.object(usage, 'http_json') as http:
            result = self.snapshot()
        self.assertIn('busy', result['error'])
        http.assert_not_called()
        self.assertTrue(path.is_dir())

    def test_contended_storage_lock_never_overwrites_credentials(self):
        """Do not rotate a token when its replacement cannot be saved under the write lock."""
        self.expire()
        before = self.auth.read_bytes()
        (self.root / '.storage-write.lock').mkdir()
        with mock.patch.object(usage, 'http_json', return_value=self.renewal) as http:
            result = self.snapshot()
        self.assertIn('busy', result['error'])
        http.assert_not_called()
        self.assertEqual(self.auth.read_bytes(), before)

    def test_lost_refresh_lock_prevents_writing_or_removing_replacement(self):
        """A replaced lock invalidates our right to persist credentials or remove that lock."""
        self.expire()
        path = self.root / '.oauth_refresh.lock'
        before = self.auth.read_bytes()
        def response(*args):
            path.rename(self.root / 'old-lock')
            path.mkdir()
            return self.renewal
        with self.assertRaisesRegex(AgentBoxError, 'lock expired or changed'):
            usage_auth.refresh(self.auth, self.oauth, response)
        self.assertTrue(path.is_dir())
        self.assertEqual(self.auth.read_bytes(), before)

    def test_statusline_read_never_refreshes_an_expired_or_rejected_token(self):
        """Frequent display callbacks must not trigger account-token rotation."""
        self.expire()
        with mock.patch.object(usage, 'http_json', side_effect=self.rejected()) as http:
            usage.claude_usage(allow_refresh=False)
        self.assertEqual(http.call_count, 1)
        self.assertIsNone(http.call_args.kwargs.get('data'))
        self.assertEqual(read_json(self.auth)['claudeAiOauth'], self.oauth)

    def test_fresh_cache_never_requires_credentials(self):
        """A permitted cached display should work without accessing account credentials."""
        self.cache.write_text(json.dumps(self.live))
        self.auth.unlink()
        with mock.patch.object(usage, 'http_json') as http:
            self.assertEqual(usage.usage_snapshot('claude')['source'], 'cache')
        http.assert_not_called()

    def test_expired_lock_lease_does_not_persist_refresh(self):
        """A stalled request must not write after Claude may reclaim its lease."""
        self.expire()
        before = self.auth.read_bytes()
        with mock.patch.object(usage_auth.time, 'monotonic', side_effect=[0, 0, 46]):
            with self.assertRaisesRegex(AgentBoxError, 'lock expired or changed'):
                usage_auth.refresh(self.auth, self.oauth, lambda *args: self.renewal)
        self.assertEqual(self.auth.read_bytes(), before)

    def test_malformed_credentials_are_reported_without_network_or_secrets(self):
        """Invalid local credentials must fail before HTTP without echoing their contents."""
        for text in ('SECRET', '[]', '{"claudeAiOauth": null}'):
            self.auth.write_text(text)
            with mock.patch.object(usage, 'http_json') as http:
                result = self.snapshot()
            self.assertFalse(result['fresh'])
            self.assertNotIn('SECRET', result['error'])
            http.assert_not_called()

    def test_missing_refresh_token_returns_actionable_error(self):
        """A login that cannot renew needs an explicit sign-in diagnostic."""
        self.expire()
        del self.oauth['refreshToken']
        self.save(self.oauth)
        with mock.patch.object(usage, 'http_json') as http:
            result = self.snapshot()
        self.assertIn('refresh token missing', result['error'])
        http.assert_not_called()

    def test_custom_oauth_credentials_never_go_to_default_refresh_endpoint(self):
        """Custom-server refresh credentials must not be sent to the default server."""
        self.expire()
        with mock.patch.dict(os.environ, {'CLAUDE_CODE_CUSTOM_OAUTH_URL': 'https://claude.fedstart.com/'}), \
                mock.patch.object(usage, 'http_json', side_effect=[self.renewal, self.live, self.live]) as http:
            self.assertTrue(self.snapshot()['fresh'])
            self.assertTrue(self.snapshot()['fresh'])
        self.assertEqual([c.args[0] for c in http.call_args_list], [
            'https://claude.fedstart.com/v1/oauth/token',
            'https://claude.fedstart.com/api/oauth/usage',
            'https://claude.fedstart.com/api/oauth/usage'])

    def test_custom_endpoint_401_uses_its_own_token_endpoint(self):
        """Server rejection must recover on the configured server, without a default-server fallback."""
        with mock.patch.dict(os.environ, {'CLAUDE_CODE_CUSTOM_OAUTH_URL': 'https://claude.fedstart.com'}), \
                mock.patch.object(usage, 'http_json', side_effect=[self.rejected(), self.renewal, self.live]) as http:
            self.assertTrue(self.snapshot()['fresh'])
        self.assertEqual([c.args[0] for c in http.call_args_list], [
            'https://claude.fedstart.com/api/oauth/usage',
            'https://claude.fedstart.com/v1/oauth/token',
            'https://claude.fedstart.com/api/oauth/usage'])

    def test_renewal_uses_the_credentials_client_or_configured_claude_client(self):
        """Refresh grants must identify the client that owns the credentials."""
        for stored in (None, 'stored-client'):
            self.expire()
            if stored:
                self.oauth['clientId'] = stored
                self.save(self.oauth)
            with mock.patch.dict(os.environ, {'CLAUDE_CODE_OAUTH_CLIENT_ID': 'configured-client'}), \
                    mock.patch.object(usage, 'http_json', side_effect=[self.renewal, self.live]) as http:
                self.assertTrue(self.snapshot()['fresh'])
            self.assertEqual(json.loads(http.call_args_list[0].kwargs['data'])['client_id'],
                             stored or 'configured-client')

    def test_unapproved_custom_origin_never_receives_refresh_credentials(self):
        """An access-token override must not also expose reusable refresh credentials."""
        for renewing in (False, True):
            if renewing:
                self.expire()
            with self.subTest(renewing=renewing), \
                    mock.patch.dict(os.environ, {'CLAUDE_CODE_CUSTOM_OAUTH_URL': 'https://example.invalid'}), \
                    mock.patch.object(usage, 'http_json', side_effect=self.rejected()) as http:
                result = self.snapshot()
            self.assertFalse(result['fresh'])
            self.assertIn('not approved', result['error'])
            self.assertEqual(http.call_count, 0 if renewing else 1)
            self.assertTrue(all(c.kwargs.get('data') is None for c in http.call_args_list))

    def test_credential_lock_and_save_errors_are_distinct_from_network_errors(self):
        """Local storage failures need file diagnostics without exposing exception details."""
        self.expire()
        before = self.auth.read_bytes()
        for operation in ('credential_lock', 'atomic_write_json'):
            with self.subTest(operation=operation), \
                    mock.patch.object(usage_auth, operation, side_effect=OSError('SECRET')), \
                    mock.patch.object(usage, 'http_json', return_value=self.renewal):
                result = self.snapshot()
            self.assertFalse(result['fresh'])
            self.assertIn('credentials could not be updated', result['error'])
            self.assertNotIn('network', result['error'])
            self.assertNotIn('SECRET', result['error'])
            self.assertEqual(self.auth.read_bytes(), before)

    def test_diagnostics_never_include_network_or_response_secrets(self):
        """Exceptions may contain credentials or URLs unsuitable for public diagnostics."""
        for exc in (OSError('SECRET'), ValueError('SECRET'), self.rejected(500)):
            with self.subTest(exc=type(exc)), mock.patch.object(usage, 'http_json', side_effect=exc):
                result = self.snapshot()
                self.assertFalse(result['fresh'])
                self.assertNotIn('SECRET', json.dumps(result))

    def test_truncated_http_bodies_preserve_failure_and_routing_fallback(self):
        """Partial responses must neither escape routing nor expose response bytes."""
        from agent_box.routing import select
        from test_routing import RoutingTests
        config = RoutingTests().config()
        config.routing = 'usage-aware'
        for renewing in (False, True):
            if renewing:
                self.expire()
            before = self.auth.read_bytes()
            for exc in (http.client.IncompleteRead(b'SECRET', 10), http.client.BadStatusLine('SECRET')):
                with self.subTest(renewing=renewing, exception=type(exc).__name__), \
                        mock.patch.object(usage, 'http_json', side_effect=exc):
                    result = self.snapshot()
                self.assertFalse(result['fresh'])
                self.assertIn('network', result['error'])
                self.assertNotIn('SECRET', json.dumps(result))
                self.assertEqual(self.auth.read_bytes(), before)
                route = select(config, 'worker', 2, 'codex', {}, {'claude': result})
                self.assertIn('static weights', route['reason'])

    def test_redirects_are_refused_before_credentials_can_leave_endpoint(self):
        """An endpoint redirect must not forward bearer credentials to another destination."""
        handler = usage.NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 307, '', {}, 'https://elsewhere.invalid'))
