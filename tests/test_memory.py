from __future__ import annotations

import stat
import sqlite3

import pytest

from datalab_chat.memory import (
    GenerationStatus,
    MemoryConflict,
    MemoryNotFound,
    MemoryStorageError,
    MemoryValidationError,
    QueuedMessageStatus,
    SQLiteChatMemory,
)
from datalab_chat.profiles import ModelSnapshot, ProfileFormat


SNAPSHOT = ModelSnapshot(
    display_name="Giga PROD",
    provider_format=ProfileFormat.GIGACHAT,
    model_id="risk-model",
)


def complete(memory: SQLiteChatMemory, generation_id: str, content: str):
    memory.mark_generation_running(generation_id, attempt=1)
    return memory.complete_generation(generation_id, content, SNAPSHOT)


def test_conversation_survives_restart_with_model_snapshot(tmp_path):
    database = tmp_path / "chat.db"
    memory = SQLiteChatMemory(database)
    conversation = memory.create_conversation("profile-a")

    generation = memory.begin_user_generation(
        conversation.id,
        "Объясни кредитный риск",
        "profile-a",
    )
    answer = complete(memory, generation.id, "Кредитный риск — это вероятность потерь.")

    reloaded = SQLiteChatMemory(database)
    view = reloaded.get_conversation(conversation.id)
    assert view.title == "Объясни кредитный риск"
    assert [message.role for message in view.messages] == ["user", "assistant"]
    assert view.messages[1].content == "Кредитный риск — это вероятность потерь."
    assert view.messages[1].model_snapshot == SNAPSHOT
    assert view.active_generation is None
    assert answer.model_snapshot == SNAPSHOT
    assert stat.S_IMODE(database.stat().st_mode) == 0o600


def test_automatic_title_is_derived_once_from_the_first_message(tmp_path):
    memory = SQLiteChatMemory(tmp_path / "chat.db")
    conversation = memory.create_conversation("profile-a")
    first = memory.begin_user_generation(conversation.id, "Первый вопрос", "profile-a")
    complete(memory, first.id, "Первый ответ")

    memory.begin_user_generation(conversation.id, "Второй вопрос", "profile-a")

    assert memory.get_conversation(conversation.id).title == "Первый вопрос"


def test_newer_database_version_is_rejected_without_schema_or_wal_mutation(tmp_path):
    database = tmp_path / "chat.db"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version = 999")
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"

    with pytest.raises(MemoryStorageError, match="Версия локальной базы данных"):
        SQLiteChatMemory(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 999
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    assert tables == []


@pytest.mark.parametrize("legacy_version", [0, 1])
def test_legacy_database_is_migrated_without_losing_history(
    tmp_path,
    legacy_version,
):
    database = tmp_path / "chat.db"
    memory = SQLiteChatMemory(database)
    conversation = memory.create_conversation("profile-a")
    generation = memory.begin_user_generation(
        conversation.id, "Старый вопрос", "profile-a"
    )
    complete(memory, generation.id, "Старый ответ")

    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE queued_messages")
        connection.execute("ALTER TABLE generations RENAME TO generations_v2")
        connection.execute(
            """
            CREATE TABLE generations (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL
                    REFERENCES conversations(id) ON DELETE CASCADE,
                prompt_message_id TEXT NOT NULL REFERENCES messages(id),
                profile_id TEXT NOT NULL,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                error_code TEXT,
                error_message TEXT,
                response_message_id TEXT REFERENCES messages(id),
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO generations (
                id, conversation_id, prompt_message_id, profile_id, status,
                attempts, error_code, error_message, response_message_id,
                cancel_requested, created_at, updated_at
            )
            SELECT
                id, conversation_id, prompt_message_id, profile_id, status,
                attempts, error_code, error_message, response_message_id,
                cancel_requested, created_at, updated_at
            FROM generations_v2
            """
        )
        connection.execute("DROP TABLE generations_v2")
        connection.execute(f"PRAGMA user_version = {legacy_version}")

    migrated = SQLiteChatMemory(database)

    assert migrated.get_generation(generation.id).profile_revision is None
    assert [
        item.content for item in migrated.get_conversation(conversation.id).messages
    ] == [
        "Старый вопрос",
        "Старый ответ",
    ]
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        queue_table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'queued_messages'"
        ).fetchone()
        generation_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(generations)")
        }
    assert queue_table == ("queued_messages",)
    assert "profile_revision" in generation_columns


def test_edit_and_regenerate_create_navigable_branches(tmp_path):
    memory = SQLiteChatMemory(tmp_path / "chat.db")
    conversation = memory.create_conversation("profile-a")
    first = memory.begin_user_generation(conversation.id, "Первый вопрос", "profile-a")
    first_answer = complete(memory, first.id, "Первый ответ")
    follow_up = memory.begin_user_generation(conversation.id, "Уточнение", "profile-a")
    complete(memory, follow_up.id, "Ответ на уточнение")

    edited = memory.begin_edit_generation(
        first.prompt_message_id, "Изменённый вопрос", "profile-a"
    )
    edited_answer = complete(memory, edited.id, "Ответ на изменение")

    active = memory.get_conversation(conversation.id)
    assert [message.content for message in active.messages] == [
        "Изменённый вопрос",
        "Ответ на изменение",
    ]
    assert active.messages[0].variant_index == 2
    assert active.messages[0].variant_count == 2
    assert active.messages[0].variant_ids == (
        first.prompt_message_id,
        edited.prompt_message_id,
    )

    memory.select_variant(conversation.id, first.prompt_message_id)
    original = memory.get_conversation(conversation.id)
    assert [message.content for message in original.messages] == [
        "Первый вопрос",
        "Первый ответ",
        "Уточнение",
        "Ответ на уточнение",
    ]

    memory.select_variant(conversation.id, edited.prompt_message_id)
    regenerated = memory.begin_regeneration(edited_answer.id, "profile-a")
    second_answer = complete(memory, regenerated.id, "Альтернативный ответ")
    alternative = memory.get_conversation(conversation.id)
    assert alternative.messages[-1].id == second_answer.id
    assert alternative.messages[-1].variant_index == 2
    assert alternative.messages[-1].variant_count == 2
    assert alternative.messages[-1].variant_ids == (edited_answer.id, second_answer.id)

    memory.select_variant(conversation.id, edited_answer.id)
    selected_first_answer = memory.get_conversation(conversation.id)
    assert selected_first_answer.messages[-1].content == "Ответ на изменение"
    assert first_answer.id != edited_answer.id


def test_failed_generation_keeps_prompt_and_can_be_retried(tmp_path):
    memory = SQLiteChatMemory(tmp_path / "chat.db")
    conversation = memory.create_conversation("profile-a")
    generation = memory.begin_user_generation(conversation.id, "Вопрос", "profile-a")
    memory.mark_generation_running(generation.id, attempt=1)

    memory.fail_generation(
        generation.id,
        error_code="provider_unavailable",
        error_message="Модель временно недоступна.",
        attempts=3,
    )

    failed = memory.get_conversation(conversation.id)
    assert [message.content for message in failed.messages] == ["Вопрос"]
    assert failed.active_generation.status is GenerationStatus.FAILED
    assert failed.active_generation.error_code == "provider_unavailable"
    retry = memory.begin_retry_generation(generation.id, "profile-a")
    complete(memory, retry.id, "Готовый ответ")
    assert (
        memory.get_conversation(conversation.id).messages[-1].content == "Готовый ответ"
    )


def test_restart_marks_unfinished_generation_as_interrupted(tmp_path):
    database = tmp_path / "chat.db"
    memory = SQLiteChatMemory(database)
    conversation = memory.create_conversation("profile-a")
    generation = memory.begin_user_generation(
        conversation.id, "Долгий вопрос", "profile-a"
    )
    memory.mark_generation_running(generation.id, attempt=2)

    restarted = SQLiteChatMemory(database)
    assert restarted.recover_interrupted_generations() == 1
    recovered = restarted.get_generation(generation.id)
    assert recovered.status is GenerationStatus.INTERRUPTED
    assert recovered.error_code == "process_interrupted"
    assert restarted.recover_interrupted_generations() == 0


def test_conversation_list_supports_search_rename_and_delete(tmp_path):
    memory = SQLiteChatMemory(tmp_path / "chat.db")
    alpha = memory.create_conversation()
    beta = memory.create_conversation()
    memory.rename_conversation(alpha.id, "Анализ портфеля")
    memory.rename_conversation(beta.id, "Проверка лимитов")
    memory.set_active_profile(alpha.id, "profile-b")

    assert [item.title for item in memory.list_conversations("портф")] == [
        "Анализ портфеля"
    ]
    assert memory.get_conversation(alpha.id).active_profile_id == "profile-b"
    memory.delete_conversation(beta.id)
    with pytest.raises(MemoryNotFound):
        memory.get_conversation(beta.id)


def test_only_one_generation_can_be_active_in_a_conversation(tmp_path):
    memory = SQLiteChatMemory(tmp_path / "chat.db")
    conversation = memory.create_conversation("profile-a")
    generation = memory.begin_user_generation(conversation.id, "Первый", "profile-a")

    with pytest.raises(MemoryConflict):
        memory.begin_user_generation(conversation.id, "Второй", "profile-a")

    memory.cancel_generation(generation.id)
    next_generation = memory.begin_user_generation(
        conversation.id, "Второй", "profile-a"
    )
    assert next_generation.status is GenerationStatus.QUEUED


def test_messages_submitted_during_generation_are_durable_fifo(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "datalab_chat.memory._now",
        lambda: "2026-07-29T12:00:00.000000+00:00",
    )
    database = tmp_path / "chat.db"
    memory = SQLiteChatMemory(database)
    conversation = memory.create_conversation("profile-a")

    first = memory.submit_user_message(conversation.id, "Первый", "profile-a", SNAPSHOT)
    second = memory.submit_user_message(
        conversation.id, "Второй", "profile-b", SNAPSHOT
    )
    third = memory.submit_user_message(conversation.id, "Третий", "profile-a", SNAPSHOT)

    assert first.status is GenerationStatus.QUEUED
    assert [second.content, third.content] == ["Второй", "Третий"]

    restarted = SQLiteChatMemory(database)
    waiting = restarted.get_conversation(conversation.id)
    assert [item.content for item in waiting.queued_messages] == ["Второй", "Третий"]
    assert [item.profile_id for item in waiting.queued_messages] == [
        "profile-b",
        "profile-a",
    ]
    assert all(
        item.status is QueuedMessageStatus.WAITING for item in waiting.queued_messages
    )
    assert all(item.model_snapshot == SNAPSHOT for item in waiting.queued_messages)

    complete(restarted, first.id, "Ответ один")
    next_generation = restarted.activate_next_queued_message(conversation.id)
    assert next_generation is not None
    assert restarted.context_for_generation(next_generation.id) == [
        {"role": "user", "content": "Первый"},
        {"role": "assistant", "content": "Ответ один"},
        {"role": "user", "content": "Второй"},
    ]
    complete(restarted, next_generation.id, "Ответ два")

    last_generation = restarted.activate_next_queued_message(conversation.id)
    assert last_generation is not None
    complete(restarted, last_generation.id, "Ответ три")
    final = restarted.get_conversation(conversation.id)
    assert [message.content for message in final.messages] == [
        "Первый",
        "Ответ один",
        "Второй",
        "Ответ два",
        "Третий",
        "Ответ три",
    ]
    assert final.queued_messages == ()


def test_queue_activation_preserves_the_latest_profile_selection(tmp_path):
    memory = SQLiteChatMemory(tmp_path / "chat.db")
    conversation = memory.create_conversation("profile-a")
    first = memory.submit_user_message(conversation.id, "Первый", "profile-a", SNAPSHOT)
    memory.submit_user_message(conversation.id, "Второй", "profile-b", SNAPSHOT)
    memory.submit_user_message(conversation.id, "Третий", "profile-c", SNAPSHOT)
    assert memory.get_conversation(conversation.id).active_profile_id == "profile-c"

    complete(memory, first.id, "Ответ один")
    activated = memory.activate_next_queued_message(conversation.id)

    assert activated is not None
    assert activated.profile_id == "profile-b"
    after_activation = memory.get_conversation(conversation.id)
    assert after_activation.active_profile_id == "profile-c"
    assert [item.profile_id for item in after_activation.queued_messages] == [
        "profile-c"
    ]

    memory.submit_user_message(conversation.id, "Четвёртый", "profile-d", SNAPSHOT)
    with_new_message = memory.get_conversation(conversation.id)
    assert with_new_message.active_profile_id == "profile-d"
    assert [item.profile_id for item in with_new_message.queued_messages] == [
        "profile-c",
        "profile-d",
    ]


def test_queued_message_can_be_removed_without_cancelling_active_generation(tmp_path):
    memory = SQLiteChatMemory(tmp_path / "chat.db")
    conversation = memory.create_conversation("profile-a")
    active = memory.submit_user_message(
        conversation.id, "Активный", "profile-a", SNAPSHOT
    )
    queued = memory.submit_user_message(
        conversation.id, "Лишний", "profile-a", SNAPSHOT
    )

    memory.delete_queued_message(queued.id)

    view = memory.get_conversation(conversation.id)
    assert view.queued_messages == ()
    assert view.active_generation.id == active.id


def test_message_operations_reject_wrong_roles_and_empty_content(tmp_path):
    memory = SQLiteChatMemory(tmp_path / "chat.db")
    conversation = memory.create_conversation("profile-a")
    generation = memory.begin_user_generation(conversation.id, "Вопрос", "profile-a")
    answer = complete(memory, generation.id, "Ответ")

    with pytest.raises(MemoryValidationError):
        memory.begin_user_generation(conversation.id, "   ", "profile-a")
    with pytest.raises(MemoryValidationError):
        memory.begin_edit_generation(answer.id, "Нельзя", "profile-a")
    with pytest.raises(MemoryValidationError):
        memory.begin_regeneration(generation.prompt_message_id, "profile-a")
    with pytest.raises(MemoryNotFound):
        memory.select_variant(conversation.id, "missing")
