"""The Vikunja integration: what agent-box owns, what reaches the container.

Three questions this file exists to answer.

* **Who holds the settings?** agent-box does. The server URL and the API token
  live in its own config file, and the container sees a *rendered* pair of
  files derived from them -- never agent-box's config, and never a
  configuration file the user had to create on the host first. An integration
  that can be switched on but then reports a missing file is not an
  integration.
* **Does the credential reach the container only because the switch is on?**
  Asserted against the generated podman argv, not against the helper
  functions, because argv is what actually determines exposure.
* **Does the address the container receives point at the host's server?** A
  loopback URL that is merely copied verbatim addresses the container itself,
  which fails with no useful diagnostic. So does the host's own LAN address or
  a DNS name pointing at it, because rootless Podman gives the container the
  host's addresses.

Locality is stubbed rather than measured: whether a name or address belongs
to this machine is a property of whoever runs the suite, and the behaviour
under test is what agent-box does with the answer.
"""
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_box import runtime
from agent_box.config import Config
from agent_box.workspace import Workspace

TOKEN = "tk_secret_vikunja_token"
URL = "http://127.0.0.1:3456"


class _Host:
    """A temporary stand-in for the host agent-box is running on.

    Deliberately *empty* of Vikunja configuration: the point of the current
    design is that nothing has to exist here beforehand. ``skill`` controls
    only whether agent-box has installed its shipped skill yet.
    """

    def __init__(self, root: Path, *, skill: bool = True):
        self.home = root / "home"
        self.state = self.home / "agent-box-state"
        self.credentials = self.state / "runtime/vikunja"
        self.import_state = self.home / ".local/state/vikunja"
        self.skills = self.home / ".agents/skills"
        self.entrypoint = self.skills / "vikunja/scripts/vikunja.py"
        self.home.mkdir(parents=True)
        if skill:
            self.entrypoint.parent.mkdir(parents=True)
            self.entrypoint.write_text("#!/usr/bin/env python3\n")

    def patches(self):
        return (
            mock.patch.object(runtime, "HOST_HOME", self.home),
            mock.patch.object(runtime, "AGENTS_DIR", self.home / ".agents"),
            mock.patch.object(runtime, "AGENT_SKILLS_DIR", self.skills),
            mock.patch.object(runtime, "VIKUNJA_SKILL_ENTRYPOINT", self.entrypoint),
            mock.patch.object(runtime, "VIKUNJA_CREDENTIALS_DIR", self.credentials),
            mock.patch.object(runtime, "VIKUNJA_STATE_DIR", self.import_state),
            mock.patch.object(
                runtime, "CONTAINER_VIKUNJA_CONFIG_DIR", self.home / ".config/vikunja"),
            mock.patch.object(
                runtime, "CONTAINER_VIKUNJA_STATE_DIR", self.home / ".local/state/vikunja"),
            mock.patch.object(runtime, "CONTAINER_VIKUNJA_BIN", self.home / ".local/bin/vikunja"),
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
    """Full podman argv for one run against this fake host."""
    project = host.home / "project"
    project.mkdir(parents=True, exist_ok=True)

    def build(cfg):
        with mock.patch.object(runtime, "STATE_ROOT", host.state), \
             mock.patch.object(runtime, "BUILD_STATE", host.state / "build"), \
             mock.patch.object(runtime, "NOTIFY_STATE_ROOT", host.state / "notify"), \
             mock.patch.object(
                 runtime, "NOTIFY_CREDENTIALS", host.state / "runtime/notify.json"), \
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


def _enabled(url: str = URL, token: str = TOKEN):
    config = Config()
    config.vikunja.enabled = True
    config.vikunja.url = url
    config.vikunja.token = token
    return config


def _locality(local: bool):
    """Pin :func:`runtime.is_host_local` so the suite never consults DNS."""
    return mock.patch.object(runtime, "is_host_local", lambda host: local)


class ContainerUrlTests(unittest.TestCase):
    """Which addresses get rewritten, and which are left exactly alone."""

    def test_loopback_ip_becomes_the_container_host_alias(self):
        self.assertEqual(
            runtime.container_vikunja_url("http://127.0.0.1:3456"),
            "http://host.containers.internal:3456")

    def test_localhost_becomes_the_container_host_alias(self):
        self.assertEqual(
            runtime.container_vikunja_url("http://localhost:3456"),
            "http://host.containers.internal:3456")

    def test_ipv6_loopback_becomes_the_container_host_alias(self):
        self.assertEqual(
            runtime.container_vikunja_url("http://[::1]:3456"),
            "http://host.containers.internal:3456")

    def test_scheme_port_and_path_survive_the_rewrite(self):
        self.assertEqual(
            runtime.container_vikunja_url("https://127.0.0.1:8443/vikunja/api"),
            "https://host.containers.internal:8443/vikunja/api")

    def test_an_absent_port_stays_absent(self):
        self.assertEqual(
            runtime.container_vikunja_url("http://localhost"),
            "http://host.containers.internal")

    def test_a_host_local_ip_literal_is_rewritten(self):
        with _locality(True):
            self.assertEqual(
                runtime.container_vikunja_url("http://192.168.1.203:3456"),
                "http://host.containers.internal:3456")

    def test_a_dns_name_keeps_its_url_even_when_it_is_this_machine(self):
        """TLS and virtual-host routing depend on the name surviving."""
        with _locality(True):
            self.assertEqual(
                runtime.container_vikunja_url("https://pm.example.org/api"),
                "https://pm.example.org/api")

    def test_a_remote_server_is_returned_unchanged(self):
        with _locality(False):
            for url in ("https://tasks.example.com", "http://10.1.2.3:3456"):
                with self.subTest(url=url):
                    self.assertEqual(runtime.container_vikunja_url(url), url)

    def test_a_malformed_authority_is_handed_through(self):
        self.assertEqual(
            runtime.container_vikunja_url("http://127.0.0.1:not-a-port"),
            "http://127.0.0.1:not-a-port")

    def test_an_empty_url_stays_empty(self):
        self.assertEqual(runtime.container_vikunja_url("   "), "")


class HostAliasTests(unittest.TestCase):
    def test_a_host_local_dns_name_is_mapped(self):
        with _locality(True):
            self.assertEqual(
                runtime.vikunja_host_alias("http://pm.example.org:3456"), "pm.example.org")

    def test_a_remote_dns_name_is_not_mapped(self):
        with _locality(False):
            self.assertEqual(runtime.vikunja_host_alias("https://tasks.example.com"), "")

    def test_addresses_and_loopback_names_are_not_mapped(self):
        """Those are the rewrite's job, not the host mapping's."""
        with _locality(True):
            for url in ("http://127.0.0.1:3456", "http://localhost", "http://[::1]:3456"):
                with self.subTest(url=url):
                    self.assertEqual(runtime.vikunja_host_alias(url), "")

    def test_unusable_input_maps_nothing(self):
        self.assertEqual(runtime.vikunja_host_alias("   "), "")
        self.assertEqual(runtime.vikunja_host_alias("http://pm.example.org:not-a-port"), "")


class CredentialRenderingTests(unittest.TestCase):
    """agent-box owns the settings; the container gets a derived copy."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _render(self, config):
        host = _Host(self.root)
        _run(host, config, runtime.sync_vikunja_credentials)
        return host

    def test_the_rendered_url_is_the_container_view(self):
        """The value the user configured stays the host's; only the copy moves."""
        config = _enabled()
        host = self._render(config)
        rendered = json.loads((host.credentials / "config.json").read_text())
        self.assertEqual(rendered["url"], "http://host.containers.internal:3456")
        self.assertEqual(config.vikunja.url, URL)

    def test_the_token_is_written_where_the_skill_reads_it(self):
        host = self._render(_enabled())
        self.assertEqual((host.credentials / "token").read_text().strip(), TOKEN)

    def test_the_rendered_files_are_private(self):
        host = self._render(_enabled())
        for name in ("config.json", "token"):
            with self.subTest(name=name):
                mode = stat.S_IMODE((host.credentials / name).stat().st_mode)
                self.assertEqual(mode, 0o600)

    def test_disabling_removes_the_credentials(self):
        """The credential stops being reachable, not merely ignored."""
        host = self._render(_enabled())
        self.assertTrue((host.credentials / "token").is_file())
        _run(host, Config(), runtime.sync_vikunja_credentials)
        self.assertFalse(host.credentials.exists())

    def test_an_incomplete_configuration_renders_nothing(self):
        host = self._render(_enabled(token=""))
        self.assertFalse(host.credentials.exists())


class ExposureGatingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_off_by_default(self):
        self.assertFalse(Config().vikunja.enabled)

    def test_disabled_exposes_nothing(self):
        host = _Host(self.root)
        argv = _argv(host, Config())
        joined = " ".join(argv)
        self.assertNotIn(str(host.credentials), joined)
        self.assertNotIn(str(host.import_state), joined)
        self.assertEqual(_mount_for(argv, host.home / ".local/bin/vikunja"), [])

    def test_an_exported_url_alone_exposes_nothing(self):
        """The environment is not a configuration channel for this."""
        host = _Host(self.root)
        argv = _argv(host, Config(), env={"VIKUNJA_URL": "https://tasks.example.com"})
        self.assertNotIn("VIKUNJA_URL", " ".join(argv))

    def test_enabled_mounts_the_rendered_credentials_read_only(self):
        host = _Host(self.root)
        argv = _argv(host, _enabled())
        flags = _mount_for(argv, host.home / ".config/vikunja")
        self.assertEqual(len(flags), 1)
        self.assertIn(f"src={host.credentials}", flags[0])
        self.assertIn("ro=true", flags[0])

    def test_enabled_mounts_the_import_state_writable(self):
        """A partially applied import is only resumable if its journal survives."""
        host = _Host(self.root)
        argv = _argv(host, _enabled())
        flags = _mount_for(argv, host.home / ".local/state/vikunja")
        self.assertEqual(len(flags), 1)
        self.assertIn(f"src={host.import_state}", flags[0])
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

    def test_the_sources_are_created_by_agent_box(self):
        """So Podman does not invent them with an ownership nobody chose."""
        host = _Host(self.root)
        self.assertFalse(host.import_state.exists())
        _argv(host, _enabled())
        self.assertTrue(host.import_state.is_dir())
        self.assertTrue(host.credentials.is_dir())

    def test_the_url_travels_in_the_file_not_the_environment(self):
        """One source for the address, so it cannot disagree with the token."""
        host = _Host(self.root)
        argv = _argv(host, _enabled())
        self.assertFalse([a for a in argv if a.startswith("--env=VIKUNJA_URL")])
        rendered = json.loads((host.credentials / "config.json").read_text())
        self.assertEqual(rendered["url"], "http://host.containers.internal:3456")

    def test_a_host_local_name_keeps_its_url_and_is_redirected(self):
        """The URL keeps the name the server was configured for."""
        host = _Host(self.root)
        with _locality(True):
            argv = _argv(host, _enabled(url="http://pm.example.org:3456"))
        self.assertIn(f"--add-host=pm.example.org:{runtime.CONTAINER_HOST_GATEWAY}", argv)
        rendered = json.loads((host.credentials / "config.json").read_text())
        self.assertEqual(rendered["url"], "http://pm.example.org:3456")

    def test_a_remote_server_is_never_redirected(self):
        host = _Host(self.root)
        with _locality(False):
            argv = _argv(host, _enabled(url="https://tasks.example.com"))
        self.assertEqual([a for a in argv if a.startswith("--add-host=")], [])

    def test_disabled_maps_no_host_even_when_the_name_is_local(self):
        host = _Host(self.root)
        with _locality(True):
            argv = _argv(host, Config())
        self.assertEqual([a for a in argv if a.startswith("--add-host=")], [])

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


class WarningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _warnings(self, config, *, skill: bool = True):
        return _run(_Host(self.root, skill=skill), config, runtime.vikunja_warnings)

    def test_a_complete_installation_warns_about_nothing(self):
        self.assertEqual(self._warnings(_enabled()), [])

    def test_disabled_never_warns(self):
        self.assertEqual(self._warnings(Config(), skill=False), [])

    def test_an_uninstalled_skill_is_reported(self):
        self.assertTrue(any(
            "skill is not installed" in w for w in self._warnings(_enabled(), skill=False)))

    def test_a_missing_token_is_reported(self):
        self.assertTrue(any("no API token" in w for w in self._warnings(_enabled(token=""))))

    def test_a_missing_url_is_reported(self):
        self.assertTrue(any("no server URL" in w for w in self._warnings(_enabled(url=""))))

    def test_a_warning_never_quotes_the_token(self):
        for warning in self._warnings(_enabled(url=""), skill=False):
            self.assertNotIn(TOKEN, warning)


class ConfigSchemaTests(unittest.TestCase):
    def test_round_trips(self):
        loaded = Config.from_dict(_enabled().to_dict()).vikunja
        self.assertTrue(loaded.enabled)
        self.assertEqual(loaded.url, URL)
        self.assertEqual(loaded.token, TOKEN)

    def test_a_config_file_without_the_section_loads_with_it_off(self):
        self.assertFalse(Config.from_dict({"version": 1, "github": {}}).vikunja.enabled)

    def test_usable_requires_all_three(self):
        self.assertTrue(_enabled().vikunja.usable)
        self.assertFalse(_enabled(url="").vikunja.usable)
        self.assertFalse(_enabled(token="").vikunja.usable)
        disabled = _enabled()
        disabled.vikunja.enabled = False
        self.assertFalse(disabled.vikunja.usable)


if __name__ == "__main__":
    unittest.main()
