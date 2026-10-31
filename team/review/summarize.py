"""Team review: summarize chunks with an injectable model call.

Send each chunk to a model via an injectable callable (default model name read
from existing config), merge per-chunk notes into a markdown summary under
the target's state directory with sections: what it is, structure, risks,
suggested improvements.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from pathlib import Path

from team import roles, targets
from team.ledger import state_dir
from team.review.chunk import Chunk

# Module constants
ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_ROLE = "implement"
MAX_SUMMARY_CHARS = 6000
SECTIONS = (
    ("what", "What it is"),
    ("structure", "Structure"),
    ("risks", "Risks"),
    ("improve", "Suggested improvements"),
)

# Type aliases
ModelCall = Callable[[str, str], str]
def default_model(root: Path = ROOT) -> str:
    """Return the default model name from the implement role's first candidate."""
    return roles.load(root, DEFAULT_ROLE)["models"]["candidates"][0]
def chunk_prompt(chunk: Chunk) -> str:
    """Generate a prompt for reviewing a single chunk.

    Returns text that:
    (a) says: you are reviewing one piece of a repo, reply in at most 8 short lines,
        each line starting with exactly one of the tags `what:`, `structure:`,
        `risks:`, `improve:`, followed by one short note;
    (b) then gives `File: {chunk.path} (part {chunk.index})`, a blank line, and
        `chunk.text`.
    """
    intro = (
        "You are reviewing one piece of a repo. Reply in at most 8 short lines. "
        "Each line must start with exactly one of the tags: what:, structure:, risks:, improve:. "
        f"After each line, provide one short note. File: {chunk.path} (part {chunk.index})"
    )
    return f"{intro}\n\n{chunk.text}"
def parse_notes(reply: str, path: str) -> dict[str, list[str]]:
    """Parse the model reply into a dict of tag → list of notes.

    Returns a dict with every key of SECTIONS mapped to a list. For each line of
    the reply, strip leading '-', '*' and spaces. Match case-insensitively with
    `re.match(r\"(what|structure|risks|improve)\\s*:\\s*(.+)\", line, re.I)`. On a
    match, append `f\"{note.strip()} ({path})\"` to that tag's list. Ignore lines
    that do not match.
    """
    result: dict[str, list[str]] = {tag: [] for tag, _ in SECTIONS}

    for line in reply.splitlines():
        # Strip leading bullet characters and whitespace
        cleaned = line.lstrip("-* ")
        # Match case-insensitive
        match = re.match(r"(what|structure|risks|improve)\s*:\s*(.+)", cleaned, re.IGNORECASE)
        if match:
            tag = match.group(1).lower()
            note = match.group(2).strip()
            result[tag].append(f"{note} ({path})")

    return result
def merge_notes(notes: list[dict[str, list[str]]], max_chars: int = MAX_SUMMARY_CHARS) -> str:
    """Merge a list of per-chunk notes into a markdown summary.

    Builds markdown that starts with `# Repository summary` and has one
    `## {title}` section per SECTIONS entry, in SECTIONS order. Under each
    heading put the de-duplicated notes (keep first-seen order) as `- ` bullets.
    A section with no notes gets the single line `- (none)`. Give each section a
    budget of `max_chars // len(SECTIONS)` characters. Add bullets while they fit.
    If the next bullet would not fit, stop the section and append `- …(truncated)`.
    So the result is always <= max_chars + a small margin for headings; make the
    budget `(max_chars - 200) // len(SECTIONS)` so the total stays <= max_chars.
    """
    # Build de-duplicated notes preserving order
    merged: dict[str, list[str]] = {}
    seen: dict[str, set[str]] = {}

    for note_dict in notes:
        for tag in note_dict:
            if tag not in merged:
                merged[tag] = []
                seen[tag] = set()
            for note in note_dict[tag]:
                if note not in seen[tag]:
                    seen[tag].add(note)
                    merged[tag].append(note)

    # Calculate budget per section
    section_count = len(SECTIONS)
    heading_chars = 200  # approximate total heading characters
    per_section_budget = (max_chars - heading_chars) // section_count

    lines = ["# Repository summary", ""]

    for tag, title in SECTIONS:
        notes_list = merged.get(tag, [])
        lines.append(f"## {title}")

        if not notes_list:
            lines.append("- (none)")
        else:
            for note in notes_list:
                bullet = f"- {note}"
                # Check if adding this bullet would exceed the budget
                current_length = sum(len(l) + 1 for l in lines)  # +1 for newlines
                if current_length + len(bullet) > per_section_budget:
                    lines.append("- …(truncated)")
                    break
                lines.append(bullet)
        lines.append("")

    return "\n".join(lines).strip()
def summary_path(target: str) -> Path:
    """Return the path for a target’s review summary.

    Raises `ValueError(f\"bad target name: {target!r}\")` unless
    `targets.NAME_RE.fullmatch(target)`. Returns `state_dir() / \"targets\" / target /
    \"review\" / \"summary.md\"`.
    """
    if not targets.NAME_RE.fullmatch(target):
        raise ValueError(f"bad target name: {target!r}")

    return state_dir() / "targets" / target / "review" / "summary.md"
def summarize_chunks(
    chunks: Iterable[Chunk],
    target: str,
    *,
    call: ModelCall,
    model: str | None = None,
    max_chars: int = MAX_SUMMARY_CHARS,
) -> Path:
    """Summarize chunks into a markdown file.

    `call` is keyword-only and required. Resolve `model = model or default_model()`.
    Compute the path with summary_path(target) first so a bad name fails before
    any model call. For each chunk run `parse_notes(call(model, chunk_prompt(chunk)),
    chunk.path)` and collect the results. Then run `merge_notes`, `path.parent.mkdir(
    parents=True, exist_ok=True)`, `path.write_text(text)` and return the path.

    Read-only v1: write nothing except this one file.
    """
    # Resolve model
    resolved_model = model or default_model()

    # Compute path first (validates target name)
    path = summary_path(target)

    # Process all chunks
    all_notes = []
    for chunk in chunks:
        reply = call(resolved_model, chunk_prompt(chunk))
        notes = parse_notes(reply, chunk.path)
        all_notes.append(notes)

    # Merge and write
    text = merge_notes(all_notes, max_chars)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)

    return path