import json
import subprocess

import pytest

from team import kanban
from team.catalog import ModelChoice


class FakeRunner:
    """Records every argv and answers from a queue of (returncode, stdout, stderr)."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        rc, out, err = self.answers.pop(0) if self.answers else (0, "", "")
        return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr=err)


CHATTER = "\x1b[0;32m✓\x1b[0m Tools + dependencies installed\n"


def test_every_call_goes_through_the_wrapper_with_the_board():
    r = FakeRunner((0, CHATTER + json.dumps({"id": "t_1", "status": "ready"}), ""))
    task = kanban.create_task("team", "title", "body", assignee="implement", runner=r)
    assert task == {"id": "t_1", "status": "ready"}
    cmd, kwargs = r.calls[0]
    assert cmd[0] == str(kanban.wrapper()) and cmd[0].endswith("team/bin/team-hermes")
    assert cmd[1:4] == ["kanban", "--board", "team"]
    assert kwargs == {"capture_output": True, "text": True}


def test_kanban_home_under_team_state(team_state):
    assert kanban.kanban_home() == team_state / "kanban"


def test_create_task_flags_and_model_choice():
    r = FakeRunner((0, json.dumps({"id": "t_2", "status": "ready"}), ""))
    choice = ModelChoice("nvidia:z-ai/glm-5.3", "litellm", "z-ai/glm-5.3", "free")
    kanban.create_task(
        "team",
        "do x",
        "the brief",
        assignee="implement",
        model=choice,
        workspace="worktree:/wt/x",
        branch="team/x",
        max_runtime_s=900,
        max_retries=1,
        skills=("commit-before-done",),
        parents=("t_0",),
        runner=r,
    )
    cmd = r.calls[0][0]
    assert cmd[4:] == [
        "create",
        "do x",
        "--body",
        "the brief",
        "--assignee",
        "implement",
        "--json",
        "--parent",
        "t_0",
        "--workspace",
        "worktree:/wt/x",
        "--branch",
        "team/x",
        "--max-runtime",
        "900s",
        "--max-retries",
        "1",
        "--skill",
        "commit-before-done",
        "--provider",
        "litellm",
        "--model",
        "z-ai/glm-5.3",
    ]


def test_create_task_refuses_free_form_model_and_bad_retries():
    with pytest.raises(TypeError, match="ModelChoice"):
        kanban.create_task(
            "team", "t", "b", assignee="implement", model="gpt-5", runner=FakeRunner()
        )
    with pytest.raises(ValueError, match="at least 1"):
        kanban.create_task(
            "team", "t", "b", assignee="implement", max_retries=0, runner=FakeRunner()
        )


def test_errors_carry_stderr_tail_and_bad_json():
    r = FakeRunner((1, "", "boom: no such board"))
    with pytest.raises(kanban.KanbanError, match="create failed") as e:
        kanban.create_task("team", "t", "b", assignee="implement", runner=r)
    assert e.value.stderr == "boom: no such board"
    with pytest.raises(kanban.KanbanError, match="no JSON"):
        kanban.create_task(
            "team", "t", "b", assignee="implement", runner=FakeRunner((0, CHATTER, ""))
        )
    with pytest.raises(kanban.KanbanError, match="bad JSON"):
        kanban.create_task(
            "team", "t", "b", assignee="implement", runner=FakeRunner((0, "{oops", ""))
        )
    with pytest.raises(kanban.KanbanError, match="no task"):
        kanban.create_task(
            "team", "t", "b", assignee="implement", runner=FakeRunner((0, "[]", ""))
        )


def test_show_full_accepts_both_shapes_and_runs():
    full = {"task": {"id": "t_1", "status": "review"}, "runs": [{"worker_pid": 42}]}
    r = FakeRunner(
        (0, json.dumps(full), ""),
        (0, json.dumps({"id": "t_1"}), ""),
        (0, json.dumps({"runs": [{"worker_pid": 7, "started_at": 1}]}), ""),
    )
    assert kanban.show_full("team", "t_1", runner=r) == full
    assert kanban.show_full("team", "t_1", runner=r) == {
        "task": {"id": "t_1"},
        "runs": [],
    }
    assert kanban.runs("team", "t_1", runner=r) == [{"worker_pid": 7, "started_at": 1}]
    assert r.calls[0][0][4:] == ["show", "t_1", "--json"]
    assert r.calls[2][0][4:] == ["runs", "t_1", "--json"]


def test_boards_list_ensure_and_create():
    r = FakeRunner(
        (0, json.dumps([{"slug": "team"}]), ""),
        (0, json.dumps({"boards": [{"slug": "team"}]}), ""),
        (0, "Board created", ""),
    )
    assert kanban.ensure_board("team", runner=r) is False
    assert kanban.ensure_board("brain", "Brain repo", runner=r) is True
    assert r.calls[0][0][1:] == ["kanban", "boards", "list", "--json"]
    assert r.calls[2][0][1:] == [
        "kanban",
        "boards",
        "create",
        "brain",
        "--description",
        "Brain repo",
    ]


def test_list_dispatch_and_state_changes():
    r = FakeRunner(
        (0, json.dumps([{"id": "t_1", "status": "review"}]), ""),
        (0, json.dumps({"spawned": ["t_2"], "skipped_unassigned": []}), ""),
        (0, "", ""),
        (0, "", ""),
        (0, "", ""),
        (0, "", ""),
        (0, "", ""),
        (0, "", ""),
        (0, "", ""),
        (0, "worker log line\n", ""),
    )
    assert kanban.list_tasks("team", status="review", runner=r) == [
        {"id": "t_1", "status": "review"}
    ]
    assert kanban.dispatch("team", max_spawns=3, runner=r)["spawned"] == ["t_2"]
    kanban.comment("team", "t_1", "check failed:\n...", runner=r)
    kanban.block("team", "t_1", "needs Eli", runner=r)
    kanban.unblock("team", "t_1", "answered", runner=r)
    kanban.reopen_review("team", "t_1", "attempt 2", runner=r)
    kanban.request_changes("team", "t_1", "fix the test", runner=r)
    kanban.complete("team", "t_1", "landed abc1234", runner=r)
    kanban.reclaim("team", "t_1", "dead worker", runner=r)
    assert kanban.log("team", "t_1", tail=4000, runner=r) == "worker log line\n"
    tails = [c[0][4:] for c in r.calls]
    assert tails[0] == ["list", "--json", "--status", "review"]
    assert tails[1] == ["dispatch", "--json", "--max", "3"]
    assert tails[2] == ["comment", "t_1", "check failed:\n...", "--author", "team"]
    assert tails[3] == ["block", "t_1", "needs Eli", "--kind", "needs_input"]
    assert tails[4] == ["unblock", "t_1", "--reason", "answered"]
    assert tails[5] == ["reopen-review", "t_1", "--reason", "attempt 2"]
    assert tails[6] == ["request-changes", "t_1", "fix the test"]
    assert tails[7] == ["complete", "t_1", "--result", "landed abc1234"]
    assert tails[8] == ["reclaim", "t_1", "--reason", "dead worker"]
    assert tails[9] == ["log", "t_1", "--tail", "4000"]
