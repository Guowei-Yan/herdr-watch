"""A stdio peer for exercising the bridge's real JSON-RPC transport."""
import json
import sys
from pathlib import Path

log = Path(sys.argv[1])
mode = sys.argv[2]
for line in sys.stdin:
    message = json.loads(line)
    with log.open("a", encoding="utf-8") as output:
        output.write(json.dumps(message) + "\n")
    if "id" not in message or "method" not in message:
        continue
    method = message["method"]
    if method == "initialize":
        result = {"userAgent": "codex/0.153.4", "codexHome": "/test",
                  "platformFamily": "unix", "platformOs": "linux"}
    elif method == "thread/read":
        result = {"thread": {"id": "thread-test", "status": {"type": mode}}}
    elif method == "turn/start":
        if mode == "reject":
            print(json.dumps({"id": message["id"], "error": {
                "code": -32602, "message": "toolOutput unavailable"}}), flush=True)
            continue
        result = {"turn": {"id": "turn-test", "status": "inProgress"}}
    else:
        raise AssertionError(method)
    print(json.dumps({"method": "thread/status/changed", "params": {}}), flush=True)
    print(json.dumps({"id": message["id"], "result": result}), flush=True)
    if method == "turn/start" and mode == "disconnect":
        break
    if method == "turn/start" and mode == "idle-events":
        for _ in range(1000):
            print(json.dumps({"method": "item/agentMessage/delta", "params": {"delta": "x"}}))
        print(json.dumps({"id": "approval-test", "method": "item/commandExecution/requestApproval",
                          "params": {}}), flush=True)
