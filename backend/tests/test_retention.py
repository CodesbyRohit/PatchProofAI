"""Retention, deletion, backup and restore, and secret-key rotation."""

import dataclasses
import sqlite3
import time

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from github_fake import FakeGitHub  # noqa: F401 - used through the fixtures below
from test_github import RUN_GITHUB, fake, gh_settings, make_app, sign_in, wait  # noqa: F401

from patchproof.schemas import PatchSource, Run, RunRequest
from patchproof.store import Store


def start_run(c, csrf):
    return wait(c, c.post("/api/github/runs", json=RUN_GITHUB, headers={"X-CSRF-Token": csrf}).json()["id"])


def stored(db_path, table):
    return sqlite3.connect(db_path).execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608


def test_owner_can_delete_a_finished_run_and_nobody_else_can(gh_settings, fake):  # noqa: F811
    app = make_app(gh_settings, fake)
    with TestClient(app) as alice, TestClient(app) as bob:
        a_csrf, b_csrf = sign_in(alice), sign_in(bob, "code-bob")
        run = start_run(alice, a_csrf)
        assert bob.delete(f"/api/runs/{run['id']}", headers={"X-CSRF-Token": b_csrf}).status_code == 404
        assert alice.delete(f"/api/runs/{run['id']}").status_code == 403  # no CSRF token
        assert alice.delete(f"/api/runs/{run['id']}", headers={"X-CSRF-Token": a_csrf}).status_code == 204
        assert alice.get(f"/api/runs/{run['id']}").status_code == 404
        assert alice.get(f"/api/projects/{run['project_id']}").status_code == 404  # its project copy is gone too


def test_a_run_in_progress_cannot_be_deleted(gh_settings, fake):  # noqa: F811
    app = make_app(gh_settings, fake)
    with TestClient(app) as c:
        csrf = sign_in(c)
        running = Run(id="run_inprogress01", created_at=time.time(), state="running", owner="user:1", project_id="p",
                      project_name="p", patch_source=PatchSource.user, request=RunRequest(patch_source=PatchSource.user))
        app.state.ctx.store.save_run(running)
        r = c.delete("/api/runs/run_inprogress01", headers={"X-CSRF-Token": csrf})
        assert r.status_code == 409 and "in progress" in r.json()["detail"]


def test_finished_runs_past_the_retention_period_are_deleted_at_startup(settings):
    store = Store(settings.db_path)
    now = time.time()

    def add(run_id, state, age_days):
        store.save_run(Run(id=run_id, created_at=now - age_days * 86400, state=state, project_id="example:x",
                           project_name="p", patch_source=PatchSource.user, request=RunRequest(patch_source=PatchSource.user)))

    add("run_old_finished", "finished", 100)
    add("run_old_queued00", "queued", 100)
    add("run_recent_done0", "finished", 5)
    assert store.purge_before(now - 90 * 86400) == 1
    assert store.get_run("run_old_finished") is None
    assert store.get_run("run_old_queued00") is not None  # unfinished runs are never purged
    assert store.get_run("run_recent_done0") is not None


def test_expired_project_copies_are_deleted_even_when_nothing_new_is_saved(settings):
    from patchproof.projects import build_project, validate_files

    store = Store(settings.db_path)
    project = build_project("p", "upload", validate_files([("m.py", b"x = 1\n")]))
    store.save_project(project)
    sqlite3.connect(settings.db_path).execute("UPDATE projects SET created_at = ? WHERE id = ?",
                                              (time.time() - 3 * 86400, project.id)).connection.commit()
    assert store.expire_projects() == 1 and store.get_project(project.id) is None


def test_startup_applies_the_configured_retention(settings):
    from patchproof.app import create_app
    from patchproof.verification.engine import Engine

    store = Store(settings.db_path)
    store.save_run(Run(id="run_ancient_done", created_at=time.time() - 400 * 86400, state="finished", project_id="p",
                       project_name="p", patch_source=PatchSource.user))
    s = dataclasses.replace(settings, run_retention_days=30)
    with TestClient(create_app(s, engine=Engine(s, None, docker_check=lambda: (False, "off")))):
        assert Store(settings.db_path).get_run("run_ancient_done") is None
    store.save_run(Run(id="run_ancient_kept", created_at=time.time() - 400 * 86400, state="finished", project_id="p",
                       project_name="p", patch_source=PatchSource.user))
    keep = dataclasses.replace(settings, run_retention_days=0)  # 0 keeps everything
    with TestClient(create_app(keep, engine=Engine(keep, None, docker_check=lambda: (False, "off")))):
        assert Store(settings.db_path).get_run("run_ancient_kept") is not None


def test_deleting_an_account_removes_its_data_and_only_its_data(gh_settings, fake):  # noqa: F811
    app = make_app(gh_settings, fake)
    with TestClient(app) as alice, TestClient(app) as bob:
        a_csrf, b_csrf = sign_in(alice), sign_in(bob, "code-bob")
        a_run = start_run(alice, a_csrf)
        bob_run = bob.post("/api/runs", headers={"X-CSRF-Token": b_csrf},
                           json={"example_id": "pagination-off-by-one", "patch_source": "scripted",
                                 "scripted_patch_id": "minimal-fix"}).json()
        wait(bob, bob_run["id"])
        assert alice.delete("/api/account").status_code == 403  # CSRF required
        deleted = alice.delete("/api/account", headers={"X-CSRF-Token": a_csrf})
        assert deleted.status_code == 200 and deleted.json()["deleted"]["runs"] == 1
        assert fake.revoked  # the GitHub authorization was revoked
        assert alice.get("/api/auth/me").json()["authenticated"] is False
        assert alice.get(f"/api/runs/{a_run['id']}").status_code == 401
        assert bob.get(f"/api/runs/{bob_run['id']}").status_code == 200  # untouched
    db = gh_settings.db_path
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM users WHERE id = 1").fetchone()[0] == 0
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM github_tokens WHERE user_id = 1").fetchone()[0] == 0
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM usage WHERE owner = 'user:1'").fetchone()[0] == 0
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM usage WHERE owner = 'user:2'").fetchone()[0] == 1


def test_a_live_backup_restores_runs_sessions_and_tokens(gh_settings, fake, tmp_path):  # noqa: F811
    """The documented backup command (SQLite's online backup) yields a database a fresh server can use."""
    app = make_app(gh_settings, fake)
    with TestClient(app) as c:
        csrf = sign_in(c)
        run = start_run(c, csrf)
        live = sqlite3.connect(gh_settings.db_path)
        backup_path = tmp_path / "backup.sqlite3"
        backup = sqlite3.connect(backup_path)
        live.backup(backup)  # same mechanism as `sqlite3 .backup`
        backup.close()
        live.close()
    restored = dataclasses.replace(gh_settings, db_path=backup_path)
    with TestClient(make_app(restored, fake)) as c2:
        assert c2.get(f"/api/runs/{run['id']}").status_code == 401  # a new browser still has to sign in
        c2.cookies.clear()
        sign_in(c2)
        assert [r["id"] for r in c2.get("/api/runs").json()] == [run["id"]]  # history survived the restore


def test_rotating_the_secret_key_forces_a_new_sign_in_instead_of_failing_obscurely(gh_settings, fake):  # noqa: F811
    first = make_app(gh_settings, fake)
    with TestClient(first) as c:
        sign_in(c)
        cookies = dict(c.cookies)
    rotated = make_app(dataclasses.replace(gh_settings, secret_key=Fernet.generate_key().decode()), fake)
    with TestClient(rotated) as c:
        c.cookies.update(cookies)
        r = c.get("/api/github/installations")  # the stored tokens can no longer be decrypted
        assert r.status_code == 401 and r.json()["code"] == "github.reauthorize"
