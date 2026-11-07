"""Integration boundary owned by the AI teammate. No fake runtime evaluations."""

from dataclasses import dataclass
from math import isfinite
from typing import Protocol

from .errors import DomainError, ErrorCode
from .models import ChecklistVersion, Conversation, EvaluationResult
from .work_models import ProviderUsage


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
    usage: ProviderUsage | None = None


class EvaluatorFailure(DomainError):
    """Optional usage on a failed provider call; never include raw provider errors."""

    def __init__(self, code: ErrorCode, message: str, usage: ProviderUsage | None = None) -> None:
        super().__init__(code, message)
        self.usage = usage


class Evaluator(Protocol):
    configured: bool

    async def evaluate(self, context: EvaluationContext) -> EvaluatorOutput: ...


class ProviderDeferred(EvaluatorFailure):
    """Known refusal/cooldown, distinct from an ambiguous network interruption."""

    def __init__(
        self,
        retry_at: float,
        *,
        quota_exhausted: bool = False,
        usage: ProviderUsage | None = None,
        provider_not_called: bool = False,
        safe_to_retry: bool = True,
    ) -> None:
        if not isfinite(retry_at) or retry_at < 0:
            raise ValueError("Retry time must be finite and nonnegative")
        # Keep the existing HTTP error contract for upstream failures.
        super().__init__(
            "AI_UNAVAILABLE",
            "AI daily quota exhausted; retry after cooldown"
            if quota_exhausted
            else "AI rate limited; retry after cooldown",
            usage,
        )
        self.retry_at = retry_at
        self.quota_exhausted = quota_exhausted
        self.provider_not_called = provider_not_called
        self.safe_to_retry = safe_to_retry

    @property
    def batch_error_code(self) -> str:
        return "AI_QUOTA_EXHAUSTED" if self.quota_exhausted else "AI_RATE_LIMITED"


class RequestGate(Protocol):
    async def wait(self, model: str) -> None: ...
    async def defer(self, model: str, retry_at: float, quota_exhausted: bool) -> None: ...


class UnconfiguredEvaluator:
    configured = False

    async def evaluate(self, context: EvaluationContext) -> EvaluatorOutput:
        raise DomainError(
            "AI_NOT_CONFIGURED",
            "AI is not connected yet. Import, checklists and manual evaluations are available.",
        )
