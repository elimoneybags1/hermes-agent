"""Team review: chunk walker for a cloned repo.

Walks a cloned repo, skips .git, binaries, lockfiles and files over a size limit,
and yields bounded text chunks with their file paths. This is the only way
review code reads a cloned repo; it never calls a model.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

# Constants defined in the specification
SKIP_DIRS = frozenset({".git"})
LOCKFILE_NAMES = frozenset({
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "bun.lockb",
    "poetry.lock",
    "uv.lock",
    "Pipfile.lock",
    "Cargo.lock",
    "Gemfile.lock",
    "composer.lock",
    "go.sum",
    "flake.lock",
})
MAX_FILE_BYTES = 200_000
MAX_CHUNK_CHARS = 4_000
_SNIFF_BYTES = 8192


@dataclass(frozen=True)
class Chunk:
    """A text chunk from a file in the repo."""

    path: str
    index: int
    text: str

    # path is relative to root, always with forward slashes
    # index is 0-based position within that file
    # text is the chunk content


def _is_lockfile(name: str) -> bool:
    """Return True if the name is a lockfile to skip."""
    return name in LOCKFILE_NAMES or name.endswith(".lock")


def _read_text(path: Path, max_file_bytes: int) -> str | None:
    """Read text from a file, returning None if it should be skipped."""
    # Skip symlinks
    if path.is_symlink():
        return None
    # Skip non-files
    if not path.is_file():
        return None
    # Skip empty files or files too large
    try:
        st = path.stat()
    except OSError:
        return None
    if st.st_size == 0 or st.st_size > max_file_bytes:
        return None
    # Sniff for binary content (null byte anywhere in first _SNIFF_BYTES)
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    # Check for null bytes in the file (up to _SNIFF_BYTES bytes)
    check_bytes = raw[:_SNIFF_BYTES] if len(raw) >= _SNIFF_BYTES else raw
    if b"\0" in check_bytes:
        return None
    # Try to decode as UTF-8
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return text


def _split(text: str, max_chars: int) -> Iterator[str]:
    """Split text into chunks of at most max_chars each."""
    buffer = ""
    for line in text.splitlines(keepends=True):
        if len(buffer) + len(line) > max_chars:
            # Buffer would overflow; yield what we have and start new
            if buffer.strip():
                yield buffer
            buffer = line
            # If the line itself is too long, slice it
            while len(buffer) > max_chars:
                yield buffer[:max_chars]
                buffer = buffer[max_chars:]
        else:
            buffer += line
    # Yield any remaining buffer
    if buffer.strip():
        yield buffer


def chunk_repo(
    root: str | Path,
    *,
    max_chunk_chars: int = MAX_CHUNK_CHARS,
    max_file_bytes: int = MAX_FILE_BYTES,
) -> Iterator[Chunk]:
    """Walk root, yielding Chunk objects for readable files.

    Walks the cloned repo rooted at `root`, skipping .git, binaries, lockfiles
    and files over a size limit, and yields bounded text chunks with their file
    paths. This is the only way review code reads a cloned repo; it never calls
    a model.
    """
    # Validate limits
    if max_chunk_chars < 1 or max_file_bytes < 1:
        raise ValueError("max_chunk_chars and max_file_bytes must be at least 1")

    root_path = Path(root)
    for dirpath, dirnames, filenames in os.walk(root_path):
        # Prune directories not to walk (e.g., .git)
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]

        # Process files in sorted order
        for filename in sorted(filenames):
            if _is_lockfile(filename):
                continue

            filepath = Path(dirpath) / filename
            text = _read_text(filepath, max_file_bytes)
            if text is None:
                continue

            # Get relative path with forward slashes
            rel_path = filepath.relative_to(root_path).as_posix()

            # Split into chunks and yield them
            for index, chunk_text in enumerate(_split(text, max_chunk_chars)):
                if chunk_text.strip():  # Skip empty/whitespace-only chunks
                    yield Chunk(rel_path, index, chunk_text)