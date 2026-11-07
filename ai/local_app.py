"""Optional backend entrypoint for an experimentally trained local LoRA adapter."""

import os
from pathlib import Path

from backend.main import create_app

from .local_lora import LocalLoRAEvaluator

app = create_app(
    evaluator=LocalLoRAEvaluator(
        Path(os.getenv("CQA_LORA_ADAPTER", "data/lora/qwen3-cqa-criterion/adapter")),
        device=os.getenv("CQA_LORA_DEVICE", "auto"),
        batch_size=int(os.getenv("CQA_LORA_BATCH_SIZE", "2")),
    )
)
