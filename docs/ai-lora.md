# Локальное обучение модели оценки чатов

Для обучения используется `Qwen/Qwen3-0.6B` с PEFT LoRA (`r=8`, `q_proj` и `v_proj`). Обучается 1 146 880 параметров адаптера. Базовые веса остаются замороженными. Порядок соответствует [PEFT Quicktour](https://huggingface.co/docs/peft/quicktour): загрузить базовую модель, создать `LoraConfig`, применить `get_peft_model`, обучить и сохранить адаптер.

В локальной рабочей копии подготовлены предварительные оценки 60 текстовых чатов из `invoke.json`: 30 RU, 15 KZ и 15 EN. Пять звонков в этот запуск не входят. Все метки имеют `annotation_status: provisional`; их составил ассистент по транскриптам, они не подтверждены экспертами. Продуктовые утверждения без внутреннего справочника помечены для проверки. Четыре отдельно полученных черновика Gemini сохранены в `data/labels/teacher-drafts/` и не подменяют экспертный эталон.

Для видеокарты с 6 ГБ памяти каждый пример содержит весь диалог и один критерий. 48 чатов дают 384 обучающих примера, 12 отложенных чатов — 96 проверочных. Все восемь критериев одного чата попадают в один раздел. Семантически близкие сценарии на разных языках всё ещё могут оказаться в разных разделах: это предварительная техническая проверка, её нельзя считать независимым измерением качества.

## Окружение

ML-зависимости устанавливаются отдельно от рабочего backend:

```powershell
uv venv --python 3.12 .venv-train
uv pip install --python .venv-train\Scripts\python.exe -e .
uv pip install --python .venv-train\Scripts\python.exe torch==2.7.1 --index-url https://download.pytorch.org/whl/cu126
uv pip install --python .venv-train\Scripts\python.exe -r requirements-training.txt
```

CUDA-сборка проверена на RTX 3050 6GB Laptop GPU. Для другой платформы выберите сборку по [инструкции PyTorch](https://pytorch.org/get-started/locally/). Базовые веса должны находиться в `data/models/qwen3-0.6b/`; их можно загрузить из [официального репозитория Qwen](https://huggingface.co/Qwen/Qwen3-0.6B). Код не загружает диалоги или адаптер на Hub.

## Сборка данных и обучение

После экспертной проверки меток используйте `annotation_status: reviewed`. Для экспериментального запуска с текущими черновиками флаг `--allow-provisional` указан явно:

```powershell
.\.venv-train\Scripts\python.exe -m ai.labels build --drafts data/labels/expanded-drafts --out data/labels/criterion-sft --per-criterion --allow-provisional
.\.venv-train\Scripts\python.exe -m ai.train_lora --train data/labels/criterion-sft/train.jsonl --validation data/labels/criterion-sft/validation.jsonl --model data/models/qwen3-0.6b --output data/lora/qwen3-cqa-criterion --device cuda --epochs 1 --max-length 2560 --grad-accum 4
```

Максимальная длина текущего примера — 2496 токенов. Скрипт проверяет длину каждого примера и прекращает запуск при обрезании. На GPU включаются BF16, если он поддерживается, TF32 и gradient checkpointing. Память PyTorch ограничена 85% видеопамяти, чтобы при превышении лимита получить явную ошибку вместо длительного вытеснения данных в общую память Windows.

Результат сохраняется в `data/lora/qwen3-cqa-criterion/adapter/`. Манифест рядом содержит параметры, хеши обучающих файлов, список разделённых чатов, число шагов и ошибку до и после обучения. Данные, веса и манифесты находятся под игнорируемой Git папкой `data/`.

## Проверка генерации и подключение к backend

```powershell
.\.venv-train\Scripts\python.exe -m ai.evaluate_lora --adapter data/lora/qwen3-cqa-criterion/adapter --validation data/labels/criterion-sft/validation.jsonl --drafts data/labels/expanded-drafts --output data/lora/qwen3-cqa-criterion/evaluation.json --device cuda
```

Отчёт считает успешной только оценку, в которой все восемь ответов соответствуют шкалам и содержат точные цитаты. Отдельный локальный файл `evaluation.generations.json` сохраняет тексты генераций для разбора ошибок; он тоже содержит данные чатов и остаётся вне Git. Снижение функции потерь само по себе не подтверждает правильность оценок.

Экспериментальный `ai.local_lora.LocalLoRAEvaluator` реализует тот же `Evaluator`, что и Gemini. Он формирует ответы по одному критерию, объединяет их и проверяет весь результат через backend. Для локального API:

```powershell
.\.venv-train\Scripts\python.exe -m uvicorn ai.local_app:app --host 127.0.0.1 --port 8001
```

Swagger: `http://127.0.0.1:8001/docs`, метод оценки — `POST /api/v1/evaluations`. Ключ Gemini для этого запуска не нужен. Настройки: `CQA_LORA_ADAPTER`, `CQA_LORA_DEVICE=auto|cuda|cpu`, `CQA_LORA_BATCH_SIZE=1|2|3|4`. Два ответа генерируются одновременно по умолчанию. Для переноса на другой компьютер нужны адаптер, соседний `training-manifest.json`, базовые веса в том же относительном каталоге и ML-окружение.

Адаптер экспериментальный. Перед выбором его основным evaluator нужны независимая экспертная проверка меток и измерение ошибок на новых диалогах. Рабочий Gemini-адаптер запускается отдельно через `ai.app:app`.

## Результат запуска 7 ноября 2026

На RTX 3050 завершён один проход: 96 шагов оптимизатора, 384 обучающих примера, 15 минут 27 секунд обучения. Ошибка на 96 отложенных примерах снизилась с 1.8341 до 0.3150. Файл `adapter_model.safetensors` занимает 4 602 248 байт.

При свободной генерации только 3 из 12 полных оценок прошли проверку схемы и цитат. На этих трёх средняя ошибка итогового балла против предварительных меток составила 39.91 пункта; остальные девять не включены в эту метрику, поэтому её нельзя трактовать как общую ошибку модели. Адаптер не прошёл проверку для выбора основным оценщиком. Снижение ошибки токенов не означает правильную оценку обслуживания. Текущие веса и подробные отчёты сохранены локально для дальнейших экспериментов.
