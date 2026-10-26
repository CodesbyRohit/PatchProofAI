"""SQLite persistence: projects and run history (JSON documents, one row each), plus accounts and sessions."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from .schemas import Project, Run, RunSummary

PROJECT_TTL_S = 24 * 3600
OAUTH_STATE_TTL_S = 600

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, created_at REAL, data TEXT);
CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, created_at REAL, data TEXT);
CREATE INDEX IF NOT EXISTS runs_created ON runs(created_at);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,           -- GitHub user id: stable even if the login is renamed
    login TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_login_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id_hash TEXT PRIMARY KEY,         -- SHA-256 of the cookie value; the value itself is never stored
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS github_tokens (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    access_enc TEXT NOT NULL,         -- Fernet-encrypted with PATCHPROOF_SECRET_KEY
    access_expires_at REAL,
    refresh_enc TEXT,
    refresh_expires_at REAL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_states (
    state_hash TEXT PRIMARY KEY,      -- SHA-256 of the state sent to GitHub; single use
    verifier TEXT NOT NULL,           -- PKCE code verifier
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS usage (    -- one row per run: what it cost, never what it contained
    run_id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    period TEXT NOT NULL,             -- UTC month, e.g. 2026-10
    created_at REAL NOT NULL,
    origin TEXT NOT NULL,             -- example | upload | inline | github
    patch_source TEXT NOT NULL,
    private INTEGER NOT NULL DEFAULT 0,
    counts_toward_quota INTEGER NOT NULL,
    state TEXT NOT NULL,
    verdict TEXT,
    duration_s REAL,
    sandbox_s REAL NOT NULL DEFAULT 0,
    model_calls INTEGER NOT NULL DEFAULT 0,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS usage_owner_period ON usage(owner, period);
CREATE TABLE IF NOT EXISTS entitlements (owner TEXT PRIMARY KEY, plan TEXT NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS events (   -- first-party product analytics: allowlisted names, no content
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    subject TEXT,                     -- keyed hash of the owner, never the owner itself
    at REAL NOT NULL,
    props TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS events_at ON events(at);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.executescript(SCHEMA)
            # Databases created before ownership existed: their rows belong to the local (open) principal.
            for table in ("projects", "runs"):
                columns = {row[1] for row in self._conn.execute(f"PRAGMA table_info({table})")}  # noqa: S608
                if "owner" not in columns:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN owner TEXT NOT NULL DEFAULT 'local'")
            self._conn.execute("CREATE INDEX IF NOT EXISTS runs_owner ON runs(owner, created_at)")
            self._conn.commit()

    def ping(self) -> None:
        """Raise unless the database can take a write lock (used by the readiness check)."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute("ROLLBACK")

    # -- projects and runs ---------------------------------------------------------------------------

    def save_project(self, project: Project) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO projects (id, created_at, data, owner) VALUES (?, ?, ?, ?)",
                (project.id, time.time(), project.model_dump_json(), project.owner),
            )
            self._conn.execute("DELETE FROM projects WHERE created_at < ?", (time.time() - PROJECT_TTL_S,))
            self._conn.commit()

    def get_project(self, project_id: str) -> Project | None:
        with self._lock:
            row = self._conn.execute("SELECT data FROM projects WHERE id = ?", (project_id,)).fetchone()
        return Project.model_validate_json(row[0]) if row else None

    def save_run(self, run: Run) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO runs (id, created_at, data, owner) VALUES (?, ?, ?, ?)",
                (run.id, run.created_at, run.model_dump_json(), run.owner),
            )
            self._conn.commit()

    def get_run(self, run_id: str) -> Run | None:
        with self._lock:
            row = self._conn.execute("SELECT data FROM runs WHERE id = ?", (run_id,)).fetchone()
        return Run.model_validate_json(row[0]) if row else None

    def list_runs(self, limit: int = 30, owner: str = "local") -> list[RunSummary]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM runs WHERE owner = ? ORDER BY created_at DESC LIMIT ?", (owner, limit)
            ).fetchall()
        out = []
        for (data,) in rows:
            run = Run.model_validate_json(data)
            out.append(RunSummary(
                id=run.id, created_at=run.created_at, state=run.state, project_name=run.project_name,
                repository=run.source.full_name if run.source else None,
                patch_source=run.patch_source,
                verdict=run.verdict.status if run.verdict else None,
                headline=run.verdict.headline if run.verdict else None,
            ))
        return out

    def recover_after_restart(self) -> list[Run]:
        """Prepare runs left by a previous process; return the queued ones that can start again.

        A run that was executing can never finish (its sandbox containers are gone), so it is marked as an
        error. A queued run had not started: if its request was stored it is returned to be resumed, never
        given a result it did not earn.
        """
        with self._lock:
            rows = self._conn.execute("SELECT data FROM runs ORDER BY created_at").fetchall()
        resumable = []
        for (data,) in rows:
            run = Run.model_validate_json(data)
            if run.state == "running" or (run.state == "queued" and run.request is None and run.source is None):
                run.state = "error"
                run.error = "The server restarted before this run finished."
                run.finished_at = time.time()
                self.save_run(run)
                with self._lock:
                    self._conn.execute("UPDATE usage SET state = 'error' WHERE run_id = ?", (run.id,))
                    self._conn.commit()
            elif run.state == "queued":
                resumable.append(run)
        return resumable

    # -- accounts --------------------------------------------------------------------------------------

    def upsert_user(self, user_id: int, login: str) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO users (id, login, created_at, last_login_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET login = excluded.login, last_login_at = excluded.last_login_at",
                (user_id, login, now, now),
            )
            self._conn.commit()

    def get_user(self, user_id: int) -> tuple[int, str] | None:
        with self._lock:
            row = self._conn.execute("SELECT id, login FROM users WHERE id = ?", (user_id,)).fetchone()
        return (row[0], row[1]) if row else None

    def create_session(self, id_hash: str, user_id: int, csrf: str, ttl_s: float) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
            self._conn.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?)", (id_hash, user_id, csrf, now, now + ttl_s))
            self._conn.commit()

    def get_session(self, id_hash: str) -> tuple[int, str] | None:
        """(user_id, csrf) for a live session, else None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT user_id, csrf FROM sessions WHERE id_hash = ? AND expires_at > ?", (id_hash, time.time())
            ).fetchone()
        return (row[0], row[1]) if row else None

    def delete_session(self, id_hash: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM sessions WHERE id_hash = ?", (id_hash,))
            self._conn.commit()

    # -- retention and deletion -----------------------------------------------------------------------------

    def delete_run(self, run_id: str, owner: str) -> bool:
        """Delete one run and its project copy. Only the owner's own run; returns whether anything was deleted."""
        with self._lock:
            row = self._conn.execute("SELECT data FROM runs WHERE id = ? AND owner = ?", (run_id, owner)).fetchone()
            if not row:
                return False
            project_id = json.loads(row[0]).get("project_id")
            self._conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
            self._conn.execute("DELETE FROM projects WHERE id = ? AND owner = ? AND owner != 'local'", (project_id, owner))
            self._conn.commit()
        return True

    def expire_projects(self) -> int:
        """Delete project copies (uploaded or fetched source files) older than their 24-hour lifetime."""
        with self._lock:
            n = self._conn.execute("DELETE FROM projects WHERE created_at < ? AND id NOT LIKE 'example:%'",
                                   (time.time() - PROJECT_TTL_S,)).rowcount
            self._conn.commit()
        return n

    def purge_before(self, cutoff: float) -> int:
        """Delete finished runs created before `cutoff`, with their projects. Queued and running runs are kept."""
        with self._lock:
            old = self._conn.execute(
                "SELECT id, data FROM runs WHERE created_at < ?", (cutoff,)).fetchall()
            gone = 0
            for run_id, data in old:
                doc = json.loads(data)
                if doc.get("state") in ("queued", "running"):
                    continue
                self._conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
                if not str(doc.get("project_id", "")).startswith("example:"):
                    self._conn.execute("DELETE FROM projects WHERE id = ?", (doc.get("project_id"),))
                gone += 1
            self._conn.execute("DELETE FROM usage WHERE created_at < ? AND state IN ('finished', 'error')", (cutoff,))
            self._conn.commit()
        return gone

    def delete_owner_data(self, owner: str, user_id: int | None, subject: str | None) -> dict[str, int]:
        """Remove everything stored for one account: runs, projects, usage, analytics events, tokens, sessions."""
        with self._lock:
            counts = {}
            for table in ("runs", "projects", "usage"):
                counts[table] = self._conn.execute(f"DELETE FROM {table} WHERE owner = ?", (owner,)).rowcount  # noqa: S608
            counts["events"] = (self._conn.execute("DELETE FROM events WHERE subject = ?", (subject,)).rowcount
                                if subject else 0)
            self._conn.execute("DELETE FROM entitlements WHERE owner = ?", (owner,))
            if user_id is not None:
                self._conn.execute("DELETE FROM users WHERE id = ?", (user_id,))  # cascades to sessions and tokens
            self._conn.commit()
        return counts

    def delete_user_sessions(self, user_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            self._conn.commit()

    def save_github_tokens(self, user_id: int, access_enc: str, access_expires_at: float | None,
                           refresh_enc: str | None, refresh_expires_at: float | None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO github_tokens VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, access_enc, access_expires_at, refresh_enc, refresh_expires_at, time.time()),
            )
            self._conn.commit()

    def get_github_tokens(self, user_id: int) -> tuple[str, float | None, str | None, float | None] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT access_enc, access_expires_at, refresh_enc, refresh_expires_at FROM github_tokens "
                "WHERE user_id = ?", (user_id,)).fetchone()
        return (row[0], row[1], row[2], row[3]) if row else None

    def delete_github_tokens(self, user_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM github_tokens WHERE user_id = ?", (user_id,))
            self._conn.commit()

    def save_oauth_state(self, state_hash: str, verifier: str) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute("DELETE FROM oauth_states WHERE created_at < ?", (now - OAUTH_STATE_TTL_S,))
            self._conn.execute("INSERT INTO oauth_states VALUES (?, ?, ?)", (state_hash, verifier, now))
            self._conn.commit()

    # -- usage, plans and analytics -------------------------------------------------------------------------

    def meta_value(self, key: str, default_factory) -> str:
        """A per-instance value created once (e.g. the analytics salt)."""
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            if row:
                return row[0]
            value = default_factory()
            self._conn.execute("INSERT INTO meta VALUES (?, ?)", (key, value))
            self._conn.commit()
            return value

    def reserve_usage(self, run_id: str, owner: str, period: str, origin: str, patch_source: str, private: bool,
                      counts_toward_quota: bool, limit: int | None) -> int | None:
        """Atomically check the monthly limit and record the run. Returns the count that blocked it, else None.

        The check and the insert happen under one lock, so parallel requests cannot all pass the check.
        """
        with self._lock:
            if counts_toward_quota and limit is not None:
                used = self._conn.execute(self._COUNTED, (owner, period)).fetchone()[0]
                if used >= limit:
                    return used
            self._conn.execute(
                "INSERT OR IGNORE INTO usage (run_id, owner, period, created_at, origin, patch_source, private, "
                "counts_toward_quota, state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued')",
                (run_id, owner, period, time.time(), origin, patch_source, int(private), int(counts_toward_quota)))
            self._conn.commit()
        return None

    # Runs that ended in an error before any verdict (a failed fetch, a restart) do not use up the quota.
    _COUNTED = ("SELECT COUNT(*) FROM usage WHERE owner = ? AND period = ? AND counts_toward_quota = 1 "
                "AND state != 'error'")

    def record_usage_finish(self, run_id: str, state: str, verdict: str | None, duration_s: float | None,
                            sandbox_s: float, model_calls: int, prompt_tokens: int, completion_tokens: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE usage SET state = ?, verdict = ?, duration_s = ?, sandbox_s = ?, model_calls = ?, "
                "prompt_tokens = ?, completion_tokens = ? WHERE run_id = ?",
                (state, verdict, duration_s, sandbox_s, model_calls, prompt_tokens, completion_tokens, run_id))
            self._conn.commit()

    def counted_runs(self, owner: str, period: str) -> int:
        with self._lock:
            return self._conn.execute(self._COUNTED, (owner, period)).fetchone()[0]

    def usage_rows(self, since: float) -> list[sqlite3.Row]:
        with self._lock:
            self._conn.row_factory = sqlite3.Row
            try:
                return self._conn.execute("SELECT * FROM usage WHERE created_at >= ?", (since,)).fetchall()
            finally:
                self._conn.row_factory = None

    def get_plan(self, owner: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT plan FROM entitlements WHERE owner = ?", (owner,)).fetchone()
        return row[0] if row else None

    def set_plan(self, owner: str, plan: str) -> None:
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO entitlements VALUES (?, ?, ?)", (owner, plan, time.time()))
            self._conn.commit()

    def add_event(self, name: str, subject: str | None, props: str, retention_s: float) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute("INSERT INTO events (name, subject, at, props) VALUES (?, ?, ?, ?)", (name, subject, now, props))
            self._conn.execute("DELETE FROM events WHERE at < ?", (now - retention_s,))
            self._conn.commit()

    def events_since(self, since: float) -> list[tuple[str, str | None, float, str]]:
        with self._lock:
            return self._conn.execute("SELECT name, subject, at, props FROM events WHERE at >= ?", (since,)).fetchall()

    def take_oauth_state(self, state_hash: str) -> str | None:
        """Consume a state (single use): its PKCE verifier if it exists and has not expired."""
        with self._lock:
            row = self._conn.execute("SELECT verifier, created_at FROM oauth_states WHERE state_hash = ?",
                                     (state_hash,)).fetchone()
            self._conn.execute("DELETE FROM oauth_states WHERE state_hash = ?", (state_hash,))
            self._conn.commit()
        if not row or time.time() - row[1] > OAUTH_STATE_TTL_S:
            return None
        return row[0]
