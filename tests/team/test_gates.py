"""Pipelines, gates and risk: the table that decides what a goal goes through and how it lands."""

from __future__ import annotations

import pytest

from team import gates, roles


def test_every_pipeline_stage_names_a_registered_gate_and_a_landing_policy():
    for kind, stages in gates.PIPELINES.items():
        assert kind in gates.LANDING
        assert gates.LANDING[kind] in ("auto", "ask")
        for s in stages:
            assert s.gate in gates.GATES, (kind, s)
        assert any(s.role for s in stages), kind
    assert gates.LANDING["bugfix"] == "auto" and gates.LANDING["security"] == "ask"


def test_inactive_roles_drop_their_stages_but_a_builder_must_remain():
    only_implement = gates.stages_for("bugfix", {"implement"})
    assert [s.name for s in only_implement] == ["implement", "test_verify"]
    full = gates.stages_for("bugfix", {"implement", "test-write", "review"})
    assert [s.name for s in full] == ["test_write", "implement", "test_verify", "review"]
    assert [s.name for s in gates.stages_for("security", {"implement", "security"})] == [
        "implement", "test_verify", "security",
    ]
    with pytest.raises(gates.GateError):
        gates.stages_for("bugfix", {"review"})  # nobody to build
    with pytest.raises(gates.GateError):
        gates.stages_for("refactor", {"implement"})


def test_risk_only_goes_up():
    auto, why = gates.risk_of("bugfix", ["team/run.py"], 40, max_lines=800)
    assert auto == "auto" and "40 lines" in why
    ask, why = gates.risk_of("bugfix", ["team/run.py", "package-lock.json"], 40, max_lines=800)
    assert ask == "ask" and "package-lock.json" in why
    ask, why = gates.risk_of("bugfix", ["lib/auth.ts"], 3, max_lines=800)
    assert ask == "ask" and "auth" in why
    ask, why = gates.risk_of("bugfix", ["team/run.py"], 801, max_lines=800)
    assert ask == "ask" and "801 lines" in why
    ask, why = gates.risk_of("bugfix", ["docs/x.md"], 1, extra_paths=["docs/*"], max_lines=800)
    assert ask == "ask" and "docs/x.md" in why
    ask, why = gates.risk_of("dependencies", ["README.md"], 1, max_lines=800)
    assert ask == "ask" and "always waits" in why
    # an ordinary word that happens to contain 'auth' is not a risk
    assert gates.risky_paths(["team/author.py", "src/authors/list.ts"]) == []
    assert gates.risky_paths(["prisma/migrations/0001_init/migration.sql", ".github/workflows/ci.yml", ".env.local"]) == [
        "prisma/migrations/0001_init/migration.sql", ".github/workflows/ci.yml", ".env.local",
    ]


def test_test_paths():
    yes = ["tests/team/test_run.py", "src/__tests__/app.test.ts", "lib/x.spec.js", "pkg/thing_test.go", "e2e/login.ts"]
    no = ["team/run.py", "src/app.ts", "tests_helper.py", "contest/x.py"]
    assert all(gates.is_test_path(p) for p in yes), [p for p in yes if not gates.is_test_path(p)]
    assert not any(gates.is_test_path(p) for p in no), [p for p in no if gates.is_test_path(p)]


def test_a_role_may_only_name_a_registered_gate(tmp_path):
    d = tmp_path / "team" / "roles" / "r"
    d.mkdir(parents=True)
    def toml(gate_line=""):
        return (
            'name = "r"\ntitle = "R"\ndoes = "x"\ngraded_by = "y"\nstatus = "active"\n'
            f'tools = ["file"]\n{gate_line}[runs_on]\nharness = "hermes"\n[models]\ncandidates = ["tier-simple"]\n'
        )

    (d / "role.toml").write_text(toml())
    assert roles.load(tmp_path, "r")["gate"] == "check"
    (d / "role.toml").write_text(toml('gate = "tests_only"\n'))
    assert roles.load(tmp_path, "r")["gate"] == "tests_only"
    (d / "role.toml").write_text(toml('gate = "vibes"\n'))
    with pytest.raises(roles.RoleError, match="gate must be one of"):
        roles.load(tmp_path, "r")
