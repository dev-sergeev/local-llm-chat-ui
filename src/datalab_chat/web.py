from __future__ import annotations

import json
import logging
import mimetypes
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from datalab_chat import __version__
from datalab_chat.application import ChatApplication
from datalab_chat.gateways import GatewayFailure
from datalab_chat.memory import (
    MemoryConflict,
    MemoryNotFound,
    MemoryStorageError,
    MemoryValidationError,
)
from datalab_chat.profiles import (
    ProfileDraft,
    ProfileFormat,
    ProfileNotFound,
    ProfileStorageError,
    ProfileValidationError,
)


LOGGER = logging.getLogger("datalab_chat.web")
MAX_JSON_BODY = 1_000_000


class ChatHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        application: ChatApplication,
        static_dir: Path,
    ):
        self.application = application
        self.static_dir = static_dir.resolve()
        super().__init__(server_address, ChatRequestHandler)


def create_server(
    application: ChatApplication,
    *,
    static_dir: str | Path,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> ChatHTTPServer:
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("DataLab Risk Chat can bind only to localhost")
    if not 0 <= port <= 65535:
        raise ValueError("Port must be within [0, 65535]")
    directory = Path(static_dir)
    if not (directory / "index.html").is_file():
        raise FileNotFoundError("Frontend index.html is missing")
    return ChatHTTPServer((host, port), application, directory)


class ChatRequestHandler(BaseHTTPRequestHandler):
    server: ChatHTTPServer
    server_version = "DataLabRiskChat"
    sys_version = ""

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle("HEAD")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._handle("PUT")

    def do_PATCH(self) -> None:  # noqa: N802
        self._handle("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle("DELETE")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._json_error(
            HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed", "Метод запрещён."
        )

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        del size
        LOGGER.info("HTTP %s %s %s", self.command, urlsplit(self.path).path, code)

    def log_message(self, message_format: str, *args: object) -> None:
        del message_format, args
        LOGGER.info("HTTP server event")

    def _handle(self, method: str) -> None:
        try:
            parsed = urlsplit(self.path)
            path = unquote(parsed.path)
            if path.startswith("/api/") or path == "/api":
                if method not in {"GET", "HEAD"}:
                    self._validate_mutation_request()
                status, payload = self._dispatch_api(
                    method, path, parse_qs(parsed.query)
                )
                if status is HTTPStatus.NO_CONTENT:
                    self._empty(status)
                else:
                    self._json(status, payload, head_only=method == "HEAD")
                return
            if method not in {"GET", "HEAD"}:
                self._json_error(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    "method_not_allowed",
                    "Метод не поддерживается.",
                )
                return
            self._serve_static(path, head_only=method == "HEAD")
        except RequestError as exc:
            self._json_error(exc.status, exc.code, exc.message)
        except (ProfileValidationError, MemoryValidationError, ValueError, TypeError):
            self._json_error(
                HTTPStatus.BAD_REQUEST,
                "validation_error",
                "Проверьте заполненные поля.",
            )
        except (ProfileNotFound, MemoryNotFound) as exc:
            self._json_error(HTTPStatus.NOT_FOUND, "not_found", str(exc))
        except MemoryConflict as exc:
            self._json_error(HTTPStatus.CONFLICT, "conflict", str(exc))
        except GatewayFailure as exc:
            self._json_error(HTTPStatus.BAD_GATEWAY, exc.code, exc.message)
        except (ProfileStorageError, MemoryStorageError):
            self._json_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "local_storage_error",
                "Не удалось безопасно обновить локальные данные.",
            )
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            LOGGER.error("Unhandled HTTP exception: %s", type(exc).__name__)
            self._json_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "internal_error",
                "Внутренняя ошибка обработана; сервис продолжает работу.",
            )

    def _dispatch_api(
        self,
        method: str,
        path: str,
        query: dict[str, list[str]],
    ) -> tuple[HTTPStatus, object | None]:
        app = self.server.application

        if method in {"GET", "HEAD"} and path == "/api/health":
            return HTTPStatus.OK, {"status": "ok", "version": __version__}

        if path == "/api/profiles":
            if method in {"GET", "HEAD"}:
                return HTTPStatus.OK, [
                    profile.to_public_dict() for profile in app.list_profiles()
                ]
            if method == "POST":
                profile = app.create_profile(
                    self._profile_draft(self._json_body(), creating=True)
                )
                return HTTPStatus.CREATED, profile.to_public_dict()

        if path == "/api/profiles/test" and method == "POST":
            body = self._json_body()
            profile_id = _optional_string(body, "profile_id")
            timeout = body.get("timeout_seconds", 30)
            if not isinstance(timeout, (int, float)) or isinstance(timeout, bool):
                raise RequestError(
                    HTTPStatus.BAD_REQUEST,
                    "validation_error",
                    "Неверный тайм-аут.",
                )
            result = app.test_profile_draft(
                self._profile_draft(body, creating=profile_id is None),
                profile_id=profile_id,
                timeout_seconds=float(timeout),
            )
            return HTTPStatus.OK, result.to_public_dict()

        profile_match = re.fullmatch(r"/api/profiles/([0-9a-f]{32})", path)
        if profile_match:
            profile_id = profile_match.group(1)
            if method == "PUT":
                profile = app.update_profile(
                    profile_id,
                    self._profile_draft(self._json_body(), creating=False),
                )
                return HTTPStatus.OK, profile.to_public_dict()
            if method == "DELETE":
                app.delete_profile(profile_id)
                return HTTPStatus.NO_CONTENT, None

        test_match = re.fullmatch(r"/api/profiles/([0-9a-f]{32})/test", path)
        if test_match and method == "POST":
            body = self._json_body()
            timeout = body.get("timeout_seconds", 30)
            if not isinstance(timeout, (int, float)) or isinstance(timeout, bool):
                raise RequestError(
                    HTTPStatus.BAD_REQUEST, "validation_error", "Неверный тайм-аут."
                )
            result = app.test_profile(
                test_match.group(1), timeout_seconds=float(timeout)
            )
            return HTTPStatus.OK, result.to_public_dict()

        if path == "/api/conversations":
            if method in {"GET", "HEAD"}:
                search = query.get("query", [None])[0]
                return HTTPStatus.OK, [
                    item.to_public_dict() for item in app.list_conversations(search)
                ]
            if method == "POST":
                body = self._json_body()
                profile_id = _optional_string(body, "profile_id")
                conversation = app.create_conversation(profile_id)
                return HTTPStatus.CREATED, conversation.to_public_dict()

        conversation_match = re.fullmatch(r"/api/conversations/([0-9a-f]{32})", path)
        if conversation_match:
            conversation_id = conversation_match.group(1)
            if method in {"GET", "HEAD"}:
                return HTTPStatus.OK, app.get_conversation(
                    conversation_id
                ).to_public_dict()
            if method == "PATCH":
                body = self._json_body()
                if "title" not in body and "profile_id" not in body:
                    raise RequestError(
                        HTTPStatus.BAD_REQUEST,
                        "validation_error",
                        "Нет изменений для диалога.",
                    )
                result = app.update_conversation(
                    conversation_id,
                    title=(
                        _required_string(body, "title") if "title" in body else None
                    ),
                    profile_id=_optional_string(body, "profile_id"),
                    set_profile="profile_id" in body,
                )
                return HTTPStatus.OK, result.to_public_dict()
            if method == "DELETE":
                app.delete_conversation(conversation_id)
                return HTTPStatus.NO_CONTENT, None

        send_match = re.fullmatch(r"/api/conversations/([0-9a-f]{32})/messages", path)
        if send_match and method == "POST":
            body = self._json_body()
            generation = app.send_message(
                send_match.group(1),
                _required_string(body, "content"),
                _optional_string(body, "profile_id"),
            )
            return HTTPStatus.ACCEPTED, generation.to_public_dict()

        select_match = re.fullmatch(r"/api/conversations/([0-9a-f]{32})/select", path)
        if select_match and method == "POST":
            body = self._json_body()
            view = app.select_variant(
                select_match.group(1),
                _required_string(body, "message_id"),
            )
            return HTTPStatus.OK, view.to_public_dict()

        edit_match = re.fullmatch(r"/api/messages/([0-9a-f]{32})/edit", path)
        if edit_match and method == "POST":
            body = self._json_body()
            generation = app.edit_message(
                edit_match.group(1),
                _required_string(body, "content"),
                _optional_string(body, "profile_id"),
            )
            return HTTPStatus.ACCEPTED, generation.to_public_dict()

        regenerate_match = re.fullmatch(
            r"/api/messages/([0-9a-f]{32})/regenerate", path
        )
        if regenerate_match and method == "POST":
            body = self._json_body()
            generation = app.regenerate(
                regenerate_match.group(1),
                _optional_string(body, "profile_id"),
            )
            return HTTPStatus.ACCEPTED, generation.to_public_dict()

        generation_match = re.fullmatch(r"/api/generations/([0-9a-f]{32})", path)
        if generation_match and method in {"GET", "HEAD"}:
            generation = app.get_generation(generation_match.group(1))
            return HTTPStatus.OK, generation.to_public_dict()

        retry_match = re.fullmatch(r"/api/generations/([0-9a-f]{32})/retry", path)
        if retry_match and method == "POST":
            body = self._json_body()
            generation = app.retry_generation(
                retry_match.group(1),
                _optional_string(body, "profile_id"),
            )
            return HTTPStatus.ACCEPTED, generation.to_public_dict()

        cancel_match = re.fullmatch(r"/api/generations/([0-9a-f]{32})/cancel", path)
        if cancel_match and method == "POST":
            self._json_body()
            generation = app.cancel_generation(cancel_match.group(1))
            return HTTPStatus.OK, generation.to_public_dict()

        raise RequestError(HTTPStatus.NOT_FOUND, "not_found", "Маршрут не найден.")

    def _profile_draft(self, body: dict[str, Any], *, creating: bool) -> ProfileDraft:
        try:
            provider_format = ProfileFormat(_required_string(body, "format"))
        except ValueError as exc:
            raise ProfileValidationError("Неизвестный формат API.") from exc
        token = (
            _required_string(body, "token")
            if creating
            else _optional_string(body, "token")
        )
        return ProfileDraft(
            display_name=_required_string(body, "display_name"),
            provider_format=provider_format,
            base_url=_required_string(body, "base_url"),
            token=token,
            model_id=_required_string(body, "model_id"),
        )

    def _validate_mutation_request(self) -> None:
        origin = self.headers.get("Origin")
        if origin:
            parsed = urlsplit(origin)
            server_port = self.server.server_address[1]
            expected_port = parsed.port or (443 if parsed.scheme == "https" else 80)
            if (
                parsed.scheme != "http"
                or parsed.hostname not in {"127.0.0.1", "localhost"}
                or expected_port != server_port
            ):
                raise RequestError(
                    HTTPStatus.FORBIDDEN,
                    "forbidden_origin",
                    "Запрос пришёл не из локального интерфейса.",
                )
        content_type = self.headers.get_content_type()
        if content_type != "application/json":
            raise RequestError(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "json_required",
                "Для изменения данных требуется application/json.",
            )

    def _json_body(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length or "0")
        except ValueError as exc:
            raise RequestError(
                HTTPStatus.BAD_REQUEST, "invalid_json", "Неверная длина запроса."
            ) from exc
        if length < 0 or length > MAX_JSON_BODY:
            raise RequestError(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "payload_too_large",
                "Запрос превышает допустимый размер.",
            )
        try:
            raw = self.rfile.read(length)
            decoded = json.loads(raw.decode("utf-8") if raw else "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RequestError(
                HTTPStatus.BAD_REQUEST, "invalid_json", "Тело запроса не является JSON."
            ) from exc
        if not isinstance(decoded, dict):
            raise RequestError(
                HTTPStatus.BAD_REQUEST, "invalid_json", "Ожидается JSON-объект."
            )
        return decoded

    def _serve_static(self, path: str, *, head_only: bool) -> None:
        if path == "/favicon.ico":
            self._empty(HTTPStatus.NO_CONTENT)
            return
        if path == "/":
            relative = Path("index.html")
        elif path.startswith("/assets/") and ".." not in path.split("/"):
            relative = Path(path.removeprefix("/"))
        else:
            self._json_error(HTTPStatus.NOT_FOUND, "not_found", "Файл не найден.")
            return
        candidate = (self.server.static_dir / relative).resolve()
        if (
            self.server.static_dir not in candidate.parents
            and candidate != self.server.static_dir
        ):
            self._json_error(HTTPStatus.NOT_FOUND, "not_found", "Файл не найден.")
            return
        try:
            data = candidate.read_bytes()
        except OSError:
            self._json_error(HTTPStatus.NOT_FOUND, "not_found", "Файл не найден.")
            return
        content_type = (
            mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        )
        self.send_response(HTTPStatus.OK)
        self.send_header(
            "Content-Type",
            f"{content_type}; charset=utf-8"
            if content_type.startswith("text/")
            or content_type == "application/javascript"
            else content_type,
        )
        self.send_header("Content-Length", str(len(data)))
        self._common_headers(static=True)
        self.end_headers()
        if not head_only:
            self.wfile.write(data)

    def _json(
        self, status: HTTPStatus, payload: object, *, head_only: bool = False
    ) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self._common_headers(static=False)
        self.end_headers()
        if not head_only:
            self.wfile.write(data)

    def _json_error(self, status: HTTPStatus, code: str, message: str) -> None:
        try:
            self._json(
                status,
                {"error": {"code": code, "message": message}},
                head_only=self.command == "HEAD",
            )
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _empty(self, status: HTTPStatus) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self._common_headers(static=False)
        self.end_headers()

    def _common_headers(self, *, static: bool) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("X-Frame-Options", "DENY")
        if static:
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
                "base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
            )


class RequestError(Exception):
    def __init__(self, status: HTTPStatus, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _required_string(body: dict[str, Any], key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str):
        raise RequestError(
            HTTPStatus.BAD_REQUEST, "validation_error", f"Поле {key} обязательно."
        )
    return value


def _optional_string(body: dict[str, Any], key: str) -> str | None:
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise RequestError(
            HTTPStatus.BAD_REQUEST,
            "validation_error",
            f"Поле {key} имеет неверный тип.",
        )
    return value
