"""team/VERSION holds the single version line."""

from pathlib import Path

VERSION_FILE = Path(__file__).resolve().parents[2] / "team" / "VERSION"


def test_version_file_is_0_1_0():
    assert VERSION_FILE.read_text().strip() == "0.1.0"
