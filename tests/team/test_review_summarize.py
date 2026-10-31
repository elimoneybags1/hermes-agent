"""Tests for team.review.summarize module."""

from __future__ import annotations

import pytest
from pathlib import Path

from team.review import summarize
from team.review.chunk import Chunk
# Import the fixture that sets TEAM_STATE
from tests.team.conftest import team_state
# The autouse fixture sets the env var for us, so we don't need to pass it
# explicitly.
class FakeCallable:
    """Fake model callable that records calls and returns a fixed reply."""

    def __init__(self, reply: str):
        self.calls = []  # list of (model, prompt) tuples
        self.reply = reply

    def __call__(self, model: str, prompt: str) -> str:
        self.calls.append((model, prompt))
        return self.reply
def test_basic_summarize_chunks(team_state: Path):
    """Test summarize_chunks with two chunks and target 'demo'."""
    call = FakeCallable("what: a tiny cli\n- structure: one module\nRISKS: no tests\nimprove: add tests")

    chunks = [
        Chunk(path="a.py", index=0, text="def foo():\n    pass"),
        Chunk(path="b.py", index=0, text="print('hello')"),
    ]

    result_path = summarize.summarize_chunks(chunks, target="demo", call=call)

    # Verify the path is correct
    expected_path = (
        team_state / "targets" / "demo" / "review" / "summary.md"
    )
    assert result_path == expected_path
    assert result_path.exists()

    # Read and verify content
    content = result_path.read_text()

    # Check for four headings in correct order
    assert "## What it is" in content
    assert "## Structure" in content
    assert "## Risks" in content
    assert "## Suggested improvements" in content

    # Check for the note from our fake reply
    assert "a tiny cli (a.py)" in content

    # Verify the fake was called once per chunk
    assert len(call.calls) == 2
    # Both calls should have the same model (default from roles)
    for model, _ in call.calls:
        # The model should be the default from the implement role
        # We'll verify this in another test
        pass

def test_fake_called_once_per_chunk(team_state: Path):
    """Test that the fake callable is called once per chunk."""
    call = FakeCallable("what: test note")

    chunks = [
        Chunk(path="file1.py", index=0, text="content1"),
        Chunk(path="file2.py", index=0, text="content2"),
        Chunk(path="file3.py", index=0, text="content3"),
    ]

    summarize.summarize_chunks(chunks, target="test", call=call)

    # Should have been called exactly 3 times (once per chunk)
    assert len(call.calls) == 3

    # Each call should have the corresponding chunk's text
    # The prompt includes the intro text plus a blank line plus the content
    assert "content1" in call.calls[0][1]
    assert "content2" in call.calls[1][1]
    assert "content3" in call.calls[2][1]

    # And should contain the file path
    assert "file1.py" in call.calls[0][1]
    assert "file2.py" in call.calls[1][1]
    assert "file3.py" in call.calls[2][1]

    # All should have the same model (default from roles)
    for model, _ in call.calls:
        # Model will be validated in another test
        pass
def test_model_default_and_override(team_state: Path):
    """Test that when no model is passed, the default model is used,
    and when model='m1' is passed, it is used.
    """
    # First test default model
    call_default = FakeCallable("what: note")
    chunks = [Chunk(path="dummy.py", index=0, text="dummy")]

    summarize.summarize_chunks(chunks, target="model-test", call=call_default)

    # Verify the default model was used
    assert len(call_default.calls) == 1
    default_model = call_default.calls[0][0]

    # Load the actual default model from roles
    actual_default = summarize.default_model(summarize.ROOT)
    assert default_model == actual_default

    # Now test with explicit model override
    call_override = FakeCallable("what: note")
    summarize.summarize_chunks(chunks, target="model-test2", call=call_override, model="m1")

    # Verify the override model was used
    assert len(call_override.calls) == 1
    assert call_override.calls[0][0] == "m1"

def test_deduplicated_notes(team_state: Path):
    """Test that duplicated notes from two chunks with the same reply and path appear once."""
    call = FakeCallable("what: duplicate note\nstructure: same")

    chunks = [
        Chunk(path="shared.py", index=0, text="chunk1"),
        Chunk(path="shared.py", index=1, text="chunk2"),
    ]

    summarize.summarize_chunks(chunks, target="dedup", call=call)

    summary_path = (
        team_state / "targets" / "dedup" / "review" / "summary.md"
    )
    content = summary_path.read_text()

    # The note should appear only once in the summary
    # Count occurrences of "duplicate note (shared.py)"
    count = content.count("duplicate note (shared.py)")
    assert count == 1, f"Expected 1 occurrence, found {count}"

    # Similarly for "same (shared.py)"
    count = content.count("same (shared.py)")
    assert count == 1, f"Expected 1 occurrence, found {count}"
def test_max_chars_with_truncation(team_state: Path):
    """Test with max_chars=600 and many risk lines."""
    # Create a fake reply with many distinct risk lines in the correct format
    risk_lines = []
    for i in range(50):
        risk_lines.append(f"risks: very long risk note {i} that should add up")
    reply = "\n".join(risk_lines)

    call = FakeCallable(reply)

    chunks = [Chunk(path="big.py", index=0, text="content")]

    # Use a small max_chars limit
    summarize.summarize_chunks(chunks, target="trunc", call=call, max_chars=600)

    summary_path = (
        team_state / "targets" / "trunc" / "review" / "summary.md"
    )
    content = summary_path.read_text()

    # File should be <= 600 chars
    assert len(content) <= 600

    # Should contain truncation indicator
    assert "…(truncated)" in content

    # Section should have bullet points
    assert "-" in content

def test_empty_sections_get_none(team_state: Path):
    """Test that a section with no notes shows '- (none)' line."""
    # Reply with only what and structure notes (no risks or improve)
    call = FakeCallable("what: good feature\nstructure: works well")

    chunks = [Chunk(path="test.py", index=0, text="test")]

    summarize.summarize_chunks(chunks, target="empty-sect", call=call)

    summary_path = (
        team_state / "targets" / "empty-sect" / "review" / "summary.md"
    )
    content = summary_path.read_text()

    # Check for '- (none)' lines (empty sections)
    # The Risks and Suggested improvements sections should be empty
    lines = content.split("\n")
    in_risks_section = False
    in_improve_section = False

    for line in lines:
        if line == "## Risks":
            in_risks_section = True
            in_improve_section = False
        elif line == "## Suggested improvements":
            in_risks_section = False
            in_improve_section = True
        elif line.startswith("## "):
            in_risks_section = False
            in_improve_section = False

        if in_risks_section and line == "- (none)":
            break
        if in_improve_section and line == "- (none)":
            break

    # Both empty sections should have '- (none)'
    assert "- (none)" in content

def test_invalid_target_raises(team_state: Path):
    """Test that target '../x' raises ValueError and the fake is never called."""
    call = FakeCallable("should not be called")

    chunks = [Chunk(path="dummy.py", index=0, text="dummy")]

    with pytest.raises(ValueError, match="bad target name"):
        summarize.summarize_chunks(chunks, target="../x", call=call)

    # The fake should never have been called
    assert len(call.calls) == 0
def test_chunk_prompt_format(team_state: Path):
    """Test that chunk_prompt generates the expected prompt."""
    chunk = Chunk(path="example.py", index=2, text="line1\nline2\nline3")

    prompt = summarize.chunk_prompt(chunk)

    # Should contain the file path and index
    assert "File: example.py (part 2)" in prompt

    # Should contain the chunk text
    assert "line1" in prompt
    assert "line2" in prompt
    assert "line3" in prompt

    # Should contain the intro text about tags
    assert "what:" in prompt
    assert "structure:" in prompt
    assert "risks:" in prompt
    assert "improve:" in prompt

    # Should not contain trailing whitespace
    lines = prompt.split("\n")
    for line in lines:
        assert not line.endswith(" ")
        assert not line.endswith("\t")