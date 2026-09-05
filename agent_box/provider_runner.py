"""Fresh CLI contexts with explicit models and machine-checked reports."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
from pathlib import Path

from .errors import AgentBoxError


class ProviderFailure(AgentBoxError):
    def __init__(self, message, category="provider-error"):
        super().__init__(message)
        self.category = category


def report_schema(role):
    if role == "reviewer":
        finding = {"type": "object", "properties": {
            **{key: {"type": "string"} for key in ("severity", "path", "message", "evidence", "requirement")},
            "line": {"type": "integer"}}, "required": ["severity", "path", "line", "message", "evidence", "requirement"], "additionalProperties": False}
        properties = {"verdict": {"type": "string", "enum": ["PASS", "NEEDS_WORK", "INCONCLUSIVE"]},
                      "summary": {"type": "string"}, "findings": {"type": "array", "items": finding}}
    else:
        properties = {"status": {"type": "string", "enum": ["completed", "blocked", "needs-human"]},
                      "summary": {"type": "string"}, "evidence": {"type": "array", "items": {"type": "string"}}}
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def validate_report(report, role):
    invalid = not isinstance(report, dict) or not isinstance(report.get("summary"), str) or not report.get("summary", "").strip()
    if role == "reviewer":
        invalid |= not isinstance(report, dict) or report.get("verdict") not in ("PASS", "NEEDS_WORK", "INCONCLUSIVE")
        findings = report.get("findings") if isinstance(report, dict) else None
        invalid |= not isinstance(findings, list)
        for finding in findings if isinstance(findings, list) else []:
            if not isinstance(finding, dict):
                invalid = True
                continue
            invalid |= any(not isinstance(finding.get(k), str) or not finding[k].strip()
                           for k in ("severity", "path", "message", "evidence", "requirement"))
            invalid |= not isinstance(finding.get("line"), int) or finding.get("line", -1) < 0
        if isinstance(report, dict):
            invalid |= report.get("verdict") == "PASS" and bool(findings)
            invalid |= report.get("verdict") == "NEEDS_WORK" and not findings
    else:
        invalid |= not isinstance(report, dict) or report.get("status") not in ("completed", "blocked", "needs-human")
        evidence = report.get("evidence") if isinstance(report, dict) else None
        invalid |= not isinstance(evidence, list) or any(not isinstance(v, str) for v in evidence or [])
    if invalid:
        raise ProviderFailure("Provider returned an invalid report; no acceptance recorded", "invalid-report")
    return report


def parse_result(provider, returncode, output, stderr):
    try:
        result = json.loads(output)
    except (ValueError, TypeError):
        result = None
    if returncode or isinstance(result, dict) and result.get("is_error"):
        detail = str(result.get("result", "")) if isinstance(result, dict) else stderr
        lowered = detail.lower()
        category = "provider-error"
        if isinstance(result, dict) and result.get("api_error_status") == 404 or "model" in lowered and any(s in lowered for s in ("not found", "unavailable", "not supported", "does not exist")):
            category = "model-unavailable"
        elif isinstance(result, dict) and result.get("api_error_status") in (401, 403) or any(s in lowered for s in ("unauthorized", "authentication", "authenticate", "login")):
            category = "authentication"
        elif any(s in lowered for s in ("rate limit", "quota", "usage limit")):
            category = "quota"
        raise ProviderFailure(f"{provider} failed ({category}, exit {returncode}); no task verdict", category)
    if provider == "claude" and isinstance(result, dict):
        result = result.get("structured_output", result.get("result", result))
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except ValueError:
                result = None
    if not isinstance(result, dict):
        raise ProviderFailure(f"{provider} did not return a JSON report", "invalid-report")
    return result


def build_command(route, role, root, schema_path, result_path):
    readonly = role != "worker"
    if route["provider"] == "codex":
        return ["codex", "exec", "--ephemeral", "--ignore-user-config", "--json", "-C", str(root),
                "--sandbox", "read-only" if readonly else "workspace-write", "--model", route["model"],
                "-c", 'model_reasoning_effort=' + json.dumps(route["effort"]),
                *([] if readonly else ["-c", "sandbox_workspace_write.network_access=true"]),
                "--output-schema", str(schema_path), "-o", str(result_path), "-"]
    # Excluding Bash prevents a read-only reviewer from gaining shell write access.
    command = ["claude", "-p", "--model", route["model"], "--effort", route["effort"],
               "--output-format", "json", "--no-session-persistence", "--safe-mode", "--strict-mcp-config",
               "--mcp-config", '{"mcpServers":{}}', "--permission-mode", "dontAsk",
               "--tools", "Read,Grep,Glob" if readonly else "Read,Grep,Glob,Bash,Edit,Write",
               "--allowedTools", "Read,Grep,Glob" if readonly else "Read,Grep,Glob,Bash,Edit,Write"]
    if schema_path.exists():
        command += ["--json-schema", schema_path.read_text()]
    return command


def run(route, role, root, prompt, *, timeout=3600, task=None):
    env = os.environ.copy()
    for key in ("CLAUDE_CODE_SUBAGENT_MODEL", "CLAUDE_CODE_EFFORT_LEVEL", "CLAUDECODE"):
        env.pop(key, None)
    from .notify import SUPPRESS_NOTIFY_ENV
    env[SUPPRESS_NOTIFY_ENV] = "1"
    env.update(AGENT_BOX_ROLE=role, AGENT_BOX_ASSIGNED_TASK=str(task or ""))
    with tempfile.TemporaryDirectory(prefix="agent-box-run-") as directory:
        directory = Path(directory)
        schema, result = directory / "schema.json", directory / "result.json"
        schema.write_text(json.dumps(report_schema(role)))
        command = build_command(route, role, Path(root), schema, result)
        with (directory / "stdout").open("w+") as stdout, (directory / "stderr").open("w+") as stderr:
            try:
                process = subprocess.Popen(command, cwd=root, env=env, stdin=subprocess.PIPE, stdout=stdout,
                                           stderr=stderr, text=True, start_new_session=True)
                try:
                    process.communicate(prompt, timeout=timeout)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                    raise ProviderFailure("Provider timed out; inspect work before retrying", "timeout") from None
            except OSError:
                raise ProviderFailure(f"Cannot start {route['provider']} CLI", "unavailable") from None
            stdout.seek(0)
            stderr.seek(0)
            output = result.read_text() if result.exists() else stdout.read(4 * 1024 * 1024)
            return validate_report(parse_result(route["provider"], process.returncode, output, stderr.read(65536)), role)
