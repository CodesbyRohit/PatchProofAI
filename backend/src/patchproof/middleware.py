"""ASGI middleware that runs before any request parsing: body-size limit and authentication."""

from __future__ import annotations

import hmac
from collections.abc import Callable

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .auth import Principal

# Probes for load balancers, and the endpoints that tell a browser how to sign in. They reveal no data.
PUBLIC_API_PATHS = frozenset({"/api/health", "/api/ready", "/api/auth/me", "/api/auth/github/start",
                              "/api/auth/github/callback", "/api/events"})
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def is_api_path(path: str) -> bool:
    return path == "/api" or path.startswith("/api/")


class _BodyTooLarge(Exception):
    pass


class BodyLimit:
    """Reject request bodies over `max_bytes` with 413, whether or not Content-Length is sent.

    Without this, the framework would accept an unbounded multipart body (spooled to disk) before the
    upload handler ever sees its own 4 MB limit.
    """

    def __init__(self, app: ASGIApp, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = Headers(scope=scope).get("content-length")
        if declared is not None:
            try:
                too_large = int(declared) > self.max_bytes
            except ValueError:
                too_large = True
            if too_large:
                await self._reject(scope, receive, send)
                return

        received = 0
        exceeded = False
        started = False

        async def limited_receive() -> Message:
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    exceeded = True
                    raise _BodyTooLarge
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal started
            if exceeded:
                # FastAPI turns errors raised while parsing a body into its own 400; answer 413 instead.
                if not started:
                    started = True
                    await self._reject(scope, receive, send)
                return
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except _BodyTooLarge:
            if not started:
                await self._reject(scope, receive, send)

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        limit_mb = self.max_bytes / (1024 * 1024)
        response = JSONResponse({"detail": f"The request is too large (limit {limit_mb:g} MB)."}, status_code=413,
                                headers={"Connection": "close"})
        await response(scope, receive, send)


class Authenticate:
    """Attach the request's principal and refuse protected API paths without one.

    Public paths: the health/readiness probes and the sign-in endpoints (which say how to authenticate).
    Requests authenticated by the session cookie must carry the session's CSRF token when they change state;
    bearer-token requests cannot be forged cross-site, so they need none.
    """

    def __init__(self, app: ASGIApp, resolve: Callable[[Scope], Principal | None], sign_in_hint: str):
        self.app = app
        self.resolve = resolve
        self.sign_in_hint = sign_in_hint

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not is_api_path(scope["path"]):
            await self.app(scope, receive, send)
            return
        principal = self.resolve(scope)
        scope.setdefault("state", {})["principal"] = principal
        public = scope["path"] in PUBLIC_API_PATHS or scope["method"] == "OPTIONS"
        if principal is None and not public:
            headers = Headers(scope=scope)
            detail = "The access token is not valid." if headers.get("authorization") else self.sign_in_hint
            await JSONResponse({"detail": detail}, status_code=401,
                               headers={"WWW-Authenticate": 'Bearer realm="PatchProof"'})(scope, receive, send)
            return
        if (principal is not None and principal.kind == "user" and scope["method"] not in SAFE_METHODS
                and not hmac.compare_digest(Headers(scope=scope).get("x-csrf-token", ""), principal.csrf or "")):
            await JSONResponse({"detail": "Missing or invalid CSRF token. Reload the page and try again."},
                               status_code=403)(scope, receive, send)
            return
        await self.app(scope, receive, send)
