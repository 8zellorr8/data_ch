"""Additive contracts for durable execution, batches and usage reporting."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .models import Contract, EvaluationRecord, EvaluationRequest
from .value_types import NonBlank

WorkStatus = Literal["pending", "running", "succeeded", "failed", "interrupted"]
PositiveInt = Annotated[int, Field(strict=True, ge=1)]
TokenCount = Annotated[int, Field(strict=True, ge=0)]
SafeId = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")]


@dataclass(frozen=True)
class Reservation:
    id: str
    replay: EvaluationRecord | None = None


class ProviderUsage(Contract):
    """Aggregate of ALL adapter calls, including retries; optional for old adapters.

    complete=True also attests that the two token categories cover all billable
    usage. Adapters using caching/tools/other tariffs must leave it False unless
    they can express that usage accurately with the configured two-rate tariff.
    """

    model: Annotated[NonBlank, Field(min_length=1, max_length=200)]
    calls: PositiveInt
    input_tokens: TokenCount | None = None
    output_tokens: TokenCount | None = None
    complete: bool = False


class BatchRequest(Contract):
    conversation_ids: Annotated[list[SafeId], Field(min_length=1, max_length=100)]
    checklist_id: SafeId
    checklist_version: PositiveInt

    @model_validator(mode="after")
    def unique_conversations(self) -> "BatchRequest":
        if len(set(self.conversation_ids)) != len(self.conversation_ids):
            raise ValueError("Duplicate conversation IDs are not allowed")
        return self


class BatchItem(Contract):
    id: str
    conversation_id: str
    status: WorkStatus
    evaluation_id: str | None = None
    error_code: str | None = None


class BatchRecord(Contract):
    id: str
    created_at: str
    checklist_id: str
    checklist_version: int
    completed: bool
    items: list[BatchItem]


class BatchSummary(Contract):
    id: str
    created_at: str
    checklist_id: str
    checklist_version: int
    completed: bool
    total: int
    pending: int
    running: int
    succeeded: int
    failed: int
    interrupted: int


class ClaimedItem(Contract):
    id: str
    batch_id: str
    request: EvaluationRequest


class UsageRecord(Contract):
    id: str
    evaluation_id: str | None = None
    batch_id: str | None = None
    conversation_id: str
    status: WorkStatus
    error_code: str | None = None
    duration_ms: int | None = None
    usage: ProviderUsage | None = None


class UsageSelection(Contract):
    evaluation_ids: Annotated[list[SafeId], Field(max_length=1000)] = Field(default_factory=list)
    execution_ids: Annotated[list[SafeId], Field(max_length=1000)] = Field(default_factory=list)
    batch_id: SafeId | None = None

    @model_validator(mode="after")
    def one_selector(self) -> "UsageSelection":
        if sum(bool(x) for x in (self.evaluation_ids, self.execution_ids, self.batch_id)) != 1:
            raise ValueError("Select exactly one of evaluation_ids, execution_ids or batch_id")
        for values in (self.evaluation_ids, self.execution_ids):
            if len(set(values)) != len(values):
                raise ValueError("Duplicate IDs are not allowed")
        return self


class Tariff(Contract):
    model: Annotated[NonBlank, Field(min_length=1, max_length=200)]
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    version: Annotated[NonBlank, Field(min_length=1, max_length=100)]
    effective_date: date
    token_unit: PositiveInt
    input_price: Annotated[Decimal, Field(ge=0, allow_inf_nan=False, max_digits=24)]
    output_price: Annotated[Decimal, Field(ge=0, allow_inf_nan=False, max_digits=24)]


class UsageLine(Contract):
    execution: UsageRecord
    tariff: Tariff | None
    known_cost: Decimal | None
    complete: bool
    limitations: list[str]


class UsageReport(Contract):
    rows: list[UsageLine]
    known_cost_by_currency: dict[str, Decimal]
    complete: bool
    known_duration_ms: int
    duration_complete: bool
    known_calls: int
    calls_complete: bool
    known_input_tokens: int
    known_output_tokens: int
    tokens_complete: bool
    missing_ids: list[str]
    pending_items: int = 0
