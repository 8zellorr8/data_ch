from typing import Annotated, Literal
from math import isfinite
from urllib.parse import urlsplit
from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt, field_validator, model_validator

Text = Annotated[str, Field(min_length=1, max_length=100_000)]
Identifier = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")]
AnswerValue = Literal["yes", "no", "na"] | StrictInt | StrictFloat


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


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
    def valid_audio(cls, value):
        if value:
            parsed = urlsplit(value)
            if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
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
    def unique_ids(self):
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
    def valid_definition(self):
        if not self.name.strip() or not self.rule.strip():
            raise ValueError("name and rule must not be blank")
        if self.allow_na and not self.na_rule.strip():
            raise ValueError("allow_na requires na_rule")
        if self.type == "flag" and self.weight != 0:
            raise ValueError("flag criteria must have zero weight")
        if self.type == "scale":
            if len(self.scale) < 2 or not all(isfinite(v) for v in self.scale) or min(self.scale) != 0 or max(self.scale) <= 0 or len(set(self.scale)) != len(self.scale):
                raise ValueError("scale must contain unique nonnegative steps including 0 and a positive maximum")
        elif self.scale:
            raise ValueError("only scale criteria can define scale steps")
        return self


class ChecklistDefinition(Contract):
    name: Annotated[str, Field(min_length=1, max_length=200)]
    global_rules: Annotated[str, Field(max_length=20_000)] = ""
    criteria: Annotated[list[Criterion], Field(min_length=1, max_length=40)]

    @model_validator(mode="after")
    def unique_criteria(self):
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
    def not_blank(cls, value):
        if not value.strip():
            raise ValueError("must not be blank")
        return value
