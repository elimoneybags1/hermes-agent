from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from team.review.clone import clone_repo, CloneError
from team.review.chunk import chunk_repo
from team.review.summarize import summarize_chunks
class ReviewError(RuntimeError):
    pass
def _no_model(prompt: str, model: str) -> str:
    raise ReviewError('no small-model backend is wired for the CLI; pass call= to main()')
def main(argv: list[str] | None = None, *, call=None, cloner=clone_repo) -> int:
    parser = argparse.ArgumentParser(prog='python -m team.review')
    parser.add_argument('url', help='a git URL or local path')
    parser.add_argument('--target', default='review', help='target name (default: review)')
    args = parser.parse_args(argv)
    
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = cloner(args.url, Path(tmp) / 'repo')
            path = summarize_chunks(
                chunk_repo(root), 
                args.target, 
                call=call or _no_model
            )
            print(path)
            return 0
    except (CloneError, ReviewError, ValueError) as e:
        print(f'team review: {e}', file=sys.stderr)
        return 1
if __name__ == '__main__':
    sys.exit(main())