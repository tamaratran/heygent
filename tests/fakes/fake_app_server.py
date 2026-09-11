"""A stand-in for `codex app-server`, speaking just enough protocol.

The real one needs an account and a model; these tests need the wire.
Shapes are copied from `codex app-server generate-json-schema` and from
a live 0.151 session (see tests/test_codex_app.py).

argv[1] picks the script: "plain" answers, "approval" asks first,
"work" runs a command, edits a file and thinks before answering,
"unknown" sends a server request nothing handles, "silent" never
answers initialize.
"""

import json
import sys

MODE = sys.argv[1] if len(sys.argv) > 1 else "plain"
THREAD = "01a05536-5029-7f03-a98b-56096fe6a6b0"
TURN = "01a05536-50bf-76d0-b8b7-a5f2bf2f378b"


def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def notify(method, params):
    send({"jsonrpc": "2.0", "method": method, "params": params})


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        message = json.loads(line)
        method, mid = message.get("method"), message.get("id")
        if method == "initialize":
            if MODE == "silent":
                continue
            send({"jsonrpc": "2.0", "id": mid,
                  "result": {"codexHome": "/tmp/codex", "userAgent": "fake"}})
        elif method == "initialized":
            continue
        elif method == "thread/start":
            send({"jsonrpc": "2.0", "id": mid,
                  "result": {"thread": {"id": THREAD}, "model": "fake-model",
                             "cwd": (message.get("params") or {}).get("cwd"),
                             "approvalPolicy":
                                 (message.get("params") or {}).get("approvalPolicy")}})
        elif method == "thread/resume":
            send({"jsonrpc": "2.0", "id": mid,
                  "result": {"thread": {"id": (message["params"])["threadId"]},
                             "model": "fake-model"}})
        elif method == "turn/start":
            send({"jsonrpc": "2.0", "id": mid, "result": {"turn": {"id": TURN}}})
            run_turn(message["params"]["input"][0]["text"])
        elif method == "turn/interrupt":
            send({"jsonrpc": "2.0", "id": mid, "result": {}})
        elif mid is not None:
            send({"jsonrpc": "2.0", "id": mid, "result": {}})


def run_turn(prompt):
    notify("turn/started", {"threadId": THREAD, "turn": {"id": TURN}})
    if MODE == "unknown":
        send({"jsonrpc": "2.0", "id": 9001, "method": "item/tool/call",
              "params": {"itemId": "tool_1"}})
        answer = read_reply(9001)
        notify("item/completed", {"item": {"type": "agentMessage", "id": "m1",
                                           "text": f"refused: {answer}"}})
        notify("turn/completed", {"threadId": THREAD, "turn": {"id": TURN, "items": [
            {"type": "agentMessage", "id": "m1", "text": f"refused: {answer}"}]}})
        return
    if MODE == "approval":
        send({"jsonrpc": "2.0", "id": 9000,
              "method": "item/commandExecution/requestApproval",
              "params": {"itemId": "exec_1", "threadId": THREAD, "turnId": TURN,
                         "startedAtMs": 0, "command": "/bin/bash -lc 'date +%Y'",
                         "cwd": "/tmp/somewhere",
                         "reason": "outside the sandbox"}})
        decision = read_reply(9000)
        if decision != "accept":
            notify("item/completed", {"item": {"type": "agentMessage", "id": "m0",
                                               "text": f"not run ({decision})"}})
            notify("turn/completed", {"threadId": THREAD, "turn": {
                "id": TURN, "items": [{"type": "agentMessage", "id": "m0",
                                       "text": f"not run ({decision})"}]}})
            return
        notify("item/completed", {"item": {"type": "commandExecution", "id": "exec_1",
                                           "command": "/bin/bash -lc 'date +%Y'"}})
    if MODE == "work":
        notify("item/completed", {"item": {
            "type": "reasoning", "id": "r1",
            "text": "Which year?\nThe clock will say."}})
        notify("item/completed", {"item": {
            "type": "commandExecution", "id": "exec_9",
            "command": "date +%Y", "aggregatedOutput": "2026\n",
            "exitCode": 1}})
        notify("item/completed", {"item": {
            "type": "fileChange", "id": "patch_1",
            "changes": [{"path": "year.txt", "kind": "add",
                         "diff": "+2026"}]}})
        notify("item/completed", {"item": {
            "type": "todoList", "id": "plan_1",
            "items": [{"text": "read the clock", "completed": True},
                       {"text": "write it down", "completed": False}]}})
    for piece in ("2", "0", "2", "6"):
        notify("item/agentMessage/delta",
               {"threadId": THREAD, "turnId": TURN, "itemId": "m1", "delta": piece})
    notify("item/completed", {"item": {"type": "agentMessage", "id": "m1",
                                       "text": "2026"}})
    notify("turn/completed", {"threadId": THREAD, "turn": {"id": TURN, "items": [
        {"type": "agentMessage", "id": "m1", "text": "2026"}]}})


def read_reply(request_id):
    """Block for the client's answer, the way the real server does."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        message = json.loads(line)
        if message.get("id") != request_id:
            continue
        if "error" in message:
            return "unhandled"
        return (message.get("result") or {}).get("decision", "")
    return ""


if __name__ == "__main__":
    main()
