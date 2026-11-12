"""Reading agent transcript files (JSONL), shared by the subagent adapters."""
from __future__ import annotations

import json
import re


def read_jsonl(path: str) -> list[dict]:
    """Records from a JSONL transcript; malformed lines are skipped."""
    out = []
    try:
        with open(path, errors="replace") as handle:
            for line in handle:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return out


def blocks(record: dict) -> list:
    message = record.get("message") or {}
    content = message.get("content") if isinstance(message, dict) else None
    return content if isinstance(content, list) else []


def text(record: dict) -> str:
    message = record.get("message") or {}
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text")
    return ""


def role(record: dict) -> str | None:
    message = record.get("message")
    if isinstance(message, dict) and message.get("role"):
        return message["role"]
    return record.get("role")


def final_text(records: list[dict]) -> str | None:
    """Last assistant message that is prose, not a tool call — the agent's report."""
    last = None
    for record in records:
        if role(record) != "assistant":
            continue
        if any(isinstance(b, dict) and b.get("type") == "tool_use" for b in blocks(record)):
            continue
        body = text(record).strip()
        if body and body != "[REDACTED]":
            last = body
    return last


def count_user_messages(records: list[dict], stop_at: int = 2) -> int:
    """User-role messages, counted only up to `stop_at` (enough to classify)."""
    count = 0
    for record in records:
        if role(record) == "user":
            count += 1
            if count >= stop_at:
                break
    return count


def clean(value: str | None, limit: int) -> str:
    return re.sub(r"\s+", " ", value or "").strip()[:limit]


def short_id(ident: str) -> str:
    trimmed = re.sub(r"^agent-", "", ident)
    # UUIDs read better cut at the first group.
    return trimmed[:8] if re.match(r"^[0-9a-f]{8}-", trimmed) else trimmed[:10]


def project_slug(path: str, leading_dash: bool) -> str:
    slug = re.sub(r"[/_]", "-", path)
    return slug if leading_dash else slug.lstrip("-")
