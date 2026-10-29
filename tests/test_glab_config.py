"""Keyring export must produce usable, isolated credentials without borrowing host renewal."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from agent_box import glab_config
from agent_box.errors import AgentBoxError


class GlabSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "config.yml"

    def render(self, text):
        self.path.write_text(text)
        rendered = glab_config.snapshot_config(self.path)
        self.assertEqual(self.path.read_text(), text, "host configuration must stay unchanged")
        return rendered

    def test_each_keyring_host_gets_its_own_token_and_file_hosts_need_no_lookup(self):
        """File fallbacks and a previous host's token cannot masquerade as keyring success."""
        text = ("host: one.example\nhosts:\n"
                "  one.example:\n    token: stale-one\n    use_keyring: \"true\"\n"
                "    custom_headers:\n      - name: Example\n        value: example-value\n"
                "  'two.example:8443':\n    use_keyring: true\n    api_host: api.two.example\n"
                "  file.example:\n    token: file-secret\n    use_keyring: false\n")
        with mock.patch.object(glab_config, "_keyring_credentials", side_effect=[
            {"token": "one-secret", "job_token": ""}, {"token": "", "job_token": "two-job"},
        ]) as lookup:
            result = self.render(text)
        self.assertEqual(lookup.call_args_list, [mock.call("one.example", 1), mock.call("two.example:8443", 2)])
        one, two = result.split("  'two.example:8443':")
        self.assertIn('token: "one-secret"', one)
        self.assertNotIn("two-job", one)
        self.assertIn('job_token: "two-job"', two)
        self.assertNotIn("one-secret", two)
        self.assertNotIn("stale-one", result)
        self.assertEqual(result.count('use_keyring: "false"'), 2)
        self.assertIn("    custom_headers:\n      - name: Example\n        value: example-value\n", result)
        self.assertIn("    token: file-secret\n    use_keyring: false\n", result)

    def test_file_oauth_keeps_expiry_and_type_but_never_shares_rotating_refresh_tokens(self):
        """A writable copy must not consume the host's single-use OAuth refresh token."""
        with mock.patch.object(glab_config.subprocess, "run") as run:
            result = self.render(
                "oauth2_refresh_token: root-refresh\nhosts:\n  oauth.example:\n"
                "    token: access-secret\n    is_oauth2: \"true\"\n"
                "    oauth2_expiry_date: \"2030-01-01T00:00:00Z\"\n"
                "    oauth2_refresh_token: rotating-secret\n    refresh_token: old-refresh\n")
        run.assert_not_called()
        self.assertNotIn("refresh", result)
        self.assertIn("token: access-secret", result)
        self.assertIn('is_oauth2: "true"', result)
        self.assertIn('oauth2_expiry_date: "2030-01-01T00:00:00Z"', result)

    def test_keyring_oauth_keeps_access_token_metadata_and_drops_file_refresh_fallbacks(self):
        """The CLI must choose Bearer auth without ever trying shared renewal."""
        with mock.patch.object(glab_config, "_keyring_credentials", return_value={"token": "access", "job_token": ""}):
            result = self.render("hosts:\n  oauth.example:\n    use_keyring: true\n"
                                 "    is_oauth2: true\n    oauth2_expiry_date: 1900000000\n"
                                 "    oauth2_refresh_token: rotating\n")
        self.assertIn("is_oauth2: true", result)
        self.assertIn("oauth2_expiry_date: 1900000000", result)
        self.assertIn('token: "access"', result)
        self.assertNotIn("rotating", result)
        self.assertNotIn("refresh_token", result)

    def test_plain_configs_and_missing_config_need_neither_glab_nor_keyring(self):
        """The host CLI is needed only to export explicitly configured keyring credentials."""
        with mock.patch.object(glab_config.subprocess, "run") as run:
            self.assertIsNone(glab_config.snapshot_config(self.path))
            for text in ("", "# empty\n", "hosts: {}\n", "host: gitlab.com\n",
                         "hosts:\n  file.example:\n    token: \"file-secret\"\n"):
                self.assertEqual(self.render(text), text)
        run.assert_not_called()

    def test_utf8_bom_cannot_hide_hosts_or_rotating_refresh_tokens(self):
        """A BOM is valid YAML file encoding, not part of the hosts key."""
        with mock.patch.object(glab_config, "_keyring_credentials",
                               return_value={"token": "access", "job_token": ""}) as lookup:
            result = self.render('\ufeffhosts:\n  one.example:\n    use_keyring: true\n'
                                 '    oauth2_refresh_token: rotating-secret\n')
        lookup.assert_called_once_with("one.example", 1)
        self.assertNotIn("rotating-secret", result)
        self.assertIn('token: "access"', result)
        self.assertIn('use_keyring: "false"', result)

    def test_ambiguous_authentication_is_refused_before_any_keyring_read(self):
        """Do not guess YAML aliases, duplicate keys, invalid nesting or multiline credentials."""
        invalid = [
            "hosts: {one.example: {use_keyring: true}}\n",
            "hosts:\n  one.example: *defaults\n",
            "hosts:\n  one.example:\n    <<: *defaults\n",
            "hosts:\n  one.example:\n    use_keyring: *secret\n",
            "hosts:\n  one.example:\n    use_keyring: true\n    use_keyring: false\n",
            "hosts:\n  one.example:\n    token: |\n      secret\n",
            "hosts:\n  one.example:\n    token: \"secret\n",
            "hosts:\n  one.example:\n    token: !!str secret\n",
            "hosts:\n  one.example: {}\nhosts: {}\n",
            "hosts:\n  one.example: {}\n  'one.example': {}\n",
            "hosts:\n  one.example:\n\tuse_keyring: true\n",
            "---\nhosts: {}\n---\nhosts: {}\n",
            "hosts:\n  'bad host':\n    use_keyring: true\n",
        ]
        for text in invalid:
            with self.subTest(text=text), mock.patch.object(glab_config.subprocess, "run") as run:
                self.path.write_text(text)
                with self.assertRaises(AgentBoxError) as caught:
                    glab_config.snapshot_config(self.path)
                self.assertNotIn("secret", str(caught.exception))
                run.assert_not_called()

    def test_lookup_isolated_from_environment_file_and_repository_fallbacks(self):
        """Only the host-specific keyring can supply a result, despite poisoned ambient config."""
        temporary_paths = []
        def lookup(argv, **kwargs):
            temporary = Path(kwargs["env"]["GLAB_CONFIG_DIR"])
            temporary_paths.append(temporary)
            self.assertEqual(json.loads((temporary / "config.yml").read_text()),
                             {"hosts": {"one.example": {"use_keyring": "true"}}})
            self.assertEqual(temporary.stat().st_mode & 0o777, 0o700)
            self.assertEqual((temporary / "config.yml").stat().st_mode & 0o777, 0o600)
            self.assertEqual(kwargs["cwd"], temporary)
            env = kwargs["env"]
            for name in ("GITLAB_TOKEN", "GITLAB_ACCESS_TOKEN", "OAUTH_TOKEN", "GLAB_IS_OAUTH2",
                         "USE_KEYRING", "GLAB_USE_KEYRING", "CI_JOB_TOKEN", "GIT_DIR", "DEBUG"):
                self.assertNotIn(name, env)
            self.assertEqual(env["DBUS_SESSION_BUS_ADDRESS"], "session-bus")
            self.assertEqual(env["GLAB_SEND_TELEMETRY"], "false")
            self.assertEqual(env["GLAB_CHECK_UPDATE"], "false")
            self.assertEqual(argv, ["glab", "config", "get", argv[3], "--host", "one.example"])
            self.assertTrue(kwargs["capture_output"])
            return subprocess.CompletedProcess(argv, 0, "keyring-secret\n" if argv[3] == "token" else "", "")
        poison = {key: "ambient-secret" for key in (
            "GITLAB_TOKEN", "GITLAB_ACCESS_TOKEN", "OAUTH_TOKEN", "GLAB_IS_OAUTH2", "USE_KEYRING",
            "GLAB_USE_KEYRING", "CI_JOB_TOKEN", "GIT_DIR", "DEBUG")}
        poison["DBUS_SESSION_BUS_ADDRESS"] = "session-bus"
        with mock.patch.dict(os.environ, poison), mock.patch.object(glab_config.subprocess, "run", side_effect=lookup):
            result = self.render("token: global-fallback\nhosts:\n  one.example:\n"
                                 "    token: file-fallback\n    use_keyring: true\n")
        self.assertIn('token: "keyring-secret"', result)
        self.assertNotIn("file-fallback", result)
        self.assertTrue(all(not path.exists() for path in temporary_paths))

    def test_lookup_errors_empty_or_malformed_output_cannot_leak_subprocess_secrets(self):
        """CLI stderr, timeout buffers and decoded stdout never become diagnostics."""
        failures = [
            FileNotFoundError("secret-in-exception"),
            OSError("secret-in-exception"),
            subprocess.TimeoutExpired(["glab"], 15, output="secret-in-output"),
            subprocess.CompletedProcess([], 1, "secret-in-output", "secret-in-error"),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "secret\ninjected\n", ""),
        ]
        for failure in failures:
            with self.subTest(kind=type(failure).__name__):
                kwargs = {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}
                with mock.patch.object(glab_config.subprocess, "run", **kwargs):
                    with self.assertRaises(AgentBoxError) as caught:
                        self.render("hosts:\n  one.example:\n    use_keyring: true\n    token: fallback\n")
                self.assertNotIn("secret", str(caught.exception))
                self.assertNotIn("fallback", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
