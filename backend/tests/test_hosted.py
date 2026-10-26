"""Hosted-deployment controls: config validation, access tokens, CORS, body limits, readiness, idempotency."""

import dataclasses
import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from patchproof import access
from patchproof.app import MAX_REQUEST_BYTES, create_app
from patchproof.config import ConfigError, origin_problem
from patchproof.sandbox import DockerSandbox
from patchproof.store import Store
from patchproof.verification.engine import Engine

TOKEN = "ppa_test-token-0123456789abcdefghijklmnopqrstuv"
ORIGIN = "https://patch-proof-ai.vercel.app"
RUN = {"example_id": "pagination-off-by-one", "patch_source": "scripted", "scripted_patch_id": "minimal-fix"}


def no_docker(settings):
    return Engine(settings, None, docker_check=lambda: (False, "Docker disabled in tests."))


def hosted(settings, **changes):
    return dataclasses.replace(settings, hosted=True, sandbox_backend="docker", data_dir_set=True,
                               access_token_hashes=(access.hash_token(TOKEN),), cors_origins=(ORIGIN,), **changes)


@pytest.fixture
def secured(settings):
    s = dataclasses.replace(settings, access_token_hashes=(access.hash_token(TOKEN),), cors_origins=(ORIGIN,))
    with TestClient(create_app(s, engine=no_docker(s))) as client:
        yield client


def auth(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


# -- configuration -------------------------------------------------------------------------------------


def test_default_development_configuration_is_accepted(settings):
    assert settings.problems() == []


def test_hosted_configuration_is_accepted(settings):
    assert hosted(settings).problems() == []


@pytest.mark.parametrize(("changes", "expected"), [
    ({"sandbox_backend": "dokcer"}, "PATCHPROOF_SANDBOX must be one of"),
    ({"access_token_hashes": ("not-a-hash",)}, "PATCHPROOF_ACCESS_TOKEN_HASHES must hold SHA-256"),
    ({"cors_origins": ("*",)}, "wildcards are not allowed"),
    ({"cors_origins": ("https://app.example.com/",)}, "no path"),
    ({"cors_origins": ("app.example.com",)}, "http:// or https://"),
    ({"cors_origins": ("ftp://app.example.com",)}, "http:// or https://"),
    ({"cors_origins": ("https://app.example.com:99999",)}, "not a valid origin"),
])
def test_invalid_configuration_is_rejected_at_startup(settings, changes, expected):
    bad = dataclasses.replace(settings, **changes)
    assert any(expected in p for p in bad.problems())
    with pytest.raises(ConfigError, match="will not start"):
        create_app(bad, engine=no_docker(bad))


@pytest.mark.parametrize(("changes", "expected"), [
    ({"access_token_hashes": ()}, "requires access control"),
    ({"sandbox_backend": "auto"}, "requires PATCHPROOF_SANDBOX=docker"),
    ({"sandbox_backend": "local"}, "requires PATCHPROOF_SANDBOX=docker"),
    ({"allow_unsandboxed": True}, "forbids PATCHPROOF_ALLOW_UNSANDBOXED=1"),
    ({"data_dir_set": False}, "requires PATCHPROOF_DATA_DIR"),
])
def test_hosted_mode_fails_closed(settings, changes, expected):
    bad = dataclasses.replace(hosted(settings), **changes)
    assert any(expected in p for p in bad.problems())
    with pytest.raises(ConfigError, match=expected):
        create_app(bad, engine=no_docker(bad))


def test_valid_origins():
    for origin in ("https://patch-proof-ai.vercel.app", "http://localhost:5173", "http://127.0.0.1:4173"):
        assert origin_problem(origin) is None, origin


def test_hosted_mode_allows_one_process_per_data_directory(settings):
    s = hosted(settings)
    first = create_app(s, engine=no_docker(s))
    with pytest.raises(ConfigError, match="Another PatchProof process"):
        create_app(s, engine=no_docker(s))
    first.state.process_lock.close()


# -- access tokens -------------------------------------------------------------------------------------


def test_token_helpers():
    token = access.new_token()
    assert token.startswith("ppa_") and len(token) > 40
    assert access.token_allowed(token, [access.hash_token(token)])
    assert not access.token_allowed(token + "x", [access.hash_token(token)])
    assert not access.token_allowed(None, [access.hash_token(token)])
    assert access.bearer_token("Bearer abc") == "abc"
    assert access.bearer_token("bearer  abc ") == "abc"
    assert access.bearer_token("Basic abc") is None
    assert access.bearer_token("Bearer ") is None


def test_token_cli(capsys, monkeypatch):
    assert access.main(["access", "new"]) == 0
    out = capsys.readouterr().out
    token = out.split("shown once): ")[1].split()[0]
    assert access.hash_token(token) in out
    monkeypatch.setattr("sys.stdin", io.StringIO(token + "\n"))
    assert access.main(["access", "hash"]) == 0
    assert capsys.readouterr().out.strip() == access.hash_token(token)


@pytest.mark.parametrize("path", ["/api/status", "/api/examples", "/api/runs", "/api/runs/run_x",
                                  "/api/runs/run_x/report.md", "/api/projects/example:x"])
def test_api_requires_a_token(secured, path):
    r = secured.get(path)
    assert r.status_code == 401
    assert r.json() == {"detail": "This PatchProof instance requires an access token."}
    assert r.headers["www-authenticate"].startswith("Bearer")
    wrong = secured.get(path, headers=auth("ppa_wrong"))
    assert wrong.status_code == 401
    assert wrong.json()["detail"] == "The access token is not valid."


def test_writes_require_a_token(secured):
    assert secured.post("/api/runs", json=RUN).status_code == 401
    assert secured.post("/api/projects/inline", json={"files": [{"path": "m.py", "content": "x=1\n"}]}).status_code == 401
    assert secured.post("/api/projects/upload", files={"file": ("p.zip", b"PK")}).status_code == 401


def test_probes_are_public_and_valid_token_works(secured):
    assert secured.get("/api/health").json()["ok"] is True
    assert secured.get("/api/ready").status_code == 200
    assert secured.get("/api/status", headers=auth()).status_code == 200
    r = secured.post("/api/runs", json=RUN, headers=auth())
    assert r.status_code == 202
    assert secured.get(f"/api/runs/{r.json()['id']}", headers=auth()).status_code == 200


def test_open_api_without_configured_tokens(settings):
    with TestClient(create_app(settings, engine=no_docker(settings))) as c:
        assert c.get("/api/status").status_code == 200


# -- CORS --------------------------------------------------------------------------------------------


def test_cors_preflight_allows_only_the_configured_origin(secured):
    pre = {"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "authorization,content-type"}
    ok = secured.options("/api/runs", headers={"Origin": ORIGIN, **pre})
    assert ok.status_code == 200
    assert ok.headers["access-control-allow-origin"] == ORIGIN
    assert "authorization" in ok.headers["access-control-allow-headers"].lower()
    # Credentials are allowed (a same-site frontend sends the session cookie) only for this exact origin.
    assert ok.headers["access-control-allow-credentials"] == "true"
    assert ok.headers["access-control-allow-origin"] != "*"
    other = secured.options("/api/runs", headers={"Origin": "https://evil.example", **pre})
    assert other.status_code == 400
    assert "access-control-allow-origin" not in other.headers


def test_cors_headers_on_errors_and_downloads(secured):
    denied = secured.get("/api/status", headers={"Origin": ORIGIN})
    assert denied.status_code == 401
    assert denied.headers["access-control-allow-origin"] == ORIGIN  # the browser can read the 401
    assert "access-control-allow-origin" not in secured.get("/api/status", headers={"Origin": "https://evil.example"}).headers
    run = secured.post("/api/runs", json=RUN, headers=auth()).json()
    report = secured.get(f"/api/runs/{run['id']}/report.json", headers={**auth(), "Origin": ORIGIN})
    assert report.status_code == 200
    assert "content-disposition" in report.headers["access-control-expose-headers"].lower()


def test_no_cors_headers_without_configured_origins(settings):
    with TestClient(create_app(settings, engine=no_docker(settings))) as c:
        assert "access-control-allow-origin" not in c.get("/api/status", headers={"Origin": ORIGIN}).headers


# -- request size ------------------------------------------------------------------------------------


def test_oversized_body_is_rejected_by_declared_length(secured):
    r = secured.post("/api/projects/upload", headers={**auth(), "Content-Length": str(MAX_REQUEST_BYTES + 1)},
                     content=b"x" * 16)
    assert r.status_code == 413
    assert "too large" in r.json()["detail"]


def test_oversized_streamed_body_is_rejected(secured):
    """A valid multipart upload sent chunked (no Content-Length): the form parser reads it, the limit stops it."""
    boundary = "pp-boundary"

    def chunks():
        yield (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"big.zip\"\r\n"
               "Content-Type: application/zip\r\n\r\n").encode()
        for _ in range(MAX_REQUEST_BYTES // (1024 * 1024) + 2):
            yield b"x" * (1024 * 1024)
        yield f"\r\n--{boundary}--\r\n".encode()

    r = secured.post("/api/projects/upload", content=chunks(),
                     headers={**auth(), "Content-Type": f"multipart/form-data; boundary={boundary}"})
    assert "content-length" not in {k.lower() for k in r.request.headers}
    assert r.status_code == 413
    assert "too large" in r.json()["detail"]


def test_upload_under_the_limit_still_works(secured):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("m.py", "def f():\n    return 1\n")
        zf.writestr("tests/test_m.py", "from m import f\n\ndef test_f():\n    assert f() == 1\n")
    r = secured.post("/api/projects/upload", headers=auth(), files={"file": ("p.zip", buf.getvalue())})
    assert r.status_code == 200, r.text


# -- readiness ---------------------------------------------------------------------------------------


def test_ready_reports_missing_docker_when_docker_is_required(settings):
    s = dataclasses.replace(settings, sandbox_backend="docker")
    with TestClient(create_app(s, engine=no_docker(s))) as c:
        r = c.get("/api/ready")
        assert r.status_code == 503
        assert r.json() == {"ready": False, "checks": {"store": True, "docker_sandbox": False}}
        assert c.get("/api/health").status_code == 200  # liveness is independent of the sandbox


def test_ready_reports_unwritable_store(settings, monkeypatch):
    def broken(self):
        raise OSError("disk full")

    with TestClient(create_app(settings, engine=no_docker(settings))) as c:
        monkeypatch.setattr(Store, "ping", broken)
        r = c.get("/api/ready")
        assert r.status_code == 503
        assert r.json()["checks"]["store"] is False
        assert "disk full" not in r.text  # no internal details to clients


# -- duplicate submissions ---------------------------------------------------------------------------


def test_idempotency_key_prevents_duplicate_runs(settings):
    s = dataclasses.replace(settings, runs_per_minute=1)
    with TestClient(create_app(s, engine=no_docker(s))) as c:
        key = {"Idempotency-Key": "click-0123456789"}
        first = c.post("/api/runs", json=RUN, headers=key)
        again = c.post("/api/runs", json=RUN, headers=key)
        assert first.status_code == again.status_code == 202
        assert first.json()["id"] == again.json()["id"]  # a retry neither duplicates nor spends rate budget
        assert len(c.get("/api/runs").json()) == 1
        assert c.post("/api/runs", json=RUN, headers={"Idempotency-Key": "click-other-0001"}).status_code == 429
        assert c.post("/api/runs", json=RUN, headers={"Idempotency-Key": "bad key!"}).status_code == 422


# -- sandbox containers ------------------------------------------------------------------------------


def test_sandbox_containers_carry_the_instance_label(settings, tmp_path):
    cmd = DockerSandbox("img", instance=settings.instance_id).build_command("c", tmp_path, tmp_path, 5)
    assert cmd[cmd.index("--label") + 1] == f"patchproof.instance={settings.instance_id}"
    other = dataclasses.replace(settings, db_path=tmp_path / "other" / "db.sqlite3")
    assert other.instance_id != settings.instance_id
    assert "--label" not in DockerSandbox("img").build_command("c", tmp_path, tmp_path, 5)


def test_remove_orphans_targets_only_this_instance(monkeypatch):
    calls = []

    class Done:
        def __init__(self, out=""):
            self.returncode, self.stdout = 0, out

    def fake_run(cmd, **_):
        calls.append(cmd)
        return Done("abc123\ndef456\n") if cmd[:2] == ["docker", "ps"] else Done()

    monkeypatch.setattr("patchproof.sandbox.docker_runner.subprocess.run", fake_run)
    assert DockerSandbox("img", instance="inst1").remove_orphans() == 2
    assert calls[0] == ["docker", "ps", "-aq", "--filter", "label=patchproof.instance=inst1"]
    assert calls[1] == ["docker", "rm", "-f", "abc123", "def456"]
    calls.clear()
    assert DockerSandbox("img").remove_orphans() == 0
    assert calls == []
