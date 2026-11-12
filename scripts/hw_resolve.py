"""Selector matching: turn "the cursor agent in payments-api" into one socket and pane."""
from __future__ import annotations

import os

from hw_core import (EXIT_AMBIGUOUS, EXIT_NO_MATCH, EXIT_NO_SERVER, Fail,
                     agents_on, sessions)

SELECTOR_FLAGS = ("session", "socket", "pane", "name", "kind", "cwd", "title")


def target_sessions(socket: str | None, session: str | None) -> list[tuple[str, str]]:
    """(name, socket) pairs to search."""
    running = sessions()
    if socket:
        name = next((s["name"] for s in running if s.get("socket_path") == socket), "custom")
        return [(name, socket)]
    if session:
        return [(s["name"], s["socket_path"]) for s in running if s.get("name") == session]
    if not running:
        raise Fail("no running herdr session (is the herdr server up?)", EXIT_NO_SERVER)
    return [(s["name"], s["socket_path"]) for s in running]


def _matches(agent: dict, args) -> bool:
    title = (agent.get("terminal_title_stripped") or agent.get("terminal_title") or "")
    if args.pane and agent.get("pane_id") != args.pane:
        return False
    if args.name and (agent.get("name") or "") != args.name:
        return False
    if args.kind and agent.get("agent") != args.kind:
        return False
    if args.cwd:
        want = os.path.realpath(os.path.expanduser(args.cwd))
        if want not in (agent.get("cwd"), agent.get("foreground_cwd")):
            return False
    if args.title and args.title.lower() not in title.lower():
        return False
    if not getattr(args, "include_self", False):
        # Never match the pane this session is itself running in.
        if (agent.get("socket") == os.environ.get("HERDR_SOCKET_PATH")
                and agent.get("pane_id") == os.environ.get("HERDR_PANE_ID")):
            return False
    return True


def find_agents(args) -> list[dict]:
    found = []
    for name, socket in target_sessions(getattr(args, "socket", None),
                                        getattr(args, "session", None)):
        for agent in agents_on(socket, name):
            if _matches(agent, args):
                found.append(agent)
    return found


def row(agent: dict) -> str:
    return "\t".join([
        agent.get("session", ""),
        agent.get("socket", ""),
        agent.get("pane_id", ""),
        agent.get("agent", ""),
        agent.get("agent_status", ""),
        agent.get("cwd", ""),
        (agent.get("terminal_title_stripped") or agent.get("terminal_title") or ""),
    ])


HEADER = "SESSION\tSOCKET\tPANE\tKIND\tSTATUS\tCWD\tTITLE"


def resolve_one(args) -> dict:
    """Exactly one matching agent, or a Fail carrying the right exit code."""
    found = find_agents(args)
    if not found:
        searched = ", ".join(n for n, _ in target_sessions(
            getattr(args, "socket", None), getattr(args, "session", None)))
        raise Fail("no agent matches the selector (searched: %s)" % searched, EXIT_NO_MATCH)
    if len(found) > 1:
        lines = [HEADER] + [row(a) for a in found]
        raise Fail("\n".join(lines)
                   + "\nambiguous: %d agents match; add --pane, --title, --kind or --session"
                   % len(found), EXIT_AMBIGUOUS)
    return found[0]
