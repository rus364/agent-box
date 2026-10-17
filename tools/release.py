"""Release guards and isolated artifact/index acceptance; no publishing credentials."""
import argparse
from email.parser import Parser
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
from urllib.parse import urlsplit
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import package_metadata, markdown_links  # noqa: E402

NAME = "agent-box-tool"
INDEXES = {"pypi": "https://pypi.org", "testpypi": "https://test.pypi.org"}


def project(root=ROOT):
    return tomllib.loads((root / "pyproject.toml").read_text())["project"]


def check_tag(tag, metadata):
    if not re.fullmatch(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", tag):
        raise ValueError("Release tags must be stable SemVer vMAJOR.MINOR.PATCH")
    if metadata["name"] != NAME or tag != "v" + metadata["version"]:
        raise ValueError("Release tag and project name/version do not match")


def artifacts(dist, root=ROOT):
    metadata = project(root)
    version = metadata["version"]
    check_tag("v" + version, metadata)
    paths = [dist / f"agent_box_tool-{version}-py3-none-any.whl",
             dist / f"agent_box_tool-{version}.tar.gz"]
    if set(dist.iterdir()) != set(paths):
        raise ValueError("dist must contain exactly the release wheel and sdist")
    expected = {str(p.relative_to(root)): p.read_bytes() for p in (root / "agent_box").rglob("*")
                if p.is_file() and "__pycache__" not in p.parts and p.suffix not in {".pyc", ".pyo"}}
    with zipfile.ZipFile(paths[0]) as wheel:
        package = {n: wheel.read(n) for n in wheel.namelist() if n.startswith("agent_box/") and not n.endswith("/")}
        info = Parser().parsestr(wheel.read(f"agent_box_tool-{version}.dist-info/METADATA").decode())
        if package != expected:
            raise ValueError("Wheel runtime resources differ from the source tree")
        if not wheel.getinfo("agent_box/container-launcher").external_attr >> 16 & 0o111:
            raise ValueError("Wheel launcher is not executable")
    if (info["Name"] != NAME or info["Version"] != version or info.get_all("Requires-Dist")
            or info["Requires-Python"] != metadata["requires-python"]):
        raise ValueError("Wheel metadata differs from the dependency-free release contract")
    check_publication_metadata(info, root)
    prefix = f"agent_box_tool-{version}/"
    with tarfile.open(paths[1]) as source:
        package = {m.name.removeprefix(prefix): source.extractfile(m).read()
                   for m in source.getmembers() if m.isfile() and m.name.startswith(prefix + "agent_box/")}
        if package != expected:
            raise ValueError("sdist runtime resources differ from the source tree")
        source_project = tomllib.loads(source.extractfile(prefix + "pyproject.toml").read().decode())["project"]
        if source_project != metadata:
            raise ValueError("sdist project metadata differs from the release")
        check_publication_metadata(Parser().parsestr(source.extractfile(prefix + "PKG-INFO").read().decode()), root)
    return paths


def project_urls(info):
    return dict(value.split(", ", 1) for value in info.get_all("Project-URL", []))


def check_publication_metadata(info, root):
    expected = package_metadata.distribution_metadata(root)
    if (info["Description-Content-Type"] != expected["long_description_content_type"]
            or info.get_payload().strip() != expected["long_description"].strip()
            or project_urls(info) != expected["project_urls"]):
        raise ValueError("Built description or project URLs differ from the release metadata")
    urls = [target for _, _, target, _ in markdown_links.destinations(info.get_payload())]
    for url in urls + list(project_urls(info).values()):
        if urlsplit(url).scheme not in {"https", "http", "mailto"}:
            raise ValueError(f"Published metadata contains a relative URL: {url}")


def isolated_environment(root):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("AGENT_BOX_", "PIP_", "UV_", "PIPX_")) and k != "PYTHONPATH"}
    for key, child in {"AGENT_BOX_CONFIG_DIR": "config", "AGENT_BOX_STATE_DIR": "state",
                       "AGENT_BOX_AGENTS_DIR": "agents", "AGENT_BOX_WORKFLOW_STATE": "workflow",
                       "CLAUDE_CONFIG_DIR": "claude", "CODEX_HOME": "codex",
                       "CLAUDE_CREDENTIALS_PATH": "claude/.credentials.json", "CODEX_AUTH_PATH": "codex/auth.json",
                       "UV_CACHE_DIR": "uv-cache", "UV_TOOL_DIR": "uv-tools", "UV_TOOL_BIN_DIR": "uv-bin",
                       "PIPX_HOME": "pipx", "PIPX_BIN_DIR": "pipx-bin", "PIPX_MAN_DIR": "pipx-man",
                       "PIP_CACHE_DIR": "pip-cache"}.items():
        env[key] = str(root / child)
    env["PIP_CONFIG_FILE"] = os.devnull
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    return env


def run(command, root, env):
    subprocess.run([str(x) for x in command], cwd=root, env=env, check=True)


def installed_checks(python, command, version, root, env):
    run([python, "-c", "import importlib.metadata as m; "
         f"assert m.version('{NAME}') == {version!r}; assert not m.requires('{NAME}')"], root, env)
    run([command, "selftest"], root, env)


def install_artifacts(paths, version):
    for path in paths:
        with tempfile.TemporaryDirectory(prefix="agent-box-dist-") as directory:
            root = Path(directory)
            env = isolated_environment(root)
            python = root / "venv/bin/python"
            run([sys.executable, "-m", "venv", root / "venv"], root, env)
            run([python, "-m", "pip", "install", "--no-deps", path.resolve()], root, env)
            installed_checks(python, root / "venv/bin/agent-box", version, root, env)


def install_tools(spec, version, index=None):
    with tempfile.TemporaryDirectory(prefix="agent-box-installers-") as directory:
        root = Path(directory)
        env = isolated_environment(root)
        uv_args = ["--default-index", index + "/simple/"] if index else ["--no-index"]
        pipx_args = ["--index-url", index + "/simple/"] if index else []
        run(["uv", "tool", "install", "--no-config", "--python", sys.executable, *uv_args, spec], root, env)
        installed_checks(root / "uv-tools/agent-box-tool/bin/python", root / "uv-bin/agent-box", version, root, env)
        run([sys.executable, "-m", "pipx", "install", "--python", sys.executable,
             "--pip-args=--no-deps", *pipx_args, spec], root, env)
        installed_checks(root / "pipx/venvs/agent-box-tool/bin/python", root / "pipx-bin/agent-box", version, root, env)


def verify_index(index, paths, version):
    expected = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    # Uploads and index propagation are separate; tolerate bounded publication lag.
    for attempt in range(20):
        try:
            with urllib.request.urlopen(f"{index}/pypi/{NAME}/{version}/json", timeout=30) as response:
                release = json.load(response)
            published = {f["filename"]: f for f in release["urls"]}
            if set(published) == set(expected):
                break
        except (urllib.error.URLError, TimeoutError):
            pass
        if attempt == 19:
            raise ValueError("The complete release did not appear in the index")
        time.sleep(3)
    for name, digest in expected.items():
        file = published[name]
        if file["digests"]["sha256"] != digest:
            raise ValueError(f"Published digest differs from the checked build: {name}")
        with urllib.request.urlopen(file["url"], timeout=60) as response:
            downloaded = hashlib.sha256(response.read()).hexdigest()
        if downloaded != digest:
            raise ValueError(f"Downloaded bytes differ from the checked build: {name}")
    with zipfile.ZipFile(paths[0]) as wheel:
        info = Parser().parsestr(wheel.read(f"agent_box_tool-{version}.dist-info/METADATA").decode())
    if (release["info"]["description"].strip() != info.get_payload().strip()
            or release["info"]["project_urls"] != project_urls(info)):
        raise ValueError("Published description or project URLs differ from the checked wheel")
    install_tools(f"{NAME}=={version}", version, index)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("tag", "artifacts", "index", "notes"))
    parser.add_argument("--tag", default=os.environ.get("RELEASE_TAG", ""))
    parser.add_argument("--dist", type=Path, default=ROOT / "dist")
    parser.add_argument("--index", choices=INDEXES)
    parser.add_argument("--install", action="store_true")
    args = parser.parse_args()
    metadata = project()
    if args.operation == "notes":
        print(package_metadata.release_notes(ROOT), end="")
        return
    if args.operation == "tag":
        check_tag(args.tag, metadata)
        return
    paths = artifacts(args.dist)
    if args.operation == "index":
        if not args.index:
            parser.error("index requires --index pypi|testpypi")
        verify_index(INDEXES[args.index], paths, metadata["version"])
    elif args.install:
        install_artifacts(paths, metadata["version"])
        install_tools(str(paths[0].resolve()), metadata["version"])


if __name__ == "__main__":
    main()
