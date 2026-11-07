"""Gemini-backed local and hosted application entrypoints."""

import os

from fastapi import FastAPI

from backend.hosting import create_hosted_app as create_backend_hosted_app
from backend.main import create_app as create_backend_app

from .gemini import DEFAULT_FALLBACK_MODEL, DEFAULT_MODEL, GeminiEvaluator


def build_evaluator() -> GeminiEvaluator:
    return GeminiEvaluator(
        api_key=os.getenv("GEMINI_API_KEY", ""),
        model=os.getenv("CQA_GEMINI_MODEL", DEFAULT_MODEL),
        fallback_model=os.getenv("CQA_GEMINI_FALLBACK_MODEL", DEFAULT_FALLBACK_MODEL) or None,
        thinking_level=os.getenv("CQA_GEMINI_THINKING_LEVEL", "low"),
        allow_voice_calls=os.getenv("CQA_GEMINI_ALLOW_VOICE_CALLS", "0") == "1",
        timeout=float(os.getenv("CQA_GEMINI_PRIMARY_TIMEOUT_SECONDS", "15")),
        fallback_timeout=float(os.getenv("CQA_GEMINI_FALLBACK_TIMEOUT_SECONDS", "40")),
    )


def create_hosted_app() -> FastAPI:
    """Use the hosted frontend, snapshots, and queues with the Gemini evaluator."""
    return create_backend_hosted_app(evaluator=build_evaluator())


app = create_backend_app(evaluator=build_evaluator())
