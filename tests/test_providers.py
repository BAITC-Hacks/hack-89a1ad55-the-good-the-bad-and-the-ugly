from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from contractor_matching.providers import ChatCompletionsProvider, ProviderConfig, ProviderError


def test_config_requires_explicit_protocol_full_url_model_and_key(monkeypatch):
    for key in ("ASTRA_API_PROTOCOL", "ASTRA_CHAT_COMPLETIONS_URL", "ASTRA_MODEL", "ASTRA_API_KEY", "ASTRA_API_KEY_1", "ASTRA_API_KEY_2", "ASTRA_API_KEY_3"):
        monkeypatch.delenv(key, raising=False)
    assert ProviderConfig.from_env("ASTRA", "astra", 2.5) is None
    monkeypatch.setenv("ASTRA_CHAT_COMPLETIONS_URL", "https://example.test/v1/chat/completions")
    monkeypatch.setenv("ASTRA_MODEL", "explicit-model")
    monkeypatch.setenv("ASTRA_API_KEY_1", "very-secret-test-key")
    assert ProviderConfig.from_env("ASTRA", "astra", 2.5) is None
    monkeypatch.setenv("ASTRA_API_PROTOCOL", "openai-chat-completions")
    config = ProviderConfig.from_env("ASTRA", "astra", 2.5)
    assert config.url == "https://example.test/v1/chat/completions"
    assert config.total_timeout == 2.5
    assert "very-secret-test-key" not in repr(config)
    assert "very-secret-test-key" not in json.dumps(config.public_identity())


def test_sol_configuration_keeps_env_compatibility_but_reports_openai(monkeypatch):
    monkeypatch.setenv("ASTRA_API_PROTOCOL", "openai-chat-completions")
    monkeypatch.setenv("ASTRA_CHAT_COMPLETIONS_URL", "https://api.openai.com/v1/chat/completions")
    monkeypatch.setenv("ASTRA_MODEL", "gpt-6-sol")
    monkeypatch.setenv("ASTRA_API_KEY_1", "dummy-sol-test-secret")
    config = ProviderConfig.from_env("ASTRA", "astra", 2.5)
    assert config.name == "openai"
    assert config.model == "gpt-6-sol"
    assert "dummy-sol-test-secret" not in repr(config)
    assert "dummy-sol-test-secret" not in json.dumps(config.public_identity())
    monkeypatch.setenv("ASTRA_MODEL", "gpt-6-astra")
    assert ProviderConfig.from_env("ASTRA", "astra", 2.5).name == "astra"


@pytest.mark.parametrize("url", ["http://example.test/chat", "https://username:password@example.test/chat", "https://example.test/chat?api_key=key", "not-a-url"])
def test_config_rejects_insecure_or_credential_bearing_url(monkeypatch, url):
    monkeypatch.setenv("ASTRA_API_PROTOCOL", "openai-chat-completions")
    monkeypatch.setenv("ASTRA_CHAT_COMPLETIONS_URL", url)
    monkeypatch.setenv("ASTRA_MODEL", "explicit-model")
    monkeypatch.setenv("ASTRA_API_KEY_1", "key")
    with pytest.raises(ValueError, match="complete HTTPS"):
        ProviderConfig.from_env("ASTRA", "astra", 2.5)


def test_one_batch_request_rotates_rate_limited_key_without_changing_prompt():
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"cards":[]}'}}]})

    provider = ChatCompletionsProvider(ProviderConfig("astra", "https://example.test/chat", "explicit-model", ("first", "second"), 2.5), httpx.MockTransport(handler))
    result = asyncio.run(provider.generate({"cards": []}))
    assert result == {"cards": []}
    assert len(requests) == 2
    assert requests[0].content == requests[1].content
    body = json.loads(requests[1].content)
    assert body["temperature"] == 0
    assert body["response_format"] == {"type": "json_object"}
    assert "approved_options" not in body["messages"][0]["content"]
    assert "fact_id" in body["messages"][0]["content"]


@pytest.mark.parametrize("url,model,is_reasoning", [
    ("https://api.openai.com/v1/chat/completions", "gpt-6-astra", True),
    ("https://api.openai.com/v1/chat/completions", "gpt-4.1-mini", False),
    ("https://explicit-provider.test/chat", "gpt-6-astra", False),
])
def test_openai_astra_payload_uses_documented_reasoning_parameters(url, model, is_reasoning):
    recorded = []

    def handler(request):
        recorded.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"cards":[]}'}}]})

    provider = ChatCompletionsProvider(ProviderConfig("astra", url, model, ("dummy-unit-test-key",), 2.5), httpx.MockTransport(handler))
    asyncio.run(provider.generate({"cards": []}))
    body = recorded[0]
    if is_reasoning:
        assert body["reasoning_effort"] == "low"
        assert body["max_completion_tokens"] == 2048
        assert "temperature" not in body and "max_tokens" not in body
    else:
        assert body["temperature"] == 0
        assert body["max_tokens"] == 700
        assert "reasoning_effort" not in body and "max_completion_tokens" not in body


def test_openai_sol_payload_uses_none_temperature_and_completion_budget():
    recorded = []

    def handler(request):
        recorded.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"cards":[]}'}}]})

    config = ProviderConfig("openai", "https://api.openai.com/v1/chat/completions", "gpt-6-sol", ("dummy-sol-test-secret",), 2.5)
    provider = ChatCompletionsProvider(config, httpx.MockTransport(handler))
    asyncio.run(provider.generate({"cards": []}))
    body = recorded[0]
    assert body["model"] == "gpt-6-sol"
    assert body["reasoning_effort"] == "none"
    assert body["temperature"] == 0
    assert body["max_completion_tokens"] == 1000
    assert "max_tokens" not in body
    assert "dummy-sol-test-secret" not in json.dumps(body)


def test_all_key_retries_share_total_timeout():
    calls = []

    async def handler(request):
        calls.append(request)
        await asyncio.sleep(0.02)
        return httpx.Response(429)

    provider = ChatCompletionsProvider(ProviderConfig("astra", "https://example.test/chat", "explicit-model", ("a", "b", "c"), 0.035), httpx.MockTransport(handler))
    start = time.monotonic()
    with pytest.raises(ProviderError, match="unavailable"):
        asyncio.run(provider.generate({"cards": []}))
    assert time.monotonic() - start < 0.2
    assert len(calls) <= 2


@pytest.mark.parametrize("body", [
    {},
    {"choices": []},
    {"choices": [{"message": {"content": "not JSON"}}]},
    {"choices": [{"message": {"content": []}}]},
    {"choices": [{"message": {"content": "[]"}}]},
])
def test_malformed_provider_response_is_sanitized(body):
    provider = ChatCompletionsProvider(ProviderConfig("astra", "https://example.test/chat", "explicit-model", ("secret-test-key",), 2.5),
                                       httpx.MockTransport(lambda request: httpx.Response(200, json=body)))
    with pytest.raises(ProviderError) as error:
        asyncio.run(provider.generate({"cards": []}))
    assert "secret-test-key" not in str(error.value)
    assert "example.test" not in str(error.value)


def test_redirect_not_followed():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "https://other.test/chat"})

    provider = ChatCompletionsProvider(ProviderConfig("astra", "https://example.test/chat", "explicit-model", ("secret",), 2.5), httpx.MockTransport(handler))
    with pytest.raises(ProviderError):
        asyncio.run(provider.generate({"cards": []}))
    assert len(calls) == 1
