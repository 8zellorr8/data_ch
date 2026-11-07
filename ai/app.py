"""Run with: python -m uvicorn ai.app:app --env-file .env"""

import os

from backend.main import create_app

from .gemini import GeminiEvaluator

app = create_app(
    evaluator=GeminiEvaluator(
        api_key=os.getenv("GEMINI_API_KEY", ""),
        model=os.getenv("CQA_GEMINI_MODEL", "gemini-3.8-flash"),
        thinking_level=os.getenv("CQA_GEMINI_THINKING_LEVEL", "low"),
        allow_voice_calls=os.getenv("CQA_GEMINI_ALLOW_VOICE_CALLS", "0") == "1",
    )
)
