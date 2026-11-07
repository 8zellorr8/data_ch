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

Настоящие DEV-диалоги не включены в Git. Не отправляйте их в бесплатный облачный API, пока команда не согласовала обработку данных: [условия free tier](https://ai.google.dev/gemini-api/docs/pricing) предусматривают использование контента для улучшения продуктов. Звонки дополнительно закрыты настройкой `CQA_GEMINI_ALLOW_VOICE_CALLS=0`; даже при разрешении отправляется только транскрипт, не аудио. Бесплатный тариф и лимиты зависят от проекта; проверить их можно в AI Studio.

Если ключа нет, backend продолжает поддерживать импорт и ручные оценки. Ответы модели, которые нарушают схему, шкалу или содержат вымышленную цитату, не сохраняются как успешная оценка.

## 2. Сделать эталонную разметку

`invoke.json` содержит диалоги, но не правильные ответы по критериям. Без ручной разметки LoRA не узнает, какую оценку считать верной.

```powershell
.\.venv\Scripts\python.exe -m ai.labels prepare "C:\Users\erken\Downloads\Doomsday\invoke.json" --per-language 10
```

Команда создаёт 30 черновиков: по 10 чатов RU/KZ/EN, в `data/labels/drafts/`. Файлы остаются локально; `data/` исключён из Git. Нумерация `m1`, `m2` и т. д. даёт точные ссылки на реплики. В каждом файле заполняйте `result.answers` для всех восьми критериев: `value`, короткое `explanation`, `evidence_kind` и **дословную** `quote` с верным `message_id`. Для `na` нужен `na_reason`; для `needs_review=true` нужен `limitation`. Для шкалы указывайте число, не строку. Флаг `gross: yes` означает нарушение.

Первые 30 чатов — пилот. Сверяйте спорные случаи с чек-листом и вторым участником команды. Затем можно подготовить остальные чаты в отдельной папке (`--per-language 20 --out ...`) и разметить их. Сохраните часть размеченных примеров для независимой проверки; команда `build` автоматически откладывает каждый пятый чат каждого языка.

После заполнения черновиков:

```powershell
.\.venv\Scripts\python.exe -m ai.labels build
```

Скрипт проверит формат, значения и точные цитаты. Только при полной валидности он создаст `data/labels/sft/train.jsonl` и `validation.jsonl` в формате conversational prompt/completion для [TRL SFTTrainer](https://huggingface.co/docs/trl/sft_trainer). Эти файлы тоже содержат тексты диалогов и не должны попадать в Git или публичный Hugging Face Hub.

## 3. Экспериментальное локальное обучение LoRA

Gemini доступен через API и не является базовой моделью для этого скрипта. Для LoRA используется открытая `Qwen/Qwen3-0.6B` по умолчанию; можно указать другую совместимую Qwen3 модель. Скрипт повторяет порядок из [PEFT Quicktour](https://huggingface.co/docs/peft/quicktour): загрузить базовую модель, задать `LoraConfig`, обернуть через `get_peft_model`, обучить и сохранить только адаптер. Данные не загружаются на Hub. Это отдельный опытный путь; обученный адаптер пока не подключён к backend.

Установите `torch` с поддержкой вашей CUDA по [инструкции PyTorch](https://pytorch.org/get-started/locally/), затем `datasets`, `transformers`, `accelerate`, `peft`, `trl` в отдельное окружение. После ручной разметки и `build`:

```powershell
python -m ai.train_lora
```

Выход: `data/lora/qwen3-cqa/adapter/`. Скрипт требует CUDA и останавливается, если пример будет обрезан длиной `--max-length`. Он ещё не запускался на реальных метках, поэтому точность и время обучения неизвестны. Сравните LoRA с Gemini на отложенных RU/KZ/EN примерах по каждому критерию, N/A, флагу грубых ошибок и точности цитат, прежде чем менять рабочий evaluator.
