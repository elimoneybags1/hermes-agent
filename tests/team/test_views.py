"""The app's read-only views: the roster with its numbers, and the gateway as a diagram."""

from __future__ import annotations

from team import ledger, overlay
from team.app import server, views
from tests.team.test_boss import team  # fixture
from tests.team.test_catalog import CATALOG
from tests.team.test_run import world  # fixture team builds on


def test_agents_view_carries_every_role_its_gate_and_its_rate(team):
    conn = team["conn"]
    for outcome, detail in (("pass", "ok"), ("fail", "1 failed"), ("error", "kanban gave up after crashed: Error code: 502")):
        j = ledger.start_job(conn, "team", "c", "implement", "v", task_ref="t_" + outcome)
        ledger.finish_job(conn, j, outcome, "check" if outcome != "error" else "kanban", detail)
    overlay.role_dir("team", "implement").mkdir(parents=True)
    (overlay.role_dir("team", "implement") / "SOUL.md").write_text("repo rules")
    data = views.agents(team["root"], conn, CATALOG)
    by = {r["name"]: r for r in data["roles"]}
    assert set(by) == {"boss", "implement", "lead"}
    imp = by["implement"]
    assert imp["gate"] == "check" and "acceptance" in imp["gate_means"] and imp["status"] == "active"
    assert imp["tools"] == ["terminal", "file"] and imp["candidates"] == ["tier-simple", "nvidia:z-ai/glm-5.3"]
    assert imp["skills"] == ["commit-before-done"] and imp["overlays"] == ["team"]
    assert imp["rate"] == {"pass": 1, "n": 2, "infra": 1}  # the infra death is counted apart
    assert by["lead"]["harness"] == "hermes-subscription" and by["lead"]["model"] == "claude-sonnet-5-5"
    assert data["stations"] == ["implement"]
    kinds = {p["type"]: p for p in data["pipelines"]}
    assert [s["name"] for s in kinds["bugfix"]["stages"]] == ["test_write", "implement", "test_verify", "review"]
    assert kinds["security"]["landing"] == "ask" and "check" in data["gates"]
    assert data["proposals"] == [] and "terminal" in data["toolsets"]
    narrowed = views.agents(team["root"], conn, CATALOG, "other")
    assert narrowed["roles"][1]["rate"] == {"pass": 0, "n": 0, "infra": 0} and narrowed["roles"][1]["rate_all"]["pass"] == 1


def test_gateway_diagram_is_mermaid_with_the_route_and_every_routed_model():
    d = views.gateway_diagram(CATALOG, {"litellm": {"healthy": True, "port": 4000}, "portkey": {"healthy": False, "port": 8788}})
    text = d["mermaid"]
    assert text.startswith("flowchart LR")
    assert "callers --> litellm --> portkey" in text
    assert f"LiteLLM :{CATALOG['litellm']['port']} ✓ up" in text and f"Portkey :{CATALOG['gateway']['port']} ✗ down" in text
    routed = [m for m in CATALOG["models"] if m.get("tier")]
    for m in routed:
        assert m["id"] in text
    for tier in {m["tier"] for m in routed}:
        assert f'tier-{tier}"]' in text
    assert len(d["models"]) == len(routed) and all(m["rate"] is None for m in d["models"])  # no usage.db in tests
    assert d["ports"] == {"litellm": CATALOG["litellm"]["port"], "portkey": CATALOG["gateway"]["port"]}


def test_the_app_routes_the_new_views(team, monkeypatch):
    app = server.App(team["root"], chat=type("C", (), {"view": lambda s: {}})())
    monkeypatch.setattr(server, "_cat", lambda: CATALOG)
    agents = app.get("/api/agents", {})
    assert [r["name"] for r in agents["roles"]] == ["boss", "implement", "lead"]
    board = app.get("/api/board", {"target": ["team"]})
    assert board["target"] == "team" and board["targets"] == ["team"] and "implement" in board["columns"]
    assert "lead" in board["configured"]["stations"]
