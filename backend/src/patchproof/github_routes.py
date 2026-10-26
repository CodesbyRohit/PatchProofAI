"""HTTP routes for GitHub sign-in, repository discovery, repository runs and pull requests."""

from __future__ import annotations

import threading
import time
import uuid
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from .accounts import SESSION_COOKIE, STATE_COOKIE
from .github import SHA_RE, GitHubError, split_repository
from .pull_requests import create_pull_request, patch_sha256, verified_patch
from .schemas import GitHubRunRequest, PatchSource, PullRequestCreate, RepoSource, Run, RunRequest

if TYPE_CHECKING:
    from .app import Context
    from .auth import Principal

# Shown after a failed sign-in as /github?error=<code>; never contains provider details or secrets.
SIGN_IN_ERRORS = {"invalid": "state", "forbidden": "not_allowed", "reauthorize": "denied"}


def _repo_summary(repo: dict[str, Any], access: str) -> dict[str, Any]:
    return {
        "id": repo["id"], "full_name": repo["full_name"], "private": bool(repo.get("private")),
        "description": repo.get("description"), "default_branch": repo.get("default_branch"),
        "archived": bool(repo.get("archived")), "html_url": repo.get("html_url"),
        "pushed_at": repo.get("pushed_at"), "access": access,
    }


def register_github_routes(app: FastAPI, ctx: Context) -> None:
    settings, store, github, accounts = ctx.settings, ctx.store, ctx.github, ctx.accounts
    pr_lock = threading.Lock()  # one pull-request write at a time: a double click cannot create two

    def require_accounts():
        if accounts is None:
            raise HTTPException(404, "GitHub sign-in is not configured on this PatchProof instance.")
        return accounts

    def require_user(request: Request) -> Principal:
        principal = ctx.principal(request)
        if principal.kind != "user" or principal.user_id is None:
            raise HTTPException(401, "Sign in with GitHub to use repositories.")
        return principal

    def user_token(principal: Principal) -> str:
        return require_accounts().access_token(principal.user_id or 0)

    def cookie_args(max_age: int, path: str = "/") -> dict[str, Any]:
        return {"max_age": max_age, "path": path, "httponly": True, "secure": settings.cookie_secure,
                "samesite": "lax"}

    def app_page(path: str) -> str:
        return (settings.app_url or "") + path

    def lookup_repository(full_name: str, principal: Principal) -> tuple[dict[str, Any], str]:
        """Repository metadata and how the user may read it: "installed" (via the app) or "public"."""
        split_repository(full_name)
        try:
            return github.repository(full_name, user_token(principal)), "installed"
        except GitHubError as err:
            if err.kind not in {"not_found", "forbidden"}:
                raise
        # Not reachable through the app installation: only a public repository may be used, and GitHub decides
        # that from an anonymous request (a private repository answers 404 here).
        repo = github.repository(full_name, None)
        if repo.get("private"):
            raise GitHubError("not_found", "Repository not found, or not accessible to you.")
        return repo, "public"

    # -- sign-in -------------------------------------------------------------------------------------

    @app.get("/api/auth/me")
    def me(request: Request):
        principal = getattr(request.state, "principal", None)
        body: dict[str, Any] = {
            "authenticated": principal is not None and principal.kind != "local",
            "kind": principal.kind if principal else None,
            "login": principal.login if principal else None,
            "csrf": principal.csrf if principal and principal.kind == "user" else None,
            "github": {
                "configured": settings.github_configured,
                "install_url": f"https://github.com/apps/{settings.github_app_slug}/installations/new"
                if settings.github_app_slug else None,
                "pull_requests": settings.github_pr_writes,
            },
            "token_sign_in": bool(settings.access_token_hashes),
        }
        return JSONResponse(body, headers={"Cache-Control": "no-store"})

    @app.get("/api/auth/github/start")
    def start_sign_in():
        url, state = require_accounts().start_login()
        ctx.usage.event("github_sign_in_started", None)
        response = RedirectResponse(url, status_code=302)
        response.set_cookie(STATE_COOKIE, state, **cookie_args(600, "/api/auth/github"))
        return response

    @app.get("/api/auth/github/callback")
    def finish_sign_in(request: Request, code: str = "", state: str = "", error: str = ""):
        service = require_accounts()
        response = RedirectResponse(app_page("/github"), status_code=302)
        response.delete_cookie(STATE_COOKIE, path="/api/auth/github")
        if error or not code or not state:
            response.headers["location"] = app_page("/github?error=denied")
            return response
        try:
            session = service.complete_login(code[:200], state[:200], request.cookies.get(STATE_COOKIE))
        except GitHubError as err:
            response.headers["location"] = app_page(f"/github?error={SIGN_IN_ERRORS.get(err.kind, 'unavailable')}")
            return response
        response.set_cookie(SESSION_COOKIE, session, **cookie_args(settings.session_ttl_s))
        user = service.session_user(session)
        ctx.usage.event("github_connected", f"user:{user.user_id}" if user else None)
        return response

    @app.post("/api/auth/logout")
    def sign_out(request: Request):
        cookie = request.cookies.get(SESSION_COOKIE)
        if accounts is not None and cookie:
            accounts.logout(cookie)
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @app.post("/api/auth/github/disconnect")
    def disconnect(request: Request):
        principal = require_user(request)
        require_accounts().disconnect(principal.user_id or 0)
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    # -- repositories --------------------------------------------------------------------------------

    @app.get("/api/github/installations")
    def installations(request: Request):
        token = user_token(require_user(request))
        return [{"id": i["id"], "account": i["account"]["login"], "repository_selection": i.get("repository_selection")}
                for i in github.installations(token)]

    @app.get("/api/github/installations/{installation_id}/repos")
    def installation_repos(installation_id: int, request: Request, page: int = 1):
        token = user_token(require_user(request))
        page = min(max(page, 1), 1000)
        repos, total = github.installation_repositories(token, installation_id, page)
        return {"repositories": [_repo_summary(r, "installed") for r in repos], "total_count": total,
                "page": page, "has_more": page * 100 < total}

    @app.get("/api/github/repo")
    def repository(full_name: str, request: Request):
        repo, access = lookup_repository(full_name, require_user(request))
        return _repo_summary(repo, access)

    @app.get("/api/github/branches")
    def branches(full_name: str, request: Request, page: int = 1):
        principal = require_user(request)
        _, access = lookup_repository(full_name, principal)
        token = user_token(principal) if access == "installed" else None
        items = github.branches(full_name, token, min(max(page, 1), 100))
        return {"branches": [{"name": b["name"], "sha": b["commit"]["sha"], "protected": bool(b.get("protected"))}
                             for b in items], "page": page, "has_more": len(items) == 100}

    @app.get("/api/github/commit")
    def commit(full_name: str, ref: str, request: Request):
        """Resolve a branch (or SHA) to the exact commit a run will be pinned to."""
        principal = require_user(request)
        _, access = lookup_repository(full_name, principal)
        found = github.commit(full_name, ref, user_token(principal) if access == "installed" else None)
        info = found.get("commit", {})
        return {"sha": found["sha"], "message": (info.get("message") or "").split("\n", 1)[0][:200],
                "author": (info.get("author") or {}).get("name"), "date": (info.get("author") or {}).get("date"),
                "html_url": found.get("html_url")}

    # -- runs on a repository ------------------------------------------------------------------------

    @app.post("/api/github/runs", status_code=202)
    def create_github_run(req: GitHubRunRequest, request: Request) -> Run:
        principal = require_user(request)
        return ctx.idempotent(request, principal, lambda: start_github_run(req, request, principal))

    def start_github_run(req: GitHubRunRequest, request: Request, principal: Principal) -> Run:
        if req.patch_source == PatchSource.user and not (req.user_patch or "").strip():
            raise HTTPException(422, "Paste a unified diff to verify.")
        repo, access = lookup_repository(req.repository, principal)
        if repo.get("private") and req.patch_source == PatchSource.ai and not req.allow_model_upload:
            raise HTTPException(422, "AI mode sends selected files of this private repository to the BharatCode "
                                     "model API. Confirm that before starting the run.")
        # The commit must exist in this repository (and be readable by this user) before anything runs.
        found = github.commit(req.repository, req.sha, user_token(principal) if access == "installed" else None)
        if found.get("sha") != req.sha or not SHA_RE.match(req.sha):
            raise HTTPException(409, "That commit could not be confirmed. Reload the branch and try again.")
        ctx.check_capacity(request, principal)
        source = RepoSource(repo_id=int(repo["id"]), full_name=repo["full_name"], private=bool(repo.get("private")),
                            ref=req.ref, sha=req.sha, html_url=f"{repo['html_url']}/tree/{req.sha}")
        run = Run(id=f"run_{uuid.uuid4().hex[:12]}", created_at=time.time(), state="queued", owner=principal.owner,
                  project_id="pending", project_name=repo["full_name"], source=source, patch_source=req.patch_source,
                  issue=req.issue,
                  request=RunRequest(patch_source=req.patch_source, user_patch=req.user_patch, issue=req.issue))
        ctx.usage.run_started(run, "github", private=source.private)  # atomic quota check; raises before storing
        store.save_run(run)
        ctx.submit(run)
        return run

    # -- pull requests -------------------------------------------------------------------------------

    @app.get("/api/runs/{run_id}/pull-request")
    def pull_request_preview(run_id: str, request: Request):
        """What would be written, for the confirmation screen."""
        principal = require_user(request)
        run = ctx.owned_run(run_id, principal)
        if run.source is None:
            raise HTTPException(409, "Only runs on a GitHub repository can become a pull request.")
        patch = verified_patch(run)
        return {"enabled": settings.github_pr_writes, "repository": run.source.full_name, "source_sha": run.source.sha,
                "base": run.source.ref, "branch": f"patchproof/{run.id}", "changed_files": patch.changed_files,
                "diff": patch.diff, "patch_sha256": patch_sha256(patch.diff), "existing": run.pull_request}

    @app.post("/api/runs/{run_id}/pull-request")
    def open_pull_request(run_id: str, req: PullRequestCreate, request: Request):
        principal = require_user(request)
        if not settings.github_pr_writes:
            raise HTTPException(403, "Pull requests are disabled on this instance. Download the patch instead.")
        run = ctx.owned_run(run_id, principal)
        with pr_lock:
            record = create_pull_request(github, user_token(principal), run, req, settings.repo_max_archive_bytes)
            run.pull_request = record
            store.save_run(run)
        if record.state != "created":
            raise HTTPException(502, f"GitHub did not create the pull request: {record.error}")
        ctx.usage.event("pull_request_created", run.owner)
        return record
