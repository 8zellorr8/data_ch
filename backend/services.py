"""Application scenarios. Blocking storage operations run outside the event loop."""

import asyncio
import logging
from collections.abc import Callable
from time import perf_counter
from uuid import uuid4

from pydantic import ValidationError

from .async_utils import durable_thread
from .clock import utc_now
from .domain import new_appeal, new_evaluation, resolve_appeal
from .errors import DomainError, InvalidEvaluationResult
from .evaluator import EvaluationContext, Evaluator, EvaluatorOutput
from .models import (
    AIModelMetadata,
    AppealDecision,
    AppealRecord,
    AppealRequest,
    DatasetSummary,
    EvaluationRecord,
    EvaluationRequest,
    EvaluationResult,
    ManualEvaluationRequest,
    ManualMetadata,
    OrganizerImport,
)
from .ports import AppealStore, DatasetSource, DatasetStore, EvaluationStore
from .work_models import ProviderUsage

logger = logging.getLogger(__name__)


class EvaluationService:
    def __init__(self, store: EvaluationStore, evaluator: Evaluator, timeout: float = 180) -> None:
        self.store = store
        self.evaluator = evaluator
        self.timeout = timeout

    def context_for(self, request: EvaluationRequest) -> EvaluationContext:
        return EvaluationContext(
            self.store.get_conversation(request.conversation_id),
            self.store.get_checklist(request.checklist_id, request.checklist_version),
        )

    async def evaluate(
        self,
        request: EvaluationRequest,
        *,
        commit: Callable[[EvaluationRecord], EvaluationRecord] | None = None,
        report_usage: Callable[[ProviderUsage], None] | None = None,
    ) -> EvaluationRecord:
        context = await asyncio.to_thread(self.context_for, request)
        # A provider may mutate Pydantic objects. Never expose the audit snapshots.
        provider_context = EvaluationContext(
            context.conversation.model_copy(deep=True), context.checklist.model_copy(deep=True)
        )
        started = perf_counter()
        try:
            output = await asyncio.wait_for(
                self.evaluator.evaluate(provider_context), timeout=self.timeout
            )
        except DomainError:
            raise
        except TimeoutError:
            raise DomainError(
                "AI_TIMEOUT", f"Model did not respond within {self.timeout:g} seconds"
            ) from None
        except Exception as exc:
            logger.warning("Evaluator failed (%s)", type(exc).__name__)
            raise DomainError(
                "AI_UNAVAILABLE", "Model evaluator failed; no evaluation was saved"
            ) from None
        # Only provider validation errors map to AI_INVALID_RESULT. Storage errors
        # propagate separately and must never be misreported as a model failure.
        if isinstance(output, EvaluatorOutput):
            try:
                usage = (
                    ProviderUsage.model_validate(output.usage)
                    if output.usage
                    else ProviderUsage(
                        model=output.model,
                        calls=output.calls,
                        input_tokens=output.input_tokens,
                        output_tokens=output.output_tokens,
                    )
                )
                if report_usage:
                    report_usage(usage)
            except ValidationError:
                raise DomainError("AI_INVALID_RESULT", "Invalid usage metadata") from None
        record = await asyncio.to_thread(
            self._prepare, context, output, round((perf_counter() - started) * 1000)
        )
        return await durable_thread(commit or self.store.insert_evaluation, record)

    @staticmethod
    def _prepare(context: EvaluationContext, output: object, duration_ms: int) -> EvaluationRecord:
        invalid_result_message = (
            "Model result failed schema, scale, metadata or evidence validation; nothing was saved"
        )
        if not isinstance(output, EvaluatorOutput):
            raise DomainError("AI_INVALID_RESULT", invalid_result_message)
        # Limit schema-error translation to the data supplied by the adapter.
        try:
            result = EvaluationResult.model_validate(output.result)
            metadata = AIModelMetadata(
                model=output.model,
                prompt_version=output.prompt_version,
                input_tokens=output.input_tokens,
                output_tokens=output.output_tokens,
                calls=output.calls,
                audio_analyzed=output.audio_analyzed,
                duration_ms=duration_ms,
            )
        except ValidationError:
            raise DomainError("AI_INVALID_RESULT", invalid_result_message) from None

        try:
            return new_evaluation(
                context.conversation,
                context.checklist,
                result,
                metadata,
                record_id=str(uuid4()),
                created_at=utc_now(),
            )
        except InvalidEvaluationResult:
            raise DomainError("AI_INVALID_RESULT", invalid_result_message) from None

    def evaluate_manual(
        self,
        request: ManualEvaluationRequest,
        *,
        commit: Callable[[EvaluationRecord], EvaluationRecord] | None = None,
    ) -> EvaluationRecord:
        context = self.context_for(request)
        record = new_evaluation(
            context.conversation,
            context.checklist,
            request.result,
            ManualMetadata(author=request.author),
            record_id=str(uuid4()),
            created_at=utc_now(),
        )
        return (commit or self.store.insert_evaluation)(record)


class AppealService:
    def __init__(self, store: AppealStore) -> None:
        self.store = store

    def create(self, evaluation_id: str, request: AppealRequest) -> AppealRecord:
        evaluation = self.store.get_evaluation(evaluation_id)
        return self.store.insert_appeal(
            new_appeal(evaluation, request, record_id=str(uuid4()), created_at=utc_now())
        )

    def decide(self, appeal_id: str, request: AppealDecision) -> EvaluationRecord:
        appeal = self.store.get_appeal(appeal_id)
        evaluation = self.store.get_evaluation(appeal.evaluation_id)
        updated, resolved = resolve_appeal(evaluation, appeal, request, created_at=utc_now())
        return self.store.commit_review(updated, resolved, request.expected_revision)


class ImportService:
    def __init__(self, store: DatasetStore, source: DatasetSource) -> None:
        self.store, self.source = store, source

    async def import_organizer(self, request: OrganizerImport) -> DatasetSummary:
        dataset = await self.source.fetch(request)
        return await asyncio.to_thread(self.store.import_dataset, dataset)
