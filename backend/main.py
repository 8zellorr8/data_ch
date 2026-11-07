from contextlib import asynccontextmanager
from time import perf_counter
from typing import Annotated, Literal
from dataclasses import asdict
import asyncio

from fastapi import FastAPI, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from pydantic import ValidationError

from .config import Settings
from .db import Repository
from .errors import DomainError
from .evaluator import Evaluator, EvaluationContext, UnconfiguredEvaluator
from .models import (DatasetImport, OrganizerImport, ChecklistDefinition, ChecklistVersion,
                     NewChecklistVersion, EvaluationRequest, ManualEvaluationRequest,
                     AppealRequest, AppealDecision, EvaluationResult, Conversation)
from .organizer import fetch_dataset, MAX_BYTES


def create_app(settings: Settings | None = None, evaluator: Evaluator | None = None) -> FastAPI:
    settings = settings or Settings()
    repository = Repository(settings.database_path)
    evaluator = evaluator or UnconfiguredEvaluator()

    @asynccontextmanager
    async def lifespan(app):
        repository.initialize()
        yield

    app = FastAPI(title="CQA Backend", version="0.1.0", lifespan=lifespan,
                  description="Backend contract for the frontend and AI teammates. AI is not connected by default.")
    app.state.repository = repository
    app.state.evaluator = evaluator
    app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins), allow_credentials=False,
                       allow_methods=["GET", "POST"], allow_headers=["Content-Type"])

    @app.middleware("http")
    async def body_limit(request: Request, call_next):
        if request.method == "POST":
            chunks, size = [], 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_BYTES:
                    return JSONResponse(status_code=413, content={"error": {"code": "BODY_TOO_LARGE", "message": "Maximum request size is 8 MiB"}})
                chunks.append(chunk)
            request._body = b"".join(chunks)
        return await call_next(request)

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        return JSONResponse(status_code=exc.status, content={"error": {"code": exc.code, "message": exc.message}})

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        details = [{"location": list(e["loc"]), "message": e["msg"], "type": e["type"]} for e in exc.errors()]
        return JSONResponse(status_code=422, content={"error": {"code": "VALIDATION_ERROR", "message": "Invalid request", "details": details}})

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return JSONResponse(status_code=exc.status_code, content={"error": {"code": "HTTP_ERROR", "message": str(exc.detail)}})

    @app.get("/health", tags=["system"])
    def health():
        return {"status": "ok", "database": "sqlite", "ai_configured": app.state.evaluator.configured}

    @app.post("/api/v1/datasets", status_code=201, tags=["datasets"])
    def import_dataset(payload: DatasetImport):
        return repository.import_dataset(payload)

    @app.post("/api/v1/datasets/import-organizer", status_code=201, tags=["datasets"])
    async def import_organizer(payload: OrganizerImport):
        data = await fetch_dataset(settings.organizer_api_url, payload)
        return repository.import_dataset(data)

    @app.get("/api/v1/datasets", tags=["datasets"])
    def datasets():
        return {"items": repository.list_datasets()}

    @app.get("/api/v1/datasets/{dataset_id}/conversations", tags=["conversations"])
    def conversations(dataset_id: str, language: Literal["RU", "KZ", "EN"] | None = None,
                      channel: Literal["agent chat", "voice call"] | None = None,
                      limit: Annotated[int, Query(ge=1, le=1000)] = 100,
                      offset: Annotated[int, Query(ge=0)] = 0):
        return repository.list_conversations(dataset_id, language, channel, limit, offset)

    @app.get("/api/v1/conversations/{conversation_id}", response_model=Conversation, tags=["conversations"])
    def conversation(conversation_id: str):
        return repository.get_conversation(conversation_id)

    @app.get("/api/v1/checklists", tags=["checklists"])
    def checklists():
        return {"items": repository.list_checklists()}

    @app.post("/api/v1/checklists", response_model=ChecklistVersion, status_code=201, tags=["checklists"])
    def create_checklist(payload: ChecklistDefinition):
        return repository.create_checklist(payload)

    @app.get("/api/v1/checklists/{checklist_id}", response_model=ChecklistVersion, tags=["checklists"])
    def latest_checklist(checklist_id: str):
        return repository.get_checklist(checklist_id)

    @app.get("/api/v1/checklists/{checklist_id}/versions/{version}", response_model=ChecklistVersion, tags=["checklists"])
    def checklist_version(checklist_id: str, version: Annotated[int, Path(ge=1)]):
        return repository.get_checklist(checklist_id, version)

    @app.post("/api/v1/checklists/{checklist_id}/versions", response_model=ChecklistVersion, status_code=201, tags=["checklists"])
    def add_checklist_version(checklist_id: str, payload: NewChecklistVersion):
        return repository.new_version(checklist_id, payload.expected_version, payload.definition)

    @app.post("/api/v1/evaluations", status_code=201, tags=["evaluations"])
    async def evaluate(payload: EvaluationRequest):
        conversation = repository.get_conversation(payload.conversation_id)
        checklist = repository.get_checklist(payload.checklist_id, payload.checklist_version)
        started = perf_counter()
        try:
            output = await asyncio.wait_for(app.state.evaluator.evaluate(EvaluationContext(conversation, checklist)), timeout=180)
        except DomainError:
            raise
        except TimeoutError:
            raise DomainError("AI_TIMEOUT", "Model did not respond within 180 seconds", 504) from None
        except Exception:
            raise DomainError("AI_UNAVAILABLE", "Model evaluator failed; no evaluation was saved", 502) from None
        try:
            result = EvaluationResult.model_validate(output.result)
            metadata = {k: v for k, v in asdict(output).items() if k != "result"}
            metadata["duration_ms"] = round((perf_counter() - started) * 1000)
            return repository.save_evaluation(conversation, checklist, result, "ai", metadata)
        except (DomainError, ValidationError, TypeError, ValueError, AttributeError):
            raise DomainError("AI_INVALID_RESULT", "Model result failed schema, scale or evidence validation; nothing was saved", 502) from None

    @app.post("/api/v1/evaluations/manual", status_code=201, tags=["evaluations"])
    def manual_evaluation(payload: ManualEvaluationRequest):
        conversation = repository.get_conversation(payload.conversation_id)
        checklist = repository.get_checklist(payload.checklist_id, payload.checklist_version)
        return repository.save_evaluation(conversation, checklist, payload.result, "manual", {"author": payload.author, "audio_analyzed": False})

    @app.get("/api/v1/evaluations/{evaluation_id}", tags=["evaluations"])
    def evaluation(evaluation_id: str):
        return repository.get_evaluation(evaluation_id)

    @app.get("/api/v1/conversations/{conversation_id}/evaluations", tags=["evaluations"])
    def evaluations(conversation_id: str, limit: Annotated[int, Query(ge=1, le=1000)] = 100,
                    offset: Annotated[int, Query(ge=0)] = 0):
        return repository.list_evaluations(conversation_id, limit, offset)

    @app.post("/api/v1/evaluations/{evaluation_id}/appeals", status_code=201, tags=["appeals"])
    def appeal(evaluation_id: str, payload: AppealRequest):
        return repository.create_appeal(evaluation_id, payload)

    @app.post("/api/v1/appeals/{appeal_id}/decision", tags=["appeals"])
    def decide(appeal_id: str, payload: AppealDecision):
        return repository.decide_appeal(appeal_id, payload)

    @app.get("/api/v1/datasets/{dataset_id}/export", tags=["export"])
    def export(dataset_id: str):
        return repository.export_dataset(dataset_id)

    return app


app = create_app()
