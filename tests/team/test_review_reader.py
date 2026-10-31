import pytest

from team.review import reader
from team.review.summarize import summary_path


@pytest.fixture(autouse=True)
def state(monkeypatch, tmp_path):
    monkeypatch.setenv("TEAM_STATE", str(tmp_path))


def _write(target, text):
    p = summary_path(target)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_missing_summary_returns_none():
    assert reader.read_summary("demo") is None
    assert not summary_path("demo").parent.exists()


def test_present_summary_is_returned_whole():
    _write("demo", "# What it is\nA tool.\n")
    assert reader.read_summary("demo") == "# What it is\nA tool.\n"


def test_oversize_summary_is_capped():
    _write("demo", "x" * 10_000)
    out = reader.read_summary("demo", max_chars=500)
    assert len(out) == 500
    assert out.endswith(reader.TRUNCATION_MARK)
    assert out.startswith("xxx")


def test_exactly_at_the_limit_is_not_truncated():
    _write("demo", "y" * 500)
    assert reader.read_summary("demo", max_chars=500) == "y" * 500


def test_default_cap_applies():
    _write("demo", "z" * (reader.MAX_SUMMARY_CHARS + 1))
    assert len(reader.read_summary("demo")) == reader.MAX_SUMMARY_CHARS


def test_bad_target_name_is_refused():
    import pytest
    for bad in ("../x", "A", ""):
        with pytest.raises(ValueError):
            reader.read_summary(bad)