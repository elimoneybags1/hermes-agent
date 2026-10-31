import io
import json
import subprocess

import pytest
from pathlib import Path

from team import cli
from team.review import __main__


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "proj"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.name", "T")
    git(r, "config", "user.email", "t@t")
    (r / "README.md").write_text("# demo")
    (r / "app.py").write_text("print(1)")
    git(r, "add", "README.md", "app.py")
    git(r, "commit", "-q", "-m", "one")
    return r
class FakeCallable:
    """Fake model callable that records calls and returns a fixed reply."""
    def __init__(self, reply: str):
        self.calls = []  # list of (model, prompt) tuples
        self.reply = reply

    def __call__(self, model: str, prompt: str) -> str:
        self.calls.append((model, prompt))
        return self.reply
def test_review_prints_summary_path(repo, team_state):
    """Test that team review prints the summary path."""
    fake = FakeCallable("what: a tiny cli\n- structure: one module\nRISKS: no tests\nimprove: add tests")
    
    # Call main with the repo path and fake call
    result = __main__.main([str(repo)], call=fake)
    
    # Should return 0 for success
    assert result == 0
    
    # The fake should have been called at least once
    assert len(fake.calls) >= 1
    
    # Capture stdout by redirecting sys.stdout temporarily
    import sys
    from io import StringIO
    
    old_stdout = sys.stdout
    sys.stdout = StringIO()
    
    try:
        __main__.main([str(repo)], call=fake)
        output = sys.stdout.getvalue().strip()
    finally:
        sys.stdout = old_stdout
    
    # Output should be a path that exists inside team_state
    output_path = Path(output)
    assert output_path.exists()
    assert str(team_state) in str(output_path)
    assert output_path.read_text()  # Should have non-empty text
    
    # The output should NOT contain 'print(1)' from app.py
    assert 'print(1)' not in output
def test_review_bad_url_exits_1(tmp_path, team_state):
    """Test that team review exits with 1 on bad URL."""
    fake = FakeCallable("should not be called")
    
    # Call with a non-existent path
    result = __main__.main([str(tmp_path / 'nope')], call=fake)
    
    # Should return 1 for error
    assert result == 1
    
    # The fake should not have been called
    assert len(fake.calls) == 0
def test_default_call_refuses(repo, team_state, capsys):
    """Test that team review fails when no call is provided."""
    # Call without providing a call function
    result = __main__.main([str(repo)])
    
    # Should return 1 for error
    assert result == 1
    
    # Error should be in stderr
    err_output = capsys.readouterr().err
    assert "team review:" in err_output


if __name__ == "__main__":
    pytest.main([__file__, "-v"])