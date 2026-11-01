"""Remove secrets and personal data from anything that leaves the machine (bug reports, logs)."""

from __future__ import annotations

import re

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("api_key", re.compile(
        r"\b(?:AIza[0-9A-Za-z_\-]{30,}|gh[pousr]_[0-9A-Za-z]{30,}|sk-[0-9A-Za-z\-_]{20,}|AKIA[0-9A-Z]{16})\b")),
    ("bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{20,}", re.I)),
    ("assigned_secret", re.compile(
        r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key)(\s*[=:]\s*)(?!\[REDACTED)\S+")),
    ("card_number", re.compile(r"\b(?:\d[ -]?){13,16}\b")),
    ("email", re.compile(r"\b[A-Za-z0-9._%+\-]+@(?!example\.(?:com|org|net)\b)[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
]


def redact(text: str) -> str:
    for label, pattern in _PATTERNS:
        if label == "assigned_secret":
            text = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED_SECRET]", text)
        else:
            text = pattern.sub(f"[REDACTED_{label.upper()}]", text)
    return text


def truncate(text: str, limit: int, *, keep: str = "head") -> str:
    """Cap text for a tool result. ``keep='tail'`` keeps the end (useful for logs)."""
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    if keep == "tail":
        return f"[... {omitted} characters omitted ...]\n" + text[-limit:]
    return text[:limit] + f"\n[... {omitted} characters omitted ...]"
