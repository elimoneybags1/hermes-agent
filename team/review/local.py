"""Read the existing checkout, including dirty/untracked source, without checking it out."""
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

from team.review.chunk import LOCKFILE_NAMES, MAX_FILE_BYTES, _read_text

SKIP = {'.git', '.data', '.next', 'node_modules', 'vendor', 'dist', 'build', 'outputs',
        '__pycache__', '.cache', '.claude', '.codex', '.hermes', '.agents',
        '.claude-flow', '.ruflo', '.swarm', '.hive-mind', 'generated'}
EXTENSIONS = {'.py', '.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs', '.md', '.toml',
              '.yaml', '.yml', '.json', '.sh', '.sql', '.prisma', '.rs', '.go', '.swift',
              '.css', '.html', '.vue', '.svelte', '.txt'}
PRIORITY = ('AGENTS.md', 'CLAUDE.md', 'README.md', 'BUILD-PLAN.md', 'PLAN.md', 'package.json',
            'pyproject.toml', 'Makefile', 'docs/PLAN.md')


def git(tree: Path, *args: str) -> str:
    return subprocess.check_output(['git', '-C', str(tree), *args], text=True,
                                   env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'})


def allowed(rel: str) -> bool:
    p = Path(rel)
    if p.is_absolute() or '..' in p.parts:
        return False
    if any(x.lower() in SKIP or x.startswith(('.venv', 'venv')) for x in p.parts):
        return False
    name = p.name.lower()
    if any(s in name for s in ('.env', 'credential', 'secret', 'token', 'id_rsa', 'id_ed25519')):
        return False
    return p.name not in LOCKFILE_NAMES and (p.suffix.lower() in EXTENSIONS or p.name in PRIORITY)


def inventory(tree: Path) -> dict:
    tree = tree.resolve()
    names = sorted(set(git(tree, 'ls-files', '-z', '--cached', '--others', '--exclude-standard').split('\0')) - {''},
                   key=lambda n: (PRIORITY.index(n) if n in PRIORITY else len(PRIORITY), n))
    # Interleave areas so a call cap does not spend everything on app/.
    ordered = [n for n in names if n in PRIORITY]
    groups = {}
    for name in names:
        if name not in PRIORITY:
            group = name.split('/')[0] if '/' in name else '(root)'
            groups.setdefault(group, []).append(name)
    while any(groups.values()):
        for group in groups.values():
            if group:
                ordered.append(group.pop(0))
    names = ordered
    files, skipped = {}, []
    for rel in names:
        path = tree / rel
        if not allowed(rel) or any(p.is_symlink() for p in [path, *path.parents] if p != tree):
            skipped.append(rel)
            continue
        text = _read_text(path, MAX_FILE_BYTES)
        if text is None:
            skipped.append(rel)
            continue
        files[rel] = {'hash': hashlib.sha256(text.encode()).hexdigest(), 'text': text}
    return {'head': git(tree, 'rev-parse', 'HEAD').strip(),
            'dirty': bool(git(tree, 'status', '--porcelain', '--untracked-files=normal')),
            'files': files, 'skipped': skipped}


def chunks(rel: str, text: str, limit: int = 12000):
    """Bound prompts and preserve exact line ranges, including a single oversized line."""
    parts, size, start = [], 0, 1
    for number, line in enumerate(text.splitlines(), 1):
        pieces = [line[i:i + limit // 2] for i in range(0, len(line), limit // 2)] or ['']
        for piece in pieces:
            row = f'{number}: {piece}\n'
            if parts and size + len(row) > limit:
                yield start, number - 1 if number > start else number, ''.join(parts)
                parts, size, start = [], 0, number
            parts.append(row)
            size += len(row)
    if parts:
        yield start, number, ''.join(parts)
