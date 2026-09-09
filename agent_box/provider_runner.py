"""Fresh CLI contexts with explicit models and machine-checked reports."""
from __future__ import annotations

import base64
import fcntl
import json
import math
import os
import re
import signal
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from .atomicio import atomic_write_json
from .errors import AgentBoxError
from .paths import WORKFLOW_STATE


# Failures retain bounded raw bytes privately, never provider text in public evidence.
RESPONSE_LIMIT = 4 * 1024 * 1024
DIAGNOSTIC_STREAM_LIMIT = 1024 * 1024
STDERR_LIMIT = 64 * 1024
DIAGNOSTIC_MAX_FILES = 20
DIAGNOSTIC_MAX_AGE = 7 * 24 * 3600


class ProviderFailure(AgentBoxError):
    def __init__(self, message, category="provider-error", *, stage="execution", reason=None):
        super().__init__(message)
        self.category = category
        self.stage = stage
        self.reason = reason or category
        self.diagnostic = {}


def invalid(stage, reason):
    raise ProviderFailure(f"Invalid provider report ({stage}: {reason}); no acceptance recorded",
                          "invalid-report", stage=stage, reason=reason)


def report_schema(role):
    nonblank = {"type": "string", "pattern": r"\S"}
    if role == "reviewer":
        finding = {"type": "object", "properties": {
            **{key: dict(nonblank) for key in ("severity", "path", "message", "evidence", "requirement")},
            "line": {"type": "integer", "minimum": 0}},
            "required": ["severity", "path", "line", "message", "evidence", "requirement"], "additionalProperties": False}
        properties = {"verdict": {"type": "string", "enum": ["PASS", "NEEDS_WORK", "INCONCLUSIVE"]},
                      "summary": nonblank, "findings": {"type": "array", "items": finding}}
    else:
        properties = {"status": {"type": "string", "enum": ["completed", "blocked", "needs-human"]},
                      "summary": nonblank, "evidence": {"type": "array", "items": {"type": "string"}}}
    schema = {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}
    if role == "reviewer":
        # Claude rejects top-level conditional schemas; keep cross-field rules
        # explicit for generation and enforce them locally before acceptance.
        properties["findings"]["description"] = "Must be empty for PASS and nonempty for NEEDS_WORK."
    return schema


def validate_report(report, role):
    # Reasons use schema-owned field names, never values or unknown provider keys.
    if not isinstance(report, dict):
        invalid("schema", "report must be an object")
    expected = {"summary", "verdict", "findings"} if role == "reviewer" else {"summary", "status", "evidence"}
    if report.keys() - expected:
        invalid("schema", "report has unexpected fields")
    if not isinstance(report.get("summary"), str) or not report["summary"].strip():
        invalid("schema", "summary must be a nonblank string")
    if role == "reviewer":
        if report.get("verdict") not in ("PASS", "NEEDS_WORK", "INCONCLUSIVE"):
            invalid("schema", "verdict must be PASS, NEEDS_WORK or INCONCLUSIVE")
        findings = report.get("findings")
        if not isinstance(findings, list):
            invalid("schema", "findings must be an array")
        for index, finding in enumerate(findings):
            field = f"findings[{index}]"
            if not isinstance(finding, dict):
                invalid("schema", f"{field} must be an object")
            if finding.keys() - {"severity", "path", "line", "message", "evidence", "requirement"}:
                invalid("schema", f"{field} has unexpected fields")
            for key in ("severity", "path", "message", "evidence", "requirement"):
                if not isinstance(finding.get(key), str) or not finding[key].strip():
                    invalid("schema", f"{field}.{key} must be a nonblank string")
            if type(finding.get("line")) is not int or finding["line"] < 0:
                invalid("schema", f"{field}.line must be a nonnegative integer")
        if report["verdict"] == "PASS" and findings:
            invalid("schema", "PASS requires empty findings")
        if report["verdict"] == "NEEDS_WORK" and not findings:
            invalid("schema", "NEEDS_WORK requires findings")
    else:
        if report.get("status") not in ("completed", "blocked", "needs-human"):
            invalid("schema", "status must be completed, blocked or needs-human")
        evidence = report.get("evidence")
        if not isinstance(evidence, list) or any(not isinstance(v, str) for v in evidence):
            invalid("schema", "evidence must be an array of strings")
    return report


def response_metadata(result):
    """Whitelist numeric counters; arbitrary provider keys may contain secrets."""
    def number(value):
        return value if type(value) in (int, float) and 0 <= value <= 1e15 and math.isfinite(value) else None

    usage = result.get("usage")
    safe = {}
    if isinstance(usage, dict):
        for key in ("input_tokens", "output_tokens", "cached_input_tokens", "cache_write_input_tokens",
                    "reasoning_output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
            safe[key] = number(usage.get(key))
        for key, fields in (("cache_creation", ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")),
                            ("server_tool_use", ("web_search_requests", "web_fetch_requests"))):
            if isinstance(usage.get(key), dict):
                safe[key] = {field: number(usage[key].get(field)) for field in fields}
    return {"usage": safe or None, "total_cost_usd": number(result.get("total_cost_usd"))}


def parse_result(provider, returncode, output, stderr, *, metadata=None):
    metadata = metadata if metadata is not None else {}
    try:
        result = json.loads(output)
    except (ValueError, TypeError, RecursionError):
        result = None
        if not returncode:
            invalid("json", "response is not valid JSON")
    if isinstance(result, dict):
        metadata.update(response_metadata(result))
    envelope = result if isinstance(result, dict) else {}
    subtype = envelope.get("subtype")
    failed_envelope = (envelope.get("is_error") or
                       provider == "claude" and (isinstance(subtype, str) and subtype.startswith("error_") or envelope.get("errors")))
    if returncode or failed_envelope:
        # Error details are used for classification locally but never interpolated into evidence.
        lowered = (str(envelope.get("result", "")) + str(envelope.get("errors", "")) + stderr).lower()
        category = "provider-error"
        if envelope.get("api_error_status") == 404 or "model" in lowered and any(s in lowered for s in ("not found", "unavailable", "not supported", "does not exist")):
            category = "model-unavailable"
        elif envelope.get("api_error_status") in (401, 403) or any(s in lowered for s in ("unauthorized", "authentication", "authenticate", "login")):
            category = "authentication"
        elif envelope.get("api_error_status") == 429 or any(s in lowered for s in ("rate limit", "quota", "usage limit")):
            category = "quota"
        reason = subtype if subtype in ("error_max_structured_output_retries", "error_max_turns", "error_during_execution", "error_max_budget_usd") else "provider error envelope" if failed_envelope else "nonzero exit"
        raise ProviderFailure(f"{provider} failed ({category}: {reason}, exit {returncode}); no task verdict",
                              category, stage="envelope" if failed_envelope else "execution", reason=reason)
    if provider == "claude" and isinstance(result, dict):
        if "structured_output" in result:
            metadata["response_source"] = "stdout.structured_output"
            result = result["structured_output"]
            if not isinstance(result, dict):
                invalid("envelope", "structured_output must be an object")
        elif "result" in result:
            metadata["response_source"] = "stdout.result"
            result = result["result"]
            if isinstance(result, str):
                try:
                    result = json.loads(result)
                except (ValueError, RecursionError):
                    invalid("json", "result string is not valid JSON")
            if not isinstance(result, dict):
                invalid("envelope", "result must contain a JSON object")
        elif result.get("type") == "result" or "subtype" in result:
            invalid("envelope", "Claude envelope has no structured_output or result")
    if not isinstance(result, dict):
        invalid("envelope", "response must be a JSON object")
    return result


def failure_metadata(route, role):
    def identifier(value):
        return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", value) else None
    return {**{key: identifier(route.get(key)) for key in ("provider", "model", "effort")},
            "role": identifier(role), "exit_code": None, "duration_seconds": None,
            "response_source": None, "stdout_bytes": None, "stderr_bytes": None,
            "result_bytes": None, "usage": None, "total_cost_usd": None}


def save_failure(metadata, directory):
    """Keep at most 20 private artifacts for seven days; prune on each failure."""
    target = WORKFLOW_STATE / "provider-failures"
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.chmod(0o700)
    # A blocking local lock keeps retention bounded even for overlapping scouts.
    fd = os.open(target / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        now = time.time()
        files = sorted(target.glob("*.json"), key=lambda path: path.lstat().st_mtime, reverse=True)
        for index, path in enumerate(files):
            if index >= DIAGNOSTIC_MAX_FILES - 1 or now - path.lstat().st_mtime > DIAGNOSTIC_MAX_AGE:
                path.unlink()
        ident = uuid.uuid4().hex + ".json"
        reference = "provider-failures/" + ident
        streams = {}
        for name in ("stdout", "stderr", "result.json"):
            path = directory / name
            if path.exists():
                limit = STDERR_LIMIT if name == "stderr" else DIAGNOSTIC_STREAM_LIMIT
                with path.open("rb") as stream:
                    data = stream.read(limit)
                streams[name] = {"base64": base64.b64encode(data).decode("ascii"),
                                 "truncated": path.stat().st_size > len(data)}
        atomic_write_json(target / ident, {"metadata": {**metadata, "artifact": reference},
                                          "created_at": now, "streams": streams}, 0o600)
        return reference
    finally:
        os.close(fd)


def preflight(route):
    """Check local launch facts without guessing live model or account availability."""
    if route.get("provider") not in ("codex", "claude"):
        raise ProviderFailure("Unsupported managed provider", "unsupported")
    if not route.get("model") or not route.get("effort"):
        raise ProviderFailure("Managed execution requires known model and effort", "unsupported")
    if not shutil.which(route["provider"]):
        raise ProviderFailure(f"Cannot find {route['provider']} CLI", "unavailable")


def build_command(route, role, root, schema_path, result_path, read_paths=()):
    readonly = role != "worker"
    if route["provider"] == "codex":
        command = ["codex", "exec", "--ephemeral", "--ignore-user-config", "--json", "-C", str(root),
                   "--sandbox", "read-only" if readonly else "workspace-write", "--model", route["model"]]
        if route["effort"] != "default":
            command += ["-c", 'model_reasoning_effort=' + json.dumps(route["effort"])]
        command += [*([] if readonly else ["-c", "sandbox_workspace_write.network_access=true"]),
                    "--output-schema", str(schema_path), "-o", str(result_path), "-"]
        return command
    # Excluding Bash prevents a read-only reviewer from gaining shell write access.
    command = ["claude", "-p", "--model", route["model"]]
    if route["effort"] != "default":
        command += ["--effort", route["effort"]]
    for path in read_paths:
        command += ["--add-dir", str(path)]
    command += ["--output-format", "json", "--no-session-persistence", "--safe-mode", "--strict-mcp-config",
               "--mcp-config", '{"mcpServers":{}}', "--permission-mode", "dontAsk",
               "--tools", "Read,Grep,Glob" if readonly else "Read,Grep,Glob,Bash,Edit,Write",
               "--allowedTools", "Read,Grep,Glob" if readonly else "Read,Grep,Glob,Bash,Edit,Write"]
    if schema_path.exists():
        command += ["--json-schema", schema_path.read_text()]
    return command


def codex_usage(path):
    """Read turn counters from JSONL; the final report file contains no usage."""
    totals = {}
    with path.open("rb") as stream:
        while line := stream.readline(RESPONSE_LIMIT + 1):
            if len(line) > RESPONSE_LIMIT:
                while line and not line.endswith(b"\n"):
                    line = stream.readline(RESPONSE_LIMIT + 1)
                continue
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError, RecursionError):
                continue
            if not isinstance(event, dict) or event.get("type") != "turn.completed":
                continue
            usage = response_metadata(event)["usage"] or {}
            for key in ("input_tokens", "output_tokens", "cached_input_tokens", "cache_write_input_tokens",
                        "reasoning_output_tokens"):
                value = usage.get(key)
                if value is not None:
                    totals[key] = totals.get(key, 0) + value
    return response_metadata({"usage": totals or None})


def run(route, role, root, prompt, *, timeout=3600, read_paths=(), diagnostics=None):
    env = os.environ.copy()
    for key in ("CLAUDE_CODE_SUBAGENT_MODEL", "CLAUDE_CODE_EFFORT_LEVEL", "CLAUDECODE"):
        env.pop(key, None)
    from .notify import SUPPRESS_NOTIFY_ENV
    env[SUPPRESS_NOTIFY_ENV] = "1"
    metadata = failure_metadata(route, role)
    started = time.monotonic()
    process = None
    with tempfile.TemporaryDirectory(prefix="agent-box-run-") as directory:
        directory = Path(directory)
        schema, result = directory / "schema.json", directory / "result.json"
        schema.write_text(json.dumps(report_schema(role), ensure_ascii=False), encoding="utf-8")
        command = build_command(route, role, Path(root), schema, result, read_paths)
        with (directory / "stdout").open("w+") as stdout, (directory / "stderr").open("w+") as stderr:
            try:
                try:
                    process = subprocess.Popen(command, cwd=root, env=env, stdin=subprocess.PIPE, stdout=stdout,
                                               stderr=stderr, text=True, encoding="utf-8", start_new_session=True)
                    try:
                        process.communicate(prompt, timeout=timeout)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                        raise ProviderFailure("Provider timed out; inspect work before retrying", "timeout") from None
                except OSError:
                    raise ProviderFailure(f"Cannot start {route['provider']} CLI", "unavailable") from None
                stdout.flush()
                stderr.flush()
                source = result if route["provider"] == "codex" and result.exists() else directory / "stdout"
                metadata["response_source"] = "result-file" if source == result else "stdout"
                with source.open("rb") as stream:
                    output = stream.read(RESPONSE_LIMIT + 1)
                if len(output) > RESPONSE_LIMIT and not process.returncode:
                    invalid("json", "response exceeds 4 MiB parse limit")
                with (directory / "stderr").open("rb") as stream:
                    error = stream.read(STDERR_LIMIT)
                try:
                    output = output.decode("utf-8")
                except UnicodeDecodeError:
                    if process.returncode:
                        output = ""
                    else:
                        invalid("json", "response is not valid UTF-8")
                report = validate_report(parse_result(route["provider"], process.returncode, output,
                                                     error.decode("utf-8", errors="replace"), metadata=metadata), role)
                if route["provider"] == "codex":
                    metadata.update(codex_usage(directory / "stdout"))
                if diagnostics is not None:
                    diagnostics.update({key: metadata[key] for key in ("usage", "total_cost_usd")})
                return report
            except ProviderFailure as exc:
                stdout.flush()
                stderr.flush()
                metadata.update(category=exc.category, stage=exc.stage, reason=exc.reason,
                                duration_seconds=round(time.monotonic() - started, 3),
                                exit_code=process.returncode if process is not None else None)
                for name, path in (("stdout", directory / "stdout"), ("stderr", directory / "stderr"), ("result", result)):
                    metadata[name + "_bytes"] = path.stat().st_size if path.exists() else None
                try:
                    metadata["artifact"] = save_failure(metadata, directory)
                except OSError:
                    metadata.update(artifact=None, diagnostic_error="cannot persist private diagnostic")
                exc.diagnostic = metadata
                raise
