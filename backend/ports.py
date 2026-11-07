"""Small storage contracts for application services; implemented by Repository."""

from typing import Protocol

from .models import (
    AppealRecord,
    ChecklistVersion,
    Conversation,
    DatasetImport,
    DatasetSummary,
    EvaluationRecord,
    OrganizerImport,
)


class EvaluationStore(Protocol):
    def get_conversation(self, conversation_id: str) -> Conversation: ...
    def get_checklist(self, checklist_id: str, version: int | None = None) -> ChecklistVersion: ...
    def insert_evaluation(self, record: EvaluationRecord) -> EvaluationRecord: ...


class AppealStore(Protocol):
    def get_evaluation(self, evaluation_id: str) -> EvaluationRecord: ...
    def get_appeal(self, appeal_id: str) -> AppealRecord: ...
    def insert_appeal(self, record: AppealRecord) -> AppealRecord: ...
    def commit_review(
        self, evaluation: EvaluationRecord, appeal: AppealRecord, expected_revision: int
    ) -> EvaluationRecord: ...


class DatasetStore(Protocol):
    def import_dataset(self, payload: DatasetImport) -> DatasetSummary: ...


class DatasetSource(Protocol):
    async def fetch(self, request: OrganizerImport) -> DatasetImport: ...
