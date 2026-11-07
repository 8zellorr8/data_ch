# API для frontend

Base URL: `http://127.0.0.1:8000`. Все запросы и ответы — JSON UTF-8. `Content-Type: application/json` для POST. Полные схемы входных данных: `/docs` и `/openapi.json`.

Версия backend 0.2 сохраняет прежние пути, HTTP-методы и JSON-структуру успешных ответов. В OpenAPI теперь также описаны все выходные модели и единая структура ошибок. Подключённому frontend менять запросы не требуется.

Входные имена, подписи авторов и комментарии должны содержать хотя бы один непробельный символ. Строка из одних пробелов возвращает прежний формат `422 VALIDATION_ERROR`. Допустимые строки сохраняются дословно, включая пробелы и переносы. Существующие записи читаются без изменения. Внутренняя ошибка сервера при обработке ответа AI возвращает `500 INTERNAL_ERROR`; код `502 AI_INVALID_RESULT` используется для некорректных данных модели.

Важное различие: `conversation.id` — внутренний UUID для запросов оценки; `conversation.conversation_id` — исходный ID организаторов для показа. Сообщения получают `m1`, `m2`, … внутри диалога.

## Маршруты

| Метод | Путь | Результат |
| --- | --- | --- |
| GET | `/health` | `status`, `database`, `ai_configured` |
| POST | `/api/v1/datasets` | Импорт `{name, group_id, records}` → `{id, name, group_id, count, created_at}`, 201 |
| POST | `/api/v1/datasets/import-organizer` | `{group_id, name}` → тот же результат; оба поля необязательны |
| GET | `/api/v1/datasets` | `{items:[{id, name, group_id, count, created_at}]}` |
| GET | `/api/v1/datasets/{id}/conversations` | `{items, total, limit, offset}`; фильтры `language`, `channel` |
| GET | `/api/v1/conversations/{id}` | Полный диалог с `transcript` |
| GET | `/api/v1/checklists` | `{items:[{checklist_id, version, name, created_at}]}` — последние версии |
| POST | `/api/v1/checklists` | Новый чек-лист → полная версия, 201 |
| GET | `/api/v1/checklists/{id}` | Последняя полная версия |
| GET | `/api/v1/checklists/{id}/versions/{n}` | Конкретная полная версия |
| POST | `/api/v1/checklists/{id}/versions` | `{expected_version, definition}` → новая полная версия, 201 |
| POST | `/api/v1/evaluations` | `{conversation_id, checklist_id, checklist_version}` → оценка, 201 |
| POST | `/api/v1/evaluations/manual` | Те же поля + `author`, `result` → явно ручная оценка, 201 |
| GET | `/api/v1/evaluations/{id}` | Полная оценка с апелляциями |
| GET | `/api/v1/conversations/{id}/evaluations` | `{items:[{id, origin, revision, created_at, score}], total, limit, offset}` |
| POST | `/api/v1/evaluations/{id}/appeals` | `{criterion_id, comment, author}` → апелляция, 201 |
| POST | `/api/v1/appeals/{id}/decision` | Решение → обновлённая полная оценка |
| GET | `/api/v1/datasets/{id}/export` | `{schema_version, exported_at, dataset, conversations, evaluations}` |
| POST | `/api/v1/evaluation-batches` | `{conversation_ids, checklist_id, checklist_version}` + необязательный Idempotency-Key → пакет, 201 |
| GET | `/api/v1/evaluation-batches` | `{items,total,limit,offset}`; сводки и счётчики статусов, limit 1–100 (20 по умолчанию), offset от 0 |
| GET | `/api/v1/evaluation-batches/{id}` | Пакет, completed, статусы и ID оценок по каждому элементу |
| POST | `/api/v1/reports/usage` | Один из evaluation_ids / execution_ids / batch_id → отчёт известного расхода |

Пагинация: `limit` 1–1000, по умолчанию 100; `offset` от 0. Языки: `RU`, `KZ`, `EN`. Каналы: `agent chat`, `voice call`. В списке диалогов вместо `transcript` приходит `message_count`; остальные поля те же. Все временные отметки исходного транскрипта — строки, без преобразования часового пояса.

## Импорт

```json
{
  "name": "Мой набор",
  "group_id": "DEV",
  "records": [{
    "conversation_id": "example-1",
    "language": "RU",
    "channel": "agent chat",
    "transcript": [
      {"timestamp": "09:00:00", "speaker": "client", "text": "Здравствуйте! Где инструкция?"},
      {"timestamp": "09:00:03", "speaker": "operator", "text": "Здравствуйте, я Анна. Инструкция в разделе Помощь. До свидания!"}
    ],
    "audio_url": null
  }]
}
```

Роли: `client`, `operator`, `bot`, `system`. `audio_url` необязателен. API принимает до 1000 диалогов в одном запросе и до 8 MiB тела. Весь импорт отклоняется, если хотя бы один диалог невалиден. Дубликаты внешнего ID в одном наборе запрещены; одинаковые ID в разных наборах разрешены. Если пользователь выбрал исходный JSON организаторов, взять `file.data.records` и обернуть в `{name,group_id,records}`.

## Чек-листы

Исходный `checklist_id` — `freedom-broker`, версия 1. UI должен загрузить критерии и строить поля по `type`, `scale`, `allow_na`.

```json
{
  "name": "Мой чек-лист",
  "global_rules": "Оценивать только оператора",
  "criteria": [{
    "id": "clarity",
    "name": "Понятность ответа",
    "section": "Коммуникация",
    "type": "scale",
    "weight": 20,
    "rule": "0 — ответ непонятен; 5 — частично понятен; 10 — понятен",
    "call_rule": "Для звонка оценивать устные формулировки",
    "scale": [0, 5, 10],
    "allow_na": true,
    "na_rule": "Нет ответа оператора из-за обрыва"
  }]
}
```

Для `binary` и `flag` — `scale: []`. Флаг имеет вес 0. ID критериев уникальны: латинские буквы, цифры, `_`, `-`. Поддерживается 1–40 критериев; сумма весов не обязана равняться 100, итог нормируется. При редактировании отправить только `name`, `global_rules`, `criteria` в `definition`; служебные `checklist_id`, `version`, `created_at` туда не копировать. При 409 перезагрузить текущую версию и предложить повторить изменение.

## Оценка и цитаты

```json
{"conversation_id":"ВНУТРЕННИЙ_UUID","checklist_id":"freedom-broker","checklist_version":1}
```

Полная оценка содержит:

- `id`, `conversation_id`, `origin` (`ai` / `manual`), `created_at`, `revision`;
- `conversation`, `checklist` — снимки входов;
- `original.answers` и `final.answers` — исходные и актуальные ответы;
- `original_score` и `score` — исходный и актуальный расчёт;
- `metadata`, `input_hash`, `reviews`, `appeals`.

Формат каждого ответа описан в [контракте AI](ai-integration.md). Чтобы создать ручную оценку, передать `result: {"answers":[...]}` и `author` вместе с идентификаторами. Для восьми критериев нужны восемь ответов, даже если часть — N/A. Для нового чек-листа число ответов следует его конфигурации.

`score.total` — число 0–100 или `null`; `score.flags` — список ID нарушений. `score.criteria` содержит `criterion_id`, `value`, `base_weight`, `raw_points`, `points`, `effective_weight`. `score.sections` содержит `{name,total}`. Показывать N/A, `null` и ноль разными состояниями. `needs_review` не меняет балл автоматически.

По клику на цитату найти сообщение из `evidence.message_id`. Для `missing` цитата обозначает контекст отсутствующего действия, для `not_applicable` — обоснование N/A. Не заменять эти типы общим значком «доказано».

## Апелляция

Создание возвращает `{id, evaluation_id, criterion_id, comment, author, status:"open", opened_revision, created_at, decision:null}`. Одна открытая апелляция на критерий; повтор даст 409.

Решение:

```json
{
  "expected_revision": 1,
  "value": "no",
  "reason": "Приветствие есть, представления нет.",
  "reviewer": "QA",
  "evidence_kind": "missing",
  "evidence": [{"message_id": "m2", "quote": "Здравствуйте"}]
}
```

Использовать текущую `revision` оценки, а не автоматически `opened_revision` апелляции. Значение и цитаты проверяются по сохранённой версии чек-листа и транскрипту. При успехе апелляция `resolved`, решение добавлено в `reviews`, `revision` увеличена. При 409 обновить оценку и показать конфликт. Решение с прежним значением означает, что оценка подтверждена; объяснение всё равно сохраняется.

## Ошибки

```json
{"error":{"code":"AI_NOT_CONFIGURED","message":"AI is not connected yet. Import, checklists and manual evaluations are available."}}
```

| HTTP | Код / смысл | Поведение UI |
| --- | --- | --- |
| 422 | `VALIDATION_ERROR`, `INVALID_RESULT`, `INVALID_ANSWER`, `INVALID_EVIDENCE` | Показать ошибку ввода; для схемы есть `details` |
| 404 | `NOT_FOUND` | Обновить список / объяснить отсутствие записи |
| 409 | `VERSION_CONFLICT`, `REVISION_CONFLICT`, `APPEAL_EXISTS`, `APPEAL_CLOSED` | Перезагрузить объект, не перетирать молча |
| 413 | `BODY_TOO_LARGE`, `DATASET_TOO_LARGE` | Предложить меньший набор |
| 503 | `AI_NOT_CONFIGURED`, `ORGANIZER_NOT_CONFIGURED`, `ORGANIZER_CONFIG_ERROR` | Показать недоступную интеграцию |
| 503 | `STORAGE_BUSY` | База занята; перечитать состояние перед повтором |
| 500 | `STORAGE_ERROR`, `INTERNAL_ERROR` | Показать сбой сервера; при неопределённом исходе перечитать историю |
| 502 | `AI_UNAVAILABLE`, `AI_INVALID_RESULT`, ошибки организаторов | Показать неудачную попытку, не создавать успешный результат |
| 504 | `AI_TIMEOUT` | Сообщить о тайм-ауте модели |

Сервер поддерживает сохранённые пакетные задания через `/evaluation-batches`; существующие одиночные запросы продолжают работать. POST создания AI/ручной оценки принимает необязательный `Idempotency-Key`. Ответы содержат `X-Request-ID`, а после резервирования AI-запуска также `X-Execution-ID`. Новые ошибки: 503 AI_BUSY/BATCH_QUEUE_FULL и 409 IDEMPOTENCY_CONFLICT/EVALUATION_IN_PROGRESS/EVALUATION_FAILED/EVALUATION_INTERRUPTED. Формат ошибки прежний. Полные примеры, повторные запросы, восстановление и отчёты: [backend-operations.md](backend-operations.md).

Конфликт `REVISION_CONFLICT` возможен и при открытии апелляции, если оценка изменилась между чтением и записью. Для успешного решения `revision` увеличивается ровно на один. При сбое записи решение и изменение балла откатываются вместе.
