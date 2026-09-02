"""Vikunja skill access: what reaches the container, and at what address.

Two questions this file exists to answer, both of which an earlier agent-box
got wrong by omission rather than by a bad decision:

* Does the credential reach the container *only* because the switch is on?
  Asserted against the generated podman argv, not against the helper
  functions, because argv is what actually determines exposure.
* Does the address the container receives point at the *host's* server? A
  loopback URL that is merely forwarded verbatim addresses the container
  itself, which fails with no useful diagnostic.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_box import runtime
from agent_box.config import Config
from agent_box.workspace import Workspace

TOKEN = "tk_secret_vikunja_token"


class _Host:
    """A temporary stand-in for the host's Vikunja installation."""

    def __init__(self, root: Path, *, url: str | None = "http://127.0.0.1:3456",
                 token: bool = True, skill: bool = True):
        self.home = root / "home"
        self.config_dir = self.home / ".config/vikunja"
        self.state_dir = self.home / ".local/state/vikunja"
        self.entrypoint = self.home / ".agents/skills/vikunja/scripts/vikunja.py"
        self.config_dir.mkdir(parents=True)
        if url is not None:
            (self.config_dir / "config.json").write_text('{"url": "%s"}\n' % url)
        if token:
            (self.config_dir / "token").write_text(TOKEN + "\n")
        if skill:
            self.entrypoint.parent.mkdir(parents=True)
            self.entrypoint.write_text("#!/usr/bin/env python3\n")

    def patches(self):
        return (
            mock.patch.object(runtime, "HOST_HOME", self.home),
            mock.patch.object(runtime, "VIKUNJA_CONFIG_DIR", self.config_dir),
            mock.patch.object(runtime, "VIKUNJA_STATE_DIR", self.state_dir),
            mock.patch.object(runtime, "VIKUNJA_SKILL_ENTRYPOINT", self.entrypoint),
        )


def _run(host: _Host, config: Config, fn, env=None):
    base = {k: v for k, v in os.environ.items() if k != "VIKUNJA_URL"}
    base["AGENT_BOX_KVM"] = "0"
    base.update(env or {})
    with mock.patch.dict(os.environ, base, clear=True):
        for patch in host.patches():
            patch.start()
        try:
            return fn(config)
        finally:
            mock.patch.stopall()


def _argv(host: _Host, config: Config, env=None):
    """Full podman argv for one run against this fake host installation."""
    project = host.home / "project"
    project.mkdir(parents=True, exist_ok=True)
    state = host.home / "agent-box-state"

    def build(cfg):
        with mock.patch.object(runtime, "STATE_ROOT", state), \
             mock.patch.object(runtime, "BUILD_STATE", state / "build"), \
             mock.patch.object(runtime, "NOTIFY_STATE_ROOT", state / "notify"), \
             mock.patch.object(runtime, "NOTIFY_CREDENTIALS", state / "runtime/notify.json"), \
             mock.patch.object(runtime, "CLAUDE_HOME", host.home / ".claude"), \
             mock.patch.object(runtime, "CODEX_HOME", host.home / ".codex"), \
             mock.patch.object(runtime, "XDG_CACHE_HOME", host.home / ".cache"), \
             mock.patch.object(runtime.subprocess, "run",
                               return_value=mock.Mock(returncode=1, stdout="")):
            return runtime.container_argv(
                Workspace(project, project), [], project, ["claude"], cfg)

    return _run(host, config, build, env)


def _mount_flags(argv):
    return [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "--mount"]


def _mount_for(argv, dst):
    return [f for f in _mount_flags(argv) if f.split(",dst=")[1].split(",")[0] == str(dst)]


def _enabled():
    config = Config()
    config.vikunja.enabled = True
    return config


class UrlRewritingTests(unittest.TestCase):
    """The host's own setting must keep working, unedited, inside a container."""

    def test_ipv4_loopback_becomes_the_host_alias(self):
        self.assertEqual(
            runtime.container_vikunja_url("http://127.0.0.1:3456"),
            "http://host.containers.internal:3456")

    def test_localhost_becomes_the_host_alias(self):
        self.assertEqual(
            runtime.container_vikunja_url("http://localhost:3456"),
            "http://host.containers.internal:3456")

    def test_ipv6_loopback_becomes_the_host_alias(self):
        self.assertEqual(
            runtime.container_vikunja_url("http://[::1]:3456"),
            "http://host.containers.internal:3456")

    def test_scheme_port_and_path_are_preserved(self):
        self.assertEqual(
            runtime.container_vikunja_url("https://127.0.0.1:8443/vikunja/api"),
            "https://host.containers.internal:8443/vikunja/api")

    def test_a_loopback_url_without_a_port_keeps_having_none(self):
        self.assertEqual(
            runtime.container_vikunja_url("http://localhost"),
            "http://host.containers.internal")

    def test_a_routable_host_is_left_exactly_alone(self):
        for url in ("https://tasks.example.com/api", "http://192.168.1.203:3456"):
            with self.subTest(url=url):
                self.assertEqual(runtime.container_vikunja_url(url), url)

    def test_a_malformed_authority_is_handed_through_unrepaired(self):
        """Not agent-box's error to fix; the skill reports it far better."""
        self.assertEqual(
            runtime.container_vikunja_url("http://127.0.0.1:not-a-port"),
            "http://127.0.0.1:not-a-port")

    def test_empty_stays_empty(self):
        self.assertEqual(runtime.container_vikunja_url("   "), "")


class UrlResolutionTests(unittest.TestCase):
    """Same precedence the skill's own CLI applies, so the two never disagree."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_the_config_file_url_is_used(self):
        host = _Host(self.root)
        self.assertEqual(
            _run(host, Config(), lambda _: runtime.host_vikunja_url()),
            "http://127.0.0.1:3456")

    def test_an_exported_url_wins_over_the_config_file(self):
        host = _Host(self.root)
        self.assertEqual(
            _run(host, Config(), lambda _: runtime.host_vikunja_url(),
                 env={"VIKUNJA_URL": "https://tasks.example.com"}),
            "https://tasks.example.com")

    def test_no_url_anywhere_is_empty_not_an_error(self):
        host = _Host(self.root, url=None)
        self.assertEqual(_run(host, Config(), lambda _: runtime.host_vikunja_url()), "")

    def test_an_unreadable_config_file_is_empty_not_an_error(self):
        host = _Host(self.root)
        (host.config_dir / "config.json").write_text("{not json")
        self.assertEqual(_run(host, Config(), lambda _: runtime.host_vikunja_url()), "")


class ExposureGatingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_off_by_default(self):
        self.assertFalse(Config().vikunja.enabled)

    def test_a_configured_host_alone_exposes_nothing(self):
        """Having the skill and a token on the host is not consent."""
        host = _Host(self.root)
        argv = _argv(host, Config())
        joined = " ".join(argv)
        self.assertNotIn(str(host.config_dir), joined)
        self.assertNotIn(str(host.state_dir), joined)
        self.assertNotIn("VIKUNJA_URL", joined)
        self.assertEqual(_mount_for(argv, host.home / ".local/bin/vikunja"), [])

    def test_an_exported_url_alone_exposes_nothing(self):
        host = _Host(self.root)
        argv = _argv(host, Config(), env={"VIKUNJA_URL": "https://tasks.example.com"})
        self.assertNotIn("VIKUNJA_URL", " ".join(argv))

    def test_enabled_mounts_the_credentials_read_only(self):
        host = _Host(self.root)
        argv = _argv(host, _enabled())
        flags = _mount_for(argv, host.home / ".config/vikunja")
        self.assertEqual(len(flags), 1)
        self.assertIn(f"src={host.config_dir}", flags[0])
        self.assertIn("ro=true", flags[0])

    def test_enabled_mounts_the_import_state_writable(self):
        """A partially applied import is only resumable if its journal survives."""
        host = _Host(self.root)
        argv = _argv(host, _enabled())
        flags = _mount_for(argv, host.home / ".local/state/vikunja")
        self.assertEqual(len(flags), 1)
        self.assertIn(f"src={host.state_dir}", flags[0])
        self.assertNotIn("ro=true", flags[0])

    def test_enabled_puts_the_skill_cli_on_the_container_path(self):
        host = _Host(self.root)
        argv = _argv(host, _enabled())
        flags = _mount_for(argv, host.home / ".local/bin/vikunja")
        self.assertEqual(len(flags), 1)
        self.assertIn(f"src={host.entrypoint}", flags[0])
        self.assertIn("ro=true", flags[0])
        # The bind is pointless unless that directory is actually searched.
        self.assertIn(f"{runtime.HOST_HOME}/.local/bin", runtime.CONTAINER_RUNTIME_PATH)

    def test_the_import_state_source_is_created_by_agent_box(self):
        """So Podman does not invent one with an ownership nobody chose."""
        host = _Host(self.root)
        self.assertFalse(host.state_dir.exists())
        _argv(host, _enabled())
        self.assertTrue(host.state_dir.is_dir())

    def test_enabled_forwards_the_rewritten_url(self):
        host = _Host(self.root)
        self.assertIn(
            "--env=VIKUNJA_URL=http://host.containers.internal:3456",
            _argv(host, _enabled()))

    def test_the_token_value_never_appears_in_argv(self):
        """It travels as a read-only file, never as a process argument."""
        host = _Host(self.root)
        self.assertNotIn(TOKEN, " ".join(_argv(host, _enabled())))

    def test_a_missing_skill_omits_only_the_cli_bind(self):
        """The credentials still mount; a broken half must not fail the run."""
        host = _Host(self.root, skill=False)
        argv = _argv(host, _enabled())
        self.assertEqual(_mount_for(argv, host.home / ".local/bin/vikunja"), [])
        self.assertEqual(len(_mount_for(argv, host.home / ".config/vikunja")), 1)

    def test_no_url_forwards_no_variable(self):
        """Better an unset variable than an empty one that overrides the file."""
        host = _Host(self.root, url=None)
        self.assertFalse([a for a in _argv(host, _enabled()) if a.startswith("--env=VIKUNJA_URL")])


class WarningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _warnings(self, **kwargs):
        host = _Host(self.root, **kwargs)
        return _run(host, _enabled(), runtime.vikunja_warnings)

    def test_a_complete_installation_warns_about_nothing(self):
        self.assertEqual(self._warnings(), [])

    def test_disabled_never_warns(self):
        host = _Host(self.root, skill=False, token=False, url=None)
        self.assertEqual(_run(host, Config(), runtime.vikunja_warnings), [])

    def test_a_missing_skill_is_reported(self):
        self.assertTrue(any("skill CLI is missing" in w for w in self._warnings(skill=False)))

    def test_a_missing_token_is_reported(self):
        self.assertTrue(any("no API token" in w for w in self._warnings(token=False)))

    def test_a_missing_url_is_reported(self):
        self.assertTrue(any("no server URL" in w for w in self._warnings(url=None)))

    def test_a_warning_never_quotes_the_token(self):
        for warning in self._warnings(skill=False, url=None):
            self.assertNotIn(TOKEN, warning)


class ConfigSchemaTests(unittest.TestCase):
    def test_round_trips(self):
        config = _enabled()
        self.assertTrue(Config.from_dict(config.to_dict()).vikunja.enabled)

    def test_a_config_file_predating_the_section_loads_with_it_off(self):
        self.assertFalse(Config.from_dict({"version": 1, "github": {}}).vikunja.enabled)


if __name__ == "__main__":
    unittest.main()
