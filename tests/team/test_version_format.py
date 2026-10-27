"""team/VERSION holds a semantic version: three dot-separated integers (e.g., 0.1.0)."""

import re
from pathlib import Path

VERSION_FILE = Path(__file__).resolve().parents[2] / "team" / "VERSION"

# Semantic version regex: three dot-separated integers (major.minor.patch)
SEMVER_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")

def test_version_file_is_semantic_version():
    """Assert that team/VERSION contains a semantic version: three dot-separated integers."""
    content = VERSION_FILE.read_text().strip()
    assert SEMVER_PATTERN.match(content), f"team/VERSION must be a semantic version (major.minor.patch), got: {content}"