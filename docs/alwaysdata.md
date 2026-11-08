# Replimetr на alwaysdata Free

> Обновление документации 8 ноября не выполняет deploy. Общий main после
> объединения веток новее указанного релиза; push не переключает сайт.
> Проверка сдачи с локального компьютера: [submission_check](submission.md#проверка-готовности-перед-выгрузкой).

Рабочая версия: **https://replimetr.alwaysdata.net/**.
Swagger: https://replimetr.alwaysdata.net/docs.
Последнее подтверждённое обновление сервера — 7 ноября 2026 года, commit `d88ba31`, Gemini 3.6
Flash подключён через окружение сайта. База и история сохранены. В публичном демо
только синтетические диалоги, ручная оценка и апелляция.

**Текущий предел проверки:** Google вернул 429 из-за исчерпанной суточной квоты
этого проекта — 20 запросов для Gemini 3.6 Flash. Диагностический ответ содержал
`retryDelay=53045s` (около 14 часов 44 минут). Это время ожидания на момент ответа,
а не таймер, который начинается заново при чтении документа. Успешная оценка
через хостинг пока не подтверждена; успешная локальная синтетическая проверка
описана в [ai-readiness.md](ai-readiness.md).

## Что работает без ноутбука

alwaysdata запускает Uvicorn как **Web → Sites → Programme utilisateur**.
Это процесс сайта под управлением хостинга; SSH-сессия для его работы не нужна.
Docker на этом shared-хостинге не используется. Docker-вариант остаётся доступен
для VM: [deployment.md](deployment.md).

Существующий frontend и API обслуживаются одним приложением через
`ai.app:create_hosted_app`, передающую Gemini-адаптер в существующую hosted-фабрику.
Сначала сопоставляются прежние API-маршруты,
затем статические файлы **только из frontend/**. Backend, `.env`, SQLite,
резервные копии и `.git` через этот каталог не публикуются.
В `frontend/api.js` изменён только адрес подключения по умолчанию:
в последнем развёрнутом релизе на публичном домене используется текущий origin,
на localhost остаётся порт 8000. В новом main локальный frontend использует свой
origin и relay frontend/serve.py; для локального backend задайте --api явно.
Пользовательская настройка адреса по-прежнему имеет приоритет.

## Исходная конфигурация сайта

| Назначение | Путь / значение |
| --- | --- |
| Аккаунт | replimetr, Free |
| Код | /home/replimetr/apps/releases/d88ba31fff2ba8e7d8a8732a5bd43406364ce086 |
| Python | /home/replimetr/venv/bin/python, 3.12.15 при проверке |
| Рабочая SQLite | /home/replimetr/var/replimetr/cqa.sqlite3 |
| Проверенные снимки | /home/replimetr/var/replimetr/backups |
| Сайт | replimetr.alwaysdata.net, site 1101422 |
| Рабочий каталог в панели | apps/releases/d88ba31fff2ba8e7d8a8732a5bd43406364ce086 |
| Предыдущий релиз | /home/replimetr/apps/releases/4ebace2d2884bf04c0baadb396f7b5d1899303f3 |
| HTTP host при проверке | http20, локальная файловая система XFS |

Команда сайта (порт 8100 выдан именно этому сайту; для нового аккаунта сверить
подсказку панели):

```text
/home/replimetr/venv/bin/python -m uvicorn ai.app:create_hosted_app --factory --host :: --port 8100 --workers 1 --no-access-log --timeout-graceful-shutdown 200
```

Переменные в поле **Environnement**:

```dotenv
PYTHON_VERSION=3.12
PYTHONUNBUFFERED=1
PYTHONDONTWRITEBYTECODE=1
CQA_DATABASE_PATH=/home/replimetr/var/replimetr/cqa.sqlite3
CQA_FRONTEND_PATH=/home/replimetr/apps/releases/d88ba31fff2ba8e7d8a8732a5bd43406364ce086/frontend
CQA_BACKUP_DIRECTORY=/home/replimetr/var/replimetr/backups
CQA_BACKUP_INTERVAL_SECONDS=21600
CQA_REQUIRE_LOCAL_SQLITE=1
CQA_CORS_ORIGINS=https://replimetr.alwaysdata.net
CQA_AI_CONCURRENCY=1
CQA_GEMINI_MODEL=gemini-3.6-flash
CQA_GEMINI_FALLBACK_MODEL=
CQA_GEMINI_PRIMARY_TIMEOUT_SECONDS=40
CQA_GEMINI_THINKING_LEVEL=low
CQA_GEMINI_ALLOW_VOICE_CALLS=0
```

`GEMINI_API_KEY` задан отдельно в этом же поле; его значение не публикуется.
URL организаторов в исходной конфигурации не настроен. Для версии с AI для
жюри включите [защиту hosted-приложения](access-control.md) и принудительный HTTPS.

## Настройка aijuju для жюри

В окружении серверного процесса задайте параметры aijuju из
[инструкции интеграции](integration.md) и пару
`CQA_HOSTED_HTTP_USER` / `CQA_HOSTED_HTTP_PASSWORD`. Ключ и пароль храните
в отдельном файле вне каталога релиза и статики, с правами `600`.
Путь `CQA_LLM_BUDGET_PATH` указывает на постоянную SQLite учёта токенов на
HTTP-хосте. При переносе используйте согласованный снимок счётчика и ту же
политику лимита. Для демо можно перенести отдельный ограниченный счётчик,
сохранив его историю.

После переключения релиза проверьте редирект HTTP → HTTPS, ответ 401 без
пароля и работу `/api/v1/ai/status` после входа. Затем выполните один запрос
на синтетическом диалоге и проверьте сохранённую оценку. Передавайте жюри
логин сайта, пароль и инструкцию «Диалоги → учебный набор → Оценить с AI».

Для скачивания JSON сдачи из локального клона:

```text
python -m backend.submission_remote --api https://replimetr.alwaysdata.net --dataset-id DATASET_UUID --manifest data/submission-dev.json
```

[Манифест, PROD, обработка ошибок и push](submission.md).
Контракт [hosted-фабрики с Evaluator](ai-integration.md) сохраняет прежние пути
БД/копий и API-маршруты.

## Особенность SQLite: HTTP и SSH — разные серверы

При проверке `ssh-replimetr.alwaysdata.net` подключал к `ssh2`, где домашняя папка
видна по NFS. Сам сайт исполняется на `http20`, где та же папка находится на XFS.
Режим WAL требует корректного локального доступа к файлу и общей памяти.
[Ограничения SQLite WAL](https://www.sqlite.org/wal.html).

Поэтому **не открывайте работающую SQLite из SSH**, включая `sqlite3`,
`backend.benchmark`, `backend.submission_cli` и `backend.backup create`.
Для работы с живыми данными используйте HTTP API, в том числе отчёты и
`POST /api/v1/datasets/{dataset_id}/submission`. Не запускайте второй backend
из SSH с тем же `CQA_DATABASE_PATH`.

`CQA_REQUIRE_LOCAL_SQLITE=1` проверяет файловую систему в самом процессе сайта
до инициализации БД и отказывает в запуске на NFS или неизвестном типе хранения.
После миграции аккаунта хостингом эту проверку нельзя отключать ради запуска:
сначала повторно проверить размещение данных.

## Резервные копии

`HostedSnapshots` вызывает существующий SQLite backup API **на HTTP host**:
при старте, раз в 6 часов во время работы приложения и при штатной остановке
после закрытия обработчика заданий. Операции выполняются вне event loop.
Сохраняются восемь последних завершённых пар `hosted-*.sqlite3` + манифест.
Посторонние файлы, ручные копии и символические ссылки не удаляются.

Готовый снимок — самостоятельная закрытая база в DELETE journal mode с SHA-256
и проверкой структуры. Такие **снимки**, в отличие от живой WAL-базы, можно
скачивать по SFTP и проверять через CLI. HTTP-доступа к ним нет.
При ошибке записи/места приложение пишет `Hosted snapshot failed` в журнал;
неудачная копия не объявляется успешной. Мониторинг ошибок и свободного места
остаётся обязанностью владельца. Жёсткое завершение процесса не гарантирует
заключительный снимок; рабочий файл и периодические копии сохраняются отдельно.

Проверка и восстановление копии в **новый файл** (подставить фактическое имя):

```text
/home/replimetr/venv/bin/python -m backend.backup verify --backup /home/replimetr/var/replimetr/backups/SNAPSHOT.sqlite3
/home/replimetr/venv/bin/python -m backend.backup restore --backup /home/replimetr/var/replimetr/backups/SNAPSHOT.sqlite3 --output /home/replimetr/var/replimetr/restored-NEW.sqlite3
```

Эти команды запускаются из каталога кода. Для перехода на восстановленную базу
получить новый файл, поменять `CQA_DATABASE_PATH` в настройках сайта и перезапустить
сайт. Старую рабочую БД не удалять и не перезаписывать. Восстановление блокирует
автоматический повтор незавершённых AI-вызовов.

Дополнительно alwaysdata ежедневно копирует файлы аккаунта и хранит резервные
данные в другом дата-центре; тариф Free даёт три дня истории. Завершённые
снимки приложения попадают и в эту копию. Это не заменяет проверку восстановления.
[План резервирования](https://help.alwaysdata.com/en/docs/technical-specifications/drp/),
[лимиты тарифов](https://help.alwaysdata.com/en/docs/admin-billing/billing/public-cloud-prices/).

## Обновление кода

Первый деплой загрузил только исходники backend/frontend и файлы зависимостей.
Локальная `.env`, рабочие данные и SSH-ключи не переносились в приложение.

При интеграции Gemini точный архив GitHub commit `4ebace2` был загружен отдельной
одноразовой командой сайта, проверен по SHA-256 и распакован в новый каталог.
SHA-256 архива: `ea5817f7cc7d1304bb5ebb00eed3535be894b28696bae106154f6098b1ebb5d4`.
После публикации команда заменена обычным запуском Uvicorn, указанным выше:
при перезапусках GitHub не нужен, код повторно не скачивается. Зависимости не менялись.
Git push сам по себе не обновляет этот сервер.

Для следующего обновления удобно подготовить новый каталог через SSH:

```text
git clone --single-branch --branch main https://github.com/8zellorr8/data_ch.git /home/replimetr/apps/replimetr-NEW
cd /home/replimetr/apps/replimetr-NEW
/home/replimetr/venv/bin/python -m pip install --no-cache-dir -c requirements-lock.txt .
```

Проверить нужный commit, затем в **Web → Sites** поменять рабочий каталог и
`CQA_FRONTEND_PATH` на новый каталог. Путь SQLite и папку копий оставить прежними.
Сохранить и проверить `/health`, чтение наборов и оценок. Старый каталог сохранить
для отката. При изменении зависимостей для полноценного отката потребуется также
восстановить их прежние версии или подготовить отдельное venv для нового релиза.
Загрузка кода/зависимостей в SSH разрешена; запуск там приложения с живой БД — нет.

## Проверенный результат

- HTTPS, главная страница, `/docs`, `/health` и чтение чек-листов возвращают 200.
- При первоначальном деплое без AI `frontend_kit.py --api https://replimetr.alwaysdata.net`
  прошёл 16 HTTP-шагов. Этот сценарий ожидает отсутствие AI; после подключения
  Gemini он не запускался повторно как проверка живой модели.
- После перезапуска через панель сохранились набор, оценка 75 → 100,
  ревизия 2 и решение апелляции. После дополнительного перезапуска сохранился
  и новый синтетический набор для проверки Gemini.
- `/health` возвращает `ai_configured=true`. Это наличие конфигурации, не гарантия
  свободной квоты. Новая AI-оценка получила `502 AI_UNAVAILABLE` / HTTP 429 Google.
  У синтетического диалога по-прежнему ноль успешных оценок.
- Служебные пути (`/.env`, `/backend/schema.sql`, `/ai/gemini.py`, `/cqa.sqlite3`,
  `/.git/config`) возвращают 404.
- При первоначальном деплое снимок с тестовым набором, оценкой и апелляцией проверен и восстановлен в
  отдельный `restore-check-20261107.sqlite3`; рабочая база не заменялась.
- В браузере видны подключённый сервер, исходные 75 и итоговые 100 баллов.

## Условия бесплатного размещения

Free: 1 ГБ диска, 256 МБ RAM, 0.25 CPU, личное некоммерческое использование,
домен `*.alwaysdata.net`. Коммерческий продукт потребует подходящего тарифа.
Размещение не требует банковской карты. Не устанавливайте локальную LLM на этот
тариф: памяти мало; облачный AI подключён отдельным адаптером.

В течение первого года нужно входить в **панель администратора** минимум раз
в 120 дней, чтобы аккаунт не признали неактивным. Посещение самого сайта этого
не заменяет. Хостинг и сохранность данных не обещаются бессрочно без обслуживания.
[Правила активности](https://help.alwaysdata.com/en/docs/admin-billing/profile/alerts-notifications/).

Официальные инструкции:
[Python/ASGI](https://help.alwaysdata.com/en/docs/web-hosting/languages/python/configuration/),
[User program](https://help.alwaysdata.com/en/docs/web-hosting/sites/http-servers/user-program/).
