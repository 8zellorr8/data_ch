"""Pure arithmetic tests: no HTTP application or database fixtures."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from backend.errors import DomainError
from backend.models import ChecklistDefinition, ChecklistVersion, EvaluationResult
from backend.scoring import calculate


def score(checklist, answers):
    return calculate(
        ChecklistVersion.model_validate(checklist), EvaluationResult.model_validate(answers)
    ).model_dump()


def test_perfect_and_gross_flag_are_independent(checklist, answers):
    assert score(checklist, answers)["total"] == 100
    answers["answers"][-1]["value"] = "yes"
    result = score(checklist, answers)
    assert result["total"] == 100
    assert result["flags"] == ["gross"]


def test_na_redistribution_and_scale_normalization(checklist, answers):
    values = {"needs": "na", "identity": "na", "consultation": 15, "management": 7}
    for answer in answers["answers"]:
        answer["value"] = values.get(answer["criterion_id"], answer["value"])
    result = score(checklist, answers)
    assert result["applicable_weight"] == 65
    assert result["earned"] == 55.5
    assert result["total"] == 85.38
    rows = {r["criterion_id"]: r for r in result["criteria"]}
    assert rows["management"]["raw_points"] == 10.5
    assert rows["needs"]["effective_weight"] == 0
    assert rows["management"]["points"] == 16.15


def test_no_applicable_weights_has_no_numeric_score():
    checklist = ChecklistDefinition(
        name="Custom",
        criteria=[
            {
                "id": "x",
                "name": "X",
                "type": "binary",
                "weight": 10,
                "rule": "Rule",
                "allow_na": True,
                "na_rule": "When irrelevant",
            }
        ],
    )
    result = EvaluationResult(
        answers=[
            {
                "criterion_id": "x",
                "value": "na",
                "explanation": "Not relevant",
                "na_reason": "Not relevant",
                "evidence_kind": "not_applicable",
                "evidence": [{"message_id": "m1", "quote": "Context"}],
            }
        ]
    )
    assert calculate(checklist, result).total is None


@pytest.mark.parametrize(
    "criterion,value", [("contact", "na"), ("contact", 1), ("consultation", 7), ("gross", 0)]
)
def test_rejects_values_outside_rubric(checklist, answers, criterion, value):
    next(a for a in answers["answers"] if a["criterion_id"] == criterion)["value"] = value
    with pytest.raises(DomainError):
        score(checklist, answers)


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "unknown"])
def test_criterion_set_must_match_exactly(checklist, answers, mutation):
    if mutation == "missing":
        answers["answers"].pop()
    elif mutation == "duplicate":
        answers["answers"].append(deepcopy(answers["answers"][0]))
    else:
        answers["answers"][0]["criterion_id"] = "unknown"
    with pytest.raises(DomainError):
        score(checklist, answers)


@pytest.mark.parametrize("value", [True, False, float("nan"), float("inf")])
def test_boolean_and_nonfinite_answers_rejected(answers, value):
    answers["answers"][0]["value"] = value
    with pytest.raises(ValidationError):
        EvaluationResult.model_validate(answers)


def test_total_rounding_uses_unrounded_contributions():
    rubric = ChecklistDefinition(
        name="Precision",
        criteria=[
            {
                "id": f"c{i}",
                "name": "Third",
                "type": "scale",
                "weight": 1,
                "rule": "Rule",
                "scale": [0, 1, 3],
            }
            for i in range(3)
        ],
    )
    result = EvaluationResult(
        answers=[
            {
                "criterion_id": f"c{i}",
                "value": 1,
                "explanation": "Test",
                "evidence_kind": "present",
                "evidence": [{"message_id": "m1", "quote": "Context"}],
            }
            for i in range(3)
        ]
    )
    assert calculate(rubric, result).total == 33.33
