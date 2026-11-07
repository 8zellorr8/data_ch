"""Snapshot comparison service and pure report calculations; no model calls."""

from decimal import Decimal

from pydantic import Field, model_validator

from .models import AIModelMetadata, Contract, EvaluationRecord
from .ports import QualityReportStore
from .work_models import (
    PositiveInt,
    SafeId,
    Tariff,
    UsageLine,
    UsageRecord,
    UsageReport,
)


class ComparisonPair(Contract):
    reference_id: SafeId
    reference_revision: PositiveInt
    ai_id: SafeId | None = None


class ComparisonManifest(Contract):
    pairs: list[ComparisonPair] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def unique_references(self) -> "ComparisonManifest":
        if len({p.reference_id for p in self.pairs}) != len(self.pairs):
            raise ValueError("Each reference must appear once; use separate reports for model runs")
        return self


class CriterionMetric(Contract):
    checklist_id: str
    checklist_version: int
    criterion_id: str
    kind: str
    compared: int = 0
    matches: int = 0
    accuracy: float | None = None
    na_false_positive: int = 0
    na_false_negative: int = 0
    flag_compared: int = 0
    flag_false_positive: int = 0
    flag_false_negative: int = 0
    flag_na_excluded: int = 0


class PairOutcome(Contract):
    reference_id: str
    ai_id: str | None
    issue: str | None = None
    absolute_score_error: Decimal | None = None
    model: str | None = None
    prompt_version: str | None = None


class QualityReport(Contract):
    purpose: str = "Internal team metrics, not the organizer ranking formula"
    selected: int
    compared: int
    missing_or_mismatched: int
    coverage: float
    score_compared: int
    score_null_excluded: int
    mae: Decimal | None
    criteria: list[CriterionMetric]
    pairs: list[PairOutcome]


class QualityReportService:
    def __init__(self, store: QualityReportStore) -> None:
        self.store = store

    def generate(self, manifest: ComparisonManifest) -> QualityReport:
        ids = {p.reference_id for p in manifest.pairs} | {
            p.ai_id for p in manifest.pairs if p.ai_id
        }
        return compare_evaluations(manifest, self.store.get_evaluation_selection(ids))


def compare_evaluations(
    manifest: ComparisonManifest,
    records: dict[str, EvaluationRecord],
) -> QualityReport:
    metrics: dict[tuple[str, int, str], CriterionMetric] = {}
    outcomes: list[PairOutcome] = []
    score_errors: list[Decimal] = []
    compared, null_excluded = 0, 0
    for pair in manifest.pairs:
        reference = records.get(pair.reference_id)
        ai = records.get(pair.ai_id) if pair.ai_id else None
        outcome = PairOutcome(reference_id=pair.reference_id, ai_id=pair.ai_id)
        outcomes.append(outcome)
        if reference is None or ai is None:
            outcome.issue = "MISSING_REFERENCE" if reference is None else "MISSING_AI"
            continue
        if reference.origin != "manual" or ai.origin != "ai":
            outcome.issue = "WRONG_ORIGIN"
            continue
        if reference.revision != pair.reference_revision:
            outcome.issue = "REFERENCE_REVISION_CHANGED"
            continue
        if (
            reference.conversation_id != ai.conversation_id
            or reference.checklist.checklist_id != ai.checklist.checklist_id
            or reference.checklist.version != ai.checklist.version
            or reference.input_hash != ai.input_hash
        ):
            outcome.issue = "INPUT_MISMATCH"
            continue
        compared += 1
        if isinstance(ai.metadata, AIModelMetadata):
            outcome.model, outcome.prompt_version = ai.metadata.model, ai.metadata.prompt_version
        expected = {a.criterion_id: a.value for a in reference.final.answers}
        actual = {a.criterion_id: a.value for a in ai.original.answers}
        for criterion in reference.checklist.criteria:
            key = (reference.checklist.checklist_id, reference.checklist.version, criterion.id)
            metric = metrics.setdefault(
                key,
                CriterionMetric(
                    checklist_id=key[0],
                    checklist_version=key[1],
                    criterion_id=key[2],
                    kind=criterion.type,
                ),
            )
            want, got = expected[criterion.id], actual[criterion.id]
            metric.compared += 1
            metric.matches += int(want == got)
            metric.accuracy = metric.matches / metric.compared
            metric.na_false_positive += int(got == "na" and want != "na")
            metric.na_false_negative += int(got != "na" and want == "na")
            if criterion.type == "flag":
                if "na" in (want, got):
                    metric.flag_na_excluded += 1
                else:
                    metric.flag_compared += 1
                    metric.flag_false_positive += int(got == "yes" and want == "no")
                    metric.flag_false_negative += int(got == "no" and want == "yes")
        if reference.score.total is None or ai.original_score.total is None:
            null_excluded += 1
        else:
            error = abs(Decimal(str(reference.score.total)) - Decimal(str(ai.original_score.total)))
            score_errors.append(error)
            outcome.absolute_score_error = error
    return QualityReport(
        selected=len(manifest.pairs),
        compared=compared,
        missing_or_mismatched=len(manifest.pairs) - compared,
        coverage=compared / len(manifest.pairs),
        score_compared=len(score_errors),
        score_null_excluded=null_excluded,
        mae=sum(score_errors, Decimal(0)) / len(score_errors) if score_errors else None,
        criteria=[metrics[k] for k in sorted(metrics)],
        pairs=outcomes,
    )


def usage_report(
    records: list[UsageRecord],
    missing: list[str],
    pending: int,
    tariffs: tuple[Tariff, ...],
) -> UsageReport:
    by_model = {t.model: t for t in tariffs}
    rows: list[UsageLine] = []
    totals: dict[str, Decimal] = {}
    for record in records:
        if record.provider_not_called:
            rows.append(
                UsageLine(
                    execution=record,
                    tariff=None,
                    known_cost=Decimal(0),
                    complete=True,
                    limitations=[],
                )
            )
            continue
        usage = record.usage
        tariff = by_model.get(usage.model) if usage else None
        limitations: list[str] = []
        if usage is None:
            limitations.append("USAGE_UNKNOWN")
        elif not usage.complete:
            limitations.append("ADAPTER_USAGE_INCOMPLETE")
        if tariff is None:
            limitations.append("TARIFF_UNKNOWN")
        if not usage or usage.input_tokens is None or usage.output_tokens is None:
            limitations.append("TOKENS_UNKNOWN")
        if record.status in {"running", "pending", "interrupted"}:
            limitations.append("EXECUTION_INCOMPLETE")
        known_cost: Decimal | None = None
        if tariff and usage:
            components = []
            if usage.input_tokens is not None:
                components.append(
                    Decimal(usage.input_tokens) * tariff.input_price / tariff.token_unit
                )
            if usage.output_tokens is not None:
                components.append(
                    Decimal(usage.output_tokens) * tariff.output_price / tariff.token_unit
                )
            if components:
                known_cost = sum(components, Decimal(0))
                totals[tariff.currency] = totals.get(tariff.currency, Decimal(0)) + known_cost
        rows.append(
            UsageLine(
                execution=record,
                tariff=tariff,
                known_cost=known_cost,
                complete=not limitations,
                limitations=limitations,
            )
        )
    coverage_complete = not missing and pending == 0
    return UsageReport(
        rows=rows,
        known_cost_by_currency=totals,
        complete=coverage_complete and all(r.complete for r in rows),
        known_duration_ms=sum(r.duration_ms for r in records if r.duration_ms is not None),
        duration_complete=coverage_complete and all(r.duration_ms is not None for r in records),
        known_calls=sum(r.usage.calls for r in records if r.usage),
        calls_complete=coverage_complete
        and all(
            r.provider_not_called or (r.usage is not None and r.usage.complete) for r in records
        ),
        known_input_tokens=sum(
            r.usage.input_tokens for r in records if r.usage and r.usage.input_tokens is not None
        ),
        known_output_tokens=sum(
            r.usage.output_tokens for r in records if r.usage and r.usage.output_tokens is not None
        ),
        tokens_complete=coverage_complete
        and all(
            r.provider_not_called
            or (
                r.usage is not None
                and r.usage.complete
                and r.usage.input_tokens is not None
                and r.usage.output_tokens is not None
            )
            for r in records
        ),
        missing_ids=missing,
        pending_items=pending,
    )
