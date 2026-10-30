"""Container HTTPS auto-push must reuse the existing `github.gh_token`
mechanism through an actually usable git credential path -- forwarding
GH_TOKEN into the container environment alone does nothing, because plain
git never consults it on its own. These tests check the generated git
config directly and, mechanically, that git will actually invoke the
intended credential helper program with the intended arguments -- without
ever performing a real GitHub network call.
"""
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_box import runtime
from agent_box.config import Config
from agent_box.workspace import Workspace


def _unprompted_env() -> dict[str, str]:
    """The suite's environment with every way for git to ask the user closed.

    Run from an interactive terminal, ``git credential fill`` with nothing to
    fill from asks for a username on /dev/tty and waits.
    """
    env = {name: value for name, value in os.environ.items() if name not in ("GIT_ASKPASS", "SSH_ASKPASS")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _git_config_get_all(config_file: Path, key: str) -> list[str]:
    result = subprocess.run(
        ["git", "config", "--file", str(config_file), "--get-all", key],
        capture_output=True, text=True, check=False,
    )
    return result.stdout.strip().splitlines() if result.returncode == 0 else []


class SeedGitConfigCredentialHelperTests(unittest.TestCase):
    def test_whole_home_mount_cannot_redirect_git_reads_or_writes_into_host_configs(self):
        """Standard file locations must preserve the box snapshot even with a host dotfile visible."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            host = root / "host"
            host_xdg = host / ".config/git/config"
            host_xdg.parent.mkdir(parents=True)
            host_xdg.write_text("[user]\n\tname = host-xdg\n")
            host_dotfile = host / ".gitconfig"
            host_dotfile.write_text("[user]\n\tname = host-dotfile\n")
            box = root / "box"
            (box / ".config").mkdir(parents=True)
            snapshot = root / "snapshot"
            settings = Config()
            settings.credentials.git.identity = False
            settings.credentials.github.token = True
            settings.box.persistent_cache = True
            runtime.seed_git_config(snapshot, settings)
            runtime._set_git_config(snapshot / "config", "user.name", "snapshot-user")
            # Model the visible home bind and its private XDG-directory overlay.
            (box / ".gitconfig").symlink_to(host_dotfile)
            (box / ".config/git").symlink_to(snapshot, target_is_directory=True)
            workspace = Workspace(box, box)
            with mock.patch.object(runtime, "HOST_HOME", box):
                flags = runtime.container_lifetime_args(workspace, [], runtime.PACKAGE_ROOT, settings)
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith("GIT_CONFIG") and key != "XDG_CONFIG_HOME"}
            env.update(HOME=str(box), GIT_CONFIG_NOSYSTEM="1")
            for flag in flags:
                if flag.startswith("--env=GIT_CONFIG_GLOBAL="):
                    env["GIT_CONFIG_GLOBAL"] = flag.split("=", 2)[2]
            name = subprocess.check_output(["git", "config", "--global", "--get", "user.name"],
                                           env=env, text=True).strip()
            self.assertEqual(name, "snapshot-user")
            subprocess.run(["git", "config", "--global", "user.name", "edited-snapshot"], env=env, check=True)
            self.assertEqual(_git_config_get_all(snapshot / "config", "user.name"), ["edited-snapshot"])
            self.assertEqual(_git_config_get_all(host_dotfile, "user.name"), ["host-dotfile"])
            self.assertEqual(_git_config_get_all(host_xdg, "user.name"), ["host-xdg"])

    def test_enabled_github_transport_uses_https_without_changing_repository_urls(self):
        """The token can authenticate HTTPS; copied SSH URLs cannot use it."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "git"
            repo = Path(tmp) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            config = Config()
            config.credentials.git.identity = False
            cases = {
                "git@github.com:owner/repo.git": "https://github.com/owner/repo.git",
                "ssh://git@github.com/owner/repo.git": "https://github.com/owner/repo.git",
                "git@elsewhere.example:owner/repo.git": "git@elsewhere.example:owner/repo.git",
                "git@github.com.evil:owner/repo.git": "git@github.com.evil:owner/repo.git",
            }
            for enabled in (False, True):
                config.credentials.github.token = enabled
                runtime.seed_git_config(target, config)
                env = dict(os.environ, GIT_CONFIG_GLOBAL=str(target / "config"), GIT_CONFIG_NOSYSTEM="1")
                for original, rewritten in cases.items():
                    with self.subTest(enabled=enabled, original=original):
                        subprocess.run(["git", "config", "remote.origin.url", original], cwd=repo, check=True)
                        result = subprocess.check_output(["git", "remote", "get-url", "--push", "origin"],
                                                         cwd=repo, env=env, text=True).strip()
                        self.assertEqual(result, rewritten if enabled else original)
                        stored = subprocess.check_output(["git", "config", "--local", "remote.origin.url"],
                                                         cwd=repo, text=True).strip()
                        self.assertEqual(stored, original)

    def test_standard_global_lookup_finds_identity_and_github_transport_without_overrides(self):
        """Git must consume the snapshot through its own standard XDG lookup."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            target = home / ".config/git"
            settings = Config()
            settings.credentials.git.identity = False
            settings.credentials.github.token = True
            runtime.seed_git_config(target, settings)
            runtime._set_git_config(target / "config", "user.name", "standard-user")
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith("GIT_CONFIG") and key != "XDG_CONFIG_HOME"}
            env.update(HOME=str(home), GIT_CONFIG_NOSYSTEM="1")
            name = subprocess.check_output(["git", "config", "--global", "--get", "user.name"],
                                           env=env, text=True).strip()
            self.assertEqual(name, "standard-user")
            helper = subprocess.check_output(
                ["git", "config", "--global", "--get-all", "credential.https://github.com.helper"],
                env=env, text=True).splitlines()
            self.assertEqual(helper, [runtime.GH_CREDENTIAL_HELPER])
            subprocess.run(["git", "config", "--global", "user.name", "changed"], env=env, check=True)
            self.assertEqual(_git_config_get_all(target / "config", "user.name"), ["changed"])

    def test_gh_token_enabled_configures_credential_helper(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "git"
            config = Config()
            config.credentials.github.token = True
            runtime.seed_git_config(target, config)
            values = _git_config_get_all(target / "config", "credential.https://github.com.helper")
            self.assertEqual(values, ["!gh auth git-credential"])

    def test_gh_token_disabled_configures_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "git"
            config = Config()
            config.credentials.github.token = False
            runtime.seed_git_config(target, config)
            config_file = target / "config"
            if config_file.exists():
                values = _git_config_get_all(config_file, "credential.https://github.com.helper")
                self.assertEqual(values, [])

    def test_helper_is_scoped_to_github_com_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "git"
            config = Config()
            config.credentials.github.token = True
            runtime.seed_git_config(target, config)
            content = (target / "config").read_text()
            self.assertIn("https://github.com", content)
            self.assertNotIn("[credential]\n", content)  # not set globally/unscoped

    def test_replaces_multiple_pre_existing_helper_values_deterministically(self):
        """A copied-in full host gitconfig may already define this key more
        than once (a common gh-CLI-produced pattern: an empty reset value
        followed by the real helper) -- a plain `git config <key> <value>`
        refuses to overwrite that ambiguously; agent-box's write must not."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "git"
            target.mkdir(parents=True)
            config_file = target / "config"
            config_file.write_text(
                '[credential "https://github.com"]\n'
                "\thelper =\n"
                "\thelper = !/usr/bin/gh auth git-credential\n"
            )
            config = Config()
            config.credentials.github.token = True
            with mock.patch.object(runtime, "seed_state_dir"):  # keep our hand-seeded file as-is
                runtime.seed_git_config(target, config)
            values = _git_config_get_all(config_file, "credential.https://github.com.helper")
            self.assertEqual(values, ["!gh auth git-credential"])

    def test_full_config_path_still_configures_helper(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "git"
            config = Config()
            config.credentials.git.full_config = True
            config.credentials.github.token = True
            with mock.patch.object(runtime, "HOST_HOME", Path(tmp)):
                (Path(tmp) / ".gitconfig").write_text("[user]\n\tname = Someone\n")
                runtime.seed_git_config(target, config)
            values = _git_config_get_all(target / "config", "credential.https://github.com.helper")
            self.assertEqual(values, ["!gh auth git-credential"])


class CredentialHelperInvocationTests(unittest.TestCase):
    """Prove mechanically that git will actually invoke the configured
    helper program with the arguments the `!gh auth git-credential`
    convention expects -- without any real network access or real `gh`."""

    def test_git_credential_fill_invokes_configured_helper(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            target = tmp_path / "git"
            config = Config()
            config.credentials.github.token = True
            runtime.seed_git_config(target, config)
            config_file = target / "config"

            # A fake `gh` on PATH standing in for the real GitHub CLI. Real
            # `gh auth git-credential get` reads GH_TOKEN from its own
            # environment and prints credential lines on stdout; this fake
            # does the same minimal thing so the test proves the *wiring*
            # (git -> configured helper -> program on PATH -> credentials
            # back to git), not gh's own internals.
            fake_bin = tmp_path / "fakebin"
            fake_bin.mkdir()
            fake_gh = fake_bin / "gh"
            fake_gh.write_text(
                "#!/bin/sh\n"
                'if [ "$1 $2 $3" = "auth git-credential get" ]; then\n'
                '  echo "username=x-access-token"\n'
                '  echo "password=token-from-env:${GH_TOKEN}"\n'
                "fi\n"
            )
            fake_gh.chmod(fake_gh.stat().st_mode | stat.S_IEXEC)

            env = _unprompted_env()
            env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
            env["GH_TOKEN"] = "sentinel-token-value"
            env["GIT_CONFIG_GLOBAL"] = str(config_file)
            env["GIT_CONFIG_NOSYSTEM"] = "1"
            env["HOME"] = str(tmp_path)  # keep the real user gitconfig out of this

            result = subprocess.run(
                ["git", "credential", "fill"],
                input="protocol=https\nhost=github.com\n\n",
                capture_output=True, text=True, env=env, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("username=x-access-token", result.stdout)
            self.assertIn("password=token-from-env:sentinel-token-value", result.stdout)

    def test_gh_token_disabled_leaves_no_helper_to_invoke(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            target = tmp_path / "git"
            config = Config()
            config.credentials.github.token = False
            runtime.seed_git_config(target, config)
            config_file = target / "config"

            env = _unprompted_env()
            env["GIT_CONFIG_GLOBAL"] = str(config_file)
            env["GIT_CONFIG_NOSYSTEM"] = "1"
            env["HOME"] = str(tmp_path)

            result = subprocess.run(
                ["git", "credential", "fill"],
                input="protocol=https\nhost=github.com\n\n",
                capture_output=True, text=True, env=env, timeout=5, check=False,
            )
            # With no helper configured and prompts closed, git has nothing to
            # fill from and nothing to ask -- it must not have invoked any
            # credential helper that hands back our sentinel value.
            self.assertNotIn("x-access-token", result.stdout)


if __name__ == "__main__":
    unittest.main()
