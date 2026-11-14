"""integrations/hermes/inherit.py: a project profile is main plus its own additions."""

from __future__ import annotations

import importlib.util
import sys
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("team_inherit", ROOT / "integrations" / "hermes" / "inherit.py")
inherit_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(inherit_mod)

MAIN_CONFIG = """\
model:
  default: tier-auto
plugins:
  enabled:
    - security-guidance
command_allowlist:
  - script execution via -e/-c flag
gateway:
  multiplex_profiles: true
"""

CHILD_CONFIG = """\
model:
  default: tier-auto
plugins:
  enabled:
    - project-plugin
terminal:
  cwd: /somewhere/project
"""


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "hermes"
    (h / "profiles" / "proj").mkdir(parents=True)
    (h / "config.yaml").write_text(MAIN_CONFIG)
    (h / "SOUL.md").write_text("You are main.\n")
    (h / "profiles" / "proj" / "config.yaml").write_text(CHILD_CONFIG)
    return h


def load(path: Path) -> dict:
    y = inherit_mod._yaml()
    with open(path) as f:
        return y.load(f)


def test_lists_union_main_first_and_keep_project_entries(home):
    inherit_mod.inherit(home, "proj", log=lambda m: None)
    cfg = load(home / "profiles" / "proj" / "config.yaml")
    assert list(cfg["plugins"]["enabled"]) == ["security-guidance", "project-plugin"]
    assert list(cfg["command_allowlist"]) == ["script execution via -e/-c flag"]


def test_shared_skills_dir_comes_first(home):
    inherit_mod.inherit(home, "proj", log=lambda m: None)
    cfg = load(home / "profiles" / "proj" / "config.yaml")
    assert list(cfg["skills"]["external_dirs"]) == [str(home / "skills")]


def test_project_keys_survive_and_main_only_keys_do_not_leak(home):
    inherit_mod.inherit(home, "proj", log=lambda m: None)
    cfg = load(home / "profiles" / "proj" / "config.yaml")
    assert cfg["terminal"]["cwd"] == "/somewhere/project"
    assert "gateway" not in cfg


def test_second_run_changes_nothing(home):
    inherit_mod.inherit(home, "proj", log=lambda m: None)
    path = home / "profiles" / "proj" / "config.yaml"
    before = path.read_text()
    assert not inherit_mod.merge_config(load(home / "config.yaml"), load(path), str(home / "skills"))
    inherit_mod.inherit(home, "proj", log=lambda m: None)
    assert path.read_text() == before


def test_soul_is_main_plus_project(home):
    pdir = home / "profiles" / "proj"
    (pdir / "SOUL.project.md").write_text("Work only on the project.\n")
    inherit_mod.inherit(home, "proj", log=lambda m: None)
    assert (pdir / "SOUL.md").read_text() == "You are main.\n\nWork only on the project.\n"


def test_soul_follows_a_change_in_main(home):
    pdir = home / "profiles" / "proj"
    inherit_mod.inherit(home, "proj", log=lambda m: None)
    (home / "SOUL.md").write_text("You are main, revised.\n")
    inherit_mod.inherit(home, "proj", log=lambda m: None)
    assert (pdir / "SOUL.md").read_text() == "You are main, revised.\n"
    assert not list(pdir.glob("SOUL.md.edited-*"))


def test_hand_edited_soul_is_saved_aside_not_lost(home):
    pdir = home / "profiles" / "proj"
    inherit_mod.inherit(home, "proj", log=lambda m: None)
    (pdir / "SOUL.md").write_text("Edited by hand.\n")
    messages = []
    inherit_mod.inherit(home, "proj", log=messages.append)
    saved = list(pdir.glob("SOUL.md.edited-*"))
    assert len(saved) == 1 and saved[0].read_text() == "Edited by hand.\n"
    assert (pdir / "SOUL.md").read_text() == "You are main.\n"
    assert messages


def test_soul_identical_to_main_is_not_treated_as_an_edit(home):
    pdir = home / "profiles" / "proj"
    (pdir / "SOUL.md").write_text("You are main.\n")  # a clone, before any stamp
    (pdir / "SOUL.project.md").write_text("Project rules.\n")
    inherit_mod.inherit(home, "proj", log=lambda m: None)
    assert not list(pdir.glob("SOUL.md.edited-*"))


def test_default_and_missing_profiles_are_left_alone(home):
    before = (home / "config.yaml").read_text()
    inherit_mod.inherit(home, "default", log=lambda m: None)
    inherit_mod.inherit(home, "nope", log=lambda m: None)
    assert (home / "config.yaml").read_text() == before
    assert not (home / "profiles" / "nope").exists()


def test_cli_entry_point(home):
    (home / "profiles" / "proj" / "SOUL.project.md").write_text("Project rules.\n")
    subprocess.run([sys.executable, str(ROOT / "integrations" / "hermes" / "inherit.py"),
                    str(home), "proj"], check=True, capture_output=True)
    assert (home / "profiles" / "proj" / "SOUL.md").read_text().endswith("Project rules.\n")
