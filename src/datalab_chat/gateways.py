from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from datalab_chat.profiles import ModelConnection, ProfileFormat


class GatewayFailure(Exception):
    """A sanitized failure crossing the true-external LLM seam."""

    def __init__(self, code: str, message: str, *, retryable: bool):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class LLMGateway(Protocol):
    def complete(
        self,
        messages: Sequence[dict[str, str]],
        *,
        timeout_seconds: float,
    ) -> str: ...


class GatewayFactory(Protocol):
    def create(self, connection: ModelConnection) -> LLMGateway: ...


class LangChainGatewayFactory:
    def create(self, connection: ModelConnection) -> LLMGateway:
        if connection.provider_format is ProfileFormat.GIGACHAT:
            return GigaChatGateway(connection)
        if connection.provider_format is ProfileFormat.OPENAI:
            return OpenAICompatibleGateway(connection)
        raise GatewayFailure(
            "unsupported_format",
            "Формат API этого профиля не поддерживается.",
            retryable=False,
        )


class OpenAICompatibleGateway:
    def __init__(self, connection: ModelConnection):
        self._connection = connection

    def complete(
        self,
        messages: Sequence[dict[str, str]],
        *,
        timeout_seconds: float,
    ) -> str:
        try:
            from langchain_openai import ChatOpenAI

            model = ChatOpenAI(
                model=self._connection.model_id,
                api_key=self._connection.token,
                base_url=self._connection.base_url,
                timeout=timeout_seconds,
                max_retries=0,
                streaming=False,
            )
            response = model.invoke(_langchain_messages(messages))
            return _response_text(response.content)
        except GatewayFailure:
            raise
        except Exception as exc:
            raise classify_gateway_exception(exc) from None


class GigaChatGateway:
    def __init__(self, connection: ModelConnection):
        self._connection = connection

    def complete(
        self,
        messages: Sequence[dict[str, str]],
        *,
        timeout_seconds: float,
    ) -> str:
        try:
            from langchain_gigachat import GigaChat

            model = GigaChat(
                access_token=self._connection.token,
                base_url=self._connection.base_url,
                model=self._connection.model_id,
                timeout=timeout_seconds,
                max_retries=0,
                streaming=False,
            )
            response = model.invoke(_langchain_messages(messages))
            return _response_text(response.content)
        except GatewayFailure:
            raise
        except Exception as exc:
            raise classify_gateway_exception(exc) from None


def _langchain_messages(messages: Sequence[dict[str, str]]):
    try:
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
    except Exception as exc:
        raise GatewayFailure(
            "adapter_unavailable",
            "Библиотеки LangChain недоступны в локальном окружении.",
            retryable=False,
        ) from exc

    converted = []
    for message in messages:
        role = message.get("role")
        content = message.get("content", "")
        if role == "user":
            converted.append(HumanMessage(content=content))
        elif role == "assistant":
            converted.append(AIMessage(content=content))
        elif role == "system":
            converted.append(SystemMessage(content=content))
        else:
            raise GatewayFailure(
                "invalid_context",
                "История содержит неподдерживаемую роль сообщения.",
                retryable=False,
            )
    return converted


def _response_text(content: object) -> str:
    if isinstance(content, str):
        text = content.strip()
    elif isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        text = "\n".join(parts).strip()
    else:
        text = str(content).strip() if content is not None else ""
    if not text:
        raise GatewayFailure(
            "empty_response",
            "Модель вернула пустой ответ.",
            retryable=True,
        )
    return text


def classify_gateway_exception(exc: Exception) -> GatewayFailure:
    status_code = _status_code(exc)
    if status_code in {401, 403}:
        return GatewayFailure(
            "authentication",
            "Не удалось авторизоваться. Проверьте URL и токен профиля.",
            retryable=False,
        )
    if status_code == 408:
        return GatewayFailure(
            "request_timeout",
            "Модель не ответила вовремя.",
            retryable=True,
        )
    if status_code == 429:
        return GatewayFailure(
            "rate_limited",
            "Модель временно ограничила частоту запросов.",
            retryable=True,
        )
    if status_code is not None and status_code >= 500:
        return GatewayFailure(
            "provider_unavailable",
            "Сервис модели временно недоступен.",
            retryable=True,
        )
    if status_code is not None and 400 <= status_code < 500:
        return GatewayFailure(
            "invalid_request",
            "Модель отклонила параметры запроса.",
            retryable=False,
        )

    name = type(exc).__name__.lower()
    if isinstance(exc, TimeoutError) or "timeout" in name:
        return GatewayFailure(
            "request_timeout",
            "Модель не ответила вовремя.",
            retryable=True,
        )
    if isinstance(exc, (ConnectionError, OSError)) or any(
        marker in name for marker in ("connect", "network", "transport")
    ):
        return GatewayFailure(
            "network",
            "Не удалось установить соединение с моделью.",
            retryable=True,
        )
    if isinstance(exc, ImportError) or "modulenotfound" in name:
        return GatewayFailure(
            "adapter_unavailable",
            "Нужный LLM-адаптер не установлен в локальном окружении.",
            retryable=False,
        )
    return GatewayFailure(
        "unexpected_provider_error",
        "Модель вернула непредвиденную ошибку.",
        retryable=False,
    )


def _status_code(exc: Exception) -> int | None:
    candidates = [exc, getattr(exc, "response", None), getattr(exc, "__cause__", None)]
    for candidate in candidates:
        if candidate is None:
            continue
        value = getattr(candidate, "status_code", None)
        if isinstance(value, int):
            return value
        response = getattr(candidate, "response", None)
        value = getattr(response, "status_code", None)
        if isinstance(value, int):
            return value
    return None

