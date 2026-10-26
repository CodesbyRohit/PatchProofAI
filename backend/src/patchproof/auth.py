"""Who is making a request: a GitHub-signed-in user, an access-token holder, or the open local principal.

The principal's `owner` key is stored on every project and run; reads, lists and writes are filtered by it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from starlette.datastructures import Headers
from starlette.requests import cookie_parser

from .access import bearer_token, hash_token, token_allowed
from .accounts import SESSION_COOKIE, AccountService
from .config import Settings


@dataclass(frozen=True)
class Principal:
    kind: Literal["user", "token", "local"]
    owner: str
    user_id: int | None = None
    login: str | None = None
    csrf: str | None = None


LOCAL = Principal(kind="local", owner="local")


def access_control_enabled(settings: Settings) -> bool:
    """Open (single-user, local) only when neither access tokens nor GitHub sign-in are configured."""
    return bool(settings.access_token_hashes) or settings.github_configured


def resolve_principal(scope, settings: Settings, accounts: AccountService | None) -> Principal | None:
    headers = Headers(scope=scope)
    if accounts is not None:
        session = cookie_parser(headers.get("cookie", "")).get(SESSION_COOKIE)
        user = accounts.session_user(session)
        if user is not None:
            return Principal(kind="user", owner=f"user:{user.user_id}", user_id=user.user_id, login=user.login,
                             csrf=user.csrf)
    token = bearer_token(headers.get("authorization"))
    if token and token_allowed(token, settings.access_token_hashes):
        return Principal(kind="token", owner=f"token:{hash_token(token)[:16]}")
    if not access_control_enabled(settings):
        return LOCAL
    return None
