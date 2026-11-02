"""usage_breakdown groups token totals by model, role and goal."""
import json

import pytest

from team import ledger, usage
from team.ledger import state_dir


@pytest.fixture
def conn(tmp_path):
    c = ledger.connect(tmp_path / "team.db")
    ledger.init(c)

    # usage_breakdown requires the target to exist; register 'team' here so
    # targets.require("team") accepts it. TEAM_STATE is sandboxed per test by
    # tests/team/conftest.py, so this never touches $HOME/state/team.
    target_dir = state_dir() / "targets" / "team"
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "target.json").write_text(
        json.dumps(
            {
                "name": "team",
                "path": str(tmp_path / "repo"),
                "commit": "fake-commit-id",
                "attached_at": "2024-01-01T00:00:00Z",
                "board": "team",
                "check": None,
                "enabled": True,
            }
        )
    )
    yield c
    c.close()


def test_breakdown_groups_sum_to_total(conn):
    # Start three jobs: two 'implement', one 'lead'.
    job1 = ledger.start_job(conn, "team", "c", "implement", "v")
    job2 = ledger.start_job(conn, "team", "c", "implement", "v")
    job3 = ledger.start_job(conn, "team", "c", "lead", "v")

    # Create two goals and attach job 1 to goal 1, job 2 to goal 2.
    goal1 = ledger.create_goal(conn, "team", "g", "implement", "b", "/wt", "pytest")
    goal2 = ledger.create_goal(conn, "team", "g", "implement", "b", "/wt", "pytest")
    ledger.update_goal(conn, goal1, job_id=job1)
    ledger.update_goal(conn, goal2, job_id=job2)

    # Record five calls:
    # 1. response_model 'glm-5.3' with requested model 'tier-medium' -> glm-5.3
    # 2. response_model None with model 'tier-medium' -> tier-medium
    # 3. response_model None with model 'unknown' -> unknown
    # 4. lead job, task_class 'lead', 100 in / 200 out
    # 5. tokens_in and tokens_out both NULL
    ledger.record_call(
        conn, job1, "litellm", "tier-medium", "implement", None, None, 10, True, response_model="glm-5.3"
    )
    ledger.record_call(
        conn, job2, "litellm", "tier-medium", "implement", None, None, 10, True
    )
    ledger.record_call(
        conn, job1, "litellm", "unknown", "implement", None, None, 10, True
    )
    ledger.record_call(
        conn, job3, "litellm", "tier-medium", "lead", 100, 200, 10, True, response_model="tier-medium"
    )
    ledger.record_call(
        conn, job2, "litellm", "tier-medium", "implement", None, None, 10, True, response_model="tier-medium"
    )

    result = usage.usage_breakdown(conn, "team")

    expected_by_model = {
        "glm-5.3": {"input": 0, "output": 0, "calls": 1},
        "tier-medium": {"input": 100, "output": 200, "calls": 3},
        "unknown": {"input": 0, "output": 0, "calls": 1},
    }
    expected_by_role = {
        "implement": {"input": 0, "output": 0, "calls": 4},
        "lead": {"input": 100, "output": 200, "calls": 1},
    }
    expected_by_goal = {
        str(goal1): {"input": 0, "output": 0, "calls": 2},
        str(goal2): {"input": 0, "output": 0, "calls": 2},
        "none": {"input": 100, "output": 200, "calls": 1},
    }
    expected_total = {"input": 100, "output": 200, "calls": 5}

    assert result["by_model"] == expected_by_model
    assert result["by_role"] == expected_by_role
    assert result["by_goal"] == expected_by_goal
    assert result["total"] == expected_total

    # Each group's input/output/calls sum to the overall totals.
    for group in (result["by_model"], result["by_role"], result["by_goal"]):
        assert sum(bucket["input"] for bucket in group.values()) == expected_total["input"]
        assert sum(bucket["output"] for bucket in group.values()) == expected_total["output"]
        assert sum(bucket["calls"] for bucket in group.values()) == expected_total["calls"]


def test_breakdown_ignores_other_targets_and_unattached_calls(conn):
    team_job = ledger.start_job(conn, "team", "c", "implement", "v")

    # A call with no job_id must be excluded (the join drops it).
    ledger.record_call(conn, None, "litellm", "tier-medium", "implement", None, None, 10, True)

    goal = ledger.create_goal(conn, "team", "g", "implement", "b", "/wt", "pytest")
    ledger.update_goal(conn, goal, job_id=team_job)
    ledger.record_call(conn, team_job, "litellm", "tier-medium", "implement", None, None, 10, True)

    result = usage.usage_breakdown(conn, "team")

    assert result["total"] == {"input": 0, "output": 0, "calls": 1}
    assert result["by_model"] == {"tier-medium": {"input": 0, "output": 0, "calls": 1}}
    assert result["by_role"] == {"implement": {"input": 0, "output": 0, "calls": 1}}
    assert result["by_goal"] == {str(goal): {"input": 0, "output": 0, "calls": 1}}
    assert "none" not in result["by_goal"]
