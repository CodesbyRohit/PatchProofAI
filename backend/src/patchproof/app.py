"""FastAPI application: the HTTP API and (optionally) the built frontend."""

from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .agent.client import BharatCodeClient, BharatCodeError
from .config import REPO_ROOT, ConfigError, Settings, load_settings
from .examples import load_examples
from .middleware import BodyLimit, RequireToken
from .process_lock import acquire_process_lock
from .projects import MAX_ARCHIVE_BYTES, ProjectRejected, build_project, read_zip, validate_files
from .redact import redact
from .sandbox import DockerSandbox
from .schemas import InlineProjectRequest, PatchSource, Project, Run, RunRequest
from .store import Store
from .verification.engine import Engine
from .verification.report import render_markdown

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("patchproof")

# Largest accepted request body: a 4 MB upload plus multipart overhead, or an inline project as JSON.
MAX_REQUEST_BYTES = 6 * 1024 * 1024
IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9_-]{8,100}")
IDEMPOTENCY_TTL_S = 600.0


class RateLimiter:
    """Sliding-window limiter keyed by client address."""

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


def create_app(settings: Settings | None = None, engine: Engine | None = None,
               client: BharatCodeClient | None = None) -> FastAPI:
    settings = settings or load_settings()
    problems = settings.problems()
    if problems:
        raise ConfigError("PatchProof will not start with this configuration:\n- " + "\n- ".join(problems))
    process_lock = acquire_process_lock(settings.data_dir / "patchproof.lock") if settings.hosted else None
    store = Store(settings.db_path)
    store.mark_interrupted()
    if client is None and settings.api_key:
        client = BharatCodeClient(settings.api_key, settings.base_url, timeout_s=settings.request_timeout_s,
                                  max_retries=settings.max_retries)
    engine = engine or Engine(settings, client)
    if (settings.sandbox_backend != "local" and isinstance(engine.docker_runner, DockerSandbox)
            and engine.docker_check()[0]):
        removed = engine.docker_runner.remove_orphans()
        if removed:
            log.warning("removed %d sandbox container(s) left behind by a previous server process", removed)
    if not settings.access_token_hashes:
        log.warning("no access tokens configured: the API is open to anyone who can reach it "
                    "(fine on a trusted machine; hosted deployments must set PATCHPROOF_HOSTED=1)")
    examples = load_examples(settings.examples_dir)
    for _, project in examples.values():
        store.save_project(project)
    executor = ThreadPoolExecutor(max_workers=settings.max_concurrent_runs, thread_name_prefix="run")
    limiter = RateLimiter(settings.runs_per_minute)
    active: set[str] = set()
    active_lock = threading.Lock()
    started_runs: dict[str, tuple[float, str]] = {}  # Idempotency-Key -> (time, run id)
    start_lock = threading.Lock()

    app = FastAPI(title="PatchProof AI", version=__version__)
    app.state.process_lock = process_lock
    # Added innermost first: CORS wraps everything, so browsers can read 401 and 413 responses too.
    app.add_middleware(RequireToken, token_hashes=settings.access_token_hashes)
    app.add_middleware(BodyLimit, max_bytes=MAX_REQUEST_BYTES)
    if settings.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins), allow_credentials=False,
                           allow_methods=["GET", "POST"],
                           allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
                           expose_headers=["Content-Disposition"], max_age=600)

    @app.exception_handler(ProjectRejected)
    async def _rejected(_: Request, exc: ProjectRejected):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

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
    def get_project(project_id: str) -> Project:
        project = store.get_project(project_id)
        if not project:
            raise HTTPException(404, "Project not found or expired.")
        return project

    @app.post("/api/projects/upload")
    async def upload(file: UploadFile = File(...)) -> Project:
        data = await file.read(MAX_ARCHIVE_BYTES + 1)
        name = (file.filename or "upload.zip").rsplit("/", 1)[-1]
        if not name.lower().endswith(".zip"):
            raise ProjectRejected("Only .zip archives are supported for upload.")
        project = build_project(name.removesuffix(".zip"), "upload", read_zip(data))
        store.save_project(project)
        return project

    @app.post("/api/projects/inline")
    def inline(req: InlineProjectRequest) -> Project:
        files = validate_files((f.path, f.content.encode("utf-8")) for f in req.files)
        project = build_project(req.name, "inline", files)
        store.save_project(project)
        return project

    # -- runs ----------------------------------------------------------------------

    @app.post("/api/runs", status_code=202)
    def create_run(req: RunRequest, request: Request) -> Run:
        """Start a run. A repeated Idempotency-Key returns the run it already started (no duplicate)."""
        key = request.headers.get("idempotency-key")
        if key is not None and not IDEMPOTENCY_KEY.fullmatch(key):
            raise HTTPException(422, "Idempotency-Key must be 8-100 letters, digits, '-' or '_'.")
        with start_lock:
            now = time.monotonic()
            for stale in [k for k, (t, _) in started_runs.items() if now - t > IDEMPOTENCY_TTL_S]:
                del started_runs[stale]
            if key and key in started_runs:
                existing = store.get_run(started_runs[key][1])
                if existing:
                    return existing
            run = start_run(req, request)
            if key:
                started_runs[key] = (now, run.id)
            return run

    def start_run(req: RunRequest, request: Request) -> Run:
        client_key = request.client.host if request.client else "unknown"
        if not limiter.allow(client_key):
            raise HTTPException(429, "Too many runs. Please wait a minute.")
        with active_lock:
            if len(active) >= settings.max_concurrent_runs * 3:
                raise HTTPException(503, "The verification queue is full. Try again shortly.")
        project_id = req.project_id or (f"example:{req.example_id}" if req.example_id else None)
        project = store.get_project(project_id) if project_id else None
        if not project:
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
        run = Run(id=f"run_{uuid.uuid4().hex[:12]}", created_at=time.time(), state="queued",
                  project_id=project.id, project_name=project.name, patch_source=req.patch_source, issue=issue)
        store.save_run(run)
        with active_lock:
            active.add(run.id)

        def job():
            try:
                engine.execute(run, project, scripted=scripted, user_patch=req.user_patch, notify=store.save_run)
                store.save_run(run)
            finally:
                with active_lock:
                    active.discard(run.id)

        executor.submit(job)
        return run

    @app.get("/api/runs")
    def list_runs(limit: int = 30):
        return store.list_runs(min(max(limit, 1), 100))

    def _get(run_id: str) -> Run:
        run = store.get_run(run_id)
        if not run:
            raise HTTPException(404, "Run not found.")
        return run

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> Run:
        return _get(run_id)

    @app.get("/api/runs/{run_id}/patch.diff")
    def download_patch(run_id: str):
        run = _get(run_id)
        patch = next((a.patch for a in reversed(run.attempts) if a.patch and a.patch.diff), None)
        if not patch:
            raise HTTPException(404, "This run has no applicable patch.")
        return PlainTextResponse(patch.diff, media_type="text/x-diff",
                                 headers={"Content-Disposition": f'attachment; filename="{run_id}.diff"'})

    @app.get("/api/runs/{run_id}/report.md")
    def report_md(run_id: str):
        run = _get(run_id)
        return PlainTextResponse(render_markdown(run), media_type="text/markdown",
                                 headers={"Content-Disposition": f'attachment; filename="{run_id}-report.md"'})

    @app.get("/api/runs/{run_id}/report.json")
    def report_json(run_id: str):
        run = _get(run_id)
        return JSONResponse(run.model_dump(mode="json"),
                            headers={"Content-Disposition": f'attachment; filename="{run_id}-report.json"'})

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

    return app
