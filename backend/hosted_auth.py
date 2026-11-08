"""Optional HTTP Basic authentication for the single-origin hosted app."""

import os
import re
from base64 import b64decode, b64encode
from binascii import Error
from hashlib import sha256
from hmac import compare_digest
from urllib.parse import urlsplit

from fastapi import FastAPI
from starlette.datastructures import MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_USER_ENV = "CQA_HOSTED_HTTP_USER"
_PASSWORD_ENV = "CQA_HOSTED_HTTP_PASSWORD"
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}


def _header_values(scope: Scope, header: bytes) -> list[bytes]:
    return [value for name, value in scope["headers"] if name.lower() == header]


def _allowed_origin(scope: Scope) -> bool:
    origins = _header_values(scope, b"origin")
    if not origins:
        return True
    hosts = _header_values(scope, b"host")
    if len(origins) != 1 or len(hosts) != 1:
        return False
    origin = origins[0]
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    # The reverse proxy may use HTTP upstream even for a public HTTPS origin.
    return (
        parsed.scheme in (b"http", b"https")
        and bool(parsed.netloc)
        and parsed.netloc == hosts[0]
        and b"@" not in parsed.netloc
        and origin == parsed.scheme + b"://" + parsed.netloc
    )


class HostedBasicAuth:
    def __init__(self, app: ASGIApp, credentials_digest: bytes) -> None:
        self.app = app
        self.credentials_digest = credentials_digest

    def authorized(self, scope: Scope) -> bool:
        headers = _header_values(scope, b"authorization")
        if len(headers) != 1 or len(headers[0]) > 512:
            return False
        scheme, _, token = headers[0].partition(b" ")
        token = token.lstrip(b" ")
        if scheme.lower() != b"basic" or not token:
            return False
        try:
            credentials = b64decode(token, validate=True)
        except Error:
            return False
        return b64encode(credentials) == token and compare_digest(
            sha256(credentials).digest(), self.credentials_digest
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if not self.authorized(scope):
            response = JSONResponse(
                {"error": {"code": "UNAUTHORIZED", "message": "Hosted access requires sign-in"}},
                status_code=401,
                headers={
                    "WWW-Authenticate": 'Basic realm="Replimetr", charset="UTF-8"',
                    "Cache-Control": "no-store",
                },
            )
            await response(scope, receive, send)
            return
        if scope["method"] not in _SAFE_METHODS and not _allowed_origin(scope):
            response = JSONResponse(
                {"error": {"code": "FORBIDDEN_ORIGIN", "message": "Hosted origin is not allowed"}},
                status_code=403,
                headers={"Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
            return

        async def uncached_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["Cache-Control"] = "no-store"
            await send(message)

        await self.app(scope, receive, uncached_send)


def install_hosted_auth(app: FastAPI) -> None:
    user, password = os.getenv(_USER_ENV), os.getenv(_PASSWORD_ENV)
    if user is None and password is None:
        return
    if not (
        user
        and re.fullmatch(r"[A-Za-z0-9._-]{1,64}", user)
        and password
        and re.fullmatch(r"[A-Za-z0-9_-]{16,128}", password)
    ):
        raise ValueError(f"Set both {_USER_ENV} and {_PASSWORD_ENV} to valid hosted credentials")
    credentials_digest = sha256(f"{user}:{password}".encode()).digest()
    app.add_middleware(HostedBasicAuth, credentials_digest=credentials_digest)
