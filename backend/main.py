"""Composition root: construct adapters once and inject application services."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import ApplicationServices, router
from .config import Settings
from .db import Repository
from .evaluator import Evaluator, UnconfiguredEvaluator
from .http import BodyLimitMiddleware, UnexpectedErrorMiddleware, install_error_handlers
from .observability import configure_logging
from .organizer import MAX_BYTES, OrganizerClient
from .ports import DatasetSource
from .services import AppealService, EvaluationService, ImportService


def create_app(
    settings: Settings | None = None,
    evaluator: Evaluator | None = None,
    *,
    dataset_source: DatasetSource | None = None,
) -> FastAPI:
    configure_logging()
    settings = settings if settings is not None else Settings()
    repository = Repository(settings.database_path, settings.database_timeout)
    evaluator = evaluator if evaluator is not None else UnconfiguredEvaluator()
    source = (
        dataset_source
        if dataset_source is not None
        else OrganizerClient(settings.organizer_api_url)
    )
    services = ApplicationServices(
        repository=repository,
        evaluations=EvaluationService(repository, evaluator, settings.evaluation_timeout),
        appeals=AppealService(repository),
        imports=ImportService(repository, source),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await asyncio.to_thread(repository.initialize)
        yield

    app = FastAPI(
        title="CQA Backend",
        version="0.2.0",
        lifespan=lifespan,
        description="Backend contract for the frontend and AI teammates. AI is not connected by default.",
    )
    app.state.services = services
    app.state.repository = repository
    app.state.evaluator = evaluator
    app.add_middleware(BodyLimitMiddleware, max_bytes=MAX_BYTES)
    app.add_middleware(UnexpectedErrorMiddleware)
    # CORS wraps the body limiter so a 413 is readable by the frontend too.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )
    install_error_handlers(app)
    app.include_router(router)
    return app


app = create_app()
