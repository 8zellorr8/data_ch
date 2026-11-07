from math import isfinite
from typing import Annotated, Generic, Literal, Self, TypeVar
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    field_validator,
    model_validator,
)

Text = Annotated[str, Field(min_length=1, max_length=100_000)]
Identifier = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")]
AnswerValue = Literal["yes", "no", "na"] | StrictInt | StrictFloat


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, revalidate_instances="always")


class MessageInput(Contract):
    timestamp: Annotated[str, Field(min_length=1, max_length=100)]
    speaker: Literal["client", "operator", "bot", "system"]
    text: Text


class Message(MessageInput):
    id: Identifier


class ConversationInput(Contract):
    conversation_id: Annotated[str, Field(min_length=1, max_length=200)]
    language: Literal["RU", "KZ", "EN"]
    channel: Literal["agent chat", "voice call"]
    transcript: Annotated[list[MessageInput], Field(min_length=1, max_length=5000)]
    audio_url: str | None = None

    @field_validator("audio_url")
    @classmethod
    def valid_audio(cls, value: str | None) -> str | None:
        if value:
            parsed = urlsplit(value)
            if (
                parsed.scheme not in ("http", "https")
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                raise ValueError("audio_url must be an HTTP(S) URL without credentials")
        return value or None


class Conversation(Contract):
    id: str
    dataset_id: str
    conversation_id: str
    language: Literal["RU", "KZ", "EN"]
    channel: Literal["agent chat", "voice call"]
    transcript: list[Message]
    audio_url: str | None


class DatasetImport(Contract):
    name: Annotated[str, Field(min_length=1, max_length=200)]
    group_id: Annotated[str, Field(min_length=1, max_length=100)] = "DEV"
    records: Annotated[list[ConversationInput], Field(min_length=1, max_length=1000)]

    @model_validator(mode="after")
    def unique_ids(self) -> Self:
        ids = [c.conversation_id for c in self.records]
        if len(set(ids)) != len(ids):
            raise ValueError("conversation_id must be unique inside a dataset")
        return self


class OrganizerImport(Contract):
    group_id: Annotated[str, Field(min_length=1, max_length=100)] = "DEV"
    name: Annotated[str, Field(min_length=1, max_length=200)] = "Organizer dataset"


class Criterion(Contract):
    id: Identifier
    name: Annotated[str, Field(min_length=1, max_length=200)]
    section: Annotated[str, Field(max_length=200)] = ""
    type: Literal["binary", "scale", "flag"]
    weight: Annotated[float, Field(strict=True, ge=0, le=1000)]
    rule: Annotated[str, Field(min_length=1, max_length=20_000)]
    call_rule: Annotated[str, Field(max_length=20_000)] = ""
    allow_na: bool = False
    na_rule: Annotated[str, Field(max_length=5000)] = ""
    scale: list[StrictInt | StrictFloat] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def valid_definition(self) -> Self:
        if not self.name.strip() or not self.rule.strip():
            raise ValueError("name and rule must not be blank")
        if self.allow_na and not self.na_rule.strip():
            raise ValueError("allow_na requires na_rule")
        if self.type == "flag" and self.weight != 0:
            raise ValueError("flag criteria must have zero weight")
        if self.type == "scale":
            if (
                len(self.scale) < 2
                or not all(isfinite(v) for v in self.scale)
                or min(self.scale) != 0
                or max(self.scale) <= 0
                or len(set(self.scale)) != len(self.scale)
            ):
                raise ValueError(
                    "scale must contain unique nonnegative steps including 0 and a positive maximum"
                )
        elif self.scale:
            raise ValueError("only scale criteria can define scale steps")
        return self


class ChecklistDefinition(Contract):
    name: Annotated[str, Field(min_length=1, max_length=200)]
    global_rules: Annotated[str, Field(max_length=20_000)] = ""
    criteria: Annotated[list[Criterion], Field(min_length=1, max_length=40)]

    @model_validator(mode="after")
    def unique_criteria(self) -> Self:
        if not self.name.strip():
            raise ValueError("name must not be blank")
        ids = [c.id for c in self.criteria]
        if len(set(ids)) != len(ids):
            raise ValueError("criterion ids must be unique")
        return self


class ChecklistVersion(ChecklistDefinition):
    checklist_id: str
    version: int
    created_at: str


class NewChecklistVersion(Contract):
    expected_version: Annotated[int, Field(strict=True, ge=1)]
    definition: ChecklistDefinition


class Evidence(Contract):
    message_id: Identifier
    quote: Text


class CriterionAnswer(Contract):
    criterion_id: Identifier
    value: AnswerValue
    explanation: Annotated[str, Field(min_length=1, max_length=20_000)]
    evidence_kind: Literal["present", "missing", "not_applicable"]
    evidence: Annotated[list[Evidence], Field(min_length=1, max_length=30)]
    na_reason: Annotated[str, Field(max_length=5000)] = ""
    needs_review: bool = False
    limitation: Annotated[str, Field(max_length=5000)] = ""


class EvaluationResult(Contract):
    answers: Annotated[list[CriterionAnswer], Field(min_length=1, max_length=40)]


class EvaluationRequest(Contract):
    conversation_id: str = Field(description="Internal UUID returned by the conversations API")
    checklist_id: str = "freedom-broker"
    checklist_version: Annotated[int, Field(strict=True, ge=1)] = 1


class ManualEvaluationRequest(EvaluationRequest):
    result: EvaluationResult
    author: Annotated[str, Field(min_length=1, max_length=100)]


class AppealRequest(Contract):
    criterion_id: Identifier
    comment: Annotated[str, Field(min_length=1, max_length=5000)]
    author: Annotated[str, Field(min_length=1, max_length=100)]


class AppealDecision(Contract):
    expected_revision: Annotated[int, Field(strict=True, ge=1)]
    value: AnswerValue
    reason: Annotated[str, Field(min_length=1, max_length=5000)]
    reviewer: Annotated[str, Field(min_length=1, max_length=100)]
    evidence_kind: Literal["present", "missing", "not_applicable"]
    evidence: Annotated[list[Evidence], Field(min_length=1, max_length=30)]

    @field_validator("reason", "reviewer")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class CriterionScore(Contract):
    criterion_id: str
    value: AnswerValue
    base_weight: float
    raw_points: float
    points: float
    effective_weight: float


class SectionScore(Contract):
    name: str
    total: float | None


class Score(Contract):
    total: float | None
    earned: float
    applicable_weight: float
    flags: list[str]
    criteria: list[CriterionScore]
    sections: list[SectionScore]


class AIModelMetadata(Contract):
    model: Annotated[str, Field(min_length=1, max_length=200)]
    prompt_version: Annotated[str, Field(min_length=1, max_length=200)]
    input_tokens: Annotated[int, Field(strict=True, ge=0)] | None = None
    output_tokens: Annotated[int, Field(strict=True, ge=0)] | None = None
    calls: Annotated[int, Field(strict=True, ge=1)] = 1
    audio_analyzed: Annotated[bool, Field(strict=True)] = False
    duration_ms: Annotated[int, Field(strict=True, ge=0)]

    @field_validator("model", "prompt_version")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class ManualMetadata(Contract):
    author: str
    audio_analyzed: bool = False


class ReviewRecord(Contract):
    appeal_id: str
    criterion_id: str
    previous_value: AnswerValue
    value: AnswerValue
    reason: str
    reviewer: str
    evidence_kind: Literal["present", "missing", "not_applicable"]
    evidence: list[Evidence]
    created_at: str


class AppealRecord(Contract):
    id: str
    evaluation_id: str
    criterion_id: str
    comment: str
    author: str
    status: Literal["open", "resolved"]
    opened_revision: int
    created_at: str
    decision: ReviewRecord | None = None


class EvaluationRecord(Contract):
    id: str
    conversation_id: str
    origin: Literal["ai", "manual"]
    revision: int
    created_at: str
    conversation: Conversation
    checklist: ChecklistVersion
    input_hash: str
    original: EvaluationResult
    final: EvaluationResult
    original_score: Score
    score: Score
    metadata: AIModelMetadata | ManualMetadata
    reviews: list[ReviewRecord] = Field(default_factory=list)
    appeals: list[AppealRecord] = Field(default_factory=list)


class DatasetInfo(Contract):
    id: str
    name: str
    group_id: str
    created_at: str


class DatasetSummary(DatasetInfo):
    count: int


class ConversationSummary(Contract):
    id: str
    dataset_id: str
    conversation_id: str
    language: Literal["RU", "KZ", "EN"]
    channel: Literal["agent chat", "voice call"]
    audio_url: str | None
    message_count: int


class ChecklistSummary(Contract):
    checklist_id: str
    version: int
    name: str
    created_at: str


class EvaluationSummary(Contract):
    id: str
    origin: Literal["ai", "manual"]
    revision: int
    created_at: str
    score: float | None


Item = TypeVar("Item")


class Items(Contract, Generic[Item]):
    items: list[Item]


class Page(Items[Item], Generic[Item]):
    total: int
    limit: int
    offset: int


class DatasetExport(Contract):
    schema_version: Literal[1] = 1
    exported_at: str
    dataset: DatasetInfo
    conversations: list[Conversation]
    evaluations: list[EvaluationRecord]


class Health(Contract):
    status: Literal["ok"] = "ok"
    database: Literal["sqlite"] = "sqlite"
    ai_configured: bool
