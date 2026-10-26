import io
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from patchproof.app import create_app
from patchproof.verification.engine import Engine


@pytest.fixture
def api(settings):
    engine = Engine(settings, None, docker_check=lambda: (False, "Docker disabled in tests."))
    with TestClient(create_app(settings, engine=engine)) as client:
        yield client


def wait(api, run_id, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = api.get(f"/api/runs/{run_id}").json()
        if run["state"] in ("finished", "error"):
            return run
        time.sleep(0.2)
    raise AssertionError("run did not finish")


def test_status_reports_real_configuration(api):
    data = api.get("/api/status").json()
    assert data["model"]["configured"] is False
    assert data["sandbox"]["docker_available"] is False
    assert data["sandbox"]["untrusted_execution"] is False


def test_examples_listed(api):
    ids = {e["id"] for e in api.get("/api/examples").json()}
    assert {"pagination-off-by-one", "cart-shared-state", "report-missing-dependency"} <= ids


def test_scripted_example_run_end_to_end_and_downloads(api):
    resp = api.post("/api/runs", json={"example_id": "pagination-off-by-one", "patch_source": "scripted",
                                       "scripted_patch_id": "plausible-plus-one"})
    assert resp.status_code == 202
    run = wait(api, resp.json()["id"])
    assert run["verdict"]["status"] == "FAILED_VERIFICATION"
    assert any(c["category"] == "regression" for c in run["verdict"]["comparison"])
    diff = api.get(f"/api/runs/{run['id']}/patch.diff")
    assert diff.status_code == 200 and "+    return total_items // per_page + 1" in diff.text
    report = api.get(f"/api/runs/{run['id']}/report.md")
    assert "FAILED_VERIFICATION" in report.text and "Scripted example patch" in report.text
    assert api.get(f"/api/runs/{run['id']}/report.json").json()["id"] == run["id"]
    assert api.get("/api/runs").json()[0]["id"] == run["id"]


def test_uploaded_project_is_blocked_without_sandbox(api):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("m.py", "def f():\n    return 1\n")
        zf.writestr("tests/test_m.py", "from m import f\n\ndef test_f():\n    assert f() == 2\n")
    up = api.post("/api/projects/upload", files={"file": ("proj.zip", buf.getvalue(), "application/zip")})
    assert up.status_code == 200
    project = up.json()
    assert project["trusted"] is False and project["summary"]["test_files"] == ["tests/test_m.py"]
    run = wait(api, api.post("/api/runs", json={"project_id": project["id"], "patch_source": "ai"}).json()["id"])
    assert run["verdict"]["status"] == "BLOCKED_BY_SAFETY_POLICY"
    assert run["baseline_runs"] == []


def test_malicious_upload_rejected(api):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../../evil.py", "x = 1\n")
    resp = api.post("/api/projects/upload", files={"file": ("evil.zip", buf.getvalue(), "application/zip")})
    assert resp.status_code == 422 and "traversal" in resp.json()["detail"]
    resp = api.post("/api/projects/upload", files={"file": ("x.tar", b"abc", "application/x-tar")})
    assert resp.status_code == 422


def test_inline_project_and_validation(api):
    ok = api.post("/api/projects/inline", json={"files": [{"path": "a.py", "content": "x = 1\n"}]})
    assert ok.status_code == 200 and ok.json()["origin"] == "inline"
    bad = api.post("/api/projects/inline", json={"files": [{"path": "../a.py", "content": "x"}]})
    assert bad.status_code == 422


def test_scripted_patches_only_for_examples_and_unknown_ids(api):
    proj = api.post("/api/projects/inline", json={"files": [{"path": "a.py", "content": "x = 1\n"}]}).json()
    r = api.post("/api/runs", json={"project_id": proj["id"], "patch_source": "scripted", "scripted_patch_id": "minimal-fix"})
    assert r.status_code == 422
    assert api.post("/api/runs", json={"project_id": "nope"}).status_code == 404
    assert api.post("/api/runs", json={"example_id": "pagination-off-by-one", "patch_source": "user"}).status_code == 422
    assert api.get("/api/runs/run_missing").status_code == 404


def test_rate_limit(settings):
    import dataclasses
    s = dataclasses.replace(settings, runs_per_minute=1)
    engine = Engine(s, None, docker_check=lambda: (False, "off"))
    with TestClient(create_app(s, engine=engine)) as c:
        body = {"example_id": "pagination-off-by-one", "patch_source": "scripted", "scripted_patch_id": "minimal-fix"}
        assert c.post("/api/runs", json=body).status_code == 202
        assert c.post("/api/runs", json=body).status_code == 429
