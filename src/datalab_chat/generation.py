from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass

from datalab_chat.gateways import (
    BoundedGatewayCaller,
    ChunkCallback,
    GatewayCallCancelled,
    GatewayCallDeadline,
    GatewayFactory,
    GatewayFailure,
    LLMGateway,
)
from datalab_chat.memory import (
    GenerationStatus,
    GenerationView,
    MemoryConflict,
    MemoryError,
    SQLiteChatMemory,
)
from datalab_chat.profiles import ModelConnection


@dataclass(frozen=True, slots=True)
class GenerationPolicy:
    total_timeout_seconds: float = 600.0
    max_attempts: int = 3
    base_backoff_seconds: float = 1.0
    max_backoff_seconds: float = 8.0
    jitter_ratio: float = 0.2
    poll_interval_seconds: float = 0.1
    max_concurrent_generations: int = 4

    def __post_init__(self) -> None:
        if self.total_timeout_seconds <= 0 or self.total_timeout_seconds > 600:
            raise ValueError("Generation timeout must be within (0, 600] seconds")
        if self.max_attempts < 1 or self.max_attempts > 3:
            raise ValueError("Generation attempts must be within [1, 3]")
        if self.base_backoff_seconds < 0 or self.max_backoff_seconds < 0:
            raise ValueError("Backoff cannot be negative")
        if not 0 <= self.jitter_ratio <= 1:
            raise ValueError("Jitter ratio must be within [0, 1]")
        if self.poll_interval_seconds <= 0:
            raise ValueError("Poll interval must be positive")
        if not 1 <= self.max_concurrent_generations <= 32:
            raise ValueError("Concurrent generations must be within [1, 32]")


@dataclass(slots=True)
class _Task:
    thread: threading.Thread
    cancel_event: threading.Event


class _Cancelled(Exception):
    pass


class _DeadlineExceeded(Exception):
    pass


class GenerationCapacityError(MemoryConflict):
    """The bounded local generation executor has no free task slot."""


class GenerationCoordinator:
    """Runs durable LLM generations without exposing threads or retries to callers."""

    def __init__(
        self,
        memory: SQLiteChatMemory,
        gateway_factory: GatewayFactory,
        *,
        policy: GenerationPolicy | None = None,
        caller: BoundedGatewayCaller | None = None,
    ):
        self._memory = memory
        self._gateway_factory = gateway_factory
        self._policy = policy or GenerationPolicy()
        self._caller = caller or BoundedGatewayCaller(
            poll_interval_seconds=self._policy.poll_interval_seconds
        )
        self._tasks: dict[str, _Task] = {}
        self._lock = threading.RLock()

    def submit(
        self,
        generation_id: str,
        connection: ModelConnection,
        *,
        on_chunk: ChunkCallback | None = None,
    ) -> GenerationView:
        generation = self._memory.get_generation(generation_id)
        if generation.status is not GenerationStatus.QUEUED:
            raise MemoryConflict("Запустить можно только новую генерацию.")
        with self._lock:
            if generation_id in self._tasks:
                raise MemoryConflict("Генерация уже запущена.")
            if len(self._tasks) >= self._policy.max_concurrent_generations:
                raise GenerationCapacityError(
                    "Слишком много одновременных генераций. Повторите запрос позже."
                )
            cancel_event = threading.Event()
            thread = threading.Thread(
                target=self._run,
                args=(generation_id, connection, cancel_event, on_chunk),
                name=f"generation-{generation_id[:8]}",
                daemon=True,
            )
            self._tasks[generation_id] = _Task(thread=thread, cancel_event=cancel_event)
            try:
                thread.start()
            except BaseException:
                self._tasks.pop(generation_id, None)
                raise
        return generation

    def cancel(self, generation_id: str) -> GenerationView:
        with self._lock:
            task = self._tasks.get(generation_id)
            if task is not None:
                task.cancel_event.set()
        return self._memory.cancel_generation(generation_id)

    def wait(self, generation_id: str, *, timeout: float | None = None) -> bool:
        with self._lock:
            task = self._tasks.get(generation_id)
        if task is None:
            return True
        task.thread.join(timeout)
        finished = not task.thread.is_alive()
        if finished:
            with self._lock:
                self._tasks.pop(generation_id, None)
        return finished

    def shutdown(self, *, timeout: float = 2.0) -> None:
        with self._lock:
            tasks = list(self._tasks.items())
            for _, task in tasks:
                task.cancel_event.set()
        for generation_id, task in tasks:
            try:
                self._memory.cancel_generation(generation_id)
            except MemoryError:
                pass
            task.thread.join(timeout)
        with self._lock:
            self._tasks = {
                generation_id: task
                for generation_id, task in self._tasks.items()
                if task.thread.is_alive()
            }

    def _run(
        self,
        generation_id: str,
        connection: ModelConnection,
        cancel_event: threading.Event,
        on_chunk: ChunkCallback | None,
    ) -> None:
        deadline = time.monotonic() + self._policy.total_timeout_seconds
        attempts = 0
        try:
            messages = self._memory.context_for_generation(generation_id)
            gateway = self._gateway_factory.create(connection)
            while attempts < self._policy.max_attempts:
                if self._cancelled(generation_id, cancel_event):
                    self._cancel_safely(generation_id)
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._fail_deadline(generation_id, attempts)
                    return

                attempts += 1
                self._memory.mark_generation_running(generation_id, attempt=attempts)
                try:
                    answer = self._invoke_interruptibly(
                        gateway,
                        messages,
                        timeout_seconds=remaining,
                        deadline=deadline,
                        cancel_event=cancel_event,
                        on_chunk=on_chunk,
                    )
                    if self._cancelled(generation_id, cancel_event):
                        self._cancel_safely(generation_id)
                        return
                    self._memory.complete_generation(
                        generation_id,
                        answer,
                        connection.snapshot(),
                    )
                    return
                except _Cancelled:
                    self._cancel_safely(generation_id)
                    return
                except _DeadlineExceeded:
                    self._fail_deadline(generation_id, attempts)
                    return
                except GatewayFailure as failure:
                    if not failure.retryable or attempts >= self._policy.max_attempts:
                        self._fail_safely(
                            generation_id,
                            code=failure.code,
                            message=failure.message,
                            attempts=attempts,
                        )
                        return
                    self._memory.mark_generation_retrying(
                        generation_id,
                        attempt=attempts,
                        error_code=failure.code,
                        error_message=failure.message,
                    )
                    if not self._wait_backoff(attempts, deadline, cancel_event):
                        if self._cancelled(generation_id, cancel_event):
                            self._cancel_safely(generation_id)
                        else:
                            self._fail_deadline(generation_id, attempts)
                        return
        except GatewayFailure as failure:
            self._fail_safely(
                generation_id,
                code=failure.code,
                message=failure.message,
                attempts=attempts,
            )
        except MemoryConflict:
            return
        except Exception:
            self._fail_safely(
                generation_id,
                code="unexpected_provider_error",
                message="Модель вернула непредвиденную ошибку.",
                attempts=attempts,
            )
        finally:
            with self._lock:
                self._tasks.pop(generation_id, None)

    def _invoke_interruptibly(
        self,
        gateway: LLMGateway,
        messages: list[dict[str, str]],
        *,
        timeout_seconds: float,
        deadline: float,
        cancel_event: threading.Event,
        on_chunk: ChunkCallback | None,
    ) -> str:
        remaining = min(timeout_seconds, deadline - time.monotonic())
        if remaining <= 0:
            raise _DeadlineExceeded
        try:
            value = self._caller.call(
                gateway,
                messages,
                timeout_seconds=remaining,
                cancel_event=cancel_event,
                on_chunk=on_chunk,
            )
        except GatewayCallCancelled:
            raise _Cancelled from None
        except GatewayCallDeadline:
            raise _DeadlineExceeded from None
        if not value.strip():
            raise GatewayFailure(
                "empty_response",
                "Модель вернула пустой ответ.",
                retryable=False,
            )
        return value.strip()

    def _wait_backoff(
        self,
        attempt: int,
        deadline: float,
        cancel_event: threading.Event,
    ) -> bool:
        base = min(
            self._policy.max_backoff_seconds,
            self._policy.base_backoff_seconds * (2 ** (attempt - 1)),
        )
        jitter = base * self._policy.jitter_ratio
        delay = max(0.0, base + random.uniform(-jitter, jitter))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        return (
            not cancel_event.wait(min(delay, remaining)) and time.monotonic() < deadline
        )

    def _cancelled(self, generation_id: str, cancel_event: threading.Event) -> bool:
        if cancel_event.is_set():
            return True
        try:
            return self._memory.is_generation_cancelled(generation_id)
        except MemoryError:
            return False

    def _cancel_safely(self, generation_id: str) -> None:
        try:
            self._memory.cancel_generation(generation_id)
        except MemoryError:
            pass

    def _fail_deadline(self, generation_id: str, attempts: int) -> None:
        self._fail_safely(
            generation_id,
            code="deadline_exceeded",
            message="Превышен общий тайм-аут генерации 10 минут.",
            attempts=attempts,
        )

    def _fail_safely(
        self,
        generation_id: str,
        *,
        code: str,
        message: str,
        attempts: int,
    ) -> None:
        try:
            self._memory.fail_generation(
                generation_id,
                error_code=code,
                error_message=message,
                attempts=attempts,
            )
        except MemoryError:
            pass
