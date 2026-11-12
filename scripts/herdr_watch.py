#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""herdr-watch — watch a coding agent in a herdr pane; one stdout line per event.

Standard library only. Runs anywhere Python does:
    uv run --script herdr_watch.py <command> ...      (uv pins the interpreter)
    python3 herdr_watch.py <command> ...              (any Python 3.9+)

The only external programs used are `herdr` and `git`.

Exit codes: 0 ok · 2 usage · 3 no match · 4 ambiguous · 5 no herdr server
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from hw_core import (EXIT_AMBIGUOUS, EXIT_NO_MATCH, Fail, agent_get,  # noqa: E402
                     clean_tail, sessions, status_of)
from hw_git import watch_git  # noqa: E402
from hw_pane import PaneWatch  # noqa: E402
from hw_resolve import HEADER, find_agents, resolve_one, row  # noqa: E402
from hw_subagents import watch_subagents  # noqa: E402


def add_selector(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--session", help="herdr session name (default: search all)")
    parser.add_argument("--socket", help="herdr socket path (overrides --session)")
    parser.add_argument("--pane", help="pane id such as w3:p4")
    parser.add_argument("--name", help="live agent name from `herdr agent start`")
    parser.add_argument("--kind", help="agent kind: claude, cursor, ...")
    parser.add_argument("--cwd", help="agent working directory")
    parser.add_argument("--title", help="case-insensitive substring of the pane title")
    parser.add_argument("--include-self", action="store_true",
                        help="allow matching the pane this session runs in")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="herdr-watch", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("sessions", help="list running herdr sessions")

    resolve = sub.add_parser("resolve", help="find the agent matching a selector")
    add_selector(resolve)

    for name, helptext in (("status", "show one agent's status"),
                           ("tail", "print the cleaned tail of a pane")):
        cmd = sub.add_parser(name, help=helptext)
        cmd.add_argument("--socket", required=True)
        cmd.add_argument("--pane", required=True)
        if name == "tail":
            cmd.add_argument("--lines", type=int, default=12)

    watch = sub.add_parser("watch", help="watch a pane's lifecycle")
    add_selector(watch)
    watch.add_argument("--heartbeat", default="15m",
                       help="notify after this much silence (default 15m; 0 = off)")
    watch.add_argument("--on-output", default="",
                       help="also notify when pane output changed, checked every DUR")
    watch.add_argument("--debounce", type=int, default=8,
                       help="ignore stops shorter than this many seconds")
    watch.add_argument("--tail", type=int, default=12)
    watch.add_argument("--hb-tail", type=int, default=6)
    watch.add_argument("--gone-after", default="2m")
    watch.add_argument("--gone-max", default="60m")
    watch.add_argument("--once", action="store_true",
                       help="end after the first STOPPED or BLOCKED")
    watch.add_argument("--no-resume", action="store_true")
    watch.add_argument("--label", default="")

    git = sub.add_parser("watch-git", help="watch a repo's commits and history")
    git.add_argument("--repo", required=True)
    git.add_argument("--interval", default="15s")
    git.add_argument("--subject-max", type=int, default=50,
                     help="flag subjects longer than N chars (0 = off)")
    git.add_argument("--subject-re", default="",
                     help="flag subjects not matching this regex")
    git.add_argument("--flag-docs", action="store_true")
    git.add_argument("--no-ai-trace", action="store_true",
                     help="do not flag AI attribution in commit messages")
    git.add_argument("--label", default="")

    agents = sub.add_parser("watch-subagents", help="watch an agent's subagents")
    agents.add_argument("--kind", required=True, choices=("claude", "cursor"))
    agents.add_argument("--cwd", required=True)
    agents.add_argument("--session-id", default="")
    agents.add_argument("--profile", default="",
                        help="config dir for a claude kind (default: search all)")
    agents.add_argument("--exclude", action="append", default=[])
    agents.add_argument("--interval", default="10s")
    agents.add_argument("--quiet-for", default="45s")
    agents.add_argument("--adopt-window", default="10m",
                        help="also track subagents already running and active within this window")
    agents.add_argument("--label", default="")
    return parser


def cmd_sessions() -> int:
    running = sessions()
    if not running:
        raise Fail("no running herdr session (is the herdr server up?)", 5)
    print("SESSION\tSOCKET")
    for s in running:
        print("%s\t%s" % (s.get("name", "?"), s.get("socket_path", "?")))
    return 0


def cmd_resolve(args) -> int:
    found = find_agents(args)
    if not found:
        return _no_match(args)
    print(HEADER)
    for agent in found:
        print(row(agent))
    if len(found) > 1:
        print("ambiguous: %d agents match; add --pane, --title, --kind or --session"
              % len(found), file=sys.stderr)
        return EXIT_AMBIGUOUS
    return 0


def _no_match(args) -> int:
    from hw_resolve import target_sessions
    searched = ", ".join(n for n, _ in target_sessions(
        getattr(args, "socket", None), getattr(args, "session", None)))
    print("no agent matches the selector (searched: %s)" % searched, file=sys.stderr)
    return EXIT_NO_MATCH


def cmd_status(args) -> int:
    reply = agent_get(args.socket, args.pane)
    if "error" in reply:
        print("error: %s: %s" % (reply["error"].get("code"), reply["error"].get("message")))
        return 1
    agent = reply.get("result", {}).get("agent", {})
    print('%s %s %s cwd=%s title="%s"'
          % (agent.get("pane_id"), agent.get("agent"), agent.get("agent_status"),
             agent.get("cwd"),
             agent.get("terminal_title_stripped") or agent.get("terminal_title") or ""))
    return 0


def cmd_tail(args) -> int:
    body = clean_tail(args.socket, args.pane, args.lines)
    for line in body.splitlines():
        print("    " + line)
    return 0


def cmd_watch(args) -> int:
    if not (args.socket and args.pane):
        agent = resolve_one(args)          # a selector alone is enough
        args.socket, args.pane = agent["socket"], agent["pane_id"]
    return PaneWatch(args).run()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "sessions":
            return cmd_sessions()
        if args.command == "resolve":
            return cmd_resolve(args)
        if args.command == "status":
            return cmd_status(args)
        if args.command == "tail":
            return cmd_tail(args)
        if args.command == "watch":
            return cmd_watch(args)
        if args.command == "watch-git":
            return watch_git(args)
        if args.command == "watch-subagents":
            return watch_subagents(args)
    except Fail as exc:
        if str(exc):
            print(str(exc) if "\t" in str(exc) else "herdr-watch: %s" % exc,
                  file=sys.stderr)
        return exc.code
    except KeyboardInterrupt:
        return 0
    except BrokenPipeError:
        return 0
    return EXIT_NO_MATCH


if __name__ == "__main__":
    sys.exit(main())
