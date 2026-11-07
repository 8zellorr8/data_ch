"""Configured cloud or local evaluator application entrypoints."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

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
    return _warm_local(create_backend_hosted_app(evaluator=evaluator), evaluator)


_evaluator = build_evaluator()
app = _warm_local(create_backend_app(evaluator=_evaluator), _evaluator)
