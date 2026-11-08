"""Compatibility entrypoint for the configured local LoRA evaluator."""

import os
from pathlib import Path

from backend.main import create_app

from .app import _warm_local
from .local_lora import LocalLoRAEvaluator

# Keep the legacy module local even when the general application uses a cloud provider.
# No key or process-wide environment mutation is needed for this entrypoint.
_evaluator = LocalLoRAEvaluator(
    Path(os.getenv("CQA_LORA_ADAPTER", "data/lora/qwen3-1.7b-compact/adapter")),
    device=os.getenv("CQA_LORA_DEVICE", "auto"),
    batch_size=1,
    max_new_tokens=int(os.getenv("CQA_LORA_MAX_NEW_TOKENS", "2048")),
    timeout=float(os.getenv("CQA_LORA_TIMEOUT_SECONDS", "150")),
)
app = _warm_local(create_app(evaluator=_evaluator), _evaluator)
