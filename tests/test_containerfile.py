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
import unittest
from pathlib import Path

CONTAINERFILE = Path(__file__).resolve().parent.parent / "Containerfile"
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
        self.assertIn("tmux -V;", text)


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
        text = containerfile_text()
        checks = text[text.index("# Sanity checks"):]
        self.assertIn("playwright-mcp --version;", checks)
        self.assertIn("test -d /opt/ms-playwright;", checks)
        # An executable check is not enough: prove a browser really starts.
        self.assertIn("agent-box-browser-smoke;", checks)

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
        text = containerfile_text()
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

    def test_runtime_agrees_with_the_image_on_names(self):
        from agent_box import runtime
        text = containerfile_text()
        self.assertIn(runtime.BROWSER_MCP_EXECUTABLE, text)
        self.assertIn("agent-box-browser-smoke", runtime.DOCTOR_SCRIPT)


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
