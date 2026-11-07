# Docker и размещение backend

Конфигурация запускает только FastAPI: один Linux-контейнер, один процесс Uvicorn,
встроенный обработчик пакетов и SQLite в отдельном Docker volume. Frontend и AI
в образ не входят. API, формула баллов и поведение при отсутствии модели сохраняются.

Нужны Docker Engine с Compose **2.30+** на Linux либо запущенный Docker Desktop
с Linux containers на Windows/macOS. Python на хосте нужен только для необязательных
тестов. Серверу нужен постоянный локальный диск; эта конфигурация не предназначена
для Vercel Functions, Cloudflare Workers или хостинга с временной файловой системой.

## Быстрый запуск без AI

Из корня репозитория, ветка `codex/backend-core`:

```text
docker version
docker compose version
docker compose up --build --detach --wait --wait-timeout 120
docker compose ps
```

Без `.env.docker` работают настройки по умолчанию. Откройте
[health](http://127.0.0.1:8000/health) и [Swagger](http://127.0.0.1:8000/docs).
Ожидается `{"status":"ok","database":"sqlite","ai_configured":false}`.
Автооценка возвращает `503 AI_NOT_CONFIGURED`; Docker не подключает Gemini.

Образ использует Python 3.12 slim / Debian bookworm и устанавливает только runtime
зависимости из pyproject с ограничениями requirements-lock.txt. Тег базового образа
получает обновления Python 3.12; это не фиксация по digest. Новые сборки проверяются CI.
`.dockerignore` разрешает только исходники backend и нужные ресурсы: `.env`, базы,
резервные копии, Git, frontend и тестовые данные в контекст сборки не попадают.

Контейнер работает от UID/GID `10001`, корневая файловая система доступна только
для чтения, `/tmp` временный. Healthcheck проверяет HTTP-доступность `/health`;
он не проверяет модель, целостность базы или доступность внешних API. `unhealthy`
сам по себе не перезапускает контейнер; `unless-stopped` срабатывает при завершении
процесса и запуске Docker, если контейнер не был остановлен вручную.

## Конфигурация

Создайте отдельный файл, не перезаписывая имеющийся. PowerShell:

```powershell
if (-not (Test-Path -LiteralPath .env.docker)) { Copy-Item .env.docker.example .env.docker }
```

Linux/macOS:

```sh
test -e .env.docker || cp .env.docker.example .env.docker
chmod 600 .env.docker
```

Редактируйте `.env.docker`, затем применяйте изменения через
`docker compose up --detach --wait`. Обычный `restart` не обновляет окружение.
Файл исключён из Git и образа. Он передаётся контейнеру в режиме `raw`: пишите
`KEY=value` **без кавычек, подстановок `${...}` и комментариев после значения**.
Знаки `$` в подписанном URL сохраняются буквально. Не показывайте полный вывод
`docker compose config` или `docker inspect`: он может содержать секреты.
Для проверки конфигурации без значений используйте `docker compose config --quiet`.

| Переменная | Значение / назначение |
| --- | --- |
| CQA_DATABASE_PATH | `/var/lib/replimetr/cqa.sqlite3`; при восстановлении можно выбрать новое имя **в этой же папке** |
| CQA_CORS_ORIGINS | Точные origin фронта через запятую, например `https://your-project.pages.dev`, без пути и завершающего `/` |
| CQA_ORGANIZER_API_URL | Пусто для ручного демо; полная подписанная ссылка хранится только на сервере |
| CQA_EVALUATION_TIMEOUT | 180 секунд по умолчанию |
| CQA_DATABASE_TIMEOUT | 15 секунд ожидания SQLite |
| CQA_AI_CONCURRENCY | 2 параллельных вызова на процесс; это не количество процессов Uvicorn |
| CQA_BATCH_QUEUE_LIMIT | 1000 элементов очереди |
| CQA_TARIFFS_PATH | Пусто либо доступный контейнеру путь к JSON тарифов; путь хоста сам по себе не монтируется |

Обычная `.env` для нативного запуска не передаётся приложению в контейнере.
Переменная **Compose** `REPLIMETR_PORT` задаёт порт хоста (по умолчанию 8000):
в PowerShell `$env:REPLIMETR_PORT = '8001'`, в Linux `export REPLIMETR_PORT=8001`.
Её задают в терминале или обычной `.env`, не в runtime-файле `.env.docker`.
Внутри контейнера порт всегда 8000. Публикация всегда на `127.0.0.1`.

## Где сохраняются данные

Compose project имеет постоянное имя `replimetr`, том называется
`replimetr_backend-data` и монтируется в `/var/lib/replimetr`. Весь каталог,
включая `cqa.sqlite3`, `-wal` и `-shm`, находится на одном томе. Новый пустой том
получает права UID/GID `10001` из образа; запуск от root не нужен.

- `docker compose restart backend`, пересборка и пересоздание контейнера сохраняют том.
- `docker compose down` удаляет контейнер и сеть, но сохраняет том. Следующий `up`
  подключает его снова. Не меняйте имя проекта или `-p` для рабочего сервиса.
- **`docker compose down --volumes` удаляет данные.** Не используйте его для обновления.
- Том находится на конкретном Docker-хосте. Удаление тома, диска, VM или сброс Docker
  Desktop могут уничтожить данные. Volume не заменяет резервную копию на другом хосте.
- Локальная `data/cqa.sqlite3` автоматически не переносится. Для переноса сначала
  остановите старый сервер и создайте согласованную копию через backup CLI; переносите
  копию вместе с манифестом и восстанавливайте под новым именем.

Используйте локальную файловую систему Docker-хоста; сетевые NFS/SMB-папки не
подходят для этой конфигурации SQLite WAL. Оставляйте один экземпляр backend и
один Uvicorn worker. Горизонтальное масштабирование и несколько Docker-хостов
для одного файла БД здесь не настроены.

## Проверка и обслуживание

С установленным Python-окружением проекта запустите существующий HTTP-сценарий:

```text
python scripts/frontend_kit.py --api http://127.0.0.1:8000
docker compose restart backend
```

Сценарий создаёт только синтетические данные: 16 шагов, ручная оценка 75,
апелляция, итог 100 и ревизия 2. После перезапуска проверьте те же ID через API.
Для полной изолированной проверки контейнеризации:

```text
python -m scripts.docker_smoke
```

Скрипт создаёт временный контекст без ваших конфигураций и данных, отдельный
Compose project с уникальным именем, случайный loopback-порт и собственный том.
Проверяет OpenAPI, ручной сценарий, CORS, `AI_NOT_CONFIGURED`, SQLite WAL, сохранение
истории после restart и удаления/создания контейнера, backup и запуск с восстановленной
копии. После проверки удаляет только собственные временные ресурсы. Настоящий AI
не вызывается. Эта же проверка запускается отдельной задачей GitHub Actions.

Обновление сервера после резервного копирования:

```text
git pull --ff-only origin codex/backend-core
docker compose build --pull
docker compose up --detach --wait --wait-timeout 120
docker compose ps
docker compose logs --tail 100 backend
```

Логи ротируются: до трёх файлов по 10 MB. `docker compose stop backend` оставляет
контейнер остановленным до явного `up`/`start`. Дайте остановке завершиться:
Compose ждёт до 240 секунд, Uvicorn — до 200 секунд на активные HTTP-запросы.
Увеличивая тайм-аут AI, согласуйте эти два лимита и тайм-аут reverse proxy.
Завершение процесса может прервать фоновый вызов; graceful shutdown не обещает
завершения всех пакетов. Прерванные running-задания автоматически не повторяются,
pending после обычного перезапуска могут продолжиться по существующим правилам.

## Резервная копия и восстановление

Создайте согласованный снимок работающей SQLite. Имя каждой копии должно быть новым:

```text
docker compose exec -T backend python -c "from pathlib import Path; Path('/var/lib/replimetr/backups').mkdir(exist_ok=True)"
docker compose exec -T backend python -m backend.backup create --database /var/lib/replimetr/cqa.sqlite3 --output /var/lib/replimetr/backups/backup-001.sqlite3
docker compose exec -T backend python -m backend.backup verify --backup /var/lib/replimetr/backups/backup-001.sqlite3
docker compose cp backend:/var/lib/replimetr/backups/backup-001.sqlite3 ./backup-001.sqlite3
docker compose cp backend:/var/lib/replimetr/backups/backup-001.sqlite3.manifest.json ./backup-001.sqlite3.manifest.json
```

Если рабочий `CQA_DATABASE_PATH` менялся, подставьте его в `--database`.
Пару файлов храните также вне сервера. Не копируйте живой SQLite-файл командой `cp`:
последние изменения могут находиться в WAL. Публичного HTTP-доступа к базе нет.

Восстановление уже находящейся в томе копии в **новый** файл:

```text
docker compose stop backend
docker compose run --rm --no-deps backend python -m backend.backup restore --backup /var/lib/replimetr/backups/backup-001.sqlite3 --output /var/lib/replimetr/restored-001.sqlite3
```

Эта одноразовая команда не запускает FastAPI или обработчик пакетов. CLI проверяет
SHA-256, integrity_check, внешние ключи и чтение истории; приостанавливает
незавершённые задания для ручной проверки. Существующий файл назначения отвергается.
После успеха измените в `.env.docker` только
`CQA_DATABASE_PATH=/var/lib/replimetr/restored-001.sqlite3` и выполните:

```text
docker compose up --detach --wait --wait-timeout 120
```

Старая рабочая база сохраняется. При ошибке восстановления не переключайте путь.
Подробности, включая безопасный перенос копий: [backup и restore](backup-restore.md).

## Размещение на Linux-сервере

1. Подготовьте VM с постоянным диском и установите
   [Docker Engine и Compose](https://docs.docker.com/engine/install/).
   Docker должен запускаться после перезагрузки ОС. Автозапуск процесса контейнера
   задан в Compose; он не заменяет автозапуск самого Docker.
2. Клонируйте репозиторий, без токена в URL/командах:
   `git clone --single-branch --branch codex/backend-core https://github.com/8zellorr8/data_ch.git`.
   Перейдите в `data_ch` и выполните быстрый запуск выше.
3. Проверьте `curl --fail http://127.0.0.1:8000/health` на сервере. Для личной проверки
   с ноутбука используйте `ssh -L 8000:127.0.0.1:8000 USER@SERVER` и откройте локальный
   Swagger. Приложение продолжает работать на сервере после выхода из SSH.
4. Для публичного HTTPS направьте DNS API-домена на сервер и настройте reverse proxy
   на **хосте**, например [Caddy](https://caddyserver.com/docs/quick-starts/reverse-proxy).
   Минимальная конфигурация уже установленного Caddy (замените домен своим):

   ```caddyfile
   api.example.com {
       reverse_proxy 127.0.0.1:8000
   }
   ```

   Для автоматического сертификата домен должен разрешаться на сервер, а необходимые
   порты HTTP/HTTPS — быть доступны. Порт 8000 в интернет не открывайте. Конфигурация
   TLS, домен и защита доступа не создаются командой `docker compose up`.
5. В `.env.docker` задайте точный HTTPS-origin размещённого frontend и примените
   конфигурацию. Передайте frontend-участнику HTTPS-адрес API; его надо указать
   в настройках интерфейса вместо `127.0.0.1`. CORS проверяйте с реального origin.

В приложении пока нет авторизации. Перед публикацией реальных диалогов и платного
AI ограничьте доступ на reverse proxy/VPN/шлюзе идентификации; CORS не является
авторизацией. Публичное демо без такой защиты должно использовать только синтетические
данные. Настройка пользовательских ролей, frontend и Gemini не входит в этот Docker-набор.

Справка: [Docker volumes](https://docs.docker.com/engine/storage/volumes/),
[Compose env_file и параметры сервиса](https://docs.docker.com/reference/compose-file/services/).

## Текущий бесплатный хостинг

Replimetr опубликован на alwaysdata нативно, через User program, без Docker.
Фактические пути, запуск на HTTP host с локальным XFS, запрет открывать живую
WAL-базу с SSH host и проверенный порядок восстановления: [alwaysdata.md](alwaysdata.md).
