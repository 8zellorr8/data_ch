"""Request-scoped diagnostics without raw URLs, credentials or transcripts."""

import logging
import re
from contextvars import ContextVar
from time import perf_counter
from uuid import uuid4

from starlette.types import ASGIApp, Message, Receive, Scope, Send

request_id_context: ContextVar[str | None] = ContextVar("request_id", default=None)
execution_id_context: ContextVar[str | None] = ContextVar("execution_id", default=None)
error_code_context: ContextVar[str | None] = ContextVar("error_code", default=None)
_SAFE_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
request_logger = logging.getLogger("backend.requests")

_URL = re.compile(r"https?://[^\s\"']+")


class UrlQueryRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = _URL.sub("[REDACTED_URL]", message)
        if redacted != message:
            record.msg, record.args = redacted, ()
        return True


def configure_logging() -> None:
    logger = logging.getLogger("httpx")
    if not any(isinstance(item, UrlQueryRedactionFilter) for item in logger.filters):
        logger.addFilter(UrlQueryRedactionFilter())
    # Uvicorn's default access log contains the raw path and query. Our template
    # logger replaces it, including when the app is launched with --access-log.
    logging.getLogger("uvicorn.access").disabled = True
    request_logger.setLevel(logging.INFO)
    if not request_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        request_logger.addHandler(handler)


class RequestDiagnosticsMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        supplied = [v.decode("latin1") for k, v in scope["headers"] if k.lower() == b"x-request-id"]
        request_id = (
            supplied[0] if len(supplied) == 1 and _SAFE_ID.fullmatch(supplied[0]) else str(uuid4())
        )
        rid_token = request_id_context.set(request_id)
        eid_token = execution_id_context.set(None)
        error_token = error_code_context.set(None)
        started, status = perf_counter(), 499

        async def traced_send(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if k.lower() not in {b"x-request-id", b"x-execution-id"}
                ]
                headers.append((b"x-request-id", request_id.encode("ascii")))
                execution_id = execution_id_context.get()
                if execution_id:
                    headers.append((b"x-execution-id", execution_id.encode("ascii")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, traced_send)
        finally:
            # Only the route template is logged; raw paths may contain personal data.
            route = getattr(scope.get("route"), "path", "<unmatched>")
            method = (
                scope["method"]
                if scope["method"] in {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"}
                else "OTHER"
            )
            code = error_code_context.get() or (f"HTTP_{status}" if status >= 400 else "OK")
            request_logger.info(
                "request id=%s method=%s route=%s status=%s duration_ms=%s code=%s",
                request_id,
                method,
                route,
                status,
                round((perf_counter() - started) * 1000),
                code,
            )
            request_id_context.reset(rid_token)
            execution_id_context.reset(eid_token)
            error_code_context.reset(error_token)
