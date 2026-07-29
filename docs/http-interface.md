# Локальный HTTP-интерфейс

Интерфейс обслуживается только на loopback. Изменяющие запросы требуют `Content-Type: application/json`; CORS не включён, чужой `Origin` отклоняется. Ошибки имеют форму:

```json
{"error":{"code":"validation_error","message":"Проверьте заполненные поля."}}
```

## Профили

- `GET /api/profiles` — безопасные сведения без токена;
- `POST /api/profiles` — создать профиль;
- `PUT /api/profiles/{id}` — изменить; пустой токен сохраняет прежний;
- `DELETE /api/profiles/{id}` — удалить, не удаляя исторические снимки;
- `POST /api/profiles/test` — проверить ещё не сохранённые значения формы без записи секрета;
- `POST /api/profiles/{id}/test` — короткая проверка подключения без записи в чат.

## Диалоги

- `GET /api/conversations?query=...` — список и поиск;
- `POST /api/conversations` — новый диалог;
- `GET /api/conversations/{id}` — активная ветка, варианты и состояние генерации;
- `PATCH /api/conversations/{id}` — атомарно изменить название и/или активный профиль;
- `DELETE /api/conversations/{id}` — подтверждаемое удаление;
- `POST /api/conversations/{id}/messages` — новое сообщение и фоновая генерация;
- `POST /api/conversations/{id}/select` — выбрать версию сообщения и её последнюю ветку.

## Сообщения и генерации

- `POST /api/messages/{id}/edit` — новая ветка от изменённого сообщения пользователя;
- `POST /api/messages/{id}/regenerate` — альтернативная версия ответа;
- `GET /api/generations/{id}` — `queued`, `running`, `retrying`, `succeeded`, `failed`, `cancelled` или `interrupted`;
- `POST /api/generations/{id}/retry` — ручной повтор терминальной ошибки;
- `POST /api/generations/{id}/cancel` — немедленно запретить сохранение ответа.

`GET /api/health` возвращает версию и готовность самого localhost-сервиса. Он не вызывает внешнюю модель.
