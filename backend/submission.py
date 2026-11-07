"""Organizer JSON 1.0 adapter; original AI assessments only, no provider calls."""

from typing import Annotated, Literal, Protocol, Self

from pydantic import Field, StrictBool, StrictInt, model_validator

from .errors import DomainError
from .models import (
    AIModelMetadata,
    Contract,
    Conversation,
    DatasetExport,
    EvaluationRecord,
)
from .scoring import calculate, validate_result
from .value_types import NonBlank
from .work_models import BatchRecord, SafeId

# External vocabulary is deliberately isolated from persisted rubric/API IDs.
# (internal ID, organizer ID, type, weight, N/A allowed, raw scale)
RULES = (
    ("contact", "contact", "binary", 10, False, ()),
    ("needs", "discovery", "binary", 15, True, ()),
    ("identity", "identification", "binary", 20, True, ()),
    ("consultation", "consultation", "scale", 20, True, (0, 5, 10, 15, 20)),
    ("empathy", "empathy", "binary", 15, False, ()),
    ("management", "dialogue", "scale", 15, False, (0, 4, 7, 10)),
    ("closing", "closure", "binary", 5, True, ()),
    ("gross", "critical", "flag", 0, False, ()),
)
ModelName = Annotated[NonBlank, Field(max_length=200)]


class SubmissionSelection(Contract):
    conversation_id: Annotated[NonBlank, Field(max_length=200)]
    evaluation_id: SafeId | None = None
    error: Annotated[NonBlank, Field(max_length=1000)] | None = None

    @model_validator(mode="after")
    def one_outcome(self) -> Self:
        if (self.evaluation_id is None) == (self.error is None):
            raise ValueError("Choose exactly one evaluation_id or explicit error")
        return self


class SubmissionRequest(Contract):
    split: Literal["dev", "prod"]
    team_name: Annotated[NonBlank, Field(max_length=200)]
    model_mode: Literal["local", "external_api", "hybrid"]
    model_names: Annotated[list[ModelName], Field(min_length=1, max_length=20)]
    batch_id: SafeId | None = None
    items: Annotated[list[SubmissionSelection], Field(max_length=65)] = Field(default_factory=list)

    @model_validator(mode="after")
    def explicit_selection(self) -> Self:
        if bool(self.batch_id) == bool(self.items):
            raise ValueError("Choose exactly one completed batch_id or items")
        ids = [item.conversation_id for item in self.items]
        if len(set(ids)) != len(ids):
            raise ValueError("Duplicate conversation_id")
        if len(set(self.model_names)) != len(self.model_names):
            raise ValueError("Duplicate model name")
        return self


class SubmissionModel(Contract):
    mode: Literal["local", "external_api", "hybrid"]
    name: str


class SubmissionCriterion(Contract):
    criterion_id: str
    value: StrictBool | StrictInt | None
    rationale: NonBlank
    evidence_turns: list[Annotated[int, Field(strict=True, ge=1)]]


class SubmissionScore(Contract):
    final_score: Annotated[float, Field(ge=0, le=100)]
    critical_flag: bool


class SubmissionSuccess(Contract):
    conversation_id: str
    status: Literal["ok"] = "ok"
    criteria: Annotated[list[SubmissionCriterion], Field(min_length=8, max_length=8)]
    score: SubmissionScore


class SubmissionFailure(Contract):
    conversation_id: str
    status: Literal["error"] = "error"
    criteria: Annotated[list[SubmissionCriterion], Field(max_length=0)] = Field(
        default_factory=list
    )
    score: None = None
    error: NonBlank


class SubmissionDocument(Contract):
    schema_version: Literal["1.0"] = "1.0"
    team_name: str
    model: SubmissionModel
    results: list[SubmissionSuccess | SubmissionFailure]


class SubmissionStore(Protocol):
    def export_dataset(self, dataset_id: str) -> DatasetExport: ...


class SubmissionBatchStore(Protocol):
    def get_batch(self, batch_id: str) -> BatchRecord: ...


def invalid(message: str) -> DomainError:
    return DomainError("INVALID_SUBMISSION", message)


def export_assessment(record: EvaluationRecord, conversation: Conversation) -> SubmissionSuccess:
    if record.origin != "ai" or not isinstance(record.metadata, AIModelMetadata):
        raise invalid("Submission requires original AI evaluations, not manual references")
    if record.conversation_id != conversation.id or record.conversation != conversation:
        raise invalid("Selected evaluation has a different conversation snapshot")
    criteria = {criterion.id: criterion for criterion in record.checklist.criteria}
    if set(criteria) != {rule[0] for rule in RULES}:
        raise invalid("Submission requires the eight Freedom Broker criteria")
    for internal, _, kind, weight, allow_na, scale in RULES:
        criterion = criteria[internal]
        if (
            criterion.type != kind
            or criterion.weight != weight
            or criterion.allow_na != allow_na
            or tuple(sorted(criterion.scale)) != scale
        ):
            raise invalid("Rubric weights, scales or N/A differ from organizer JSON 1.0")
    validate_result(record.checklist, conversation, record.original)
    score = calculate(record.checklist, record.original)
    if score.total is None:
        raise invalid("A successful organizer result requires a numeric score")
    # Count ALL input turns, including client, bot and system; IDs need not be mN.
    turns = {message.id: index for index, message in enumerate(conversation.transcript, 1)}
    answers = {answer.criterion_id: answer for answer in record.original.answers}
    exported = []
    for internal, external, *_ in RULES:
        answer = answers[internal]
        value = answer.value
        external_value = (
            None if value == "na" else value == "yes" if isinstance(value, str) else int(value)
        )
        exported.append(
            SubmissionCriterion(
                criterion_id=external,
                value=external_value,
                rationale=answer.explanation,
                evidence_turns=sorted({turns[e.message_id] for e in answer.evidence}),
            )
        )
    return SubmissionSuccess(
        conversation_id=conversation.conversation_id,
        criteria=exported,
        score=SubmissionScore(final_score=score.total, critical_flag="gross" in score.flags),
    )


class SubmissionService:
    def __init__(self, store: SubmissionStore, batches: SubmissionBatchStore) -> None:
        self.store, self.batches = store, batches

    def generate(self, dataset_id: str, request: SubmissionRequest) -> SubmissionDocument:
        # Read a terminal batch first: its evaluation IDs cannot subsequently change.
        # Conversations, immutable originals and rubric snapshots share one DB snapshot.
        batch = self.batches.get_batch(request.batch_id) if request.batch_id else None
        if batch and any(item.status in ("pending", "running") for item in batch.items):
            raise invalid("Batch is unfinished; wait or cancel pending items before export")
        snapshot = self.store.export_dataset(dataset_id)
        conversations = snapshot.conversations
        expected = 65 if request.split == "dev" else 10
        voices = sum(c.channel == "voice call" for c in conversations)
        if len(conversations) != expected or voices != (5 if request.split == "dev" else 0):
            raise invalid("Expected DEV: 60 chats + 5 calls; PROD: exactly 10 chats")
        items = request.items
        if batch:
            external_ids = {c.id: c.conversation_id for c in conversations}
            items = []
            for batch_item in batch.items:
                if batch_item.conversation_id not in external_ids:
                    raise invalid("Batch contains a conversation from another dataset")
                if batch_item.status == "succeeded" and batch_item.evaluation_id:
                    items.append(
                        SubmissionSelection(
                            conversation_id=external_ids[batch_item.conversation_id],
                            evaluation_id=batch_item.evaluation_id,
                        )
                    )
                elif batch_item.status in ("failed", "interrupted") and batch_item.error_code:
                    items.append(
                        SubmissionSelection(
                            conversation_id=external_ids[batch_item.conversation_id],
                            error=batch_item.error_code,
                        )
                    )
                else:
                    raise invalid("Batch item has no valid terminal outcome")
        selected = {item.conversation_id: item for item in items}
        if len(selected) != len(items) or set(selected) != {
            c.conversation_id for c in conversations
        }:
            raise invalid("Select exactly one outcome for every original conversation_id")
        evaluations = {record.id: record for record in snapshot.evaluations}
        results: list[SubmissionSuccess | SubmissionFailure] = []
        for conversation in conversations:
            item = selected[conversation.conversation_id]
            if item.error is not None:
                results.append(
                    SubmissionFailure(
                        conversation_id=conversation.conversation_id,
                        error=item.error,
                    )
                )
                continue
            record = evaluations.get(item.evaluation_id or "")
            if record is None:
                raise invalid("Selected evaluation does not exist in this dataset")
            success = export_assessment(record, conversation)
            assert isinstance(record.metadata, AIModelMetadata)
            if record.metadata.model not in request.model_names:
                raise invalid("Declared model_names do not include the recorded AI model")
            if batch and (
                record.checklist.checklist_id != batch.checklist_id
                or record.checklist.version != batch.checklist_version
            ):
                raise invalid("Evaluation rubric does not match selected batch")
            results.append(success)
        return SubmissionDocument(
            team_name=request.team_name,
            model=SubmissionModel(mode=request.model_mode, name=", ".join(request.model_names)),
            results=results,
        )
