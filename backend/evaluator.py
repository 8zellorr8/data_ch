"""Integration boundary owned by the AI teammate. No fake runtime evaluations."""

from dataclasses import dataclass
from typing import Protocol

from .errors import DomainError
from .models import ChecklistVersion, Conversation, EvaluationResult


@dataclass(frozen=True)
class EvaluationContext:
    conversation: Conversation
    checklist: ChecklistVersion


@dataclass(frozen=True)
class EvaluatorOutput:
    result: EvaluationResult
    model: str
    prompt_version: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    calls: int = 1
    audio_analyzed: bool = False


class Evaluator(Protocol):
    configured: bool

    async def evaluate(self, context: EvaluationContext) -> EvaluatorOutput: ...


class UnconfiguredEvaluator:
    configured = False

    async def evaluate(self, context: EvaluationContext) -> EvaluatorOutput:
        raise DomainError(
            "AI_NOT_CONFIGURED",
            "AI is not connected yet. Import, checklists and manual evaluations are available.",
        )
