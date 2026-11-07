"""Admission, durable idempotency and accounting around the existing evaluator."""

import asyncio
import hashlib
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from threading import BoundedSemaphore
from time import perf_counter, time

from pydantic import ValidationError

from .async_utils import durable_thread, finish_task
from .errors import DomainError
from .evaluator import EvaluatorFailure
from .models import EvaluationRecord, EvaluationRequest, ManualEvaluationRequest
from .observability import execution_id_context
from .ports import ExecutionStore
from .services import EvaluationService
from .work_models import ProviderUsage

logger = logging.getLogger(__name__)


class AdmissionLimit:
    """Nonblocking, process-local admission. No waiting HTTP requests accumulate."""

    def __init__(self, maximum: int) -> None:
        self._slots = BoundedSemaphore(maximum)

    @contextmanager
    def slot(self) -> Iterator[None]:
        if not self._slots.acquire(blocking=False):
            raise DomainError("AI_BUSY", "All AI evaluation slots are occupied")
        try:
            yield
        finally:
            self._slots.release()


def request_fingerprint(request: EvaluationRequest) -> str:
    return hashlib.sha256(
        json.dumps(
            request.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()


class EvaluationCoordinator:
    def __init__(
        self,
        service: EvaluationService,
        store: ExecutionStore,
        owner: str,
        limit: AdmissionLimit,
    ) -> None:
        self.service, self.store, self.owner, self.limit = service, store, owner, limit

    async def evaluate(
        self,
        request: EvaluationRequest,
        key: str | None = None,
        *,
        item_id: str | None = None,
    ) -> EvaluationRecord:
        fingerprint = request_fingerprint(request)
        key_hash = hashlib.sha256(key.encode()).hexdigest() if key else None
        if key_hash:
            replay = await asyncio.to_thread(self.store.replay, "ai", key_hash, fingerprint)
            if replay and replay.replay:
                execution_id_context.set(replay.id)
                return replay.replay
        # Validate references before reserving FK-backed bookkeeping rows.
        await asyncio.to_thread(self.service.context_for, request)
        if not self.service.evaluator.configured:
            raise DomainError(
                "AI_NOT_CONFIGURED",
                "AI is not connected yet. Import, checklists and manual evaluations are available.",
            )
        with self.limit.slot():
            reservation_task = asyncio.create_task(
                asyncio.to_thread(
                    self.store.reserve,
                    self.owner,
                    time(),
                    "ai",
                    key_hash,
                    fingerprint,
                    request.conversation_id,
                    item_id,
                )
            )
            try:
                reservation = await asyncio.shield(reservation_task)
            except asyncio.CancelledError:
                reservation = await finish_task(reservation_task)
                if reservation.replay is None:
                    await durable_thread(
                        self.store.fail, reservation.id, "EVALUATION_INTERRUPTED", 0, None, True
                    )
                raise
            execution_id_context.set(reservation.id)
            if reservation.replay:
                return reservation.replay
            started = perf_counter()
            usage: ProviderUsage | None = None

            def capture(value: ProviderUsage) -> None:
                nonlocal usage
                usage = value

            def elapsed() -> int:
                return round((perf_counter() - started) * 1000)

            def commit(record: EvaluationRecord) -> EvaluationRecord:
                return self.store.complete(
                    reservation.id, self.owner, time(), record, elapsed(), usage
                )

            try:
                # Join the evaluation task on cancellation: a SQLite commit already
                # dispatched by the service must finish before terminal bookkeeping.
                task = asyncio.create_task(
                    self.service.evaluate(
                        request,
                        commit=commit,
                        report_usage=capture,
                    )
                )
                record = await task
                return record
            except BaseException as exc:
                if isinstance(exc, EvaluatorFailure) and exc.usage is not None:
                    try:
                        usage = ProviderUsage.model_validate(exc.usage)
                    except ValidationError:
                        usage = None
                code = exc.code if isinstance(exc, DomainError) else "INTERNAL_ERROR"
                interrupted = isinstance(exc, asyncio.CancelledError)
                if interrupted:
                    code = "EVALUATION_INTERRUPTED"
                try:
                    await durable_thread(
                        self.store.fail, reservation.id, code, elapsed(), usage, interrupted
                    )
                except DomainError:
                    # The failed audit write is visible in logs; the lease will
                    # eventually mark the unfinished execution as interrupted.
                    logger.error("Could not finalize execution bookkeeping")
                raise

    def manual(self, request: ManualEvaluationRequest, key: str | None = None) -> EvaluationRecord:
        if key is None:
            return self.service.evaluate_manual(request)
        fingerprint, key_hash = (
            request_fingerprint(request),
            hashlib.sha256(key.encode()).hexdigest(),
        )
        replay = self.store.replay("manual", key_hash, fingerprint)
        if replay and replay.replay:
            execution_id_context.set(replay.id)
            return replay.replay
        self.service.context_for(request)
        reservation = self.store.reserve(
            self.owner, time(), "manual", key_hash, fingerprint, request.conversation_id
        )
        execution_id_context.set(reservation.id)
        if reservation.replay:
            return reservation.replay
        started = perf_counter()

        def commit(record: EvaluationRecord) -> EvaluationRecord:
            return self.store.complete(
                reservation.id,
                self.owner,
                time(),
                record,
                round((perf_counter() - started) * 1000),
                None,
            )

        try:
            return self.service.evaluate_manual(request, commit=commit)
        except Exception as exc:
            code = exc.code if isinstance(exc, DomainError) else "INTERNAL_ERROR"
            try:
                self.store.fail(
                    reservation.id, code, round((perf_counter() - started) * 1000), None
                )
            except DomainError:
                logger.error("Could not finalize manual execution bookkeeping")
            raise
