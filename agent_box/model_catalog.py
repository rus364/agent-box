"""Best-effort CLI suggestions; a returned model does not guarantee account access."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile

from .paths import CLAUDE_HOME, CODEX_HOME


# Offline fallback from the providers' model configuration documentation.
BUNDLED_MODELS = {
    "codex": ("gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5", "gpt-5.3-codex-spark"),
    "claude": ("opus", "sonnet", "haiku", "claude-opus-5", "claude-sonnet-5"),
}


def _valid_model(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*(?:\[1m\])?", value) is not None


def _codex_models(output):
    data = json.loads(output)
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, list):
        return []
    return [item["slug"] for item in models if isinstance(item, dict)
            and item.get("visibility") == "list" and _valid_model(item.get("slug"))]


def _claude_models(output):
    for line in output.splitlines():
        data = json.loads(line)
        if not isinstance(data, dict) or data.get("type") != "control_response":
            continue
        response = data.get("response")
        if not isinstance(response, dict) or response.get("request_id") != "catalog-probe":
            continue
        payload = response.get("response")
        if response.get("subtype") != "success" or not isinstance(payload, dict):
            return []
        models = payload.get("models")
        if not isinstance(models, list):
            return []
        # "default" resets Claude's preference; it is not a model identifier.
        return [value for item in models if isinstance(item, dict)
                for value in (item.get("value"), item.get("resolvedModel"))
                if value != "default" and _valid_model(value)]
    return []


def _probe(command, *, env, cwd=None, request=None):
    # npm launchers can leave a child holding stdout open after the parent dies.
    with subprocess.Popen(command, env=env, cwd=cwd, text=True, start_new_session=True,
                          stdin=subprocess.PIPE if request is not None else subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
        try:
            output, _ = process.communicate(request, timeout=3)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
            raise
        if process.returncode:
            raise subprocess.CalledProcessError(process.returncode, command)
        return output


def load_models(provider):
    """Each probe is bounded and failures stay quiet so the editor remains usable."""
    try:
        executable = shutil.which(provider)
        if not executable:
            return BUNDLED_MODELS[provider]
        executable = str(Path(executable).resolve())
        if provider == "codex":
            output = _probe([executable, "debug", "models"],
                            env={**os.environ, "CODEX_HOME": str(CODEX_HOME)})
            models = _codex_models(output)
        else:
            # Initialization writes state even with session persistence disabled.
            # Keep it outside the user's settings and clean up refreshed credentials.
            with tempfile.TemporaryDirectory(prefix="agent-box-models-") as directory:
                credentials = CLAUDE_HOME / ".credentials.json"
                if credentials.is_file():
                    target = Path(directory) / ".credentials.json"
                    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "wb") as destination, credentials.open("rb") as source:
                        shutil.copyfileobj(source, destination)
                env = {**os.environ, "CLAUDE_CONFIG_DIR": directory}
                for key in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID"):
                    env.pop(key, None)
                output = _probe([
                    executable, "-p", "--safe-mode", "--no-session-persistence",
                    "--input-format", "stream-json", "--output-format", "stream-json",
                    "--verbose", "--tools", "", "--strict-mcp-config", "--mcp-config",
                    '{"mcpServers":{}}', "--setting-sources", "",
                ], request=json.dumps({"type": "control_request", "request_id": "catalog-probe",
                                     "request": {"subtype": "initialize"}}) + "\n",
                    cwd=directory, env=env)
                models = _claude_models(output)
        return tuple(dict.fromkeys(models)) or BUNDLED_MODELS[provider]
    except (OSError, ValueError, subprocess.SubprocessError):
        return BUNDLED_MODELS[provider]


def load_model_catalogs():
    """Refresh once at menu entry, without adding the clients' timeout budgets."""
    with ThreadPoolExecutor(max_workers=2) as executor:
        return dict(zip(BUNDLED_MODELS, executor.map(load_models, BUNDLED_MODELS)))
