"""The shared gateway is restarted when it runs older code than the checkout."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from team import gateway
from team.app import chat

HEAD = "005f8a883bee" + "0" * 28
OLD = "2ee78c315965" + "0" * 28


def _home(tmp_path: Path, *, pid: int | None = None, sha: str | None = HEAD, age_s: float = 3600) -> Path:
    home = tmp_path / "hermes"
    home.mkdir()
    lock = home / "gateway.lock"
    lock.write_text(json.dumps({"pid": pid or os.getpid(), "kind": "hermes-gateway"}))
    started = time.time() - age_s
    os.utime(lock, (started, started))
    if sha is not None:
        (home / "gateway_state.json").write_text(json.dumps({"pid": pid or os.getpid(), "code_sha": sha}))
    return home


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "team").mkdir(parents=True)
    (root / "plugins" / "team").mkdir(parents=True)
    old = time.time() - 7200
    for rel in ("team/roles.py", "plugins/team/__init__.py"):
        p = root / rel
        p.write_text("# old\n")
        os.utime(p, (old, old))
    return root


def test_a_gateway_from_another_commit_is_stale(tmp_path):
    root, home = _root(tmp_path), _home(tmp_path, sha=OLD)
    why = gateway.stale(root, home, head=HEAD)
    assert why == f"gateway: started from {OLD[:10]}, checkout is at {HEAD[:10]}"


def test_a_source_edit_after_the_gateway_started_is_stale(tmp_path):
    root, home = _root(tmp_path), _home(tmp_path)
    assert gateway.stale(root, home, head=HEAD) is None
    (root / "team" / "roles.py").write_text("# edited\n")  # now, after the lock's mtime
    assert gateway.stale(root, home, head=HEAD) == "gateway: a Team source file changed after it started"
    # Caches are not source.
    cache = root / "team" / "__pycache__"
    cache.mkdir()
    (cache / "roles.cpython-314.pyc").write_bytes(b"x")
    os.utime(root / "team" / "roles.py", (time.time() - 7200, time.time() - 7200))
    assert gateway.stale(root, home, head=HEAD) is None


def test_no_gateway_or_a_dead_one_is_never_stale(tmp_path):
    root = _root(tmp_path)
    assert gateway.stale(root, tmp_path / "missing", head=HEAD) is None
    home = _home(tmp_path, pid=2**22 - 1, sha=OLD)  # no such process
    assert gateway.stale(root, home, head=HEAD) is None


def test_refresh_restarts_only_a_stale_gateway(tmp_path):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return type("P", (), {"returncode": 0})()

    root = _root(tmp_path)
    assert gateway.refresh(root, _home(tmp_path), run=run, head=HEAD) is None
    assert calls == []
    home = _home(tmp_path / "b", sha=OLD) if (tmp_path / "b").mkdir() is None else None
    assert gateway.refresh(root, home, run=run, head=HEAD).startswith("gateway: started from")
    assert calls == [[str(root / "team" / "bin" / "team-hermes"), "gateway", "restart", "--detach"]]


def test_the_chat_refreshes_the_gateway_before_each_run(tmp_path):
    class Proc:
        stdout = iter([json.dumps({"type": "result", "session_id": "b1", "exit_code": 0, "text": "Hi.",
                                   "tokens": {"input": 1, "output": 1}}) + "\n"])
        stderr = type("E", (), {"read": staticmethod(lambda: "")})()

        def wait(self):
            return 0

    refreshed = []
    c = chat.Chat(
        tmp_path, popen=lambda argv, **kw: Proc(), aide=False,
        refresh=lambda root: refreshed.append(root),
    )
    c.send("hello", wait=True)
    assert refreshed == [tmp_path]
