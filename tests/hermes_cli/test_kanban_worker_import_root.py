"""A task checkout must not shadow the dispatcher's installed Hermes modules."""

import os
from pathlib import Path
import subprocess

from hermes_cli.kanban_db_dispatch import _worker_argv


def test_worker_boots_installed_tree_from_task_checkout(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[2]
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "task"
    workspace.mkdir()
    (workspace / "hermes_bootstrap.py").write_text(
        'raise RuntimeError("wrong checkout")\n'
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("HERMES_KANBAN_")}
    env.update(HERMES_HOME=str(home), PYTHONPATH=str(root))
    env.pop("PYTHONSAFEPATH", None)
    result = subprocess.run(
        _worker_argv(None, "implement", str(home)),
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    # Reaching argument validation proves the real bootstrap completed, without
    # starting an owner or making a model call.
    assert "Kanban owner refused:" in result.stderr, result.stderr
    assert "wrong checkout" not in result.stderr
