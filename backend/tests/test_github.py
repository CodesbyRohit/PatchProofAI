"""GitHub sign-in, repository access, repository runs and pull requests, against an in-memory GitHub (no network)."""

import dataclasses
import sqlite3
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from github_fake import FakeGitHub, Repo, make_zip

from patchproof.access import hash_token
from patchproof.app import create_app
from patchproof.github import GitHubClient, GitHubError
from patchproof.projects import ProjectRejected
from patchproof.pull_requests import patch_sha256
from patchproof.repo_intake import encode_like, read_repository_zip
from patchproof.schemas import (
    Attempt,
    PatchSource,
    RepoSource,
    Run,
    RunRequest,
    Verdict,
    VerdictStatus,
)
from patchproof.store import Store
from patchproof.verification.engine import Engine
from patchproof.verification.patching import prepare_candidate

KEY = Fernet.generate_key().decode()
SHA = "a" * 40
BUGGY = {
    "calc.py": b"def add(a, b):\n    return a - b\n",
    "tests/test_calc.py": b"from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n\n\ndef test_zero():\n    assert add(0, 0) == 0\n",
}
FIX = "--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a - b\n+    return a + b\n"
RUN_GITHUB = {"repository": "alice/private-app", "ref": "main", "sha": SHA, "patch_source": "user", "user_patch": FIX}


@pytest.fixture
def fake():
    gh = FakeGitHub()
    gh.repos["alice/private-app"] = Repo(
        id=11, full_name="alice/private-app", private=True, push={1: True},
        files={**BUGGY, "logo.png": b"\x89PNG\r\n\x1a\n\x00\x00", "LICENSE": b"MIT",
               "data/model.txt": b"version https://git-lfs.github.com/spec/v1\noid sha256:abc\n"},
        symlinks={"link.py": "calc.py"})
    gh.repos["octo/public-lib"] = Repo(id=12, full_name="octo/public-lib", private=False, files=BUGGY)
    gh.repos["bob/secret"] = Repo(id=13, full_name="bob/secret", private=True, files=BUGGY, push={2: True})
    gh.installed = {1: ["alice/private-app"], 2: ["bob/secret"]}
    return gh


@pytest.fixture
def gh_settings(settings):
    return dataclasses.replace(settings, github_client_id="Iv1.test", github_client_secret="test-secret",
                               github_callback_url="http://testserver/api/auth/github/callback", secret_key=KEY,
                               github_app_slug="patchproof-test")


def make_app(gh_settings, fake, **changes):
    s = dataclasses.replace(gh_settings, **changes)
    engine = Engine(s, None, docker_check=lambda: (False, "Docker disabled in tests."))
    github = GitHubClient(s.github_client_id, s.github_client_secret, transport=fake.transport())
    return create_app(s, engine=engine, github=github)


def sign_in(client: TestClient, code: str = "code-alice") -> str:
    start = client.get("/api/auth/github/start", follow_redirects=False)
    assert start.status_code == 302
    query = parse_qs(urlsplit(start.headers["location"]).query)
    assert query["code_challenge_method"] == ["S256"] and query["code_challenge"][0]
    done = client.get(f"/api/auth/github/callback?code={code}&state={query['state'][0]}", follow_redirects=False)
    assert done.status_code == 302 and done.headers["location"] == "/github", done.headers.get("location")
    me = client.get("/api/auth/me").json()
    assert me["authenticated"] is True
    return me["csrf"]


def wait(client, run_id, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = client.get(f"/api/runs/{run_id}").json()
        if run["state"] in ("finished", "error"):
            return run
        time.sleep(0.1)
    raise AssertionError("run did not finish")


# -- sign-in -----------------------------------------------------------------------------------------------


def test_sign_in_binds_the_github_identity_and_encrypts_tokens(gh_settings, fake):
    app = make_app(gh_settings, fake)
    with TestClient(app) as c:
        anonymous = c.get("/api/auth/me").json()
        assert anonymous["authenticated"] is False
        assert anonymous["github"]["install_url"] == "https://github.com/apps/patchproof-test/installations/new"
        assert c.get("/api/status").status_code == 401  # sign-in is required once GitHub is configured
        csrf = sign_in(c)
        me = c.get("/api/auth/me").json()
        assert me["login"] == "alice" and me["kind"] == "user" and len(csrf) > 20
        assert c.get("/api/status").status_code == 200
    raw = sqlite3.connect(gh_settings.db_path).execute("SELECT access_enc, refresh_enc FROM github_tokens").fetchone()
    assert raw and not any("ghu_" in v or "ghr_" in v for v in raw)  # never stored in the clear
    sessions = sqlite3.connect(gh_settings.db_path).execute("SELECT id_hash FROM sessions").fetchall()
    assert all(len(h) == 64 for (h,) in sessions)  # only the hash of the cookie value


def test_callback_requires_this_browsers_state_once(gh_settings, fake):
    app = make_app(gh_settings, fake)
    with TestClient(app) as attacker, TestClient(app) as victim:
        start = attacker.get("/api/auth/github/start", follow_redirects=False)
        state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
        # A forged callback link: valid state and code, but the victim's browser did not start this sign-in.
        forged = victim.get(f"/api/auth/github/callback?code=code-alice&state={state}", follow_redirects=False)
        assert forged.headers["location"] == "/github?error=state"
        assert victim.get("/api/auth/me").json()["authenticated"] is False
        ok = attacker.get(f"/api/auth/github/callback?code=code-alice&state={state}", follow_redirects=False)
        assert ok.headers["location"] == "/github"
        # Replaying the same state (single use) fails even from the right browser.
        attacker.cookies.set("pp_oauth_state", state, path="/api/auth/github")
        replay = attacker.get(f"/api/auth/github/callback?code=code-alice&state={state}", follow_redirects=False)
        assert replay.headers["location"] == "/github?error=state"


def test_bad_code_and_user_denial_do_not_sign_in(gh_settings, fake):
    with TestClient(make_app(gh_settings, fake)) as c:
        start = c.get("/api/auth/github/start", follow_redirects=False)
        state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
        bad = c.get(f"/api/auth/github/callback?code=wrong&state={state}", follow_redirects=False)
        assert bad.headers["location"] == "/github?error=denied"
        denied = c.get("/api/auth/github/callback?error=access_denied", follow_redirects=False)
        assert denied.headers["location"] == "/github?error=denied"
        assert c.get("/api/auth/me").json()["authenticated"] is False


def test_allowlist_refuses_other_accounts_and_revokes_their_grant(gh_settings, fake):
    with TestClient(make_app(gh_settings, fake, github_allowed_logins=("alice",))) as c:
        start = c.get("/api/auth/github/start", follow_redirects=False)
        state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
        r = c.get(f"/api/auth/github/callback?code=code-bob&state={state}", follow_redirects=False)
        assert r.headers["location"] == "/github?error=not_allowed"
        assert c.get("/api/auth/me").json()["authenticated"] is False
        assert fake.revoked  # the unused authorization was revoked


def test_session_writes_need_the_csrf_token(gh_settings, fake):
    with TestClient(make_app(gh_settings, fake)) as c:
        csrf = sign_in(c)
        body = {"example_id": "pagination-off-by-one", "patch_source": "scripted", "scripted_patch_id": "minimal-fix"}
        refused = c.post("/api/runs", json=body)
        assert refused.status_code == 403 and "CSRF" in refused.json()["detail"]
        assert c.post("/api/runs", json=body, headers={"X-CSRF-Token": "wrong"}).status_code == 403
        assert c.post("/api/runs", json=body, headers={"X-CSRF-Token": csrf}).status_code == 202


def test_users_cannot_see_each_others_runs_projects_or_reports(gh_settings, fake):
    app = make_app(gh_settings, fake, access_token_hashes=(hash_token("ppa_token-holder-0123456789abcdef"),))
    with TestClient(app) as alice, TestClient(app) as bob, TestClient(app) as holder:
        a_csrf, b_csrf = sign_in(alice), sign_in(bob, "code-bob")
        body = {"example_id": "pagination-off-by-one", "patch_source": "scripted", "scripted_patch_id": "minimal-fix"}
        run = alice.post("/api/runs", json=body, headers={"X-CSRF-Token": a_csrf}).json()
        project = alice.post("/api/projects/inline", headers={"X-CSRF-Token": a_csrf},
                             json={"files": [{"path": "m.py", "content": "x = 1\n"}]}).json()
        for path in (f"/api/runs/{run['id']}", f"/api/runs/{run['id']}/report.md", f"/api/runs/{run['id']}/report.json",
                     f"/api/projects/{project['id']}"):
            assert bob.get(path).status_code == 404, path
            assert holder.get(path, headers={"Authorization": "Bearer ppa_token-holder-0123456789abcdef"}).status_code == 404
            assert alice.get(path).status_code == 200, path
        assert bob.get("/api/runs").json() == []
        assert [r["id"] for r in alice.get("/api/runs").json()] == [run["id"]]
        # Bob cannot start a run on Alice's uploaded project either.
        stolen = bob.post("/api/runs", json={"project_id": project["id"], "patch_source": "user", "user_patch": "x"},
                          headers={"X-CSRF-Token": b_csrf})
        assert stolen.status_code == 404
        # Bundled examples stay shared.
        assert bob.get("/api/projects/example:pagination-off-by-one").status_code == 200


def test_sign_out_disconnect_and_expired_sessions(gh_settings, fake):
    with TestClient(make_app(gh_settings, fake)) as c:
        csrf = sign_in(c)
        assert c.post("/api/auth/logout", headers={"X-CSRF-Token": csrf}).status_code == 200
        assert c.get("/api/auth/me").json()["authenticated"] is False
        csrf = sign_in(c)
        sqlite3.connect(gh_settings.db_path).execute("UPDATE sessions SET expires_at = 0").connection.commit()
        assert c.get("/api/auth/me").json()["authenticated"] is False  # expired session is no session
        assert c.get("/api/runs").status_code == 401
        csrf = sign_in(c)
        assert c.post("/api/auth/github/disconnect", headers={"X-CSRF-Token": csrf}).status_code == 200
        assert fake.revoked
        db = sqlite3.connect(gh_settings.db_path)
        assert db.execute("SELECT COUNT(*) FROM github_tokens").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_expired_access_token_is_refreshed_once_and_rotated(gh_settings, fake):
    fake.expires_in = 1  # GitHub sends 28800; 1 s forces the refresh path
    with TestClient(make_app(gh_settings, fake)) as c:
        sign_in(c)
        first_refresh = set(fake.refresh_tokens)
        assert c.get("/api/github/installations").status_code == 200
        assert set(fake.refresh_tokens).isdisjoint(first_refresh)  # the single-use refresh token was used and replaced
        assert c.get("/api/github/installations").status_code == 200  # and the new pair keeps working


def test_revoked_refresh_token_asks_for_sign_in_again(gh_settings, fake):
    fake.expires_in = 1
    with TestClient(make_app(gh_settings, fake)) as c:
        sign_in(c)
        fake.refresh_tokens.clear()  # user revoked the app on GitHub
        r = c.get("/api/github/installations")
        assert r.status_code == 401 and r.json()["code"] == "github.reauthorize"
        assert sqlite3.connect(gh_settings.db_path).execute("SELECT COUNT(*) FROM github_tokens").fetchone()[0] == 0


# -- repositories ------------------------------------------------------------------------------------------


def test_repository_listing_is_paginated(gh_settings, fake):
    for i in range(150):
        fake.repos[f"alice/r{i}"] = Repo(id=1000 + i, full_name=f"alice/r{i}", private=i % 2 == 0, files=BUGGY)
    fake.installed[1] = [f"alice/r{i}" for i in range(150)]
    with TestClient(make_app(gh_settings, fake)) as c:
        sign_in(c)
        inst = c.get("/api/github/installations").json()
        assert inst == [{"id": 101, "account": "alice", "repository_selection": "selected"}]
        p1 = c.get("/api/github/installations/101/repos?page=1").json()
        p2 = c.get("/api/github/installations/101/repos?page=2").json()
        assert (len(p1["repositories"]), p1["has_more"], p1["total_count"]) == (100, True, 150)
        assert (len(p2["repositories"]), p2["has_more"]) == (50, False)
        assert {r["private"] for r in p1["repositories"]} == {True, False}
        assert c.get("/api/github/installations/102/repos").status_code == 404  # bob's installation


def test_private_repositories_need_the_app_installation(gh_settings, fake):
    with TestClient(make_app(gh_settings, fake)) as c:
        sign_in(c)
        mine = c.get("/api/github/repo?full_name=alice/private-app").json()
        assert (mine["private"], mine["access"]) == (True, "installed")
        r = c.get("/api/github/repo?full_name=bob/secret")
        assert r.status_code == 404 and r.json()["code"] == "github.not_found"
        public = c.get("/api/github/repo?full_name=octo/public-lib").json()
        assert (public["private"], public["access"]) == (False, "public")


@pytest.mark.parametrize("name", ["../etc/passwd", "evil.com/x", "a/b/c", "alice/", "-bad/repo", "alice/re po"])
def test_repository_names_are_validated_before_any_request(gh_settings, fake, name):
    with TestClient(make_app(gh_settings, fake)) as c:
        sign_in(c)
        before = len(fake.calls)
        r = c.get("/api/github/repo", params={"full_name": name})
        assert r.status_code == 422 and r.json()["code"] == "github.invalid"
        assert len(fake.calls) == before


def test_branches_commits_and_rate_limits(gh_settings, fake):
    with TestClient(make_app(gh_settings, fake)) as c:
        sign_in(c)
        branches = c.get("/api/github/branches?full_name=alice/private-app").json()
        assert branches["branches"] == [{"name": "main", "sha": SHA, "protected": False}]
        commit = c.get("/api/github/commit?full_name=alice/private-app&ref=main").json()
        assert commit["sha"] == SHA and commit["message"] == "Initial"
        bad = c.get("/api/github/commit", params={"full_name": "alice/private-app", "ref": "main..evil"})
        assert bad.status_code == 422
        fake.rate_limited = True
        limited = c.get("/api/github/branches?full_name=alice/private-app")
        assert limited.status_code == 429 and limited.json()["code"] == "github.rate_limited"
        assert limited.headers.get("retry-after")


def test_repository_endpoints_need_a_github_session(gh_settings, fake):
    token = "ppa_token-holder-0123456789abcdef"
    with TestClient(make_app(gh_settings, fake, access_token_hashes=(hash_token(token),))) as c:
        r = c.get("/api/github/installations", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401 and "Sign in with GitHub" in r.json()["detail"]


# -- repository runs ---------------------------------------------------------------------------------------


def test_run_is_pinned_to_the_commit_and_fetched_without_leaking_credentials(gh_settings, fake):
    with TestClient(make_app(gh_settings, fake)) as c:
        csrf = sign_in(c)
        r = c.post("/api/github/runs", json=RUN_GITHUB, headers={"X-CSRF-Token": csrf})
        assert r.status_code == 202, r.text
        run = wait(c, r.json()["id"])
        assert run["source"]["sha"] == SHA and run["source"]["full_name"] == "alice/private-app"
        assert run["source"]["skipped_count"] == 4  # PNG, LICENSE, LFS pointer, symlink
        assert any("symbolic link" in s for s in run["source"]["skipped"])
        assert any("Git LFS" in s for s in run["source"]["skipped"])
        steps = [(e["step"], e["status"]) for e in run["events"]]
        assert ("source", "completed") in steps
        # Docker is off in this test, so the untrusted code is blocked, never run on the host.
        assert run["verdict"]["status"] == "BLOCKED_BY_SAFETY_POLICY"
        project = c.get(f"/api/projects/{run['project_id']}").json()
        assert sorted(f["path"] for f in project["files"]) == ["calc.py", "tests/test_calc.py"]
        # The archive came from codeload after the API redirect; the user token never went to codeload.
        assert any("codeload.github.com" in call for call in fake.calls)


def test_public_repository_run_uses_no_credentials(gh_settings, fake):
    with TestClient(make_app(gh_settings, fake)) as c:
        csrf = sign_in(c)
        body = {**RUN_GITHUB, "repository": "octo/public-lib"}
        run = wait(c, c.post("/api/github/runs", json=body, headers={"X-CSRF-Token": csrf}).json()["id"])
        assert run["source"]["private"] is False
        assert ("source", "completed") in [(e["step"], e["status"]) for e in run["events"]]


def test_private_ai_runs_need_explicit_consent(gh_settings, fake):
    with TestClient(make_app(gh_settings, fake)) as c:
        csrf = sign_in(c)
        body = {**RUN_GITHUB, "patch_source": "ai", "user_patch": None}
        refused = c.post("/api/github/runs", json=body, headers={"X-CSRF-Token": csrf})
        assert refused.status_code == 422 and "private repository" in refused.json()["detail"]
        ok = c.post("/api/github/runs", json={**body, "allow_model_upload": True}, headers={"X-CSRF-Token": csrf})
        assert ok.status_code == 202


def test_unknown_commits_and_other_peoples_repositories_are_refused(gh_settings, fake):
    with TestClient(make_app(gh_settings, fake)) as c:
        csrf = sign_in(c)
        missing = c.post("/api/github/runs", json={**RUN_GITHUB, "sha": "b" * 40}, headers={"X-CSRF-Token": csrf})
        assert missing.status_code == 404
        other = c.post("/api/github/runs", json={**RUN_GITHUB, "repository": "bob/secret"}, headers={"X-CSRF-Token": csrf})
        assert other.status_code == 404
        assert c.get("/api/runs").json() == []


def test_archive_redirects_only_to_codeload(gh_settings, fake):
    fake.archive_location = "https://evil.example/steal.zip"
    with TestClient(make_app(gh_settings, fake)) as c:
        csrf = sign_in(c)
        run = wait(c, c.post("/api/github/runs", json=RUN_GITHUB, headers={"X-CSRF-Token": csrf}).json()["id"])
        assert run["state"] == "error" and "unexpected host" in run["error"]
        assert not any("evil.example" in call for call in fake.calls)


def test_oversized_archives_are_refused(gh_settings, fake):
    with TestClient(make_app(gh_settings, fake, repo_max_archive_bytes=200)) as c:
        csrf = sign_in(c)
        run = wait(c, c.post("/api/github/runs", json=RUN_GITHUB, headers={"X-CSRF-Token": csrf}).json()["id"])
        assert run["state"] == "error" and "larger than" in run["error"]


# -- repository intake -------------------------------------------------------------------------------------


def test_intake_skips_what_it_cannot_use_and_rejects_what_it_cannot_verify():
    data = make_zip("o-r-abc", {**BUGGY, "../escape.py": b"x = 1\n", "bin.py": b"\x00\x01", "big.py": b"#" * 300_000})
    result = read_repository_zip(data)
    assert [f.path for f in result.files] == ["calc.py", "tests/test_calc.py"]
    reasons = " | ".join(result.skipped)
    assert "unsupported path" in reasons and "binary" in reasons and "larger than" in reasons
    with pytest.raises(ProjectRejected, match="Python projects only"):
        read_repository_zip(make_zip("o-r-abc", {"README.md": b"# hi\n"}))
    with pytest.raises(ProjectRejected, match="beyond PatchProof's current limits"):
        read_repository_zip(make_zip("o-r-abc", {f"m{i}.py": b"x = 1\n" for i in range(301)}))


def test_patched_files_keep_their_line_endings_and_bom():
    assert encode_like(b"a\r\nb\r\n", "a\nc\n") == b"a\r\nc\r\n"
    assert encode_like(b"\xef\xbb\xbfa\n", "b\n") == b"\xef\xbb\xbfb\n"
    assert encode_like(None, "x\n") == b"x\n"


def test_github_client_rejects_non_sha_archives():
    client = GitHubClient(transport=FakeGitHub().transport())
    with pytest.raises(GitHubError, match="exact commit SHA"):
        client.download_archive("o/r", "main", None, 1000)


# -- pull requests -----------------------------------------------------------------------------------------


def verified_run(store: Store, owner: str, files: dict[str, str]) -> Run:
    candidate, _ = prepare_candidate(files, provenance="user_supplied", label="User-supplied patch", diff=FIX)
    candidate.applied = True
    run = Run(id="run_0123456789ab", created_at=time.time(), state="finished", owner=owner, project_id="p",
              project_name="alice/private-app", patch_source=PatchSource.user,
              source=RepoSource(repo_id=11, full_name="alice/private-app", private=True, ref="main", sha=SHA,
                                html_url="https://github.com/alice/private-app"),
              request=RunRequest(patch_source=PatchSource.user, user_patch=FIX),
              attempts=[Attempt(number=1, diagnosis_source="user_supplied", patch=candidate)],
              verdict=Verdict(status=VerdictStatus.verified, headline="Fixes 1 target, no regressions."))
    store.save_run(run)
    return run


def test_pull_request_needs_the_feature_approval_and_the_exact_patch(gh_settings, fake):
    fake.repos["alice/private-app"].files["calc.py"] = b"def add(a, b):\r\n    return a - b\r\n"  # CRLF file
    app = make_app(gh_settings, fake, github_pr_writes=True)
    with TestClient(app) as c:
        csrf = sign_in(c)
        files = {"calc.py": "def add(a, b):\n    return a - b\n", "tests/test_calc.py": BUGGY["tests/test_calc.py"].decode()}
        run = verified_run(app.state.ctx.store, "user:1", files)
        preview = c.get(f"/api/runs/{run.id}/pull-request").json()
        assert preview["branch"] == f"patchproof/{run.id}" and preview["changed_files"] == ["calc.py"]
        body = {"title": "Fix add()", "body": "Verified by PatchProof.", "patch_sha256": preview["patch_sha256"]}

        changed = c.post(f"/api/runs/{run.id}/pull-request", headers={"X-CSRF-Token": csrf},
                         json={**body, "patch_sha256": "0" * 64})
        assert changed.status_code == 409 and "differs" in changed.json()["detail"]
        assert not fake.refs

        created = c.post(f"/api/runs/{run.id}/pull-request", json=body, headers={"X-CSRF-Token": csrf})
        assert created.status_code == 200, created.text
        assert created.json()["url"] == "https://github.com/alice/private-app/pull/1"
        commit_sha = fake.refs[f"alice/private-app:patchproof/{run.id}"]
        assert fake.commits[commit_sha]["parents"] == [SHA]  # branched from the verified commit, not the default branch
        written = list(fake.blobs.values())
        assert written == [b"def add(a, b):\r\n    return a + b\r\n"]  # only the changed file, CRLF kept
        assert fake.pulls["1"]["draft"] is True and fake.pulls["1"]["base"] == "main"

        again = c.post(f"/api/runs/{run.id}/pull-request", json=body, headers={"X-CSRF-Token": csrf})
        assert again.json()["url"] == created.json()["url"] and len(fake.pulls) == 1  # idempotent


def test_pull_requests_are_refused_without_push_rights_or_when_disabled(gh_settings, fake):
    fake.repos["alice/private-app"].push = {}
    for enabled, status in ((False, 403), (True, 403)):
        app = make_app(gh_settings, fake, github_pr_writes=enabled)
        with TestClient(app) as c:
            csrf = sign_in(c)
            files = {"calc.py": BUGGY["calc.py"].decode(), "tests/test_calc.py": BUGGY["tests/test_calc.py"].decode()}
            run = verified_run(app.state.ctx.store, "user:1", files)
            body = {"title": "Fix", "patch_sha256": patch_sha256(run.attempts[0].patch.diff)}
            r = c.post(f"/api/runs/{run.id}/pull-request", json=body, headers={"X-CSRF-Token": csrf})
            assert r.status_code == status
    assert not fake.refs and not fake.pulls


def test_unverified_runs_never_become_pull_requests(gh_settings, fake):
    app = make_app(gh_settings, fake, github_pr_writes=True)
    with TestClient(app) as c:
        csrf = sign_in(c)
        files = {"calc.py": BUGGY["calc.py"].decode(), "tests/test_calc.py": BUGGY["tests/test_calc.py"].decode()}
        run = verified_run(app.state.ctx.store, "user:1", files)
        run.verdict = Verdict(status=VerdictStatus.failed, headline="Breaks 2 tests.")
        app.state.ctx.store.save_run(run)
        r = c.post(f"/api/runs/{run.id}/pull-request", headers={"X-CSRF-Token": csrf},
                   json={"title": "Fix", "patch_sha256": patch_sha256(run.attempts[0].patch.diff)})
        assert r.status_code == 409 and "verified" in r.json()["detail"]


# -- restarts ----------------------------------------------------------------------------------------------


def test_queued_runs_resume_after_a_restart_and_running_ones_are_marked(settings):
    store = Store(settings.db_path)
    queued = Run(id="run_queued000001", created_at=time.time(), state="queued", project_id="example:pagination-off-by-one",
                 project_name="pagination", patch_source=PatchSource.scripted,
                 request=RunRequest(example_id="pagination-off-by-one", patch_source=PatchSource.scripted,
                                    scripted_patch_id="minimal-fix"))
    running = queued.model_copy(update={"id": "run_running00001", "state": "running"})
    legacy = queued.model_copy(update={"id": "run_legacy000001", "request": None})
    for run in (queued, running, legacy):
        store.save_run(run)
    engine = Engine(settings, None, docker_check=lambda: (False, "off"))
    with TestClient(create_app(settings, engine=engine)) as c:
        resumed = wait(c, queued.id)
        assert resumed["state"] == "finished" and resumed["verdict"]["status"] == "VERIFIED_FOR_TESTED_CASES"
        for run_id in (running.id, legacy.id):
            stale = c.get(f"/api/runs/{run_id}").json()
            assert stale["state"] == "error" and "restarted" in stale["error"] and stale["verdict"] is None


def test_concurrent_requests_refresh_a_user_token_only_once(gh_settings, fake):
    from concurrent.futures import ThreadPoolExecutor

    fake.expires_in = 1  # the sign-in token is about to expire...
    fake.refresh_delay_s = 0.2
    with TestClient(make_app(gh_settings, fake)) as c:
        sign_in(c)
        fake.expires_in = 28800  # ...and the refreshed one is a normal 8-hour token
        before = sum("access_token" in call for call in fake.calls)
        with ThreadPoolExecutor(max_workers=8) as pool:
            codes = list(pool.map(lambda _: c.get("/api/github/installations").status_code, range(8)))
        assert codes == [200] * 8  # a second refresh with the burnt token would have signed the user out
        # Exactly one refresh happened for the 8 concurrent requests.
        assert sum("access_token" in call for call in fake.calls) - before == 1
