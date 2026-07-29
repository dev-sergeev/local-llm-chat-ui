from __future__ import annotations

import threading
import time

import pytest

from datalab_chat.gateways import GatewayFailure
from datalab_chat.generation import (
    GenerationCapacityError,
    GenerationCoordinator,
    GenerationPolicy,
)
from datalab_chat.memory import GenerationStatus, SQLiteChatMemory
from datalab_chat.profiles import ModelConnection, ProfileFormat


CONNECTION = ModelConnection(
    id="profile-a",
    display_name="Giga PROD",
    provider_format=ProfileFormat.GIGACHAT,
    base_url="https://llm.bank.local",
    token="secret",
    model_id="risk-model",
)


class ScriptedGateway:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls: list[tuple[list[dict[str, str]], float]] = []

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


class FixedFactory:
    def __init__(self, gateway):
        self.gateway = gateway
        self.connections = []

    def create(self, connection):
        self.connections.append(connection)
        return self.gateway


def policy(**overrides):
    values = {
        "total_timeout_seconds": 1.0,
        "max_attempts": 3,
        "base_backoff_seconds": 0.001,
        "max_backoff_seconds": 0.002,
        "jitter_ratio": 0,
        "poll_interval_seconds": 0.005,
    }
    values.update(overrides)
    return GenerationPolicy(**values)


def new_generation(memory):
    conversation = memory.create_conversation(CONNECTION.id)
    generation = memory.begin_user_generation(
        conversation.id,
        "Что такое PD?",
        CONNECTION.id,
    )
    return conversation, generation


def test_successful_generation_saves_answer_and_context(tmp_path):
    memory = SQLiteChatMemory(tmp_path / "chat.db")
    conversation, generation = new_generation(memory)
    gateway = ScriptedGateway(["PD — вероятность дефолта."])
    coordinator = GenerationCoordinator(memory, FixedFactory(gateway), policy=policy())

    coordinator.submit(generation.id, CONNECTION)
    assert coordinator.wait(generation.id, timeout=1)

    result = memory.get_generation(generation.id)
    view = memory.get_conversation(conversation.id)
    assert result.status is GenerationStatus.SUCCEEDED
    assert result.attempts == 1
    assert view.messages[-1].content == "PD — вероятность дефолта."
    assert view.messages[-1].model_snapshot == CONNECTION.snapshot()
    assert gateway.calls[0][0] == [{"role": "user", "content": "Что такое PD?"}]
    assert gateway.calls[0][1] <= 1.0


def test_retryable_failures_use_at_most_three_attempts(tmp_path):
    memory = SQLiteChatMemory(tmp_path / "chat.db")
    _, generation = new_generation(memory)
    gateway = ScriptedGateway(
        [
            GatewayFailure("network", "Сетевая ошибка.", retryable=True),
            GatewayFailure("rate_limited", "Слишком много запросов.", retryable=True),
            "Ответ после повторов",
        ]
    )
    coordinator = GenerationCoordinator(memory, FixedFactory(gateway), policy=policy())

    coordinator.submit(generation.id, CONNECTION)
    assert coordinator.wait(generation.id, timeout=1)

    result = memory.get_generation(generation.id)
    assert result.status is GenerationStatus.SUCCEEDED
    assert result.attempts == 3
    assert len(gateway.calls) == 3
    assert gateway.calls[2][1] <= gateway.calls[0][1]


def test_non_retryable_failure_is_safe_and_immediate(tmp_path):
    memory = SQLiteChatMemory(tmp_path / "chat.db")
    conversation, generation = new_generation(memory)
    gateway = ScriptedGateway(
        [GatewayFailure("authentication", "Проверьте токен.", retryable=False)]
    )
    coordinator = GenerationCoordinator(memory, FixedFactory(gateway), policy=policy())

    coordinator.submit(generation.id, CONNECTION)
    assert coordinator.wait(generation.id, timeout=1)

    result = memory.get_generation(generation.id)
    assert result.status is GenerationStatus.FAILED
    assert result.error_code == "authentication"
    assert result.error_message == "Проверьте токен."
    assert len(gateway.calls) == 1
    assert [
        message.role for message in memory.get_conversation(conversation.id).messages
    ] == ["user"]


def test_total_deadline_discards_a_late_response(tmp_path):
    memory = SQLiteChatMemory(tmp_path / "chat.db")
    conversation, generation = new_generation(memory)

    def late_response():
        time.sleep(0.15)
        return "Слишком поздно"

    gateway = ScriptedGateway([late_response])
    coordinator = GenerationCoordinator(
        memory,
        FixedFactory(gateway),
        policy=policy(total_timeout_seconds=0.03),
    )

    coordinator.submit(generation.id, CONNECTION)
    assert coordinator.wait(generation.id, timeout=1)

    result = memory.get_generation(generation.id)
    assert result.status is GenerationStatus.FAILED
    assert result.error_code == "deadline_exceeded"
    assert [
        message.role for message in memory.get_conversation(conversation.id).messages
    ] == ["user"]
    time.sleep(0.16)
    assert memory.get_generation(generation.id).status is GenerationStatus.FAILED


def test_user_can_cancel_without_waiting_for_provider(tmp_path):
    memory = SQLiteChatMemory(tmp_path / "chat.db")
    conversation, generation = new_generation(memory)
    release = threading.Event()
    gateway = ScriptedGateway([lambda: release.wait(1) or "ignored"])
    coordinator = GenerationCoordinator(memory, FixedFactory(gateway), policy=policy())

    coordinator.submit(generation.id, CONNECTION)
    deadline = time.monotonic() + 0.5
    while memory.get_generation(generation.id).status is GenerationStatus.QUEUED:
        assert time.monotonic() < deadline
        time.sleep(0.005)
    cancelled = coordinator.cancel(generation.id)
    assert cancelled.status is GenerationStatus.CANCELLED
    assert coordinator.wait(generation.id, timeout=1)
    release.set()

    assert memory.get_generation(generation.id).status is GenerationStatus.CANCELLED
    assert len(memory.get_conversation(conversation.id).messages) == 1


def test_unexpected_adapter_exception_does_not_escape_worker(tmp_path):
    memory = SQLiteChatMemory(tmp_path / "chat.db")
    _, generation = new_generation(memory)
    gateway = ScriptedGateway([RuntimeError("secret internals")])
    coordinator = GenerationCoordinator(memory, FixedFactory(gateway), policy=policy())

    coordinator.submit(generation.id, CONNECTION)
    assert coordinator.wait(generation.id, timeout=1)

    result = memory.get_generation(generation.id)
    assert result.status is GenerationStatus.FAILED
    assert result.error_code == "unexpected_provider_error"
    assert "secret internals" not in result.error_message


def test_empty_answer_is_not_retried(tmp_path):
    memory = SQLiteChatMemory(tmp_path / "chat.db")
    _, generation = new_generation(memory)
    gateway = ScriptedGateway(["", "must not be requested"])
    coordinator = GenerationCoordinator(memory, FixedFactory(gateway), policy=policy())

    coordinator.submit(generation.id, CONNECTION)
    assert coordinator.wait(generation.id, timeout=1)

    result = memory.get_generation(generation.id)
    assert result.status is GenerationStatus.FAILED
    assert result.error_code == "empty_response"
    assert result.attempts == 1
    assert len(gateway.calls) == 1


def test_transport_chunks_cross_coordinator_boundary_without_partial_persistence(
    tmp_path,
):
    memory = SQLiteChatMemory(tmp_path / "chat.db")
    conversation, generation = new_generation(memory)

    class ChunkGateway:
        def complete(self, messages, *, timeout_seconds, on_chunk=None):
            on_chunk("PD — ")
            on_chunk("вероятность дефолта")
            return "PD — вероятность дефолта"

    chunks = []
    coordinator = GenerationCoordinator(
        memory, FixedFactory(ChunkGateway()), policy=policy()
    )

    coordinator.submit(generation.id, CONNECTION, on_chunk=chunks.append)
    assert coordinator.wait(generation.id, timeout=1)

    assert chunks == ["PD — ", "вероятность дефолта"]
    assert memory.get_conversation(conversation.id).messages[-1].content == (
        "PD — вероятность дефолта"
    )


def test_stuck_transports_cannot_create_unbounded_generation_threads(tmp_path):
    memory = SQLiteChatMemory(tmp_path / "chat.db")
    release = threading.Event()

    def blocked_answer():
        release.wait(1)
        return "done"

    gateway = ScriptedGateway([blocked_answer] * 4)
    coordinator = GenerationCoordinator(
        memory,
        FixedFactory(gateway),
        policy=policy(max_concurrent_generations=4),
    )
    generation_ids = []
    for _ in range(4):
        _, generation = new_generation(memory)
        generation_ids.append(generation.id)
        coordinator.submit(generation.id, CONNECTION)

    _, overflow = new_generation(memory)
    with pytest.raises(GenerationCapacityError):
        coordinator.submit(overflow.id, CONNECTION)

    generation_threads = [
        thread
        for thread in threading.enumerate()
        if thread.name.startswith("generation-")
    ]
    assert len(generation_threads) <= 4
    release.set()
    for generation_id in generation_ids:
        assert coordinator.wait(generation_id, timeout=1)


def test_thread_start_failure_releases_generation_capacity(tmp_path, monkeypatch):
    memory = SQLiteChatMemory(tmp_path / "chat.db")
    _, generation = new_generation(memory)
    coordinator = GenerationCoordinator(
        memory,
        FixedFactory(ScriptedGateway(["recovered"])),
        policy=policy(max_concurrent_generations=1),
    )
    original_start = threading.Thread.start

    def fail_start(_thread):
        raise RuntimeError("cannot start thread")

    monkeypatch.setattr(threading.Thread, "start", fail_start)
    with pytest.raises(RuntimeError, match="cannot start thread"):
        coordinator.submit(generation.id, CONNECTION)

    monkeypatch.setattr(threading.Thread, "start", original_start)
    coordinator.submit(generation.id, CONNECTION)
    assert coordinator.wait(generation.id, timeout=1)
    assert memory.get_generation(generation.id).status is GenerationStatus.SUCCEEDED


def test_terminal_callback_failure_is_logged_without_secret_details(tmp_path, caplog):
    memory = SQLiteChatMemory(tmp_path / "chat.db")
    _, generation = new_generation(memory)

    def fail_callback(_generation_id):
        raise RuntimeError("secret callback details")

    coordinator = GenerationCoordinator(
        memory,
        FixedFactory(ScriptedGateway(["Ответ сохранён"])),
        policy=policy(),
        on_terminal=fail_callback,
    )

    with caplog.at_level("ERROR", logger="datalab_chat.generation"):
        coordinator.submit(generation.id, CONNECTION)
        assert coordinator.wait(generation.id, timeout=1)

    messages = "\n".join(caplog.messages)
    assert "terminal callback failed" in messages.lower()
    assert "RuntimeError" in messages
    assert "secret callback details" not in messages


def test_terminal_callback_is_retried_after_one_transient_failure(tmp_path):
    memory = SQLiteChatMemory(tmp_path / "chat.db")
    _, generation = new_generation(memory)
    attempts = []

    def flaky_callback(generation_id):
        attempts.append(generation_id)
        if len(attempts) == 1:
            raise RuntimeError("temporary callback failure")

    coordinator = GenerationCoordinator(
        memory,
        FixedFactory(ScriptedGateway(["Ответ сохранён"])),
        policy=policy(),
        on_terminal=flaky_callback,
    )

    coordinator.submit(generation.id, CONNECTION)
    assert coordinator.wait(generation.id, timeout=1)

    assert attempts == [generation.id, generation.id]


def test_shutdown_does_not_turn_registered_queued_work_into_user_cancellation(
    tmp_path,
):
    memory = SQLiteChatMemory(tmp_path / "chat.db")
    _, generation = new_generation(memory)
    factory_entered = threading.Event()
    release_factory = threading.Event()

    class PausedFactory:
        def create(self, _connection):
            factory_entered.set()
            release_factory.wait(1)
            return ScriptedGateway(["Ответ после следующего запуска"])

    coordinator = GenerationCoordinator(memory, PausedFactory(), policy=policy())
    coordinator.submit(generation.id, CONNECTION)
    assert factory_entered.wait(0.5)
    assert memory.get_generation(generation.id).status is GenerationStatus.QUEUED

    coordinator.shutdown(timeout=0.01)
    release_factory.set()
    assert coordinator.wait(generation.id, timeout=1)

    assert memory.get_generation(generation.id).status is GenerationStatus.QUEUED
