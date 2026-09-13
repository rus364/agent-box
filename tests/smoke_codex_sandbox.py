"""Offline smoke inside a workspace; uses no API or user Codex configuration."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", default="codex", help="Codex executable to check")
    args = parser.parse_args()
    codex = shutil.which(args.codex)
    if not codex:
        parser.error("Codex executable not found")
    with tempfile.TemporaryDirectory(prefix="agent-box-sandbox-smoke-") as directory:
        root = Path(directory)
        workspace = root / "workspace"
        workspace.mkdir()
        (workspace / ".git").mkdir()
        (workspace / "project").write_text("project evidence")
        (root / "canonical-evidence").write_text("review evidence")
        codex_home = root / "codex-home"
        codex_home.mkdir()
        settings = codex_home / "config.toml"
        settings.write_text('# Smoke-owned settings must remain intact.\n')
        original_settings = settings.read_bytes()
        env = {**os.environ, "CODEX_HOME": str(codex_home)}
        capabilities = [line for line in Path("/proc/self/status").read_text().splitlines()
                        if line.startswith("Cap")]
        for mode in ("read-only", "workspace-write"):
            # A project command reads both source and out-of-tree review evidence,
            # then checks writable roots and the protected Git metadata directory.
            script = """
import pathlib
assert pathlib.Path('project').read_text() == 'project evidence'
assert pathlib.Path('../canonical-evidence').read_text() == 'review evidence'
for target, allowed in CASES:
    try:
        pathlib.Path(target).write_text('probe')
        actual = True
    except OSError:
        actual = False
    assert actual == allowed, (target, actual, allowed)
""".replace("CASES", repr([
                ("output", mode == "workspace-write"),
                (".git/blocked", False),
                ("../outside", False),
            ]))
            result = subprocess.run([
                codex, "sandbox", "-c", f'sandbox_mode="{mode}"',
                "-c", "sandbox_workspace_write.exclude_slash_tmp=true",
                "-c", "sandbox_workspace_write.exclude_tmpdir_env_var=true",
                shutil.which("python3"), "-c", script,
            ], cwd=workspace, env=env, capture_output=True, text=True, timeout=30)
            if result.returncode:
                raise RuntimeError(f"{mode}: {result.stdout}\n{result.stderr}")
            assert settings.read_bytes() == original_settings
            print(f"{mode}: source/evidence reads and write boundaries passed")
        assert capabilities == [line for line in Path("/proc/self/status").read_text().splitlines()
                                if line.startswith("Cap")]
        print("Parent capabilities and Codex settings preserved; no API calls")


if __name__ == "__main__":
    main()
