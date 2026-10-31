"""Team tab backend, mounted at /api/plugins/team/: the inbox of plans and decisions.

Thin wrappers over ``team.boss`` and ``team.ledger``. Approving here only
answers the decision; ``team run`` releases approved plans on its next tick.
"""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from team import board as board_mod
from team import boss, ledger

router = APIRouter()


def _conn():
    conn = ledger.connect()
    ledger.init(conn)
    return conn


class Answer(BaseModel):
    answer: str


@router.get("/flow")
def flow() -> dict:
    with _conn() as conn:
        return {"stations": list(boss.STATIONS), "plans": boss.flow(conn)}


@router.get("/board")
def board() -> dict:
    with _conn() as conn:
        return board_mod.board(conn, _REPO)


@router.get("/inbox")
def inbox() -> dict:
    with _conn() as conn:
        decisions = [
            d
            for d in ledger.list_decisions(conn)
            if not d["source"].startswith(("boss-plan:", "boss-goal:"))
        ]
    return {"plans": [boss.card(p) for p in boss.proposals()[:30]], "decisions": decisions}


def _plan(plan_id: int) -> dict:
    p = next((x for x in boss.proposals() if x["id"] == plan_id), None)
    if p is None:
        raise HTTPException(404, f"no plan {plan_id}")
    if p["status"] != "open":
        raise HTTPException(409, f"plan {plan_id} is {p['status']}")
    return p


def _settle(decision_id: int, answer: str | None) -> dict:
    with _conn() as conn:
        try:
            boss.settle(conn, decision_id, answer)
        except boss.BossError as e:
            raise HTTPException(409, str(e)) from e
    return {"ok": True}


def _settle_goals(plan_id: int, answer: str | None) -> dict:
    """Every goal of the plan still waiting for Eli; one goal with ``/goals/{n}``."""
    p = _plan(plan_id)
    with _conn() as conn:
        try:
            return {"ok": True, "goals": boss.settle_goals(conn, p, answer)}
        except boss.BossError as e:
            raise HTTPException(409, str(e)) from e


@router.post("/plans/{plan_id}/approve")
def approve(plan_id: int) -> dict:
    return _settle_goals(plan_id, "approve")


@router.post("/plans/{plan_id}/dismiss")
def dismiss(plan_id: int) -> dict:
    return _settle_goals(plan_id, None)


@router.post("/plans/{plan_id}/goals/{n}/approve")
def approve_goal(plan_id: int, n: int) -> dict:
    return _settle(boss.goal_of(_plan(plan_id), n)["decision_id"], "approve")


@router.post("/plans/{plan_id}/goals/{n}/skip")
def skip_goal(plan_id: int, n: int) -> dict:
    return _settle(boss.goal_of(_plan(plan_id), n)["decision_id"], None)


@router.post("/decisions/{decision_id}/answer")
def answer(decision_id: int, body: Answer) -> dict:
    return _settle(decision_id, body.answer)


@router.post("/decisions/{decision_id}/dismiss")
def dismiss_decision(decision_id: int) -> dict:
    return _settle(decision_id, None)
