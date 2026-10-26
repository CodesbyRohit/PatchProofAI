"""SQLite persistence for projects and run history (JSON documents, one row each)."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from .schemas import Project, Run, RunSummary

PROJECT_TTL_S = 24 * 3600


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, created_at REAL, data TEXT);
                CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, created_at REAL, data TEXT);
                CREATE INDEX IF NOT EXISTS runs_created ON runs(created_at);
                """
            )
            self._conn.commit()

    def save_project(self, project: Project) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO projects VALUES (?, ?, ?)",
                (project.id, time.time(), project.model_dump_json()),
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
                "INSERT OR REPLACE INTO runs VALUES (?, ?, ?)",
                (run.id, run.created_at, run.model_dump_json()),
            )
            self._conn.commit()

    def get_run(self, run_id: str) -> Run | None:
        with self._lock:
            row = self._conn.execute("SELECT data FROM runs WHERE id = ?", (run_id,)).fetchone()
        return Run.model_validate_json(row[0]) if row else None

    def list_runs(self, limit: int = 30) -> list[RunSummary]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        out = []
        for (data,) in rows:
            run = Run.model_validate_json(data)
            out.append(RunSummary(
                id=run.id, created_at=run.created_at, state=run.state, project_name=run.project_name,
                patch_source=run.patch_source,
                verdict=run.verdict.status if run.verdict else None,
                headline=run.verdict.headline if run.verdict else None,
            ))
        return out

    def mark_interrupted(self) -> None:
        """Runs left 'running' by a previous process can never finish; mark them as errors."""
        with self._lock:
            rows = self._conn.execute("SELECT data FROM runs").fetchall()
        for (data,) in rows:
            run = Run.model_validate_json(data)
            if run.state in ("queued", "running"):
                run.state = "error"
                run.error = "The server restarted before this run finished."
                self.save_run(run)
