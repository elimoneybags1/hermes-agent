"""Read-only views the app serves beside the board: the roster with its numbers, and the
gateway as a diagram.

Everything here is derived from files and databases that exist (role.toml, the ledger,
LiteLLM's usage.db, the catalog, launchd); nothing is a model's word.
"""

from __future__ import annotations

import sqlite3
import tomllib
from pathlib import Path

from team import boss, gates, ledger, overlay, roles, targets
from team.ledger import CODE_ROLE


def role_rates(conn: sqlite3.Connection, target: str | None = None) -> dict[str, dict]:
    """Per role: passes, work attempts (infra deaths left out), infra deaths; agents only."""
    sql = (
        "SELECT role, COALESCE(SUM(outcome = 'pass'), 0), COUNT(*),"
        " COALESCE(SUM(failure_kind = 'infra'), 0) FROM jobs WHERE ended IS NOT NULL AND role != ?"
    )
    args: list = [CODE_ROLE]
    if target:
        sql += " AND target = ?"
        args.append(target)
    sql += " GROUP BY role"
    out = {}
    for role, p, n, infra in conn.execute(sql, args):
        out[role] = {"pass": int(p), "n": int(n) - int(infra), "infra": int(infra)}
    return out


def agents(root: Path, conn: sqlite3.Connection, cat: dict | None, target: str | None = None) -> dict:
    """The roster as the Agents page shows it: every role, its gate, tools, models, skills,
    status, overlays, and its pass rate next to the roster's; the pipelines; proposals waiting."""
    rates = role_rates(conn, target)
    all_rates = role_rates(conn) if target else rates
    names = [t["name"] for t in targets.list_targets()]
    rows = []
    for name in roles.names(root):
        raw = tomllib.loads((roles.roles_dir(root) / name / "role.toml").read_text())
        try:
            r = roles.load(root, name, cat, target=target)
            problem = None
            gate, skills, tools = r.get("gate", "check"), r["skills"], r.get("tools", [])
            cands, harness = r["models"]["candidates"], r["runs_on"]["harness"]
            escalation, budget = r.get("escalation") or {}, r.get("budget") or {}
        except roles.RoleError as e:
            problem, gate, skills, tools, cands = str(e), raw.get("gate", "check"), [], raw.get("tools", []), []
            harness, escalation, budget = raw.get("runs_on", {}).get("harness"), raw.get("escalation") or {}, raw.get("budget") or {}
        rows.append({
            "name": name,
            "title": raw.get("title", name),
            "does": raw.get("does", ""),
            "graded_by": raw.get("graded_by", ""),
            "status": raw.get("status"),
            "gate": gate,
            "gate_means": gates.GATES.get(gate, ""),
            "tools": tools,
            "candidates": cands,
            "skills": skills,
            "extends": raw.get("extends"),
            "assignable": raw.get("assignable", True),
            "harness": harness,
            "model": raw.get("runs_on", {}).get("model"),
            "escalation": escalation,
            "budget": budget,
            "overlays": [t for t in names if overlay.exists(t, name)],
            "rate": rates.get(name, {"pass": 0, "n": 0, "infra": 0}),
            "rate_all": all_rates.get(name, {"pass": 0, "n": 0, "infra": 0}),
            "problem": problem,
        })
    from team import board

    stations = [s for s in board.stations(root) if s not in ("lead", "rescue")]
    pipelines = [
        {
            "type": kind,
            "stages": [{"name": s.name, "role": s.role, "gate": s.gate} for s in stages],
            "landing": gates.LANDING[kind],
        }
        for kind, stages in gates.PIPELINES.items()
    ]
    return {
        "roles": rows,
        "stations": stations,
        "pipelines": pipelines,
        "gates": gates.GATES,
        "proposals": [a for a in boss.agents() if a["status"] == "waiting"],
        "toolsets": list(roles.TOOLSETS),
        "targets": names,
        "target": target,
    }


# --- the gateway as a diagram ------------------------------------------------------------


def _usage_rates(hours: int = 24) -> dict[str, dict]:
    """Per catalog model: calls and successes in LiteLLM's log over the last ``hours``."""
    path = ledger.state_dir() / "litellm" / "usage.db"
    if not path.exists():
        return {}
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT model, COUNT(*), COALESCE(SUM(ok), 0), COALESCE(SUM(rate_limited), 0) FROM calls"
            f" WHERE ts >= datetime('now', '-{int(hours)} hours') GROUP BY model"
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    out = {}
    for model, n, ok, rl in rows:
        name = str(model or "")
        name = name.split("/", 1)[1] if name.startswith("openai/") else name
        cur = out.setdefault(name, {"n": 0, "ok": 0, "rl": 0})
        cur["n"] += int(n)
        cur["ok"] += int(ok)
        cur["rl"] += int(rl)
    return out


def _mid(text: str) -> str:
    """A Mermaid node id: letters, digits and underscores only."""
    return "".join(c if c.isalnum() else "_" for c in text)


def _label(text: str) -> str:
    return text.replace('"', "'")


def gateway_diagram(cat: dict, services: dict | None = None, hours: int = 24) -> dict:
    """The route every call takes, as a Mermaid flowchart with live numbers on it.

    Callers → LiteLLM (the model) → Portkey (the provider) → providers. Each tier lists its
    models in LiteLLM's order with their success rate over the last ``hours`` from usage.db;
    a model with every provider unavailable is drawn dimmed. ``services`` is
    ``gatewayctl.service_status`` (healthy or not per hop).
    """
    services = services or {}
    rates = _usage_rates(hours)
    ports = {
        "litellm": (cat.get("litellm") or {}).get("port", 4000),
        "portkey": (cat.get("gateway") or {}).get("port", 8788),
    }

    def health(name: str) -> str:
        s = services.get(name) or {}
        if not s:
            return ""
        return " ✓ up" if s.get("healthy") else " ✗ down"

    lines = ["flowchart LR"]
    lines.append('  callers["Hermes profiles<br/>boss · lead · workers<br/>and every other tool"]')
    lines.append(f'  litellm["LiteLLM :{ports["litellm"]}{health("litellm")}<br/>chooses the model<br/>tiers · fallbacks · session pin"]')
    lines.append(f'  portkey["Portkey :{ports["portkey"]}{health("portkey")}<br/>chooses the provider<br/>config by name · failover · request log"]')
    lines.append("  callers --> litellm --> portkey")
    providers = cat.get("providers") or {}
    for name in providers:
        lines.append(f'  prov_{_mid(name)}(["{_label(name)}"])')
        lines.append(f"  portkey --> prov_{_mid(name)}")
    models = cat.get("models") or []
    tiers = [t for t in (cat.get("tiers") or []) if any(m.get("tier") == t for m in models)]
    for tier in tiers:
        lines.append(f'  subgraph tier_{_mid(tier)}["tier-{_label(tier)}"]')
        lines.append("    direction TB")
        ordered = sorted((m for m in models if m.get("tier") == tier), key=lambda m: m.get("order", 99))
        for m in ordered:
            live = [s for s in m.get("served_by", []) if not s.get("unavailable")]
            r = rates.get(m["id"])
            stat = f"<br/>{r['ok']}/{r['n']} ok in {hours}h" if r and r["n"] else f"<br/>no calls in {hours}h"
            if r and r.get("rl"):
                stat += f" · {r['rl']} rate-limited"
            via = " · ".join(s["provider"] for s in live) or "no provider live"
            lines.append(f'    m_{_mid(m["id"])}["#{m.get("order", "?")} {_label(m["id"])}<br/>{_label(via)}{stat}"]')
            if not live:
                lines.append(f"    style m_{_mid(m['id'])} opacity:0.45,stroke-dasharray:4 3")
        lines.append("  end")
        lines.append(f"  litellm -.-> tier_{_mid(tier)}")
    for m in models:
        if not m.get("tier"):
            continue
        for s in m.get("served_by", []):
            if s.get("unavailable") or s["provider"] not in providers:
                continue
            lines.append(f"  m_{_mid(m['id'])} -.-> prov_{_mid(s['provider'])}")
    model_rows = [
        {
            "id": m["id"], "tier": m.get("tier"), "order": m.get("order"),
            "providers": [s["provider"] for s in m.get("served_by", []) if not s.get("unavailable")],
            "unavailable": [s["provider"] for s in m.get("served_by", []) if s.get("unavailable")],
            "rate": rates.get(m["id"]),
        }
        for m in models if m.get("tier")
    ]
    return {"mermaid": "\n".join(lines), "hours": hours, "models": model_rows, "services": services, "ports": ports}
