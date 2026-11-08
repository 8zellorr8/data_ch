"""Wire contract between a CPU backend and its private GPU worker."""

from typing import Literal, Self

from pydantic import model_validator

from backend.models import (
    AIModelMetadata,
    ChecklistVersion,
    Contract,
    Conversation,
    EvaluationResult,
)


class WorkerInput(Contract):
    conversation: Conversation
    checklist: ChecklistVersion

    @model_validator(mode="after")
    def valid_evidence_source(self) -> Self:
        messages = self.conversation.transcript
        if not 1 <= len(messages) <= 5000:
            raise ValueError("A transcript must contain 1 to 5000 messages")
        ids = [message.id for message in messages]
        if len(set(ids)) != len(ids):
            raise ValueError("Transcript message IDs must be unique")
        if not any(message.text.strip() for message in messages):
            raise ValueError("The transcript has no nonblank evidence source")
        return self


class WorkerOutput(Contract):
    result: EvaluationResult
    metadata: AIModelMetadata
    usage_is_local: Literal[True]
