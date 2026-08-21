#!/usr/bin/env python3
"""A fake `codex app-server` for integration-testing CodexProvider's actual
JSON-RPC/stdio orchestration (handshake ordering, turn streaming, and a
server-initiated request_user_input round trip), without a real Codex
installation or credentials.
"""
import json
import sys
import threading


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main():
    initialized_seen = False
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        method = obj.get("method")
        if method == "initialize":
            emit({"id": obj["id"], "result": {}})
        elif method == "initialized":
            initialized_seen = True
        elif method == "thread/start":
            if not initialized_seen:
                emit({"id": obj["id"], "error": {"message": "thread/start before initialized"}})
                continue
            emit({"id": obj["id"], "result": {"thread": {"id": "thread-fake"}}})
        elif method == "thread/resume":
            emit({"id": obj["id"], "result": {"thread": {"id": obj["params"]["threadId"]}}})
        elif method == "turn/start":
            emit({"id": obj["id"], "result": {"turn": {"id": "turn-fake"}}})
            text = obj["params"]["input"][0]["text"]

            def respond():
                if text == "ask":
                    emit({
                        "id": 999, "method": "item/tool/requestUserInput",
                        "params": {
                            "threadId": "thread-fake", "turnId": "turn-fake", "itemId": "item-1",
                            "isBlocking": True,
                            "questions": [{"id": "q1", "header": "Confirm", "question": "Proceed?",
                                           "options": [{"label": "Yes", "description": ""}]}],
                        },
                    })
                else:
                    emit({"method": "item/agentMessage/delta",
                          "params": {"delta": f"echo: {text}", "itemId": "i", "threadId": "thread-fake", "turnId": "turn-fake"}})
                    emit({"method": "turn/completed", "params": {"threadId": "thread-fake", "turnId": "turn-fake"}})

            threading.Thread(target=respond, daemon=True).start()
        elif method == "turn/interrupt":
            emit({"id": obj["id"], "result": {}})
        elif "id" in obj and "result" in obj:
            pass  # a client response to our server request (request_user_input)


if __name__ == "__main__":
    main()
