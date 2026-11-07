"""HTTP-only error presentation and bounded ASGI request buffering."""

import logging
from math import ceil
from time import time

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .errors import DomainError, ErrorCode
from .evaluator import ProviderDeferred
from .models import Contract
from .observability import error_code_context

logger = logging.getLogger(__name__)

# Preserve the public error codes/statuses while keeping HTTP out of business rules.
ERROR_HTTP_STATUS: dict[ErrorCode, int] = {
    "NOT_FOUND": 404,
    "STORAGE_ERROR": 500,
    "STORAGE_BUSY": 503,
    "VERSION_CONFLICT": 409,
    "REVISION_CONFLICT": 409,
    "APPEAL_EXISTS": 409,
    "APPEAL_CLOSED": 409,
    "UNKNOWN_CRITERION": 422,
    "INVALID_APPEAL": 422,
    "INVALID_RESULT": 422,
    "INVALID_ANSWER": 422,
    "INVALID_EVIDENCE": 422,
    "AI_NOT_CONFIGURED": 503,
    "AI_TIMEOUT": 504,
    "AI_UNAVAILABLE": 502,
    "AI_INVALID_RESULT": 502,
    "ORGANIZER_NOT_CONFIGURED": 503,
    "ORGANIZER_CONFIG_ERROR": 503,
    "ORGANIZER_INVALID_RESPONSE": 502,
    "ORGANIZER_UNAVAILABLE": 502,
    "DATASET_TOO_LARGE": 413,
    "AI_BUSY": 503,
    "IDEMPOTENCY_CONFLICT": 409,
    "EVALUATION_IN_PROGRESS": 409,
    "EVALUATION_INTERRUPTED": 409,
    "EVALUATION_FAILED": 409,
    "BATCH_QUEUE_FULL": 503,
    "INVALID_SUBMISSION": 422,
}


class ValidationDetail(Contract):
    location: list[str | int]
    message: str
    type: str


class ErrorDetail(Contract):
    code: str
    message: str
    details: list[ValidationDetail] | None = None


class ErrorResponse(Contract):
    error: ErrorDetail


def error_response(code: str, message: str, status: int) -> JSONResponse:
    error_code_context.set(code)
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


class BodyLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in {"POST", "PUT", "PATCH"}:
            await self.app(scope, receive, send)
            return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > self.max_bytes:
                response = error_response("BODY_TOO_LARGE", "Maximum request size is 8 MiB", 413)
                await response(scope, receive, send)
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break
        delivered = False

        async def replay() -> Message:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


class UnexpectedErrorMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def tracked_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, tracked_send)
        except Exception as exc:
            logger.error("Unhandled request failure (%s)", type(exc).__name__)
            if started:
                raise
            await error_response("INTERNAL_ERROR", "Unexpected server error", 500)(
                scope, receive, send
            )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError) -> JSONResponse:
        response = error_response(exc.code, exc.message, ERROR_HTTP_STATUS[exc.code])
        if isinstance(exc, ProviderDeferred):
            response.headers["Retry-After"] = str(max(1, ceil(exc.retry_at - time())))
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        error_code_context.set("VALIDATION_ERROR")
        details = [
            {"location": list(e["loc"]), "message": e["msg"], "type": e["type"]}
            for e in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "Invalid request",
                    "details": details,
                }
            },
        )

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        response = error_response("HTTP_ERROR", str(exc.detail), exc.status_code)
        if exc.headers:
            response.headers.update(exc.headers)
        return response
