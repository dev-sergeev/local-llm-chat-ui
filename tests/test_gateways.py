from __future__ import annotations

from types import SimpleNamespace

import pytest

from datalab_chat.gateways import (
    GatewayFailure,
    GigaChatGateway,
    OpenAICompatibleGateway,
)
from datalab_chat.profiles import ModelConnection, ProfileFormat


CONNECTION = ModelConnection(
    id="profile-a",
    display_name="OpenAI-compatible PROD",
    provider_format=ProfileFormat.OPENAI,
    base_url="https://llm.bank.local/v1",
    token="secret-token",
    model_id="risk-model",
)


def install_openai_model(monkeypatch, content):
    import langchain_openai

    captured = {}

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            captured["kwargs"] = kwargs

        def invoke(self, messages):
            captured["messages"] = messages
            return SimpleNamespace(content=content)

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", FakeChatOpenAI)
    return captured


def test_openai_adapter_passes_model_id_and_emits_one_complete_chunk(monkeypatch):
    captured = install_openai_model(monkeypatch, "  Готовый ответ  ")
    chunks = []

    answer = OpenAICompatibleGateway(CONNECTION).complete(
        [{"role": "user", "content": "Вопрос"}],
        timeout_seconds=42,
        on_chunk=chunks.append,
    )

    assert answer == "Готовый ответ"
    assert chunks == ["Готовый ответ"]
    assert captured["kwargs"] == {
        "model": "risk-model",
        "api_key": "secret-token",
        "base_url": "https://llm.bank.local/v1",
        "timeout": 42,
        "max_retries": 0,
        "streaming": False,
    }


def test_gigachat_adapter_passes_model_id_url_and_token(monkeypatch):
    import langchain_gigachat

    captured = {}

    class FakeGigaChat:
        def __init__(self, **kwargs):
            captured["kwargs"] = kwargs

        def invoke(self, messages):
            captured["messages"] = messages
            return SimpleNamespace(content="Giga ответ")

    monkeypatch.setattr(langchain_gigachat, "GigaChat", FakeGigaChat)
    connection = ModelConnection(
        id="profile-giga",
        display_name="Giga PROD",
        provider_format=ProfileFormat.GIGACHAT,
        base_url="https://giga.bank.local/v1",
        token="giga-token",
        model_id="GigaChat-Pro",
    )

    answer = GigaChatGateway(connection).complete(
        [{"role": "user", "content": "Вопрос"}],
        timeout_seconds=60,
    )

    assert answer == "Giga ответ"
    assert captured["kwargs"] == {
        "access_token": "giga-token",
        "base_url": "https://giga.bank.local/v1",
        "model": "GigaChat-Pro",
        "timeout": 60,
        "max_retries": 0,
        "streaming": False,
    }


@pytest.mark.parametrize(
    "content",
    [
        "   ",
        None,
        {"text": "not a supported top-level response"},
        [
            {"type": "text", "text": "partial text"},
            {"type": "image_url", "image_url": "ignored"},
        ],
    ],
)
def test_adapter_rejects_empty_or_damaged_responses_without_retry(monkeypatch, content):
    install_openai_model(monkeypatch, content)

    with pytest.raises(GatewayFailure) as failure:
        OpenAICompatibleGateway(CONNECTION).complete(
            [{"role": "user", "content": "Вопрос"}],
            timeout_seconds=42,
        )

    assert failure.value.code in {"empty_response", "invalid_response"}
    assert failure.value.retryable is False
