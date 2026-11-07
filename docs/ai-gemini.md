# Участник 3: оценка чатов и подготовка LoRA

## 1. Запустить рабочую оценку через Gemini

Адаптер `ai.gemini.GeminiEvaluator` принимает диалог и **текущую версию** чек-листа от backend, просит Gemini вернуть JSON, затем проверяет каждый ответ и точность цитат. Балл вычисляет backend. По умолчанию используется `gemini-3.8-flash` с `thinkingLevel=low` для скорости; `medium` можно выбрать после сравнения качества. [Модель](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash) поддерживает структурированный JSON и уровни thinking.

1. Создать API-ключ в [Google AI Studio](https://aistudio.google.com/apikey).
2. Скопировать `.env.example` в `.env` и вписать `GEMINI_API_KEY=...` **только в локальный файл**.
3. Запустить из корня репозитория:

   ```powershell
   .\.venv\Scripts\python.exe -m uvicorn ai.app:app --env-file .env --host 127.0.0.1 --port 8000
   ```

4. Открыть <http://127.0.0.1:8000/docs>. `/health` должен вернуть `ai_configured: true`. Импортировать синтетический набор через `scripts/import_dataset.py examples/synthetic-dataset.json`, затем вызвать `POST /api/v1/evaluations` с внутренним `conversation_id` из списка диалогов, `checklist_id: "freedom-broker"`, `checklist_version: 1`.

Для сервера с frontend, очередью и резервными копиями используйте hosted-фабрику:

```text
python -m uvicorn ai.app:create_hosted_app --factory
```

Ей нужны те же `CQA_*` переменные и `CQA_FRONTEND_PATH`, что и
`backend.hosting:create_hosted_app`. Ключ Gemini читается из окружения при
создании приложения; публичный сервер остаётся без AI, пока ключ там не настроен.

Настоящие DEV-диалоги не включены в Git. Не отправляйте их в бесплатный облачный API, пока команда не согласовала обработку данных: [условия free tier](https://ai.google.dev/gemini-api/docs/pricing) предусматривают использование контента для улучшения продуктов. Звонки дополнительно закрыты настройкой `CQA_GEMINI_ALLOW_VOICE_CALLS=0`; даже при разрешении отправляется только транскрипт, не аудио. Бесплатный тариф и лимиты зависят от проекта; проверить их можно в AI Studio.

Если ключа нет, backend продолжает поддерживать импорт и ручные оценки. Ответы модели, которые нарушают схему, шкалу или содержат вымышленную цитату, не сохраняются как успешная оценка.

## 2. Сделать эталонную разметку

`invoke.json` содержит диалоги, но не правильные ответы по критериям. Без ручной разметки LoRA не узнает, какую оценку считать верной.

```powershell
.\.venv\Scripts\python.exe -m ai.labels prepare "C:\Users\erken\Downloads\Doomsday\invoke.json" --per-language 10
```

Команда создаёт 30 черновиков: по 10 чатов RU/KZ/EN, в `data/labels/drafts/`. Файлы остаются локально; `data/` исключён из Git. Нумерация `m1`, `m2` и т. д. даёт точные ссылки на реплики. В каждом файле заполняйте `result.answers` для всех восьми критериев: `value`, короткое `explanation`, `evidence_kind` и **дословную** `quote` с верным `message_id`. Для `na` нужен `na_reason`; для `needs_review=true` нужен `limitation`. Для шкалы указывайте число, не строку. Флаг `gross: yes` означает нарушение.

Первые 30 чатов — пилот. Сверяйте спорные случаи с чек-листом и вторым участником команды. Затем можно подготовить остальные чаты в отдельной папке (`--per-language 20 --out ...`) и разметить их. Сохраните часть размеченных примеров для независимой проверки; команда `build` автоматически откладывает каждый пятый чат каждого языка.

После заполнения и проверки каждого черновика человеком установите
`"annotation_status": "reviewed"`, затем выполните:

```powershell
.\.venv\Scripts\python.exe -m ai.labels build
```

Скрипт проверит формат, значения и точные цитаты. Только при полной валидности он создаст `data/labels/sft/train.jsonl` и `validation.jsonl` в формате conversational prompt/completion для [TRL SFTTrainer](https://huggingface.co/docs/trl/sft_trainer). Эти файлы тоже содержат тексты диалогов и не должны попадать в Git или публичный Hugging Face Hub.

В этой рабочей копии есть отдельные 15 предварительно размеченных чатов в
`data/labels/seed-drafts/`. Они имеют `annotation_status: "provisional"` и не являются
экспертным эталоном. Для пробного локального обучения их можно собрать явно:

```powershell
.\.venv\Scripts\python.exe -m ai.labels build --drafts data/labels/seed-drafts --out data/labels/seed-sft --allow-provisional
```

Получится 12 примеров для обучения и 3 для проверки. Для оценки качества
потребуется независимая экспертная проверка меток и более широкий набор.

## 3. Экспериментальное локальное обучение LoRA

Gemini доступен через API и не является базовой моделью для этого скрипта. Для LoRA используется открытая `Qwen/Qwen3-0.6B` по умолчанию; можно указать другую совместимую Qwen3 модель. Скрипт повторяет порядок из [PEFT Quicktour](https://huggingface.co/docs/peft/quicktour): загрузить базовую модель, задать `LoraConfig`, обернуть через `get_peft_model`, обучить и сохранить только адаптер. Данные не загружаются на Hub. Это отдельный опытный путь; обученный адаптер пока не подключён к backend.

Создайте **отдельное** окружение для обучения: новые библиотеки ML не должны менять
зависимости рабочего backend. Например, с `uv`:

```powershell
uv venv --python 3.12 .venv-train
uv pip install --python .venv-train\Scripts\python.exe -e .
uv pip install --python .venv-train\Scripts\python.exe torch==2.7.1 transformers datasets peft trl accelerate
```

Для CUDA-обучения замените обычную сборку `torch` на вариант по
[инструкции PyTorch](https://pytorch.org/get-started/locally/). После разметки и `build`:

```powershell
.\.venv-train\Scripts\python.exe -m ai.train_lora
```

Выход: `data/lora/qwen3-cqa/adapter/`. Скрипт использует CUDA, если она доступна;
`--device cpu` позволяет технически проверить обучение без неё, но работает медленнее.
Скрипт останавливается,
если пример будет обрезан длиной `--max-length`. Для пробного набора укажите
`--train data/labels/seed-sft/train.jsonl --validation data/labels/seed-sft/validation.jsonl`.
Адаптер и манифест обучения сохраняются локально в выбранном `--output`.
Проверка трёх отложенных чатов:

```powershell
.\.venv-train\Scripts\python.exe -m ai.evaluate_lora --adapter data/lora/seed-qwen3/adapter --validation data/labels/seed-sft/validation.jsonl --drafts data/labels/seed-drafts --output data/lora/seed-evaluation.json
```

Отчёт показывает, сколько ответов прошло проверку формата и цитат, совпадения по
критериям и ошибку итогового балла. Поскольку метки предварительные и выборка мала,
это техническая проверка обучения, а не доказательство качества для хакатона.
Сравните LoRA с Gemini на отложенных RU/KZ/EN примерах по каждому критерию,
N/A, флагу грубых ошибок и точности цитат, прежде чем менять рабочий evaluator.
