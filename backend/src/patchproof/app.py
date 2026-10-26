"""FastAPI application: the HTTP API and (optionally) the built frontend."""

from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__
from .accounts import AccountService
from .agent.client import BharatCodeClient, BharatCodeError
from .auth import Principal, access_control_enabled, resolve_principal
from .config import REPO_ROOT, ConfigError, Settings, load_settings
from .examples import ScriptedPatch, load_examples
from .github import GitHubClient, GitHubError
from .middleware import Authenticate, BodyLimit
from .process_lock import acquire_process_lock
from .projects import MAX_ARCHIVE_BYTES, ProjectRejected, build_project, read_zip, validate_files
from .pull_requests import PullRequestRefused
from .redact import redact
from .repo_intake import read_repository_zip
from .sandbox import DockerSandbox
from .schemas import InlineProjectRequest, PatchSource, Project, Run, RunEvent, RunRequest
from .store import Store
from .usage import CLIENT_EVENTS, QuotaExceeded, Usage
from .verification.engine import Engine
from .verification.report import render_markdown

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("patchproof")

# Largest accepted request body: a 4 MB upload plus multipart overhead, or an inline project as JSON.
MAX_REQUEST_BYTES = 6 * 1024 * 1024
IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9_-]{8,100}")
IDEMPOTENCY_TTL_S = 600.0


class RateLimiter:
    """Sliding-window limiter keyed by principal (or client address for the open local principal)."""

    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= self.per_minute:
                return False
            q.append(now)
            return True


@dataclass
class Context:
    """Everything the route modules share."""

    settings: Settings
    store: Store
    engine: Engine
    github: GitHubClient
    accounts: AccountService | None
    examples: dict
    executor: ThreadPoolExecutor
    limiter: RateLimiter
    usage: Usage
    active: set[str] = field(default_factory=set)
    active_lock: threading.Lock = field(default_factory=threading.Lock)
    started_runs: dict[str, tuple[float, str]] = field(default_factory=dict)  # owner|Idempotency-Key -> run
    start_lock: threading.Lock = field(default_factory=threading.Lock)

    # -- request helpers -------------------------------------------------------------------------------

    @staticmethod
    def principal(request: Request) -> Principal:
        principal = getattr(request.state, "principal", None)
        if principal is None:  # only reachable on public paths that need a signed-in user
            raise HTTPException(401, "Sign in to continue.")
        return principal

    def rate_key(self, request: Request, principal: Principal) -> str:
        if principal.kind == "local":
            return request.client.host if request.client else "unknown"
        return principal.owner

    def check_capacity(self, request: Request, principal: Principal) -> None:
        if not self.limiter.allow(self.rate_key(request, principal)):
            raise HTTPException(429, "Too many runs. Please wait a minute.")
        with self.active_lock:
            if len(self.active) >= self.settings.max_concurrent_runs * 3:
                raise HTTPException(503, "The verification queue is full. Try again shortly.")

    def idempotent(self, request: Request, principal: Principal, start):
        """Run `start()` once per Idempotency-Key (scoped to the principal); a repeat returns the same run."""
        key = request.headers.get("idempotency-key")
        if key is not None and not IDEMPOTENCY_KEY.fullmatch(key):
            raise HTTPException(422, "Idempotency-Key must be 8-100 letters, digits, '-' or '_'.")
        scoped = f"{principal.owner}|{key}" if key else None
        with self.start_lock:
            now = time.monotonic()
            for stale in [k for k, (t, _) in self.started_runs.items() if now - t > IDEMPOTENCY_TTL_S]:
                del self.started_runs[stale]
            if scoped and scoped in self.started_runs:
                existing = self.store.get_run(self.started_runs[scoped][1])
                if existing:
                    return existing
            run = start()
            if scoped:
                self.started_runs[scoped] = (now, run.id)
            return run

    def owned_run(self, run_id: str, principal: Principal) -> Run:
        run = self.store.get_run(run_id)
        if not run or run.owner != principal.owner:  # someone else's run is indistinguishable from none
            raise HTTPException(404, "Run not found.")
        return run

    # -- jobs ------------------------------------------------------------------------------------------

    def submit(self, run: Run, project: Project | None = None, scripted: ScriptedPatch | None = None) -> None:
        with self.active_lock:
            self.active.add(run.id)
        self.executor.submit(self._job, run, project, scripted)

    def _job(self, run: Run, project: Project | None, scripted: ScriptedPatch | None) -> None:
        try:
            if project is None and run.source is not None:
                project = self._acquire(run)
            if project is not None:
                user_patch = run.request.user_patch if run.request else None
                self.engine.execute(run, project, scripted=scripted, user_patch=user_patch, notify=self.store.save_run)
                self.store.save_run(run)
        finally:
            with self.active_lock:
                self.active.discard(run.id)
            if run.state in ("finished", "error"):
                self.usage.run_finished(run)

    def _acquire(self, run: Run) -> Project | None:
        """Fetch the pinned commit and build the project. Credentials stay here, never in the sandbox."""
        source = run.source
        assert source is not None
        run.state = "running"
        self._event(run, "source", "started", f"Fetching {source.full_name} at {source.sha[:12]}")
        try:
            token = None
            if source.private:
                if self.accounts is None or not run.owner.startswith("user:"):
                    raise GitHubError("reauthorize", "Private repositories need a signed-in GitHub account.")
                token = self.accounts.access_token(int(run.owner.removeprefix("user:")))
            archive = self.github.download_archive(source.full_name, source.sha, token,
                                                   self.settings.repo_max_archive_bytes)
            repo_files = read_repository_zip(archive)
        except (GitHubError, ProjectRejected) as exc:
            message = exc.message if isinstance(exc, GitHubError) else str(exc)
            run.state, run.error, run.finished_at = "error", message, time.time()
            self._event(run, "source", "failed", message)
            return None
        source.skipped_count, source.skipped = repo_files.skipped_count, repo_files.skipped
        project = build_project(source.full_name.split("/", 1)[1], "github", repo_files.files)
        project.owner, project.source = run.owner, source
        self.store.save_project(project)
        run.project_id, run.project_name = project.id, source.full_name
        skipped = f"; skipped {repo_files.skipped_count} unsupported files" if repo_files.skipped_count else ""
        self._event(run, "source", "completed",
                    f"Fetched {len(repo_files.files)} files from {source.full_name} at {source.sha[:12]}{skipped}")
        return project

    def _event(self, run: Run, step: str, status: str, message: str) -> None:
        run.events.append(RunEvent(at=time.time(), step=step, status=status, message=message))  # type: ignore[arg-type]
        self.store.save_run(run)

    def resume(self, runs: list[Run]) -> None:
        """Restart runs that were queued (not yet started) when the previous process stopped."""
        for run in runs:
            if run.source is not None:
                self.submit(run)
                continue
            project = self.store.get_project(run.project_id)
            scripted = None
            if project and run.patch_source == PatchSource.scripted and run.request:
                ex = self.examples.get(project.id.removeprefix("example:"))
                scripted = next((p for p in ex[0].patches if p.id == run.request.scripted_patch_id), None) if ex else None
            if project is None or (run.patch_source == PatchSource.scripted and scripted is None):
                run.state, run.error, run.finished_at = "error", "The project expired before this run could start.", time.time()
                self.store.save_run(run)
                continue
            log.info("resuming queued run %s", run.id)
            self.submit(run, project, scripted)


def create_app(settings: Settings | None = None, engine: Engine | None = None,
               client: BharatCodeClient | None = None, github: GitHubClient | None = None) -> FastAPI:
    settings = settings or load_settings()
    problems = settings.problems()
    if problems:
        raise ConfigError("PatchProof will not start with this configuration:\n- " + "\n- ".join(problems))
    process_lock = acquire_process_lock(settings.data_dir / "patchproof.lock") if settings.hosted else None
    store = Store(settings.db_path)
    resumable = store.recover_after_restart()
    if client is None and settings.api_key:
        client = BharatCodeClient(settings.api_key, settings.base_url, timeout_s=settings.request_timeout_s,
                                  max_retries=settings.max_retries)
    engine = engine or Engine(settings, client)
    if (settings.sandbox_backend != "local" and isinstance(engine.docker_runner, DockerSandbox)
            and engine.docker_check()[0]):
        removed = engine.docker_runner.remove_orphans()
        if removed:
            log.warning("removed %d sandbox container(s) left behind by a previous server process", removed)
    if not access_control_enabled(settings):
        log.warning("no access control configured: the API is open to anyone who can reach it "
                    "(fine on a trusted machine; hosted deployments must set PATCHPROOF_HOSTED=1)")
    github = github or GitHubClient(settings.github_client_id, settings.github_client_secret)
    accounts = AccountService(settings, store, github) if settings.github_configured else None
    examples = load_examples(settings.examples_dir)
    for _, project in examples.values():
        store.save_project(project)
    ctx = Context(settings=settings, store=store, engine=engine, github=github, accounts=accounts,
                  examples=examples, limiter=RateLimiter(settings.runs_per_minute), usage=Usage(settings, store),
                  executor=ThreadPoolExecutor(max_workers=settings.max_concurrent_runs, thread_name_prefix="run"))
    event_limiter = RateLimiter(30)

    app = FastAPI(title="PatchProof AI", version=__version__)
    app.state.process_lock = process_lock
    app.state.ctx = ctx
    sign_in_hint = ("Sign in with GitHub to use this PatchProof instance." if settings.github_configured
                    else "This PatchProof instance requires an access token.")
    # Added innermost first: CORS wraps everything, so browsers can read 401, 403 and 413 responses too.
    app.add_middleware(Authenticate, resolve=lambda scope: resolve_principal(scope, settings, accounts),
                       sign_in_hint=sign_in_hint)
    app.add_middleware(BodyLimit, max_bytes=MAX_REQUEST_BYTES)
    if settings.cors_origins:
        # Exact origins only (validated at startup). Credentials let a same-site frontend send the session cookie.
        app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins), allow_credentials=True,
                           allow_methods=["GET", "POST"],
                           allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-CSRF-Token"],
                           expose_headers=["Content-Disposition"], max_age=600)

    @app.exception_handler(ProjectRejected)
    async def _rejected(_: Request, exc: ProjectRejected):
        return JSONResponse(status_code=422, content={"detail": str(exc), "code": "project.rejected"})

    @app.exception_handler(GitHubError)
    async def _github_error(_: Request, exc: GitHubError):
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
        return JSONResponse(status_code=exc.status, content={"detail": exc.message, "code": f"github.{exc.kind}"},
                            headers=headers)

    @app.exception_handler(QuotaExceeded)
    async def _quota(_: Request, exc: QuotaExceeded):
        resets = time.strftime("%Y-%m-%d", time.gmtime(exc.resets_at))
        return JSONResponse(status_code=429, content={
            "detail": f"Monthly limit reached: {exc.used} of {exc.limit} verifications used. It resets on {resets} (UTC).",
            "code": "quota.exceeded"})

    @app.exception_handler(PullRequestRefused)
    async def _pr_refused(_: Request, exc: PullRequestRefused):
        return JSONResponse(status_code=exc.status, content={"detail": exc.message, "code": "pull_request.refused"})

    # -- status ------------------------------------------------------------------

    @app.get("/api/health")
    def health():
        """Liveness: the process answers. Depends on nothing else, including the model provider."""
        return {"ok": True, "version": __version__}

    @app.get("/api/ready")
    def ready():
        """Readiness: the run store accepts writes and, where required, the Docker sandbox is usable."""
        try:
            store.ping()
            store_ok = True
        except Exception:
            log.exception("readiness: run store is not writable")
            store_ok = False
        docker_ok = engine.docker_check()[0] if settings.sandbox_backend != "local" else False
        is_ready = store_ok and (docker_ok or settings.sandbox_backend != "docker")
        body = {"ready": is_ready, "checks": {"store": store_ok, "docker_sandbox": docker_ok}}
        return JSONResponse(body, status_code=200 if is_ready else 503)

    @app.get("/api/status")
    def status():
        docker_ok, docker_detail = engine.docker_check()
        model: dict = {"configured": bool(client and client.configured), "model": settings.model,
                       "base_url": settings.base_url}
        if client and client.configured:
            try:
                ids, source = client.list_models()
                model.update(catalog=ids, catalog_source=source, model_available=settings.model in ids)
            except BharatCodeError as err:
                model.update(error=err.user_message())
        untrusted_ok = (docker_ok and settings.sandbox_backend != "local") or settings.allow_unsandboxed
        return {
            "version": __version__,
            "model": model,
            "sandbox": {
                "backend": settings.sandbox_backend,
                "docker_available": docker_ok,
                "detail": docker_detail,
                "untrusted_execution": untrusted_ok,
                "unsandboxed_override": settings.allow_unsandboxed,
            },
            "limits": {"verification_runs": settings.verification_runs, "max_ai_attempts": settings.max_ai_attempts,
                       "run_timeout_s": settings.run_timeout_s, "per_test_timeout_s": settings.per_test_timeout_s},
        }

    # -- examples & projects -------------------------------------------------------

    @app.get("/api/examples")
    def list_examples():
        return [
            {**ex.model_dump(exclude={"patches"}),
             "project_id": project.id,
             "patches": [p.model_dump(include={"id", "label", "description", "expected_verdict"}) for p in ex.patches],
             "summary": project.summary}
            for ex, project in examples.values()
        ]

    @app.get("/api/projects/{project_id}")
    def get_project(project_id: str, request: Request) -> Project:
        principal = ctx.principal(request)
        project = store.get_project(project_id)
        if not project or not (project.trusted or project.owner == principal.owner):
            raise HTTPException(404, "Project not found or expired.")
        return project

    @app.post("/api/projects/upload")
    async def upload(request: Request, file: UploadFile = File(...)) -> Project:
        principal = ctx.principal(request)
        data = await file.read(MAX_ARCHIVE_BYTES + 1)
        name = (file.filename or "upload.zip").rsplit("/", 1)[-1]
        if not name.lower().endswith(".zip"):
            raise ProjectRejected("Only .zip archives are supported for upload.")
        project = build_project(name.removesuffix(".zip"), "upload", read_zip(data))
        project.owner = principal.owner
        store.save_project(project)
        return project

    @app.post("/api/projects/inline")
    def inline(req: InlineProjectRequest, request: Request) -> Project:
        principal = ctx.principal(request)
        files = validate_files((f.path, f.content.encode("utf-8")) for f in req.files)
        project = build_project(req.name, "inline", files)
        project.owner = principal.owner
        store.save_project(project)
        return project

    # -- runs ----------------------------------------------------------------------

    @app.post("/api/runs", status_code=202)
    def create_run(req: RunRequest, request: Request) -> Run:
        """Start a run. A repeated Idempotency-Key returns the run it already started (no duplicate)."""
        principal = ctx.principal(request)
        return ctx.idempotent(request, principal, lambda: start_run(req, request, principal))

    def start_run(req: RunRequest, request: Request, principal: Principal) -> Run:
        ctx.check_capacity(request, principal)
        project_id = req.project_id or (f"example:{req.example_id}" if req.example_id else None)
        project = store.get_project(project_id) if project_id else None
        if not project or not (project.trusted or project.owner == principal.owner):
            raise HTTPException(404, "Project not found. Upload it again or pick an example.")
        scripted = None
        if req.patch_source == PatchSource.scripted:
            ex = examples.get(project.id.removeprefix("example:")) if project.trusted else None
            scripted = next((p for p in ex[0].patches if p.id == req.scripted_patch_id), None) if ex else None
            if not scripted:
                raise HTTPException(422, "Scripted patches are only available for bundled examples.")
        if req.patch_source == PatchSource.user and not (req.user_patch or "").strip():
            raise HTTPException(422, "Paste a unified diff to verify.")
        issue = req.issue
        if issue is None and project.trusted:
            ex = examples.get(project.id.removeprefix("example:"))
            issue = ex[0].issue if ex else None
        run = Run(id=f"run_{uuid.uuid4().hex[:12]}", created_at=time.time(), state="queued", owner=principal.owner,
                  project_id=project.id, project_name=project.name, patch_source=req.patch_source, issue=issue,
                  request=req)
        ctx.usage.run_started(run, project.origin, private=False)  # raises QuotaExceeded before anything is stored
        store.save_run(run)
        ctx.submit(run, project, scripted)
        return run

    @app.get("/api/runs")
    def list_runs(request: Request, limit: int = 30):
        return store.list_runs(min(max(limit, 1), 100), owner=ctx.principal(request).owner)

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str, request: Request) -> Run:
        return ctx.owned_run(run_id, ctx.principal(request))

    @app.get("/api/runs/{run_id}/patch.diff")
    def download_patch(run_id: str, request: Request):
        run = ctx.owned_run(run_id, ctx.principal(request))
        patch = next((a.patch for a in reversed(run.attempts) if a.patch and a.patch.diff), None)
        if not patch:
            raise HTTPException(404, "This run has no applicable patch.")
        ctx.usage.event("patch_downloaded", run.owner)
        return PlainTextResponse(patch.diff, media_type="text/x-diff",
                                 headers={"Content-Disposition": f'attachment; filename="{run_id}.diff"'})

    @app.get("/api/runs/{run_id}/report.md")
    def report_md(run_id: str, request: Request):
        run = ctx.owned_run(run_id, ctx.principal(request))
        ctx.usage.event("report_downloaded", run.owner)
        return PlainTextResponse(render_markdown(run), media_type="text/markdown",
                                 headers={"Content-Disposition": f'attachment; filename="{run_id}-report.md"'})

    @app.get("/api/runs/{run_id}/report.json")
    def report_json(run_id: str, request: Request):
        run = ctx.owned_run(run_id, ctx.principal(request))
        return JSONResponse(run.model_dump(mode="json", exclude={"request"}),
                            headers={"Content-Disposition": f'attachment; filename="{run_id}-report.json"'})

    # -- account and product events ---------------------------------------------------------------

    @app.get("/api/account/usage")
    def account_usage(request: Request):
        return ctx.usage.account(ctx.principal(request).owner)

    @app.post("/api/events", status_code=204)
    def client_event(body: dict, request: Request):
        """The two events only the browser can observe. Anything else is ignored; nothing but the name is kept."""
        name = body.get("name")
        if name in CLIENT_EVENTS and event_limiter.allow(request.client.host if request.client else "unknown"):
            principal = getattr(request.state, "principal", None)
            props = {"visibility": str(body.get("visibility"))} if name == "repository_selected" else {}
            ctx.usage.event(name, principal.owner if principal else None, **props)
        return Response(status_code=204)

    from .github_routes import register_github_routes  # noqa: PLC0415 - avoids an import cycle with Context

    register_github_routes(app, ctx)

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception):
        log.exception("unhandled error")
        return JSONResponse(status_code=500, content={"detail": redact(f"Internal error: {type(exc).__name__}")})

    # -- frontend (production build) ---------------------------------------------------
    dist = Path(REPO_ROOT / "frontend" / "dist")
    if dist.is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            # Unknown API paths must stay JSON 404s, never the app shell.
            if path == "api" or path.startswith("api/"):
                raise HTTPException(404, "Not found.")
            candidate = (dist / path).resolve()
            if path and candidate.is_file() and candidate.is_relative_to(dist.resolve()):
                return FileResponse(candidate)
            return FileResponse(dist / "index.html")

    ctx.resume(resumable)
    return app
