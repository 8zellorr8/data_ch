"""Authenticated, stateless inference API for a single local GPU model."""

import asyncio
import hmac
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, HTTPException, Security
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import ValidationError
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from backend.errors import DomainError
from backend.evaluator import EvaluationContext, Evaluator
from backend.models import AIModelMetadata
from backend.scoring import validate_result

from .config import build_local_evaluator
from .worker_protocol import WorkerInput, WorkerOutput


class _PrivateInferenceMiddleware:
    def __init__(self, app: ASGIApp, *, api_key: str, maximum_bytes: int) -> None:
        self.app, self.api_key, self.maximum_bytes = app, api_key, maximum_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") != "/v1/evaluate":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        scheme, _, supplied = headers.get(b"authorization", b"").partition(b" ")
        if (
            not self.api_key
            or scheme.lower() != b"bearer"
            or not hmac.compare_digest(supplied, self.api_key.encode())
        ):
            await JSONResponse({"error": {"code": "UNAUTHORIZED"}}, status_code=401)(
                scope, receive, send
            )
            return
        count = 0

        async def limited_receive() -> Message:
            nonlocal count
            message = await receive()
            count += len(message.get("body", b""))
            if count > self.maximum_bytes:
                raise HTTPException(413, "Inference payload is too large")
            return message

        await self.app(scope, limited_receive, send)


def create_worker_app(evaluator: Evaluator | None = None, *, api_key: str | None = None) -> FastAPI:
    model = evaluator if evaluator is not None else build_local_evaluator()
    if not getattr(model, "local_only", False):
        raise ValueError("The GPU worker accepts local inference models only")
    key = os.getenv("CQA_LOCAL_AI_KEY", "") if api_key is None else api_key
    gate = asyncio.Semaphore(1)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        if not key:
            raise RuntimeError("Set CQA_LOCAL_AI_KEY before starting the worker")
        if not model.configured:
            raise RuntimeError("The local model artifact is missing")
        warmup = getattr(model, "warmup", None)
        if callable(warmup):
            await asyncio.to_thread(warmup)
        keep_awake = os.name == "nt" and os.getenv("CQA_KEEP_AWAKE", "0") == "1"
        if keep_awake:
            import ctypes

            # A process-scoped request; no persistent power-plan changes.
            windows_library = getattr(ctypes, "windll")
            windows_library.kernel32.SetThreadExecutionState(0x80000001)
        try:
            yield
        finally:
            if keep_awake:
                windows_library.kernel32.SetThreadExecutionState(0x80000000)

    application = FastAPI(title="Replimetr local GPU worker", lifespan=lifespan)
    application.state.evaluator = model
    application.add_middleware(_PrivateInferenceMiddleware, api_key=key, maximum_bytes=1_000_000)

    @application.get("/health")
    async def health() -> dict[str, object]:
        return {
            "status": "ok",
            "ai_configured": model.configured and bool(key),
            "usage_is_local": True,
            "model": str(getattr(model, "model", "local-model")),
            "paid_fallback": False,
        }

    security = HTTPBearer()

    @application.post("/v1/evaluate", response_model=WorkerOutput)
    async def evaluate(
        payload: WorkerInput,
        credentials: Annotated[HTTPAuthorizationCredentials, Security(security)],
    ) -> WorkerOutput:
        if gate.locked():
            raise HTTPException(429, "The GPU is busy; retry later")
        async with gate:
            context = EvaluationContext(
                payload.conversation.model_copy(deep=True), payload.checklist.model_copy(deep=True)
            )
            started = time.perf_counter()
            try:
                output = await model.evaluate(context)
                validate_result(payload.checklist, payload.conversation, output.result)
                metadata = AIModelMetadata(
                    model=output.model,
                    prompt_version=output.prompt_version,
                    input_tokens=output.input_tokens,
                    output_tokens=output.output_tokens,
                    calls=output.calls,
                    audio_analyzed=output.audio_analyzed,
                    duration_ms=int((time.perf_counter() - started) * 1000),
                )
            except DomainError as error:
                status = 504 if error.code == "AI_TIMEOUT" else 502
                raise HTTPException(
                    status, {"code": error.code, "message": error.message}
                ) from None
            except ValidationError:
                raise HTTPException(502, "Invalid model metadata") from None
            return WorkerOutput(
                result=output.result,
                usage_is_local=True,
                metadata=metadata,
            )

    return application


app = create_worker_app()
