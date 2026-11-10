"""Disposable state snapshots for comparing dashboards without running workers."""
from __future__ import annotations

import os
import shutil
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from team import ledger


@contextmanager
def snapshot_state():
    source = ledger.state_dir()
    original = {key: os.environ.get(key) for key in
                ('TEAM_STATE', 'HERMES_HOME', 'HERMES_RUNTIME_DIR', 'HERMES_KANBAN_HOME')}
    with TemporaryDirectory(prefix='team-dashboard-preview-') as temp:
        dest = Path(temp)
        for relative in ('team.db', 'litellm/usage.db'):
            if (source / relative).exists():
                (dest / relative).parent.mkdir(parents=True, exist_ok=True)
                with sqlite3.connect(f'{(source / relative).as_uri()}?mode=ro', uri=True) as old:
                    with sqlite3.connect(dest / relative) as new:
                        old.backup(new)
        if (source / 'boss' / 'chat.json').is_file():
            (dest / 'boss').mkdir()
            shutil.copy2(source / 'boss' / 'chat.json', dest / 'boss' / 'chat.json')
        for record in (source / 'targets').glob('*/target.json'):
            target = dest / 'targets' / record.parent.name
            target.mkdir(parents=True)
            shutil.copy2(record, target / record.name)
            for relative in ('scout/report.json', 'scout/ideas.json', 'scout/review-status.json', 'routines.toml'):
                path = record.parent / relative
                if path.is_file():
                    (target / relative).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, target / relative)
            if (record.parent / 'overlay').is_dir():
                shutil.copytree(record.parent / 'overlay', target / 'overlay', symlinks=True)
        try:
            os.environ.update(TEAM_STATE=str(dest), HERMES_HOME=str(dest / 'hermes'),
                              HERMES_RUNTIME_DIR=str(dest / 'hermes-runtime'),
                              HERMES_KANBAN_HOME=str(dest / 'kanban'))
            yield dest
        finally:
            for key, value in original.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
