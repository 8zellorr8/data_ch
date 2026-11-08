# Локальное обучение Qwen4B

Текущая база — [Qwen3-4B-Instruct-2507 в NF4](https://huggingface.co/unsloth/Qwen3-4B-Instruct-2507-bnb-4bit), revision `f12db89cd5156e090618dded9b4367f23f8f3b33`. QLoRA обучает 16 515 072 параметра: `r=8`, `alpha=16`, dropout 0.05, все линейные слои. Веса базы заморожены. Применяется [PEFT Quicktour](https://huggingface.co/docs/peft/quicktour): `LoraConfig` → `get_peft_model` → обучение → `save_pretrained`. Это дообучение открытой модели, а не изменение Gemini.

Обучение завершено на RTX 3050 Laptop 6GB: 3 эпохи, 36 шагов за 1589,27 секунды. Пиковая выделенная память 4156 MiB, loss validation 1,5998 → 0,4671. Падение loss не доказывает качество оценок; [сравнение с базой и частотным прогнозом](ai-local-4b-validation.json) содержит выявленные ошибки.

## Данные и разделение

Использованы 60 текстовых диалогов из `invoke.json`: 30 RU, 15 KZ, 15 EN. Предварительные метки уже находились в `data/labels/expanded-drafts`. Новых платных запросов для них не делалось. Это не экспертная разметка. Пять RU-транскриптов звонков не включены в обучение; проверка на них отдельно дала только 20/40 совпадений.

48 чатов обучают модель, 12 отложены. Числовые номера, кратные пяти, во всех языках относятся к validation; остальные — train. Совпадающие полные тексты между разделами не найдены. Семантическая независимость сценариев не доказана. Validation использована для выбора эпохи и профиля промпта, отдельного test нет.

Один пример содержит весь диалог и восемь значений, ID реплик и короткие обоснования. Цитаты восстанавливаются по ID из оригинала. JSON и цитирование проверяются backend. Дословность цитаты не гарантирует её смысловую уместность.

## Окружение

Проверено: Windows, Python 3.12, PyTorch 2.7.1 CUDA 12.6. ML-библиотеки отделены от CPU-backend:

```powershell
uv venv --python 3.12 .venv-train
uv pip install --python .venv-train\Scripts\python.exe -e .
uv pip install --python .venv-train\Scripts\python.exe torch==2.7.1 --index-url https://download.pytorch.org/whl/cu126
uv pip install --python .venv-train\Scripts\python.exe -r requirements-training.txt
```

Версии закреплены в `requirements-training.txt`. Для другой платформы выберите CUDA-сборку по [PyTorch](https://pytorch.org/get-started/locally/). Установите базу и адаптер по [инструкции backend](ai-local-handoff.md). Никакие диалоги автоматически на Hugging Face не публикуются.

## Воспроизведение

Владелец располагает замороженными проверенными черновиками в `data/local-finetune-20261108-qwen4b/dataset-compact/references`. Эта папка содержит реальные данные и исключена из Git. Результат повторной сборки совпал с исходным обучением по SHA-256:

| Файл | SHA-256 |
| --- | --- |
| train.jsonl | `c329a6458a5f3a97ca0c5a3a413855d409320c19632cfc4bd573c2efc6c66b3b` |
| validation.jsonl | `38f809fc7a0d5d9b0a2dbca56f958999fd99d3917f2954560ef01330da1ffac3` |

```powershell
.\.venv-train\Scripts\python.exe -m ai.compact --drafts data/local-finetune-20261108-qwen4b/dataset-compact/references --out data/reproduce-4b-dataset
.\.venv-train\Scripts\python.exe -m ai.train_lora --train data/reproduce-4b-dataset/train.jsonl --validation data/reproduce-4b-dataset/validation.jsonl --model data/models/qwen3-4b-instruct-2507-nf4 --output data/lora/qwen3-4b-retrained --device cuda --load-in-4bit --epochs 3 --max-length 3712 --grad-accum 4 --gpu-memory-fraction 0.8 --target-modules all-linear --optimizer adamw_bnb_8bit --warmup-steps 4 --scheduler cosine --cpu-threads 2
```

Это повторно запустит обучение: для использования уже готового адаптера команда не нужна. На первом обучении использовалась изолированная копия этого процесса; общий CLI поддерживает те же параметры. Побитовая идентичность новой тренировки не гарантируется.

Используются NF4 double quantization, BF16, gradient checkpointing без reentrant, AdamW 8bit, learning rate 1e-4, seed 42 и небольшие блоки вычисления loss. Проверка останавливает обучение при нечисловом loss/градиенте. Длинные пути Windows обходятся загрузкой JSONL в память. Ни один обучающий пример не обрезается: самый длинный — 3587 токенов, лимит — 3712. SDPA использует экономный CUDA-режим с повторением KV вместо native GQA, который на этой версии иначе превышает память.

`adapter/` содержит обученные веса и tokenizer; соседний `training-manifest.json` хранит параметры, хеши данных, разделение и метрики. В релизном манифесте дополнительно заданы профиль `guarded`, порядок полей JSON, лимит пояснения 180 символов и чтение длинных входов блоками 512. Для свежего адаптера профиль и качество нужно проверять заново.

## Проверка

```powershell
.\.venv-train\Scripts\python.exe -m ai.evaluate_lora --adapter data/lora/qwen3-4b-cqa/adapter --validation data/reproduce-4b-dataset/validation.jsonl --drafts data/reproduce-4b-dataset/references --output data/lora/qwen3-4b-cqa/recheck.json --device cuda --max-new-tokens 1280
```

Эта команда создаёт новые локальные генерации и не обращается к платному API. Ошибки остаются в знаменателе покрытия. Полные генерации содержат реальные тексты и сохраняются только локально. Агрегаты находятся в [ai-local-4b-validation.json](ai-local-4b-validation.json).

На 12 текстовых чатах: база без адаптера с тем же защищённым промптом — 11/12 допустимых, 62/96 совпадений с учётом ошибки; обученный адаптер — 12/12, 77/96, MAE 11,03. Частотный прогноз — 76/96, MAE 12,35. Для звонков качество заметно слабее. Четыре синтетических контроля пройдены вместе с фильтром команд изменения оценок; устойчивость к произвольному prompt injection не доказана. Продуктовый FAQ и анализ аудио отсутствуют.

Предыдущие 0.6B и 1.7B не выбраны: 0.6B давала мало допустимых свободных JSON, 1.7B свелась к одному набору оценок на 12 текстовых чатах. Обучение на одних черновиках не заменяет экспертного эталона. Текущий результат требует проверки человеком перед сдачей.
