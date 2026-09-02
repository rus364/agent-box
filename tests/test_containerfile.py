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
