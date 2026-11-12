"""Shared helpers: herdr access, pane reading, event printing, durations.

Standard library only, so the tool runs on any platform with Python. The only
external programs used anywhere in herdr-watch are `herdr` and `git`, both of
which ship cross-platform binaries.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

HERDR = os.environ.get("HERDR_BIN_PATH") or "herdr"

# Exit codes shared by every subcommand.
EXIT_USAGE, EXIT_NO_MATCH, EXIT_AMBIGUOUS, EXIT_NO_SERVER = 2, 3, 4, 5


class Fail(Exception):
    """Fatal, user-facing error carrying an exit code."""

    def __init__(self, message: str, code: int = EXIT_USAGE):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------- durations

_DUR = re.compile(r"^(\d+)([smh]?)$")
_MULT = {"": 1, "s": 1, "m": 60, "h": 3600}


def duration(value: str) -> int:
    """Parse 45s / 15m / 1h / plain seconds into seconds."""
    m = _DUR.match(str(value).strip())
    if not m:
        raise Fail("bad duration: %s (use 45s, 15m, 1h or seconds)" % value)
    return int(m.group(1)) * _MULT[m.group(2)]


def fmt_duration(seconds: int) -> str:
    if seconds >= 3600:
        return "%dh%02dm" % (seconds // 3600, seconds % 3600 // 60)
    if seconds >= 60:
        return "%dm%02ds" % (seconds // 60, seconds % 60)
    return "%ds" % seconds


def clock() -> str:
    return time.strftime("%H:%M:%S")


# ------------------------------------------------------------------- events


class Emitter:
    """One stdout line per event; the Monitor tool turns each into a notification."""

    def __init__(self, label: str):
        self.label = label

    def event(self, message: str) -> None:
        print("[herdr-watch %s] %s" % (self.label, message), flush=True)

    def payload(self, text: str) -> None:
        for line in text.splitlines():
            print("    " + line, flush=True)


# -------------------------------------------------------------- herdr calls


def run(argv: list[str], timeout: float | None = 60, env: dict | None = None) -> str:
    """Run a command, returning stdout+stderr. Never raises on non-zero exit."""
    merged = dict(os.environ)
    if env:
        merged.update(env)
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, env=merged
        )
    except FileNotFoundError:
        raise Fail("command not found: %s" % argv[0], EXIT_USAGE)
    except subprocess.TimeoutExpired:
        return ""
    return (proc.stdout or "") + (proc.stderr or "")


def run_status(argv: list[str], timeout: float | None = 60) -> int:
    """Run a command only for its exit status."""
    try:
        return subprocess.run(argv, capture_output=True, timeout=timeout).returncode
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 1


def herdr(args: list[str], socket: str | None = None, timeout: float | None = 60) -> dict:
    """Call the herdr CLI and parse its JSON reply. {} when it is not JSON."""
    env = {"HERDR_SOCKET_PATH": socket} if socket else None
    out = run([HERDR] + args, timeout=timeout, env=env).strip()
    if not out:
        return {}
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return {}


def status_of(reply: dict) -> str:
    """Reduce a herdr reply to a status word, or its error code."""
    if not reply:
        return "parse-error"
    if "error" in reply:
        return reply["error"].get("code", "error")
    return (reply.get("result", {}).get("agent", {}) or {}).get("agent_status", "unknown")


def sessions() -> list[dict]:
    """Running herdr sessions as [{name, socket_path}, ...]."""
    reply = herdr(["session", "list", "--json"])
    return [s for s in reply.get("sessions", []) if s.get("running")]


def session_name(socket: str) -> str:
    for s in sessions():
        if s.get("socket_path") == socket:
            return s.get("name", "custom")
    return "custom"


def agents_on(socket: str, name: str) -> list[dict]:
    reply = herdr(["agent", "list"], socket=socket)
    out = []
    for a in reply.get("result", {}).get("agents", []) or []:
        a = dict(a)
        a["session"] = name
        a["socket"] = socket
        out.append(a)
    return out


def agent_get(socket: str, pane: str) -> dict:
    return herdr(["agent", "get", pane], socket=socket)


def agent_wait(socket: str, pane: str, until: list[str], timeout_ms: int) -> str:
    """Block server-side until the agent reaches one of `until`. Returns a status word."""
    args = ["agent", "wait", pane, "--timeout", str(timeout_ms)]
    for u in until:
        args += ["--until", u]
    # Allow generous slack over the server-side deadline before giving up locally.
    return status_of(herdr(args, socket=socket, timeout=timeout_ms / 1000.0 + 30))


# --------------------------------------------------------------- pane text

# Terminal chrome that carries no information: rules, borders, spinner-only rows,
# and the footer hints Claude Code and Cursor draw under the transcript.
_NOISE = re.compile(
    r"^\s*[─━═╌┄┈▀▄█▔▁╭╮╰╯│┃┆┊╴╶]+\s*$"
    r"|^\s*[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏⠘⠤⠰⠳✳✻✽✶✢·◐◓◑◒]+\s*$"
    r"|shift\+tab to cycle|\? for shortcuts|esc to interrupt|ctrl\+[a-z] to |Bypassing Permissions"
    r"|accept edits on|plan mode on|auto mode on|Add a follow-up|Run Everything|Cursor Grok"
    r"|Composer [0-9.]+ ·|lines hidden · ctrl\+o|truncated \([0-9]+ more lines\)|^\s*Tip: "
    r"|^\s*~/[^ ]* · |Update installed · Restart|^\s*[❯>›]\s*$|^\s*[▐▛▝▜]|^\s*◉ "
)


def read_pane(socket: str, pane: str) -> str:
    """Scrollback when the agent is idle; the visible viewport otherwise.

    A working or blocked agent sits on the alternate screen, where herdr answers
    `agent_not_idle` for scrollback reads, so fall back to what is on screen.
    """
    env = {"HERDR_SOCKET_PATH": socket}
    out = run(
        [HERDR, "agent", "read", pane, "--source", "recent-unwrapped",
         "--lines", "200", "--format", "text"],
        env=env,
    )
    if out.lstrip().startswith('{"error"'):
        out = run(
            [HERDR, "agent", "read", pane, "--source", "visible", "--format", "text"],
            env=env,
        )
        if out.lstrip().startswith('{"error"'):
            return ""
    return out


def clean_tail(socket: str, pane: str, lines: int) -> str:
    """Last `lines` meaningful rows of a pane, indent-ready."""
    kept = []
    for raw in read_pane(socket, pane).splitlines():
        row = raw.rstrip()
        if not row.strip() or _NOISE.search(row):
            continue
        kept.append(row[:220])
    return "\n".join(kept[-lines:]) if lines > 0 else ""


def die(message: str, code: int = EXIT_USAGE) -> None:
    print("herdr-watch: %s" % message, file=sys.stderr)
    raise SystemExit(code)
