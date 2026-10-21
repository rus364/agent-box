"""Optional installed-CLI transport probe; only a scripted loopback API answers.

Run PROVIDER MODE BYTES [KIND], for example: codex code 4096 unicode.
Providers: codex, claude. Modes: stdin, bash, code (Codex), read (Claude),
native_input, native. Kinds: ascii, unicode, longline. Captures and a summary
are kept in a new temporary directory. No saved account or external model is used.
The scripted tools only read the generated fixture file. Sandbox bypass in this
probe isolates transport from nested-sandbox setup; it is not role-policy evidence.
"""

import argparse
import http.server
import json
import os
from pathlib import Path
import subprocess
import re
import shlex
import tempfile
import threading


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def content(size, kind="ascii"):
    row = (
        'Текст 日本語 🙂 ``` "quoted" $()\\n\n'
        if kind == "unicode"
        else "0123456789abcdef" * 4 + ("\n" if kind != "longline" else "")
    )
    prefix = "FIRST_BOUNDARY_94a7\n"
    suffix = "\nLAST_BOUNDARY_6f31"
    available = size - len(prefix) - len(suffix)
    body = (
        (row * (size // len(row) + 1))
        .encode()[:available]
        .decode("utf-8", errors="ignore")
    )
    return prefix + body + suffix


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"data":[]}')

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        request = json.loads(raw)
        state = self.server.state
        state["requests"].append({"path": self.path, "body": request})
        if self.path.endswith("count_tokens"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"input_tokens":1000}')
            return
        call = state["calls"]
        state["calls"] += 1
        if state["provider"] == "claude":
            if call == 0 and state["mode"] != "stdin":
                tool = {
                    "type": "tool_use",
                    "id": "toolu_fixture",
                    "name": "Bash",
                    "input": {
                        "command": f"cat {shlex.quote(str(state['payload']))}",
                        "description": "Read a bounded local transport fixture",
                    },
                }
                if state["mode"] == "read":
                    tool.update(name="Read", input={"file_path": str(state["payload"])})
                if state["mode"].startswith("native"):
                    tool.update(
                        name="Agent",
                        input={
                            "subagent_type": "general-purpose",
                            "description": "Native return fixture",
                            "prompt": state["payload"].read_text()
                            if state["mode"] == "native_input"
                            else "CHILD_FIXTURE_ONLY: return the assigned fixture text.",
                        },
                    )
                reply = {
                    "id": "msg_fixture",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-haiku-4-5",
                    "content": [tool],
                    "stop_reason": "tool_use",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 1000, "output_tokens": 20},
                }
            else:
                reply = {
                    "id": "msg_done",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-haiku-4-5",
                    "content": [
                        {
                            "type": "text",
                            "text": state["payload"].read_text()
                            if state["mode"] == "native" and call == 1
                            else "FIXTURE_COMPLETE",
                        }
                    ],
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 1000, "output_tokens": 10},
                }
            self.send_response(200)
            if request.get("stream"):
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                events = [
                    (
                        "message_start",
                        {
                            "type": "message_start",
                            "message": {**reply, "content": [], "stop_reason": None},
                        },
                    )
                ]
                for index, block in enumerate(reply["content"]):
                    start = (
                        {**block, "text": ""}
                        if block["type"] == "text"
                        else {**block, "input": {}}
                    )
                    events.append(
                        (
                            "content_block_start",
                            {
                                "type": "content_block_start",
                                "index": index,
                                "content_block": start,
                            },
                        )
                    )
                    delta = (
                        {"type": "text_delta", "text": block["text"]}
                        if block["type"] == "text"
                        else {
                            "type": "input_json_delta",
                            "partial_json": json.dumps(block["input"]),
                        }
                    )
                    events += [
                        (
                            "content_block_delta",
                            {
                                "type": "content_block_delta",
                                "index": index,
                                "delta": delta,
                            },
                        ),
                        (
                            "content_block_stop",
                            {"type": "content_block_stop", "index": index},
                        ),
                    ]
                events += [
                    (
                        "message_delta",
                        {
                            "type": "message_delta",
                            "delta": {
                                "stop_reason": reply["stop_reason"],
                                "stop_sequence": None,
                            },
                            "usage": {"output_tokens": 20},
                        },
                    ),
                    ("message_stop", {"type": "message_stop"}),
                ]
                for name, event in events:
                    self.wfile.write(
                        (
                            "event: " + name + "\ndata: " + json.dumps(event) + "\n\n"
                        ).encode()
                    )
            else:
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(reply).encode())
            return
        tools = request.get("tools", [])
        flat = []
        for tool in tools:
            if tool.get("type") == "namespace":
                flat.extend(
                    (tool["name"] + "." + t["name"], t) for t in tool.get("tools", [])
                )
            elif "name" in tool:
                flat.append((tool["name"], tool))
        if call == 0 and state["mode"] != "stdin":
            name = next(
                (n for n, t in flat if n.endswith("exec_command")), "exec_command"
            )
            arguments = {
                "cmd": f"cat {shlex.quote(str(state['payload']))}",
                "max_output_tokens": 10000,
            }
            item = {
                "id": "fc_fixture",
                "type": "function_call",
                "call_id": "call_fixture",
                "name": name,
                "arguments": json.dumps(arguments),
            }
            if state["mode"] == "code":
                item = {
                    "id": "fc_fixture",
                    "type": "custom_tool_call",
                    "call_id": "call_fixture",
                    "name": "exec",
                    "input": "text(await tools.exec_command("
                    + json.dumps(arguments)
                    + "))",
                }
        else:
            item = {
                "id": "msg_done",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {
                        "type": "output_text",
                        "text": "FIXTURE_COMPLETE",
                        "annotations": [],
                    }
                ],
            }
        if state["mode"].startswith("native"):
            original = "Perform the requested local fixture read and then finish."
            values = list(strings(request.get("input", [])))
            is_parent = any(original in value for value in values)
            if call == 0:
                item = {
                    "id": "fc_spawn",
                    "type": "function_call",
                    "call_id": "call_spawn",
                    "namespace": "collaboration",
                    "name": "spawn_agent",
                    "arguments": json.dumps(
                        {
                            "task_name": "fixture_child",
                            "fork_turns": "none",
                            "message": state["payload"].read_text()
                            if state["mode"] == "native_input"
                            else "CHILD_FIXTURE_ONLY: return the assigned fixture text.",
                        }
                    ),
                }
            elif not is_parent:
                item = {
                    "id": "msg_child",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {
                            "type": "output_text",
                            "text": state["payload"].read_text(),
                            "annotations": [],
                        }
                    ],
                }
            elif (
                any(state["payload"].read_text() in value for value in values)
                or call > 6
            ):
                item = {
                    "id": "msg_done",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "FIXTURE_COMPLETE",
                            "annotations": [],
                        }
                    ],
                }
            else:
                item = {
                    "id": "fc_wait_" + str(call),
                    "type": "function_call",
                    "call_id": "call_wait_" + str(call),
                    "namespace": "collaboration",
                    "name": "wait_agent",
                    "arguments": json.dumps({"timeout_ms": 10000}),
                }
        response = {
            "id": "resp_" + str(call),
            "object": "response",
            "status": "completed",
            "model": "gpt-6-luna",
            "output": [item],
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 20,
                "total_tokens": 1020,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 0},
            },
        }
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for event in [
            {
                "type": "response.created",
                "response": {**response, "status": "in_progress", "output": []},
            },
            {"type": "response.output_item.done", "output_index": 0, "item": item},
            {"type": "response.completed", "response": response},
        ]:
            self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())


def run(provider, mode, size, kind="ascii"):
    run_dir = Path(
        tempfile.mkdtemp(
            prefix=provider + "-" + mode + "-",
        )
    )
    payload = run_dir / "payload.txt"
    payload.write_text(content(size, kind), encoding="utf-8")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state = {
        "provider": provider,
        "mode": mode,
        "kind": kind,
        "calls": 0,
        "requests": [],
        "payload": payload,
    }
    server.state = state
    threading.Thread(target=server.serve_forever, daemon=True).start()
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("CODEX_", "AGENT_BOX_", "CLAUDE_", "ANTHROPIC_"))
    }
    # Local dummy auth and isolated homes prevent the fixture from using saved accounts.
    env.update(
        ANTHROPIC_API_KEY="local-fixture",
        ANTHROPIC_BASE_URL=f"http://127.0.0.1:{server.server_port}",
        CLAUDE_CONFIG_DIR=str(run_dir / "claude-home"),
        CODEX_HOME=str(run_dir / "codex-home"),
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
        DISABLE_TELEMETRY="1",
        DISABLE_ERROR_REPORTING="1",
        AGENT_BOX_SUPPRESS_NOTIFY="1",
    )
    for key in (
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "CLAUDECODE",
        "CLAUDE_CODE_SUBAGENT_MODEL",
    ):
        env.pop(key, None)
    (run_dir / "codex-home").mkdir()
    (run_dir / "claude-home").mkdir()
    if provider == "codex":
        # The local server requests only a fixed cat of our own payload. This probe
        # measures transport, avoiding nested sandbox setup as a separate variable.
        cmd = [
            "codex",
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--skip-git-repo-check",
            "--sandbox",
            "danger-full-access",
            "--json",
            "-C",
            str(run_dir),
            "--model",
            "gpt-6-luna",
            "-c",
            'model_provider="fixture"',
            "-c",
            'model_providers.fixture.name="Local fixture"',
            "-c",
            f'model_providers.fixture.base_url="http://127.0.0.1:{server.server_port}/v1"',
            "-c",
            'model_providers.fixture.wire_api="responses"',
            "-c",
            "model_providers.fixture.requires_openai_auth=false",
            "-c",
            "model_providers.fixture.supports_websockets=false",
            "-c",
            f"features.code_mode={str(mode == 'code').lower()}",
            "-o",
            str(run_dir / "final.txt"),
            "-",
        ]
    else:
        cmd = [
            "claude",
            "-p",
            "--model",
            "haiku",
            "--safe-mode",
            "--no-session-persistence",
            "--output-format",
            "json",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--permission-mode",
            "dontAsk",
            "--tools",
            "Read,Bash,Agent"
            if mode.startswith("native")
            else ("Read" if mode == "read" else "Read,Bash"),
            "--allowedTools",
            "Read,Bash,Agent"
            if mode.startswith("native")
            else ("Read" if mode == "read" else "Read,Bash"),
            "--max-turns",
            "3",
        ]
    prompt = (
        payload.read_text()
        if mode == "stdin"
        else "Perform the requested local fixture read and then finish."
    )
    try:
        result = subprocess.run(
            cmd,
            cwd=run_dir,
            env=env,
            input=prompt,
            text=True,
            capture_output=True,
            timeout=35,
        )
        status = {
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    except subprocess.TimeoutExpired as e:
        status = {"timeout": True, "stdout": str(e.stdout), "stderr": str(e.stderr)}
    finally:
        server.shutdown()
        server.server_close()
    (run_dir / "capture.json").write_text(
        json.dumps(
            {"state": {**state, "payload": str(payload)}, "process": status},
            ensure_ascii=False,
            indent=2,
        )
    )
    verdict = inspect_capture(state, status)
    version = subprocess.run(
        [provider, "--version"], env=env, capture_output=True, text=True, timeout=10
    )
    summary = {
        "provider": provider,
        "version": version.stdout.strip(),
        "mode": mode,
        "bytes": len(payload.read_bytes()),
        "calls": state["calls"],
        "run_dir": str(run_dir),
        **verdict,
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary), flush=True)
    return 0 if verdict["status"] in ("exact", "limited") else 1


def inspect_capture(state, process):
    """Only subsequent input proves tool delivery; scripted response text proves nothing."""
    mode = state["mode"]
    payload = state["payload"].read_text()
    requests = [
        r["body"] for r in state["requests"] if not r["path"].endswith("count_tokens")
    ]
    observed = []
    for index, request in enumerate(requests):
        source = request.get("input", request.get("messages", []))
        if mode == "stdin":
            if index == 0:
                observed.extend(strings(source))
            continue
        if mode == "native_input":
            # Only a child's user prompt counts, not the parent's scripted spawn arguments.
            for item in source:
                if item.get("role") == "user" or (
                    item.get("type") == "agent_message" and item.get("author") == "/root"
                ):
                    observed.extend(strings(item.get("content", [])))
            continue
        for item in source:
            if item.get("type") in ("function_call_output", "custom_tool_call_output"):
                observed.extend(strings(item.get("output", [])))
            for block in (
                item.get("content", []) if isinstance(item.get("content"), list) else []
            ):
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    observed.extend(strings(block.get("content", [])))
            # Codex delivers a native final answer as an injected user message.
            if mode == "native" and item.get("role") == "user" and index:
                observed.extend(strings(item.get("content", [])))
            if mode == "native" and item.get("type") == "agent_message" and item.get("recipient") == "/root":
                observed.extend(strings(item.get("content", [])))
    expanded = []
    for value in observed:
        expanded.append(value)
        try:
            expanded.extend(strings(json.loads(value)))
        except (ValueError, TypeError):
            pass
    if mode == "read":
        expanded += [re.sub(r"(?m)^\s*\d+[→\t]", "", text) for text in expanded]
    exact = any(payload in value for value in expanded)
    combined = "\n".join(expanded)
    setup = (
        "unsupported call",
        "unsupported custom tool",
        "error building bubblewrap",
        "not allowed",
        "permission denied",
        "tool permission",
    )
    limited = ("truncated output", "Output too large", "exceeds maximum allowed size")
    status = (
        "setup_error"
        if process.get("timeout")
        or process.get("returncode") != 0
        or any(s in combined for s in setup)
        else "exact"
        if exact
        else "limited"
        if any(s in combined for s in limited)
        else "incomplete"
    )
    return {
        "status": status,
        "exact": exact,
        "returncode": process.get("returncode"),
        "observed_fragments": len(observed),
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("provider", choices=("codex", "claude"))
    p.add_argument(
        "mode", choices=("stdin", "bash", "code", "read", "native", "native_input")
    )
    p.add_argument("size", type=int)
    p.add_argument(
        "kind", nargs="?", choices=("ascii", "unicode", "longline"), default="ascii"
    )
    args = p.parse_args()
    if args.size < 64 or args.size > 1048576:
        p.error("size must be between 64 and 1048576 bytes")
    if (args.mode == "code" and args.provider != "codex") or (
        args.mode == "read" and args.provider != "claude"
    ):
        p.error("code mode is Codex-only; read mode is Claude-only")
    raise SystemExit(run(args.provider, args.mode, args.size, args.kind))
