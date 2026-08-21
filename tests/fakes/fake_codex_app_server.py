#!/usr/bin/env python3
"""A fake `codex app-server` for integration-testing CodexProvider's actual
JSON-RPC/stdio orchestration, without a real Codex install.

It enforces the parts of the real protocol a client can get wrong:

* ``thread/start`` before the ``initialized`` notification is an error.
* ``item/tool/requestUserInput`` is EXPERIMENTAL, so it is only ever sent to a
  client that declared ``capabilities.experimentalApi = true`` on
  ``initialize``. A client that does not declare it gets no question at all --
  the same silent no-op the real server is free to produce -- so a test can
  detect a missing capability instead of assuming one.

Turn inputs understood: "ask" (one question), "ask2" (two questions in ONE
request), "cancel" (asks, then withdraws it with ``serverRequest/resolved``),
anything else (a text delta and turn completion).
"""
import json
import sys
import threading

ONE = [{"id": "q1", "header": "Confirm", "question": "Proceed?",
        "options": [{"label": "Yes", "description": ""}]}]
TWO = [
    {"id": "q1", "header": "Confirm", "question": "Proceed?",
     "options": [{"label": "Yes", "description": ""}]},
    {"id": "q2", "header": "Branch", "question": "Which branch?",
     "options": [{"label": "main", "description": ""}]},
]


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main():
    initialized_seen = False
    experimental = False
    request_counter = 900
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        method = obj.get("method")
        params = obj.get("params") or {}
        if method == "initialize":
            experimental = bool((params.get("capabilities") or {}).get("experimentalApi"))
            emit({"id": obj["id"], "result": {}})
        elif method == "initialized":
            initialized_seen = True
        elif method == "thread/start":
            if not initialized_seen:
                emit({"id": obj["id"], "error": {"message": "thread/start before initialized"}})
                continue
            emit({"id": obj["id"], "result": {"thread": {"id": "thread-fake"}}})
        elif method == "thread/resume":
            emit({"id": obj["id"], "result": {"thread": {"id": params["threadId"]}}})
        elif method == "turn/start":
            emit({"id": obj["id"], "result": {"turn": {"id": "turn-fake"}}})
            text = params["input"][0]["text"]
            request_counter += 1
            request_id = request_counter

            def respond(text=text, request_id=request_id, experimental=experimental):
                if text in ("ask", "ask2", "cancel"):
                    if not experimental:
                        # EXPERIMENTAL surface not opted into: the server
                        # simply never asks.
                        emit({"method": "turn/completed",
                              "params": {"threadId": "thread-fake", "turnId": "turn-fake"}})
                        return
                    emit({
                        "id": request_id, "method": "item/tool/requestUserInput",
                        "params": {
                            "threadId": "thread-fake", "turnId": "turn-fake", "itemId": "item-1",
                            "isBlocking": True,
                            "questions": TWO if text == "ask2" else ONE,
                        },
                    })
                    if text == "cancel":
                        emit({"method": "serverRequest/resolved",
                              "params": {"requestId": request_id, "threadId": "thread-fake"}})
                else:
                    emit({"method": "item/agentMessage/delta",
                          "params": {"delta": f"echo: {text}", "itemId": "i",
                                     "threadId": "thread-fake", "turnId": "turn-fake"}})
                    emit({"method": "turn/completed",
                          "params": {"threadId": "thread-fake", "turnId": "turn-fake"}})

            threading.Thread(target=respond, daemon=True).start()
        elif method == "turn/interrupt":
            emit({"id": obj["id"], "result": {}})
        elif "id" in obj and "result" in obj:
            # A client response to our server request (requestUserInput). Echo
            # it back as a text delta so a test can assert the exact aggregate
            # payload actually reached the server.
            emit({"method": "item/agentMessage/delta",
                  "params": {"delta": "answers:" + json.dumps(obj["result"], sort_keys=True),
                             "itemId": "i", "threadId": "thread-fake", "turnId": "turn-fake"}})
            emit({"method": "turn/completed",
                  "params": {"threadId": "thread-fake", "turnId": "turn-fake"}})


if __name__ == "__main__":
    main()
