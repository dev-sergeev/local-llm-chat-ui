from __future__ import annotations

import threading
import time

import pytest

from datalab_chat.application import ChatApplication
from datalab_chat.gateways import GatewayFailure
from datalab_chat.generation import GenerationPolicy
from datalab_chat.memory import GenerationStatus, SQLiteChatMemory
from datalab_chat.profiles import (
    EnvProfileCatalog,
    ProfileDraft,
    ProfileFormat,
    ProfileNotFound,
)


class QueueGateway:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def complete(self, messages, *, timeout_seconds, on_chunk=None):
        self.calls.append((messages, timeout_seconds))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if callable(outcome):
            return outcome()
        if on_chunk is not None:
            on_chunk(outcome)
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
    factory = QueueFactory(gateway)
    app = ChatApplication(
        EnvProfileCatalog(tmp_path / ".env"),
        SQLiteChatMemory(tmp_path / "chat.db"),
        factory,
        generation_policy=GenerationPolicy(
            total_timeout_seconds=1,
            max_attempts=3,
            base_backoff_seconds=0,
            max_backoff_seconds=0,
            jitter_ratio=0,
            poll_interval_seconds=0.005,
        ),
    )
    return app, gateway, factory


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
    app, gateway, _ = make_app(
        tmp_path, ["Первый ответ", "Ответ после изменения", "Ещё вариант"]
    )
    profile = add_profile(app)
    conversation = app.create_conversation()
    assert conversation.active_profile_id == profile.id

    generation = app.send_message(conversation.id, "Первый вопрос")
    assert wait_terminal(app, generation.id).status is GenerationStatus.SUCCEEDED
    view = app.get_conversation(conversation.id)
    assert [message.content for message in view.messages] == [
        "Первый вопрос",
        "Первый ответ",
    ]

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
    app, _, _ = make_app(tmp_path, ["Giga answer", "OpenAI answer"])
    giga = add_profile(app)
    openai = add_profile(app, name="OpenAI TEST", provider_format=ProfileFormat.OPENAI)
    conversation = app.create_conversation(giga.id)

    first = app.send_message(conversation.id, "Раз", giga.id)
    wait_terminal(app, first.id)
    second = app.send_message(conversation.id, "Два", openai.id)
    wait_terminal(app, second.id)

    view = app.get_conversation(conversation.id)
    assistant_messages = [
        message for message in view.messages if message.role == "assistant"
    ]
    assert assistant_messages[0].model_snapshot.display_name == "Giga PROD"
    assert assistant_messages[1].model_snapshot.display_name == "OpenAI TEST"
    assert view.active_profile_id == openai.id
    app.shutdown()


def test_deleting_profile_clears_selection_but_keeps_readable_history(tmp_path):
    app, _, _ = make_app(tmp_path, ["Сохранённый ответ"])
    profile = add_profile(app)
    conversation = app.create_conversation(profile.id)
    generation = app.send_message(conversation.id, "Вопрос")
    wait_terminal(app, generation.id)

    app.delete_profile(profile.id)

    view = app.get_conversation(conversation.id)
    assert view.active_profile_id is None
    assert view.messages[-1].model_snapshot.display_name == "Giga PROD"
    with pytest.raises(ProfileNotFound):
        app.send_message(conversation.id, "Новый вопрос", profile.id)
    app.shutdown()


def test_connection_check_is_not_written_to_chat_history(tmp_path):
    app, gateway, _ = make_app(tmp_path, ["OK"])
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
    app, _, _ = make_app(
        tmp_path,
        [GatewayFailure("authentication", "Проверьте токен.", retryable=False)],
    )
    profile = add_profile(app)

    with pytest.raises(GatewayFailure) as failure:
        app.test_profile(profile.id, timeout_seconds=0.5)

    assert failure.value.code == "authentication"
    assert "secret-token" not in failure.value.message
    app.shutdown()


def test_connection_check_enforces_deadline_when_adapter_ignores_timeout(tmp_path):
    release = threading.Event()
    app, _, _ = make_app(tmp_path, [lambda: release.wait(1) or "late"])
    profile = add_profile(app)
    started = time.monotonic()

    with pytest.raises(GatewayFailure) as failure:
        app.test_profile(profile.id, timeout_seconds=0.03)

    assert failure.value.code == "request_timeout"
    assert time.monotonic() - started < 0.2
    release.set()
    app.shutdown()


def test_connection_check_uses_unsaved_form_values_without_persisting_them(tmp_path):
    app, gateway, factory = make_app(tmp_path, ["OK"])
    profile = add_profile(app)

    result = app.test_profile_draft(
        ProfileDraft(
            display_name="Изменённое имя",
            provider_format=ProfileFormat.OPENAI,
            base_url="https://draft.gateway.local/v1",
            token="draft-secret",
            model_id="draft-model",
        ),
        profile_id=profile.id,
        timeout_seconds=0.5,
    )

    assert result.ok is True
    tested = factory.connections[-1]
    assert tested.base_url == "https://draft.gateway.local/v1"
    assert tested.token == "draft-secret"
    assert tested.model_id == "draft-model"
    assert app.get_profile(profile.id).base_url == "https://gateway.bank.local/v1"
    assert gateway.calls
    app.shutdown()


def test_combined_conversation_update_is_atomic_when_profile_is_invalid(tmp_path):
    app, _, _ = make_app(tmp_path, [])
    profile = add_profile(app)
    conversation = app.create_conversation(profile.id)

    with pytest.raises(ProfileNotFound):
        app.update_conversation(
            conversation.id,
            title="Не должно сохраниться",
            profile_id="missing-profile",
            set_profile=True,
        )

    unchanged = app.get_conversation(conversation.id)
    assert unchanged.title == "Новый чат"
    assert unchanged.active_profile_id == profile.id
    app.shutdown()


def test_capacity_overflow_remains_durable_and_runs_when_a_slot_is_free(tmp_path):
    release = threading.Event()

    def blocked_answer():
        release.wait(1)
        return "done"

    app, _, _ = make_app(tmp_path, [blocked_answer] * 4 + ["after capacity"])
    profile = add_profile(app)
    active_generations = []
    for _ in range(4):
        conversation = app.create_conversation(profile.id)
        active_generations.append(app.send_message(conversation.id, "Запрос"))

    overflow_conversation = app.create_conversation(profile.id)
    overflow = app.send_message(overflow_conversation.id, "Лишний запрос")

    assert overflow.status is GenerationStatus.QUEUED
    assert overflow.error_code is None
    view = app.get_conversation(overflow_conversation.id)
    assert [message.content for message in view.messages] == ["Лишний запрос"]
    assert view.active_generation.id == overflow.id
    release.set()
    for generation in active_generations:
        assert wait_terminal(app, generation.id).status is GenerationStatus.SUCCEEDED
    assert wait_terminal(app, overflow.id).status is GenerationStatus.SUCCEEDED
    assert app.get_conversation(overflow_conversation.id).messages[-1].content == (
        "after capacity"
    )
    app.shutdown()


def test_generation_start_failure_is_returned_after_prompt_is_committed(
    tmp_path,
    monkeypatch,
):
    app, _, _ = make_app(tmp_path, ["Не вызывается"])
    profile = add_profile(app)
    conversation = app.create_conversation(profile.id)

    def fail_start(_thread):
        raise RuntimeError("cannot start thread")

    monkeypatch.setattr(threading.Thread, "start", fail_start)

    generation = app.send_message(conversation.id, "Сохранённый запрос")

    assert generation.status is GenerationStatus.FAILED
    assert generation.error_code == "internal_start_error"
    view = app.get_conversation(conversation.id)
    assert [message.content for message in view.messages] == ["Сохранённый запрос"]
    assert view.active_generation.id == generation.id
    assert view.active_generation.status is GenerationStatus.FAILED
    app.shutdown()


def test_capacity_queued_generation_survives_shutdown_and_resumes(tmp_path):
    release = threading.Event()

    def blocked_answer():
        release.wait(1)
        return "Не сохранять"

    app, _, _ = make_app(tmp_path, [blocked_answer] * 4)
    profile = add_profile(app)
    for _ in range(4):
        conversation = app.create_conversation(profile.id)
        app.send_message(conversation.id, "Занять слот")

    queued_conversation = app.create_conversation(profile.id)
    queued = app.send_message(queued_conversation.id, "Возобновить после рестарта")
    assert queued.status is GenerationStatus.QUEUED

    app.shutdown()
    release.set()

    assert app.get_generation(queued.id).status is GenerationStatus.QUEUED
    resumed_gateway = QueueGateway(["Ответ после рестарта"])
    restarted = ChatApplication(
        EnvProfileCatalog(tmp_path / ".env"),
        SQLiteChatMemory(tmp_path / "chat.db"),
        QueueFactory(resumed_gateway),
    )
    assert wait_terminal(restarted, queued.id).status is GenerationStatus.SUCCEEDED
    assert restarted.get_conversation(queued_conversation.id).messages[-1].content == (
        "Ответ после рестарта"
    )
    restarted.shutdown()


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
    generation = memory.begin_user_generation(
        conversation.id, "До рестарта", profile.id
    )
    memory.mark_generation_running(generation.id, attempt=1)

    app = ChatApplication(
        env, SQLiteChatMemory(tmp_path / "chat.db"), QueueFactory(QueueGateway([]))
    )

    assert app.get_generation(generation.id).status is GenerationStatus.INTERRUPTED
    app.shutdown()


def test_messages_sent_while_model_is_busy_run_in_fifo_order(tmp_path):
    release = threading.Event()

    def first_answer():
        release.wait(1)
        return "Ответ один"

    app, gateway, _ = make_app(tmp_path, [first_answer, "Ответ два", "Ответ три"])
    profile = add_profile(app)
    conversation = app.create_conversation(profile.id)

    first = app.send_message(conversation.id, "Первый")
    deadline = time.monotonic() + 1
    while (
        app.get_generation(first.id).status is GenerationStatus.QUEUED
        and time.monotonic() < deadline
    ):
        time.sleep(0.005)

    second = app.send_message(conversation.id, "Второй")
    third = app.send_message(conversation.id, "Третий")
    assert [second.content, third.content] == ["Второй", "Третий"]
    assert [
        item.content for item in app.get_conversation(conversation.id).queued_messages
    ] == [
        "Второй",
        "Третий",
    ]

    release.set()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        view = app.get_conversation(conversation.id)
        if len(view.messages) == 6 and not view.queued_messages:
            break
        time.sleep(0.005)

    assert [message.content for message in view.messages] == [
        "Первый",
        "Ответ один",
        "Второй",
        "Ответ два",
        "Третий",
        "Ответ три",
    ]
    assert [call[0][-1]["content"] for call in gateway.calls] == [
        "Первый",
        "Второй",
        "Третий",
    ]
    assert [item["content"] for item in gateway.calls[1][0]] == [
        "Первый",
        "Ответ один",
        "Второй",
    ]
    assert [item["content"] for item in gateway.calls[2][0]] == [
        "Первый",
        "Ответ один",
        "Второй",
        "Ответ два",
        "Третий",
    ]
    app.shutdown()


def test_cancelled_generation_starts_next_queued_message(tmp_path):
    release = threading.Event()

    def late_answer():
        release.wait(1)
        return "Поздний ответ"

    app, _, _ = make_app(tmp_path, [late_answer, "Ответ очереди"])
    profile = add_profile(app)
    conversation = app.create_conversation(profile.id)
    first = app.send_message(conversation.id, "Отмени меня")

    deadline = time.monotonic() + 1
    while (
        app.get_generation(first.id).status is GenerationStatus.QUEUED
        and time.monotonic() < deadline
    ):
        time.sleep(0.005)
    queued = app.send_message(conversation.id, "Выполни следующим")
    assert queued.content == "Выполни следующим"

    cancelled = app.cancel_generation(first.id)
    assert cancelled.status is GenerationStatus.CANCELLED
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        view = app.get_conversation(conversation.id)
        if [message.content for message in view.messages][-1:] == ["Ответ очереди"]:
            break
        time.sleep(0.005)

    release.set()
    assert [message.content for message in view.messages] == [
        "Отмени меня",
        "Выполни следующим",
        "Ответ очереди",
    ]
    assert "Поздний ответ" not in [message.content for message in view.messages]
    app.shutdown()


def test_failed_generation_pauses_queue_until_retry_succeeds(tmp_path):
    release = threading.Event()

    def failed_answer():
        release.wait(1)
        raise GatewayFailure(
            "provider_rejected",
            "Модель отклонила запрос.",
            retryable=False,
        )

    app, gateway, _ = make_app(
        tmp_path,
        [failed_answer, "Ответ повтора", "Ответ очереди"],
    )
    profile = add_profile(app)
    conversation = app.create_conversation(profile.id)
    first = app.send_message(conversation.id, "Первый")
    deadline = time.monotonic() + 1
    while (
        app.get_generation(first.id).status is GenerationStatus.QUEUED
        and time.monotonic() < deadline
    ):
        time.sleep(0.005)
    app.send_message(conversation.id, "Второй")

    release.set()
    assert wait_terminal(app, first.id).status is GenerationStatus.FAILED
    time.sleep(0.03)
    paused = app.get_conversation(conversation.id)
    assert [item.content for item in paused.queued_messages] == ["Второй"]
    assert len(gateway.calls) == 1

    retried = app.retry_generation(first.id)
    assert wait_terminal(app, retried.id).status is GenerationStatus.SUCCEEDED
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        resumed = app.get_conversation(conversation.id)
        if len(resumed.messages) == 4 and not resumed.queued_messages:
            break
        time.sleep(0.005)
    assert [message.content for message in resumed.messages] == [
        "Первый",
        "Ответ повтора",
        "Второй",
        "Ответ очереди",
    ]
    app.shutdown()


def test_persisted_queued_generation_resumes_after_restart(tmp_path):
    env = EnvProfileCatalog(tmp_path / ".env")
    profile = env.create(
        ProfileDraft(
            display_name="Giga",
            provider_format=ProfileFormat.GIGACHAT,
            base_url="https://gateway.local",
            token="secret",
            model_id="model",
        )
    )
    memory = SQLiteChatMemory(tmp_path / "chat.db")
    conversation = memory.create_conversation(profile.id)
    connection = env.resolve(profile.id)
    generation = memory.submit_user_message(
        conversation.id,
        "Принято до рестарта",
        profile.id,
        connection.snapshot(),
        connection.revision,
    )
    assert generation.status is GenerationStatus.QUEUED

    gateway = QueueGateway(["Ответ после рестарта"])
    app = ChatApplication(
        env, SQLiteChatMemory(tmp_path / "chat.db"), QueueFactory(gateway)
    )

    assert wait_terminal(app, generation.id).status is GenerationStatus.SUCCEEDED
    assert [
        message.content for message in app.get_conversation(conversation.id).messages
    ] == [
        "Принято до рестарта",
        "Ответ после рестарта",
    ]
    app.shutdown()


def test_deleting_profile_blocks_queued_message_without_losing_content(tmp_path):
    release = threading.Event()

    def active_answer():
        release.wait(1)
        return "Активный ответ"

    app, _, _ = make_app(tmp_path, [active_answer])
    profile = add_profile(app)
    conversation = app.create_conversation(profile.id)
    first = app.send_message(conversation.id, "Активный")
    deadline = time.monotonic() + 1
    while (
        app.get_generation(first.id).status is GenerationStatus.QUEUED
        and time.monotonic() < deadline
    ):
        time.sleep(0.005)
    queued = app.send_message(conversation.id, "Не потерять")

    app.delete_profile(profile.id)
    blocked = app.get_conversation(conversation.id).queued_messages[0]

    assert blocked.id == queued.id
    assert blocked.content == "Не потерять"
    assert blocked.status == "blocked"
    assert blocked.error_code == "profile_not_found"
    assert blocked.model_snapshot.display_name == "Giga PROD"
    assert "secret-token" not in str(blocked.to_public_dict())

    release.set()
    assert wait_terminal(app, first.id).status is GenerationStatus.SUCCEEDED
    assert app.get_conversation(conversation.id).queued_messages[0].id == queued.id
    app.delete_queued_message(queued.id)
    assert app.get_conversation(conversation.id).queued_messages == ()
    app.shutdown()


def test_profile_revision_change_blocks_waiting_message_without_leaking_token(tmp_path):
    release = threading.Event()

    def active_answer():
        release.wait(1)
        return "Активный ответ"

    app, gateway, _ = make_app(tmp_path, [active_answer, "Не должен запускаться"])
    profile = add_profile(app)
    conversation = app.create_conversation(profile.id)
    first = app.send_message(conversation.id, "Активный")
    deadline = time.monotonic() + 1
    while (
        app.get_generation(first.id).status is GenerationStatus.QUEUED
        and time.monotonic() < deadline
    ):
        time.sleep(0.005)
    queued = app.send_message(conversation.id, "Используй исходный профиль")

    app.update_profile(
        profile.id,
        ProfileDraft(
            display_name="Giga PROD",
            provider_format=ProfileFormat.GIGACHAT,
            base_url="https://gateway.bank.local/v1",
            token="rotated-secret-token",
            model_id="risk-model",
        ),
    )
    release.set()
    assert wait_terminal(app, first.id).status is GenerationStatus.SUCCEEDED

    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        blocked = app.get_conversation(conversation.id).queued_messages[0]
        if blocked.status == "blocked":
            break
        time.sleep(0.005)

    assert blocked.id == queued.id
    assert blocked.error_code == "profile_changed"
    assert len(gateway.calls) == 1
    public = str(blocked.to_public_dict())
    assert "rotated-secret-token" not in public
    assert "secret-token" not in public
    app.shutdown()


def test_graceful_shutdown_interrupts_active_turn_and_keeps_follow_up_paused(
    tmp_path,
):
    release = threading.Event()

    def active_answer():
        release.wait(1)
        return "Поздний ответ"

    app, _, _ = make_app(tmp_path, [active_answer])
    profile = add_profile(app)
    conversation = app.create_conversation(profile.id)
    first = app.send_message(conversation.id, "Активный")
    deadline = time.monotonic() + 1
    while (
        app.get_generation(first.id).status is GenerationStatus.QUEUED
        and time.monotonic() < deadline
    ):
        time.sleep(0.005)
    app.send_message(conversation.id, "После рестарта")

    app.shutdown()
    release.set()

    assert app.get_generation(first.id).status is GenerationStatus.INTERRUPTED
    restarted_gateway = QueueGateway(["Не должен запускаться автоматически"])
    restarted = ChatApplication(
        EnvProfileCatalog(tmp_path / ".env"),
        SQLiteChatMemory(tmp_path / "chat.db"),
        QueueFactory(restarted_gateway),
    )
    time.sleep(0.03)
    view = restarted.get_conversation(conversation.id)
    assert view.active_generation.status is GenerationStatus.INTERRUPTED
    assert [item.content for item in view.queued_messages] == ["После рестарта"]
    assert restarted_gateway.calls == []
    restarted.shutdown()
