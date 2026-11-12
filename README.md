# herdr-watch

[![CI](https://github.com/Guowei-Yan/herdr-watch/actions/workflows/ci.yml/badge.svg)](https://github.com/Guowei-Yan/herdr-watch/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B-3776AB.svg)](https://www.python.org/)

Non-invasive cross-agent observability daemon and JSON-RPC event bridge for autonomous coding agents (**zero third-party runtime dependencies**, pure Python standard library).

`herdr-watch` monitors another running coding agent session — across **Claude Code**, **Codex** (including local App Server WebSocket / Unix-socket JSON-RPC bridges), **Cursor**, **kimi-code**, and **zcode** — surfacing turn completions, blocking user questions, subagent lifecycle transitions, and Git working-tree activity without injecting keystrokes into the watched pane or mutating its repository.

---

## Architecture

```text
Watched Target Session (Pane / Transcript / Git Worktree)
        │
        ├─► [Pane & Transcript Watcher]  (stop / question / heartbeat detection)
        ├─► [Subagent Watcher]           (spawn, description, completion tracking)
        └─► [Git Activity Watcher]       (HEAD, branch, and worktree state deltas)
                │
                ▼
     ┌───────────────────────────────────────────────────────────┐
     │                herdr-watch Event Delivery                 │
     │  • Persistent Monitor stream (Claude Code / CLI hosts)    │
     │  • Background JSON-RPC / WebSocket bridge (Codex Server)  │
     └───────────────────────────────────────────────────────────┘
```

### Engineering Highlights

- **Read-Only Target Isolation**: Watchers inspect terminal state, structured JSONL transcripts, and Git metadata strictly out-of-band; they never send terminal input or alter the watched repository.
- **Zero-Dependency WebSocket & JSON-RPC Client (`hw_codex_socket.py`, `hw_codex_rpc.py`)**: Implements RFC 6455 WebSocket upgrade framing and JSON-RPC request/notification multiplexing over TCP and Unix domain sockets using only Python `socket` and `select`, including idle server-disconnect detection during quiet intervals.
- **Fault-Tolerant Batching & Delivery (`hw_background.py`, `codex_monitor.py`)**: Coalesces rapid event bursts while preserving undelivered events across transient disconnects and stopping cleanly when the upstream session terminates.
- **Transactional Skill Installer (`sync_herdr_watch.py`)**: Byte-compiles all modules prior to installation and stages files with atomic `os.replace` rollback on error.

---

## Repository Layout

| Path | Description |
| :--- | :--- |
| [`scripts/herdr_watch.py`](scripts/herdr_watch.py) | Main CLI watcher entry point (`pane`, `subagents`, `git` watchers) |
| [`scripts/codex_monitor.py`](scripts/codex_monitor.py) | Background monitor & JSON-RPC event bridge for Codex App Server threads |
| [`scripts/hw_codex_socket.py`](scripts/hw_codex_socket.py) | Pure-stdlib WebSocket & Unix-socket transport with idle disconnect detection |
| [`scripts/hw_subagents.py`](scripts/hw_subagents.py) | Parent/child transcript correlator for Claude Code & Cursor subagents |
| [`scripts/herdr_watch_pump.py`](scripts/herdr_watch_pump.py) | Optional atomic `.event` file spooler for file-polling agent host extensions |
| [`sync_herdr_watch.py`](sync_herdr_watch.py) | Atomic, rollback-safe skill installer across local agent profiles |
| [`tests/`](tests/) | **42 unit & mock-RPC integration tests** (`fake_rpc.py`, `fake_watcher.py`, `test_*.py`) |

---

## Prerequisites

- **Python 3.9+** (pure Python standard library — zero third-party `pip` packages)
- **`git`** (for `watch-git` commit, branch, and history-rewrite monitoring)
- **`herdr` CLI** (`HERDR_BIN_PATH` or `herdr` on `PATH`, required for terminal-pane `watch` / `resolve` / `tail`)
- **Optional**: Local Codex App Server socket/WebSocket endpoint when running `codex_monitor.py`

---

## Quickstart

### Run the Test Suite

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

### Install Into Local Agent Skill Directories

```bash
python3 sync_herdr_watch.py
# Or install to an explicit directory:
python3 sync_herdr_watch.py --destination ~/.claude/skills
```

### Inspect Watcher CLI Options

```bash
python3 scripts/herdr_watch.py --help
python3 scripts/codex_monitor.py --help
```

See [`SKILL.md`](SKILL.md) and [`references/codex.md`](references/codex.md) for host-specific arming patterns and lifecycle states.

---

## License

[MIT](LICENSE)
