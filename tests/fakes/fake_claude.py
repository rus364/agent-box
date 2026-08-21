#!/usr/bin/env python3
"""A fake `claude` executable for integration-testing ClaudeProvider's actual
subprocess/stdio orchestration, without a real Claude Code install.

This fake implements the *real* mechanism, not a convenient one. Specifically:

* It refuses to run without ``--permission-prompt-tool stdio``. That flag is
  what the Agent SDK passes when a ``canUseTool`` callback is configured, and
  it is what makes the CLI route ``AskUserQuestion`` to the client instead of
  resolving it internally. A previous version of this fake accepted a
  ``tool_result`` block on stdin as an answer to ``AskUserQuestion`` --
  something the real CLI never does, since ``AskUserQuestion`` is a built-in
  tool the CLI executes itself. That made a broken adapter look correct.
* It asks via a ``can_use_tool`` ``control_request`` carrying
  ``requires_user_interaction: true`` and only accepts a ``control_response``
  with ``behavior: "allow"`` and the answers in ``updatedInput["answers"]``,
  keyed by question text -- the shape captured from a live Claude Code
  2.1.239 session.

Protocol implemented (a structurally real subset of stream-json):
  - "ask"   -> one AskUserQuestion permission request with one question
  - "ask2"  -> one AskUserQuestion permission request with two questions
  - "multi" -> one AskUserQuestion permission request with a multiSelect question
  - "cancel"-> asks, then immediately withdraws via control_cancel_request
  - anything else -> an assistant text echo plus a "result" turn end
"""
import json
import sys

ONE = [{"question": "Continue?", "header": "Confirm",
        "options": [{"label": "Yes", "description": ""}, {"label": "No", "description": ""}]}]
TWO = [
    {"question": "Continue?", "header": "Confirm",
     "options": [{"label": "Yes", "description": ""}, {"label": "No", "description": ""}]},
    {"question": "Which branch?", "header": "Branch",
     "options": [{"label": "main", "description": ""}, {"label": "dev", "description": ""}]},
]
# multiSelect is a real field of the AskUserQuestion input schema; the fake
# carries it through so the adapter's handling of it is actually exercised.
MULTI = [
    {"question": "Which files?", "header": "Files", "multiSelect": True,
     "options": [{"label": "a.py", "description": ""}, {"label": "b.py", "description": ""}]},
]


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def require_stdio_permission_surface(argv):
    if "--permission-prompt-tool" not in argv:
        emit({"type": "error", "error": "no permission prompt surface: refusing to ask"})
        return False
    if argv[argv.index("--permission-prompt-tool") + 1] != "stdio":
        emit({"type": "error", "error": "unsupported permission prompt tool"})
        return False
    return True


def ask(argv, questions, request_id, cancel=False):
    if not require_stdio_permission_surface(argv):
        emit({"type": "result"})
        return
    emit({"type": "assistant", "message": {"role": "assistant", "content": [{
        "type": "tool_use", "id": "toolu_fake", "name": "AskUserQuestion",
        "input": {"questions": questions},
    }]}})
    emit({"type": "control_request", "request_id": request_id, "request": {
        "subtype": "can_use_tool", "tool_name": "AskUserQuestion",
        "input": {"questions": questions}, "tool_use_id": "toolu_fake",
        "requires_user_interaction": True,
    }})
    if cancel:
        emit({"type": "control_cancel_request", "request_id": request_id})


def main():
    argv = sys.argv[1:]
    open_requests = {}
    counter = 0
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        if obj.get("type") == "control_response":
            response = obj.get("response") or {}
            request_id = response.get("request_id")
            questions = open_requests.pop(request_id, None)
            payload = response.get("response") or {}
            if questions is None or payload.get("behavior") != "allow":
                emit({"type": "error", "error": "unexpected control_response"})
                emit({"type": "result"})
                continue
            answers = (payload.get("updatedInput") or {}).get("answers") or {}
            # Mirror the real CLI: the answer reaches the model as a
            # tool_result the *CLI* synthesizes from updatedInput.answers.
            rendered = ", ".join(f'"{q}"="{answers.get(q, "")}"' for q in (x["question"] for x in questions))
            emit({"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": f"received answer: {rendered}"}
            ]}})
            emit({"type": "result"})
            continue

        content = (obj.get("message") or {}).get("content") or []
        for block in content:
            if block.get("type") != "text":
                continue
            text = block.get("text", "")
            if text in ("ask", "ask2", "multi", "cancel"):
                counter += 1
                request_id = f"req-{counter}"
                questions = {"ask2": TWO, "multi": MULTI}.get(text, ONE)
                open_requests[request_id] = questions
                ask(argv, questions, request_id, cancel=(text == "cancel"))
            else:
                emit({"type": "assistant", "message": {"role": "assistant", "content": [
                    {"type": "text", "text": f"echo: {text}"}
                ]}})
                emit({"type": "result"})


if __name__ == "__main__":
    main()
