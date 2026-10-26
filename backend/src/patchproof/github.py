"""GitHub App user authorization and the REST calls PatchProof needs.

Only api.github.com, github.com (OAuth) and codeload.github.com (archives) are ever contacted; repository
identifiers are validated before they are put in a URL. Tokens are passed per call and never logged.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

import httpx

API = "https://api.github.com"
OAUTH = "https://github.com/login/oauth"
CODELOAD_HOST = "codeload.github.com"
API_VERSION = "2022-11-28"
USER_AGENT = "PatchProof-AI"

OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
# Branch names reach GitHub only URL-encoded; this also rejects names git itself would refuse.
REF_RE = re.compile(r"^(?!/|.*(?:\.\.|//|@\{|\\|\.lock$|/$|\.$))[^\x00-\x20~^:?*\[\x7f]{1,255}$")


class GitHubError(Exception):
    """A failed GitHub call. `kind` drives the HTTP status and the message shown to the user."""

    STATUS = {"unauthorized": 401, "reauthorize": 401, "forbidden": 403, "not_found": 404, "empty": 409,
              "rate_limited": 429, "too_large": 413, "invalid": 422, "unavailable": 502, "timeout": 504,
              "invalid_response": 502}

    def __init__(self, kind: str, message: str, *, retry_after: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.retry_after = retry_after

    @property
    def status(self) -> int:
        return self.STATUS.get(self.kind, 502)


def split_repository(full_name: str) -> tuple[str, str]:
    owner, _, name = full_name.partition("/")
    if not OWNER_RE.match(owner) or not NAME_RE.match(name) or name in {".", ".."}:
        raise GitHubError("invalid", f"Not a GitHub repository name: {full_name[:120]!r}")
    return owner, name


def pkce_pair() -> tuple[str, str]:
    """(verifier, S256 challenge)."""
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


@dataclass(frozen=True)
class TokenSet:
    access_token: str
    access_expires_at: float | None
    refresh_token: str | None
    refresh_expires_at: float | None


class GitHubClient:
    def __init__(self, client_id: str | None = None, client_secret: str | None = None, *,
                 transport: httpx.BaseTransport | None = None, timeout_s: float = 20.0):
        self.client_id = client_id
        self.client_secret = client_secret
        self._http = httpx.Client(transport=transport, timeout=timeout_s, follow_redirects=False,
                                  headers={"User-Agent": USER_AGENT})

    # -- OAuth (GitHub App user authorization) ----------------------------------------------------------

    def authorize_url(self, callback_url: str, state: str, code_challenge: str) -> str:
        query = urlencode({"client_id": self.client_id, "redirect_uri": callback_url, "state": state,
                           "code_challenge": code_challenge, "code_challenge_method": "S256"})
        return f"{OAUTH}/authorize?{query}"

    def exchange_code(self, code: str, verifier: str, callback_url: str) -> TokenSet:
        return self._token_request({"client_id": self.client_id, "client_secret": self.client_secret,
                                    "code": code, "redirect_uri": callback_url, "code_verifier": verifier})

    def refresh(self, refresh_token: str) -> TokenSet:
        return self._token_request({"client_id": self.client_id, "client_secret": self.client_secret,
                                    "grant_type": "refresh_token", "refresh_token": refresh_token})

    def _token_request(self, params: dict[str, Any]) -> TokenSet:
        try:
            res = self._http.post(f"{OAUTH}/access_token", data=params, headers={"Accept": "application/json"})
        except httpx.TimeoutException as exc:
            raise GitHubError("timeout", "GitHub did not answer the sign-in request in time.") from exc
        except httpx.HTTPError as exc:
            raise GitHubError("unavailable", "Could not reach GitHub to complete sign-in.") from exc
        body = _json(res)
        if res.status_code != 200 or "error" in body or not body.get("access_token"):
            # e.g. bad_verification_code, bad_refresh_token, incorrect_client_credentials
            error = str(body.get("error", res.status_code))
            raise GitHubError("reauthorize", f"GitHub rejected the authorization ({error}). Please sign in again.")
        now = time.time()
        return TokenSet(
            access_token=body["access_token"],
            access_expires_at=now + int(body["expires_in"]) if body.get("expires_in") else None,
            refresh_token=body.get("refresh_token"),
            refresh_expires_at=now + int(body["refresh_token_expires_in"]) if body.get("refresh_token_expires_in") else None,
        )

    def revoke_grant(self, access_token: str) -> None:
        """Best effort: remove the user's authorization of the app (DELETE /applications/{client_id}/grant)."""
        try:
            self._http.request("DELETE", f"{API}/applications/{self.client_id}/grant",
                               auth=(self.client_id or "", self.client_secret or ""),
                               json={"access_token": access_token}, headers=_api_headers(None))
        except httpx.HTTPError:
            pass

    # -- REST --------------------------------------------------------------------------------------------

    def _get(self, path: str, token: str | None, params: dict[str, Any] | None = None) -> Any:
        return self._call("GET", path, token, params=params)

    def _call(self, method: str, path: str, token: str | None, *, params: dict[str, Any] | None = None,
              json: Any = None) -> Any:
        try:
            res = self._http.request(method, API + path, params=params, json=json, headers=_api_headers(token))
        except httpx.TimeoutException as exc:
            raise GitHubError("timeout", "GitHub did not answer in time.") from exc
        except httpx.HTTPError as exc:
            raise GitHubError("unavailable", "Could not reach GitHub.") from exc
        _raise_for(res)
        return _json(res) if res.content else None

    def user(self, token: str) -> dict[str, Any]:
        return self._get("/user", token)

    def installations(self, token: str, max_pages: int = 10) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            body = self._get("/user/installations", token, {"per_page": 100, "page": page})
            batch = body.get("installations", [])
            out += batch
            if len(batch) < 100:
                break
        return out

    def installation_repositories(self, token: str, installation_id: int, page: int) -> tuple[list[dict[str, Any]], int]:
        body = self._get(f"/user/installations/{int(installation_id)}/repositories", token,
                         {"per_page": 100, "page": int(page)})
        return body.get("repositories", []), int(body.get("total_count", 0))

    def repository(self, full_name: str, token: str | None) -> dict[str, Any]:
        owner, name = split_repository(full_name)
        return self._get(f"/repos/{owner}/{name}", token)

    def branches(self, full_name: str, token: str | None, page: int) -> list[dict[str, Any]]:
        owner, name = split_repository(full_name)
        return self._get(f"/repos/{owner}/{name}/branches", token, {"per_page": 100, "page": int(page)})

    def commit(self, full_name: str, ref: str, token: str | None) -> dict[str, Any]:
        """Resolve a branch name or SHA to its commit."""
        owner, name = split_repository(full_name)
        if not (SHA_RE.match(ref) or REF_RE.match(ref)):
            raise GitHubError("invalid", "Not a valid branch name or commit SHA.")
        return self._get(f"/repos/{owner}/{name}/commits/{quote(ref, safe='')}", token)

    def download_archive(self, full_name: str, sha: str, token: str | None, max_bytes: int) -> bytes:
        """The repository at `sha` as a ZIP. Follows exactly one redirect, and only to codeload.github.com."""
        owner, name = split_repository(full_name)
        if not SHA_RE.match(sha):
            raise GitHubError("invalid", "Archives are only fetched for an exact commit SHA.")
        if token:
            try:
                res = self._http.get(f"{API}/repos/{owner}/{name}/zipball/{sha}", headers=_api_headers(token))
            except httpx.HTTPError as exc:
                raise GitHubError("unavailable", "Could not reach GitHub.") from exc
            if res.status_code != 302:
                _raise_for(res)
                raise GitHubError("invalid_response", "GitHub did not return an archive location.")
            url = res.headers.get("location", "")
        else:
            url = f"https://{CODELOAD_HOST}/{owner}/{name}/zip/{sha}"  # public repositories, no credentials
        target = urlsplit(url)
        if target.scheme != "https" or target.hostname != CODELOAD_HOST:
            raise GitHubError("invalid_response", "GitHub redirected the archive download to an unexpected host.")
        try:
            # The redirect URL carries its own short-lived token; the user token is not sent to codeload.
            with self._http.stream("GET", url) as res:
                if res.status_code != 200:
                    _raise_for(res)
                    raise GitHubError("invalid_response", f"Archive download failed (HTTP {res.status_code}).")
                declared = int(res.headers.get("content-length") or 0)
                if declared > max_bytes:
                    raise GitHubError("too_large", _too_large(max_bytes))
                chunks, size = [], 0
                for chunk in res.iter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise GitHubError("too_large", _too_large(max_bytes))
                    chunks.append(chunk)
        except httpx.TimeoutException as exc:
            raise GitHubError("timeout", "Downloading the repository took too long.") from exc
        except httpx.HTTPError as exc:
            raise GitHubError("unavailable", "Could not download the repository archive.") from exc
        return b"".join(chunks)

    # -- writes (pull requests) -------------------------------------------------------------------------

    def get_ref(self, full_name: str, branch: str, token: str) -> dict[str, Any] | None:
        owner, name = split_repository(full_name)
        try:
            return self._get(f"/repos/{owner}/{name}/git/ref/heads/{quote(branch, safe='/')}", token)
        except GitHubError as err:
            if err.kind == "not_found":
                return None
            raise

    def create_blob(self, full_name: str, content: bytes, token: str) -> str:
        owner, name = split_repository(full_name)
        body = self._call("POST", f"/repos/{owner}/{name}/git/blobs", token,
                          json={"content": base64.b64encode(content).decode(), "encoding": "base64"})
        return body["sha"]

    def create_tree(self, full_name: str, base_tree: str, entries: list[dict[str, Any]], token: str) -> str:
        owner, name = split_repository(full_name)
        return self._call("POST", f"/repos/{owner}/{name}/git/trees", token,
                          json={"base_tree": base_tree, "tree": entries})["sha"]

    def create_commit(self, full_name: str, message: str, tree: str, parent: str, token: str) -> str:
        owner, name = split_repository(full_name)
        return self._call("POST", f"/repos/{owner}/{name}/git/commits", token,
                          json={"message": message, "tree": tree, "parents": [parent]})["sha"]

    def create_ref(self, full_name: str, branch: str, sha: str, token: str) -> None:
        owner, name = split_repository(full_name)
        self._call("POST", f"/repos/{owner}/{name}/git/refs", token, json={"ref": f"refs/heads/{branch}", "sha": sha})

    def find_pull(self, full_name: str, branch: str, token: str) -> dict[str, Any] | None:
        owner, name = split_repository(full_name)
        pulls = self._get(f"/repos/{owner}/{name}/pulls", token, {"head": f"{owner}:{branch}", "state": "all"})
        return pulls[0] if pulls else None

    def create_pull(self, full_name: str, title: str, body: str, head: str, base: str, token: str) -> dict[str, Any]:
        owner, name = split_repository(full_name)
        return self._call("POST", f"/repos/{owner}/{name}/pulls", token,
                          json={"title": title, "body": body, "head": head, "base": base, "draft": True})


def _api_headers(token: str | None) -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": API_VERSION}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _json(res: httpx.Response) -> Any:
    try:
        return res.json()
    except ValueError as exc:
        raise GitHubError("invalid_response", "GitHub sent a response PatchProof could not read.") from exc


def _too_large(max_bytes: int) -> str:
    return f"The repository archive is larger than {max_bytes // (1024 * 1024)} MB, PatchProof's limit."


def _raise_for(res: httpx.Response) -> None:
    status = res.status_code
    if status < 400:
        return
    remaining = res.headers.get("x-ratelimit-remaining")
    if status == 429 or (status == 403 and remaining == "0"):
        reset = res.headers.get("x-ratelimit-reset")
        wait = max(0, int(reset) - int(time.time())) if reset and reset.isdigit() else None
        raise GitHubError("rate_limited", "GitHub's API rate limit was reached. Try again later.", retry_after=wait)
    if status == 401:
        raise GitHubError("reauthorize", "Your GitHub authorization has expired or was revoked. Please sign in again.")
    if status == 403:
        raise GitHubError("forbidden", "GitHub denied access. The PatchProof app may not be installed on this "
                                       "repository, or your account lacks permission.")
    if status == 404:
        raise GitHubError("not_found", "Repository, branch or commit not found, or not accessible to you.")
    if status == 409:
        raise GitHubError("empty", "The repository is empty.")
    if status == 422:
        raise GitHubError("invalid", "GitHub rejected the request.")
    raise GitHubError("unavailable", f"GitHub returned HTTP {status}.")
