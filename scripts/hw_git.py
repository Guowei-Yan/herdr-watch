"""Git watcher: commits, branch moves, pushes, history rewrites, rule checks."""
from __future__ import annotations

import os
import re
import time

from hw_core import (EXIT_NO_MATCH, Emitter, Fail, clock, duration, run,
                     run_status)

AI_TRACE = re.compile(r"co-authored-by|generated with|claude-session|assisted-by|🤖", re.I)


class Repo:
    def __init__(self, path: str):
        self.path = path

    def git(self, *args: str) -> str:
        return run(["git", "-C", self.path] + list(args)).strip()

    def head(self) -> str:
        out = self.git("rev-parse", "HEAD")
        return "" if "fatal" in out else out

    def branch(self) -> str:
        return self.git("branch", "--show-current")

    def upstream(self) -> str:
        out = self.git("rev-parse", "@{upstream}")
        return "none" if "fatal" in out or not out else out

    def is_ancestor(self, old: str, new: str) -> bool:
        return run_status(
            ["git", "-C", self.path, "merge-base", "--is-ancestor", old, new]) == 0


def _rule_flags(repo: Repo, sha: str, args) -> str:
    subject = repo.git("log", "-1", "--format=%s", sha)
    body = repo.git("log", "-1", "--format=%b", sha)
    flags = []
    if args.subject_max > 0 and len(subject) > args.subject_max:
        flags.append("SUBJECT>%d(%d)" % (args.subject_max, len(subject)))
    if args.subject_re and not re.search(args.subject_re, subject):
        flags.append("SUBJECT!~/%s/" % args.subject_re)
    if not args.no_ai_trace and AI_TRACE.search(subject + "\n" + body):
        flags.append("AI-TRACE")
    return " ".join(flags)


def watch_git(args) -> int:
    path = os.path.realpath(os.path.expanduser(args.repo))
    if not os.path.exists(os.path.join(path, ".git")):
        raise Fail("not a git repository: %s" % path, EXIT_NO_MATCH)

    repo = Repo(path)
    every = max(5, duration(args.interval))
    out = Emitter(args.label or os.path.basename(path))

    head, branch, upstream = repo.head(), repo.branch(), repo.upstream()
    out.event("ARMED git %s branch=%s HEAD=%s every %s @%s"
              % (path, branch or "detached", head[:9], args.interval, clock()))

    while True:
        time.sleep(every)
        new_head = repo.head()
        if not new_head:
            continue
        new_branch, new_upstream = repo.branch(), repo.upstream()

        if new_branch != branch:
            out.event("BRANCH %s → %s @%s"
                      % (branch or "detached", new_branch or "detached", clock()))
            branch = new_branch

        if new_head != head:
            if repo.is_ancestor(head, new_head):
                _report_commits(repo, out, head, new_head, new_branch, args)
            else:
                _report_rewrite(repo, out, head, new_head)
            head = new_head

        if new_upstream != upstream:
            out.event("PUSHED upstream %s→ %s @%s"
                      % ("" if upstream == "none" else upstream[:9] + " ",
                         new_upstream[:9], clock()))
            upstream = new_upstream


def _report_commits(repo: Repo, out: Emitter, old: str, new: str, branch: str, args) -> None:
    shas = [s for s in repo.git("rev-list", "--reverse", "%s..%s" % (old, new)).splitlines() if s]
    out.event("COMMIT +%d on %s → %s @%s"
              % (len(shas), branch or "detached", new[:9], clock()))
    for sha in shas[:20]:
        out.payload("%s %s | %s" % (sha[:9],
                                    repo.git("log", "-1", "--format=%an", sha),
                                    repo.git("log", "-1", "--format=%s", sha)))
        flags = _rule_flags(repo, sha, args)
        if flags:
            out.payload("RULE-CHECK %s: %s" % (sha[:9], flags))
    files = [f for f in repo.git("diff", "--name-only", "%s..%s" % (old, new)).splitlines() if f]
    for f in files[:25]:
        out.payload("file: %s" % f)
    if args.flag_docs:
        for f in files:
            if f.startswith("docs/"):
                out.payload("DOCS-COMMITTED: %s" % f)


def _report_rewrite(repo: Repo, out: Emitter, old: str, new: str) -> None:
    out.event("MOVED %s → %s is NOT fast-forward — history rewritten "
              "(reset/rebase/amend/checkout) @%s" % (old[:9], new[:9], clock()))
    out.payload("now at: %s" % repo.git("log", "-1", "--format=%h %an | %s", new))
    lost = repo.git("rev-list", "--count", "%s..%s" % (new, old)) or "?"
    out.payload("%s commit(s) no longer reachable from HEAD" % lost)
