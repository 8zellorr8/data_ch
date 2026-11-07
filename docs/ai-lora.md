# Локальное обучение оценки чатов

Текущая модель — `Qwen/Qwen3-1.7B`, QLoRA NF4 с PEFT (`r=8`, `alpha=16`, `q_proj` и `v_proj`, dropout 0.05). Обучаются только адаптеры; база заморожена. Порядок соответствует [PEFT Quicktour](https://huggingface.co/docs/peft/quicktour): загрузить базу, создать `LoraConfig`, применить `get_peft_model`, обучить и сохранить адаптер. Новых вызовов платного API для этого запуска нет.

## Данные

Используются имеющиеся предварительные метки по `invoke.json`: 60 текстовых диалогов (30 RU, 15 KZ, 15 EN) и пять RU-транскриптов звонков. Первый источник — `data/labels/expanded-drafts`, недостающие пять звонков берутся из `data/labels/lite-final-medium`. Первые метки составил ассистент, метки звонков — предыдущий Gemini. Это **не экспертный эталон**. Новая платная разметка aijuju не запрашивается.

Один пример содержит весь диалог и восемь ответов: значения, ID подтверждающих реплик и обоснования. Адаптер извлекает цитаты из исходных сообщений. Это исключает ошибки копирования текста, но не доказывает смысловую уместность выбранной реплики.

Числовые номера сценариев, кратные пяти, отложены: 52 диалога обучают модель, 13 проверяют. Все критерии диалога и совпадающие числовые номера разных языков/каналов находятся в одном разделе. Семантические дубликаты отдельно не проверены. Validation используется для выбора checkpoint, поэтому это предварительная проверка, а не независимый тест.

## Окружение

Проверено: Windows, RTX 3050 Laptop 6GB, Python 3.12, PyTorch 2.7.1 CUDA 12.6. ML-зависимости отделены от backend:

```powershell
uv venv --python 3.12 .venv-train
uv pip install --python .venv-train\Scripts\python.exe -e .
uv pip install --python .venv-train\Scripts\python.exe torch==2.7.1 --index-url https://download.pytorch.org/whl/cu126
uv pip install --python .venv-train\Scripts\python.exe -r requirements-training.txt
```

Для другой платформы выберите CUDA-сборку по [инструкции PyTorch](https://pytorch.org/get-started/locally/). Скачайте базовые файлы из [официального репозитория Qwen](https://huggingface.co/Qwen/Qwen3-1.7B) в `data/models/qwen3-1.7b/`. Код не отправляет диалоги и адаптер на Hub.

## Сборка и обучение

```powershell
.\.venv-train\Scripts\python.exe -m ai.compact --drafts data/labels/expanded-drafts data/labels/lite-final-medium --out data/labels/compact-sft
.\.venv-train\Scripts\python.exe -m ai.train_lora --train data/labels/compact-sft/train.jsonl --validation data/labels/compact-sft/validation.jsonl --model data/models/qwen3-1.7b --output data/lora/qwen3-1.7b-compact --device cuda --load-in-4bit --epochs 3 --max-length 9728 --grad-accum 1
```

Максимальная длина текущего примера — 9703 токена. Скрипт прекращает запуск, если пример пришлось бы обрезать. BF16, gradient checkpointing, NF4 и небольшие блоки вычисления loss позволяют вместить самый длинный пример в 6 ГБ. Процесс ограничен 85% памяти GPU. Для PyTorch 2.7 на Windows используется memory-efficient SDPA с явным повторением KV: native GQA иначе выбирает режим с квадратичной памятью. Настройки привязаны к версиям `requirements-training.txt`.

Результат: `data/lora/qwen3-1.7b-compact/adapter/`. Соседний `training-manifest.json` содержит SHA-256 данных, разделение, шаги, параметры и метрики. Нулевой `baseline-adapter/` сохраняется до обучения для сравнения с той же базой. Данные и веса находятся вне Git.

## Проверка

```powershell
.\.venv-train\Scripts\python.exe -m ai.evaluate_lora --adapter data/lora/qwen3-1.7b-compact/adapter --validation data/labels/compact-sft/validation.jsonl --drafts data/labels/compact-sft/references --output data/lora/qwen3-1.7b-compact/evaluation.json --device cuda --max-new-tokens 2048
.\.venv-train\Scripts\python.exe -m ai.evaluate_lora --adapter data/lora/qwen3-1.7b-compact/baseline-adapter --validation data/labels/compact-sft/validation.jsonl --drafts data/labels/compact-sft/references --output data/lora/qwen3-1.7b-compact/baseline-evaluation.json --device cuda --max-new-tokens 2048
```

Проверка использует настоящий `LocalLoRAEvaluator`: генерация по JSON Schema, восстановление цитат, проверка шкал и расчёт backend. Отчёт включает покрытие, совпадения с черновиками, N/A, MAE и время. Ошибки остаются в знаменателе покрытия. `.generations.json` содержит реальные генерации и остаётся локально.

Подключение: [ai-local-handoff.md](ai-local-handoff.md).

## Предыдущий эксперимент

7 ноября Qwen3-0.6B обучалась по одному критерию на 384 примерах, 96 шагов за 15 минут 27 секунд. Loss снизился с 1.8341 до 0.3150, но лишь 3 из 12 свободных полных генераций прошли проверку; MAE на этих трёх — 39.91. Эта версия не выбрана для нового запуска. Снижение loss само по себе не подтверждает правильность оценки обслуживания.
