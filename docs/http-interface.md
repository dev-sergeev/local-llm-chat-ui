# Локальный HTTP-интерфейс

Интерфейс обслуживается только на loopback, а `Host` каждого прикладного запроса проверяется на loopback-адрес. Изменяющие запросы требуют `Content-Type: application/json`; CORS не включён, cross-site запросы отклоняются. Официальный browser UI добавляет `X-DataLab-UI: browser`: это позволяет работать через локальную browser-оболочку или preview-прокси, которые сохраняют loopback `Host`, но передают `Origin: null` либо собственный forwarding-origin. Запрос, явно помеченный браузером как `Sec-Fetch-Site: cross-site`, всё равно запрещён. Ошибки имеют форму:

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
- `GET /api/conversations/{id}` — активная ветка, варианты, состояние генерации и `queued_messages`;
- `PATCH /api/conversations/{id}` — атомарно изменить название и/или активный профиль;
- `DELETE /api/conversations/{id}` — подтверждаемое удаление;
- `POST /api/conversations/{id}/messages` — новая генерация либо устойчивое сообщение очереди, если диалог уже занят;
- `POST /api/conversations/{id}/select` — выбрать версию сообщения и её последнюю ветку.

## Сообщения и генерации

- `POST /api/messages/{id}/edit` — новая ветка от изменённого сообщения пользователя;
- `POST /api/messages/{id}/regenerate` — альтернативная версия ответа;
- `GET /api/generations/{id}` — `queued`, `running`, `retrying`, `succeeded`, `failed`, `cancelled` или `interrupted`;
- `POST /api/generations/{id}/retry` — ручной повтор терминальной ошибки;
- `POST /api/generations/{id}/cancel` — немедленно запретить сохранение ответа.
- `DELETE /api/queued-messages/{id}` — убрать ещё не активированное сообщение из очереди.

`GET /api/health` возвращает версию и готовность самого localhost-сервиса. Он не вызывает внешнюю модель.
