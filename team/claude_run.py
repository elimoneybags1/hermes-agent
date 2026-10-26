"""Compatibility entry point for Claude workers, now executed by the full Hermes runtime."""
from __future__ import annotations

import subprocess
from pathlib import Path

from team.claude_transport import SCRUB, TransportError as ClaudeRunError, clean_env
from team.harness import Session


def legacy_run(worktree, brief, *, max_turns=60, timeout_s=1800, runner=subprocess.run,
        role=None, root=None, tools=None, model=None, json_schema=None,
        allowed_files=None, check=None, decision_file=None):
    if json_schema is not None:
        raise ValueError("Worker output schemas belong in Team grading; transport uses the Team action schema")
    root = Path(root) if root else Path(__file__).resolve().parent.parent
    session = Session(root, worktree, role or "implement", runner=runner,
                      model=model, tools=tools, allowed_files=allowed_files)
    return session.turn(brief, max_turns=max_turns, timeout_s=timeout_s,
                        check=check, decision_file=decision_file)


def run(worktree, brief, *, max_turns=60, timeout_s=1800, runner=subprocess.run,
        role=None, root=None, tools=None, model=None, json_schema=None,
        allowed_files=None, check=None, decision_file=None):
    """Run the fork's real Hermes agent; the subscription gateway owns inference only."""
    import json
    import tempfile
    import time
    from team import hermes_runtime, ledger, grade
    if tools is not None or json_schema is not None or allowed_files is not None:
        raise ValueError("Configure native Hermes capabilities in the role; legacy per-call tool/schema/file overrides are not supported")
    root = Path(root) if root else Path(__file__).resolve().parent.parent
    role = role or "implement"
    home = hermes_runtime.prepare_profile(role, root)
    started = time.monotonic()
    with tempfile.NamedTemporaryFile(suffix=".json", dir=home, delete=False) as output:
        result_path = Path(output.name)
    try:
        proc, calls = hermes_runtime.launch(role, root=root, cwd=worktree, prompt=brief,
            model=model or "opus", max_turns=max_turns, timeout_s=timeout_s,
            runner=runner, capture=True, decision_file=decision_file, result_file=result_path)
        result = json.loads(result_path.read_text()) if result_path.stat().st_size else {
            "ok": False, "result": (proc.stderr or proc.stdout or "Hermes produced no result")[-3000:],
            "session_id": None, "num_turns": None, "structured": None}
        result["ok"] = bool(result["ok"] and proc.returncode == 0)
        if calls:
            result["usage"] = {
                "input_tokens": sum(c["usage"]["prompt_tokens"] for c in calls),
                "output_tokens": sum(c["usage"]["completion_tokens"] for c in calls)}
        result["duration_ms"] = int((time.monotonic() - started) * 1000)
        if result["ok"] and check:
            ok, detail = grade.run_check(worktree, check, timeout_s, runner=runner)
            if not ok:
                result.update(ok=False, result=f"Team check failed: {detail}")
        return result
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "result": str(exc), "session_id": None, "num_turns": None,
                "structured": None, "usage": None}
    finally:
        result_path.unlink(missing_ok=True)
