from __future__ import annotations

import stat

import pytest

from datalab_chat.memory import (
    GenerationStatus,
    MemoryConflict,
    MemoryNotFound,
    MemoryValidationError,
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
