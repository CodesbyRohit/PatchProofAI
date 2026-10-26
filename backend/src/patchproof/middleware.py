"""ASGI middleware that runs before any request parsing: body-size limit and access tokens."""

from __future__ import annotations

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .access import bearer_token, token_allowed

# Probes for load balancers and uptime checks; they reveal no data and need no token.
PUBLIC_API_PATHS = frozenset({"/api/health", "/api/ready"})


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


class RequireToken:
    """Require `Authorization: Bearer <token>` on every API path except the public probes."""

    def __init__(self, app: ASGIApp, token_hashes: tuple[str, ...]):
        self.app = app
        self.token_hashes = token_hashes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (scope["type"] == "http" and self.token_hashes and is_api_path(scope["path"])
                and scope["path"] not in PUBLIC_API_PATHS and scope["method"] != "OPTIONS"):
            token = bearer_token(Headers(scope=scope).get("authorization"))
            if not token_allowed(token, self.token_hashes):
                detail = ("The access token is not valid." if token
                          else "This PatchProof instance requires an access token.")
                response = JSONResponse({"detail": detail}, status_code=401,
                                        headers={"WWW-Authenticate": 'Bearer realm="PatchProof"'})
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
