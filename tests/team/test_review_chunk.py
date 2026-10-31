import pytest
from pathlib import Path
from team.review.chunk import Chunk, chunk_repo


def test_yields_text_files_with_relative_paths(tmp_path):
    """Test basic text file yielding with correct paths and order."""
    # Create test files
    (tmp_path / "a.py").write_text("print('hello')\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.md").write_text("# Title\n")

    result = list(chunk_repo(tmp_path))

    # Expect two chunks, one from each file
    assert len(result) == 2
    # Files are visited in sorted order: root files first, then sub/
    assert result[0] == Chunk("a.py", 0, "print('hello')\n")
    assert result[1] == Chunk("sub/b.md", 0, "# Title\n")


def test_skips_git_dir(tmp_path):
    """Test that .git directory and its contents are skipped."""
    (tmp_path / ".git" / "config").parent.mkdir(parents=True)
    (tmp_path / ".git" / "config").write_text("[remote \"origin\"]\n")
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (tmp_path / "a.py").write_text("print('test')\n")

    result = list(chunk_repo(tmp_path))

    # Only a.py should be included
    assert len(result) == 1
    assert result[0] == Chunk("a.py", 0, "print('test')\n")
    # No chunk should have a path starting with ".git"
    for chunk in result:
        assert not chunk.path.startswith(".git")


def test_skips_binaries(tmp_path):
    """Test that binary files (with null bytes) are skipped."""
    (tmp_path / "bin.dat").write_bytes(b"\x00\x01\x02abc")
    (tmp_path / "latin.txt").write_bytes(b"\xff\xfe\xfa")

    result = list(chunk_repo(tmp_path))

    # Both binary-like files should be skipped
    assert len(result) == 0


def test_skips_lockfiles(tmp_path):
    """Test that lockfiles are skipped but other .lock files are kept."""
    (tmp_path / "package-lock.json").write_text('{"version": "1.0.0"}')
    (tmp_path / "uv.lock").write_text("lock content")
    (tmp_path / "Cargo.lock").write_text("[[package]]")
    (tmp_path / "foo.lock").write_text("lock file")  # Should be skipped (ends with .lock)
    (tmp_path / "ok.py").write_text("print('ok')\n")

    result = list(chunk_repo(tmp_path))

    # Only ok.py should be included (foo.lock ends with .lock)
    assert len(result) == 1
    assert result[0] == Chunk("ok.py", 0, "print('ok')\n")


def test_skips_files_over_size_limit(tmp_path):
    """Test that files exceeding max_file_bytes are skipped."""
    # Create a file larger than the limit (100 bytes)
    (tmp_path / "big.txt").write_bytes(b"x" * 101)
    # Create a file exactly at the limit (100 bytes)
    (tmp_path / "exact.txt").write_bytes(b"x" * 100)

    result = list(chunk_repo(tmp_path, max_file_bytes=100))

    # Only the exact-sized file should be included
    assert len(result) == 1
    assert result[0] == Chunk("exact.txt", 0, "x" * 100)


def test_chunks_are_bounded(tmp_path):
    """Test that chunks never exceed max_chunk_chars."""
    # Create a file with 50 lines, each 9 chars plus newline = 10 bytes per line
    # Total ~500 bytes, which should create multiple chunks with default limits
    lines = ["x" * 9 + "\n" for _ in range(50)]
    content = "".join(lines)
    (tmp_path / "manylines.txt").write_text(content)

    result = list(chunk_repo(tmp_path, max_chunk_chars=100))

    # Verify each chunk is <= 100 chars
    for chunk in result:
        assert len(chunk.text) <= 100
    # Verify chunks are in order with correct indexes
    for i, chunk in enumerate(result):
        assert chunk.index == i
    # Verify concatenation equals original text (ignoring whitespace-only chunks)
    reconstructed = "".join(c.text for c in result)
    assert reconstructed == content


def test_long_line_is_sliced(tmp_path):
    """Test that a single line longer than max_chunk_chars is sliced."""
    long_line = "x" * 250
    (tmp_path / "longline.txt").write_text(long_line)

    result = list(chunk_repo(tmp_path, max_chunk_chars=100))

    # Should be split into 3 chunks: 100, 100, 50
    assert len(result) == 3
    assert result[0] == Chunk("longline.txt", 0, "x" * 100)
    assert result[1] == Chunk("longline.txt", 1, "x" * 100)
    assert result[2] == Chunk("longline.txt", 2, "x" * 50)


def test_skips_symlinks_and_empty_files(tmp_path):
    """Test that symlinks and empty files are skipped."""
    # Create an empty file
    (tmp_path / "empty.txt").write_text("")
    # Create a symlink to a file outside the root
    outside_dir = tmp_path.parent
    target_file = outside_dir / "outside.txt"
    target_file.write_text("secret")
    (tmp_path / "link.txt").symlink_to(target_file)

    result = list(chunk_repo(tmp_path))

    # Both should be skipped
    assert len(result) == 0


def test_rejects_bad_limits(tmp_path):
    """Test that invalid limits raise ValueError."""
    (tmp_path / "a.py").write_text("test")

    # max_chunk_chars=0 should raise ValueError
    with pytest.raises(ValueError):
        list(chunk_repo(tmp_path, max_chunk_chars=0))
    # max_file_bytes=0 should also raise ValueError
    with pytest.raises(ValueError):
        list(chunk_repo(tmp_path, max_file_bytes=0))