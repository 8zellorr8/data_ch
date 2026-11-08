"""Wire contract between a CPU backend and its private GPU worker."""

from typing import Literal

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


class WorkerOutput(Contract):
    result: EvaluationResult
    metadata: AIModelMetadata
    usage_is_local: Literal[True]
