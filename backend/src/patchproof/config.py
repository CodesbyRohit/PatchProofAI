"""Runtime configuration, read from environment variables (and an optional backend/.env file)."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

BACKEND_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_DIR.parent

DOCUMENTED_MODELS = ("qwen-3.8-27b", "deepseek-v4.1-flash")
SANDBOX_BACKENDS = ("auto", "docker", "local")
_TOKEN_HASH = re.compile(r"[0-9a-f]{64}")


class ConfigError(RuntimeError):
    """The configuration is invalid; the server refuses to start rather than run with it."""


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


def _list(name: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in os.environ.get(name, "").split(",") if item.strip())


def origin_problem(origin: str) -> str | None:
    """Why `origin` is not an exact browser origin (scheme://host[:port]), or None if it is."""
    if "*" in origin:
        return f"wildcards are not allowed: {origin!r}"
    try:
        parts = urlsplit(origin)
        parts.port  # noqa: B018 - raises ValueError on a malformed port
    except ValueError:
        return f"not a valid origin: {origin!r}"
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return f"must start with http:// or https:// and name a host: {origin!r}"
    if parts.username or parts.password or parts.path or parts.query or parts.fragment:
        return f"must be scheme://host[:port] with no path, query or trailing slash: {origin!r}"
    return None


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
    # Hosted deployments (PATCHPROOF_HOSTED=1) must meet the stricter checks in problems().
    hosted: bool = False
    data_dir_set: bool = False
    access_token_hashes: tuple[str, ...] = ()
    cors_origins: tuple[str, ...] = ()

    @property
    def model_configured(self) -> bool:
        return bool(self.api_key)

    @property
    def data_dir(self) -> Path:
        return self.db_path.parent

    @property
    def instance_id(self) -> str:
        """Stable per data directory: identifies the sandbox containers this server instance starts."""
        return hashlib.sha256(str(self.db_path.resolve()).encode("utf-8")).hexdigest()[:12]

    def problems(self) -> list[str]:
        """Every reason this configuration must not be served. Empty means it is acceptable."""
        found = []
        if self.sandbox_backend not in SANDBOX_BACKENDS:
            found.append(f"PATCHPROOF_SANDBOX must be one of {', '.join(SANDBOX_BACKENDS)}, "
                         f"not {self.sandbox_backend!r}.")
        found += [f"PATCHPROOF_CORS_ORIGINS: {p}" for o in self.cors_origins if (p := origin_problem(o))]
        bad_hashes = [h for h in self.access_token_hashes if not _TOKEN_HASH.fullmatch(h)]
        if bad_hashes:
            found.append("PATCHPROOF_ACCESS_TOKEN_HASHES must hold SHA-256 hex digests "
                         "(create them with `python -m patchproof.access new`).")
        if self.hosted:
            if not self.access_token_hashes:
                found.append("PATCHPROOF_HOSTED=1 requires PATCHPROOF_ACCESS_TOKEN_HASHES: "
                             "a hosted API must not be open to anyone.")
            if self.sandbox_backend != "docker":
                found.append("PATCHPROOF_HOSTED=1 requires PATCHPROOF_SANDBOX=docker.")
            if self.allow_unsandboxed:
                found.append("PATCHPROOF_HOSTED=1 forbids PATCHPROOF_ALLOW_UNSANDBOXED=1.")
            if not self.data_dir_set:
                found.append("PATCHPROOF_HOSTED=1 requires PATCHPROOF_DATA_DIR on persistent storage.")
        return found


def load_settings() -> Settings:
    _load_dotenv(BACKEND_DIR / ".env")
    _load_dotenv(REPO_ROOT / ".env")
    data_dir_env = os.environ.get("PATCHPROOF_DATA_DIR", "").strip()
    data_dir = Path(data_dir_env) if data_dir_env else BACKEND_DIR / ".data"
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
        hosted=_bool("PATCHPROOF_HOSTED", False),
        data_dir_set=bool(data_dir_env),
        access_token_hashes=tuple(h.lower() for h in _list("PATCHPROOF_ACCESS_TOKEN_HASHES")),
        cors_origins=tuple(o.lower() for o in _list("PATCHPROOF_CORS_ORIGINS")),
    )
