#!/usr/bin/env python3
"""A fake `claude` executable for integration-testing ClaudeProvider's
actual subprocess/stdio orchestration, without needing a real Claude Code
installation or credentials.

Protocol (a simplified but structurally real subset of stream-json):
  - reads one JSON object per line from stdin (type "user")
  - if the message text is "ask", emits an assistant tool_use block calling
    AskUserQuestion, then waits for a tool_result reply before continuing
  - otherwise emits an assistant text block echoing the input, then a
    "result" event marking the turn done
"""
import json
import sys


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main():
    sys.argv  # noqa: B018 (argv intentionally unused; real claude parses flags)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        content = (obj.get("message") or {}).get("content") or []
        for block in content:
            if block.get("type") == "text":
                text = block.get("text", "")
                if text == "ask":
                    emit({
                        "type": "assistant",
                        "message": {"role": "assistant", "content": [{
                            "type": "tool_use", "id": "toolu_fake", "name": "AskUserQuestion",
                            "input": {"questions": [{
                                "question": "Continue?", "header": "Confirm",
                                "options": [{"label": "Yes", "description": ""}, {"label": "No", "description": ""}],
                            }]},
                        }]},
                    })
                else:
                    emit({
                        "type": "assistant",
                        "message": {"role": "assistant", "content": [{"type": "text", "text": f"echo: {text}"}]},
                    })
                    emit({"type": "result"})
            elif block.get("type") == "tool_result":
                emit({
                    "type": "assistant",
                    "message": {"role": "assistant", "content": [
                        {"type": "text", "text": f"received answer: {block.get('content')}"}
                    ]},
                })
                emit({"type": "result"})


if __name__ == "__main__":
    main()
