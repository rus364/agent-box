"""The image must not be buildable with an invented fallback identity.

An earlier Containerfile defaulted the identity args to
``agent``/``1000``/``1000``/``/home/agent``. A plain ``podman build`` (a
manual build, a CI job, a copy-paste from the README) then produced an image
whose user, UID/GID and ``$HOME`` matched nobody: bind mounts landed at paths
the container user could not own, ``$HOME`` pointed somewhere nothing was
mounted, and the mismatch only surfaced later as confusing runtime failures.
The build must fail immediately and say what to pass instead.
"""
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

CONTAINERFILE = Path(__file__).resolve().parent.parent / "agent_box/Containerfile"
REQUIRED_ARGS = ("USER_NAME", "USER_UID", "USER_GID", "USER_HOME")


def containerfile_text() -> str:
    return CONTAINERFILE.read_text()


class IdentityArgsTests(unittest.TestCase):
    maxDiff = 2000

    def test_identity_args_have_no_defaults(self):
        text = containerfile_text()
        for name in REQUIRED_ARGS:
            declarations = re.findall(rf"^ARG {name}(=.*)?$", text, re.MULTILINE)
            self.assertEqual(
                declarations, [""],
                msg=f"ARG {name} must be declared exactly once, without a default value",
            )

    def test_no_hardcoded_agent_identity_remains(self):
        instructions = "\n".join(
            line for line in containerfile_text().splitlines() if not line.lstrip().startswith("#")
        )
        self.assertNotIn("/home/agent", instructions)
        self.assertNotIn("USER_NAME=agent", instructions)
        self.assertNotIn("USER_UID=1000", instructions)
        self.assertNotIn("USER_GID=1000", instructions)

    def test_build_args_are_validated_before_the_user_is_created(self):
        text = containerfile_text()
        validation = text.index("missing required --build-arg")
        creation = text.index("existing_group=")
        self.assertLess(validation, creation, "validation must run before useradd/usermod")

    def test_runtime_command_still_passes_every_required_arg(self):
        from agent_box import runtime
        source = Path(runtime.__file__).read_text()
        for name in REQUIRED_ARGS:
            self.assertIn(f'f"{name}={{identity.', source)


class SessionToolingTests(unittest.TestCase):
    def test_image_ships_tmux(self):
        """Detachable sessions run under a tmux server inside the box."""
        text = containerfile_text()
        self.assertRegex(text, r"(?m)^\s+tmux \\$")
        self.assertRegex(text, r"(?m)^\s+tmux -V$")


class ToolGroupTests(unittest.TestCase):
    def test_every_configured_group_is_built_and_verified_with_its_default(self):
        """A group the configuration offers but the image ignores would build
        nothing while its label claims it, and a different Containerfile default
        would make a plain ``podman build`` disagree with ``agent-box build``."""
        from agent_box.config import TOOL_GROUPS
        text = containerfile_text()
        checks = text[text.index("# Sanity checks"):]
        for group, default in TOOL_GROUPS.items():
            flag = f"TOOLS_{group.upper()}"
            with self.subTest(group=group):
                self.assertEqual(re.findall(rf"^ARG {flag}=(.*)$", text, re.MULTILINE),
                                 ["1" if default else "0"])
                self.assertIn(f'if [ "${{{flag}}}" != "1" ]; then exit 0; fi;', checks)


class CodexBwrapCapabilitiesTests(unittest.TestCase):
    """Exercise the image step without changing installed binaries or xattrs."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        text = containerfile_text()
        layer = text[text.index("# An empty file-capability set"):]
        self.script = layer[layer.index("RUN ") + 4:layer.index("\n\n#")].replace("\\\n", "")
        for name, body in {
            "npm": 'printf "%s\\n" "$PROBE_ROOT"',
            "setcap": 'printf "%s\\n" "$@" > "$PROBE_ROOT/calls"',
            "getcap": 'printf "%s =\\n" "$1"',
        }.items():
            executable = self.root / name
            executable.write_text("#!/bin/sh\n" + body + "\n")
            executable.chmod(0o755)

    def run_layer(self, enabled):
        return subprocess.run(["/bin/sh", "-c", self.script], capture_output=True, text=True,
                              env={"PATH": str(self.root), "PROBE_ROOT": str(self.root),
                                   "TOOLS_PODMAN": "1" if enabled else "0"})

    def binary(self, arch):
        binary = self.root / f"@openai/codex/node_modules/@openai/codex-{arch}/vendor/triple/codex-resources/bwrap"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"bundled binary")
        binary.chmod(0o755)
        return binary

    def test_disabled_group_never_resolves_or_changes_codex(self):
        """No Podman must also mean no dependency on capability tooling."""
        for executable in self.root.iterdir():
            executable.unlink()
        result = self.run_layer(False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_only_bwrap_receives_an_empty_capability_set(self):
        """The Codex parent must retain capabilities for ordinary Podman commands."""
        binary = self.binary("linux-x64")
        result = self.run_layer(True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "calls").read_text().splitlines(), ["=", str(binary)])
        self.assertEqual(binary.read_bytes(), b"bundled binary")

    def test_missing_or_ambiguous_bwrap_fails_before_changing_files(self):
        """A changed npm layout must fail the build instead of shipping the conflict."""
        self.assertNotEqual(self.run_layer(True).returncode, 0)
        self.binary("linux-x64")
        self.binary("linux-arm64")
        self.assertNotEqual(self.run_layer(True).returncode, 0)
        self.assertFalse((self.root / "calls").exists())


class GitHubCliTests(unittest.TestCase):
    """`gh` comes from GitHub's apt repository, not Ubuntu's archive.

    Ubuntu 24.04 froze gh at 2.45.0. Tools that *drive* `gh` rather than run
    it break on that: Claude Code's /install-github-app probes
    `gh auth status -a`, a flag that landed in 2.57.0, and calls a working
    login "GitHub CLI not authenticated" when the flag is absent. Falling back
    to the distribution package would silently reintroduce that.
    """

    def gh_layer(self) -> str:
        text = containerfile_text()
        layer = text[text.index("# GitHub CLI"):]
        return layer[:layer.index("# Analysis tooling")]

    def test_gh_is_not_taken_from_the_ubuntu_archive(self):
        text = containerfile_text()
        base = text[text.index("# Base OS / development packages"):text.index("# GitHub CLI")]
        self.assertNotRegex(base, r"(?m)^\s+gh \\$")

    def test_gh_comes_from_githubs_signed_repository(self):
        """Signed by GitHub's own keyring: an unsigned or `trusted=yes` entry
        would let anything on that host's path into the base image."""
        layer = self.gh_layer()
        self.assertIn("https://cli.github.com/packages", layer)
        self.assertIn("signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg", layer)
        self.assertNotIn("trusted=yes", layer)

    def test_the_build_proves_the_active_flag_exists(self):
        """A version number in a comment is not a check. Assert the capability
        the tooling actually needs, so a repository that regresses fails the
        build instead of the user's first /install-github-app."""
        self.assertIn("gh auth status --help | grep -q -- '--active'", self.gh_layer())


class BrowserToolingTests(unittest.TestCase):
    """Playwright MCP and its Chromium must be *in the image*.

    The point of baking them in is that no agent session ever pays for a
    lazy browser download, and that the MCP server and the browser binaries
    come from one pinned, mutually compatible pair.
    """

    def test_playwright_mcp_is_installed_at_a_pinned_version(self):
        text = containerfile_text()
        self.assertRegex(text, r"(?m)^ARG PLAYWRIGHT_MCP_VERSION=\d+\.\d+\.\d+$")
        self.assertIn('npm install --global "@playwright/mcp@${PLAYWRIGHT_MCP_VERSION}"', text)

    def test_chromium_is_downloaded_at_build_time(self):
        """Not lazily on first use: a `playwright install` inside an agent
        session would download ~150MB before the first navigation."""
        self.assertIn("install --with-deps chromium", containerfile_text())

    def test_browser_version_follows_the_playwright_that_mcp_depends_on(self):
        """Installing a second, independent `playwright` would let the MCP
        server and the downloaded browser revision drift apart."""
        text = containerfile_text()
        self.assertIn('require.resolve("playwright/package.json"', text)
        self.assertIn('paths: ["/usr/lib/node_modules/@playwright/mcp"]', text)

    def test_browsers_live_outside_the_bind_mounted_cache(self):
        """agent-box gives the container its own XDG_CACHE_HOME bind mount,
        which would otherwise hide ~/.cache/ms-playwright."""
        text = containerfile_text()
        self.assertIn("ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright", text)

    def test_unsupported_architectures_fail_the_build_with_an_explanation(self):
        text = containerfile_text()
        browser_layer = text[text.index("Playwright MCP + headless Chromium"):]
        browser_layer = browser_layer[:browser_layer.index("# Bun")]
        self.assertIn("amd64|arm64", browser_layer)
        self.assertIn("Playwright publishes no Chromium build", browser_layer)
        self.assertIn("exit 1", browser_layer)

    def test_sanity_checks_cover_the_mcp_server_and_the_browser(self):
        """Verified, but only when the browser group was actually built.

        The check lives in the group's own conditional step: running it
        unconditionally would fail the build for everyone who left the group
        out, which is the whole point of being able to leave it out.
        """
        text = containerfile_text()
        checks = text[text.index("# Sanity checks"):]
        browser_check = checks[checks.index('if [ "${TOOLS_BROWSER}" != "1" ]'):]
        self.assertIn("playwright-mcp --version;", browser_check)
        self.assertIn("test -d /opt/ms-playwright;", browser_check)
        # An executable check is not enough: prove a browser really starts.
        self.assertIn("agent-box-browser-smoke", browser_check)

    def test_the_smoke_test_is_offline_and_bounded(self):
        text = containerfile_text()
        smoke = text[text.index("browser smoke test timed out"):]
        smoke = smoke[:smoke.index("chmod 0755 /usr/local/bin/agent-box-browser-smoke")]
        self.assertIn("data:text/html", smoke)
        self.assertNotIn("http://", smoke)
        self.assertNotIn("https://", smoke)
        self.assertIn("process.exit(1)", smoke)

    def test_the_container_user_owns_its_own_config_directory(self):
        """Podman creates missing bind-mount parents root-owned, which leaves
        $HOME/.config unwritable and kills Chromium at startup with
        "chrome_crashpad_handler: --database is required"."""
        text = containerfile_text()
        self.assertIn(
            'install -d -o "${USER_UID}" -g "${USER_GID}" -m 0755 "${USER_HOME}/.config"', text)

    def test_no_capability_is_added_for_the_browser(self):
        """Browser tooling must not acquire capabilities granted to another opt-in group."""
        text = containerfile_text()
        text = text[text.index("# Playwright MCP + headless Chromium"):text.index("# Bun")]
        self.assertNotIn("--cap-add", text)
        self.assertNotIn("setcap", text)
        self.assertNotIn("chrome-sandbox", text)

    def test_no_host_browser_profile_is_copied_or_mounted(self):
        instructions = containerfile_text()
        for host_profile in (".config/google-chrome", ".config/chromium", ".mozilla", "Cookies"):
            self.assertNotIn(host_profile, instructions)

    def test_browser_layer_precedes_the_frequently_changing_agent_packages(self):
        """Keeping the ~150MB browser download above the agent install means
        reordering or adding tools below it does not re-fetch Chromium."""
        text = containerfile_text()
        self.assertLess(
            text.index("@playwright/mcp@${PLAYWRIGHT_MCP_VERSION}"),
            text.index("@anthropic-ai/claude-code@latest"),
        )

    def test_update_keeps_pinned_downloads_above_refresh(self):
        """Refreshing agents must not download unchanged Chromium or jscpd."""
        text = containerfile_text()
        refresh = text.index("ARG TOOLS_REFRESH=")
        for pinned in ("install --with-deps chromium", "jscpd@5.0.16"):
            self.assertLess(text.index(pinned), refresh)
        self.assertLess(text.index("apt-get install -y --no-install-recommends nodejs"),
                        text.index("install --with-deps chromium"))
        self.assertIn("apt-get install -y --no-install-recommends nodejs", text[refresh:])

    def test_package_download_cache_survives_layer_refresh(self):
        """Build caches must survive installs without entering image layers."""
        text = containerfile_text()
        self.assertNotIn("npm cache clean", text)
        self.assertNotIn("apt-get clean", text)
        instructions = re.split(r"\n(?=[A-Z]+ )", text)
        for instruction in instructions:
            if "npm install --global" in instruction:
                self.assertIn("type=cache,id=agent-box-npm,target=/root/.npm", instruction)
            if "apt-get update;" in instruction or "install --with-deps chromium" in instruction:
                self.assertIn("type=cache,id=agent-box-apt,target=/var/cache/apt,sharing=locked", instruction)
        self.assertIn("rm -f /etc/apt/apt.conf.d/docker-clean", text)

    def test_runtime_agrees_with_the_image_on_names(self):
        from agent_box import runtime
        text = containerfile_text()
        self.assertIn(runtime.BROWSER_MCP_EXECUTABLE, text)
        self.assertIn("agent-box-browser-smoke", runtime.DOCTOR_SCRIPT)


class ImageSurfaceTests(unittest.TestCase):
    def test_update_installs_current_npm_over_the_bundled_one(self):
        """Node.js 22 bundles an npm whose own dependencies lag behind their
        fixes; upgrading npm before the Node.js refresh would be undone by it,
        and upgrading it after the agents would leave them installed by the
        old one."""
        text = containerfile_text()
        upgrade = text.index("npm install --global npm@latest")
        self.assertLess(text.index("ARG TOOLS_REFRESH="), upgrade)
        self.assertLess(text.rindex("apt-get install -y --no-install-recommends nodejs"), upgrade)
        self.assertLess(upgrade, text.index("@openai/codex@latest"))

    def test_current_npm_still_runs_claude_codes_postinstall(self):
        """Current npm blocks install scripts that are not allowed by name;
        Claude Code's postinstall places its native binary, and without it
        `claude --version` fails the build."""
        install = next(i for i in re.split(r"\n(?=[A-Z]+ )", containerfile_text())
                       if "@anthropic-ai/claude-code@latest" in i)
        self.assertIn("--allow-scripts=@anthropic-ai/claude-code", install)

    def test_the_image_records_its_nested_podman_ranges(self):
        """A launch compares them with what its own Podman can delegate."""
        from agent_box import runtime
        text = containerfile_text()
        for kind, label in runtime.IMAGE_ID_RANGE_LABELS.items():
            self.assertIn(f'{label}="${{PODMAN_{kind.upper()}_RANGES}}"', text)

    def test_pebble_is_removed_without_invalidating_stable_layers(self):
        """The base's unpackaged Pebble is never run and carries its own Go
        advisories; removing it above the browser or jscpd layers would
        rebuild them, and the sanity check catches a base that moves it."""
        text = containerfile_text()
        removal = text.index("rm -rf /usr/bin/pebble /var/lib/pebble")
        for stable in ("install --with-deps chromium", "jscpd@5.0.16"):
            self.assertLess(text.index(stable), removal)
        self.assertLess(removal, text.index("ARG TOOLS_REFRESH="))
        self.assertIn("test ! -e /usr/bin/pebble", text)

    def test_no_binfmt_entry_can_hide_the_hosts_emulation(self):
        """Rootless podman build registers any binfmt.d entry in a fresh
        binfmt_misc instance for the nested user namespace, which hides the
        host's qemu registrations: python3.14-minimal's entry made arm64
        builds and later runs fail with "Exec format error". dpkg must keep
        later installs from adding one, the removal must not rebuild the
        stable layers, and the sanity check must catch a base that ships one."""
        text = containerfile_text()
        exclusion = text.index("> /etc/dpkg/dpkg.cfg.d/agent-box-binfmt")
        self.assertIn("printf 'path-exclude=%s/*\\n' /etc/binfmt.d /run/binfmt.d /usr/lib/binfmt.d", text)
        for stable in ("install --with-deps chromium", "jscpd@5.0.16"):
            self.assertLess(text.index(stable), exclusion)
        self.assertLess(exclusion, text.index("ARG TOOLS_REFRESH="))
        self.assertIn("rm -f /etc/binfmt.d/*.conf /run/binfmt.d/*.conf /usr/lib/binfmt.d/*.conf", text)
        self.assertIn("""test -z "$(find /etc/binfmt.d /run/binfmt.d /usr/lib/binfmt.d -name '*.conf' 2>/dev/null)\"""", text)


class IdentityValidationShellTests(unittest.TestCase):
    """Run the Containerfile's own validation snippet through /bin/sh.

    Extracting and executing the real snippet keeps this honest: it tests the
    shipped shell logic, not a paraphrase of it.
    """

    @classmethod
    def setUpClass(cls):
        text = containerfile_text()
        start = text.index("RUN set -eu; \\\n    missing=\"\";")
        end = text.index("\n\nRUN set -eux;", start)
        snippet = text[start:end]
        snippet = snippet[len("RUN "):]
        # Un-escape the Containerfile line continuations into a shell script.
        cls.script = re.sub(r"\\\n\s*", " ", snippet)

    def _run(self, env):
        if not shutil.which("sh"):  # pragma: no cover - POSIX hosts only
            self.skipTest("no POSIX shell available")
        return subprocess.run(
            ["sh", "-c", self.script], env=env, capture_output=True, text=True,
        )

    def test_missing_args_fail_with_actionable_message(self):
        result = self._run({"PATH": "/usr/bin:/bin"})
        self.assertNotEqual(result.returncode, 0)
        for name in REQUIRED_ARGS:
            self.assertIn(name, result.stderr)
        self.assertIn("agent-box build", result.stderr)

    def test_complete_args_succeed(self):
        result = self._run({
            "PATH": "/usr/bin:/bin", "USER_NAME": "alex", "USER_UID": "1000",
            "USER_GID": "1000", "USER_HOME": "/home/alex",
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("alex", result.stdout)

    def test_non_numeric_uid_rejected(self):
        result = self._run({
            "PATH": "/usr/bin:/bin", "USER_NAME": "alex", "USER_UID": "root",
            "USER_GID": "1000", "USER_HOME": "/home/alex",
        })
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("USER_UID must be numeric", result.stderr)

    def test_relative_home_rejected(self):
        result = self._run({
            "PATH": "/usr/bin:/bin", "USER_NAME": "alex", "USER_UID": "1000",
            "USER_GID": "1000", "USER_HOME": "home/alex",
        })
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("USER_HOME must be an absolute path", result.stderr)

    def test_partial_args_still_fail(self):
        result = self._run({"PATH": "/usr/bin:/bin", "USER_NAME": "alex", "USER_UID": "1000"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("USER_GID", result.stderr)
        self.assertIn("USER_HOME", result.stderr)
        self.assertNotIn(" USER_NAME", result.stderr.split("\n")[0])


if __name__ == "__main__":
    sys.exit(unittest.main())
