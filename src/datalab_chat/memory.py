from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Iterator, Sequence

from datalab_chat.profiles import ModelSnapshot


class MemoryError(Exception):
    """Base error exposed by the conversation-memory interface."""


class MemoryValidationError(MemoryError):
    """The requested conversation operation is invalid."""


class MemoryNotFound(MemoryError):
    """A requested conversation, message or generation does not exist."""


class MemoryConflict(MemoryError):
    """The requested operation conflicts with the current conversation state."""


class MemoryStorageError(MemoryError):
    """SQLite could not preserve the requested operation safely."""


class GenerationStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    RETRYING = "retrying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class QueuedMessageStatus(StrEnum):
    WAITING = "waiting"
    BLOCKED = "blocked"


_PENDING_STATUSES = (
    GenerationStatus.QUEUED.value,
    GenerationStatus.RUNNING.value,
    GenerationStatus.RETRYING.value,
)
_RECOVERY_INTERRUPTED_STATUSES = (
    GenerationStatus.RUNNING.value,
    GenerationStatus.RETRYING.value,
)
_MAX_QUEUED_MESSAGES = 100


@dataclass(frozen=True, slots=True)
class ConversationSummary:
    id: str
    title: str
    active_profile_id: str | None
    created_at: str
    updated_at: str

    def to_public_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "title": self.title,
            "active_profile_id": self.active_profile_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class MessageView:
    id: str
    parent_id: str | None
    role: str
    content: str
    model_snapshot: ModelSnapshot | None
    created_at: str
    variant_index: int
    variant_count: int
    variant_ids: tuple[str, ...]

    def to_public_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "parent_id": self.parent_id,
            "role": self.role,
            "content": self.content,
            "model_snapshot": (
                self.model_snapshot.to_public_dict() if self.model_snapshot else None
            ),
            "created_at": self.created_at,
            "variant_index": self.variant_index,
            "variant_count": self.variant_count,
            "variant_ids": list(self.variant_ids),
        }


@dataclass(frozen=True, slots=True)
class GenerationView:
    id: str
    conversation_id: str
    prompt_message_id: str
    profile_id: str
    profile_revision: str | None
    status: GenerationStatus
    attempts: int
    error_code: str | None
    error_message: str | None
    response_message_id: str | None
    cancel_requested: bool
    created_at: str
    updated_at: str

    def to_public_dict(self) -> dict[str, object]:
        return {
            "kind": "generation",
            "id": self.id,
            "conversation_id": self.conversation_id,
            "prompt_message_id": self.prompt_message_id,
            "profile_id": self.profile_id,
            "status": self.status.value,
            "attempts": self.attempts,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "response_message_id": self.response_message_id,
            "cancel_requested": self.cancel_requested,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class QueuedMessageView:
    id: str
    conversation_id: str
    content: str
    profile_id: str
    profile_revision: str | None
    model_snapshot: ModelSnapshot | None
    status: QueuedMessageStatus
    error_code: str | None
    error_message: str | None
    created_at: str
    updated_at: str

    def to_public_dict(self) -> dict[str, object]:
        return {
            "kind": "queued_message",
            "id": self.id,
            "conversation_id": self.conversation_id,
            "content": self.content,
            "profile_id": self.profile_id,
            "model_snapshot": (
                self.model_snapshot.to_public_dict() if self.model_snapshot else None
            ),
            "status": self.status.value,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class ConversationView:
    id: str
    title: str
    active_profile_id: str | None
    created_at: str
    updated_at: str
    messages: tuple[MessageView, ...]
    active_generation: GenerationView | None
    queued_messages: tuple[QueuedMessageView, ...]

    def to_public_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "title": self.title,
            "active_profile_id": self.active_profile_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "messages": [message.to_public_dict() for message in self.messages],
            "active_generation": (
                self.active_generation.to_public_dict()
                if self.active_generation
                else None
            ),
            "queued_messages": [item.to_public_dict() for item in self.queued_messages],
        }


class SQLiteChatMemory:
    """Persistent branching conversations behind transactional user operations."""

    _SCHEMA_VERSION = 2

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._schema_lock = threading.RLock()
        self._initialize()

    def create_conversation(self, profile_id: str | None = None) -> ConversationSummary:
        conversation_id = uuid.uuid4().hex
        timestamp = _now()
        with self._write() as connection:
            connection.execute(
                """
                INSERT INTO conversations (
                    id, title, title_is_auto, active_leaf_id, active_profile_id,
                    created_at, updated_at
                ) VALUES (?, ?, 1, NULL, ?, ?, ?)
                """,
                (conversation_id, "Новый чат", profile_id, timestamp, timestamp),
            )
            row = self._conversation_row(connection, conversation_id)
        return self._conversation_summary(row)

    def list_conversations(self, query: str | None = None) -> list[ConversationSummary]:
        search = (query or "").strip()
        if len(search) > 200:
            raise MemoryValidationError("Поисковый запрос слишком длинный.")
        with self._read() as connection:
            if search:
                rows = connection.execute(
                    """
                    SELECT * FROM conversations
                    WHERE title LIKE ? ESCAPE '\\'
                    ORDER BY updated_at DESC, id DESC
                    """,
                    (f"%{_escape_like(search)}%",),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM conversations ORDER BY updated_at DESC, id DESC"
                ).fetchall()
        return [self._conversation_summary(row) for row in rows]

    def get_conversation(self, conversation_id: str) -> ConversationView:
        with self._read() as connection:
            row = self._conversation_row(connection, conversation_id)
            path_rows = self._active_path_rows(connection, row["active_leaf_id"])
            messages = tuple(self._message_view(connection, item) for item in path_rows)
            active_generation = self._active_generation_for_leaf(
                connection,
                conversation_id,
                row["active_leaf_id"],
            )
            queued_messages = tuple(
                self._queued_message_view(item)
                for item in connection.execute(
                    """
                    SELECT * FROM queued_messages
                    WHERE conversation_id = ?
                    ORDER BY ordinal
                    """,
                    (conversation_id,),
                ).fetchall()
            )
        return ConversationView(
            id=row["id"],
            title=row["title"],
            active_profile_id=row["active_profile_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            messages=messages,
            active_generation=active_generation,
            queued_messages=queued_messages,
        )

    def rename_conversation(
        self, conversation_id: str, title: str
    ) -> ConversationSummary:
        return self.update_conversation(conversation_id, title=title)

    def set_active_profile(
        self,
        conversation_id: str,
        profile_id: str | None,
    ) -> ConversationSummary:
        return self.update_conversation(
            conversation_id,
            profile_id=profile_id,
            set_profile=True,
        )

    def update_conversation(
        self,
        conversation_id: str,
        *,
        title: str | None = None,
        profile_id: str | None = None,
        set_profile: bool = False,
    ) -> ConversationSummary:
        if title is None and not set_profile:
            raise MemoryValidationError("Не указаны изменения диалога.")
        clean_title = None
        if title is not None:
            clean_title = " ".join(title.split())
            if not clean_title or len(clean_title) > 120:
                raise MemoryValidationError(
                    "Название диалога должно содержать от 1 до 120 символов."
                )
        timestamp = _now()
        updates: list[str] = []
        values: list[object] = []
        if clean_title is not None:
            updates.extend(["title = ?", "title_is_auto = 0"])
            values.append(clean_title)
        if set_profile:
            updates.append("active_profile_id = ?")
            values.append(profile_id)
        updates.append("updated_at = ?")
        values.extend([timestamp, conversation_id])

        with self._write() as connection:
            self._conversation_row(connection, conversation_id)
            connection.execute(
                f"UPDATE conversations SET {', '.join(updates)} WHERE id = ?",
                values,
            )
            row = self._conversation_row(connection, conversation_id)
        return self._conversation_summary(row)

    def clear_profile_references(self, profile_id: str) -> None:
        with self._write() as connection:
            timestamp = _now()
            connection.execute(
                """
                UPDATE conversations
                SET active_profile_id = NULL, updated_at = ?
                WHERE active_profile_id = ?
                """,
                (timestamp, profile_id),
            )
            connection.execute(
                """
                UPDATE queued_messages
                SET status = ?, error_code = 'profile_not_found',
                    error_message = ?, updated_at = ?
                WHERE profile_id = ? AND status = ?
                """,
                (
                    QueuedMessageStatus.BLOCKED.value,
                    "Профиль удалён. Уберите сообщение из очереди и отправьте его снова.",
                    timestamp,
                    profile_id,
                    QueuedMessageStatus.WAITING.value,
                ),
            )

    def delete_conversation(self, conversation_id: str) -> None:
        with self._write() as connection:
            self._conversation_row(connection, conversation_id)
            connection.execute(
                "DELETE FROM conversations WHERE id = ?", (conversation_id,)
            )

    def begin_user_generation(
        self,
        conversation_id: str,
        content: str,
        profile_id: str,
        profile_revision: str | None = None,
    ) -> GenerationView:
        clean_content = _validated_content(content)
        _validated_profile_id(profile_id)
        with self._write() as connection:
            conversation = self._conversation_row(connection, conversation_id)
            self._assert_no_pending_generation(connection, conversation_id)
            row = self._begin_user_generation_in_transaction(
                connection,
                conversation,
                clean_content,
                profile_id,
                profile_revision,
            )
        return self._generation_view(row)

    def submit_user_message(
        self,
        conversation_id: str,
        content: str,
        profile_id: str,
        model_snapshot: ModelSnapshot | None = None,
        profile_revision: str | None = None,
    ) -> GenerationView | QueuedMessageView:
        clean_content = _validated_content(content)
        _validated_profile_id(profile_id)
        if model_snapshot is not None and not isinstance(model_snapshot, ModelSnapshot):
            raise MemoryValidationError("Снимок модели очереди имеет неверный формат.")
        with self._write() as connection:
            conversation = self._conversation_row(connection, conversation_id)
            if self._has_pending_generation(
                connection, conversation_id
            ) or self._has_queued_messages(connection, conversation_id):
                count = connection.execute(
                    "SELECT COUNT(*) FROM queued_messages WHERE conversation_id = ?",
                    (conversation_id,),
                ).fetchone()[0]
                if count >= _MAX_QUEUED_MESSAGES:
                    raise MemoryConflict(
                        "В очереди диалога уже слишком много сообщений."
                    )
                queued_id = uuid.uuid4().hex
                timestamp = _now()
                connection.execute(
                    """
                    INSERT INTO queued_messages (
                        id, conversation_id, content, profile_id, profile_revision,
                        model_snapshot, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        queued_id,
                        conversation_id,
                        clean_content,
                        profile_id,
                        profile_revision,
                        (
                            json.dumps(
                                model_snapshot.to_public_dict(), ensure_ascii=False
                            )
                            if model_snapshot
                            else None
                        ),
                        QueuedMessageStatus.WAITING.value,
                        timestamp,
                        timestamp,
                    ),
                )
                connection.execute(
                    """
                    UPDATE conversations
                    SET active_profile_id = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (profile_id, timestamp, conversation_id),
                )
                queued = self._queued_message_row(connection, queued_id)
                return self._queued_message_view(queued)

            generation = self._begin_user_generation_in_transaction(
                connection,
                conversation,
                clean_content,
                profile_id,
                profile_revision,
            )
        return self._generation_view(generation)

    def activate_next_queued_message(
        self,
        conversation_id: str,
        expected_queued_message_id: str | None = None,
    ) -> GenerationView | None:
        with self._write() as connection:
            conversation = self._conversation_row(connection, conversation_id)
            if self._has_pending_generation(connection, conversation_id):
                return None
            active_generation = self._active_generation_for_leaf(
                connection,
                conversation_id,
                conversation["active_leaf_id"],
            )
            if active_generation and active_generation.status in {
                GenerationStatus.FAILED,
                GenerationStatus.INTERRUPTED,
            }:
                return None
            queued = connection.execute(
                """
                SELECT * FROM queued_messages
                WHERE conversation_id = ?
                ORDER BY ordinal
                LIMIT 1
                """,
                (conversation_id,),
            ).fetchone()
            if (
                queued is None
                or queued["status"] != QueuedMessageStatus.WAITING.value
                or (
                    expected_queued_message_id is not None
                    and queued["id"] != expected_queued_message_id
                )
            ):
                return None
            generation = self._begin_user_generation_in_transaction(
                connection,
                conversation,
                queued["content"],
                queued["profile_id"],
                queued["profile_revision"],
                update_active_profile=False,
            )
            connection.execute(
                "DELETE FROM queued_messages WHERE id = ?",
                (queued["id"],),
            )
        return self._generation_view(generation)

    def next_queued_message(self, conversation_id: str) -> QueuedMessageView | None:
        with self._read() as connection:
            self._conversation_row(connection, conversation_id)
            row = connection.execute(
                """
                SELECT * FROM queued_messages
                WHERE conversation_id = ?
                ORDER BY ordinal
                LIMIT 1
                """,
                (conversation_id,),
            ).fetchone()
        return self._queued_message_view(row) if row else None

    def block_queued_message(
        self,
        queued_message_id: str,
        *,
        error_code: str,
        error_message: str,
    ) -> QueuedMessageView:
        with self._write() as connection:
            self._queued_message_row(connection, queued_message_id)
            connection.execute(
                """
                UPDATE queued_messages
                SET status = ?, error_code = ?, error_message = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    QueuedMessageStatus.BLOCKED.value,
                    error_code,
                    error_message,
                    _now(),
                    queued_message_id,
                ),
            )
            row = self._queued_message_row(connection, queued_message_id)
        return self._queued_message_view(row)

    def delete_queued_message(self, queued_message_id: str) -> None:
        with self._write() as connection:
            queued = self._queued_message_row(connection, queued_message_id)
            connection.execute(
                "DELETE FROM queued_messages WHERE id = ?",
                (queued_message_id,),
            )
            connection.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (_now(), queued["conversation_id"]),
            )

    def queued_conversation_ids(self) -> tuple[str, ...]:
        with self._read() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT conversation_id FROM queued_messages
                ORDER BY conversation_id
                """
            ).fetchall()
        return tuple(row["conversation_id"] for row in rows)

    def queued_generations(self) -> tuple[GenerationView, ...]:
        with self._read() as connection:
            rows = connection.execute(
                """
                SELECT * FROM generations
                WHERE status = ?
                ORDER BY created_at, id
                """,
                (GenerationStatus.QUEUED.value,),
            ).fetchall()
        return tuple(self._generation_view(row) for row in rows)

    def begin_edit_generation(
        self,
        message_id: str,
        content: str,
        profile_id: str,
        profile_revision: str | None = None,
    ) -> GenerationView:
        clean_content = _validated_content(content)
        _validated_profile_id(profile_id)
        with self._write() as connection:
            source = self._message_row(connection, message_id)
            if source["role"] != "user":
                raise MemoryValidationError(
                    "Редактировать можно только сообщение пользователя."
                )
            conversation_id = source["conversation_id"]
            self._assert_no_pending_generation(connection, conversation_id)
            edited_id = self._insert_message(
                connection,
                conversation_id=conversation_id,
                parent_id=source["parent_id"],
                role="user",
                content=clean_content,
                model_snapshot=None,
            )
            connection.execute(
                """
                UPDATE conversations
                SET active_leaf_id = ?, active_profile_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (edited_id, profile_id, _now(), conversation_id),
            )
            generation_id = self._insert_generation(
                connection,
                conversation_id,
                edited_id,
                profile_id,
                profile_revision,
            )
            row = self._generation_row(connection, generation_id)
        return self._generation_view(row)

    def begin_regeneration(
        self,
        message_id: str,
        profile_id: str,
        profile_revision: str | None = None,
    ) -> GenerationView:
        _validated_profile_id(profile_id)
        with self._write() as connection:
            source = self._message_row(connection, message_id)
            if source["role"] != "assistant" or not source["parent_id"]:
                raise MemoryValidationError(
                    "Регенерация доступна только для ответа ассистента."
                )
            prompt = self._message_row(connection, source["parent_id"])
            if prompt["role"] != "user":
                raise MemoryValidationError(
                    "Ответ не связан с сообщением пользователя."
                )
            conversation_id = source["conversation_id"]
            self._assert_no_pending_generation(connection, conversation_id)
            connection.execute(
                """
                UPDATE conversations
                SET active_leaf_id = ?, active_profile_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (prompt["id"], profile_id, _now(), conversation_id),
            )
            generation_id = self._insert_generation(
                connection,
                conversation_id,
                prompt["id"],
                profile_id,
                profile_revision,
            )
            row = self._generation_row(connection, generation_id)
        return self._generation_view(row)

    def begin_retry_generation(
        self,
        generation_id: str,
        profile_id: str,
        profile_revision: str | None = None,
    ) -> GenerationView:
        _validated_profile_id(profile_id)
        with self._write() as connection:
            source = self._generation_row(connection, generation_id)
            if source["status"] not in {
                GenerationStatus.FAILED.value,
                GenerationStatus.CANCELLED.value,
                GenerationStatus.INTERRUPTED.value,
            }:
                raise MemoryConflict(
                    "Эту генерацию нельзя повторить в текущем состоянии."
                )
            conversation_id = source["conversation_id"]
            self._assert_no_pending_generation(connection, conversation_id)
            prompt = self._message_row(connection, source["prompt_message_id"])
            connection.execute(
                """
                UPDATE conversations
                SET active_leaf_id = ?, active_profile_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (prompt["id"], profile_id, _now(), conversation_id),
            )
            retry_id = self._insert_generation(
                connection,
                conversation_id,
                prompt["id"],
                profile_id,
                profile_revision,
            )
            row = self._generation_row(connection, retry_id)
        return self._generation_view(row)

    def select_variant(self, conversation_id: str, message_id: str) -> ConversationView:
        with self._write() as connection:
            self._conversation_row(connection, conversation_id)
            self._assert_no_pending_generation(connection, conversation_id)
            message = self._message_row(connection, message_id)
            if message["conversation_id"] != conversation_id:
                raise MemoryNotFound("Сообщение не принадлежит этому диалогу.")
            leaf_id = self._latest_descendant_leaf(connection, message_id)
            connection.execute(
                "UPDATE conversations SET active_leaf_id = ?, updated_at = ? WHERE id = ?",
                (leaf_id, _now(), conversation_id),
            )
        return self.get_conversation(conversation_id)

    def get_generation(self, generation_id: str) -> GenerationView:
        with self._read() as connection:
            row = self._generation_row(connection, generation_id)
        return self._generation_view(row)

    def conversation_for_message(self, message_id: str) -> ConversationSummary:
        with self._read() as connection:
            message = self._message_row(connection, message_id)
            conversation = self._conversation_row(
                connection, message["conversation_id"]
            )
        return self._conversation_summary(conversation)

    def context_for_generation(self, generation_id: str) -> list[dict[str, str]]:
        with self._read() as connection:
            generation = self._generation_row(connection, generation_id)
            rows = self._active_path_rows(connection, generation["prompt_message_id"])
        return [{"role": row["role"], "content": row["content"]} for row in rows]

    def mark_generation_running(
        self, generation_id: str, *, attempt: int
    ) -> GenerationView:
        if attempt < 1:
            raise MemoryValidationError("Номер попытки должен быть положительным.")
        with self._write() as connection:
            row = self._generation_row(connection, generation_id)
            self._assert_pending(row)
            if row["cancel_requested"]:
                raise MemoryConflict("Генерация уже отменена.")
            connection.execute(
                """
                UPDATE generations
                SET status = ?, attempts = ?, error_code = NULL,
                    error_message = NULL, updated_at = ?
                WHERE id = ?
                """,
                (GenerationStatus.RUNNING.value, attempt, _now(), generation_id),
            )
            updated = self._generation_row(connection, generation_id)
        return self._generation_view(updated)

    def mark_generation_retrying(
        self,
        generation_id: str,
        *,
        attempt: int,
        error_code: str,
        error_message: str,
    ) -> GenerationView:
        with self._write() as connection:
            row = self._generation_row(connection, generation_id)
            self._assert_pending(row)
            connection.execute(
                """
                UPDATE generations
                SET status = ?, attempts = ?, error_code = ?, error_message = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    GenerationStatus.RETRYING.value,
                    attempt,
                    error_code,
                    error_message,
                    _now(),
                    generation_id,
                ),
            )
            updated = self._generation_row(connection, generation_id)
        return self._generation_view(updated)

    def complete_generation(
        self,
        generation_id: str,
        content: str,
        model_snapshot: ModelSnapshot,
    ) -> MessageView:
        clean_content = _validated_content(content)
        if not isinstance(model_snapshot, ModelSnapshot):
            raise MemoryValidationError("Снимок модели имеет неверный формат.")
        with self._write() as connection:
            generation = self._generation_row(connection, generation_id)
            self._assert_pending(generation)
            if generation["cancel_requested"]:
                raise MemoryConflict("Отменённая генерация не может сохранить ответ.")
            message_id = self._insert_message(
                connection,
                conversation_id=generation["conversation_id"],
                parent_id=generation["prompt_message_id"],
                role="assistant",
                content=clean_content,
                model_snapshot=model_snapshot,
            )
            timestamp = _now()
            connection.execute(
                """
                UPDATE generations
                SET status = ?, response_message_id = ?, error_code = NULL,
                    error_message = NULL, updated_at = ?
                WHERE id = ?
                """,
                (
                    GenerationStatus.SUCCEEDED.value,
                    message_id,
                    timestamp,
                    generation_id,
                ),
            )
            connection.execute(
                """
                UPDATE conversations
                SET active_leaf_id = CASE WHEN active_leaf_id = ? THEN ? ELSE active_leaf_id END,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    generation["prompt_message_id"],
                    message_id,
                    timestamp,
                    generation["conversation_id"],
                ),
            )
            row = self._message_row(connection, message_id)
            view = self._message_view(connection, row)
        return view

    def fail_generation(
        self,
        generation_id: str,
        *,
        error_code: str,
        error_message: str,
        attempts: int,
    ) -> GenerationView:
        with self._write() as connection:
            row = self._generation_row(connection, generation_id)
            self._assert_pending(row)
            connection.execute(
                """
                UPDATE generations
                SET status = ?, attempts = ?, error_code = ?, error_message = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    GenerationStatus.FAILED.value,
                    attempts,
                    error_code,
                    error_message,
                    _now(),
                    generation_id,
                ),
            )
            updated = self._generation_row(connection, generation_id)
        return self._generation_view(updated)

    def cancel_generation(self, generation_id: str) -> GenerationView:
        with self._write() as connection:
            row = self._generation_row(connection, generation_id)
            if row["status"] in _PENDING_STATUSES:
                connection.execute(
                    """
                    UPDATE generations
                    SET status = ?, cancel_requested = 1, error_code = ?,
                        error_message = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        GenerationStatus.CANCELLED.value,
                        "cancelled",
                        "Генерация отменена пользователем.",
                        _now(),
                        generation_id,
                    ),
                )
            updated = self._generation_row(connection, generation_id)
        return self._generation_view(updated)

    def is_generation_cancelled(self, generation_id: str) -> bool:
        generation = self.get_generation(generation_id)
        return (
            generation.cancel_requested
            or generation.status is GenerationStatus.CANCELLED
        )

    def recover_interrupted_generations(self) -> int:
        with self._write() as connection:
            cursor = connection.execute(
                f"""
                UPDATE generations
                SET status = ?, error_code = ?, error_message = ?, updated_at = ?
                WHERE status IN ({",".join("?" for _ in _RECOVERY_INTERRUPTED_STATUSES)})
                """,
                (
                    GenerationStatus.INTERRUPTED.value,
                    "process_interrupted",
                    "Генерация была прервана перезапуском сервиса.",
                    _now(),
                    *_RECOVERY_INTERRUPTED_STATUSES,
                ),
            )
            return cursor.rowcount

    def interrupt_pending_generations(self) -> int:
        with self._write() as connection:
            cursor = connection.execute(
                f"""
                UPDATE generations
                SET status = ?, error_code = ?, error_message = ?, updated_at = ?
                WHERE status IN ({",".join("?" for _ in _RECOVERY_INTERRUPTED_STATUSES)})
                """,
                (
                    GenerationStatus.INTERRUPTED.value,
                    "process_interrupted",
                    "Генерация была прервана остановкой локального сервиса.",
                    _now(),
                    *_RECOVERY_INTERRUPTED_STATUSES,
                ),
            )
            return cursor.rowcount

    def _initialize(self) -> None:
        with self._schema_lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                connection = self._connect()
                try:
                    version = connection.execute("PRAGMA user_version").fetchone()[0]
                    if version not in (0, 1, self._SCHEMA_VERSION):
                        raise MemoryStorageError(
                            "Версия локальной базы данных не поддерживается."
                        )
                    migration_statements: list[str] = []
                    if version in (0, 1, self._SCHEMA_VERSION):
                        generation_columns = {
                            row["name"]
                            for row in connection.execute(
                                "PRAGMA table_info(generations)"
                            ).fetchall()
                        }
                        if (
                            generation_columns
                            and "profile_revision" not in generation_columns
                        ):
                            migration_statements.append(
                                "ALTER TABLE generations ADD COLUMN profile_revision TEXT;"
                            )
                        queue_columns = {
                            row["name"]
                            for row in connection.execute(
                                "PRAGMA table_info(queued_messages)"
                            ).fetchall()
                        }
                        if queue_columns and "profile_revision" not in queue_columns:
                            migration_statements.append(
                                "ALTER TABLE queued_messages ADD COLUMN profile_revision TEXT;"
                            )
                    migration_sql = "\n".join(migration_statements)
                    connection.execute("PRAGMA journal_mode = WAL")
                    connection.executescript(
                        f"""
                        BEGIN IMMEDIATE;

                        {migration_sql}

                        CREATE TABLE IF NOT EXISTS conversations (
                            id TEXT PRIMARY KEY,
                            title TEXT NOT NULL,
                            title_is_auto INTEGER NOT NULL DEFAULT 1,
                            active_leaf_id TEXT,
                            active_profile_id TEXT,
                            created_at TEXT NOT NULL,
                            updated_at TEXT NOT NULL
                        );

                        CREATE TABLE IF NOT EXISTS messages (
                            id TEXT PRIMARY KEY,
                            conversation_id TEXT NOT NULL
                                REFERENCES conversations(id) ON DELETE CASCADE,
                            parent_id TEXT REFERENCES messages(id),
                            role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                            content TEXT NOT NULL,
                            model_snapshot TEXT,
                            created_at TEXT NOT NULL
                        );

                        CREATE TABLE IF NOT EXISTS generations (
                            id TEXT PRIMARY KEY,
                            conversation_id TEXT NOT NULL
                                REFERENCES conversations(id) ON DELETE CASCADE,
                            prompt_message_id TEXT NOT NULL REFERENCES messages(id),
                            profile_id TEXT NOT NULL,
                            profile_revision TEXT,
                            status TEXT NOT NULL,
                            attempts INTEGER NOT NULL DEFAULT 0,
                            error_code TEXT,
                            error_message TEXT,
                            response_message_id TEXT REFERENCES messages(id),
                            cancel_requested INTEGER NOT NULL DEFAULT 0,
                            created_at TEXT NOT NULL,
                            updated_at TEXT NOT NULL
                        );

                        CREATE TABLE IF NOT EXISTS queued_messages (
                            ordinal INTEGER PRIMARY KEY AUTOINCREMENT,
                            id TEXT NOT NULL UNIQUE,
                            conversation_id TEXT NOT NULL
                                REFERENCES conversations(id) ON DELETE CASCADE,
                            content TEXT NOT NULL,
                            profile_id TEXT NOT NULL,
                            profile_revision TEXT,
                            model_snapshot TEXT,
                            status TEXT NOT NULL
                                CHECK (status IN ('waiting', 'blocked')),
                            error_code TEXT,
                            error_message TEXT,
                            created_at TEXT NOT NULL,
                            updated_at TEXT NOT NULL
                        );

                        CREATE INDEX IF NOT EXISTS idx_messages_conversation_parent
                            ON messages(conversation_id, parent_id, created_at);
                        CREATE INDEX IF NOT EXISTS idx_generations_conversation_status
                            ON generations(conversation_id, status, created_at);
                        CREATE INDEX IF NOT EXISTS idx_queued_messages_conversation
                            ON queued_messages(conversation_id, ordinal);

                        PRAGMA user_version = {self._SCHEMA_VERSION};
                        COMMIT;
                        """
                    )
                finally:
                    if connection.in_transaction:
                        connection.rollback()
                    connection.close()
                self._secure_database_files()
            except MemoryError:
                raise
            except (OSError, sqlite3.Error) as exc:
                raise MemoryStorageError(
                    "Не удалось подготовить локальную базу диалогов."
                ) from exc

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            yield connection
        except MemoryError:
            raise
        except sqlite3.Error as exc:
            raise MemoryStorageError("Не удалось прочитать локальную историю.") from exc
        finally:
            if connection is not None:
                connection.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
            self._secure_database_files()
        except MemoryError:
            if connection is not None:
                connection.rollback()
            raise
        except sqlite3.Error as exc:
            if connection is not None:
                connection.rollback()
            raise MemoryStorageError("Не удалось сохранить локальную историю.") from exc
        finally:
            if connection is not None:
                connection.close()

    def _secure_database_files(self) -> None:
        for candidate in (
            self.path,
            Path(f"{self.path}-wal"),
            Path(f"{self.path}-shm"),
        ):
            try:
                if candidate.exists():
                    os.chmod(candidate, 0o600)
            except OSError as exc:
                raise MemoryStorageError(
                    "Не удалось ограничить права локальной базы."
                ) from exc

    def _conversation_row(
        self,
        connection: sqlite3.Connection,
        conversation_id: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()
        if row is None:
            raise MemoryNotFound("Диалог не найден.")
        return row

    def _message_row(
        self, connection: sqlite3.Connection, message_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM messages WHERE id = ?",
            (message_id,),
        ).fetchone()
        if row is None:
            raise MemoryNotFound("Сообщение не найдено.")
        return row

    def _generation_row(
        self,
        connection: sqlite3.Connection,
        generation_id: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM generations WHERE id = ?",
            (generation_id,),
        ).fetchone()
        if row is None:
            raise MemoryNotFound("Генерация не найдена.")
        return row

    def _queued_message_row(
        self,
        connection: sqlite3.Connection,
        queued_message_id: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM queued_messages WHERE id = ?",
            (queued_message_id,),
        ).fetchone()
        if row is None:
            raise MemoryNotFound("Сообщение в очереди не найдено.")
        return row

    def _conversation_summary(self, row: sqlite3.Row) -> ConversationSummary:
        return ConversationSummary(
            id=row["id"],
            title=row["title"],
            active_profile_id=row["active_profile_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _generation_view(self, row: sqlite3.Row) -> GenerationView:
        return GenerationView(
            id=row["id"],
            conversation_id=row["conversation_id"],
            prompt_message_id=row["prompt_message_id"],
            profile_id=row["profile_id"],
            profile_revision=row["profile_revision"],
            status=GenerationStatus(row["status"]),
            attempts=row["attempts"],
            error_code=row["error_code"],
            error_message=row["error_message"],
            response_message_id=row["response_message_id"],
            cancel_requested=bool(row["cancel_requested"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _queued_message_view(self, row: sqlite3.Row) -> QueuedMessageView:
        try:
            snapshot = (
                ModelSnapshot.from_dict(json.loads(row["model_snapshot"]))
                if row["model_snapshot"]
                else None
            )
            status = QueuedMessageStatus(row["status"])
        except (json.JSONDecodeError, ValueError) as exc:
            raise MemoryStorageError(
                "Сохранённый снимок модели очереди повреждён."
            ) from exc
        return QueuedMessageView(
            id=row["id"],
            conversation_id=row["conversation_id"],
            content=row["content"],
            profile_id=row["profile_id"],
            profile_revision=row["profile_revision"],
            model_snapshot=snapshot,
            status=status,
            error_code=row["error_code"],
            error_message=row["error_message"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _message_view(
        self, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> MessageView:
        siblings = connection.execute(
            """
            SELECT id FROM messages
            WHERE conversation_id = ? AND role = ?
              AND ((parent_id = ?) OR (parent_id IS NULL AND ? IS NULL))
            ORDER BY created_at, id
            """,
            (row["conversation_id"], row["role"], row["parent_id"], row["parent_id"]),
        ).fetchall()
        sibling_ids = [item["id"] for item in siblings]
        try:
            snapshot = (
                ModelSnapshot.from_dict(json.loads(row["model_snapshot"]))
                if row["model_snapshot"]
                else None
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise MemoryStorageError("Сохранённый снимок модели повреждён.") from exc
        return MessageView(
            id=row["id"],
            parent_id=row["parent_id"],
            role=row["role"],
            content=row["content"],
            model_snapshot=snapshot,
            created_at=row["created_at"],
            variant_index=sibling_ids.index(row["id"]) + 1,
            variant_count=len(sibling_ids),
            variant_ids=tuple(sibling_ids),
        )

    def _active_path_rows(
        self,
        connection: sqlite3.Connection,
        leaf_id: str | None,
    ) -> Sequence[sqlite3.Row]:
        if leaf_id is None:
            return []
        return connection.execute(
            """
            WITH RECURSIVE path(id, parent_id, depth) AS (
                SELECT id, parent_id, 0 FROM messages WHERE id = ?
                UNION ALL
                SELECT messages.id, messages.parent_id, path.depth + 1
                FROM messages JOIN path ON messages.id = path.parent_id
            )
            SELECT messages.* FROM messages JOIN path ON messages.id = path.id
            ORDER BY path.depth DESC
            """,
            (leaf_id,),
        ).fetchall()

    def _active_generation_for_leaf(
        self,
        connection: sqlite3.Connection,
        conversation_id: str,
        leaf_id: str | None,
    ) -> GenerationView | None:
        if leaf_id is None:
            return None
        message = connection.execute(
            "SELECT role FROM messages WHERE id = ?",
            (leaf_id,),
        ).fetchone()
        if message is None or message["role"] != "user":
            return None
        row = connection.execute(
            """
            SELECT * FROM generations
            WHERE conversation_id = ? AND prompt_message_id = ?
              AND response_message_id IS NULL
            ORDER BY created_at DESC, id DESC LIMIT 1
            """,
            (conversation_id, leaf_id),
        ).fetchone()
        return self._generation_view(row) if row else None

    def _assert_no_pending_generation(
        self,
        connection: sqlite3.Connection,
        conversation_id: str,
    ) -> None:
        if self._has_pending_generation(connection, conversation_id):
            raise MemoryConflict("В диалоге уже выполняется генерация.")

    def _has_pending_generation(
        self,
        connection: sqlite3.Connection,
        conversation_id: str,
    ) -> bool:
        row = connection.execute(
            f"""
            SELECT id FROM generations
            WHERE conversation_id = ? AND status IN ({",".join("?" for _ in _PENDING_STATUSES)})
            LIMIT 1
            """,
            (conversation_id, *_PENDING_STATUSES),
        ).fetchone()
        return row is not None

    def _has_queued_messages(
        self,
        connection: sqlite3.Connection,
        conversation_id: str,
    ) -> bool:
        row = connection.execute(
            "SELECT 1 FROM queued_messages WHERE conversation_id = ? LIMIT 1",
            (conversation_id,),
        ).fetchone()
        return row is not None

    def _assert_pending(self, row: sqlite3.Row) -> None:
        if row["status"] not in _PENDING_STATUSES:
            raise MemoryConflict("Генерация уже завершена.")

    def _begin_user_generation_in_transaction(
        self,
        connection: sqlite3.Connection,
        conversation: sqlite3.Row,
        content: str,
        profile_id: str,
        profile_revision: str | None,
        *,
        update_active_profile: bool = True,
    ) -> sqlite3.Row:
        conversation_id = conversation["id"]
        message_id = self._insert_message(
            connection,
            conversation_id=conversation_id,
            parent_id=conversation["active_leaf_id"],
            role="user",
            content=content,
            model_snapshot=None,
        )
        title = conversation["title"]
        if conversation["title_is_auto"]:
            title = _automatic_title(content)
        timestamp = _now()
        connection.execute(
            """
            UPDATE conversations
            SET active_leaf_id = ?, active_profile_id = ?, title = ?,
                title_is_auto = 0, updated_at = ?
            WHERE id = ?
            """,
            (
                message_id,
                (
                    profile_id
                    if update_active_profile
                    else conversation["active_profile_id"]
                ),
                title,
                timestamp,
                conversation_id,
            ),
        )
        generation_id = self._insert_generation(
            connection,
            conversation_id,
            message_id,
            profile_id,
            profile_revision,
        )
        return self._generation_row(connection, generation_id)

    def _insert_message(
        self,
        connection: sqlite3.Connection,
        *,
        conversation_id: str,
        parent_id: str | None,
        role: str,
        content: str,
        model_snapshot: ModelSnapshot | None,
    ) -> str:
        message_id = uuid.uuid4().hex
        connection.execute(
            """
            INSERT INTO messages (
                id, conversation_id, parent_id, role, content, model_snapshot, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                conversation_id,
                parent_id,
                role,
                content,
                (
                    json.dumps(model_snapshot.to_public_dict(), ensure_ascii=False)
                    if model_snapshot
                    else None
                ),
                _now(),
            ),
        )
        return message_id

    def _insert_generation(
        self,
        connection: sqlite3.Connection,
        conversation_id: str,
        prompt_message_id: str,
        profile_id: str,
        profile_revision: str | None,
    ) -> str:
        generation_id = uuid.uuid4().hex
        timestamp = _now()
        connection.execute(
            """
            INSERT INTO generations (
                id, conversation_id, prompt_message_id, profile_id,
                profile_revision, status, attempts, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                generation_id,
                conversation_id,
                prompt_message_id,
                profile_id,
                profile_revision,
                GenerationStatus.QUEUED.value,
                timestamp,
                timestamp,
            ),
        )
        return generation_id

    def _latest_descendant_leaf(
        self,
        connection: sqlite3.Connection,
        message_id: str,
    ) -> str:
        row = connection.execute(
            """
            WITH RECURSIVE descendants(id, created_at) AS (
                SELECT id, created_at FROM messages WHERE id = ?
                UNION ALL
                SELECT messages.id, messages.created_at
                FROM messages JOIN descendants ON messages.parent_id = descendants.id
            )
            SELECT descendants.id FROM descendants
            WHERE NOT EXISTS (
                SELECT 1 FROM messages child WHERE child.parent_id = descendants.id
            )
            ORDER BY descendants.created_at DESC, descendants.id DESC
            LIMIT 1
            """,
            (message_id,),
        ).fetchone()
        if row is None:
            raise MemoryNotFound("Ветка сообщения не найдена.")
        return row["id"]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _validated_content(content: str) -> str:
    clean = content.strip()
    if not clean:
        raise MemoryValidationError("Сообщение не может быть пустым.")
    if len(clean) > 200_000:
        raise MemoryValidationError("Сообщение превышает допустимый размер.")
    if "\0" in clean:
        raise MemoryValidationError("Сообщение содержит недопустимый символ.")
    return clean


def _validated_profile_id(profile_id: str) -> None:
    if not profile_id or len(profile_id) > 200:
        raise MemoryValidationError("Не указан профиль модели.")


def _automatic_title(content: str) -> str:
    compact = " ".join(content.split())
    if len(compact) <= 56:
        return compact
    return compact[:53].rstrip() + "…"


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
