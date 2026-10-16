"""Run unittest discovery with disposable integration paths and pipe-backed stdio."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parent.parent


def isolated_environment(directory):
    # A workspace session exports its runtime config, notification credentials
    # and flags; the suite must see only its own fixtures, never the caller's.
    env = {name: value for name, value in os.environ.items() if not name.startswith("AGENT_BOX_")}
    for name, child in {
        "XDG_CONFIG_HOME": "xdg-config", "XDG_DATA_HOME": "xdg-data",
        "XDG_CACHE_HOME": "cache", "XDG_STATE_HOME": "xdg-state",
        "AGENT_BOX_STATE_DIR": "state", "AGENT_BOX_CONFIG_DIR": "config",
        "AGENT_BOX_WORKFLOW_STATE": "workflow", "AGENT_BOX_AGENTS_DIR": "agents",
        "CLAUDE_CONFIG_DIR": "claude", "CODEX_HOME": "codex",
        "CODEX_AUTH_PATH": "codex/auth.json",
        "CLAUDE_CREDENTIALS_PATH": "claude/.credentials.json",
    }.items():
        env[name] = str(directory / child)
    # Keep subprocess discovery independent of developer-installed commands; the
    # guard directory main fills shadows the host's podman.
    env["PATH"] = os.pathsep.join((str(directory / "guard-bin"), str(directory / "bin"), os.defpath))
    env["PYTHONPATH"] = str(ROOT)
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return env


def run_child(arguments):
    import unittest

    sys.path.insert(0, str(ROOT))
    from agent_box import links

    # These defaults deliberately use passwd home in production, not env paths.
    directory = Path(os.environ["AGENT_BOX_STATE_DIR"]).parent
    links.LINKS_RUNTIME_DIR = directory / "links"
    unittest.main(module=None, argv=[sys.argv[0], *arguments])


def main(arguments):
    with tempfile.TemporaryDirectory(prefix="agent-box-tests-") as tmp:
        # A test that reaches the real podman passes only where Podman happens
        # to work, as launch tests did until CI ran on a host without it.
        podman = Path(tmp) / "guard-bin" / "podman"
        podman.parent.mkdir()
        podman.write_text("#!/bin/sh\necho 'a test reached the real podman; patch it out' >&2\nexit 127\n")
        podman.chmod(0o755)
        command = [sys.executable, str(Path(__file__).resolve()), "--child",
                   *(arguments or ["discover", "-s", "tests"])]
        # Pipes keep the suite noninteractive even when this wrapper has a PTY;
        # individual terminal tests create their own PTYs.
        with subprocess.Popen(command, cwd=ROOT, env=isolated_environment(Path(tmp)),
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT) as process:
            for line in process.stdout:
                sys.stdout.buffer.write(line)
                sys.stdout.buffer.flush()
            return process.wait()


if __name__ == "__main__":
    if sys.argv[1:2] == ["--child"]:
        run_child(sys.argv[2:])
    else:
        raise SystemExit(main(sys.argv[1:]))
