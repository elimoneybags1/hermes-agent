"""All goals landed utilities for the team module."""

from __future__ import annotations

from typing import Any
def all_goals_landed(proposal: dict, results: list[dict[str, Any]]) -> bool:
    """Return True only when results is non-empty and every row has outcome == 'pass'.

    An empty list gives False. A row with outcome None is still running and counts as not landed.
    proposal is accepted for the caller's signature but is not used.
    """
    return bool(results) and all(r["outcome"] == "pass" for r in results)