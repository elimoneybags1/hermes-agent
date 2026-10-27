"""Run the launchers across a task cwd without provisioning or calling a model."""

import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


def script(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)


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


def test_team_entry_uses_pinned_runtime_from_another_cwd(tmp_path, team_state):
    import shlex
    import shutil

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
    (main / "team/bin/team").symlink_to("team-hermes")
    (main / "team/__init__.py").write_text("")
    (main / "team/__main__.py").write_text('print("Team CLI")\n')
    (wt / "team.py").write_text('raise RuntimeError("task cwd shadowed Team")\n')
    result = subprocess.run(
        [str(main / "team/bin/team"), "--help"],
        cwd=wt,
        env={**os.environ, "TEAM_STATE": str(team_state), "PYTHONPATH": str(main)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "Team CLI"
