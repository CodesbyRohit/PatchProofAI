import json

import httpx
import pytest

from patchproof.agent.client import BharatCodeClient, BharatCodeError

KEY = "bc_live_supersecretkey123"


def ok_body(content="hello", finish="stop"):
    return {"model": "qwen-3.8-27b", "choices": [{"message": {"content": content}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}


def client_for(handler, **kw):
    sleeps = []
    c = BharatCodeClient(KEY, transport=httpx.MockTransport(handler), sleep=sleeps.append, **kw)
    return c, sleeps


def test_success_sends_bearer_and_parses_usage():
    seen = {}

    def handler(req: httpx.Request):
        seen["auth"] = req.headers["authorization"]
        seen["body"] = json.loads(req.content)
        seen["url"] = str(req.url)
        return httpx.Response(200, json=ok_body(), headers={"x-request-id": "rid-1"})

    c, _ = client_for(handler)
    res = c.chat([{"role": "user", "content": "hi"}], model="qwen-3.8-27b", max_tokens=100)
    assert seen["auth"] == f"Bearer {KEY}"
    assert seen["url"] == "https://bharatcode.ai/api/model/v1/chat/completions"
    assert seen["body"]["model"] == "qwen-3.8-27b" and seen["body"]["max_tokens"] == 100
    assert "reasoning_effort" not in seen["body"]
    assert res.text == "hello" and res.request_id == "rid-1" and res.usage["total_tokens"] == 5


def test_not_configured():
    c = BharatCodeClient(None)
    with pytest.raises(BharatCodeError) as info:
        c.chat([{"role": "user", "content": "x"}], model="m")
    assert info.value.kind == "not_configured"


def test_auth_failure_not_retried_and_key_redacted():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(401, json={"error": {"message": f"invalid key {KEY}"}})

    c, sleeps = client_for(handler)
    with pytest.raises(BharatCodeError) as info:
        c.chat([{"role": "user", "content": "x"}], model="m")
    assert info.value.kind == "auth" and len(calls) == 1 and not sleeps
    assert KEY not in str(info.value) and KEY not in info.value.user_message()


def test_forbidden_model():
    c, _ = client_for(lambda r: httpx.Response(403, json={"error": "Pro only"}))
    with pytest.raises(BharatCodeError) as info:
        c.chat([{"role": "user", "content": "x"}], model="deepseek-v4.1-flash")
    assert info.value.kind == "forbidden_model"


def test_rate_limit_retried_with_retry_after_then_succeeds():
    responses = iter([httpx.Response(429, headers={"retry-after": "3"}), httpx.Response(200, json=ok_body())])
    c, sleeps = client_for(lambda r: next(responses))
    res = c.chat([{"role": "user", "content": "x"}], model="m")
    assert res.text == "hello" and res.attempts == 2 and sleeps == [3.0]


def test_busy_model_retries_are_bounded():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(503, json={"error": "busy"}, headers={"x-request-id": "rid-9"})

    c, sleeps = client_for(handler, max_retries=2)
    with pytest.raises(BharatCodeError) as info:
        c.chat([{"role": "user", "content": "x"}], model="m")
    assert info.value.kind == "unavailable" and len(calls) == 3 and len(sleeps) == 2
    assert info.value.request_id == "rid-9" and "rid-9" in info.value.user_message()


def test_bad_request_not_retried():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(400, json={"error": {"message": "store:true unsupported"}})

    c, _ = client_for(handler)
    with pytest.raises(BharatCodeError) as info:
        c.chat([{"role": "user", "content": "x"}], model="m")
    assert info.value.kind == "bad_request" and len(calls) == 1


def test_read_timeout_not_retried():
    calls = []

    def handler(req):
        calls.append(1)
        raise httpx.ReadTimeout("slow", request=req)

    c, _ = client_for(handler)
    with pytest.raises(BharatCodeError) as info:
        c.chat([{"role": "user", "content": "x"}], model="m")
    assert info.value.kind == "timeout" and len(calls) == 1


def test_connect_errors_retried():
    calls = []

    def handler(req):
        calls.append(1)
        raise httpx.ConnectError("refused", request=req)

    c, sleeps = client_for(handler, max_retries=1)
    with pytest.raises(BharatCodeError) as info:
        c.chat([{"role": "user", "content": "x"}], model="m")
    assert info.value.kind == "network" and len(calls) == 2


@pytest.mark.parametrize("response,kind", [
    (httpx.Response(200, text="<html>gateway</html>"), "malformed"),
    (httpx.Response(200, json={"choices": []}), "malformed"),
    (httpx.Response(200, json={"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]}), "malformed"),
])
def test_malformed_responses(response, kind):
    c, _ = client_for(lambda r: response)
    with pytest.raises(BharatCodeError) as info:
        c.chat([{"role": "user", "content": "x"}], model="m")
    assert info.value.kind == kind


def test_truncated_response_keeps_partial_text():
    c, _ = client_for(lambda r: httpx.Response(200, json=ok_body('{"summary": "half', finish="length")))
    with pytest.raises(BharatCodeError) as info:
        c.chat([{"role": "user", "content": "x"}], model="m")
    assert info.value.kind == "truncated" and info.value.partial_text == '{"summary": "half'


def test_oversized_prompt_refused_locally():
    c, _ = client_for(lambda r: pytest.fail("must not send"))
    with pytest.raises(BharatCodeError) as info:
        c.chat([{"role": "user", "content": "x" * 300_000}], model="m")
    assert info.value.kind == "bad_request"


@pytest.mark.parametrize("payload", [
    {"data": [{"id": "qwen-3.8-27b"}, {"id": "other"}]},
    ["qwen-3.8-27b", "other"],
    {"models": [{"id": "qwen-3.8-27b"}]},
])
def test_model_catalog_formats(payload):
    c, _ = client_for(lambda r: httpx.Response(200, json=payload))
    ids, source = c.list_models()
    assert "qwen-3.8-27b" in ids and source == "catalog"
    assert c.ensure_model("qwen-3.8-27b") == "catalog"
    with pytest.raises(BharatCodeError) as info:
        c.ensure_model("gpt-unknown")
    assert info.value.kind == "unknown_model"


def test_model_catalog_falls_back_to_documented_list():
    c, _ = client_for(lambda r: httpx.Response(500))
    ids, source = c.list_models()
    assert source == "documented" and "qwen-3.8-27b" in ids
