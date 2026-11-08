# Общая версия команды

`main` — общая ветка frontend, backend и AI. Интеграция начиналась с
`main` (`6947035`) с backend-core. В неё вошли
`codex/frontend-next` (`81c9065`) и `codex/ai-gemini` (`a1374ea`).
История сохранена merge-коммитами, исходные ветки не переписываются.
Основной AI подключается к aijuju API; локальная LoRA остаётся отдельным вариантом.
[Настройки и единый запуск](integration.md).

## Получить общий код

Сначала сохраните свои незакоммиченные изменения. В чистой рабочей копии:

```text
git fetch origin
git switch main
git pull --ff-only origin main
```

Если fast-forward невозможен, не выполняйте `reset --hard` или force push:
сначала разберите свои локальные коммиты и изменения с командой.

## Продолжить свою задачу

Для новой задачи создайте ветку от общей версии:

```text
git switch -c codex/my-task origin/main
```

Если продолжаете существующую ветку, находясь в ней, выполните:

```text
git fetch origin
git merge origin/main
```

После разрешения конфликтов запустите подходящие проверки. Не заменяйте целиком
`ai/gemini.py` или фабрику приложения старой копией: в объединённой версии
одновременно работают исправление ответа, проверка цитат, учёт расхода,
ограничение каждого HTTP-вызова и сохраняемые 429-паузы.

## Проверки общего кода

```text
python -m pytest -q
python -m ruff check backend ai scripts tests
python -m ruff format --check backend ai scripts tests
python -m mypy backend ai scripts/rehearse_backend.py
npm --prefix frontend run check
npm --prefix frontend test
python -m unittest discover -s frontend/tests -p "test_*.py"
python -m scripts.check_secrets
```

CI проверяет Python 3.11/3.12, контейнер с сохранением SQLite и frontend.
Автоматические тесты модели используют синтетические ответы; успешный CI
не подтверждает доступность Google или точность на экспертном Ground Truth.

## Запуск и передача результатов

- Локальный frontend с локальным backend: `python frontend/serve.py --api http://127.0.0.1:8000`.
- Frontend с общим сервером: `python frontend/serve.py`; любые записи попадут
  на сервер команды. Для автономных тестов используйте локальный backend.
- Настройки `.env` не перезаписываются при merge. Новые значения примера не
  меняют уже сохранённую модель или ключ автоматически.
- Сервер alwaysdata закреплён за отдельным релизом; push в Git его не обновляет.
  [Порядок обновления и резервные копии](alwaysdata.md).
- DEV-данные, черновые метки и веса LoRA остаются вне Git. Проверенные файлы
  сдачи публикуются отдельно по [инструкции](submission.md).
