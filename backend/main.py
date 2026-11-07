"""Composition root: construct adapters once and inject application services."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import ApplicationServices, router
from .batches import BatchService, ExecutionRuntime
from .config import Settings
from .db import Repository
from .evaluator import Evaluator, UnconfiguredEvaluator
from .execution import AdmissionLimit, EvaluationCoordinator
from .http import BodyLimitMiddleware, UnexpectedErrorMiddleware, install_error_handlers
from .observability import RequestDiagnosticsMiddleware, configure_logging
from .organizer import MAX_BYTES, OrganizerClient
from .ports import DatasetSource
from .services import AppealService, EvaluationService, ImportService
from .work_store import WorkStore


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
    work_store = WorkStore(repository)
    evaluation_service = EvaluationService(repository, evaluator, settings.evaluation_timeout)
    owner = str(uuid4())
    coordinator = EvaluationCoordinator(
        evaluation_service, work_store, owner, AdmissionLimit(settings.ai_concurrency)
    )
    batches = BatchService(coordinator, work_store, settings.batch_queue_limit)
    runtime = ExecutionRuntime(batches, work_store, owner, settings.owner_lease_seconds)
    services = ApplicationServices(
        repository=repository,
        evaluations=evaluation_service,
        appeals=AppealService(repository),
        imports=ImportService(repository, source),
        coordinator=coordinator,
        batches=batches,
        work_store=work_store,
        tariffs=settings.tariffs,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await asyncio.to_thread(repository.initialize)
        await runtime.start()
        try:
            yield
        finally:
            await runtime.close()

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
        allow_headers=["Content-Type", "Idempotency-Key", "X-Request-ID"],
        expose_headers=["X-Request-ID", "X-Execution-ID"],
    )
    app.add_middleware(RequestDiagnosticsMiddleware)
    install_error_handlers(app)
    app.include_router(router)
    return app


app = create_app()
