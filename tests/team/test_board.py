"""The Team tab's board: role columns, the model that answered and its rank, every token."""

import sqlite3
from pathlib import Path

from team import board, ledger
from tests.team.test_catalog import CATALOG

REPO = Path(__file__).resolve().parents[2]


def gateway(state, rows):
    (state / "litellm").mkdir()
    gw = sqlite3.connect(state / "litellm" / "usage.db")
    gw.execute(
        'CREATE TABLE calls (id INTEGER PRIMARY KEY, ts TEXT, session TEXT, "group" TEXT, tier TEXT,'
        " deployment TEXT, config TEXT, model TEXT, tokens_in INTEGER, tokens_out INTEGER,"
        " latency_ms INTEGER, ok INTEGER, rate_limited INTEGER DEFAULT 0, error TEXT)"
    )
    gw.executemany(
        'INSERT INTO calls (ts, session, "group", tier, model, tokens_in, tokens_out, ok, rate_limited, error)'
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    gw.commit()
    gw.close()


# Three medium models in LiteLLM's order, one no provider serves, and LiteLLM's cooldown.
CAT = {
    "models": [
        {"id": "m-first", "tier": "medium", "order": 1, "served_by": [{"provider": "p"}]},
        {"id": "m-third", "tier": "medium", "order": 3, "served_by": [{"provider": "p"}]},
        {"id": "m-second", "tier": "medium", "order": 2, "served_by": [{"provider": "p"}]},
        {"id": "m-gone", "tier": "medium", "order": 4, "served_by": [{"provider": "p", "unavailable": "410"}]},
        {"id": "m-free", "served_by": [{"provider": "p"}]},
    ],
    "policy": {"litellm": {"cooldown_time": 300}},
}


def job(conn, goal_id, spec, outcome="pass", attempt=1, started="2026-10-30T10:00:00+00:00"):
    j = ledger.start_job(conn, "team", "abc", "implement", "v1", attempt=attempt,
                         task_ref=f"t_{goal_id}_{attempt}",
                         model_choice={"spec": spec, "model": spec, "why": "test"})
    conn.execute("UPDATE jobs SET started=? WHERE id=?", (started, j))
    ledger.finish_job(conn, j, outcome, "check", "1 passed")
    conn.execute("UPDATE jobs SET ended=? WHERE id=?", ("2026-10-30T10:10:00+00:00", j))
    ledger.update_goal(conn, goal_id, job_id=j)
    return j


def test_the_board_names_the_model_its_rank_and_why_not_first(team_state):
    conn = ledger.connect(team_state / "team.db")
    ledger.init(conn)
    assert board.ranks(CAT) == {"medium": ["m-first", "m-second", "m-third"]}
    first, second, third = board.ranks(CAT)["medium"]
    gateway(team_state, [
        # #1 failed four minutes before the attempt: inside LiteLLM's cooldown.
        ("2026-10-30T09:56:00+00:00", "x", "tier-medium", "medium", f"openai/{first}", 0, 0, 0, 1, "429"),
        ("2026-10-30T10:01:00+00:00", "s1", "tier-medium", "medium", f"openai/{third}", 0, 0, 0, 0, "500"),
        ("2026-10-30T10:02:00+00:00", "s1", "tier-medium", "medium", f"openai/{second}", 900, 20, 1, 0, None),
    ])
    g = ledger.create_goal(conn, "team", "Set VALUE to 2", "implement", "team/b", "/w", "./check.sh")
    j = job(conn, g, "tier-medium")
    ledger.finish_goal(conn, g, "pass", "landed")
    ledger.record_call(conn, j, "litellm", "tier-medium", "implement", 900, 20, 10, True, None,
                       session_id="s1", profile="implement", response_model="tier-medium")
    ledger.record_call(conn, None, "team-subscription", "claude-sonnet-5-5", "boss", 4738, 300, 10, True,
                       None, profile="boss")

    b = board.board(conn, REPO, CAT)
    [card] = b["done"]
    route = card["attempts"][0]["route"]
    assert route["by"] == "session"
    assert route["models"] == [{"model": second, "calls": 1, "rank": 2, "of": 3}]
    assert [t["model"] for t in route["tried"]] == [third, second] and route["top_tried"] is False
    assert route["passed_over"] == [{"model": first, "rank": 1, "why": "rate limited"}]
    t = b["tokens"]
    assert t["total"]["tokens_in"] == 5638 and t["zones"]["boss"]["tokens_in"] == 4738
    assert t["zones"]["implement"]["tokens_out"] == 20
    assert t["total"]["cache_read"] is None  # not recorded yet, so not shown as zero
    tier = next(x for x in b["tiers"] if x["tier"] == "medium")
    assert tier["models"][0] == {**tier["models"][0], "model": first, "rank": 1, "calls": 1, "ok": 0}


def test_goals_sit_in_the_column_of_their_latest_attempt(team_state):
    conn = ledger.connect(team_state / "team.db")
    ledger.init(conn)
    running = ledger.create_goal(conn, "team", "a", "implement", "team/a", "/w", "./c")
    job(conn, running, "tier-medium", outcome="fail")
    job(conn, running, "claude", outcome="fail", attempt=3)
    conn.execute("UPDATE jobs SET ended=NULL, outcome=NULL WHERE attempt=3")
    waiting = ledger.create_goal(conn, "team", "b", "implement", "team/b", "/w", "./c")
    b = board.board(conn, REPO, CATALOG)
    assert [c["id"] for c in b["columns"]["rescue"]] == [running]
    assert [c["id"] for c in b["columns"]["lead"]] == [waiting]
    assert b["columns"]["implement"] == [] and b["done"] == []
    assert b["configured"]["boss"]["model"] == "claude-sonnet-5-5"
