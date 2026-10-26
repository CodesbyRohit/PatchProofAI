"""Bearer-token access control for hosted deployments.

Each person allowed to use an instance gets a random token. The server stores only SHA-256 hashes of
the tokens (PATCHPROOF_ACCESS_TOKEN_HASHES), so its environment never holds a usable credential, and
removing a hash revokes that token. The browser asks for the token and keeps it for the tab session;
it is never part of the frontend build.

    uv run python -m patchproof.access new      # prints a new token and the hash to configure
    uv run python -m patchproof.access hash     # reads a token on stdin and prints its hash
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sys
from collections.abc import Iterable

TOKEN_PREFIX = "ppa_"  # noqa: S105 - a public prefix that makes tokens recognisable, not a secret


def new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def bearer_token(authorization: str | None) -> str | None:
    """The token from an `Authorization: Bearer <token>` header, or None."""
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    token = token.strip()
    return token if scheme.lower() == "bearer" and token else None


def token_allowed(token: str | None, hashes: Iterable[str]) -> bool:
    if not token or len(token) > 512:
        return False
    digest = hash_token(token)
    allowed = False
    for expected in hashes:  # no early exit: check every configured hash
        allowed |= hmac.compare_digest(digest, expected)
    return allowed


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else ""
    if command == "new":
        token = new_token()
        print(f"token (give to the user; shown once): {token}")
        print(f"hash  (add to PATCHPROOF_ACCESS_TOKEN_HASHES): {hash_token(token)}")
        return 0
    if command == "hash":
        token = sys.stdin.readline().strip()
        if not token:
            print("No token on stdin.", file=sys.stderr)
            return 1
        print(hash_token(token))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
