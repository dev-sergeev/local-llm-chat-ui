from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import threading
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit


class ProfileError(Exception):
    """Base error exposed by the profile catalog interface."""


class ProfileValidationError(ProfileError):
    """A profile cannot be represented safely or sent to an adapter."""


class ProfileNotFound(ProfileError):
    """The requested profile does not exist."""


class ProfileStorageError(ProfileError):
    """The local profile file cannot be read or updated safely."""


class ProfileFormat(StrEnum):
    GIGACHAT = "gigachat"
    OPENAI = "openai"


@dataclass(frozen=True, slots=True)
class ProfileDraft:
    display_name: str
    provider_format: ProfileFormat
    base_url: str
    token: str | None
    model_id: str


@dataclass(frozen=True, slots=True)
class ModelProfile:
    id: str
    display_name: str
    provider_format: ProfileFormat
    base_url: str
    model_id: str
    has_token: bool

    def to_public_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "format": self.provider_format.value,
            "base_url": self.base_url,
            "model_id": self.model_id,
            "has_token": self.has_token,
        }


@dataclass(frozen=True, slots=True)
class ModelConnection:
    id: str
    display_name: str
    provider_format: ProfileFormat
    base_url: str
    token: str
    model_id: str

    def summary(self) -> ModelProfile:
        return ModelProfile(
            id=self.id,
            display_name=self.display_name,
            provider_format=self.provider_format,
            base_url=self.base_url,
            model_id=self.model_id,
            has_token=bool(self.token),
        )

    def snapshot(self) -> dict[str, str]:
        return {
            "display_name": self.display_name,
            "format": self.provider_format.value,
            "model_id": self.model_id,
        }


class EnvProfileCatalog:
    """Durable model profiles stored in one generated block of a local `.env`."""

    _BEGIN = "# BEGIN DATALAB RISK CHAT MANAGED PROFILES"
    _END = "# END DATALAB RISK CHAT MANAGED PROFILES"
    _IDS_KEY = "DATALAB_PROFILE_IDS"
    _PROFILE_KEY = re.compile(
        r"^DATALAB_PROFILE_([0-9a-f]{32})_(DISPLAY_NAME|FORMAT|BASE_URL|TOKEN|MODEL_ID)$"
    )

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()

    def list(self) -> list[ModelProfile]:
        try:
            return [connection.summary() for connection in self._load_connections()]
        except ProfileError:
            raise
        except Exception as exc:
            raise ProfileStorageError("Не удалось прочитать профили моделей.") from exc

    def get(self, profile_id: str) -> ModelProfile:
        return self.resolve(profile_id).summary()

    def resolve(self, profile_id: str) -> ModelConnection:
        try:
            for connection in self._load_connections():
                if connection.id == profile_id:
                    return connection
        except ProfileError:
            raise
        except Exception as exc:
            raise ProfileStorageError("Не удалось прочитать профиль модели.") from exc
        raise ProfileNotFound("Профиль модели не найден.")

    def create(self, draft: ProfileDraft) -> ModelProfile:
        with self._lock:
            try:
                connection = self._validated_connection(secrets.token_hex(16), draft)
                connections = self._load_connections()
                connections.append(connection)
                self._write_connections(connections)
                return connection.summary()
            except ProfileError:
                raise
            except Exception as exc:
                raise ProfileStorageError("Не удалось сохранить профиль модели.") from exc

    def update(self, profile_id: str, draft: ProfileDraft) -> ModelProfile:
        with self._lock:
            try:
                connections = self._load_connections()
                for index, current in enumerate(connections):
                    if current.id != profile_id:
                        continue
                    replacement = self._validated_connection(
                        profile_id,
                        draft,
                        existing_token=current.token,
                    )
                    connections[index] = replacement
                    self._write_connections(connections)
                    return replacement.summary()
            except ProfileError:
                raise
            except Exception as exc:
                raise ProfileStorageError("Не удалось обновить профиль модели.") from exc
        raise ProfileNotFound("Профиль модели не найден.")

    def delete(self, profile_id: str) -> None:
        with self._lock:
            try:
                connections = self._load_connections()
                remaining = [item for item in connections if item.id != profile_id]
                if len(remaining) == len(connections):
                    raise ProfileNotFound("Профиль модели не найден.")
                self._write_connections(remaining)
            except ProfileError:
                raise
            except Exception as exc:
                raise ProfileStorageError("Не удалось удалить профиль модели.") from exc

    def _validated_connection(
        self,
        profile_id: str,
        draft: ProfileDraft,
        *,
        existing_token: str | None = None,
    ) -> ModelConnection:
        display_name = draft.display_name.strip()
        base_url = draft.base_url.strip()
        model_id = draft.model_id.strip()
        token = (draft.token or "").strip() or (existing_token or "")

        try:
            provider_format = ProfileFormat(draft.provider_format)
        except (TypeError, ValueError) as exc:
            raise ProfileValidationError("Неизвестный формат API.") from exc

        if not display_name or len(display_name) > 80:
            raise ProfileValidationError("Название профиля должно содержать от 1 до 80 символов.")
        if any(character in display_name for character in "\r\n\0"):
            raise ProfileValidationError("Название профиля содержит недопустимые символы.")
        if not model_id or len(model_id) > 200:
            raise ProfileValidationError("Model ID должен содержать от 1 до 200 символов.")
        if any(character in model_id for character in "\r\n\0"):
            raise ProfileValidationError("Model ID содержит недопустимые символы.")
        if not token or len(token) > 8192:
            raise ProfileValidationError("Токен обязателен и не должен превышать 8192 символа.")
        if any(character in token for character in "\r\n\0"):
            raise ProfileValidationError("Токен не может содержать перенос строки.")

        try:
            parsed_url = urlsplit(base_url)
        except ValueError as exc:
            raise ProfileValidationError("URL профиля имеет неверный формат.") from exc
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ProfileValidationError("URL профиля должен начинаться с http:// или https://.")
        if len(base_url) > 2048 or any(character in base_url for character in "\r\n\0"):
            raise ProfileValidationError("URL профиля содержит недопустимые символы.")

        return ModelConnection(
            id=profile_id,
            display_name=display_name,
            provider_format=provider_format,
            base_url=base_url.rstrip("/"),
            token=token,
            model_id=model_id,
        )

    def _load_connections(self) -> list[ModelConnection]:
        with self._lock:
            text = self._read_text()
            managed_lines = self._managed_lines(text)
            if managed_lines is None:
                return []

            values: dict[str, str] = {}
            for line in managed_lines:
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                key, raw_value = stripped.split("=", 1)
                try:
                    decoded = json.loads(raw_value)
                except json.JSONDecodeError as exc:
                    raise ProfileStorageError("Блок профилей в .env повреждён.") from exc
                if not isinstance(decoded, str):
                    raise ProfileStorageError("Блок профилей в .env имеет неверный формат.")
                values[key] = decoded

            ids_value = values.get(self._IDS_KEY, "")
            profile_ids = [value for value in ids_value.split(",") if value]
            if len(profile_ids) != len(set(profile_ids)):
                raise ProfileStorageError("В .env обнаружены повторяющиеся ID профилей.")

            connections: list[ModelConnection] = []
            for profile_id in profile_ids:
                if not re.fullmatch(r"[0-9a-f]{32}", profile_id):
                    raise ProfileStorageError("В .env обнаружен неверный ID профиля.")
                prefix = f"DATALAB_PROFILE_{profile_id}_"
                try:
                    connection = ModelConnection(
                        id=profile_id,
                        display_name=values[prefix + "DISPLAY_NAME"],
                        provider_format=ProfileFormat(values[prefix + "FORMAT"]),
                        base_url=values[prefix + "BASE_URL"],
                        token=values[prefix + "TOKEN"],
                        model_id=values[prefix + "MODEL_ID"],
                    )
                except (KeyError, ValueError) as exc:
                    raise ProfileStorageError("Профиль в .env заполнен не полностью.") from exc
                connections.append(connection)
            return connections

    def _read_text(self) -> str:
        try:
            if not self.path.exists():
                return ""
            return self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ProfileStorageError("Не удалось прочитать локальный .env.") from exc

    def _managed_lines(self, text: str) -> list[str] | None:
        lines = text.splitlines()
        begin_positions = [index for index, line in enumerate(lines) if line == self._BEGIN]
        end_positions = [index for index, line in enumerate(lines) if line == self._END]
        if not begin_positions and not end_positions:
            return None
        if len(begin_positions) != 1 or len(end_positions) != 1:
            raise ProfileStorageError("Границы блока профилей в .env повреждены.")
        begin = begin_positions[0]
        end = end_positions[0]
        if begin >= end:
            raise ProfileStorageError("Границы блока профилей в .env повреждены.")
        return lines[begin + 1 : end]

    def _write_connections(self, connections: list[ModelConnection]) -> None:
        original = self._read_text()
        managed = self._render_managed_block(connections)
        updated = self._replace_managed_block(original, managed)
        self.path.parent.mkdir(parents=True, exist_ok=True)

        temporary_path: Path | None = None
        try:
            file_descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.",
                dir=self.path.parent,
                text=True,
            )
            temporary_path = Path(temporary_name)
            with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(updated)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, self.path)
            os.chmod(self.path, 0o600)
        except OSError as exc:
            raise ProfileStorageError("Не удалось атомарно обновить локальный .env.") from exc
        finally:
            if temporary_path is not None and temporary_path.exists():
                try:
                    temporary_path.unlink()
                except OSError:
                    pass

    def _render_managed_block(self, connections: list[ModelConnection]) -> str:
        lines = [
            self._BEGIN,
            f"{self._IDS_KEY}={json.dumps(','.join(item.id for item in connections))}",
        ]
        for connection in connections:
            prefix = f"DATALAB_PROFILE_{connection.id}_"
            fields = {
                "DISPLAY_NAME": connection.display_name,
                "FORMAT": connection.provider_format.value,
                "BASE_URL": connection.base_url,
                "TOKEN": connection.token,
                "MODEL_ID": connection.model_id,
            }
            for suffix, value in fields.items():
                lines.append(f"{prefix}{suffix}={json.dumps(value, ensure_ascii=False)}")
        lines.append(self._END)
        return "\n".join(lines)

    def _replace_managed_block(self, original: str, managed: str) -> str:
        lines = original.splitlines()
        try:
            begin = lines.index(self._BEGIN)
            end = lines.index(self._END)
        except ValueError:
            clean_original = original.rstrip("\n")
            if clean_original:
                return f"{clean_original}\n\n{managed}\n"
            return f"{managed}\n"
        if begin >= end:
            raise ProfileStorageError("Границы блока профилей в .env повреждены.")
        replacement = lines[:begin] + managed.splitlines() + lines[end + 1 :]
        return "\n".join(replacement).rstrip("\n") + "\n"
