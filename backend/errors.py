"""Application error meanings. HTTP status mapping belongs to the HTTP adapter."""

from typing import Literal

EvaluationErrorCode = Literal["INVALID_RESULT", "INVALID_ANSWER", "INVALID_EVIDENCE"]
ErrorCode = (
    Literal[
        "NOT_FOUND",
        "STORAGE_ERROR",
        "STORAGE_BUSY",
        "VERSION_CONFLICT",
        "REVISION_CONFLICT",
        "APPEAL_EXISTS",
        "APPEAL_CLOSED",
        "UNKNOWN_CRITERION",
        "INVALID_APPEAL",
        "AI_NOT_CONFIGURED",
        "AI_TIMEOUT",
        "AI_UNAVAILABLE",
        "AI_INVALID_RESULT",
        "ORGANIZER_NOT_CONFIGURED",
        "ORGANIZER_CONFIG_ERROR",
        "ORGANIZER_INVALID_RESPONSE",
        "ORGANIZER_UNAVAILABLE",
        "DATASET_TOO_LARGE",
        "AI_BUSY",
        "IDEMPOTENCY_CONFLICT",
        "EVALUATION_IN_PROGRESS",
        "EVALUATION_INTERRUPTED",
        "EVALUATION_FAILED",
        "BATCH_QUEUE_FULL",
        "INVALID_SUBMISSION",
    ]
    | EvaluationErrorCode
)


class DomainError(Exception):
    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class InvalidEvaluationResult(DomainError):
    """An answer violates a rubric or its transcript evidence."""

    def __init__(self, code: EvaluationErrorCode, message: str) -> None:
        super().__init__(code, message)
