"""Configured cloud or local evaluator application entrypoints."""

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from backend.evaluator import Evaluator
from backend.hosting import create_hosted_app as create_backend_hosted_app
from backend.main import create_app as create_backend_app

from .config import build_evaluator as build_evaluator
from .local_lora import LocalLoRAEvaluator


def _warm_local(app: FastAPI, evaluator: object) -> FastAPI:
    if isinstance(evaluator, LocalLoRAEvaluator) and evaluator.configured:
        previous = app.router.lifespan_context

        @asynccontextmanager
        async def lifespan(application: FastAPI) -> AsyncIterator[None]:
            await asyncio.to_thread(evaluator.warmup)
            async with previous(application):
                yield

        app.router.lifespan_context = lifespan
    return app


def create_hosted_app() -> FastAPI:
    """Use hosted frontend, snapshots and queues with the configured evaluator."""
    evaluator = build_evaluator()
    return _configured_app(create_backend_hosted_app(evaluator=evaluator), evaluator)


def _configured_app(application: FastAPI, evaluator: Evaluator) -> FastAPI:
    @application.get("/api/v1/ai/status")
    async def ai_status() -> dict[str, object]:
        local = bool(getattr(evaluator, "local_only", False))
        return {
            "configured": evaluator.configured,
            "provider": os.getenv("CQA_AI_PROVIDER", "gemini"),
            "model": str(getattr(evaluator, "model", "unconfigured")),
            "usage_is_local": local,
            "paid_fallback": False if local else None,
        }

    return _warm_local(application, evaluator)


_evaluator = build_evaluator()
app = _configured_app(create_backend_app(evaluator=_evaluator), _evaluator)
