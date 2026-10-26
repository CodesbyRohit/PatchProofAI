"""PatchProof accounts: GitHub sign-in, sessions, and encrypted GitHub tokens.

- Sign-in uses the GitHub App web flow with `state` (single use, stored hashed, bound to the browser by a
  short-lived cookie) and PKCE. The identity comes from GitHub's /user, never from the browser.
- A session is a random 256-bit cookie value; only its SHA-256 is stored. Each session has its own CSRF token,
  required on every state-changing request.
- GitHub tokens are encrypted with PATCHPROOF_SECRET_KEY (Fernet) and refreshed on the server. Refresh tokens
  are single use, so refreshes for one user are serialized.

    uv run python -m patchproof.accounts new-key   # prints a key for PATCHPROOF_SECRET_KEY
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sys
import threading
import time
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken

from .config import Settings
from .github import GitHubClient, GitHubError, TokenSet, pkce_pair
from .store import Store

SESSION_COOKIE = "pp_session"
STATE_COOKIE = "pp_oauth_state"
REFRESH_MARGIN_S = 300  # refresh a little before GitHub's 8-hour expiry


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SessionUser:
    user_id: int
    login: str
    csrf: str


class AccountService:
    def __init__(self, settings: Settings, store: Store, github: GitHubClient):
        self.settings = settings
        self.store = store
        self.github = github
        self._fernet = Fernet(settings.secret_key.encode()) if settings.secret_key else None
        self._refresh_locks: dict[int, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # -- sign-in -----------------------------------------------------------------------------------------

    def start_login(self) -> tuple[str, str]:
        """(GitHub authorize URL, state value for the browser-binding cookie)."""
        state = secrets.token_urlsafe(32)
        verifier, challenge = pkce_pair()
        self.store.save_oauth_state(_sha256(state), verifier)
        return self.github.authorize_url(self.settings.github_callback_url or "", state, challenge), state

    def complete_login(self, code: str, state: str, cookie_state: str | None) -> str:
        """Finish the callback and return a new session cookie value. Raises GitHubError on any failure."""
        # The state must be one this server issued (single use) AND the one this browser started with:
        # a callback link forged by someone else carries their state, not the one in this browser's cookie.
        if not cookie_state or not hmac.compare_digest(state, cookie_state):
            raise GitHubError("invalid", "Sign-in could not be verified (state mismatch). Please start again.")
        verifier = self.store.take_oauth_state(_sha256(state))
        if verifier is None:
            raise GitHubError("invalid", "This sign-in link has expired or was already used. Please start again.")
        tokens = self.github.exchange_code(code, verifier, self.settings.github_callback_url or "")
        profile = self.github.user(tokens.access_token)
        user_id, login = int(profile["id"]), str(profile["login"])
        allowed = self.settings.github_allowed_logins
        if allowed and login.lower() not in allowed:
            self.github.revoke_grant(tokens.access_token)
            raise GitHubError("forbidden", f"The GitHub account {login} is not allowed to use this PatchProof instance.")
        self.store.upsert_user(user_id, login)
        self._save_tokens(user_id, tokens)
        session = secrets.token_urlsafe(32)
        self.store.create_session(_sha256(session), user_id, secrets.token_urlsafe(32), self.settings.session_ttl_s)
        return session

    # -- sessions ----------------------------------------------------------------------------------------

    def session_user(self, cookie_value: str | None) -> SessionUser | None:
        if not cookie_value or len(cookie_value) > 200:
            return None
        found = self.store.get_session(_sha256(cookie_value))
        if not found:
            return None
        user = self.store.get_user(found[0])
        return SessionUser(user_id=found[0], login=user[1], csrf=found[1]) if user else None

    def logout(self, cookie_value: str) -> None:
        self.store.delete_session(_sha256(cookie_value))

    def disconnect(self, user_id: int) -> None:
        """Forget this user's GitHub tokens, revoke the app authorization, and end every session."""
        try:
            token = self.access_token(user_id)
            self.github.revoke_grant(token)
        except GitHubError:
            pass
        self.store.delete_github_tokens(user_id)
        self.store.delete_user_sessions(user_id)

    # -- GitHub tokens -------------------------------------------------------------------------------------

    def access_token(self, user_id: int) -> str:
        """A valid GitHub user token, refreshed if it is about to expire. Raises GitHubError('reauthorize')."""
        with self._refresh_lock(user_id):
            stored = self.store.get_github_tokens(user_id)
            if not stored:
                raise GitHubError("reauthorize", "Connect your GitHub account to continue.")
            access_enc, access_exp, refresh_enc, refresh_exp = stored
            if access_exp is None or access_exp - REFRESH_MARGIN_S > time.time():
                return self._decrypt(access_enc)
            if not refresh_enc or (refresh_exp and refresh_exp < time.time()):
                self.store.delete_github_tokens(user_id)
                raise GitHubError("reauthorize", "Your GitHub authorization has expired. Please sign in again.")
            try:
                tokens = self.github.refresh(self._decrypt(refresh_enc))
            except GitHubError as err:
                if err.kind == "reauthorize":  # the refresh token was revoked or already used
                    self.store.delete_github_tokens(user_id)
                raise
            self._save_tokens(user_id, tokens)
            return tokens.access_token

    def _refresh_lock(self, user_id: int) -> threading.Lock:
        # One lock per user, created under a guard: refresh tokens are single use, so two concurrent refreshes
        # for the same user would burn the token and sign the user out.
        with self._locks_guard:
            return self._refresh_locks.setdefault(user_id, threading.Lock())

    def _save_tokens(self, user_id: int, tokens: TokenSet) -> None:
        self.store.save_github_tokens(
            user_id, self._encrypt(tokens.access_token), tokens.access_expires_at,
            self._encrypt(tokens.refresh_token) if tokens.refresh_token else None, tokens.refresh_expires_at)

    def _encrypt(self, value: str) -> str:
        if not self._fernet:
            raise GitHubError("unavailable", "GitHub sign-in is not configured on this server.")
        return self._fernet.encrypt(value.encode()).decode()

    def _decrypt(self, value: str) -> str:
        if not self._fernet:
            raise GitHubError("unavailable", "GitHub sign-in is not configured on this server.")
        try:
            return self._fernet.decrypt(value.encode()).decode()
        except InvalidToken as exc:  # key rotated: the stored token is unusable
            raise GitHubError("reauthorize", "Please sign in to GitHub again.") from exc


def main(argv: list[str]) -> int:
    if len(argv) > 1 and argv[1] == "new-key":
        print(Fernet.generate_key().decode())
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
