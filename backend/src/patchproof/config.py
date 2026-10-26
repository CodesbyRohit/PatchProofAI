"""Runtime configuration, read from environment variables (and an optional backend/.env file)."""

from __future__ import annotations

import base64
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


def _optional_float(name: str) -> float | None:
    try:
        value = os.environ.get(name, "").strip()
        return float(value) if value else None
    except ValueError:
        return None


def _list(name: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in os.environ.get(name, "").split(",") if item.strip())


def _valid_fernet_key(key: str) -> bool:
    try:
        return len(base64.urlsafe_b64decode(key.encode("ascii"))) == 32
    except (ValueError, UnicodeEncodeError):
        return False


def _url_problem(url: str, *, https: bool) -> str | None:
    parts = urlsplit(url)
    if parts.scheme not in (("https",) if https else ("https", "http")) or not parts.hostname:
        return f"must be an absolute {'https' if https else 'http(s)'} URL: {url!r}"
    if parts.username or parts.password or parts.query or parts.fragment:
        return f"must not contain credentials, a query or a fragment: {url!r}"
    return None


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
    # GitHub App sign-in and repository access (all optional; see deploy/README.md).
    github_client_id: str | None = None
    github_client_secret: str | None = None
    github_app_slug: str | None = None
    github_callback_url: str | None = None
    app_url: str | None = None  # where the browser returns after signing in; same origin when unset
    secret_key: str | None = None  # Fernet key: encrypts stored GitHub tokens
    github_allowed_logins: tuple[str, ...] = ()
    github_pr_writes: bool = False
    cookie_secure: bool = False
    session_ttl_s: int = 7 * 24 * 3600
    repo_max_archive_bytes: int = 25 * 1024 * 1024
    # Usage and product analytics (see usage.py). Prices are the operator's real costs; unset means unmeasured.
    free_monthly_runs: int = 30  # 0: no monthly limit on the free plan
    run_retention_days: int = 90  # finished runs (logs, diffs) older than this are deleted; 0 keeps them
    analytics: bool = True
    cost_per_sandbox_hour: float | None = None
    cost_per_1k_tokens: float | None = None

    @property
    def model_configured(self) -> bool:
        return bool(self.api_key)

    @property
    def github_configured(self) -> bool:
        return bool(self.github_client_id and self.github_client_secret)

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
        found += self._github_problems()
        if self.hosted:
            if not self.access_token_hashes and not (self.github_configured and self.github_allowed_logins):
                found.append("PATCHPROOF_HOSTED=1 requires access control: PATCHPROOF_ACCESS_TOKEN_HASHES, or GitHub "
                             "sign-in limited by PATCHPROOF_GITHUB_ALLOWED_LOGINS. A hosted API must not be open "
                             "to anyone.")
            if self.sandbox_backend != "docker":
                found.append("PATCHPROOF_HOSTED=1 requires PATCHPROOF_SANDBOX=docker.")
            if self.allow_unsandboxed:
                found.append("PATCHPROOF_HOSTED=1 forbids PATCHPROOF_ALLOW_UNSANDBOXED=1.")
            if not self.data_dir_set:
                found.append("PATCHPROOF_HOSTED=1 requires PATCHPROOF_DATA_DIR on persistent storage.")
        return found

    def _github_problems(self) -> list[str]:
        found = []
        parts = {"GITHUB_APP_CLIENT_ID": self.github_client_id, "GITHUB_APP_CLIENT_SECRET": self.github_client_secret,
                 "PATCHPROOF_GITHUB_CALLBACK_URL": self.github_callback_url}
        if any(parts.values()) and not all(parts.values()):
            missing = ", ".join(k for k, v in parts.items() if not v)
            found.append(f"GitHub sign-in is partly configured; also set {missing}.")
        if self.github_pr_writes and not self.github_configured:
            found.append("PATCHPROOF_GITHUB_PR_WRITES=1 requires GitHub sign-in to be configured.")
        if not self.github_configured:
            return found
        if not self.secret_key or not _valid_fernet_key(self.secret_key):
            found.append("GitHub sign-in requires PATCHPROOF_SECRET_KEY: a Fernet key that encrypts stored GitHub "
                         "tokens (create one with `python -m patchproof.accounts new-key`).")
        for name, url in (("PATCHPROOF_GITHUB_CALLBACK_URL", self.github_callback_url), ("PATCHPROOF_APP_URL", self.app_url)):
            if url and (problem := _url_problem(url, https=self.hosted)):
                found.append(f"{name} {problem}")
        if self.hosted and not self.cookie_secure:
            found.append("PATCHPROOF_HOSTED=1 with GitHub sign-in requires PATCHPROOF_COOKIE_SECURE=1 (HTTPS only).")
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
        github_client_id=os.environ.get("GITHUB_APP_CLIENT_ID") or None,
        github_client_secret=os.environ.get("GITHUB_APP_CLIENT_SECRET") or None,
        github_app_slug=os.environ.get("GITHUB_APP_SLUG") or None,
        github_callback_url=os.environ.get("PATCHPROOF_GITHUB_CALLBACK_URL") or None,
        app_url=(os.environ.get("PATCHPROOF_APP_URL") or "").rstrip("/") or None,
        secret_key=os.environ.get("PATCHPROOF_SECRET_KEY") or None,
        github_allowed_logins=tuple(login.lower() for login in _list("PATCHPROOF_GITHUB_ALLOWED_LOGINS")),
        github_pr_writes=_bool("PATCHPROOF_GITHUB_PR_WRITES", False),
        cookie_secure=_bool("PATCHPROOF_COOKIE_SECURE", _bool("PATCHPROOF_HOSTED", False)),
        repo_max_archive_bytes=max(1, _int("PATCHPROOF_REPO_MAX_ARCHIVE_MB", 25)) * 1024 * 1024,
        free_monthly_runs=max(0, _int("PATCHPROOF_FREE_MONTHLY_RUNS", 30)),
        run_retention_days=max(0, _int("PATCHPROOF_RUN_RETENTION_DAYS", 90)),
        analytics=_bool("PATCHPROOF_ANALYTICS", True),
        cost_per_sandbox_hour=_optional_float("PATCHPROOF_COST_PER_SANDBOX_HOUR"),
        cost_per_1k_tokens=_optional_float("PATCHPROOF_COST_PER_1K_MODEL_TOKENS"),
        cors_origins=tuple(o.lower() for o in _list("PATCHPROOF_CORS_ORIGINS")),
    )
