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


_PENDING_STATUSES = (
    GenerationStatus.QUEUED.value,
    GenerationStatus.RUNNING.value,
    GenerationStatus.RETRYING.value,
)


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
    model_snapshot: dict[str, str] | None
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
            "model_snapshot": self.model_snapshot,
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
class ConversationView:
    id: str
    title: str
    active_profile_id: str | None
    created_at: str
    updated_at: str
    messages: tuple[MessageView, ...]
    active_generation: GenerationView | None

    def to_public_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "title": self.title,
            "active_profile_id": self.active_profile_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "messages": [message.to_public_dict() for message in self.messages],
            "active_generation": (
                self.active_generation.to_public_dict() if self.active_generation else None
            ),
        }


class SQLiteChatMemory:
    """Persistent branching conversations behind transactional user operations."""

    _SCHEMA_VERSION = 1

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
        return ConversationView(
            id=row["id"],
            title=row["title"],
            active_profile_id=row["active_profile_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            messages=messages,
            active_generation=active_generation,
        )

    def rename_conversation(self, conversation_id: str, title: str) -> ConversationSummary:
        clean_title = " ".join(title.split())
        if not clean_title or len(clean_title) > 120:
            raise MemoryValidationError("Название диалога должно содержать от 1 до 120 символов.")
        timestamp = _now()
        with self._write() as connection:
            self._conversation_row(connection, conversation_id)
            connection.execute(
                """
                UPDATE conversations
                SET title = ?, title_is_auto = 0, updated_at = ?
                WHERE id = ?
                """,
                (clean_title, timestamp, conversation_id),
            )
            row = self._conversation_row(connection, conversation_id)
        return self._conversation_summary(row)

    def set_active_profile(
        self,
        conversation_id: str,
        profile_id: str | None,
    ) -> ConversationSummary:
        timestamp = _now()
        with self._write() as connection:
            self._conversation_row(connection, conversation_id)
            connection.execute(
                "UPDATE conversations SET active_profile_id = ?, updated_at = ? WHERE id = ?",
                (profile_id, timestamp, conversation_id),
            )
            row = self._conversation_row(connection, conversation_id)
        return self._conversation_summary(row)

    def clear_profile_references(self, profile_id: str) -> None:
        with self._write() as connection:
            connection.execute(
                """
                UPDATE conversations
                SET active_profile_id = NULL, updated_at = ?
                WHERE active_profile_id = ?
                """,
                (_now(), profile_id),
            )

    def delete_conversation(self, conversation_id: str) -> None:
        with self._write() as connection:
            self._conversation_row(connection, conversation_id)
            connection.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))

    def begin_user_generation(
        self,
        conversation_id: str,
        content: str,
        profile_id: str,
    ) -> GenerationView:
        clean_content = _validated_content(content)
        _validated_profile_id(profile_id)
        with self._write() as connection:
            conversation = self._conversation_row(connection, conversation_id)
            self._assert_no_pending_generation(connection, conversation_id)
            message_id = self._insert_message(
                connection,
                conversation_id=conversation_id,
                parent_id=conversation["active_leaf_id"],
                role="user",
                content=clean_content,
                model_snapshot=None,
            )
            title = conversation["title"]
            if conversation["title_is_auto"]:
                title = _automatic_title(clean_content)
            timestamp = _now()
            connection.execute(
                """
                UPDATE conversations
                SET active_leaf_id = ?, active_profile_id = ?, title = ?, updated_at = ?
                WHERE id = ?
                """,
                (message_id, profile_id, title, timestamp, conversation_id),
            )
            generation_id = self._insert_generation(
                connection,
                conversation_id,
                message_id,
                profile_id,
            )
            row = self._generation_row(connection, generation_id)
        return self._generation_view(row)

    def begin_edit_generation(
        self,
        message_id: str,
        content: str,
        profile_id: str,
    ) -> GenerationView:
        clean_content = _validated_content(content)
        _validated_profile_id(profile_id)
        with self._write() as connection:
            source = self._message_row(connection, message_id)
            if source["role"] != "user":
                raise MemoryValidationError("Редактировать можно только сообщение пользователя.")
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
            )
            row = self._generation_row(connection, generation_id)
        return self._generation_view(row)

    def begin_regeneration(self, message_id: str, profile_id: str) -> GenerationView:
        _validated_profile_id(profile_id)
        with self._write() as connection:
            source = self._message_row(connection, message_id)
            if source["role"] != "assistant" or not source["parent_id"]:
                raise MemoryValidationError("Регенерация доступна только для ответа ассистента.")
            prompt = self._message_row(connection, source["parent_id"])
            if prompt["role"] != "user":
                raise MemoryValidationError("Ответ не связан с сообщением пользователя.")
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
            )
            row = self._generation_row(connection, generation_id)
        return self._generation_view(row)

    def begin_retry_generation(self, generation_id: str, profile_id: str) -> GenerationView:
        _validated_profile_id(profile_id)
        with self._write() as connection:
            source = self._generation_row(connection, generation_id)
            if source["status"] not in {
                GenerationStatus.FAILED.value,
                GenerationStatus.CANCELLED.value,
                GenerationStatus.INTERRUPTED.value,
            }:
                raise MemoryConflict("Эту генерацию нельзя повторить в текущем состоянии.")
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
            conversation = self._conversation_row(connection, message["conversation_id"])
        return self._conversation_summary(conversation)

    def context_for_generation(self, generation_id: str) -> list[dict[str, str]]:
        with self._read() as connection:
            generation = self._generation_row(connection, generation_id)
            rows = self._active_path_rows(connection, generation["prompt_message_id"])
        return [{"role": row["role"], "content": row["content"]} for row in rows]

    def mark_generation_running(self, generation_id: str, *, attempt: int) -> GenerationView:
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
        model_snapshot: dict[str, str],
    ) -> MessageView:
        clean_content = _validated_content(content)
        clean_snapshot = _validated_snapshot(model_snapshot)
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
                model_snapshot=clean_snapshot,
            )
            timestamp = _now()
            connection.execute(
                """
                UPDATE generations
                SET status = ?, response_message_id = ?, error_code = NULL,
                    error_message = NULL, updated_at = ?
                WHERE id = ?
                """,
                (GenerationStatus.SUCCEEDED.value, message_id, timestamp, generation_id),
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
        return generation.cancel_requested or generation.status is GenerationStatus.CANCELLED

    def recover_interrupted_generations(self) -> int:
        with self._write() as connection:
            cursor = connection.execute(
                f"""
                UPDATE generations
                SET status = ?, error_code = ?, error_message = ?, updated_at = ?
                WHERE status IN ({','.join('?' for _ in _PENDING_STATUSES)})
                """,
                (
                    GenerationStatus.INTERRUPTED.value,
                    "process_interrupted",
                    "Генерация была прервана перезапуском сервиса.",
                    _now(),
                    *_PENDING_STATUSES,
                ),
            )
            return cursor.rowcount

    def _initialize(self) -> None:
        with self._schema_lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                connection = self._connect()
                try:
                    connection.execute("PRAGMA journal_mode = WAL")
                    connection.executescript(
                        """
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
                            status TEXT NOT NULL,
                            attempts INTEGER NOT NULL DEFAULT 0,
                            error_code TEXT,
                            error_message TEXT,
                            response_message_id TEXT REFERENCES messages(id),
                            cancel_requested INTEGER NOT NULL DEFAULT 0,
                            created_at TEXT NOT NULL,
                            updated_at TEXT NOT NULL
                        );

                        CREATE INDEX IF NOT EXISTS idx_messages_conversation_parent
                            ON messages(conversation_id, parent_id, created_at);
                        CREATE INDEX IF NOT EXISTS idx_generations_conversation_status
                            ON generations(conversation_id, status, created_at);
                        """
                    )
                    version = connection.execute("PRAGMA user_version").fetchone()[0]
                    if version not in (0, self._SCHEMA_VERSION):
                        raise MemoryStorageError("Версия локальной базы данных не поддерживается.")
                    connection.execute(f"PRAGMA user_version = {self._SCHEMA_VERSION}")
                finally:
                    connection.close()
                self._secure_database_files()
            except MemoryError:
                raise
            except (OSError, sqlite3.Error) as exc:
                raise MemoryStorageError("Не удалось подготовить локальную базу диалогов.") from exc

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
                raise MemoryStorageError("Не удалось ограничить права локальной базы.") from exc

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

    def _message_row(self, connection: sqlite3.Connection, message_id: str) -> sqlite3.Row:
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
            status=GenerationStatus(row["status"]),
            attempts=row["attempts"],
            error_code=row["error_code"],
            error_message=row["error_message"],
            response_message_id=row["response_message_id"],
            cancel_requested=bool(row["cancel_requested"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _message_view(self, connection: sqlite3.Connection, row: sqlite3.Row) -> MessageView:
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
        snapshot = json.loads(row["model_snapshot"]) if row["model_snapshot"] else None
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
        row = connection.execute(
            f"""
            SELECT id FROM generations
            WHERE conversation_id = ? AND status IN ({','.join('?' for _ in _PENDING_STATUSES)})
            LIMIT 1
            """,
            (conversation_id, *_PENDING_STATUSES),
        ).fetchone()
        if row is not None:
            raise MemoryConflict("В диалоге уже выполняется генерация.")

    def _assert_pending(self, row: sqlite3.Row) -> None:
        if row["status"] not in _PENDING_STATUSES:
            raise MemoryConflict("Генерация уже завершена.")

    def _insert_message(
        self,
        connection: sqlite3.Connection,
        *,
        conversation_id: str,
        parent_id: str | None,
        role: str,
        content: str,
        model_snapshot: dict[str, str] | None,
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
                json.dumps(model_snapshot, ensure_ascii=False) if model_snapshot else None,
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
    ) -> str:
        generation_id = uuid.uuid4().hex
        timestamp = _now()
        connection.execute(
            """
            INSERT INTO generations (
                id, conversation_id, prompt_message_id, profile_id, status,
                attempts, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                generation_id,
                conversation_id,
                prompt_message_id,
                profile_id,
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


def _validated_snapshot(snapshot: dict[str, str]) -> dict[str, str]:
    expected = {"display_name", "format", "model_id"}
    if set(snapshot) != expected or any(not isinstance(value, str) for value in snapshot.values()):
        raise MemoryValidationError("Снимок модели имеет неверный формат.")
    return {key: snapshot[key] for key in ("display_name", "format", "model_id")}


def _automatic_title(content: str) -> str:
    compact = " ".join(content.split())
    if len(compact) <= 56:
        return compact
    return compact[:53].rstrip() + "…"


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
