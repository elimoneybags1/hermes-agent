"""The boss reads the review summary only through here: capped, never the cloned repo."""

from __future__ import annotations

from pathlib import Path

from team.review.summarize import summary_path
from team.targets import NAME_RE

MAX_SUMMARY_CHARS = 6000
TRUNCATION_MARK = "\n\n[summary truncated]"


def read_summary(target: str, *, max_chars: int = MAX_SUMMARY_CHARS) -> str | None:
    if NAME_RE.fullmatch(target) is None:
        raise ValueError(f"bad target name: {target!r}")
    path = summary_path(target)
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) <= max_chars:
        return text
    if max_chars < len(TRUNCATION_MARK):
        return text[:max_chars]
    return text[:max_chars - len(TRUNCATION_MARK)] + TRUNCATION_MARK
