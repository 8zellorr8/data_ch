"""Cancellation-aware bridge for finite blocking database operations."""

import asyncio
import logging
from collections.abc import Callable
from typing import ParamSpec, TypeVar

P = ParamSpec("P")
T = TypeVar("T")
logger = logging.getLogger(__name__)


async def durable_thread(function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Join an in-flight SQLite write before propagating task cancellation."""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await finish_task(task)
        except Exception:
            logger.error("Storage operation failed during cancellation")
        raise


async def finish_task(task: asyncio.Task[T]) -> T:
    """Wait through repeated cancellation requests; used only during cleanup."""
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()
