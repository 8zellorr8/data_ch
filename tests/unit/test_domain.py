from copy import deepcopy

import pytest

from backend.domain import new_appeal, new_evaluation, resolve_appeal
from backend.errors import DomainError, InvalidEvaluationResult
from backend.models import AppealDecision, AppealRequest, ManualMetadata


def make_appeal(evaluation):
    return new_appeal(
        evaluation,
        AppealRequest(criterion_id="contact", comment="Recheck", author="Operator"),
        record_id="appeal-1",
        created_at="2026-01-01T01:00:00+00:00",
    )


def make_decision(value="no"):
    return AppealDecision(
        expected_revision=1,
        value=value,
        reason="Review",
        reviewer="QA",
        evidence_kind="missing",
        evidence=[{"message_id": "m1", "quote": "Где инструкция?"}],
    )


def test_new_evaluation_detaches_original_from_mutable_inputs(
    domain_conversation, domain_checklist, domain_result
):
    record = new_evaluation(
        domain_conversation,
        domain_checklist,
        domain_result,
        ManualMetadata(author="QA"),
        record_id="e1",
        created_at="fixed-time",
    )
    original = record.original.model_dump()
    record.final.answers[0].value = "no"
    record.score.total = 0
    domain_result.answers[0].explanation = "Changed outside aggregate"
    domain_conversation.transcript[0].text = "Changed transcript"
    domain_checklist.criteria[0].weight = 999
    assert record.original.model_dump() == original
    assert record.original_score.total == 100
    assert record.conversation.transcript[0].text == "Здравствуйте! Где инструкция?"
    assert record.checklist.criteria[0].weight == 10


def test_review_recalculates_a_copy_and_keeps_original(domain_evaluation):
    appeal = make_appeal(domain_evaluation)
    domain_evaluation.appeals.append(appeal)
    before = domain_evaluation.model_dump()
    updated, resolved = resolve_appeal(
        domain_evaluation, appeal, make_decision(), created_at="review-time"
    )
    assert domain_evaluation.model_dump() == before
    assert appeal.status == "open"
    assert updated.revision == 2 and updated.score.total == 90
    assert updated.original.model_dump() == before["original"]
    assert updated.original_score.total == 100
    assert resolved.status == "resolved" and resolved.decision.previous_value == "yes"
    assert updated.appeals[0] == resolved


@pytest.mark.parametrize("invalid", ["scale", "quote"])
def test_invalid_review_does_not_change_inputs(domain_evaluation, invalid):
    appeal = make_appeal(domain_evaluation)
    request = make_decision()
    if invalid == "scale":
        request.value = 7
    else:
        request.evidence[0].quote = "Never said"
    before = deepcopy(domain_evaluation.model_dump())
    with pytest.raises(InvalidEvaluationResult) as error:
        resolve_appeal(domain_evaluation, appeal, request, created_at="review-time")
    assert error.value.code == ("INVALID_ANSWER" if invalid == "scale" else "INVALID_EVIDENCE")
    assert domain_evaluation.model_dump() == before and appeal.status == "open"


def test_stale_review_is_a_business_conflict(domain_evaluation):
    request = make_decision()
    request.expected_revision = 2
    with pytest.raises(DomainError) as error:
        resolve_appeal(
            domain_evaluation, make_appeal(domain_evaluation), request, created_at="time"
        )
    assert error.value.code == "REVISION_CONFLICT"


def test_unknown_criterion_cannot_be_appealed(domain_evaluation):
    with pytest.raises(DomainError) as error:
        new_appeal(
            domain_evaluation,
            AppealRequest(criterion_id="unknown", comment="Recheck", author="QA"),
            record_id="appeal-1",
            created_at="time",
        )
    assert error.value.code == "UNKNOWN_CRITERION"
