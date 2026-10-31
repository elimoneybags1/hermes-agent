"""Installed provider profile: preserve native replay over the two HTTP proxies."""

from types import SimpleNamespace

from providers import register_provider
from providers.base import ProviderProfile

CARRIER = "claude-subscription-directsdk-experimental.native_assistant"


def promote(response):
    """LiteLLM parks nonstandard fields under provider_specific_fields."""
    for choice in response.choices:
        message = getattr(choice, "message", None) or getattr(choice, "delta", None)
        fields = getattr(message, "provider_specific_fields", None) or {}
        if fields.get("reasoning_details"):
            message.reasoning_details = fields["reasoning_details"]
    return response


class ProxyClient:
    def __init__(self, **kwargs):
        from openai import OpenAI

        allowed = {"api_key", "base_url", "timeout", "max_retries", "default_headers"}
        self.client = OpenAI(**{key: value for key, value in kwargs.items() if key in allowed})
        self.api_key = self.client.api_key
        self.base_url = self.client.base_url
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        response = self.client.chat.completions.create(**kwargs)
        if not kwargs.get("stream"):
            return promote(response)
        return ProxyStream(response)

    def close(self):
        self.client.close()


class ProxyStream:
    def __init__(self, response):
        self.response = response

    def __iter__(self):
        try:
            for chunk in self.response:
                yield promote(chunk)
        finally:
            self.close()

    def close(self):
        self.response.close()


class SubscriptionProxyProfile(ProviderProfile):
    def create_client(self, **kwargs):
        return ProxyClient(**kwargs)


register_provider(SubscriptionProxyProfile(
    name="team-subscription", display_name="Team Claude subscription via LiteLLM / Portkey",
    base_url="http://127.0.0.1:4000/v1", env_vars=("LITELLM_MASTER_KEY",),
    native_reasoning_details_type=CARRIER, supports_streaming=True,
))
