"""Opt-in live test: a Git event wakes an idle Codex conversation.

Run against an isolated, already-running local App Server. This invokes a model twice
and creates a temporary Git repository; it never commits in the source checkout.
"""
import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from hw_codex_rpc import CodexRpc


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Guowei",
                    "-c", "user.email=guowei@example.invalid", *args],
                   check=True, capture_output=True, text=True)


def complete_turn(client):
    deadline = time.monotonic() + 55
    reply = ""
    while time.monotonic() < deadline:
        event = client.replies.get(timeout=max(0.1, deadline - time.monotonic()))
        if event is None:
            raise RuntimeError("Server disconnected")
        if event.get("method") == "item/agentMessage/delta":
            reply += event["params"]["delta"]
        if event.get("method") == "turn/completed":
            turn = event["params"]["turn"]
            if turn["status"] != "completed":
                raise RuntimeError("Model turn failed: " + str(turn.get("error")))
            if "WATCHER_EVENT_RECEIVED" not in reply:
                raise RuntimeError("Expected acknowledgement, received: " + reply)
            print("MODEL_ACKNOWLEDGED", flush=True)
            return
    raise RuntimeError("No model acknowledgement")


def cli(*args):
    command = [sys.executable, "-B", str(SCRIPTS / "codex_monitor.py"), *args]
    result = subprocess.run(command, capture_output=True, text=True, timeout=50)
    if result.returncode:
        raise RuntimeError(result.stderr)
    return json.loads(result.stdout)


def exercise(client, url, folder):
    repo = folder / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "commit", "--allow-empty", "-m", "test: initialise monitor fixture")
    result = client.request("thread/start", {
        "model": "gpt-5.6-sol", "ephemeral": True, "cwd": str(folder),
        "approvalPolicy": "never", "sandbox": "read-only",
        "baseInstructions": "This is a notification test. Reply to each tool output with "
        "exactly WATCHER_EVENT_RECEIVED. Do not call tools.",
        "config": {"mcp_servers": {}, "model_reasoning_effort": "high"}})
    thread = result["thread"]["id"]
    print("TEST_THREAD", thread, flush=True)
    monitor = cli("start", "--state-dir", str(folder / "state"),
                  "--codex-url", url, "--thread", thread, "--", "watch-git",
                  "--repo", str(repo), "--interval", "5s", "--label", "live-smoke")
    print("DETACHED_MONITOR_ARMED", monitor["id"], flush=True)
    try:
        complete_turn(client)
        git(repo, "commit", "--allow-empty", "-m", "test: trigger background notification")
        complete_turn(client)
        print("IDLE_THREAD_WOKEN_BY_GIT_EVENT", flush=True)
    finally:
        cli("stop", "--state-dir", str(folder / "state"), "--id", monitor["id"])
        confirm_stopped(folder / "state", monitor["id"])


def confirm_stopped(root, ident):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        records = cli("list", "--state-dir", str(root))
        status = next(record["status"] for record in records if record["id"] == ident)
        if status == "stopped":
            print("MONITOR_STOPPED", flush=True)
            return
        time.sleep(0.1)
    raise RuntimeError("Monitor did not stop: " + status)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    args = parser.parse_args()
    folder = Path(tempfile.mkdtemp(prefix="herdr-watch-live-"))
    print("TEST_ARTIFACTS", folder, flush=True)
    with CodexRpc(args.url, retain_notifications=True) as client:
        exercise(client, args.url, folder)
