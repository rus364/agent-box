"""Fresh CLI contexts with explicit models: a checked reviewer report, or a delegate's Markdown."""
from __future__ import annotations

import base64
import contextlib
import fcntl
import json
import math
import os
import re
import signal
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

from .atomicio import atomic_write_json
from .errors import AgentBoxError
from .external_config import toml_string
from .paths import WORKFLOW_STATE


# Failures retain bounded raw bytes privately, never provider text in public evidence.
RESPONSE_LIMIT = 4 * 1024 * 1024
DIAGNOSTIC_STREAM_LIMIT = 1024 * 1024
STDERR_LIMIT = 64 * 1024
DIAGNOSTIC_MAX_FILES = 20
DIAGNOSTIC_MAX_AGE = 7 * 24 * 3600


class Interrupted(Exception):
    pass


def interrupted(signum, frame):
    raise Interrupted(signum)


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


def report_schema():
    """The reviewer's report; workers and scouts answer in unchecked Markdown."""
    nonblank = {"type": "string", "pattern": r"\S"}
    finding = {"type": "object", "properties": {
        **{key: dict(nonblank) for key in ("severity", "path", "message", "evidence", "requirement")},
        "line": {"type": "integer", "minimum": 0}},
        "required": ["severity", "path", "line", "message", "evidence", "requirement"], "additionalProperties": False}
    properties = {"verdict": {"type": "string", "enum": ["PASS", "NEEDS_WORK", "INCONCLUSIVE"]},
                  "summary": nonblank, "findings": {"type": "array", "items": finding}}
    # Claude rejects top-level conditional schemas; keep cross-field rules
    # explicit for generation and enforce them locally before acceptance.
    properties["findings"]["description"] = "Must be empty for PASS and nonempty for NEEDS_WORK."
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def validate_report(report):
    # Reasons use schema-owned field names, never values or unknown provider keys.
    if not isinstance(report, dict):
        invalid("schema", "report must be an object")
    if report.keys() - {"summary", "verdict", "findings"}:
        invalid("schema", "report has unexpected fields")
    if not isinstance(report.get("summary"), str) or not report["summary"].strip():
        invalid("schema", "summary must be a nonblank string")
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


FENCED_JSON = re.compile(r"^ {0,3}```json[ \t]*\n(.*?)\n {0,3}```[ \t]*$", re.MULTILINE | re.DOTALL)


def report_json(text, reason="response is not valid JSON"):
    """Read a whole JSON response or the single fenced json block beside prose.

    External APIs do not always honour an output schema, and a model that wraps a
    valid report in narration has still done the paid work. Exactly one fenced
    block is unambiguous; anything else is refused, and the extracted object
    still passes validate_report, so no verdict is read from the prose itself.
    """
    try:
        return json.loads(text)
    except (ValueError, TypeError, RecursionError):
        pass
    blocks = FENCED_JSON.findall(text) if isinstance(text, str) else []
    if len(blocks) > 1:
        invalid("json", "response has more than one fenced JSON block")
    if not blocks:
        invalid("json", reason)
    try:
        return json.loads(blocks[0])
    except (ValueError, RecursionError):
        invalid("json", "fenced JSON block is not valid JSON")


def parse_result(provider, returncode, output, stderr, *, metadata=None, text=False):
    """The reviewer's JSON report, or with ``text`` a delegate's final message as written."""
    metadata = metadata if metadata is not None else {}
    try:
        result = json.loads(output)
    except (ValueError, TypeError, RecursionError):
        result = None
        if not returncode and not text:
            result = report_json(output)
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
    if text:
        if provider == "claude":
            if not isinstance(result, dict) or not isinstance(result.get("result"), str):
                invalid("envelope", "Claude envelope has no result text")
            metadata["response_source"] = "stdout.result"
            output = result["result"]
        if not output.strip():
            invalid("response", "the final message is empty")
        return output
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
                result = report_json(result, "result string is not valid JSON")
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
            **({"api_provider": identifier(route["api_provider"])} if route.get("api_provider") else {}),
            "fast": route.get("fast") if type(route.get("fast")) is bool else None,
            "role": identifier(role), "exit_code": None, "duration_seconds": None,
            "response_source": None, "stdout_bytes": None, "stderr_bytes": None,
            "result_bytes": None, "usage": None, "total_cost_usd": None}


def save_private(kind, suffix, write):
    """Keep at most 20 private artifacts of a kind for seven days; prune on each save.

    ``write(path, reference)`` creates the artifact; the returned reference is
    relative to workflow state, the form diagnostics name it by.
    """
    target = WORKFLOW_STATE / kind
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.chmod(0o700)
    # A blocking local lock keeps retention bounded even for overlapping scouts.
    fd = os.open(target / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        now = time.time()
        files = sorted(target.glob("*" + suffix), key=lambda path: path.lstat().st_mtime, reverse=True)
        for index, path in enumerate(files):
            if index >= DIAGNOSTIC_MAX_FILES - 1 or now - path.lstat().st_mtime > DIAGNOSTIC_MAX_AGE:
                path.unlink()
        ident = uuid.uuid4().hex + suffix
        reference = f"{kind}/{ident}"
        write(target / ident, reference)
        return reference
    finally:
        os.close(fd)


def save_failure(metadata, directory):
    def write(path, reference):
        streams = {}
        for name in ("stdout", "stderr", "result.json"):
            source = directory / name
            if source.exists():
                limit = STDERR_LIMIT if name == "stderr" else DIAGNOSTIC_STREAM_LIMIT
                with source.open("rb") as stream:
                    data = stream.read(limit)
                streams[name] = {"base64": base64.b64encode(data).decode("ascii"),
                                 "truncated": source.stat().st_size > len(data)}
        atomic_write_json(path, {"metadata": {**metadata, "artifact": reference},
                                 "created_at": time.time(), "streams": streams}, 0o600)
    return save_private("provider-failures", ".json", write)


def check_settings(route):
    """Reject known unsupported requests before preparation or launch."""
    if route.get("provider") not in ("codex", "claude"):
        raise ProviderFailure("Unsupported managed provider", "unsupported")
    if not route.get("model") or not route.get("effort"):
        raise ProviderFailure(
            "Managed execution requires known model and effort; for untiered work pass verified "
            "--session-model and --session-effort before dispatch",
            "unsupported",
        )
    from .fast_mode import configuration_error
    error = configuration_error(route["provider"], route)
    if error:
        raise ProviderFailure(error, "unsupported")
    if route["provider"] == "claude" and route.get("fast", False):
        from .external_launch import builtin_environment
        environment = builtin_environment(os.environ)
        blocked = [name for name in ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
                                    "CLAUDE_CODE_DISABLE_FAST_MODE") if environment.get(name) not in (None, "", "0", "false")]
        if blocked:
            raise ProviderFailure("Claude fast is unsupported with " + ", ".join(blocked), "unsupported")


def preflight(route):
    """Check local launch facts without guessing live model or account availability."""
    check_settings(route)
    if route.get("api_provider"):
        from .config import effective_config
        from .external_launch import check_model_effort, check_route
        config = effective_config()
        check_route(config, route)
        try:
            check_model_effort(config, route)
        except AgentBoxError as exc:
            raise ProviderFailure(str(exc), 'unsupported') from None
    if not shutil.which(route["provider"]):
        raise ProviderFailure(f"Cannot find {route['provider']} CLI", "unavailable")


def build_command(route, role, root, schema_path, result_path, read_paths=()):
    """``schema_path`` is None for a delegate, whose final message is free Markdown."""
    check_settings(route)
    readonly = role != "worker"
    if route["provider"] == "codex":
        command = ["codex", "exec", "--ephemeral", "--ignore-user-config", "--json", "-C", str(root),
                   "--sandbox", "read-only" if readonly else "workspace-write", "--model", route["model"]]
        if route["effort"] != "default":
            command += ["-c", 'model_reasoning_effort=' + toml_string(route["effort"])]
        if route.get("fast", False):
            command += ["-c", 'service_tier="fast"', "-c", "features.fast_mode=true"]
        command += [*([] if readonly else ["-c", "sandbox_workspace_write.network_access=true"]),
                    *(["--output-schema", str(schema_path)] if schema_path else []), "-o", str(result_path), "-"]
        return command
    # Excluding Bash prevents a read-only reviewer from gaining shell write access.
    command = ["claude", "-p", "--model", route["model"]]
    if route.get("fast", False):
        command += ["--settings", '{"fastMode":true}']
    if route["effort"] != "default":
        command += ["--effort", route["effort"]]
    for path in read_paths:
        command += ["--add-dir", str(path)]
    command += ["--output-format", "json", "--no-session-persistence", "--safe-mode", "--strict-mcp-config",
               "--mcp-config", '{"mcpServers":{}}', "--permission-mode", "dontAsk",
               "--tools", "Read,Grep,Glob" if readonly else "Read,Grep,Glob,Bash,Edit,Write",
               "--allowedTools", "Read,Grep,Glob" if readonly else "Read,Grep,Glob,Bash,Edit,Write"]
    if schema_path and schema_path.exists():
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


def run(route, role, root, prompt, *, timeout=3600, read_paths=(), diagnostics=None, on_completed=None):
    """A reviewer returns its validated report; a worker or scout its Markdown text."""
    from .external_launch import builtin_environment, prepare
    env = builtin_environment(os.environ)
    for key in ("CLAUDE_CODE_SUBAGENT_MODEL", "CLAUDE_CODE_EFFORT_LEVEL", "CLAUDECODE"):
        env.pop(key, None)
    from .notify import SUPPRESS_NOTIFY_ENV
    env[SUPPRESS_NOTIFY_ENV] = "1"
    metadata = failure_metadata(route, role)
    started = time.monotonic()
    process = None
    with tempfile.TemporaryDirectory(prefix="agent-box-run-") as directory:
        directory = Path(directory)
        reviewer = role == "reviewer"
        schema, result = (directory / "schema.json" if reviewer else None), directory / "result.json"
        if reviewer:
            schema.write_text(json.dumps(report_schema(), ensure_ascii=False), encoding="utf-8")
        def record(exc):
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

        try:
            command = build_command(route, role, Path(root), schema, result, read_paths)
            if route.get("api_provider"):
                from .config import effective_config
                from .external_launch import check_model_effort, check_route
                api_config = effective_config()
                check_route(api_config, route)
                try:
                    check_model_effort(api_config, route)
                except AgentBoxError as exc:
                    raise ProviderFailure(str(exc), 'unsupported') from None
                command, env = prepare(command, route["api_provider"], route["provider"], api_config,
                                       directory, env, model=route["model"])
        except ProviderFailure as exc:
            # A refusal before launch has no output, but it still gets an artifact
            # naming the refused settings, like every other provider failure.
            exc.stage, exc.reason = "preflight", str(exc)
            record(exc)
            raise
        with (directory / "stdout").open("w+") as stdout, (directory / "stderr").open("w+") as stderr:
            try:
                # The CLI runs in its own session, so a signal to this process
                # alone would leave it spending unobserved; the default SIGTERM
                # would also skip every failure record and cleanup.
                handlers = {}
                try:
                    if threading.current_thread() is threading.main_thread():
                        handlers = {signum: signal.signal(signum, interrupted) for signum in (signal.SIGTERM, signal.SIGHUP)}
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
                except (Interrupted, KeyboardInterrupt):
                    if process is not None:
                        with contextlib.suppress(ProcessLookupError):
                            os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                    raise ProviderFailure("Managed execution was interrupted by a signal before a provider response",
                                          "interrupted") from None
                finally:
                    for signum, handler in handlers.items():
                        signal.signal(signum, handler)
                stdout.flush()
                stderr.flush()
                if route["provider"] == "codex" and not reviewer and not result.exists() and not process.returncode:
                    invalid("response", "Codex wrote no final message")
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
                report = parse_result(route["provider"], process.returncode, output,
                                      error.decode("utf-8", errors="replace"), metadata=metadata, text=not reviewer)
                if reviewer:
                    report = validate_report(report)
                if route["provider"] == "codex":
                    metadata.update(codex_usage(directory / "stdout"))
                if diagnostics is not None:
                    diagnostics.update({key: metadata[key] for key in ("usage", "total_cost_usd")})
                if on_completed is not None:
                    on_completed(report)
                return report
            except ProviderFailure as exc:
                stdout.flush()
                stderr.flush()
                record(exc)
                raise
