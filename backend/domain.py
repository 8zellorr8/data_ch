"""Evaluation and review invariants. No database, HTTP or provider dependencies."""

import hashlib
import json

from .errors import DomainError
from .models import (
    AIModelMetadata,
    AppealDecision,
    AppealRecord,
    AppealRequest,
    ChecklistVersion,
    Conversation,
    CriterionAnswer,
    EvaluationRecord,
    EvaluationResult,
    ManualMetadata,
    ReviewRecord,
)
from .scoring import calculate, validate_result


def new_evaluation(
    conversation: Conversation,
    checklist: ChecklistVersion,
    result: EvaluationResult,
    metadata: AIModelMetadata | ManualMetadata,
    *,
    record_id: str,
    created_at: str,
) -> EvaluationRecord:
    validate_result(checklist, conversation, result)
    snapshot = {"conversation": conversation.model_dump(), "checklist": checklist.model_dump()}
    score = calculate(checklist, result)
    return EvaluationRecord(
        id=record_id,
        conversation_id=conversation.id,
        origin="ai" if isinstance(metadata, AIModelMetadata) else "manual",
        revision=1,
        created_at=created_at,
        conversation=conversation.model_copy(deep=True),
        checklist=checklist.model_copy(deep=True),
        input_hash=hashlib.sha256(
            json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest(),
        original=result.model_copy(deep=True),
        final=result.model_copy(deep=True),
        original_score=score.model_copy(deep=True),
        score=score,
        metadata=metadata,
    )


def new_appeal(
    evaluation: EvaluationRecord, request: AppealRequest, *, record_id: str, created_at: str
) -> AppealRecord:
    if request.criterion_id not in {c.id for c in evaluation.checklist.criteria}:
        raise DomainError("UNKNOWN_CRITERION", "Criterion is not in this evaluation")
    return AppealRecord(
        id=record_id,
        evaluation_id=evaluation.id,
        criterion_id=request.criterion_id,
        comment=request.comment,
        author=request.author,
        status="open",
        opened_revision=evaluation.revision,
        created_at=created_at,
    )


def resolve_appeal(
    evaluation: EvaluationRecord,
    appeal: AppealRecord,
    decision: AppealDecision,
    *,
    created_at: str,
) -> tuple[EvaluationRecord, AppealRecord]:
    if appeal.evaluation_id != evaluation.id:
        raise DomainError("INVALID_APPEAL", "Appeal does not belong to this evaluation")
    if appeal.status != "open":
        raise DomainError("APPEAL_CLOSED", "This appeal was already resolved")
    if evaluation.revision != decision.expected_revision:
        raise DomainError(
            "REVISION_CONFLICT", "Evaluation changed; reload before submitting a decision"
        )
    updated = evaluation.model_copy(deep=True)
    previous = next(
        (a for a in updated.final.answers if a.criterion_id == appeal.criterion_id), None
    )
    if previous is None:
        raise DomainError("UNKNOWN_CRITERION", "Criterion is not in this evaluation")
    replacement = CriterionAnswer(
        criterion_id=appeal.criterion_id,
        value=decision.value,
        explanation=decision.reason,
        na_reason=decision.reason if decision.value == "na" else "",
        evidence_kind=decision.evidence_kind,
        evidence=decision.evidence,
    )
    updated.final.answers = [
        replacement if a.criterion_id == appeal.criterion_id else a for a in updated.final.answers
    ]
    validate_result(updated.checklist, updated.conversation, updated.final)
    updated.score = calculate(updated.checklist, updated.final)
    updated.revision += 1
    review = ReviewRecord(
        appeal_id=appeal.id,
        criterion_id=appeal.criterion_id,
        previous_value=previous.value,
        value=decision.value,
        reason=decision.reason,
        reviewer=decision.reviewer,
        evidence_kind=decision.evidence_kind,
        evidence=decision.evidence,
        created_at=created_at,
    )
    updated.reviews.append(review)
    resolved = appeal.model_copy(deep=True)
    resolved.status, resolved.decision = "resolved", review
    updated.appeals = [resolved if a.id == appeal.id else a for a in updated.appeals]
    return updated, resolved
