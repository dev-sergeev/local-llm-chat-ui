from __future__ import annotations

import time

import pytest

from datalab_chat.application import ChatApplication
from datalab_chat.gateways import GatewayFailure
from datalab_chat.generation import GenerationPolicy
from datalab_chat.memory import GenerationStatus, SQLiteChatMemory
from datalab_chat.profiles import EnvProfileCatalog, ProfileDraft, ProfileFormat, ProfileNotFound


class QueueGateway:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def complete(self, messages, *, timeout_seconds):
        self.calls.append((messages, timeout_seconds))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class QueueFactory:
    def __init__(self, gateway):
        self.gateway = gateway
        self.connections = []

    def create(self, connection):
        self.connections.append(connection)
        return self.gateway


def make_app(tmp_path, outcomes):
    gateway = QueueGateway(outcomes)
    app = ChatApplication(
        EnvProfileCatalog(tmp_path / ".env"),
        SQLiteChatMemory(tmp_path / "chat.db"),
        QueueFactory(gateway),
        generation_policy=GenerationPolicy(
            total_timeout_seconds=1,
            max_attempts=3,
            base_backoff_seconds=0,
            max_backoff_seconds=0,
            jitter_ratio=0,
            poll_interval_seconds=0.005,
        ),
    )
    return app, gateway


def add_profile(app, *, name="Giga PROD", provider_format=ProfileFormat.GIGACHAT):
    return app.create_profile(
        ProfileDraft(
            display_name=name,
            provider_format=provider_format,
            base_url="https://gateway.bank.local/v1",
            token="secret-token",
            model_id="risk-model",
        )
    )


def wait_terminal(app, generation_id):
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        generation = app.get_generation(generation_id)
        if generation.status not in {
            GenerationStatus.QUEUED,
            GenerationStatus.RUNNING,
            GenerationStatus.RETRYING,
        }:
            return generation
        time.sleep(0.005)
    raise AssertionError("generation did not finish")


def test_user_flow_is_available_through_one_application_interface(tmp_path):
    app, gateway = make_app(tmp_path, ["Первый ответ", "Ответ после изменения", "Ещё вариант"])
    profile = add_profile(app)
    conversation = app.create_conversation()
    assert conversation.active_profile_id == profile.id

    generation = app.send_message(conversation.id, "Первый вопрос")
    assert wait_terminal(app, generation.id).status is GenerationStatus.SUCCEEDED
    view = app.get_conversation(conversation.id)
    assert [message.content for message in view.messages] == ["Первый вопрос", "Первый ответ"]

    edited = app.edit_message(view.messages[0].id, "Изменённый вопрос")
    wait_terminal(app, edited.id)
    changed = app.get_conversation(conversation.id)
    assert [message.content for message in changed.messages] == [
        "Изменённый вопрос",
        "Ответ после изменения",
    ]

    regenerated = app.regenerate(changed.messages[-1].id)
    wait_terminal(app, regenerated.id)
    final = app.get_conversation(conversation.id)
    assert final.messages[-1].content == "Ещё вариант"
    assert final.messages[-1].variant_count == 2
    assert len(gateway.calls) == 3
    app.shutdown()


def test_model_can_change_for_the_next_message_only(tmp_path):
    app, _ = make_app(tmp_path, ["Giga answer", "OpenAI answer"])
    giga = add_profile(app)
    openai = add_profile(app, name="OpenAI TEST", provider_format=ProfileFormat.OPENAI)
    conversation = app.create_conversation(giga.id)

    first = app.send_message(conversation.id, "Раз", giga.id)
    wait_terminal(app, first.id)
    second = app.send_message(conversation.id, "Два", openai.id)
    wait_terminal(app, second.id)

    view = app.get_conversation(conversation.id)
    assistant_messages = [message for message in view.messages if message.role == "assistant"]
    assert assistant_messages[0].model_snapshot["display_name"] == "Giga PROD"
    assert assistant_messages[1].model_snapshot["display_name"] == "OpenAI TEST"
    assert view.active_profile_id == openai.id
    app.shutdown()


def test_deleting_profile_clears_selection_but_keeps_readable_history(tmp_path):
    app, _ = make_app(tmp_path, ["Сохранённый ответ"])
    profile = add_profile(app)
    conversation = app.create_conversation(profile.id)
    generation = app.send_message(conversation.id, "Вопрос")
    wait_terminal(app, generation.id)

    app.delete_profile(profile.id)

    view = app.get_conversation(conversation.id)
    assert view.active_profile_id is None
    assert view.messages[-1].model_snapshot["display_name"] == "Giga PROD"
    with pytest.raises(ProfileNotFound):
        app.send_message(conversation.id, "Новый вопрос", profile.id)
    app.shutdown()


def test_connection_check_is_not_written_to_chat_history(tmp_path):
    app, gateway = make_app(tmp_path, ["OK"])
    profile = add_profile(app)

    result = app.test_profile(profile.id, timeout_seconds=0.5)

    assert result.ok is True
    assert result.preview == "OK"
    assert result.latency_ms >= 0
    assert app.list_conversations() == []
    assert gateway.calls[0][0] == [
        {"role": "user", "content": "Ответь одним словом: OK"}
    ]
    app.shutdown()


def test_connection_check_returns_only_sanitized_gateway_failure(tmp_path):
    app, _ = make_app(
        tmp_path,
        [GatewayFailure("authentication", "Проверьте токен.", retryable=False)],
    )
    profile = add_profile(app)

    with pytest.raises(GatewayFailure) as failure:
        app.test_profile(profile.id, timeout_seconds=0.5)

    assert failure.value.code == "authentication"
    assert "secret-token" not in failure.value.message
    app.shutdown()


def test_application_recovers_unfinished_work_on_start(tmp_path):
    env = EnvProfileCatalog(tmp_path / ".env")
    memory = SQLiteChatMemory(tmp_path / "chat.db")
    profile = env.create(
        ProfileDraft(
            display_name="Giga",
            provider_format=ProfileFormat.GIGACHAT,
            base_url="https://gateway.local",
            token="secret",
            model_id="model",
        )
    )
    conversation = memory.create_conversation(profile.id)
    generation = memory.begin_user_generation(conversation.id, "До рестарта", profile.id)
    memory.mark_generation_running(generation.id, attempt=1)

    app = ChatApplication(env, SQLiteChatMemory(tmp_path / "chat.db"), QueueFactory(QueueGateway([])))

    assert app.get_generation(generation.id).status is GenerationStatus.INTERRUPTED
    app.shutdown()
