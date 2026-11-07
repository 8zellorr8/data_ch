# Запуск и проверка backend без AI

Этот сценарий проверяет установку, HTTP API и хранение данных. Gemini, локальная
модель, ключи AI и ссылка организаторов не нужны. Ручные оценки помечаются
`origin: "manual"`; синтетические ответы не выдаются за результат настоящей модели.
Новые HTTP/браузерные проверки после деплоя и результат общего прогона описаны
в [backend-readiness.md](backend-readiness.md). Раздел «Зафиксированный результат»
ниже относится к прежнему чистому клону и сохраняется как историческая проверка.

## Подготовка чистого клона

Нужны Git и Python 3.11/3.12; проверка Windows выполнена на Python 3.12.14.
Для private-репозитория нужен доступ через собственную авторизацию GitHub.
Команды выполняются в PowerShell из каталога, где ещё нет `data-ch-check`:

```powershell
git clone --single-branch --branch codex/backend-core https://github.com/8zellorr8/data_ch.git data-ch-check
Set-Location data-ch-check
python --version
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]" -c requirements-lock.txt
.\.venv\Scripts\python.exe -m pip check
Copy-Item .env.example .env
```

Используйте отдельное окно терминала для этой проверки. Не переносите рабочую
`.env`, каталог `data/`, ключи или существующую `.venv`. Пример `.env` оставляет
ссылку организаторов и тарифы пустыми, а SQLite создаётся внутри нового клона.
Если в окружении уже установлены CQA-переменные, они имеют приоритет над `.env`.
В отдельном проверочном терминале их можно убрать только из текущего процесса:

```powershell
Get-ChildItem Env:CQA_* | Remove-Item
```

Постоянные настройки Windows это не меняет. Все команды ниже запускайте из корня
нового клона; каждый терминал должен использовать его `.venv`.

## Запуск настоящего HTTP-сервера

Первый терминал:

```powershell
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --env-file .env --host 127.0.0.1 --port 8000
```

Второй терминал, тот же каталог:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
.\.venv\Scripts\python.exe scripts/frontend_kit.py --api http://127.0.0.1:8000
```

Ожидаемый health: `{"status":"ok","database":"sqlite","ai_configured":false}`.
`database` здесь обозначает используемый тип хранилища, а не самостоятельную
проверку целостности БД. `/docs` открывает Swagger; `/openapi.json` содержит схемы.
`ai_configured` сообщает состояние адаптера, а не результат пробного вызова модели.

Скрипт должен вывести `OK: 16 API steps`. Он обращается по HTTP к запущенному
backend и создаёт синтетический набор, пользовательский чек-лист, ручную оценку
и апелляцию. Проверяет N/A, отдельный флаг и пересмотр: исходный балл 75 сохраняется,
итоговый становится 100, revision — 2. Ответы 409/422/404 в предусмотренных шагах
сценария ожидаемы. Результаты и ID находятся в `data/frontend-kit/responses.json`.

При повторном запуске скрипта появляются новые синтетические записи; старые не
удаляются. Без `--api` скрипт использует временную БД и TestClient — это отдельный
режим, который не проверяет запуск Uvicorn и сеть. Полная приёмка браузерного UI
также остаётся отдельной задачей frontend-участника.

## Проверка отключённых интеграций

Во втором терминале прочитайте ID созданных данных и отправьте запросы:

```powershell
$bundle = Get-Content data/frontend-kit/responses.json -Raw -Encoding UTF8 | ConvertFrom-Json
$conversationId = $bundle.variables.conversation_id
$evaluationId = $bundle.variables.evaluation_id
$evaluationRequest = @{ conversation_id = $conversationId } | ConvertTo-Json
Invoke-RestMethod http://127.0.0.1:8000/api/v1/evaluations -Method Post -ContentType 'application/json' -Body $evaluationRequest
```

Последний запрос **должен вернуть 503** с `error.code=AI_NOT_CONFIGURED`.
PowerShell может показать это как исключение HTTP-запроса; проверьте JSON ошибки
в выводе или повторите запрос через Swagger. Не подменяйте этот ответ успехом.

| Действие | Ожидаемый результат без AI |
| --- | --- |
| Новый POST `/api/v1/evaluations` | 503 AI_NOT_CONFIGURED |
| Новый POST `/api/v1/evaluation-batches` с допустимым телом | 503 AI_NOT_CONFIGURED, пакет не создаётся |
| POST `/api/v1/datasets/import-organizer` при пустой ссылке | 503 ORGANIZER_NOT_CONFIGURED |
| Импорт собственного JSON, чек-листы, ручная оценка, апелляции, экспорт | Доступны |
| POST `/api/v1/reports/quality` с явным ручным эталоном и ai_id=null | 200, coverage=0, mae=null, причина MISSING_AI |
| GET `/api/v1/evaluation-batches` на новом клоне | Пустой список |

Повтор ранее успешного запроса с Idempotency-Key может вернуть сохранённый ответ
даже без AI; это чтение истории, а не новый вызов. Проверка отсутствия AI выше
относится к новым запросам в чистом окружении.

## Перезапуск и резервная копия

Сохраните `$evaluationId` во втором терминале. Остановите сервер Ctrl+C в первом
и запустите той же командой, из того же каталога. Во втором выполните:

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/api/v1/evaluations/$evaluationId"
```

Должны сохраниться manual, revision=2, original_score.total=75, score.total=100,
история reviews и resolved-апелляция. Каталог и `CQA_DATABASE_PATH` менять нельзя:
иначе сервер откроет другую SQLite-базу. Повторный запуск frontend_kit для этой
проверки не нужен — он создал бы новый набор вместо чтения сохранённого.

Для копирования синтетической базы, пока сервер работает:

```powershell
.\.venv\Scripts\python.exe -m backend.backup create --database data/cqa.sqlite3 --output data/no-ai-snapshot.sqlite3
.\.venv\Scripts\python.exe -m backend.backup verify --backup data/no-ai-snapshot.sqlite3
.\.venv\Scripts\python.exe -m backend.backup restore --backup data/no-ai-snapshot.sqlite3 --output data/no-ai-restored.sqlite3
```

Выходные файлы должны быть новыми; повтор с теми же путями отклоняется.
Команда restore не переключает работающий сервер на новую базу. Правила публикации,
проверки SHA-256 и приостановки незавершённых заданий — в [backup-restore.md](backup-restore.md).

## Проверки проекта

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check backend scripts tests
.\.venv\Scripts\python.exe -m ruff format --check backend scripts tests
.\.venv\Scripts\python.exe -m mypy backend
```

Интеграционные тесты используют временные синтетические базы и тестовые Evaluator.
Это проверки контрактов, транзакций и ошибок; они не измеряют точность Gemini.

## Зафиксированный результат

7 ноября 2026 года backend-код `32512e1` проверен в отдельном клоне с GitHub,
на Windows / Python 3.12.14, с новой `.venv` и установкой по requirements-lock.txt.
Рабочие данные и настройки не копировались. Изменения этой документации не меняют
проверенный backend-код, контракты, зависимости и тесты.

- Установка и pip check прошли; модуль backend загружался из нового клона.
- Отдельный процесс Uvicorn: health, Swagger, OpenAPI и CORS preflight прошли.
- 16 HTTP-шагов, исходная/итоговая ручная оценка, апелляция и экспорт прошли.
- Отключённые AI/организаторы возвращали документированные 503; в БД не появились
  AI-оценки, AI-запуски или пакеты после отклонённых запросов.
- Quality без AI вернул MISSING_AI, нулевое покрытие и неопределённый MAE.
- После остановки и запуска процесса оценка, чек-лист, апелляция и экспорт
  сохранились; при сравнении экспорта исключалось только новое exported_at.
- Backup работающей синтетической базы, verify и restore в новый файл прошли.
- 203 теста, Ruff, форматирование и mypy прошли в новом окружении.

Браузерный frontend, доступность записей звонков, реальный Gemini и его точность
этим прогоном не проверялись. Здесь нет утверждения о запуске на втором физическом
компьютере: проверен отдельный клон и окружение на текущем Windows-компьютере.

## Если запуск не получился

- `No module named backend`: проверьте текущий каталог и выполните установку
  именно через `.venv\Scripts\python.exe`, затем этой же командой запускайте Uvicorn.
- Порт занят: выберите свободный, например 8001, и замените порт также в `--api`
  и адресах HTTP-запросов; чужой сервер для освобождения порта не останавливайте.
- После перезапуска пустая история: проверьте каталог запуска и CQA_DATABASE_PATH.
- Браузер блокирует запрос: сверьте точный origin с CQA_CORS_ORIGINS и перезапустите
  backend; `localhost` и `127.0.0.1` — разные origins.
- `503 AI_NOT_CONFIGURED` при новом запросе оценки — ожидаемое состояние до
  подключения адаптера, а не повод менять формулу оценки или добавлять демо-ответ.
