"""The [team] patch to Hermes kanban: a task's ``toolsets`` narrow its worker's profile.

The lead picks the toolsets a free worker needs for one part; Hermes must give
the worker exactly those (plus ``kanban``) and refuse any the profile lacks.
"""

import json
from contextlib import closing
from types import SimpleNamespace

import pytest


def _claimed(tmp_path, monkeypatch, **task):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect

    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_DELEGATED_CHILD", raising=False)
    monkeypatch.setattr(
        "hermes_cli.profiles.resolve_profile_env",
        lambda name: str(tmp_path) if name == "default" else str(tmp_path / name),
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    kb.create_board("owned")
    with closing(connect(board="owned")) as conn:
        task_id = kb.create_task(
            conn,
            title="Part",
            body="Work order",
            assignee="default",
            workspace_kind="dir",
            workspace_path=str(workspace),
            **task,
        )
        kb.recompute_ready(conn)
        claimed = kb.claim_task(conn, task_id)
    return claimed


def _policy(tmp_path, task):
    from gateway.session_contract import Principal
    from gateway.session_kanban import build_kanban_policy

    actor = Principal("owner", str(tmp_path), frozenset({"session:create"}), "transport")
    connection = SimpleNamespace(
        authority=SimpleNamespace(profile_id=str(tmp_path)),
        actor=actor,
        native_owner=True,
    )
    params = dict(
        board="owned",
        task_id=task.id,
        run_id=task.current_run_id,
        claim_lock=task.claim_lock,
    )
    config = {
        "model": {"default": "m", "provider": "custom"},
        "platform_toolsets": {"cli": ["terminal", "file", "todo"]},
    }
    return build_kanban_policy(connection, params, config)[0]


def test_task_toolsets_narrow_the_profile(tmp_path, monkeypatch):
    task = _claimed(tmp_path, monkeypatch, toolsets=["file", "file", " todo "])
    assert task.toolsets == ["file", "todo"]
    policy = _policy(tmp_path, task)
    assert set(policy.toolsets) == {"file", "todo", "kanban"}
    assert json.loads(policy.request_json)["toolsets"] == ["file", "todo"]


def test_no_task_toolsets_keeps_the_profile(tmp_path, monkeypatch):
    task = _claimed(tmp_path, monkeypatch)
    assert task.toolsets is None
    assert {"terminal", "file", "todo", "kanban"} <= set(_policy(tmp_path, task).toolsets)


def test_a_toolset_the_profile_lacks_is_refused(tmp_path, monkeypatch):
    from hermes_state_runtime import RuntimeStoreError

    task = _claimed(tmp_path, monkeypatch, toolsets=["web"])
    with pytest.raises(RuntimeStoreError):
        _policy(tmp_path, task)


def test_unknown_toolset_name_is_refused_at_create(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="unknown toolset"):
        _claimed(tmp_path, monkeypatch, toolsets=["no-such-toolset"])
