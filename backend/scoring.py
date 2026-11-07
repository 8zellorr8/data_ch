"""Deterministic scoring. The model never calculates the final score."""
from decimal import Decimal, ROUND_HALF_UP
from .models import ChecklistDefinition, Conversation, EvaluationResult, Criterion
from .errors import DomainError


def rounded(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def check_value(criterion: Criterion, value):
    if value == "na":
        if not criterion.allow_na:
            raise DomainError("INVALID_ANSWER", f"N/A is not allowed for {criterion.id}", 422)
    elif criterion.type == "scale":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value not in criterion.scale:
            raise DomainError("INVALID_ANSWER", f"Value is outside the scale for {criterion.id}", 422)
    elif value not in ("yes", "no"):
        raise DomainError("INVALID_ANSWER", f"Expected yes/no for {criterion.id}", 422)


def validate_result(checklist: ChecklistDefinition, conversation: Conversation, result: EvaluationResult):
    expected = {c.id: c for c in checklist.criteria}
    ids = [a.criterion_id for a in result.answers]
    if len(set(ids)) != len(ids) or set(ids) != set(expected):
        raise DomainError("INVALID_RESULT", "Exactly one answer for every configured criterion is required", 422)
    messages = {m.id: m.text for m in conversation.transcript}
    for answer in result.answers:
        check_value(expected[answer.criterion_id], answer.value)
        if not answer.explanation.strip():
            raise DomainError("INVALID_RESULT", "Explanation cannot be blank", 422)
        if answer.value == "na" and (not answer.na_reason.strip() or answer.evidence_kind != "not_applicable"):
            raise DomainError("INVALID_RESULT", "N/A requires a reason and not_applicable evidence kind", 422)
        if answer.value != "na" and answer.evidence_kind == "not_applicable":
            raise DomainError("INVALID_RESULT", "not_applicable evidence requires N/A", 422)
        if answer.needs_review and not answer.limitation.strip():
            raise DomainError("INVALID_RESULT", "needs_review requires an explicit limitation", 422)
        for evidence in answer.evidence:
            if not evidence.quote.strip() or evidence.message_id not in messages or evidence.quote not in messages[evidence.message_id]:
                raise DomainError("INVALID_EVIDENCE", f"Quote does not match message for {answer.criterion_id}", 422)


def calculate(checklist: ChecklistDefinition, result: EvaluationResult) -> dict:
    answers = {a.criterion_id: a for a in result.answers}
    if len(answers) != len(result.answers) or set(answers) != {c.id for c in checklist.criteria}:
        raise DomainError("INVALID_RESULT", "Missing, duplicate or unknown criterion", 422)
    earned = Decimal(0)
    maximum = Decimal(0)
    rows, flags, sections = [], [], {}
    for criterion in checklist.criteria:
        value = answers[criterion.id].value
        check_value(criterion, value)
        weight = Decimal(str(criterion.weight))
        applicable = value != "na" and criterion.type != "flag"
        if criterion.type == "flag" and value == "yes":
            flags.append(criterion.id)
        fraction = Decimal(0)
        if applicable:
            fraction = Decimal(str(value)) / Decimal(str(max(criterion.scale))) if criterion.type == "scale" else Decimal(int(value == "yes"))
        points = weight * fraction
        applied_weight = weight if applicable else Decimal(0)
        earned += points
        maximum += applied_weight
        section = sections.setdefault(criterion.section or "Без секции", {"earned": Decimal(0), "weight": Decimal(0)})
        section["earned"] += points
        section["weight"] += applied_weight
        rows.append({"criterion_id": criterion.id, "value": value, "base_weight": float(weight), "raw_points": rounded(points), "_points": points, "_weight": applied_weight})
    for row in rows:
        row["points"] = rounded(row.pop("_points") * 100 / maximum) if maximum else 0.0
        row["effective_weight"] = rounded(row.pop("_weight") * 100 / maximum) if maximum else 0.0
    return {
        "total": rounded(earned * 100 / maximum) if maximum else None,
        "earned": rounded(earned), "applicable_weight": float(maximum), "flags": flags, "criteria": rows,
        "sections": [{"name": name, "total": rounded(s["earned"] * 100 / s["weight"]) if s["weight"] else None} for name, s in sections.items()],
    }
