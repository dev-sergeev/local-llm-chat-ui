from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from datalab_chat.gateways import (
    BoundedGatewayCaller,
    GatewayCallDeadline,
    GatewayFactory,
    GatewayFailure,
    classify_gateway_exception,
)
from datalab_chat.generation import (
    GenerationCapacityError,
    GenerationCoordinator,
    GenerationPolicy,
)
from datalab_chat.memory import (
    ConversationSummary,
    ConversationView,
    GenerationView,
    MemoryError,
    QueuedMessageStatus,
    QueuedMessageView,
    SQLiteChatMemory,
)
from datalab_chat.profiles import (
    EnvProfileCatalog,
    ModelConnection,
    ModelProfile,
    ProfileDraft,
    ProfileNotFound,
    ProfileValidationError,
)


@dataclass(frozen=True, slots=True)
class ConnectionTestResult:
    ok: bool
    latency_ms: int
    preview: str

    def to_public_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "latency_ms": self.latency_ms,
            "preview": self.preview,
        }


class ChatApplication:
    """Deep user-intent interface shared by HTTP and behavioral tests."""

    def __init__(
        self,
        profiles: EnvProfileCatalog,
        memory: SQLiteChatMemory,
        gateway_factory: GatewayFactory,
        *,
        generation_policy: GenerationPolicy | None = None,
    ):
        self._profiles = profiles
        self._memory = memory
        self._gateway_factory = gateway_factory
        self._lifecycle_lock = threading.RLock()
        self._shutting_down = False
        resolved_policy = generation_policy or GenerationPolicy()
        self._caller = BoundedGatewayCaller(
            max_concurrent_calls=resolved_policy.max_concurrent_generations,
            poll_interval_seconds=resolved_policy.poll_interval_seconds,
        )
        self._generations = GenerationCoordinator(
            memory,
            gateway_factory,
            policy=resolved_policy,
            caller=self._caller,
            on_terminal=self._on_generation_terminal,
        )
        self._memory.recover_interrupted_generations()
        self._resume_queued_generations()
        self._pump_queued_messages()

    def list_profiles(self) -> list[ModelProfile]:
        return self._profiles.list()

    def get_profile(self, profile_id: str) -> ModelProfile:
        return self._profiles.get(profile_id)

    def create_profile(self, draft: ProfileDraft) -> ModelProfile:
        return self._profiles.create(draft)

    def update_profile(self, profile_id: str, draft: ProfileDraft) -> ModelProfile:
        with self._lifecycle_lock:
            return self._profiles.update(profile_id, draft)

    def delete_profile(self, profile_id: str) -> None:
        with self._lifecycle_lock:
            self._profiles.delete(profile_id)
            try:
                self._memory.clear_profile_references(profile_id)
            except MemoryError:
                # The secret is already removed. Stale non-secret IDs are harmless and
                # are rejected on the next generation instead of restoring a secret.
                pass

    def test_profile(
        self,
        profile_id: str,
        *,
        timeout_seconds: float = 30.0,
    ) -> ConnectionTestResult:
        return self._test_connection(
            self._profiles.resolve(profile_id),
            timeout_seconds=timeout_seconds,
        )

    def test_profile_draft(
        self,
        draft: ProfileDraft,
        *,
        profile_id: str | None = None,
        timeout_seconds: float = 30.0,
    ) -> ConnectionTestResult:
        connection = self._profiles.resolve_draft(draft, profile_id=profile_id)
        return self._test_connection(connection, timeout_seconds=timeout_seconds)

    def _test_connection(
        self,
        connection: ModelConnection,
        *,
        timeout_seconds: float,
    ) -> ConnectionTestResult:
        if timeout_seconds <= 0 or timeout_seconds > 60:
            raise ProfileValidationError(
                "Тайм-аут проверки должен быть от 1 до 60 секунд."
            )
        started = time.monotonic()
        try:
            gateway = self._gateway_factory.create(connection)
            answer = self._caller.call(
                gateway,
                [{"role": "user", "content": "Ответь одним словом: OK"}],
                timeout_seconds=timeout_seconds,
            )
        except GatewayCallDeadline:
            raise GatewayFailure(
                "request_timeout",
                "Проверка подключения не завершилась вовремя.",
                retryable=True,
            ) from None
        except GatewayFailure:
            raise
        except Exception as exc:
            raise classify_gateway_exception(exc) from None
        clean_answer = answer.strip()
        if not clean_answer:
            raise GatewayFailure(
                "empty_response",
                "Модель вернула пустой ответ.",
                retryable=False,
            )
        latency_ms = max(0, round((time.monotonic() - started) * 1000))
        return ConnectionTestResult(
            ok=True,
            latency_ms=latency_ms,
            preview=clean_answer[:200],
        )

    def list_conversations(self, query: str | None = None) -> list[ConversationSummary]:
        return self._memory.list_conversations(query)

    def create_conversation(self, profile_id: str | None = None) -> ConversationSummary:
        selected_profile = profile_id
        if selected_profile is not None:
            self._profiles.get(selected_profile)
        else:
            profiles = self._profiles.list()
            selected_profile = profiles[0].id if profiles else None
        return self._memory.create_conversation(selected_profile)

    def get_conversation(self, conversation_id: str) -> ConversationView:
        return self._memory.get_conversation(conversation_id)

    def rename_conversation(
        self, conversation_id: str, title: str
    ) -> ConversationSummary:
        return self.update_conversation(conversation_id, title=title)

    def select_profile(
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
        if set_profile and profile_id is not None:
            self._profiles.get(profile_id)
        return self._memory.update_conversation(
            conversation_id,
            title=title,
            profile_id=profile_id,
            set_profile=set_profile,
        )

    def delete_conversation(self, conversation_id: str) -> None:
        self._memory.delete_conversation(conversation_id)

    def send_message(
        self,
        conversation_id: str,
        content: str,
        profile_id: str | None = None,
    ) -> GenerationView | QueuedMessageView:
        with self._lifecycle_lock:
            connection = self._connection_for_conversation(conversation_id, profile_id)
            result = self._memory.submit_user_message(
                conversation_id,
                content,
                connection.id,
                connection.snapshot(),
                connection.revision,
            )
            if isinstance(result, QueuedMessageView):
                return result
            return self._submit(result, connection)

    def edit_message(
        self,
        message_id: str,
        content: str,
        profile_id: str | None = None,
    ) -> GenerationView:
        with self._lifecycle_lock:
            conversation = self._memory.conversation_for_message(message_id)
            connection = self._connection_from_selection(conversation, profile_id)
            generation = self._memory.begin_edit_generation(
                message_id,
                content,
                connection.id,
                connection.revision,
            )
            return self._submit(generation, connection)

    def regenerate(
        self,
        message_id: str,
        profile_id: str | None = None,
    ) -> GenerationView:
        with self._lifecycle_lock:
            conversation = self._memory.conversation_for_message(message_id)
            connection = self._connection_from_selection(conversation, profile_id)
            generation = self._memory.begin_regeneration(
                message_id,
                connection.id,
                connection.revision,
            )
            return self._submit(generation, connection)

    def retry_generation(
        self,
        generation_id: str,
        profile_id: str | None = None,
    ) -> GenerationView:
        with self._lifecycle_lock:
            previous = self._memory.get_generation(generation_id)
            conversation = self._memory.get_conversation(previous.conversation_id)
            connection = self._connection_from_selection(conversation, profile_id)
            generation = self._memory.begin_retry_generation(
                generation_id,
                connection.id,
                connection.revision,
            )
            return self._submit(generation, connection)

    def select_variant(self, conversation_id: str, message_id: str) -> ConversationView:
        return self._memory.select_variant(conversation_id, message_id)

    def get_generation(self, generation_id: str) -> GenerationView:
        return self._memory.get_generation(generation_id)

    def cancel_generation(self, generation_id: str) -> GenerationView:
        with self._lifecycle_lock:
            return self._generations.cancel(generation_id)

    def delete_queued_message(self, queued_message_id: str) -> None:
        with self._lifecycle_lock:
            self._memory.delete_queued_message(queued_message_id)
            self._pump_queued_messages()

    def shutdown(self) -> None:
        with self._lifecycle_lock:
            self._shutting_down = True
        self._generations.shutdown()

    def _on_generation_terminal(self, generation_id: str) -> None:
        with self._lifecycle_lock:
            if self._shutting_down:
                return
            try:
                conversation_id = self._memory.get_generation(
                    generation_id
                ).conversation_id
            except MemoryError:
                conversation_id = None
            self._resume_queued_generations()
            self._pump_queued_messages(conversation_id)

    def _resume_queued_generations(self) -> None:
        with self._lifecycle_lock:
            if self._shutting_down:
                return
            for generation in self._memory.queued_generations():
                if self._generations.is_registered(generation.id):
                    continue
                if not self._generations.has_capacity():
                    return
                try:
                    connection = self._profiles.resolve(generation.profile_id)
                except ProfileNotFound:
                    self._memory.fail_generation(
                        generation.id,
                        error_code="profile_not_found",
                        error_message=(
                            "Профиль генерации удалён. Выберите модель и повторите запрос."
                        ),
                        attempts=0,
                    )
                    continue
                if (
                    generation.profile_revision is not None
                    and generation.profile_revision != connection.revision
                ):
                    self._memory.fail_generation(
                        generation.id,
                        error_code="profile_changed",
                        error_message=(
                            "Профиль модели изменён после отправки. Повторите запрос явно."
                        ),
                        attempts=0,
                    )
                    continue
                self._submit(generation, connection)

    def _pump_queued_messages(
        self, preferred_conversation_id: str | None = None
    ) -> None:
        with self._lifecycle_lock:
            if self._shutting_down:
                return
            conversation_ids = list(self._memory.queued_conversation_ids())
            if preferred_conversation_id in conversation_ids:
                conversation_ids.remove(preferred_conversation_id)
                conversation_ids.insert(0, preferred_conversation_id)
            for conversation_id in conversation_ids:
                if not self._generations.has_capacity():
                    return
                try:
                    queued_message = self._memory.next_queued_message(conversation_id)
                    if (
                        queued_message is None
                        or queued_message.status is QueuedMessageStatus.BLOCKED
                    ):
                        continue
                    connection = self._profiles.resolve(queued_message.profile_id)
                except ProfileNotFound:
                    self._memory.block_queued_message(
                        queued_message.id,
                        error_code="profile_not_found",
                        error_message=(
                            "Профиль удалён. Уберите сообщение из очереди и отправьте его снова."
                        ),
                    )
                    continue
                except MemoryError:
                    continue
                if (
                    queued_message.profile_revision is not None
                    and queued_message.profile_revision != connection.revision
                ):
                    self._memory.block_queued_message(
                        queued_message.id,
                        error_code="profile_changed",
                        error_message=(
                            "Профиль модели изменён после отправки. "
                            "Уберите сообщение из очереди и отправьте его снова."
                        ),
                    )
                    continue
                try:
                    generation = self._memory.activate_next_queued_message(
                        conversation_id,
                        queued_message.id,
                    )
                except MemoryError:
                    continue
                if generation is None:
                    continue
                self._submit(generation, connection)

    def _connection_for_conversation(
        self,
        conversation_id: str,
        profile_id: str | None,
    ) -> ModelConnection:
        conversation = self._memory.get_conversation(conversation_id)
        return self._connection_from_selection(conversation, profile_id)

    def _connection_from_selection(
        self,
        conversation: ConversationSummary | ConversationView,
        profile_id: str | None,
    ) -> ModelConnection:
        selected = profile_id or conversation.active_profile_id
        if not selected:
            raise ProfileNotFound("Сначала добавьте и выберите профиль модели.")
        return self._profiles.resolve(selected)

    def _submit(
        self,
        generation: GenerationView,
        connection: ModelConnection,
    ) -> GenerationView:
        try:
            self._generations.submit(generation.id, connection)
            return generation
        except GenerationCapacityError:
            return generation
        except Exception:
            return self._memory.fail_generation(
                generation.id,
                error_code="internal_start_error",
                error_message="Не удалось запустить генерацию.",
                attempts=0,
            )
