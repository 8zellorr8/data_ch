from copy import deepcopy
from dataclasses import replace
from decimal import Decimal

import pytest
from pydantic import ValidationError

from backend.config import Settings
from backend.domain import new_evaluation
from backend.models import AIModelMetadata, ChecklistVersion, EvaluationResult, ManualMetadata
from backend.reports import ComparisonManifest, compare_evaluations, usage_report
from backend.work_models import ProviderUsage, Tariff, UsageRecord


def ai_record(conversation, checklist, result, record_id="ai-1"):
    return new_evaluation(
        conversation,
        checklist,
        result,
        AIModelMetadata(model="test-only", prompt_version="v1", duration_ms=12),
        record_id=record_id,
        created_at="2026-01-01T00:00:00+00:00",
    )


def manifest(reference, ai):
    return ComparisonManifest.model_validate(
        {
            "pairs": [
                {
                    "reference_id": reference.id,
                    "reference_revision": reference.revision,
                    "ai_id": ai.id if ai else None,
                }
            ]
        }
    )


def test_quality_uses_original_ai_and_explicit_manual_revision(
    domain_evaluation,
    domain_conversation,
    domain_checklist,
    domain_result,
):
    ai = ai_record(domain_conversation, domain_checklist, domain_result)
    ai.final.answers[0].value = "no"
    ai.score.total = 90
    ai.revision = 2
    report = compare_evaluations(
        manifest(domain_evaluation, ai), {domain_evaluation.id: domain_evaluation, ai.id: ai}
    )
    assert report.mae == Decimal(0) and report.coverage == 1
    assert all(m.matches == 1 for m in report.criteria)
    assert report.pairs[0].model == "test-only"
    selection = manifest(domain_evaluation, ai)
    domain_evaluation.revision = 2
    report = compare_evaluations(selection, {domain_evaluation.id: domain_evaluation, ai.id: ai})
    assert report.coverage == 0 and report.mae is None
    assert report.pairs[0].issue == "REFERENCE_REVISION_CHANGED"


def test_quality_na_and_flags_are_separate(
    domain_evaluation,
    domain_conversation,
    domain_checklist,
    domain_result,
):
    changed = deepcopy(domain_result)
    na_id = next(c.id for c in domain_checklist.criteria if c.allow_na and c.type != "flag")
    flag_id = next(c.id for c in domain_checklist.criteria if c.type == "flag")
    for answer in changed.answers:
        if answer.criterion_id == na_id:
            answer.value, answer.evidence_kind, answer.na_reason = (
                "na",
                "not_applicable",
                "Synthetic reason",
            )
        if answer.criterion_id == flag_id:
            answer.value = "yes"
    ai = ai_record(domain_conversation, domain_checklist, changed)
    report = compare_evaluations(
        manifest(domain_evaluation, ai), {domain_evaluation.id: domain_evaluation, ai.id: ai}
    )
    metrics = {m.criterion_id: m for m in report.criteria}
    assert metrics[na_id].na_false_positive == 1
    assert metrics[na_id].matches == 0
    assert metrics[flag_id].flag_false_positive == 1
    assert metrics[flag_id].flag_false_negative == 0
    assert report.mae == 0  # Redistributed full scores still total 100; flag is separate.


def test_quality_missing_ai_and_version_mismatch_do_not_count_as_zero(
    domain_evaluation,
    domain_conversation,
    domain_checklist,
    domain_result,
):
    ai = ai_record(
        domain_conversation, domain_checklist.model_copy(update={"version": 2}), domain_result
    )
    report = compare_evaluations(
        manifest(domain_evaluation, ai), {domain_evaluation.id: domain_evaluation, ai.id: ai}
    )
    assert report.mae is None and report.compared == 0 and report.missing_or_mismatched == 1
    assert report.pairs[0].issue == "INPUT_MISMATCH"
    report = compare_evaluations(
        manifest(domain_evaluation, None), {domain_evaluation.id: domain_evaluation}
    )
    assert report.pairs[0].issue == "MISSING_AI" and report.coverage == 0


def test_quality_all_na_has_null_mae(domain_conversation):
    rubric = ChecklistVersion.model_validate(
        {
            "checklist_id": "custom",
            "version": 1,
            "created_at": "test",
            "name": "Custom",
            "global_rules": "Rules",
            "criteria": [
                {
                    "id": "one",
                    "name": "One",
                    "type": "binary",
                    "weight": 10,
                    "rule": "Rule",
                    "scale": [],
                    "allow_na": True,
                    "na_rule": "No opportunity",
                }
            ],
        }
    )
    result = EvaluationResult.model_validate(
        {
            "answers": [
                {
                    "criterion_id": "one",
                    "value": "na",
                    "explanation": "Synthetic",
                    "na_reason": "No opportunity",
                    "evidence_kind": "not_applicable",
                    "evidence": [{"message_id": "m1", "quote": "Где инструкция?"}],
                }
            ]
        }
    )
    manual = new_evaluation(
        domain_conversation,
        rubric,
        result,
        ManualMetadata(author="QA"),
        record_id="manual",
        created_at="test",
    )
    ai = ai_record(domain_conversation, rubric, result)
    report = compare_evaluations(manifest(manual, ai), {manual.id: manual, ai.id: ai})
    assert report.coverage == 1 and report.mae is None
    assert report.score_null_excluded == 1 and report.score_compared == 0
    assert report.criteria[0].accuracy == 1


def test_multiple_versions_are_reported_separately(
    domain_evaluation,
    domain_conversation,
    domain_checklist,
    domain_result,
):
    second = domain_checklist.model_copy(update={"version": 2})
    manual2 = new_evaluation(
        domain_conversation,
        second,
        domain_result,
        ManualMetadata(author="QA"),
        record_id="manual-2",
        created_at="test",
    )
    ai1 = ai_record(domain_conversation, domain_checklist, domain_result)
    ai2 = ai_record(domain_conversation, second, domain_result, "ai-2")
    selection = ComparisonManifest(
        pairs=manifest(domain_evaluation, ai1).pairs + manifest(manual2, ai2).pairs
    )
    report = compare_evaluations(
        selection, {r.id: r for r in [domain_evaluation, manual2, ai1, ai2]}
    )
    assert report.compared == 2 and len(report.criteria) == 16
    assert {m.checklist_version for m in report.criteria} == {1, 2}
    with pytest.raises(ValidationError):
        ComparisonManifest(pairs=selection.pairs * 2)


def tariff(**kwargs):
    return Tariff(
        model="test-only",
        currency="USD",
        version="test-1",
        effective_date="2026-01-01",
        token_unit=1_000_000,
        input_price=Decimal("0.10"),
        output_price=Decimal("0.30"),
        **kwargs,
    )


def usage(**kwargs):
    return UsageRecord(
        id="execution-1",
        conversation_id="conversation-1",
        status="failed",
        duration_ms=15,
        usage=ProviderUsage(
            model="test-only", calls=3, input_tokens=101, output_tokens=7, complete=True
        ),
        **kwargs,
    )


def test_cost_exact_decimal_includes_failed_calls_and_tariff_provenance():
    report = usage_report([usage()], [], 0, (tariff(),))
    assert report.known_cost_by_currency == {"USD": Decimal("0.0000122")}
    assert report.known_calls == 3 and report.complete is True
    assert report.rows[0].tariff.version == "test-1"
    assert '"known_cost":"0.0000122"' in report.model_dump_json()


@pytest.mark.parametrize(
    "missing", ["tokens", "tariff", "coverage", "adapter", "pending", "interrupted"]
)
def test_incomplete_cost_is_never_reported_as_full(missing):
    record = usage()
    tariffs, ids, pending = (tariff(),), [], 0
    if missing == "tokens":
        record.usage.output_tokens = None
    elif missing == "tariff":
        tariffs = ()
    elif missing == "coverage":
        ids = ["unavailable"]
    elif missing == "adapter":
        record.usage.complete = False
    elif missing == "pending":
        pending = 1
    elif missing == "interrupted":
        record.status = "interrupted"
    report = usage_report([record], ids, pending, tariffs)
    assert report.complete is False
    if missing == "tokens":
        assert report.known_cost_by_currency == {"USD": Decimal("0.0000101")}
    if missing == "tariff":
        assert report.rows[0].known_cost is None and report.known_cost_by_currency == {}


def test_unknown_usage_is_not_zero_cost():
    record = usage()
    record.usage = None
    report = usage_report([record], [], 0, (tariff(),))
    assert report.rows[0].known_cost is None and report.complete is False
    assert report.calls_complete is False and report.tokens_complete is False


def test_invalid_tariffs_and_settings_rejected():
    with pytest.raises(ValidationError):
        Tariff.model_validate({**tariff().model_dump(), "input_price": "NaN"})
    with pytest.raises(ValueError, match="one active tariff"):
        Settings(tariffs=(tariff(), tariff()))
    with pytest.raises(ValueError, match="positive integers"):
        replace(Settings(), ai_concurrency=0)
