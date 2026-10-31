"""Gateway workers retain tool scope, routing and accounting contracts."""

import json
import subprocess
from pathlib import Path

from team import hermes_run, ledger, model_tracking
from team.worker_guard import guard

REPO = Path(__file__).resolve().parents[2]


def test_gateway_subprocess_scrubs_parent_ownership(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "boss-task")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-leak")
    monkeypatch.setenv("PYTHONPATH", "/runtime/dependencies")

    def runner(cmd, **kw):
        assert cmd[1:4] == ["-P", "-m", "team.hermes_run"]
        request = json.loads(cmd[4])
        assert request["schema"] == {"type": "object"}
        assert request["model"] == hermes_run.LEAD_MODEL
        assert "HERMES_KANBAN_TASK" not in kw["env"]
        assert "ANTHROPIC_API_KEY" not in kw["env"]
        assert "/runtime/dependencies" in kw["env"]["PYTHONPATH"]
        return subprocess.CompletedProcess(
            cmd,
            0,
            json.dumps(
                {
                    "result": "{}",
                    "response_model": "served",
                    "usage": {"input_tokens": 5},
                }
            ),
            "",
        )

    result = hermes_run.run(
        REPO,
        "plan",
        role="lead",
        root=REPO,
        json_schema={"type": "object"},
        runner=runner,
    )
    assert result["ok"] and result["response_model"] == "served"


def test_worker_policy_blocks_edits_and_patch_bypasses(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    readonly = ["read_file", "search_files"]
    assert guard({}, readonly, "read_file", {"path": "a.py"}) is None
    assert (
        guard({}, readonly, "terminal", {"command": "touch a.py"})["action"] == "block"
    )
    request = {"allowed_files": ["a.py"]}
    tools = ["write_file", "patch"]
    assert guard(request, tools, "write_file", {"path": "a.py"}) is None
    for args in (
        {"path": "b.py"},
        {"path": "../a.py"},
        {"mode": "patch", "patch": "*** Add File: b.py"},
    ):
        assert guard(request, tools, "patch", args)["action"] == "block"
    (tmp_path / "a.py").symlink_to(tmp_path.parent / "outside")
    assert guard(request, tools, "write_file", {"path": "a.py"})["action"] == "block"


def test_results_attach_physical_calls_without_double_counting(team_state):
    conn = ledger.connect()
    ledger.init(conn)
    job = ledger.start_job(conn, "team", "sha", "lead", "v")
    ledger.record_call(
        conn,
        None,
        "team-subscription",
        "requested",
        "lead",
        12,
        3,
        50,
        True,
        session_id="s",
        response_model="served",
    )
    model_tracking.record_result(
        conn,
        job,
        {"session_id": "s", "ok": True, "usage": {"input_tokens": 12}},
        "lead",
    )
    calls = ledger.calls_for(conn, job_id=job)
    assert len(calls) == 1 and calls[0]["response_model"] == "served"
    ledger.finish_job(conn, job, "pass", "check", "ok")
    report = "\n".join(model_tracking.findings(conn, ledger.list_jobs(conn)))
    assert "served: 1/1 passed; role+route baseline 1/1" in report
    conn.close()


def test_retry_models_are_not_mixed_by_reused_kanban_task(team_state):
    conn = ledger.connect()
    ledger.init(conn)
    for model, outcome in (("first", "fail"), ("second", "pass")):
        job = ledger.start_job(
            conn,
            "team",
            "sha",
            "implement",
            "v",
            task_ref="same-task",
            model_choice={"spec": "tier-medium", "model": "tier-medium"},
        )
        ledger.record_call(
            conn,
            job,
            "litellm",
            "tier-medium",
            "implement",
            10,
            2,
            5,
            True,
            task_ref="same-task",
            response_model=model,
        )
        ledger.finish_job(conn, job, outcome, "check", "result")
    report = "\n".join(model_tracking.findings(conn, ledger.list_jobs(conn)))
    assert "first: 0/1 passed; role+route baseline 1/2" in report
    assert "second: 1/1 passed; role+route baseline 1/2" in report
    assert "mixed:" not in report
    conn.close()


def test_timeout_retains_session_for_call_attribution():
    captured = {}

    def timeout(cmd, **kw):
        captured.update(json.loads(cmd[4]))
        raise subprocess.TimeoutExpired(cmd, 1)

    result = hermes_run.run(
        REPO, "plan", root=REPO, role="lead", runner=timeout, timeout_s=1
    )
    assert not result["ok"]
    assert result["session_id"] == captured["session_id"]
    assert result["model"] == captured["model"]


def test_gateway_affinity_is_joined_without_overwriting_local_session(team_state):
    import sqlite3

    conn = ledger.connect()
    ledger.init(conn)
    job = ledger.start_job(
        conn,
        "team",
        "sha",
        "implement",
        "v",
        model_choice={"model": "tier-medium", "spec": "tier-medium"},
    )
    ledger.record_call(
        conn,
        job,
        "litellm",
        "tier-medium",
        "implement",
        10,
        2,
        5,
        True,
        session_id="local-id",
        gateway_session_id="gwk-affinity",
        response_model="tier-medium",
    )
    path = team_state / "litellm" / "usage.db"
    path.parent.mkdir()
    with sqlite3.connect(path) as gateway:
        gateway.execute(
            'CREATE TABLE calls(session TEXT, "group" TEXT, model TEXT, ok INTEGER, ts TEXT)'
        )
        gateway.execute(
            "INSERT INTO calls VALUES (?, ?, ?, ?, ?)",
            ("gwk-affinity", "tier-medium", "openai/actual-model", 1, ledger._now()),
        )
    record = ledger.list_jobs(conn)[0]
    assert model_tracking.responders(conn, record) == ["actual-model"]
    call = ledger.calls_for(conn, job_id=job)[0]
    assert call["response_model"] == "actual-model" and call["session_id"] == "local-id"
    conn.close()


def test_worktree_cannot_shadow_the_running_team_checkout(tmp_path):
    shadow = tmp_path / "team"
    shadow.mkdir()
    (shadow / "__init__.py").write_text('raise RuntimeError("old worktree package loaded")\n')

    def runner(cmd, **kwargs):
        probe = subprocess.run(
            [*cmd[:2], "-c", "import team; print(team.__file__)"],
            check=False,
            **{k: v for k, v in kwargs.items() if k != "check"},
        )
        assert probe.returncode == 0, probe.stderr
        assert Path(probe.stdout.strip()).resolve() == REPO / "team" / "__init__.py"
        return subprocess.CompletedProcess(cmd, 0, '{"result":"{}"}', "")

    result = hermes_run.run(tmp_path, "plan", root=REPO, role="lead", runner=runner)
    assert result["ok"]
