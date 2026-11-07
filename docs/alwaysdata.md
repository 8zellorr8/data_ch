# Replimetr на alwaysdata Free

Рабочая версия: **https://replimetr.alwaysdata.net/**.
Swagger: https://replimetr.alwaysdata.net/docs.
Развёрнуто и проверено 7 ноября 2026 года. AI не подключён; в публичном демо
находится только синтетический диалог с ручной оценкой и апелляцией.
Развёрнутый код на момент этой доработки — `4e6f24a`. Новая HTTP-команда работает
с уже существующим серверным маршрутом; передача Evaluator в hosted-фабрику
потребует обновления серверного кода при интеграции AI. Эта доработка не меняет
живую базу и не переключает конфигурацию сайта.

## Что работает без ноутбука

alwaysdata запускает Uvicorn как **Web → Sites → Programme utilisateur**.
Это процесс сайта под управлением хостинга; SSH-сессия для его работы не нужна.
Docker на этом shared-хостинге не используется. Docker-вариант остаётся доступен
для VM: [deployment.md](deployment.md).

Существующий frontend и API обслуживаются одним приложением через
`backend.hosting:create_hosted_app`. Сначала сопоставляются прежние API-маршруты,
затем статические файлы **только из frontend/**. Backend, `.env`, SQLite,
резервные копии и `.git` через этот каталог не публикуются.
В `frontend/api.js` изменён только адрес подключения по умолчанию:
на публичном домене используется текущий origin, на localhost остаётся порт 8000.
Пользовательская настройка адреса по-прежнему имеет приоритет.

## Фактическая конфигурация

| Назначение | Путь / значение |
| --- | --- |
| Аккаунт | replimetr, Free |
| Код | /home/replimetr/apps/replimetr |
| Python | /home/replimetr/venv/bin/python, 3.12.15 при проверке |
| Рабочая SQLite | /home/replimetr/var/replimetr/cqa.sqlite3 |
| Проверенные снимки | /home/replimetr/var/replimetr/backups |
| Сайт | replimetr.alwaysdata.net, site 1101422 |
| Рабочий каталог в панели | apps/replimetr |
| HTTP host при проверке | http20, локальная файловая система XFS |

Команда сайта (порт 8100 выдан именно этому сайту; для нового аккаунта сверить
подсказку панели):

```text
/home/replimetr/venv/bin/python -m uvicorn backend.hosting:create_hosted_app --factory --host :: --port 8100 --workers 1 --no-access-log --timeout-graceful-shutdown 200
```

Переменные в поле **Environnement**:

```dotenv
PYTHON_VERSION=3.12
PYTHONUNBUFFERED=1
PYTHONDONTWRITEBYTECODE=1
CQA_DATABASE_PATH=/home/replimetr/var/replimetr/cqa.sqlite3
CQA_FRONTEND_PATH=/home/replimetr/apps/replimetr/frontend
CQA_BACKUP_DIRECTORY=/home/replimetr/var/replimetr/backups
CQA_BACKUP_INTERVAL_SECONDS=21600
CQA_REQUIRE_LOCAL_SQLITE=1
CQA_CORS_ORIGINS=https://replimetr.alwaysdata.net
CQA_AI_CONCURRENCY=1
```

AI-ключи и URL организаторов здесь не настроены. Реальные разговоры и платную
модель нельзя добавлять в открытое демо до настройки защиты доступа: в приложении
пока нет авторизации. Наличие HTTPS или CORS её не заменяет.
Подготовлены [инструкция и конфигурация reverse proxy](access-control.md);
действующий сайт автоматически не закрывается.

Для скачивания JSON сдачи из локального клона:

```text
python -m backend.submission_remote --api https://replimetr.alwaysdata.net --dataset-id DATASET_UUID --manifest data/submission-dev.json
```

[Манифест, PROD, обработка ошибок и push](submission.md).
При появлении адаптера используйте [hosted-фабрику с Evaluator](ai-integration.md),
сохранив пути БД/копий и параметры команды сайта.

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

Для следующего обновления удобно подготовить новый каталог через SSH:

```text
git clone --single-branch --branch codex/backend-core https://github.com/8zellorr8/data_ch.git /home/replimetr/apps/replimetr-NEW
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
- `frontend_kit.py --api https://replimetr.alwaysdata.net` прошёл 16 HTTP-шагов.
- После перезапуска через панель сохранились набор, оценка 75 → 100,
  ревизия 2 и решение апелляции; в журнале изменился PID Uvicorn.
- Новая AI-оценка возвращает `503 AI_NOT_CONFIGURED`.
- Служебные пути (`/.env`, `/backend/schema.sql`, `/cqa.sqlite3`, `/backups`,
  `/.git/config`) возвращают 404.
- Снимок с тестовым набором, оценкой и апелляцией проверен и восстановлен в
  отдельный `restore-check-20261107.sqlite3`; рабочая база не заменялась.
- В браузере видны подключённый сервер, исходные 75 и итоговые 100 баллов.

## Условия бесплатного размещения

Free: 1 ГБ диска, 256 МБ RAM, 0.25 CPU, личное некоммерческое использование,
домен `*.alwaysdata.net`. Коммерческий продукт потребует подходящего тарифа.
Размещение не требует банковской карты. Не устанавливайте локальную LLM на этот
тариф: памяти мало; будущий облачный AI подключается отдельным адаптером.

В течение первого года нужно входить в **панель администратора** минимум раз
в 120 дней, чтобы аккаунт не признали неактивным. Посещение самого сайта этого
не заменяет. Хостинг и сохранность данных не обещаются бессрочно без обслуживания.
[Правила активности](https://help.alwaysdata.com/en/docs/admin-billing/profile/alerts-notifications/).

Официальные инструкции:
[Python/ASGI](https://help.alwaysdata.com/en/docs/web-hosting/languages/python/configuration/),
[User program](https://help.alwaysdata.com/en/docs/web-hosting/sites/http-servers/user-program/).
