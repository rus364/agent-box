"""Run unittest discovery with disposable integration paths and pipe-backed stdio."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parent.parent


def isolated_environment(directory):
    env = dict(os.environ)
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
    env.pop("AGENT_BOX_RUNTIME_CONFIG", None)
    # Uninstall must not discover the developer's launcher through PATH.
    env["PATH"] = os.pathsep.join((str(directory / "bin"), os.defpath))
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return env


def run_child(arguments):
    import unittest

    sys.path.insert(0, str(ROOT))
    from agent_box import pathsetup

    # These defaults deliberately use passwd home in production, not env paths.
    directory = Path(os.environ["AGENT_BOX_STATE_DIR"]).parent
    pathsetup.HOST_HOME = directory
    pathsetup.DEFAULT_BIN_DIR = directory / "bin"
    pathsetup.RC_FILES = {shell: directory / f".{shell}rc" for shell in ("bash", "zsh")}
    unittest.main(module=None, argv=[sys.argv[0], *arguments])


def main(arguments):
    with tempfile.TemporaryDirectory(prefix="agent-box-tests-") as tmp:
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
