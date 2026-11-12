"""Pane watcher: stops, questions, resumes, heartbeats, disappearance."""
from __future__ import annotations

import hashlib
import time

from hw_core import (Emitter, agent_get, agent_wait, clean_tail, clock,
                     duration, fmt_duration, session_name, status_of)
from hw_resolve import find_agents

LIVE = ("idle", "working", "blocked", "done", "unknown")
SETTLED = ("idle", "blocked", "done")


class PaneWatch:
    def __init__(self, args):
        self.args = args
        self.socket = args.socket
        self.pane = args.pane
        self.out = Emitter(args.label or args.pane)
        self.heartbeat = duration(args.heartbeat)
        if 0 < self.heartbeat < 60:
            self.heartbeat = 60
        self.on_output = duration(args.on_output) if args.on_output else 0
        if 0 < self.on_output < 30:
            self.on_output = 30
        self.gone_after = duration(args.gone_after)
        self.gone_max = duration(args.gone_max)
        self.last_event = self.state_since = time.time()
        self.gone_since = 0.0
        self.gone_reported = False
        self.out_hash = ""
        self.last_out_check = 0.0

    # -- helpers ---------------------------------------------------------
    def status(self) -> str:
        return status_of(agent_get(self.socket, self.pane))

    def tail(self, lines: int) -> str:
        return clean_tail(self.socket, self.pane, lines)

    def emit(self, header: str, lines: int) -> None:
        """Header and tail in one burst, so the Monitor batches them together."""
        body = self.tail(lines) if lines > 0 else ""
        self.out.event(header)
        if body:
            self.out.payload(body)

    def has_selector(self) -> bool:
        return bool(self.args.name or self.args.kind or self.args.cwd or self.args.title)

    def repick(self) -> str:
        """A restarted agent can land in a new pane id; re-resolve by selector."""
        if not self.has_selector():
            return self.pane
        probe = type("A", (), dict(vars(self.args)))()
        probe.pane = None
        for agent in find_agents(probe):
            if agent.get("socket") == self.socket:
                return agent.get("pane_id", self.pane)
        return self.pane

    def wait_ms(self) -> int:
        """Bound the server-side wait so heartbeats and output checks stay on time."""
        remaining = 600
        now = time.time()
        if self.heartbeat > 0:
            remaining = self.heartbeat - int(now - self.last_event)
        if self.on_output > 0:
            remaining = min(remaining, self.on_output - int(now - self.last_out_check))
        return max(1, min(600, remaining)) * 1000

    def maybe_heartbeat(self, status: str) -> None:
        if self.heartbeat <= 0 or time.time() - self.last_event < self.heartbeat:
            return
        self.emit("HEARTBEAT %s %s for %s @%s"
                  % (self.pane, status,
                     fmt_duration(int(time.time() - self.state_since)), clock()),
                  self.args.hb_tail)
        self.last_event = time.time()

    def maybe_output(self, status: str) -> None:
        if self.on_output <= 0 or time.time() - self.last_out_check < self.on_output:
            return
        digest = hashlib.md5(self.tail(40).encode("utf-8", "replace")).hexdigest()
        self.last_out_check = time.time()
        if self.out_hash and digest != self.out_hash:
            self.emit("OUTPUT %s new output while %s @%s" % (self.pane, status, clock()),
                      self.args.tail)
            self.last_event = time.time()
        self.out_hash = digest

    def mark(self) -> None:
        self.last_event = self.state_since = time.time()

    # -- lifecycle -------------------------------------------------------
    def arm(self) -> None:
        reply = agent_get(self.socket, self.pane)
        status = status_of(reply)
        session = session_name(self.socket)
        if status in LIVE:
            agent = reply.get("result", {}).get("agent", {})
            title = agent.get("terminal_title_stripped") or agent.get("terminal_title") or ""
            extra = (" on-output=%s" % fmt_duration(self.on_output)) if self.on_output else ""
            self.out.event('ARMED %s %s %s "%s" cwd=%s session=%s heartbeat=%s (after silence)%s @%s'
                           % (self.pane, agent.get("agent", "?"), status, title,
                              agent.get("cwd", "?"), session,
                              fmt_duration(self.heartbeat) if self.heartbeat else "off",
                              extra, clock()))
        else:
            self.out.event("ARMED %s not found (%s) on session=%s; looking for it for %s @%s"
                           % (self.pane, status, session,
                              fmt_duration(self.gone_max), clock()))
            self.gone_since = time.time()
        self.mark()
        if self.on_output:
            self.out_hash = hashlib.md5(self.tail(40).encode("utf-8", "replace")).hexdigest()
            self.last_out_check = time.time()

    def handle_missing(self, status: str) -> bool:
        """True when the watch should end."""
        if not self.gone_since:
            self.gone_since = time.time()
        missing = int(time.time() - self.gone_since)
        if missing >= self.gone_max:
            self.out.event("ENDED %s missing for %s (%s) — giving up @%s"
                           % (self.pane, fmt_duration(missing), status, clock()))
            return True
        if not self.gone_reported and missing >= self.gone_after:
            self.out.event("GONE %s (%s) for %s — session closed or agent exited; "
                           "still looking for up to %s @%s"
                           % (self.pane, status, fmt_duration(missing),
                              fmt_duration(self.gone_max), clock()))
            self.gone_reported = True
            self.last_event = time.time()
        time.sleep(30)
        return False

    def on_stop(self, status: str, stopped_at: float) -> bool:
        """Report a settled state. True when --once should end the watch."""
        if status in ("idle", "done"):
            self.emit("STOPPED %s → %s @%s (worked %s)"
                      % (self.pane, status, clock(),
                         fmt_duration(int(stopped_at - self.state_since))), self.args.tail)
        else:
            self.emit("BLOCKED %s @%s — waiting on a human (approval or question); "
                      "read the pane, do not answer it unprompted" % (self.pane, clock()),
                      self.args.tail)
        self.mark()
        if self.on_output:
            self.out_hash = hashlib.md5(self.tail(40).encode("utf-8", "replace")).hexdigest()
        return bool(self.args.once)

    def run(self) -> int:
        self.arm()
        while True:
            if self.has_selector():
                found = self.repick()
                if found != self.pane:
                    self.out.event("REBOUND %s → %s @%s" % (self.pane, found, clock()))
                    self.pane = found
            status = self.status()
            if status not in LIVE:
                if self.handle_missing(status):
                    return 0
                continue
            if self.gone_since:
                self.out.event("BACK %s status=%s after %s @%s"
                               % (self.pane, status,
                                  fmt_duration(int(time.time() - self.gone_since)), clock()))
                self.gone_since, self.gone_reported = 0.0, False
                self.mark()

            self.maybe_heartbeat(status)
            self.maybe_output(status)

            if status in ("working", "unknown"):
                result = agent_wait(self.socket, self.pane, list(SETTLED), self.wait_ms())
                if result == "timeout":
                    continue
                if result not in SETTLED:
                    time.sleep(15)
                    continue
                stopped_at = time.time()
                time.sleep(self.args.debounce)          # ignore flicker between tool calls
                settled = self.status()
                if settled == "working" or settled not in SETTLED:
                    continue
                if self.on_stop(settled, stopped_at):
                    self.out.event("ENDED after first %s (--once) @%s" % (settled, clock()))
                    return 0
            else:
                until = (["working", "idle", "done"] if status == "blocked"
                         else ["working", "blocked"])
                result = agent_wait(self.socket, self.pane, until, self.wait_ms())
                if result == "timeout":
                    continue
                if result == "working":
                    if not self.args.no_resume:
                        self.out.event("RESUMED %s → working @%s (was %s %s)"
                                       % (self.pane, clock(), status,
                                          fmt_duration(int(time.time() - self.state_since))))
                    self.mark()
                elif result == "blocked":
                    if self.on_stop("blocked", time.time()):
                        self.out.event("ENDED after first blocked (--once) @%s" % clock())
                        return 0
                elif result in ("idle", "done"):
                    self.emit("UNBLOCKED %s → %s @%s" % (self.pane, result, clock()),
                              self.args.tail)
                    self.mark()
                else:
                    time.sleep(15)
