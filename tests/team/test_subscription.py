"""The HTTP boundary preserves caller-owned tools, history and usage."""

import json

from fastapi.testclient import TestClient

from integrations.subscription.server import create_app
from tests.team.test_portkey_build import build, CATALOG


class Response:
    def __init__(self, payload):
        self.payload = payload

    def model_dump(self):
        return self.payload


def test_http_roundtrip_preserves_tool_calls_and_replay():
    requests = []
    closed = []
    message = {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "call_1", "type": "function",
                        "function": {"name": "my_tool", "arguments": "{}"}}],
        "reasoning_details": [{"signature": "opaque-replay"}],
    }
    usage = {"prompt_tokens": 100, "completion_tokens": 10,
             "prompt_tokens_details": {"cached_tokens": 80}}

    class Client:
        async def create(self, **kwargs):
            requests.append(kwargs)
            return Response({"choices": [{"message": message, "finish_reason": "tool_calls"}],
                             "usage": usage})

        def close(self):
            closed.append(True)

    with TestClient(create_app(Client, "local-test-token", ["opus"])) as http:
        headers = {"Authorization": "Bearer local-test-token"}
        body = {"model": "opus", "messages": [{"role": "system", "content": "Only my context"},
                {"role": "user", "content": "Use my tool"}],
                "tools": [{"type": "function", "function": {"name": "my_tool",
                           "parameters": {"type": "object", "properties": {}}}}]}
        assert http.post("/v1/chat/completions", json=body).status_code == 401
        response = http.post("/v1/chat/completions", headers=headers, json=body)
        assert response.status_code == 200
        assert response.json()["choices"][0]["message"] == message
        assert response.json()["usage"] == usage
        assert requests == [body]
        body["messages"] += [message, {"role": "tool", "tool_call_id": "call_1", "content": "done"}]
        assert http.post("/v1/chat/completions", headers=headers, json=body).status_code == 200
        assert requests[-1] == body
        assert len(closed) == 2


def test_catalog_routes_subscription_through_both_proxies_without_api_credit():
    catalog = json.loads(CATALOG.read_text())
    keys = {p["key_env"]: f"local-{name}" for name, p in catalog["providers"].items()}
    assert "ANTHROPIC_API_KEY" not in keys
    rendered = build.render(catalog, keys)
    config = json.loads(rendered["portkey/configs/team-model-claude-opus-5-5.json"])
    assert len(config["targets"]) == 1
    target = config["targets"][0]
    assert target["custom_host"] == "http://127.0.0.1:8790/v1"
    assert target["api_key"] == keys["SUBSCRIPTION_TOKEN"]
    deployments = build.litellm_config(catalog, build.routed(catalog))["model_list"]
    route = next(d for d in deployments if d["model_name"] == "claude-opus-5-5")
    assert route["litellm_params"]["api_base"] == "http://127.0.0.1:8788/v1"
    assert route["litellm_params"]["extra_headers"]["x-portkey-config"] == "team-model-claude-opus-5-5"
    assert json.loads(rendered["litellm/allowances.json"]) == {}


def test_stream_preserves_usage_and_closes_transport():
    closed = []

    class Stream:
        def __aiter__(self):
            return self.chunks()

        async def chunks(self):
            yield Response({"choices": [{"delta": {"content": "ok"}}]})
            yield Response({"choices": [], "usage": {"cache_creation_input_tokens": 12},
                            "_response": {"private": "duplicate final response"}})

        async def aclose(self):
            closed.append("stream")

    class Client:
        async def create(self, **kwargs):
            return Stream()

        def close(self):
            closed.append("client")

    with TestClient(create_app(Client, "token", ["opus"])) as http:
        response = http.post("/v1/chat/completions", headers={"Authorization": "Bearer token"},
                             json={"model": "opus", "messages": [{"role": "user", "content": "hi"}],
                                   "stream": True})
        assert response.status_code == 200
        chunks = [line.removeprefix("data: ") for line in response.text.splitlines() if line]
        assert chunks[-1] == "[DONE]"
        assert json.loads(chunks[-2]) == {"choices": [], "usage": {"cache_creation_input_tokens": 12}}
        assert closed == ["stream", "client"]


def test_hermes_keeps_proxy_replay_metadata(tmp_path, monkeypatch):
    from openai.types.chat import ChatCompletion
    from integrations.subscription.hermes_provider import promote, CARRIER
    from integrations.subscription.setup import install_profile
    from agent.transports.chat_completions import ChatCompletionsTransport
    from providers import get_provider_profile, refresh_bound_home

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    install_profile(tmp_path)
    refresh_bound_home()

    details = [{"type": CARRIER, "version": 1, "signature": "opaque"}]
    response = ChatCompletion.model_validate({
        "id": "test", "created": 0, "model": "claude-opus-5-5", "object": "chat.completion",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {
            "role": "assistant", "content": "ok", "provider_specific_fields": {"reasoning_details": details},
        }}],
    })
    promoted = promote(response)
    transport = ChatCompletionsTransport()
    profile = get_provider_profile("team-subscription")
    assert profile.native_reasoning_details_type == CARRIER
    message = {"role": "assistant", "content": "ok", "reasoning_details": promoted.choices[0].message.reasoning_details}
    wire = transport.convert_messages([message], provider_profile=profile,
                                       base_url="http://127.0.0.1:4000/v1", model="claude-opus-5-5")
    assert wire[0]["reasoning_details"] == details
