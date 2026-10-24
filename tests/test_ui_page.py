"""Static checks on team/ui/index.html (docs/UI.md, 'The page')."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PAGE = Path(__file__).resolve().parent.parent / "team" / "ui" / "index.html"


@pytest.fixture(scope="module")
def page() -> str:
    return PAGE.read_text()


def test_page_exists():
    assert PAGE.is_file()


def test_no_external_resources(page):
    assert not re.search(r"<script[^>]*\bsrc\s*=", page, re.I)
    assert not re.search(r"<link[^>]*\bhref\s*=", page, re.I)
    assert not re.search(r"@import", page)
    assert not re.search(r"https?://", page)


def test_no_blocking_dialogs(page):
    assert not re.search(r"\b(alert|confirm|prompt)\s*\(", page)


def test_dark_theme_via_custom_properties(page):
    assert "prefers-color-scheme: dark" in page
    assert re.search(r":root\s*\{[^}]*--bg:", page)


@pytest.mark.parametrize(
    "path",
    [
        "/api/state",
        "/api/agents/",
        "/role",
        "/instructions",
        "/skills/",
        "/revert",
        "/api/events",
        "/api/settings",
        "/api/targets/",
    ],
)
def test_references_api_path(page, path):
    assert path in page


@pytest.mark.parametrize("method", ["GET", "PATCH", "PUT", "DELETE", "POST"])
def test_uses_each_method(page, method):
    assert f"'{method}'" in page
