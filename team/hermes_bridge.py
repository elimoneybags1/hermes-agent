"""Run selected upstream Hermes handlers in Hermes's own Python environment.

This file is invoked by path; it must depend only on stdlib until the Hermes
checkout is placed on sys.path. Team owns capability and board authorization.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def scoped_search(files, query, limit=40):
    """Native Hermes search engine over files already admitted by Team policy."""
    import re
    from tools.file_tools import _get_file_ops, redact_sensitive_text
    matches, notes = [], []
    for path in files:
        result = _get_file_ops().search(pattern='(?i)' + re.escape(query), path=path,
                                       target='content', limit=limit-len(matches), context=0)
        if result.error:
            notes.append(f'{path}: {result.error}')
        for match in result.matches:
            matches.append({'path':path, 'line':match.line_number,
                            'text':redact_sensitive_text(match.content, file_read=True)[:500]})
        if result.truncated:
            notes.append(f'{path}: native search result truncated')
        if len(matches) >= limit:
            notes.append('Match limit reached; remaining files not searched')
            break
    return {'matches':matches, 'coverage_notes':notes}


def main() -> None:
    source = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(source))
    payload = json.load(sys.stdin)
    if payload['name'] == 'scoped_search':
        print(json.dumps(scoped_search(**payload['arguments'])))
        return
    from tools import kanban_tools

    handlers = {
        "kanban_list": kanban_tools._handle_list,
        "kanban_show": kanban_tools._handle_show,
        "kanban_comment": kanban_tools._handle_comment,
        "kanban_attachments": kanban_tools._handle_attachments,
    }
    name, args = payload["name"], payload["arguments"]
    if name not in handlers:
        raise ValueError("unexposed Hermes tool")
    # Never inherit a task identity or an unrelated default board.
    if not args.get("board") or os.environ.get("HERMES_KANBAN_TASK"):
        raise ValueError("explicit orchestrator board required")
    print(handlers[name](args))


if __name__ == "__main__":
    main()
