"""Look at and change the model gateway: `integrations/bin/gateway show | apply`.

  show [--hours N] [--tier T]
      Every tier in routing order: each model's published facts (catalog params_b,
      active_b, benchmarks) next to its measured record in LiteLLM's usage.db over
      the last N hours (default 168), and the tier's base rate over the same window.
      Rate-limited calls are counted apart, as the agent gate does.

  apply [--no-test]
      After editing catalog.json: build.py, build.py --check, restart LiteLLM (and
      Portkey when policy.portkey changed), wait for health, run both status
      scripts, then one real call to every tier whose models, order or providers
      changed against HEAD (tier-auto when the auto section changed), printing the
      deployment that answered. It never commits.

Stdlib only and Python 3.9, like build.py; run with `python3 -I`.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
CATALOG = HERE / "catalog.json"
LITELLM_JOB = "com.fullmoney.team-litellm"
PORTKEY_JOB = "com.fullmoney.team-portkey"
WEAK_OK_PCT = 50   # same bar as policy.agent_gate.min_ok_pct
MIN_SAMPLES = 5    # below this a rate is shown but not judged


def _load_build():
    spec = importlib.util.spec_from_file_location("portkey_build", HERE / "build.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


build = _load_build()


# ---------- reading usage.db ----------

def usage_db() -> Path:
    return build.state_dir() / "litellm" / "usage.db"


def model_stats(db: Path, hours: float) -> dict[str, dict]:
    """Per catalog model id: calls, ok, rate_limited, fresh asks, fresh asks answered with a tool call, latency."""
    if not db.exists():
        return {}
    since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - hours * 3600))
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = con.execute(
            """SELECT model, COUNT(*), SUM(ok), SUM(rate_limited),
                      SUM(CASE WHEN fresh_ask = 1 THEN 1 ELSE 0 END),
                      SUM(CASE WHEN fresh_ask = 1 AND tool_call = 1 THEN 1 ELSE 0 END),
                      AVG(CASE WHEN ok = 1 THEN latency_ms END)
               FROM calls WHERE ts >= ? AND model IS NOT NULL GROUP BY model""", (since,)).fetchall()
        errors = dict(con.execute(
            """SELECT model, error FROM calls c WHERE ts >= ? AND ok = 0 AND rate_limited = 0 AND error IS NOT NULL
               AND id = (SELECT MAX(id) FROM calls d WHERE d.model = c.model AND d.ok = 0
                         AND d.rate_limited = 0 AND d.error IS NOT NULL AND d.ts >= ?)""",
            (since, since)).fetchall())
    finally:
        con.close()
    out = {}
    for model, n, ok, rl, fresh, fresh_tool, lat in rows:
        mid = model.split("/", 1)[1] if model.startswith("openai/") else model
        out[mid] = {"calls": n, "ok": ok or 0, "rate_limited": rl or 0, "fresh": fresh or 0,
                    "fresh_tool": fresh_tool or 0, "latency_ms": lat, "last_error": errors.get(model)}
    return out


def judged(s: dict) -> tuple[int, int]:
    """(successes, attempts) with rate limits taken out, as the agent gate counts them."""
    attempts = s["calls"] - s["rate_limited"]
    return s["ok"], attempts


def verdict(s: dict | None) -> str:
    if not s or not s["calls"]:
        return "no record"
    ok, attempts = judged(s)
    if attempts < MIN_SAMPLES:
        return "few calls"
    if 100 * ok < WEAK_OK_PCT * attempts:
        return f"WEAK: ok under {WEAK_OK_PCT}%"
    return ""


# ---------- show ----------

def pct(num: int, den: int) -> str:
    return f"{round(100 * num / den)}% of {den}" if den else "-"


def size(m: dict) -> str:
    total, active = m.get("params_b"), m.get("active_b")
    if total is None:
        return "?"
    t = f"{total:g}B"
    if active is None:
        return f"{t}/?"
    return t if active == total else f"{t}/{active:g}B"


def bench(m: dict) -> str:
    bs = m.get("benchmarks") or []
    return "; ".join(f"{b['name']} {b['score']:g}" for b in bs) or "-"


def tier_lines(catalog: dict, tier: str, stats: dict[str, dict]) -> list[str]:
    models = build.tier_models(build.routed(catalog), tier)
    agg = {"calls": 0, "ok": 0, "rate_limited": 0, "fresh": 0, "fresh_tool": 0}
    for m in models:
        for k in agg:
            agg[k] += (stats.get(m["id"]) or {}).get(k, 0)
    ok, attempts = judged(agg)
    lines = [f"tier-{tier}   base rate: ok {pct(ok, attempts)}, rate-limited {agg['rate_limited']}, "
             f"tool call on fresh asks {pct(agg['fresh_tool'], agg['fresh'])}"]
    head = f"  {'#':>2} {'model':<28} {'size':<11} {'ok (excl. 429)':<15} {'429':>4} {'fresh→tool':<12} {'avg s':>6}  benchmarks / flag"
    lines.append(head)
    for m in models:
        s = stats.get(m["id"])
        if s:
            k, a = judged(s)
            lat = f"{s['latency_ms'] / 1000:.1f}" if s["latency_ms"] is not None else "-"
            rec = f"{pct(k, a):<15} {s['rate_limited']:>4} {pct(s['fresh_tool'], s['fresh']):<12} {lat:>6}"
        else:
            rec = f"{'-':<15} {'-':>4} {'-':<12} {'-':>6}"
        flag = verdict(s)
        lines.append(f"  {m['order']:>2} {m['id']:<28} {size(m):<11} {rec}  {bench(m)}"
                     + (f"   [{flag}]" if flag else ""))
        if flag.startswith("WEAK") and s and s.get("last_error"):
            lines.append(f"     last error: {s['last_error'][:150]}")
    return lines


def show(argv: list[str]) -> int:
    hours, only = 168.0, None
    it = iter(argv)
    for a in it:
        if a == "--hours":
            hours = float(next(it))
        elif a == "--tier":
            only = next(it).removeprefix("tier-")
        else:
            print(f"gateway show: unknown argument {a!r}", file=sys.stderr)
            return 2
    catalog = json.loads(CATALOG.read_text())
    stats = model_stats(usage_db(), hours)
    auto = catalog.get("auto") or {}
    print(f"usage.db window: last {hours:g} h   (ok excludes rate-limited calls; 'fresh→tool' = new asks answered with a tool call)")
    if auto and not only:
        mapping = ", ".join(f"{label}→{t} ({(auto.get('efforts') or {}).get(label, '-')})"
                            for label, t in auto["tiers"].items())
        print(f"tier-auto: {mapping}")
    print()
    for t in catalog["tiers"]:
        if only and t != only:
            continue
        print("\n".join(tier_lines(catalog, t, stats)))
        print()
    return 0


# ---------- apply ----------

def tier_shape(catalog: dict) -> dict[str, list]:
    """What a test call can tell apart: each tier's models in order with their live providers."""
    # Read without build.routed(): the HEAD catalog may predate a rule the build now enforces.
    out = {}
    for m in catalog["models"]:
        if m.get("tier") is None:
            continue
        live = [s["provider"] for s in m.get("served_by") or [] if not s.get("unavailable")]
        out.setdefault(m["tier"], []).append((m.get("order"), m["id"], live))
    return {t: sorted(v) for t, v in out.items()}


def changed(old: dict | None, new: dict) -> tuple[list[str], bool]:
    """(model groups worth a test call, whether Portkey must restart) between two catalogs."""
    if old is None:
        return [f"tier-{t}" for t in new["tiers"]] + (["tier-auto"] if new.get("auto") else []), True
    a, b = tier_shape(old), tier_shape(new)
    groups = [f"tier-{t}" for t in new["tiers"] if a.get(t) != b.get(t)]
    if old.get("auto") != new.get("auto") and new.get("auto"):
        groups.append("tier-auto")
    portkey = ((old.get("policy") or {}).get("portkey") != (new.get("policy") or {}).get("portkey")
               or old.get("gateway") != new.get("gateway") or old.get("providers") != new.get("providers"))
    return groups, portkey


def head_catalog() -> dict | None:
    r = subprocess.run(["git", "-C", str(REPO), "show", "HEAD:integrations/portkey/catalog.json"],
                       check=False, capture_output=True, text=True)
    return json.loads(r.stdout) if r.returncode == 0 else None


def step(title: str, cmd: list[str]) -> bool:
    print(f"→ {title}")
    r = subprocess.run(cmd, check=False, capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    if out:
        print("\n".join("    " + l for l in out.splitlines()[-12:]))
    if r.returncode != 0:
        print(f"✗ {title} failed (exit {r.returncode})")
    return r.returncode == 0


def restart(job: str) -> bool:
    return step(f"restart {job}", ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{job}"])


def wait_healthy(base: str, seconds: int = 120) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base}/health/liveliness", timeout=3) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(1)
    return False


def test_call(base: str, key: str, group: str) -> str:
    body = json.dumps({"model": group, "messages": [{"role": "user", "content": "Reply with the word ok."}],
                       "max_tokens": 20}).encode()
    req = urllib.request.Request(f"{base}/v1/chat/completions", data=body, method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json",
        "x-litellm-session-id": f"gateway-apply-{int(time.time())}-{group}"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            served = r.headers.get("x-litellm-model-id") or "?"
            return f"✓ {group}: answered by {served} in {time.time() - t0:.1f}s"
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:200]
        return f"✗ {group}: HTTP {e.code} after {time.time() - t0:.1f}s: {detail}"
    except (urllib.error.URLError, OSError) as e:
        return f"✗ {group}: {e}"


def apply(argv: list[str]) -> int:
    test = "--no-test" not in argv
    unknown = [a for a in argv if a != "--no-test"]
    if unknown:
        print(f"gateway apply: unknown argument {unknown[0]!r}", file=sys.stderr)
        return 2
    py = sys.executable
    if not (step("build.py", [py, "-I", str(HERE / "build.py")])
            and step("build.py --check", [py, "-I", str(HERE / "build.py"), "--check"])):
        return 1
    new = json.loads(CATALOG.read_text())
    groups, portkey = changed(head_catalog(), new)
    if portkey and not restart(PORTKEY_JOB):
        return 1
    if not restart(LITELLM_JOB):
        return 1
    base = f"http://{new['litellm']['host']}:{new['litellm']['port']}"
    print("→ waiting for LiteLLM health")
    if not wait_healthy(base):
        print("✗ LiteLLM did not report healthy within 120 s; see ~/state/team/litellm/proxy.log")
        return 1
    ok = step("LiteLLM status", ["sh", str(REPO / "litellm" / "team" / "status.sh")])
    ok = step("Portkey status", ["sh", str(REPO / "integrations" / "portkey" / "status.sh")]) and ok
    if test:
        if not groups:
            print("→ no tier changed against HEAD; no test calls")
        key = build.read_env(build.state_dir() / "litellm" / ".env").get("LITELLM_MASTER_KEY", "")
        for g in groups:
            line = test_call(base, key, g)
            print(line)
            ok = ok and line.startswith("✓")
    print("next: integrations/bin/gateway show, integrations/bin/check, then commit with the evidence"
          if ok else "✗ something above failed; nothing was committed")
    return 0 if ok else 1


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "show":
        return show(rest)
    if cmd == "apply":
        return apply(rest)
    print(f"gateway: unknown command {cmd!r}; use show or apply", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
