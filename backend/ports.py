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
    Page,
)
from .work_models import (
    BatchRecord,
    BatchRequest,
    BatchSummary,
    ClaimedItem,
    ProviderUsage,
    Reservation,
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


class ExecutionStore(Protocol):
    def replay(self, scope: str, key_hash: str, fingerprint: str) -> Reservation | None: ...
    def reserve(
        self,
        owner: str,
        now: float,
        scope: str,
        key_hash: str | None,
        fingerprint: str,
        conversation_id: str,
        item_id: str | None = None,
    ) -> Reservation: ...
    def complete(
        self,
        execution_id: str,
        owner: str,
        now: float,
        record: EvaluationRecord,
        duration_ms: int,
        usage: ProviderUsage | None,
    ) -> EvaluationRecord: ...
    def fail(
        self,
        execution_id: str,
        code: str,
        duration_ms: int,
        usage: ProviderUsage | None,
        interrupted: bool = False,
        *,
        retry_at: float | None = None,
        max_retries: int = 0,
        provider_not_called: bool = False,
    ) -> None: ...


class BatchStore(Protocol):
    def resume_batch(self, batch_id: str, queue_limit: int) -> BatchRecord: ...
    def cancel_pending(self, batch_id: str) -> BatchRecord: ...
    def replay_batch(self, request: BatchRequest, key_hash: str) -> BatchRecord | None: ...
    def create_batch(
        self,
        request: BatchRequest,
        queue_limit: int,
        key_hash: str | None = None,
    ) -> BatchRecord: ...
    def list_batches(self, limit: int = 20, offset: int = 0) -> Page[BatchSummary]: ...
    def claim(self, owner: str, now: float) -> ClaimedItem | None: ...
    def finish_item(
        self, item_id: str, owner: str, code: str, *, retry_busy: bool = False
    ) -> None: ...


class OwnerStore(Protocol):
    def register_owner(self, owner: str, now: float, lease: float) -> None: ...
    def heartbeat(self, owner: str, now: float, lease: float) -> None: ...
    def close_owner(self, owner: str, now: float) -> None: ...


class QualityReportStore(Protocol):
    def get_evaluation_selection(self, ids: set[str]) -> dict[str, EvaluationRecord]: ...
