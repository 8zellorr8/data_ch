"""Construct the configured evaluator without changing the backend protocol."""

import os
from collections.abc import Callable
from pathlib import Path

from backend.evaluator import EvaluationContext

from .budget import TokenBudget
from .gemini import DEFAULT_FALLBACK_MODEL, DEFAULT_MODEL, DEFAULT_THINKING_LEVEL, GeminiEvaluator
from .local_lora import LocalLoRAEvaluator
from .responses import ResponsesEvaluator


def build_evaluator(
    *,
    model: str | None = None,
    thinking_level: str | None = None,
    allow_voice_calls: bool | None = None,
    invalid_result_handler: Callable[[EvaluationContext, str, str, str], None] | None = None,
) -> GeminiEvaluator | ResponsesEvaluator | LocalLoRAEvaluator:
    provider = os.getenv("CQA_AI_PROVIDER", "gemini")
    if provider == "local-lora":
        return LocalLoRAEvaluator(
            Path(os.getenv("CQA_LORA_ADAPTER", "data/lora/qwen3-1.7b-compact/adapter")),
            device=os.getenv("CQA_LORA_DEVICE", "auto"),
            batch_size=1,
            max_new_tokens=int(os.getenv("CQA_LORA_MAX_NEW_TOKENS", "2048")),
            timeout=float(os.getenv("CQA_LORA_TIMEOUT_SECONDS", "150")),
        )
    if provider == "openai-compatible":
        return ResponsesEvaluator(
            os.getenv("CQA_LLM_API_KEY", "")
            if os.getenv("CQA_LLM_API_ENABLED", "0") == "1"
            else "",
            base_url=os.getenv("CQA_LLM_BASE_URL", "https://api.aijuju.rest/v1"),
            model=model if model is not None else os.getenv("CQA_LLM_MODEL", "gemini-3.8-flash"),
            budget=TokenBudget(
                Path(os.getenv("CQA_LLM_BUDGET_PATH", "data/aijuju-capped-budget.sqlite3")),
                total=int(os.getenv("CQA_LLM_TOKEN_BUDGET", "2000000")),
                jury_reserve=int(os.getenv("CQA_LLM_TOKEN_RESERVE", "0")),
                initial_spent=int(os.getenv("CQA_LLM_INITIAL_SPENT", "0")),
                use_jury_reserve=os.getenv("CQA_LLM_USE_JURY_RESERVE", "0") == "1",
            ),
            model_aliases=tuple(
                value.strip()
                for value in os.getenv("CQA_LLM_MODEL_ALIASES", "").split(",")
                if value.strip()
            ),
            thinking_level=thinking_level
            if thinking_level is not None
            else os.getenv("CQA_LLM_THINKING_LEVEL", "medium"),
            response_format=os.getenv("CQA_LLM_RESPONSE_FORMAT", "json_schema"),
            allow_voice_calls=allow_voice_calls
            if allow_voice_calls is not None
            else os.getenv("CQA_LLM_ALLOW_VOICE_CALLS", "0") == "1",
            max_output_tokens=int(os.getenv("CQA_LLM_MAX_OUTPUT_TOKENS", "4096")),
            max_input_bytes=int(os.getenv("CQA_LLM_MAX_INPUT_BYTES", "250000")),
            max_attempts=int(os.getenv("CQA_LLM_MAX_ATTEMPTS", "2")),
            timeout=float(os.getenv("CQA_LLM_TIMEOUT_SECONDS", "75")),
            invalid_result_handler=invalid_result_handler,
        )
    if provider != "gemini":
        raise ValueError("Unsupported CQA_AI_PROVIDER")
    return GeminiEvaluator(
        api_key=os.getenv("GEMINI_API_KEY", ""),
        model=model if model is not None else os.getenv("CQA_GEMINI_MODEL", DEFAULT_MODEL),
        fallback_model=os.getenv("CQA_GEMINI_FALLBACK_MODEL", DEFAULT_FALLBACK_MODEL or "") or None,
        thinking_level=thinking_level
        if thinking_level is not None
        else os.getenv("CQA_GEMINI_THINKING_LEVEL", DEFAULT_THINKING_LEVEL),
        allow_voice_calls=allow_voice_calls
        if allow_voice_calls is not None
        else os.getenv("CQA_GEMINI_ALLOW_VOICE_CALLS", "0") == "1",
        timeout=float(os.getenv("CQA_GEMINI_PRIMARY_TIMEOUT_SECONDS", "15")),
        fallback_timeout=float(os.getenv("CQA_GEMINI_FALLBACK_TIMEOUT_SECONDS", "40")),
        invalid_result_handler=invalid_result_handler,
    )
