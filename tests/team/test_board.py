"""The Team tab's board: role columns, the model that answered and its rank, every token."""

import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from team import board, ledger
from tests.team.test_catalog import CATALOG
from tests.team.test_run import git

REPO = Path(__file__).resolve().parents[2]
def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )


def merged_repo(tmp_path, files_by_branch):
    """Create a temporary git repo with merge commits for testing.
    
    Args:
        tmp_path: pytest temporary path
        files_by_branch: dict of {(branch, goal_id): {path: content}}
    
    Returns:
        Path to the repository
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "T")
    git(repo, "config", "user.email", "t@t")
    
    # Create initial base.txt commit
    git(repo, "-c", "core.hooksPath=/dev/null", "commit", "-q", "--allow-empty", "-m", "initial")
    
    # Process each branch and create files, then merge
    for (branch, goal_id), files in files_by_branch.items():
        git(repo, "checkout", "-q", "-b", branch)
        
        # Create files
        for path, content in files.items():
            file_path = Path(repo) / path
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content)
        
        git(repo, "add", "-A")
        git(repo, "-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", "x")
        
        # Merge back to main
        git(repo, "checkout", "-q", "main")
        git(repo, "merge", "--no-ff", "-q", "-m", f"Merge {branch}: do it (goal {goal_id})", branch)
    
    return repo


def test_merge_files_lists_what_the_merge_commit_changed(tmp_path):
    """Test that merge_files returns the correct changed files."""
    files_by_branch = {
        ("team/a", 1): {"a.txt": "content of a.txt"},
    }
    repo = merged_repo(tmp_path, files_by_branch)
    from team import board
    assert board.merge_files(repo, "team/a", 1) == ["a.txt"]


def test_merge_files_lists_every_file_of_the_merge(tmp_path):
    """Test that merge_files returns all changed files in a merge."""
    files_by_branch = {
        ("team/m", 2): {
            "x.txt": "content of x.txt",
            "sub/y.txt": "content of sub/y.txt",
            "z.txt": "content of z.txt",
        }
    }
    repo = merged_repo(tmp_path, files_by_branch)
    from team import board
    result = board.merge_files(repo, "team/m", 2)
    # git prints them sorted by path
    assert result == ["sub/y.txt", "x.txt", "z.txt"]


def test_merge_files_is_empty_when_the_merge_is_not_found(tmp_path):
    """Test that merge_files returns empty list when merge commit is not found."""
    files_by_branch = {
        ("team/a", 1): {"a.txt": "content of a.txt"},
    }
    repo = merged_repo(tmp_path, files_by_branch)
    from team import board
    
    # Wrong branch
    assert board.merge_files(repo, "team/other", 1) == []
    # Wrong goal id
    assert board.merge_files(repo, "team/a", 99) == []
    # Not a repo
    assert board.merge_files(tmp_path / "nowhere", "team/a", 1) == []


def test_a_landed_goal_card_carries_its_files_and_others_do_not(team_state, root, tmp_path):
    """Test that landed goal cards have files field with changed files, others don't."""
    conn = ledger.connect(team_state / "team.db")
    ledger.init(conn)
    
    # Create repo with branch team/a for goal 1
    files_by_branch = {
        ("team/a", 1): {"a.txt": "content of a.txt"},
    }
    repo = merged_repo(tmp_path, files_by_branch)
    
    # Create landed goal with id 1
    g1 = ledger.create_goal(conn, "team", "do it", "implement", "team/a", "/w", "./c")
    ledger.finish_goal(conn, g1, "pass", "landed abc")
    
    # Create unlanded goal
    g2 = ledger.create_goal(conn, "team", "other", "implement", "team/b", "/w", "./c")
    
    # Mock repo_path to return our test repo
    from team import board, targets
    original_repo_path = board.targets.repo_path
    board.targets.repo_path = lambda name: repo
    
    try:
        from tests.team.test_catalog import CATALOG
        b = board.board(conn, root, CATALOG)
        
        # Find the cards
        done_cards = b['done']
        columns = b['columns']
        
        # The landed goal (g1) should be in done
        g1_card = next((c for c in done_cards if c['id'] == g1), None)
        assert g1_card is not None
        assert g1_card['files'] == ["a.txt"]
        
        # The unlanded goal (g2) should be in lead or rescue or implement
        all_cards = done_cards + columns.get('lead', []) + columns.get('implement', []) + columns.get('rescue', [])
        g2_card = next((c for c in all_cards if c['id'] == g2), None)
        assert g2_card is not None
        assert g2_card['files'] == []
    finally:
        # Restore original
        board.targets.repo_path = original_repo_path


def gateway(state, rows):
    (state / "litellm").mkdir()
    gw = sqlite3.connect(state / "litellm" / "usage.db")
    gw.execute(
        'CREATE TABLE calls (id INTEGER PRIMARY KEY, ts TEXT, session TEXT, "group" TEXT, tier TEXT,'
        " deployment TEXT, config TEXT, model TEXT, tokens_in INTEGER, tokens_out INTEGER,"
        " latency_ms INTEGER, ok INTEGER, rate_limited INTEGER DEFAULT 0, error TEXT)"
    )
    gw.executemany(
        'INSERT INTO calls (ts, session, "group", tier, model, tokens_in, tokens_out, ok, rate_limited, error)'
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    gw.commit()
    gw.close()


# Three medium models in LiteLLM's order, one no provider serves, and LiteLLM's cooldown.
CAT = {
    "models": [
        {"id": "m-first", "tier": "medium", "order": 1, "served_by": [{"provider": "p"}]},
        {"id": "m-third", "tier": "medium", "order": 3, "served_by": [{"provider": "p"}]},
        {"id": "m-second", "tier": "medium", "order": 2, "served_by": [{"provider": "p"}]},
        {"id": "m-gone", "tier": "medium", "order": 4, "served_by": [{"provider": "p", "unavailable": "410"}]},
        {"id": "m-free", "served_by": [{"provider": "p"}]},
    ],
    "policy": {"litellm": {"cooldown_time": 300}},
}


@pytest.fixture
def root(tmp_path):
    """The real role files, outside git: tier order is the catalog passed in."""
    r = tmp_path / "root"
    shutil.copytree(REPO / "team" / "roles", r / "team" / "roles")
    return r


def job(conn, goal_id, spec, outcome="pass", attempt=1, started="2026-10-30T10:00:00+00:00"):
    j = ledger.start_job(conn, "team", "abc", "implement", "v1", attempt=attempt,
                         task_ref=f"t_{goal_id}_{attempt}",
                         model_choice={"spec": spec, "model": spec, "why": "test"})
    conn.execute("UPDATE jobs SET started=? WHERE id=?", (started, j))
    ledger.finish_job(conn, j, outcome, "check", "1 passed")
    conn.execute("UPDATE jobs SET ended=? WHERE id=?", ("2026-10-30T10:10:00+00:00", j))
    ledger.update_goal(conn, goal_id, job_id=j)
    return j


def test_the_board_names_the_model_its_rank_and_why_not_first(team_state, root):
    conn = ledger.connect(team_state / "team.db")
    ledger.init(conn)
    assert board.ranks(CAT) == {"medium": ["m-first", "m-second", "m-third"]}
    first, second, third = board.ranks(CAT)["medium"]
    gateway(team_state, [
        # #1 failed four minutes before the attempt: inside LiteLLM's cooldown.
        ("2026-10-30T09:56:00+00:00", "x", "tier-medium", "medium", f"openai/{first}", 0, 0, 0, 1, "429"),
        ("2026-10-30T10:01:00+00:00", "s1", "tier-medium", "medium", f"openai/{third}", 0, 0, 0, 0, "500"),
        ("2026-10-30T10:02:00+00:00", "s1", "tier-medium", "medium", f"openai/{second}", 900, 20, 1, 0, None),
    ])
    g = ledger.create_goal(conn, "team", "Set VALUE to 2", "implement", "team/b", "/w", "./check.sh")
    j = job(conn, g, "tier-medium")
    ledger.finish_goal(conn, g, "pass", "landed")
    ledger.record_call(conn, j, "litellm", "tier-medium", "implement", 900, 20, 10, True, None,
                       session_id="s1", profile="implement", response_model="tier-medium")
    ledger.record_call(conn, None, "team-subscription", "claude-sonnet-5-5", "boss", 4738, 300, 10, True,
                       None, profile="boss")

    b = board.board(conn, root, CAT)
    [card] = b["done"]
    route = card["attempts"][0]["route"]
    assert route["by"] == "session"
    assert route["models"] == [{"model": second, "calls": 1, "rank": 2, "of": 3}]
    assert [t["model"] for t in route["tried"]] == [third, second] and route["top_tried"] is False
    assert route["passed_over"] == [{"model": first, "rank": 1, "why": "rate limited"}]
    t = b["tokens"]
    assert t["total"]["tokens_in"] == 5638 and t["zones"]["boss"]["tokens_in"] == 4738
    assert t["zones"]["implement"]["tokens_out"] == 20
    assert t["total"]["cache_read"] is None  # not recorded yet, so not shown as zero
    tier = next(x for x in b["tiers"] if x["tier"] == "medium")
    assert tier["models"][0] == {**tier["models"][0], "model": first, "rank": 1, "calls": 1, "ok": 0}


def test_goals_sit_in_the_column_of_their_latest_attempt(team_state, root):
    conn = ledger.connect(team_state / "team.db")
    ledger.init(conn)
    running = ledger.create_goal(conn, "team", "a", "implement", "team/a", "/w", "./c")
    job(conn, running, "tier-medium", outcome="fail")
    job(conn, running, "claude", outcome="fail", attempt=3)
    conn.execute("UPDATE jobs SET ended=NULL, outcome=NULL WHERE attempt=3")
    waiting = ledger.create_goal(conn, "team", "b", "implement", "team/b", "/w", "./c")
    b = board.board(conn, root, CATALOG)
    assert [c["id"] for c in b["columns"]["rescue"]] == [running]
    assert [c["id"] for c in b["columns"]["lead"]] == [waiting]
    assert b["columns"]["implement"] == [] and b["done"] == []
    assert b["configured"]["boss"]["model"] == "claude-sonnet-5-5"


def test_an_attempt_is_ranked_by_the_tier_order_it_ran_under(tmp_path):
    r = tmp_path / "repo"
    path = r / "integrations" / "portkey" / "catalog.json"
    path.parent.mkdir(parents=True)
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.name", "T")
    git(r, "config", "user.email", "t@t")
    path.write_text(json.dumps(CAT))
    git(r, "add", "-A")
    git(r, "-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", "v1", "--date", "2026-10-29T00:00:00+00:00")
    later = json.loads(json.dumps(CAT))
    later["models"][0]["order"] = 9  # m-first drops to last, uncommitted
    path.write_text(json.dumps(later))
    at = board.ranks_at(r, later)
    assert at("2026-10-30T10:00:00+00:00")["medium"][0] == "m-first"  # committed order then
    assert at("2099-01-01T00:00:00+00:00")["medium"][0] == "m-second"  # the edit, dated by mtime
