"""Attempt attribution: preserve requested tiers and identify actual responders."""

from __future__ import annotations

import sqlite3
from collections import defaultdict

from team import claude_run, ledger


def record_result(conn, job_id, result, role):
    session = result.get("session_id")
    with conn:
        count = (
            conn.execute(
                "UPDATE calls SET job_id=? WHERE session_id=? AND job_id IS NULL",
                (job_id, session),
            ).rowcount
            if session
            else 0
        )
    if count:
        return  # The Hermes sink already recorded every physical call.
    usage = result.get("usage") or {}
    ledger.record_call(
        conn,
        job_id,
        "team-subscription",
        result.get("model") or "unknown",
        role,
        claude_run.input_tokens(usage),
        usage.get("output_tokens"),
        result.get("duration_ms"),
        bool(result["ok"]),
        None if result["ok"] else str(result.get("result")),
        session_id=session,
        profile=role,
        response_model=result.get("response_model"),
        base_url="http://127.0.0.1:4000/v1",
    )


def responders(conn, job):
    calls = ledger.calls_for(conn, job_id=job["id"])
    models = {
        c["response_model"]
        for c in calls
        if c["ok"]
        and c["response_model"]
        and not c["response_model"].startswith("tier-")
    }
    sessions = {
        c.get("gateway_session_id") or c["session_id"]
        for c in calls
        if c.get("gateway_session_id") or c["session_id"]
    }
    path = ledger.state_dir() / "litellm" / "usage.db"
    # Read-only: reporting must not create a gateway database.
    if sessions and path.exists():
        routed = set()
        with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as gateway:
            for session in sessions:
                rows = gateway.execute(
                    'SELECT DISTINCT model FROM calls WHERE session=? AND ok=1 AND "group"=? AND ts>=? AND ts<=?',
                    (
                        session,
                        (job.get("model_choice") or {}).get("model"),
                        job["started"],
                        job.get("ended") or ledger._now(),
                    ),
                ).fetchall()
                routed.update(r[0].removeprefix("openai/") for r in rows if r[0])
        if routed:
            models = routed
            if len(routed) == 1:
                with conn:
                    conn.execute(
                        "UPDATE calls SET response_model=? WHERE job_id=? AND (response_model IS NULL OR response_model LIKE 'tier-%') AND model=?",
                        (
                            next(iter(routed)),
                            job["id"],
                            (job.get("model_choice") or {}).get("model"),
                        ),
                    )
    return sorted(models)


def findings(conn, jobs):
    groups = defaultdict(lambda: [0, 0])
    bases = defaultdict(lambda: [0, 0])
    for job in jobs:
        if job["outcome"] not in ("pass", "fail", "error", "timeout"):
            continue
        route = (job.get("model_choice") or {}).get("spec") or "unknown"
        baseline = (job["role"], route)
        names = responders(conn, job)
        # Mixed attempts stay a separate cohort; don't award each model the same pass.
        label = ", ".join(names) if names else "unknown responder"
        if len(names) > 1:
            label = "mixed: " + label
        key = (*baseline, label)
        for tally in (groups[key], bases[baseline]):
            tally[0] += int(job["outcome"] == "pass")
            tally[1] += 1
    out = [
        "Model outcomes (whole attempts; mixed responders grouped; small samples are not rankings):"
    ]
    for (role, route, name), (passed, total) in sorted(groups.items()):
        bp, bn = bases[(role, route)]
        out.append(
            f"  {role} / {route} / {name}: {passed}/{total} passed; role+route baseline {bp}/{bn}"
        )
    return out
