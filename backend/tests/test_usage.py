"""Usage accounting, quotas, plans and first-party events."""

import dataclasses
import sqlite3
import time
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from patchproof.access import hash_token
from patchproof.app import create_app
from patchproof.store import Store
from patchproof.usage import Usage, period_end, report
from patchproof.verification.engine import Engine

TOKEN = "ppa_usage-test-token-0123456789abcdef"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
OWNER = f"token:{hash_token(TOKEN)[:16]}"
SECRET_ISSUE = "customer data: ACME-4421 crashes on refund"
DIFF = "--- a/m.py\n+++ b/m.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
DEMO = {"example_id": "pagination-off-by-one", "patch_source": "scripted", "scripted_patch_id": "minimal-fix"}


def app_with(settings, **changes):
    s = dataclasses.replace(settings, access_token_hashes=(hash_token(TOKEN),), **changes)
    return create_app(s, engine=Engine(s, None, docker_check=lambda: (False, "off"))), s


def wait(c, run_id, headers=AUTH):
    for _ in range(300):
        run = c.get(f"/api/runs/{run_id}", headers=headers).json()
        if run["state"] in ("finished", "error"):
            return run
        time.sleep(0.1)
    raise AssertionError("run did not finish")


def user_run(c):
    project = c.post("/api/projects/inline", headers=AUTH,
                     json={"files": [{"path": "m.py", "content": "x = 1\n"}, {"path": "test_m.py", "content": "def test_x():\n    pass\n"}]}).json()
    return c.post("/api/runs", headers=AUTH, json={"project_id": project["id"], "patch_source": "user",
                                                   "user_patch": DIFF, "issue": SECRET_ISSUE})


def test_monthly_quota_is_enforced_server_side_and_demo_runs_are_free(settings):
    app, _ = app_with(settings, free_monthly_runs=2, runs_per_minute=100)
    with TestClient(app) as c:
        assert user_run(c).status_code == 202
        assert user_run(c).status_code == 202
        refused = user_run(c)
        assert refused.status_code == 429 and refused.json()["code"] == "quota.exceeded"
        assert "resets on" in refused.json()["detail"]
        assert c.post("/api/runs", headers=AUTH, json=DEMO).status_code == 202  # the bundled demo stays free
        usage = c.get("/api/account/usage", headers=AUTH).json()
        assert (usage["plan"], usage["runs_used"], usage["runs_limit"]) == ("free", 2, 2)
        assert usage["billing"] == {"enabled": False}
        app.state.ctx.store.set_plan(OWNER, "member")  # an operator decision, never the browser's
        assert user_run(c).status_code == 202
        assert c.get("/api/account/usage", headers=AUTH).json()["runs_limit"] is None


def test_the_open_local_principal_has_no_quota(settings):
    s = dataclasses.replace(settings, free_monthly_runs=1, runs_per_minute=100)
    with TestClient(create_app(s, engine=Engine(s, None, docker_check=lambda: (False, "off")))) as c:
        project = c.post("/api/projects/inline", json={"files": [{"path": "m.py", "content": "x = 1\n"}]}).json()
        for _ in range(3):
            body = {"project_id": project["id"], "patch_source": "user", "user_patch": DIFF}
            assert c.post("/api/runs", json=body).status_code == 202
        assert c.get("/api/account/usage").json()["runs_limit"] is None


def test_usage_records_cost_but_never_content(settings):
    app, s = app_with(settings)
    with TestClient(app) as c:
        run = wait(c, c.post("/api/runs", headers=AUTH, json={**DEMO, "issue": SECRET_ISSUE}).json()["id"])
        assert run["state"] == "finished"
        c.get(f"/api/runs/{run['id']}/patch.diff", headers=AUTH)
    db = sqlite3.connect(s.db_path)
    row = db.execute("SELECT owner, origin, state, verdict, sandbox_s, counts_toward_quota FROM usage").fetchone()
    assert row[:4] == (OWNER, "example", "finished", "VERIFIED_FOR_TESTED_CASES")
    assert row[4] > 0 and row[5] == 0
    dump = "\n".join(str(r) for t in ("usage", "events") for r in db.execute(f"SELECT * FROM {t}"))  # noqa: S608
    assert SECRET_ISSUE not in dump  # no issue text, diffs or file content in accounting or analytics
    assert "ceiling" not in dump
    # Usage needs the owner to count quotas; analytics only ever holds a keyed hash of it.
    events = "\n".join(str(r) for r in db.execute("SELECT * FROM events"))
    assert OWNER not in events
    names = [n for (n,) in db.execute("SELECT name FROM events ORDER BY id")]
    assert names == ["run_submitted", "run_completed", "patch_downloaded"]


def test_events_are_allowlisted_and_pseudonymous(settings):
    store = Store(settings.db_path)
    usage = Usage(settings, store)
    assert usage.event("run_completed", "user:7", verdict="VERIFIED_FOR_TESTED_CASES", source="secret.py")
    assert not usage.event("arbitrary_event", "user:7")
    assert usage.event("run_completed", "user:7", verdict="whatever I like")
    rows = store.events_since(0)
    assert [r[0] for r in rows] == ["run_completed", "run_completed"]
    assert rows[0][3] == '{"verdict": "VERIFIED_FOR_TESTED_CASES"}' and rows[1][3] == "{}"
    assert rows[0][1] and rows[0][1] != "user:7" and rows[0][1] == usage.subject("user:7")
    assert usage.subject("local") is None
    off = Usage(dataclasses.replace(settings, analytics=False), store)
    assert not off.event("landing_viewed", None)


def test_browser_may_only_send_its_two_events(settings):
    app, s = app_with(settings)
    with TestClient(app) as c:
        assert c.post("/api/events", json={"name": "landing_viewed"}).status_code == 204  # public, no token
        assert c.post("/api/events", json={"name": "pull_request_created"}).status_code == 204  # ignored
        assert c.post("/api/events", json={"name": "repository_selected", "visibility": "private"},
                      headers=AUTH).status_code == 204
        for _ in range(40):  # rate limited per client address
            c.post("/api/events", json={"name": "landing_viewed"})
    names = [r[0] for r in Store(s.db_path).events_since(0)]
    assert names.count("pull_request_created") == 0
    assert names.count("repository_selected") == 1
    assert names.count("landing_viewed") <= 30


def test_operator_report_measures_cost_only_with_real_prices(settings):
    app, s = app_with(settings)
    with TestClient(app) as c:
        wait(c, c.post("/api/runs", headers=AUTH, json=DEMO).json()["id"])
    store = Store(s.db_path)
    unpriced = report(s, store, 30)
    assert "run_submitted" in unpriced and "1 verified" in unpriced and "Cost per run: not measured" in unpriced
    priced = report(dataclasses.replace(s, cost_per_sandbox_hour=0.10, cost_per_1k_tokens=0.002), store, 30)
    assert "per run" in priced and "per verified run" in priced


def test_quota_period_resets_at_the_next_utc_month():
    december = datetime(2026, 12, 15, tzinfo=UTC).timestamp()
    assert datetime.fromtimestamp(period_end(december), UTC) == datetime(2027, 1, 1, tzinfo=UTC)
    october = datetime(2026, 10, 31, 23, 59, tzinfo=UTC).timestamp()
    assert datetime.fromtimestamp(period_end(october), UTC) == datetime(2026, 11, 1, tzinfo=UTC)


def test_parallel_requests_cannot_exceed_the_monthly_quota(settings):
    from concurrent.futures import ThreadPoolExecutor

    app, _ = app_with(settings, free_monthly_runs=3, runs_per_minute=1000)
    with TestClient(app) as c:
        project = c.post("/api/projects/inline", headers=AUTH,
                         json={"files": [{"path": "m.py", "content": "x = 1\n"}]}).json()
        body = {"project_id": project["id"], "patch_source": "user", "user_patch": DIFF}
        with ThreadPoolExecutor(max_workers=12) as pool:
            codes = list(pool.map(lambda _: c.post("/api/runs", headers=AUTH, json=body).status_code, range(12)))
        assert codes.count(202) == 3 and codes.count(429) == 9
        assert len(c.get("/api/runs", headers=AUTH).json()) == 3  # refused requests stored nothing


def test_runs_that_error_before_a_verdict_do_not_use_up_the_quota(settings):
    app, s = app_with(settings, free_monthly_runs=1, runs_per_minute=100)
    store = app.state.ctx.store
    with TestClient(app) as c:
        first = user_run(c).json()
        assert wait(c, first["id"])["state"] in ("finished", "error")
        assert user_run(c).status_code == 429  # a finished run (here: blocked, no sandbox) counts
        sqlite3.connect(s.db_path).execute("UPDATE usage SET state = 'error'").connection.commit()
        assert user_run(c).status_code == 202  # an errored run is refunded
    assert store.counted_runs(OWNER, time.strftime("%Y-%m", time.gmtime())) >= 0


def test_runs_recovered_after_a_restart_are_refunded(settings):
    from patchproof.schemas import PatchSource, Run, RunRequest

    store = Store(settings.db_path)
    run = Run(id="run_stale0000001", created_at=time.time(), state="running", owner=OWNER, project_id="p",
              project_name="p", patch_source=PatchSource.user, request=RunRequest(patch_source=PatchSource.user))
    store.save_run(run)
    usage = Usage(dataclasses.replace(settings, free_monthly_runs=1), store)
    usage.run_started(run, "upload", private=False)
    assert store.counted_runs(OWNER, run_period := __import__("patchproof.usage", fromlist=["x"]).period_of(run.created_at)) == 1
    store.recover_after_restart()
    assert store.counted_runs(OWNER, run_period) == 0
