"""Runtime configuration, read from environment variables (and an optional backend/.env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_DIR.parent

DOCUMENTED_MODELS = ("qwen-3.8-27b", "deepseek-v4.1-flash")


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines, never overrides variables already set."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    api_key: str | None
    base_url: str
    model: str
    reasoning_effort: str | None
    max_output_tokens: int
    request_timeout_s: float
    max_retries: int
    sandbox_backend: str  # "auto" | "docker" | "local"
    sandbox_image: str
    run_timeout_s: float
    per_test_timeout_s: int
    verification_runs: int
    max_ai_attempts: int
    allow_unsandboxed: bool
    db_path: Path
    examples_dir: Path
    max_concurrent_runs: int
    runs_per_minute: int

    @property
    def model_configured(self) -> bool:
        return bool(self.api_key)


def load_settings() -> Settings:
    _load_dotenv(BACKEND_DIR / ".env")
    _load_dotenv(REPO_ROOT / ".env")
    data_dir = Path(os.environ.get("PATCHPROOF_DATA_DIR", BACKEND_DIR / ".data"))
    return Settings(
        api_key=os.environ.get("BHARATCODE_API_KEY") or None,
        base_url=os.environ.get("BHARATCODE_BASE_URL", "https://bharatcode.ai/api/model/v1").rstrip("/"),
        model=os.environ.get("BHARATCODE_MODEL", "qwen-3.8-27b"),
        reasoning_effort=os.environ.get("BHARATCODE_REASONING_EFFORT") or None,
        max_output_tokens=_int("BHARATCODE_MAX_OUTPUT_TOKENS", 16000),
        request_timeout_s=_float("BHARATCODE_TIMEOUT_S", 180.0),
        max_retries=_int("BHARATCODE_MAX_RETRIES", 2),
        sandbox_backend=os.environ.get("PATCHPROOF_SANDBOX", "auto").lower(),
        sandbox_image=os.environ.get("PATCHPROOF_SANDBOX_IMAGE", "patchproof-sandbox:0.1"),
        run_timeout_s=_float("PATCHPROOF_RUN_TIMEOUT_S", 60.0),
        per_test_timeout_s=_int("PATCHPROOF_PER_TEST_TIMEOUT_S", 10),
        verification_runs=max(1, _int("PATCHPROOF_VERIFICATION_RUNS", 2)),
        max_ai_attempts=max(1, min(3, _int("PATCHPROOF_MAX_AI_ATTEMPTS", 2))),
        allow_unsandboxed=_bool("PATCHPROOF_ALLOW_UNSANDBOXED", False),
        db_path=data_dir / "patchproof.sqlite3",
        examples_dir=Path(os.environ.get("PATCHPROOF_EXAMPLES_DIR", REPO_ROOT / "examples")),
        max_concurrent_runs=max(1, _int("PATCHPROOF_MAX_CONCURRENT_RUNS", 2)),
        runs_per_minute=max(1, _int("PATCHPROOF_RUNS_PER_MINUTE", 12)),
    )
