"""litellm/team/effort_clamp.py: reasoning_effort moves into what the chosen deployment accepts."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[2] / "litellm" / "team" / "effort_clamp.py"
spec = importlib.util.spec_from_file_location("effort_clamp", PATH)
clamp_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(clamp_mod)

GROQ = ["low", "medium", "high"]


@pytest.mark.parametrize("asked, sent", [
    ("none", "low"), ("minimal", "low"), ("low", "low"), ("high", "high"),
    ("xhigh", "high"), ("max", "high"), ("ultra", "high"),
])
def test_groq_efforts(asked, sent):
    assert clamp_mod.clamp(asked, GROQ) == sent


def test_a_gap_in_the_list_goes_down():
    assert clamp_mod.clamp("medium", ["low", "high"]) == "low"


def test_unknown_values_and_rows_without_a_list_are_untouched():
    assert clamp_mod.clamp("turbo", GROQ) == "turbo"
    kwargs = {"model_info": {}, "reasoning_effort": "none"}
    assert clamp_mod.apply(kwargs)["reasoning_effort"] == "none"


def test_the_deployment_hook_clamps_top_level_and_extra_body():
    kwargs = {"model_info": {clamp_mod.INFO_KEY: GROQ}, "reasoning_effort": "none",
              "extra_body": {"reasoning_effort": "xhigh"}}
    out = asyncio.run(clamp_mod.clamp_hook.async_pre_call_deployment_hook(kwargs, None))
    assert out["reasoning_effort"] == "low" and out["extra_body"]["reasoning_effort"] == "high"


def test_ladder_matches_build_py():
    bspec = importlib.util.spec_from_file_location("portkey_build_e", PATH.parents[2] / "integrations/portkey/build.py")
    b = importlib.util.module_from_spec(bspec)
    bspec.loader.exec_module(b)
    assert b.EFFORTS == clamp_mod.LADDER
