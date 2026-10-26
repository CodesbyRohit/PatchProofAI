"""A small in-memory GitHub for tests: OAuth, REST reads, archives and the Git Data API, via httpx.MockTransport.

It models what PatchProof relies on: per-token identity and repository access, anonymous access to public
repositories only, single-use refresh tokens, the zipball redirect to codeload, and branch/commit/PR writes.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import time
import zipfile
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

import httpx


def make_zip(prefix: str, files: dict[str, bytes], symlinks: dict[str, str] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for path, data in files.items():
            zf.writestr(f"{prefix}/{path}", data)
        for path, target in (symlinks or {}).items():
            info = zipfile.ZipInfo(f"{prefix}/{path}")
            info.external_attr = (0o120777 << 16)
            zf.writestr(info, target)
    return buf.getvalue()


@dataclass
class Repo:
    id: int
    full_name: str
    private: bool
    files: dict[str, bytes]
    default_branch: str = "main"
    symlinks: dict[str, str] = field(default_factory=dict)
    push: dict[int, bool] = field(default_factory=dict)  # user id -> may push
    sha: str = "a" * 40
    tree: str = "t" * 40


@dataclass
class FakeGitHub:
    users: dict[int, str] = field(default_factory=lambda: {1: "alice", 2: "bob"})
    codes: dict[str, int] = field(default_factory=lambda: {"code-alice": 1, "code-bob": 2})
    repos: dict[str, Repo] = field(default_factory=dict)
    installed: dict[int, list[str]] = field(default_factory=dict)  # user id -> repos visible via the app
    access: dict[str, int] = field(default_factory=dict)  # access token -> user id
    refresh_tokens: dict[str, int] = field(default_factory=dict)
    expires_in: int = 28800
    rate_limited: bool = False
    refresh_delay_s: float = 0.0  # widens the window in which concurrent refreshes would collide
    archive_location: str | None = None  # override the zipball redirect target
    calls: list[str] = field(default_factory=list)
    refs: dict[str, str] = field(default_factory=dict)  # "owner/name:branch" -> commit sha
    commits: dict[str, dict] = field(default_factory=dict)
    blobs: dict[str, bytes] = field(default_factory=dict)
    trees: dict[str, dict] = field(default_factory=dict)
    pulls: dict[str, dict] = field(default_factory=dict)
    revoked: list[str] = field(default_factory=list)
    _counter: int = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def _new_token(self, user_id: int) -> tuple[str, str]:
        self._counter += 1
        access, refresh = f"ghu_test{self._counter}", f"ghr_test{self._counter}"
        self.access[access] = user_id
        self.refresh_tokens[refresh] = user_id
        return access, refresh

    def _user(self, request: httpx.Request) -> int | None:
        auth = request.headers.get("authorization", "")
        return self.access.get(auth.removeprefix("Bearer ")) if auth.startswith("Bearer ") else None

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = urlsplit(str(request.url))
        self.calls.append(f"{request.method} {url.netloc}{url.path}")
        if url.netloc == "github.com" and url.path == "/login/oauth/access_token":
            return self._oauth(request)
        if url.netloc == "codeload.github.com":
            return self._codeload(url.path)
        if url.netloc != "api.github.com":
            return httpx.Response(404)
        if self.rate_limited:
            return httpx.Response(403, json={"message": "API rate limit exceeded"},
                                  headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "9999999999"})
        user = self._user(request)
        path, method = url.path, request.method
        if method == "DELETE" and path.startswith("/applications/"):
            self.revoked.append(json.loads(request.content)["access_token"])
            return httpx.Response(204)
        if path == "/user":
            return httpx.Response(200, json={"id": user, "login": self.users[user]}) if user else httpx.Response(401)
        if path == "/user/installations":
            if not user:
                return httpx.Response(401)
            return httpx.Response(200, json={"installations": [
                {"id": 100 + user, "account": {"login": self.users[user]}, "repository_selection": "selected"}]})
        m = re.fullmatch(r"/user/installations/(\d+)/repositories", path)
        if m:
            if not user or int(m.group(1)) != 100 + user:
                return httpx.Response(404 if user else 401)
            names = self.installed.get(user, [])
            page = int(parse_qs(url.query).get("page", ["1"])[0])
            per = int(parse_qs(url.query).get("per_page", ["30"])[0])
            chunk = names[(page - 1) * per: page * per]
            return httpx.Response(200, json={"total_count": len(names),
                                             "repositories": [self._repo_json(self.repos[n], user) for n in chunk]})
        m = re.fullmatch(r"/repos/([^/]+)/([^/]+)(/.*)?", path)
        if not m:
            return httpx.Response(404)
        full, rest = f"{m.group(1)}/{m.group(2)}", m.group(3) or ""
        repo = self.repos.get(full)
        installed = user is not None and full in self.installed.get(user, [])
        # Private: only through the app installation. Public: anonymously, and through the user token only where
        # the app is installed (GitHub does not document more), so PatchProof's anonymous fallback is exercised.
        visible = repo is not None and (installed or (not repo.private and user is None))
        if not visible:
            return httpx.Response(404, json={"message": "Not Found"})
        return self._repo_route(repo, rest, method, request, user)

    def _repo_json(self, repo: Repo, user: int | None) -> dict:
        return {"id": repo.id, "full_name": repo.full_name, "private": repo.private, "default_branch": repo.default_branch,
                "description": "test repo", "archived": False, "html_url": f"https://github.com/{repo.full_name}",
                "pushed_at": "2026-10-01T00:00:00Z", "permissions": {"push": repo.push.get(user or 0, False), "pull": True}}

    def _repo_route(self, repo: Repo, rest: str, method: str, request: httpx.Request, user: int | None) -> httpx.Response:
        if rest == "":
            return httpx.Response(200, json=self._repo_json(repo, user))
        if rest == "/branches":
            return httpx.Response(200, json=[{"name": repo.default_branch, "commit": {"sha": repo.sha}, "protected": False}])
        m = re.fullmatch(r"/commits/(.+)", rest)
        if m:
            ref = m.group(1)
            if ref in (repo.default_branch, repo.sha):
                return httpx.Response(200, json={"sha": repo.sha, "html_url": "https://github.com/x", "parents": [],
                                                 "commit": {"message": "Initial\n\nbody", "tree": {"sha": repo.tree},
                                                            "author": {"name": "A", "date": "2026-10-01T00:00:00Z"}}})
            if ref in self.commits:
                c = self.commits[ref]
                return httpx.Response(200, json={"sha": ref, "parents": [{"sha": p} for p in c["parents"]],
                                                 "commit": {"message": c["message"], "tree": {"sha": c["tree"]}}})
            return httpx.Response(422 if not re.fullmatch(r"[0-9a-f]{40}", ref) else 404, json={"message": "No commit"})
        m = re.fullmatch(r"/zipball/([0-9a-f]{40})", rest)
        if m:
            if m.group(1) != repo.sha:
                return httpx.Response(404)
            location = self.archive_location or f"https://codeload.github.com/{repo.full_name}/legacy.zip/{repo.sha}?token=short"
            return httpx.Response(302, headers={"location": location})
        m = re.fullmatch(r"/git/ref/heads/(.+)", rest)
        if m:
            sha = self.refs.get(f"{repo.full_name}:{m.group(1)}")
            return httpx.Response(200, json={"object": {"sha": sha}}) if sha else httpx.Response(404)
        if method == "POST" and not repo.push.get(user or 0):
            return httpx.Response(403, json={"message": "Resource not accessible"})
        body = json.loads(request.content) if request.content else {}
        if rest == "/git/blobs":
            data = base64.b64decode(body["content"])
            sha = hashlib.sha1(data).hexdigest()  # noqa: S324 - mirrors git's blob ids in the fake
            self.blobs[sha] = data
            return httpx.Response(201, json={"sha": sha})
        if rest == "/git/trees":
            sha = hashlib.sha1(json.dumps(body, sort_keys=True).encode()).hexdigest()  # noqa: S324
            self.trees[sha] = body
            return httpx.Response(201, json={"sha": sha})
        if rest == "/git/commits":
            sha = hashlib.sha1(json.dumps(body, sort_keys=True).encode()).hexdigest()  # noqa: S324
            self.commits[sha] = body
            return httpx.Response(201, json={"sha": sha})
        if rest == "/git/refs":
            key = f"{repo.full_name}:{body['ref'].removeprefix('refs/heads/')}"
            if key in self.refs:
                return httpx.Response(422, json={"message": "Reference already exists"})
            self.refs[key] = body["sha"]
            return httpx.Response(201, json={"ref": body["ref"]})
        if rest == "/pulls" and method == "GET":
            head = parse_qs(urlsplit(str(request.url)).query)["head"][0].split(":", 1)[1]
            found = [p for p in self.pulls.values() if p["head"] == head]
            return httpx.Response(200, json=found)
        if rest == "/pulls" and method == "POST":
            number = len(self.pulls) + 1
            pull = {"number": number, "html_url": f"https://github.com/{repo.full_name}/pull/{number}", **body}
            self.pulls[str(number)] = pull
            return httpx.Response(201, json=pull)
        return httpx.Response(404)

    def _oauth(self, request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode())
        if form.get("grant_type") == ["refresh_token"]:
            time.sleep(self.refresh_delay_s)
            user = self.refresh_tokens.pop(form["refresh_token"][0], None)  # single use
            if user is None:
                return httpx.Response(200, json={"error": "bad_refresh_token"})
        else:
            user = self.codes.get(form.get("code", [""])[0])
            if user is None or not form.get("code_verifier"):
                return httpx.Response(200, json={"error": "bad_verification_code"})
        access, refresh = self._new_token(user)
        return httpx.Response(200, json={"access_token": access, "expires_in": self.expires_in, "refresh_token": refresh,
                                         "refresh_token_expires_in": 15897600, "token_type": "bearer", "scope": ""})

    def _codeload(self, path: str) -> httpx.Response:
        m = re.fullmatch(r"/([^/]+/[^/]+)/(?:zip|legacy\.zip)/([0-9a-f]{40})", path)
        repo = self.repos.get(m.group(1)) if m else None
        if not repo or m.group(2) != repo.sha:
            return httpx.Response(404)
        data = make_zip(f"{repo.full_name.replace('/', '-')}-{repo.sha[:7]}", repo.files, repo.symlinks)
        return httpx.Response(200, content=data, headers={"content-length": str(len(data))})
