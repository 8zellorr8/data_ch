"""Transport adapters. Application scenarios and arithmetic live elsewhere."""

from dataclasses import dataclass
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Path, Query, Request

from .db import Repository
from .http import ErrorResponse
from .models import (
    AppealDecision,
    AppealRecord,
    AppealRequest,
    ChecklistDefinition,
    ChecklistSummary,
    ChecklistVersion,
    Conversation,
    ConversationSummary,
    DatasetExport,
    DatasetImport,
    DatasetSummary,
    EvaluationRecord,
    EvaluationRequest,
    EvaluationSummary,
    Health,
    Items,
    ManualEvaluationRequest,
    NewChecklistVersion,
    OrganizerImport,
    Page,
)
from .services import AppealService, EvaluationService, ImportService
from .value_types import Channel, Language


@dataclass(frozen=True)
class ApplicationServices:
    repository: Repository
    evaluations: EvaluationService
    appeals: AppealService
    imports: ImportService


def get_services(request: Request) -> ApplicationServices:
    return cast(ApplicationServices, request.app.state.services)


Services = Annotated[ApplicationServices, Depends(get_services)]
Limit = Annotated[int, Query(ge=1, le=1000)]
Offset = Annotated[int, Query(ge=0)]
router = APIRouter(
    responses={
        status: {"model": ErrorResponse} for status in (400, 404, 409, 413, 422, 500, 502, 503, 504)
    }
)


@router.get("/health", tags=["system"])
def health(services: Services) -> Health:
    return Health(ai_configured=services.evaluations.evaluator.configured)


@router.post("/api/v1/datasets", status_code=201, tags=["datasets"])
def import_dataset(payload: DatasetImport, services: Services) -> DatasetSummary:
    return services.repository.import_dataset(payload)


@router.post("/api/v1/datasets/import-organizer", status_code=201, tags=["datasets"])
async def import_organizer(payload: OrganizerImport, services: Services) -> DatasetSummary:
    return await services.imports.import_organizer(payload)


@router.get("/api/v1/datasets", tags=["datasets"])
def datasets(services: Services) -> Items[DatasetSummary]:
    return Items(items=services.repository.list_datasets())


@router.get("/api/v1/datasets/{dataset_id}/conversations", tags=["conversations"])
def conversations(
    dataset_id: str,
    services: Services,
    language: Language | None = None,
    channel: Channel | None = None,
    limit: Limit = 100,
    offset: Offset = 0,
) -> Page[ConversationSummary]:
    return services.repository.list_conversations(dataset_id, language, channel, limit, offset)


@router.get("/api/v1/conversations/{conversation_id}", tags=["conversations"])
def conversation(conversation_id: str, services: Services) -> Conversation:
    return services.repository.get_conversation(conversation_id)


@router.get("/api/v1/checklists", tags=["checklists"])
def checklists(services: Services) -> Items[ChecklistSummary]:
    return Items(items=services.repository.list_checklists())


@router.post("/api/v1/checklists", status_code=201, tags=["checklists"])
def create_checklist(payload: ChecklistDefinition, services: Services) -> ChecklistVersion:
    return services.repository.create_checklist(payload)


@router.get("/api/v1/checklists/{checklist_id}", tags=["checklists"])
def latest_checklist(checklist_id: str, services: Services) -> ChecklistVersion:
    return services.repository.get_checklist(checklist_id)


@router.get("/api/v1/checklists/{checklist_id}/versions/{version}", tags=["checklists"])
def checklist_version(
    checklist_id: str, version: Annotated[int, Path(ge=1)], services: Services
) -> ChecklistVersion:
    return services.repository.get_checklist(checklist_id, version)


@router.post("/api/v1/checklists/{checklist_id}/versions", status_code=201, tags=["checklists"])
def add_checklist_version(
    checklist_id: str, payload: NewChecklistVersion, services: Services
) -> ChecklistVersion:
    return services.repository.new_version(
        checklist_id, payload.expected_version, payload.definition
    )


@router.post("/api/v1/evaluations", status_code=201, tags=["evaluations"])
async def evaluate(payload: EvaluationRequest, services: Services) -> EvaluationRecord:
    return await services.evaluations.evaluate(payload)


@router.post("/api/v1/evaluations/manual", status_code=201, tags=["evaluations"])
def manual_evaluation(payload: ManualEvaluationRequest, services: Services) -> EvaluationRecord:
    return services.evaluations.evaluate_manual(payload)


@router.get("/api/v1/evaluations/{evaluation_id}", tags=["evaluations"])
def evaluation(evaluation_id: str, services: Services) -> EvaluationRecord:
    return services.repository.get_evaluation(evaluation_id)


@router.get("/api/v1/conversations/{conversation_id}/evaluations", tags=["evaluations"])
def evaluations(
    conversation_id: str, services: Services, limit: Limit = 100, offset: Offset = 0
) -> Page[EvaluationSummary]:
    return services.repository.list_evaluations(conversation_id, limit, offset)


@router.post("/api/v1/evaluations/{evaluation_id}/appeals", status_code=201, tags=["appeals"])
def appeal(evaluation_id: str, payload: AppealRequest, services: Services) -> AppealRecord:
    return services.appeals.create(evaluation_id, payload)


@router.post("/api/v1/appeals/{appeal_id}/decision", tags=["appeals"])
def decide(appeal_id: str, payload: AppealDecision, services: Services) -> EvaluationRecord:
    return services.appeals.decide(appeal_id, payload)


@router.get("/api/v1/datasets/{dataset_id}/export", tags=["export"])
def export(dataset_id: str, services: Services) -> DatasetExport:
    return services.repository.export_dataset(dataset_id)
