"""One bounded batch consumer per app; durable claims allow multiple consumers."""

import asyncio
import hashlib
import logging
from time import time

from .async_utils import durable_thread
from .errors import DomainError
from .execution import EvaluationCoordinator
from .models import Page
from .ports import BatchStore, OwnerStore
from .work_models import BatchRecord, BatchRequest, BatchSummary

logger = logging.getLogger(__name__)


class BatchService:
    def __init__(
        self, coordinator: EvaluationCoordinator, store: BatchStore, queue_limit: int
    ) -> None:
        self.coordinator, self.store, self.queue_limit = coordinator, store, queue_limit
        self.wake = asyncio.Event()
        self._stopping = False

    def stop(self) -> None:
        # Cancellation can race with a completed wait; never rely on it alone
        # to stop a long-lived consumer from starting its next iteration.
        self._stopping = True
        self.wake.set()

    async def create(self, request: BatchRequest, key: str | None = None) -> BatchRecord:
        key_hash = hashlib.sha256(key.encode()).hexdigest() if key else None
        if key_hash:
            replay = await asyncio.to_thread(self.store.replay_batch, request, key_hash)
            if replay is not None:
                # Reading a previous acceptance must work even if AI was disabled.
                return replay
        if not self.coordinator.service.evaluator.configured:
            raise DomainError("AI_NOT_CONFIGURED", "AI is not connected yet")
        result = await durable_thread(self.store.create_batch, request, self.queue_limit, key_hash)
        self.wake.set()
        return result

    def list(self, limit: int = 20, offset: int = 0) -> Page[BatchSummary]:
        return self.store.list_batches(limit, offset)

    async def consume(self) -> None:
        while not self._stopping:
            self.wake.clear()
            try:
                if self.coordinator.service.evaluator.configured:
                    item = await durable_thread(self.store.claim, self.coordinator.owner, time())
                    if item:
                        try:
                            await self.coordinator.evaluate(item.request, item_id=item.id)
                        except DomainError as exc:
                            await durable_thread(
                                self.store.finish_item,
                                item.id,
                                self.coordinator.owner,
                                exc.code,
                                retry_busy=exc.code == "AI_BUSY",
                            )
                            if exc.code == "AI_BUSY":
                                await asyncio.sleep(0.25)
                        except Exception:
                            logger.error("Batch evaluation failed unexpectedly")
                            await durable_thread(
                                self.store.finish_item,
                                item.id,
                                self.coordinator.owner,
                                "INTERNAL_ERROR",
                            )
                        continue
            except DomainError as exc:
                logger.warning("Batch storage temporarily unavailable (%s)", exc.code)
            try:
                async with asyncio.timeout(1):
                    await self.wake.wait()
            except TimeoutError:
                pass


class ExecutionRuntime:
    def __init__(self, batches: BatchService, store: OwnerStore, owner: str, lease: float) -> None:
        self.batches, self.store, self.owner, self.lease = batches, store, owner, lease
        self.tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        await durable_thread(self.store.register_owner, self.owner, time(), self.lease)
        self.tasks = [
            asyncio.create_task(self.batches.consume()),
            asyncio.create_task(self._heartbeat()),
        ]

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(self.lease / 3)
            try:
                await durable_thread(self.store.heartbeat, self.owner, time(), self.lease)
            except DomainError as exc:
                logger.error("Execution heartbeat failed (%s)", exc.code)
                if exc.code == "EVALUATION_INTERRUPTED":
                    self.batches.stop()
                    self.tasks[0].cancel()
                    return

    async def close(self) -> None:
        self.batches.stop()
        for task in self.tasks:
            task.cancel()
        results = await asyncio.gather(*self.tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                logger.error("Execution runtime stopped with an error (%s)", type(result).__name__)
        await durable_thread(self.store.close_owner, self.owner, time())
