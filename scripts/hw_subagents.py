"""Subagent watcher: what the orchestrator delegated, and what came back.

Claude Code and Cursor store subagent transcripts differently, so each layout
gets a small adapter with the same four operations: list, describe, done,
is_subagent.
"""
from __future__ import annotations

import glob
import json
import os
import re
import time

from hw_core import EXIT_NO_MATCH, Emitter, Fail, clock, duration
from hw_transcript import (clean, count_user_messages, final_text,
                           project_slug, read_jsonl, short_id, text)

HOME = os.path.expanduser("~")
CLAUDE_PROFILES = (".claude", ".zcode", ".kimicode")
META_SUFFIX = ".meta.json"


class CursorLayout:
    """~/.cursor/projects/<slug>/agent-transcripts/<id>/<id>.jsonl"""

    kind = "cursor"

    def __init__(self, cwd: str, exclude: set[str], **_):
        self.root = os.path.join(HOME, ".cursor", "projects",
                                 project_slug(cwd, False), "agent-transcripts")
        self.exclude = exclude

    def valid(self) -> bool:
        return os.path.isdir(self.root)

    def listing(self) -> dict:
        found = {}
        for entry in glob.glob(os.path.join(self.root, "*", "")):
            ident = os.path.basename(entry.rstrip(os.sep))
            if ident not in self.exclude:
                found[ident] = os.path.join(entry, ident + ".jsonl")
        return found

    def transcript(self, path: str) -> str:
        return path

    def describe(self, _ident: str, path: str) -> str | None:
        records = read_jsonl(path)
        if not records:
            return None
        prompt = re.sub(r"<timestamp>.*?</timestamp>", "", text(records[0]), flags=re.S)
        return clean(re.sub(r"</?user_query>", "", prompt), 160)

    def done(self, _ident: str, path: str) -> tuple[bool, str | None]:
        records = read_jsonl(path)
        if any(r.get("type") == "turn_ended" for r in records):
            return True, final_text(records)
        return False, None

    def is_subagent(self, _ident: str, path: str) -> bool:
        # Cursor keeps main conversations beside subagents. A subagent is spawned
        # once so it has exactly one user message; a chat you type into grows more.
        return count_user_messages(read_jsonl(path)) == 1


class ClaudeLayout:
    """<config>/projects/<slug>/<session>/subagents/agent-<id>.meta.json"""

    kind = "claude"

    def __init__(self, cwd: str, exclude: set[str], session: str = "", profile: str = "",
                 quiet: int = 45):
        self.exclude, self.quiet = exclude, quiet
        self.root = self._find(cwd, session, profile)
        self.parent_log = ""
        if self.root:
            session_dir = os.path.dirname(self.root)
            self.parent_log = os.path.join(os.path.dirname(session_dir),
                                           os.path.basename(session_dir) + ".jsonl")

    def _find(self, cwd: str, session: str, profile: str) -> str:
        roots = [profile] if profile else [os.path.join(HOME, p) for p in CLAUDE_PROFILES]
        best, best_mtime = "", -1.0
        for base in roots:
            project = os.path.join(base, "projects", project_slug(cwd, True))
            if not os.path.isdir(project):
                continue
            if session:
                candidates = [os.path.join(project, session, "subagents")]
            else:
                candidates = sorted(glob.glob(os.path.join(project, "*", "subagents")),
                                    key=os.path.getmtime, reverse=True)
            for candidate in candidates:
                if os.path.isdir(candidate):
                    mtime = os.path.getmtime(candidate)
                    if mtime > best_mtime:
                        best, best_mtime = candidate, mtime
                    break
        return best

    def valid(self) -> bool:
        return bool(self.root)

    def listing(self) -> dict:
        found = {}
        for meta in glob.glob(os.path.join(self.root, "agent-*" + META_SUFFIX)):
            ident = os.path.basename(meta)[:-len(META_SUFFIX)]
            if ident not in self.exclude:
                found[ident] = meta
        return found

    def transcript(self, path: str) -> str:
        return path[:-len(META_SUFFIX)] + ".jsonl"

    def describe(self, _ident: str, path: str) -> str | None:
        try:
            with open(path) as handle:
                meta = json.load(handle)
        except (OSError, ValueError):
            return None
        return clean("%s — %s" % (meta.get("agentType", "?"),
                                   meta.get("description", "")), 160)

    def _parent_recorded(self, tool_use_id: str) -> bool:
        if not tool_use_id or not os.path.exists(self.parent_log):
            return False
        try:
            with open(self.parent_log, errors="replace") as handle:
                for line in handle:
                    if tool_use_id in line and '"tool_result"' in line:
                        return True
        except OSError:
            pass
        return False

    def done(self, _ident: str, path: str) -> tuple[bool, str | None]:
        jsonl = self.transcript(path)
        records = read_jsonl(jsonl)
        try:
            with open(path) as handle:
                meta = json.load(handle)
        except (OSError, ValueError):
            meta = {}
        if self._parent_recorded(meta.get("toolUseId", "")):
            return True, final_text(records)
        try:                                    # fallback: the transcript went quiet
            if records and time.time() - os.path.getmtime(jsonl) > self.quiet:
                return True, final_text(records)
        except OSError:
            pass
        return False, None

    def is_subagent(self, _ident: str, _path: str) -> bool:
        return True                              # a meta.json only exists for a real subagent


def watch_subagents(args) -> int:
    cwd = os.path.realpath(os.path.expanduser(args.cwd))
    exclude = set(args.exclude or [])
    if args.kind == "cursor":
        layout = CursorLayout(cwd, exclude)
    else:
        layout = ClaudeLayout(cwd, exclude, session=args.session_id or "",
                              profile=args.profile or "", quiet=duration(args.quiet_for))
    out = Emitter(args.label or os.path.basename(cwd) + "-subagents")
    if not layout.valid():
        out.event("ENDED no %s subagent transcript directory for %s — "
                  "is that the right --cwd/--kind?" % (args.kind, cwd))
        raise Fail("", EXIT_NO_MATCH)

    every = max(3, duration(args.interval))
    adopt = duration(args.adopt_window)
    existing = layout.listing()
    known, announced, finished, pending = set(existing), set(), set(), {}

    # Arming mid-run is the normal case, so keep tracking subagents that are still
    # working: recently active and not finished. Everything older stays ignored.
    adopted = []
    if adopt > 0:
        now = time.time()
        for ident, path in existing.items():
            try:
                if now - os.path.getmtime(layout.transcript(path)) > adopt:
                    continue
            except OSError:
                continue
            try:
                is_done, _ = layout.done(ident, path)
            except Exception:
                continue
            if not is_done and layout.is_subagent(ident, path):
                announced.add(ident)
                adopted.append((ident, path))

    out.event("ARMED %s subagents under %s (%d already there, %d still running and tracked) "
              "every %ds @%s" % (args.kind, layout.root, len(known) - len(adopted),
                                 len(adopted), every, clock()))
    for ident, path in adopted:
        out.event("SPAWNED %s %s (already running) @%s"
                  % (short_id(ident), layout.describe(ident, path) or "?", clock()))

    while True:
        try:
            current = layout.listing()
        except OSError:
            time.sleep(every)
            continue
        for ident in sorted(set(current) - known):
            if ident in announced:
                continue
            described = layout.describe(ident, current[ident])
            if described is None:
                continue                         # not written yet; next tick
            if not layout.is_subagent(ident, current[ident]):
                known.add(ident)                 # a conversation, not a subagent
                continue
            announced.add(ident)
            out.event("SPAWNED %s %s @%s" % (short_id(ident), described, clock()))
        for ident in sorted(announced - finished):
            if ident not in current:
                continue
            is_done, report = layout.done(ident, current[ident])
            if not is_done:
                continue
            # The completion signal can beat the final message to disk.
            if not report and pending.get(ident, 0) < 4:
                pending[ident] = pending.get(ident, 0) + 1
                continue
            finished.add(ident)
            out.event("FINISHED %s @%s" % (short_id(ident), clock()))
            out.payload(clean(report, 900) if report
                        else "(no final message in transcript)")
        known |= set(current)
        time.sleep(every)
