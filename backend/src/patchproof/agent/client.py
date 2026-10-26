"""BharatCode model API client (OpenAI-compatible Chat Completions).

Docs: https://bharatcode.ai/build  Base URL: https://bharatcode.ai/api/model/v1
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

import httpx

from ..config import DOCUMENTED_MODELS
from ..redact import redact

log = logging.getLogger("patchproof.bharatcode")

ErrorKind = Literal[
    "not_configured", "auth", "forbidden_model", "rate_limited", "unavailable", "timeout",
    "network", "bad_request", "malformed", "truncated", "unknown_model",
]

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_PROMPT_CHARS = 200_000


class BharatCodeError(Exception):
    def __init__(self, kind: ErrorKind, message: str, *, status: int | None = None,
                 request_id: str | None = None, partial_text: str | None = None, attempts: int = 1):
        self.kind = kind
        self.status = status
        self.request_id = request_id
        self.partial_text = partial_text
        self.attempts = attempts
        super().__init__(redact(message))

    def user_message(self) -> str:
        hints = {
            "not_configured": "Set BHARATCODE_API_KEY on the server to enable live diagnosis.",
            "auth": "The BharatCode API key was rejected. Check BHARATCODE_API_KEY.",
            "forbidden_model": "This account cannot use the requested model. Try qwen-3.8-27b.",
            "rate_limited": "BharatCode rate-limited the request. Wait and try again.",
            "unavailable": "BharatCode reported that the model is busy or unavailable. Try again shortly.",
            "timeout": "The model request timed out.",
            "network": "Could not reach the BharatCode API.",
            "truncated": "The model ran out of output budget before finishing its answer.",
            "malformed": "The model returned a response PatchProof could not parse.",
            "unknown_model": "The configured model is not in the BharatCode model catalog.",
            "bad_request": "BharatCode rejected the request as invalid.",
        }
        rid = f" (x-request-id: {self.request_id})" if self.request_id else ""
        return f"{hints.get(self.kind, 'Model request failed.')} {self}{rid}".strip()


@dataclass
class ChatResult:
    text: str
    model: str
    latency_ms: int
    finish_reason: str | None
    request_id: str | None
    usage: dict = field(default_factory=dict)
    attempts: int = 1


class BharatCodeClient:
    def __init__(
        self,
        api_key: str | None,
        base_url: str = "https://bharatcode.ai/api/model/v1",
        *,
        timeout_s: float = 180.0,
        max_retries: int = 2,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.max_retries = max(0, min(max_retries, 4))
        self._sleep = sleep
        self._http = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout_s, connect=10.0),
            transport=transport,
            headers={"User-Agent": "PatchProofAI/0.1"},
        )
        self._catalog: tuple[float, list[str], str] | None = None
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    def _headers(self) -> dict[str, str]:
        if not self._api_key:
            raise BharatCodeError("not_configured", "No API key configured.")
        return {"Authorization": f"Bearer {self._api_key}"}

    def _redact(self, text: str) -> str:
        return redact(text, (self._api_key or "",))

    # -- catalog ---------------------------------------------------------------

    def list_models(self, ttl_s: float = 300.0) -> tuple[list[str], str]:
        """Return (model_ids, source) where source is 'catalog' or 'documented'."""
        with self._lock:
            if self._catalog and time.monotonic() - self._catalog[0] < ttl_s:
                return self._catalog[1], self._catalog[2]
        try:
            resp = self._http.get("/models", headers=self._headers())
        except BharatCodeError:
            raise
        except httpx.HTTPError as exc:
            log.warning("model catalog unavailable: %s", self._redact(str(exc)))
            return list(DOCUMENTED_MODELS), "documented"
        if resp.status_code in (401, 403):
            raise BharatCodeError("auth", f"HTTP {resp.status_code} from /models",
                                  status=resp.status_code, request_id=resp.headers.get("x-request-id"))
        if resp.status_code != 200:
            return list(DOCUMENTED_MODELS), "documented"
        ids = _extract_model_ids(_safe_json(resp))
        if not ids:
            return list(DOCUMENTED_MODELS), "documented"
        with self._lock:
            self._catalog = (time.monotonic(), ids, "catalog")
        return ids, "catalog"

    def ensure_model(self, model: str) -> str:
        ids, source = self.list_models()
        if model not in ids:
            raise BharatCodeError("unknown_model", f"'{model}' not in {source} model list: {', '.join(ids)}")
        return source

    # -- generation ------------------------------------------------------------

    def chat(
        self,
        messages: list[dict],
        *,
        model: str,
        max_tokens: int = 16000,
        temperature: float = 0.1,
        reasoning_effort: str | None = None,
    ) -> ChatResult:
        size = sum(len(str(m.get("content", ""))) for m in messages)
        if size > MAX_PROMPT_CHARS:
            raise BharatCodeError("bad_request", f"Prompt is {size} characters; limit is {MAX_PROMPT_CHARS}.")
        body: dict = {
            "model": model,
            "messages": messages,
            "max_tokens": min(max_tokens, 32000),
            "temperature": temperature,
            "stream": False,
        }
        if reasoning_effort:
            body["reasoning_effort"] = reasoning_effort
        headers = self._headers()

        attempt = 0
        started = time.monotonic()
        while True:
            attempt += 1
            try:
                resp = self._http.post("/chat/completions", json=body, headers=headers)
            except httpx.ConnectTimeout as exc:
                err = BharatCodeError("timeout", f"Connect timeout: {exc}", attempts=attempt)
                if attempt <= self.max_retries:
                    self._backoff(attempt, None)
                    continue
                raise err from exc
            except httpx.TimeoutException as exc:
                # Read timeouts are not retried: resending a large prompt that already took the
                # whole budget is unlikely to succeed and doubles token usage.
                raise BharatCodeError("timeout", f"No response within the timeout: {type(exc).__name__}",
                                      attempts=attempt) from exc
            except httpx.HTTPError as exc:
                err = BharatCodeError("network", f"{type(exc).__name__}: {self._redact(str(exc))}", attempts=attempt)
                if attempt <= self.max_retries:
                    self._backoff(attempt, None)
                    continue
                raise err from exc

            rid = resp.headers.get("x-request-id")
            if resp.status_code == 200:
                return self._parse_chat(resp, model, started, rid, attempt)
            detail = self._redact(_error_detail(resp))
            if resp.status_code == 401:
                raise BharatCodeError("auth", f"HTTP 401: {detail}", status=401, request_id=rid, attempts=attempt)
            if resp.status_code == 403:
                raise BharatCodeError("forbidden_model", f"HTTP 403: {detail}", status=403, request_id=rid, attempts=attempt)
            if resp.status_code in _RETRYABLE_STATUS and attempt <= self.max_retries:
                log.info("BharatCode HTTP %s (request %s); retrying", resp.status_code, rid)
                self._backoff(attempt, resp.headers.get("retry-after"))
                continue
            kind: ErrorKind = (
                "rate_limited" if resp.status_code == 429
                else "unavailable" if resp.status_code >= 500
                else "bad_request"
            )
            raise BharatCodeError(kind, f"HTTP {resp.status_code}: {detail}", status=resp.status_code,
                                  request_id=rid, attempts=attempt)

    def _backoff(self, attempt: int, retry_after: str | None) -> None:
        delay = min(2.0 * (2 ** (attempt - 1)), 10.0)
        if retry_after:
            try:
                delay = min(max(float(retry_after), 0.5), 15.0)
            except ValueError:
                pass
        self._sleep(delay)

    def _parse_chat(self, resp: httpx.Response, model: str, started: float, rid: str | None,
                    attempt: int) -> ChatResult:
        data = _safe_json(resp)
        try:
            choice = data["choices"][0]
            message = choice.get("message") or {}
            text = message.get("content")
            finish = choice.get("finish_reason")
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise BharatCodeError("malformed", "Response has no choices[0].message.", request_id=rid,
                                  attempts=attempt) from exc
        if isinstance(text, list):  # content parts
            text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
        text = text or ""
        if finish == "length":
            raise BharatCodeError("truncated", "finish_reason=length", request_id=rid,
                                  partial_text=text, attempts=attempt)
        if not text.strip():
            raise BharatCodeError("malformed", "The model returned empty content.", request_id=rid, attempts=attempt)
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        return ChatResult(
            text=text,
            model=str(data.get("model") or model),
            latency_ms=int((time.monotonic() - started) * 1000),
            finish_reason=finish,
            request_id=rid,
            usage=usage,
            attempts=attempt,
        )


def _safe_json(resp: httpx.Response):
    try:
        return resp.json()
    except ValueError as exc:
        raise BharatCodeError("malformed", f"Non-JSON response (HTTP {resp.status_code}).",
                              status=resp.status_code, request_id=resp.headers.get("x-request-id")) from exc


def _error_detail(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:300]
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err)[:300]
        if err:
            return str(err)[:300]
        return str(data.get("message") or data)[:300]
    return str(data)[:300]


def _extract_model_ids(data) -> list[str]:
    items = data
    if isinstance(data, dict):
        items = data.get("data") or data.get("models") or []
    ids: list[str] = []
    if isinstance(items, list):
        for item in items:
            if isinstance(item, str):
                ids.append(item)
            elif isinstance(item, dict) and isinstance(item.get("id"), str):
                ids.append(item["id"])
    return ids
