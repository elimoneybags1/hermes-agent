"""[team] hermes_cli/cli_status_bar_mixin.py: the bar shows tier-auto and, next to it, the model that answered."""

from __future__ import annotations

from tests.hermes_cli.test_cli_status_bar import _attach_agent, _make_cli


def _cli(served):
    cli_obj = _attach_agent(_make_cli("tier-auto"), prompt_tokens=10, completion_tokens=5, total_tokens=15,
                            api_calls=1, context_tokens=1000, context_length=131072)
    cli_obj.agent.last_served_model = served
    return cli_obj


def test_shows_the_served_deployment():
    snap = _cli("tier-medium/north-mini-code")._get_status_bar_snapshot()
    assert snap["model_short"] == "tier-auto (north-mini-code)"
    assert snap["model_name"] == "tier-auto"  # cache-hit baselines still key on the asked-for model


def test_a_long_served_name_is_cut_on_its_own():
    snap = _cli("tier-complex/llama-3.1-nemotron-ultra-253b")._get_status_bar_snapshot()
    assert snap["model_short"] == "tier-auto (llama-3.1-nemotron-ultr...)"


def test_falls_back_to_the_alias_without_a_header_or_with_an_api_base():
    assert _cli(None)._get_status_bar_snapshot()["model_short"] == "tier-auto"
    assert _cli("http://127.0.0.1:8788/v1")._get_status_bar_snapshot()["model_short"] == "tier-auto"


def test_model_is_left_of_the_context_detail():
    text = _cli("tier-medium/north-mini-code")._build_status_bar_text(width=120)
    ctx = next(part.strip() for part in text.split("│") if "/" in part)  # context used/total, e.g. "1K/131.1K"
    assert text.index("north-mini-code") < text.index(ctx)


def test_the_gateway_passes_the_served_model_back_to_the_classic_cli():
    # The live CLI talks to the agent through the gateway (GatewayAgent): served_model must
    # survive the classic-CLI result projection, or the bar only ever shows the alias.
    from gateway.session_cli_turn import take_cli_result_fields

    fields = take_cli_result_fields({"final_response": "ok", "served_model": "tier-medium/north-mini-code",
                                     "requested_model": "tier-auto"})
    assert fields["served_model"] == "tier-medium/north-mini-code" and fields["requested_model"] == "tier-auto"
