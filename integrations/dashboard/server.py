#!/usr/bin/env python3
"""Team's gateway dashboard: edit catalog.json and the keys, rebuild, restart, test, watch.

    server.py [--port N]      serve index.html and /api on 127.0.0.1 (default 8791)

Everything it changes goes through the same gates as a hand edit: a catalog
edit is rendered by integrations/portkey/build.py first and refused with
build.py's own message; only then is catalog.json written and the outputs
rebuilt. Keys are written to Portkey's .env (provider keys) or LiteLLM's .env
(the master key only) and never sent back to the page: the API shows a mask.

Portkey re-reads its generated configs when their mtime changes, so a catalog
or key change reaches it without a restart. LiteLLM reads config.yaml at start,
so a save restarts LiteLLM when config.yaml changed.

Loopback only. Requests must carry the Host this server listens on (no DNS
rebinding) and the per-start token the page was served with (no cross-site
requests). Standard library only; runs on the system python3 like build.py.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import secrets
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
CATALOG = REPO / "integrations" / "portkey" / "catalog.json"
BUILD = REPO / "integrations" / "portkey" / "build.py"
DEFAULT_PORT = 8791
ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
MASTER = "LITELLM_MASTER_KEY"
SERVICES = {
    "portkey": {"label": "com.fullmoney.team-portkey", "log": "portkey/gateway.log"},
    "litellm": {"label": "com.fullmoney.team-litellm", "log": "litellm/proxy.log"},
}
PROBE_PROMPT = "Reply with the single word pong"
STATUS_MEANING = {
    401: "key rejected", 402: "payment or trial exhausted", 403: "key rejected or model not allowed",
    404: "model not served", 408: "timed out", 429: "rate limited", 500: "provider error",
    502: "provider error", 503: "provider unavailable", 504: "provider timed out", 529: "provider overloaded",
}


def _load_build():
    spec = importlib.util.spec_from_file_location("portkey_build", BUILD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


build = _load_build()
_spend_spec = importlib.util.spec_from_file_location("team_spend", REPO / "integrations" / "portkey" / "spend.py")
spend = importlib.util.module_from_spec(_spend_spec)
_spend_spec.loader.exec_module(spend)
ledger = spend.Ledger()
lock = threading.Lock()


class Problem(Exception):
    """A request the dashboard refuses; the message goes back to the page."""

    def __init__(self, msg: str, status: int = 400) -> None:
        super().__init__(msg)
        self.status = status


def state_dir() -> Path:
    return Path(os.environ.get("TEAM_STATE") or Path.home() / "state" / "team")


def env_path(which: str) -> Path:
    if which not in ("portkey", "litellm"):
        raise Problem(f"unknown key file {which!r}")
    return state_dir() / which / ".env"


# ---------- keys ----------

def read_env(path: Path) -> dict[str, str]:
    """Same parsing as build.read_env, but a missing file is empty rather than a refusal."""
    return build.read_env(path) if path.exists() else {}


def mask(value: str) -> str:
    if not value:
        return ""
    if len(value) < 16:
        return "•" * 4 + value[-2:]
    return f"{value[:4]}…{value[-4:]}"


def set_env(path: Path, name: str, value: str) -> None:
    """Replace, add or (empty value) remove one KEY=value line; keep every other line; mode 600."""
    if not ENV_NAME.match(name):
        raise Problem(f"{name!r} is not a variable name (A-Z, 0-9, _)")
    value = value.strip()
    if any(c in value for c in "\r\n") or (value and re.search(r"\s", value)):
        raise Problem("a key cannot contain whitespace or newlines")
    lines = path.read_text().splitlines() if path.exists() else []
    out, done = [], False
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s and s.split("=", 1)[0].strip() == name:
            if value and not done:
                out.append(f"{name}={value}")
            done = True
            continue
        out.append(line)
    if value and not done:
        out.append(f"{name}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        f.write("\n".join(out) + ("\n" if out else ""))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def secrets_known() -> list[str]:
    vals = list(read_env(env_path("portkey")).values()) + list(read_env(env_path("litellm")).values())
    return sorted({v for v in vals if len(v) >= 8}, key=len, reverse=True)


def redact(text: str, known: list[str] | None = None) -> str:
    for v in known if known is not None else secrets_known():
        text = text.replace(v, mask(v))
    return text


# ---------- the catalog ----------

def catalog_text() -> str:
    return CATALOG.read_text()


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def dump_catalog(catalog: dict) -> str:
    return json.dumps(catalog, indent=1, ensure_ascii=False) + "\n"


def render(catalog: dict) -> dict[str, str]:
    """build.render, with every failure turned into the refusal text the page shows."""
    if not isinstance(catalog, dict):
        raise Problem("the catalog must be a JSON object")
    try:
        return build.render(catalog, read_env(env_path("portkey")))
    except build.Refusal as e:
        raise Problem(e.msg) from None
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise Problem(f"build.py could not read the catalog: {type(e).__name__}: {e}") from None


def changes(files: dict[str, str]) -> dict[str, list[str]]:
    state = state_dir()
    on_disk = build.generated_on_disk(state) | {"litellm/config.yaml", "litellm/allowances.json"}
    added, changed = [], []
    for rel, text in files.items():
        p = state / rel
        if not p.exists():
            added.append(rel)
        elif p.read_text() != text:
            changed.append(rel)
    removed = sorted(r for r in on_disk - set(files) if (state / r).exists())
    return {"added": sorted(added), "changed": sorted(changed), "removed": removed}


def summary(catalog: dict, files: dict[str, str]) -> dict:
    models = build.routed(catalog)
    return {
        "files": len(files),
        "routed": {t: [m["id"] for m in build.tier_models(models, t)] for t in catalog["tiers"]},
        "probes": sorted(p.split("team-probe-", 1)[1][:-5] for p in files if "/team-probe-" in p),
        "changes": changes(files),
    }


# ---------- services ----------

def launchd(label: str) -> dict:
    r = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{label}"], check=False, capture_output=True, text=True)
    if r.returncode != 0:
        return {"loaded": False}
    out = {"loaded": True}
    for key, pat in (("state", r"^\tstate = (.+)$"), ("pid", r"^\tpid = (\d+)$"),
                     ("last_exit", r"^\tlast exit code = (.+)$"), ("runs", r"^\truns = (\d+)$")):
        m = re.search(pat, r.stdout, re.MULTILINE)
        if m:
            out[key] = m.group(1)
    return out


def listening(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def http_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            return 200 <= r.status < 300
    except (urllib.error.URLError, OSError):
        return False


def ports(catalog: dict) -> dict:
    return {
        "portkey": (catalog["gateway"].get("host", "127.0.0.1"), int(catalog["gateway"]["port"])),
        "litellm": (catalog["litellm"].get("host", "127.0.0.1"), int(catalog["litellm"]["port"])),
    }


def health_url(name: str, host: str, port: int) -> str:
    return f"http://{host}:{port}/" if name == "portkey" else f"http://{host}:{port}/health/liveliness"


def service_status(catalog: dict) -> dict:
    out = {}
    for name, (host, port) in ports(catalog).items():
        out[name] = {
            "label": SERVICES[name]["label"],
            "host": host,
            "port": port,
            "launchd": launchd(SERVICES[name]["label"]),
            "listening": listening(host, port),
            "healthy": http_ok(health_url(name, host, port)),
        }
    return out


def restart(name: str, catalog: dict, wait_s: int = 90) -> dict:
    if name not in SERVICES:
        raise Problem(f"unknown service {name!r}")
    label = SERVICES[name]["label"]
    r = subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"], check=False, capture_output=True, text=True)
    if r.returncode != 0:
        raise Problem(f"launchctl kickstart {label} failed: {r.stderr.strip() or r.stdout.strip()}", 500)
    host, port = ports(catalog)[name]
    start = time.time()
    time.sleep(1)
    while time.time() - start < wait_s:
        if http_ok(health_url(name, host, port)):
            return {"service": name, "healthy": True, "seconds": round(time.time() - start, 1)}
        time.sleep(1)
    return {"service": name, "healthy": False, "seconds": wait_s,
            "log": tail_log(name, 30)}


def tail_text(path: Path, max_bytes: int = 256 * 1024) -> str:
    if not path.exists():
        return ""
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        data = f.read().decode("utf-8", "replace")
    return data if size <= max_bytes else data.split("\n", 1)[-1]


def tail_log(name: str, n: int = 200) -> str:
    lines = tail_text(state_dir() / SERVICES[name]["log"]).splitlines()[-n:]
    return redact("\n".join(lines))


# ---------- the linkage report ----------

def profile_master_keys() -> dict[str, str | None]:
    root = state_dir() / "hermes" / "profiles"
    out = {}
    if root.exists():
        for d in sorted(p for p in root.iterdir() if p.is_dir()):
            out[d.name] = read_env(d / ".env").get(MASTER)
    return out


def linkage(catalog: dict) -> dict:
    pk_env = read_env(env_path("portkey"))
    ll_env = read_env(env_path("litellm"))
    warnings = []
    try:
        models = build.routed(catalog)
    except build.Refusal as e:
        models = []
        warnings.append(f"catalog refused by build.py: {e.msg}")
    providers = []
    used_envs = set()
    for name, p in catalog.get("providers", {}).items():
        env = p.get("key_env", "")
        used_envs.add(env)
        key = pk_env.get(env, "")
        routed = [m["id"] for m in models if any(s["provider"] == name for s in m["live"])]
        first = [m["id"] for m in models if m["live"] and m["live"][0]["provider"] == name]
        listed = [m["id"] for m in catalog.get("models", []) if any(s.get("provider") == name for s in m.get("served_by", []))]
        providers.append({
            "name": name, "portkey": p.get("portkey"), "key_env": env, "key_set": bool(key), "key_mask": mask(key),
            "custom_host": p.get("custom_host"), "request_timeout_ms": p.get("request_timeout_ms"),
            "routed_models": routed, "first_choice_for": first, "listed_models": listed,
            "probe": f"team-probe-{name}" if routed else None,
            "free_basis": p.get("free_basis"), "limits": p.get("limits"), "notes": p.get("notes"),
            "allowance": p.get("allowance"),
        })
        if not key:
            warnings.append(f"{name}: {env} is not set in Portkey's .env" + (" — build.py will refuse" if routed else ""))
        if not routed:
            warnings.append(f"{name}: no routed model uses this provider, so it is never called")
    stray = sorted(k for k in pk_env if k not in used_envs)
    for k in stray:
        warnings.append(f"Portkey's .env holds {k}, which no provider in the catalog uses")
    if MASTER in pk_env:
        warnings.append(f"Portkey's .env holds {MASTER}; it belongs only in LiteLLM's .env")
    extra_ll = sorted(k for k in ll_env if k != MASTER)
    for k in extra_ll:
        warnings.append(f"LiteLLM's .env holds {k}; LiteLLM must hold no provider key (run.sh unsets it anyway)")
    master = ll_env.get(MASTER, "")
    if not master:
        warnings.append(f"{MASTER} is not set in LiteLLM's .env: every caller will be refused")
    profiles = {}
    for prof, val in profile_master_keys().items():
        state = "missing" if not val else ("match" if val == master else "differs")
        profiles[prof] = state
        if state != "match":
            warnings.append(f"Hermes profile {prof}: {MASTER} {state} from LiteLLM's (run `team roles sync`)")
    for b in catalog.get("blocked_providers", {}):
        if b in catalog.get("providers", {}):
            warnings.append(f"{b} is both a provider and blocked")
    return {
        "providers": providers,
        "blocked": catalog.get("blocked_providers", {}),
        "stray_keys": [{"name": k, "mask": mask(pk_env[k])} for k in stray],
        "litellm_keys": [{"name": k, "mask": mask(v)} for k, v in ll_env.items()],
        "master": {"set": bool(master), "mask": mask(master), "profiles": profiles},
        "warnings": warnings,
    }


def drift() -> dict:
    try:
        files = build.render(json.loads(catalog_text()), read_env(env_path("portkey")))
    except build.Refusal as e:
        return {"ok": False, "refused": e.msg, "problems": []}
    problems = build.drift(state_dir(), files)
    return {"ok": not problems, "problems": problems, "files": len(files)}


def overview() -> dict:
    text = catalog_text()
    catalog = json.loads(text)
    return {
        "catalog": catalog,
        "base": digest(text),
        "linkage": linkage(catalog),
        "services": service_status(catalog),
        "drift": drift(),
        "spend": spend_status(),
        "paths": {
            "catalog": str(CATALOG),
            "portkey_env": str(env_path("portkey")),
            "litellm_env": str(env_path("litellm")),
            "state": str(state_dir()),
        },
    }


def spend_status() -> dict:
    ledger.state = state_dir()
    return ledger.status()


# ---------- saving ----------

def save_catalog(catalog: dict, base: str, restart_litellm: bool = True) -> dict:
    with lock:
        if digest(catalog_text()) != base:
            raise Problem("catalog.json changed on disk since this page loaded it; reload, then redo the edit", 409)
        files = render(catalog)
        info = summary(catalog, files)
        text = dump_catalog(catalog)
        CATALOG.write_text(text)
        build.write(state_dir(), files)
        result = {"saved": True, "base": digest(text), **info, "restarted": []}
        if restart_litellm and "litellm/config.yaml" in info["changes"]["changed"]:
            result["restarted"].append(restart("litellm", catalog))
        return result


def save_key(which: str, name: str, value: str) -> dict:
    with lock:
        if which == "litellm" and name != MASTER:
            raise Problem(f"LiteLLM's .env holds only {MASTER}; provider keys go in Portkey's .env")
        if which == "portkey" and name == MASTER:
            raise Problem(f"{MASTER} belongs in LiteLLM's .env")
        catalog = json.loads(catalog_text())
        set_env(env_path(which), name, value)
        result = {"saved": True, "file": which, "name": name, "mask": mask(value.strip()), "restarted": []}
        try:
            files = build.render(catalog, read_env(env_path("portkey")))
            result["changes"] = changes(files)
            build.write(state_dir(), files)
            result["rebuilt"] = True
        except build.Refusal as e:
            result["rebuilt"] = False
            result["refused"] = e.msg
        if which == "litellm":
            result["restarted"].append(restart("litellm", catalog))
        return result


# ---------- testing ----------

def requests_for(trace: str, wait_s: float = 2.0) -> list[dict]:
    path = state_dir() / "portkey" / "requests.jsonl"
    end = time.time() + wait_s
    while True:
        rows = [r for r in parse_jsonl(tail_text(path)) if r.get("trace_id") == trace]
        if rows or time.time() > end:
            return rows
        time.sleep(0.25)


def parse_jsonl(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def send_test(via: str, model: str, prompt: str, max_tokens: int = 256) -> dict:
    catalog = json.loads(catalog_text())
    if not re.match(r"^[A-Za-z0-9._:/-]+$", model or ""):
        raise Problem("model must be a tier, a catalog model id, or a config name")
    trace = "dash-" + secrets.token_hex(6)
    body = {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": int(max_tokens)}
    host, port = ports(catalog)[via] if via in ("portkey", "litellm") else (None, None)
    if via == "portkey":
        for name, s in spend_status().items():
            if s["over"] and model in s["configs"]:
                raise Problem(f"{name} is at its allowance stop (${s['spent_usd']:.2f} of ${s['stop_at_usd']} "
                              f"since {s['since']}); raise stop_at_usd or wait for the next cycle")
        headers = {"x-portkey-config": model, "x-portkey-trace-id": trace}
    elif via == "litellm":
        key = read_env(env_path("litellm")).get(MASTER, "")
        headers = {"authorization": f"Bearer {key}", "x-litellm-session-id": trace}
    else:
        raise Problem("via must be portkey or litellm")
    req = urllib.request.Request(
        f"http://{host}:{port}/v1/chat/completions", data=json.dumps(body).encode(), method="POST",
        headers={"content-type": "application/json", **headers},
    )
    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            status, raw = r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as e:
        return {"trace": trace, "status": None, "error": f"{via} on {host}:{port} did not answer: {e}",
                "seconds": round(time.time() - start, 2), "attempts": []}
    out = {"trace": trace, "via": via, "model": model, "status": status, "seconds": round(time.time() - start, 2)}
    try:
        d = json.loads(raw)
    except ValueError:
        d = None
    if status == 200 and isinstance(d, dict) and d.get("choices"):
        ch = d["choices"][0]
        msg = ch.get("message") or {}
        out.update({"answer": msg.get("content"), "reasoning": bool(msg.get("reasoning_content")),
                    "finish": ch.get("finish_reason"), "answered_by": d.get("model"), "usage": d.get("usage")})
    else:
        err = d.get("error", d.get("message")) if isinstance(d, dict) else raw
        out["error"] = redact(err if isinstance(err, str) else json.dumps(err))[:2000]
        out["meaning"] = STATUS_MEANING.get(status)
    out["requests"] = [summarize_request(r) for r in requests_for(trace)]
    return out


def summarize_request(r: dict) -> dict:
    return {k: r.get(k) for k in ("time", "config", "status", "latency_ms", "provider", "provider_model",
                                  "model_answered", "prompt_tokens", "completion_tokens", "attempts", "error")}


def probe(provider: str) -> dict:
    catalog = json.loads(catalog_text())
    if provider not in catalog.get("providers", {}):
        raise Problem(f"unknown provider {provider!r}")
    name = f"team-probe-{provider}"
    if not (state_dir() / "portkey" / "configs" / f"{name}.json").exists():
        raise Problem(f"no probe config for {provider}: no routed model uses it, or the catalog has not been built")
    r = send_test("portkey", name, PROBE_PROMPT, 256)
    r["provider"] = provider
    r["ok"] = r.get("status") == 200
    return r


# ---------- verify: every model on every provider ----------

SLOW_S = 30
RETRY_WAIT_S = 15
verify_state: dict = {"running": False}
verify_lock = threading.Lock()


def verify_path() -> Path:
    return state_dir() / "dashboard" / "verify.json"


def last_verify() -> dict:
    p = verify_path()
    return json.loads(p.read_text()) if p.exists() else {}


def verify_rows(catalog: dict, include_unavailable: bool) -> list[dict]:
    keys = read_env(env_path("portkey"))
    rows = []
    for m in catalog.get("models", []):
        if not include_unavailable and not m.get("tier"):
            continue
        for s in m.get("served_by", []):
            if s.get("unavailable") and not include_unavailable:
                continue
            p = catalog["providers"].get(s["provider"], {})
            rows.append({"model": m["id"], "tier": m.get("tier"), "provider": s["provider"],
                         "provider_model_id": s.get("provider_model_id"), "unavailable": s.get("unavailable"),
                         "config": build.pair_name(m["id"], s["provider"]),
                         "key_set": bool(keys.get(p.get("key_env", "")))})
    return rows


def verify_one(row: dict) -> dict:
    if not row["key_set"]:
        return {**row, "ok": False, "status": None, "error": "no key set for this provider", "tries": 0,
                "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    first = None
    for attempt in (1, 2):
        r = send_test("portkey", row["config"], PROBE_PROMPT, 1024)
        ok = r.get("status") == 200 and bool(r.get("answer") or r.get("reasoning"))
        if ok or attempt == 2:
            break
        first = {"status": r.get("status"), "meaning": r.get("meaning")}
        time.sleep(RETRY_WAIT_S)
    return {**row, "ok": ok, "status": r.get("status"), "seconds": r.get("seconds"), "slow": (r.get("seconds") or 0) > SLOW_S,
            "answered_by": r.get("answered_by"), "meaning": r.get("meaning"), "error": r.get("error"),
            "tries": attempt, "first": first, "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def start_verify(include_unavailable: bool) -> dict:
    with verify_lock:
        if verify_state.get("running"):
            raise Problem("a verify run is already going", 409)
        catalog = json.loads(catalog_text())
        rows = verify_rows(catalog, include_unavailable)
        verify_state.clear()
        verify_state.update({"running": True, "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                             "scope": "everything" if include_unavailable else "routed", "total": len(rows), "results": []})
    by_provider: dict[str, list[dict]] = {}
    for row in rows:
        by_provider.setdefault(row["provider"], []).append(row)

    def worker(items: list[dict]) -> None:
        for row in items:
            res = verify_one(row)
            with verify_lock:
                verify_state["results"].append(res)

    def run() -> None:
        threads = [threading.Thread(target=worker, args=(items,), daemon=True) for items in by_provider.values()]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        with verify_lock:
            verify_state["running"] = False
            verify_state["finished"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            done = dict(verify_state)
        p = verify_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(done, indent=1))

    threading.Thread(target=run, daemon=True).start()
    return verify_status()


def verify_status() -> dict:
    with verify_lock:
        if verify_state.get("started"):
            return {**verify_state, "results": list(verify_state["results"])}
    return {"running": False, **last_verify()}


# ---------- logs ----------

def recent(n: int = 100) -> dict:
    known = secrets_known()
    reqs = parse_jsonl(tail_text(state_dir() / "portkey" / "requests.jsonl"))[-n:]
    rows = []
    db = state_dir() / "litellm" / "usage.db"
    if db.exists():
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        conn.row_factory = sqlite3.Row
        try:
            rows = [dict(r) for r in conn.execute("SELECT * FROM calls ORDER BY id DESC LIMIT ?", (n,))]
        except sqlite3.Error:
            rows = []
        finally:
            conn.close()
    for r in reqs:
        if r.get("error"):
            r["error"] = redact(str(r["error"]), known)
    for r in rows:
        if r.get("error"):
            r["error"] = redact(str(r["error"]), known)
    return {"portkey": list(reversed(reqs)), "litellm": rows}


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "team-dashboard"
    token = ""
    port = DEFAULT_PORT

    def log_message(self, fmt, *args):  # one line per request on stderr, no bodies
        sys.stderr.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {fmt % args}\n")

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        self.send_header("cache-control", "no-store")
        self.send_header("x-content-type-options", "nosniff")
        self.send_header("x-frame-options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, obj) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False).encode(), "application/json")

    def _allowed(self) -> bool:
        hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        if self.headers.get("host") not in hosts:
            self._json(403, {"error": "wrong Host header"})
            return False
        origin = self.headers.get("origin")
        if origin and origin not in {f"http://{h}" for h in hosts}:
            self._json(403, {"error": "cross-origin request refused"})
            return False
        if self.path.startswith("/api/") and not secrets.compare_digest(self.headers.get("x-team-token", ""), self.token):
            self._json(403, {"error": "missing or stale token; reload the page"})
            return False
        return True

    def _body(self) -> dict:
        n = int(self.headers.get("content-length") or 0)
        if n > 2 * 1024 * 1024:
            raise Problem("request too large")
        try:
            d = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            raise Problem("body is not JSON") from None
        if not isinstance(d, dict):
            raise Problem("body must be a JSON object")
        return d

    def do_GET(self):
        if not self._allowed():
            return
        path = self.path.split("?", 1)[0]
        query = dict(p.split("=", 1) for p in self.path.split("?", 1)[1].split("&") if "=" in p) if "?" in self.path else {}
        try:
            if path in ("/", "/index.html"):
                html = (HERE / "index.html").read_text().replace("__TEAM_TOKEN__", self.token)
                return self._send(200, html.encode(), "text/html; charset=utf-8")
            if path == "/api/overview":
                return self._json(200, overview())
            if path == "/api/verify":
                return self._json(200, verify_status())
            if path == "/api/recent":
                return self._json(200, recent(min(int(query.get("n", 100)), 1000)))
            if path == "/api/log":
                name = query.get("service", "")
                if name not in SERVICES:
                    raise Problem("service must be portkey or litellm")
                return self._json(200, {"service": name, "text": tail_log(name, min(int(query.get("n", 200)), 2000))})
            self._json(404, {"error": "not found"})
        except Problem as e:
            self._json(e.status, {"error": str(e)})

    def do_POST(self):
        if not self._allowed():
            return
        try:
            d = self._body()
            if self.path == "/api/check":
                files = render(d.get("catalog"))
                return self._json(200, {"ok": True, **summary(d["catalog"], files)})
            if self.path == "/api/save":
                return self._json(200, save_catalog(d.get("catalog"), str(d.get("base", "")), bool(d.get("restart", True))))
            if self.path == "/api/key":
                return self._json(200, save_key(str(d.get("file", "")), str(d.get("name", "")), str(d.get("value", ""))))
            if self.path == "/api/restart":
                return self._json(200, restart(str(d.get("service", "")), json.loads(catalog_text())))
            if self.path == "/api/test":
                return self._json(200, send_test(str(d.get("via", "")), str(d.get("model", "")),
                                                 str(d.get("prompt") or PROBE_PROMPT), int(d.get("max_tokens") or 256)))
            if self.path == "/api/verify":
                return self._json(200, start_verify(bool(d.get("include_unavailable"))))
            if self.path == "/api/probe":
                return self._json(200, probe(str(d.get("provider", ""))))
            self._json(404, {"error": "not found"})
        except Problem as e:
            self._json(e.status, {"error": str(e)})


def main(argv: list[str]) -> int:
    port = DEFAULT_PORT
    for a in argv:
        if a.startswith("--port="):
            port = int(a.split("=", 1)[1])
    Handler.token = secrets.token_urlsafe(24)
    Handler.port = port
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"team dashboard on http://127.0.0.1:{port}/", flush=True)
    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
