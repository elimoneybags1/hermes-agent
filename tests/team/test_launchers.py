"""Run the launchers across a task cwd without provisioning or calling a model."""

import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


def script(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)


def test_worker_imports_pinned_checkout_instead_of_task_cwd(tmp_path, team_state):
    main = tmp_path / "main"
    wt = tmp_path / "task"
    wt.mkdir()
    script(
        main / "scripts/run-in-hermes-env",
        "#!/bin/sh\nexport PATH="
        + shlex.quote(str(Path(sys.executable).parent))
        + ':"$PATH"\nexec "$@"\n',
    )
    script(main / "team/bin/team-hermes", "")
    shutil.copy(ROOT / "team/bin/team-hermes", main / "team/bin/team-hermes")
    script(
        main / "hermes",
        'import subprocess,sys\nsys.exit(subprocess.call([sys.executable,"-m","worker_probe"]))\n',
    )
    (main / "worker_probe.py").write_text('print("installed checkout")\n')
    (wt / "worker_probe.py").write_text(
        'raise RuntimeError("worktree shadowed the installed checkout")\n'
    )
    result = subprocess.run(
        [str(main / "team/bin/team-hermes")],
        cwd=wt,
        env={**os.environ, "TEAM_STATE": str(team_state), "PYTHONPATH": str(main)},
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "installed checkout"


@pytest.mark.parametrize("test_exit,lint_exit", [(1, 0), (0, 2), (0, 0)])
def test_check_preserves_test_and_lint_exit_codes(tmp_path, test_exit, lint_exit):
    script(
        tmp_path / "scripts/run_tests.sh",
        f'#!/bin/sh\necho "test output"\nexit {test_exit}\n',
    )
    # A separate executable stands in for the PM interpreter's ruff command.
    python = tmp_path / "python"
    script(python, f"#!/bin/sh\nexit {lint_exit}\n")
    result = subprocess.run(
        [sys.executable, str(ROOT / "team/bin/team-check"), str(tmp_path)],
        env={**os.environ, "__HERMES_TEST_PYTHON": str(python)},
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == (test_exit or lint_exit), result.stderr
    assert "test output" in result.stdout
