"""Hostile-code tests against the real Docker sandbox.

Skipped when Docker is unavailable, unless PATCHPROOF_REQUIRE_DOCKER=1 (set in CI), which turns a
missing sandbox into a failure so isolation coverage can never silently disappear.
"""

import dataclasses
import os
import time

import pytest

from patchproof.examples import load_examples
from patchproof.projects import build_project, validate_files
from patchproof.sandbox import DockerSandbox, docker_status
from patchproof.schemas import PatchSource, Run, RunHealth, VerdictStatus
from patchproof.verification.engine import Engine

pytestmark = pytest.mark.docker


@pytest.fixture
def docker_settings(settings):
    ok, detail = docker_status(settings.sandbox_image, ttl_s=0)
    if not ok:
        if os.environ.get("PATCHPROOF_REQUIRE_DOCKER") == "1":
            pytest.fail(f"PATCHPROOF_REQUIRE_DOCKER=1 but the Docker sandbox is unavailable: {detail}")
        pytest.skip(detail)
    return dataclasses.replace(settings, sandbox_backend="docker", verification_runs=1)


HOSTILE_TESTS = '''
import os, socket, subprocess, pytest

def test_network_is_blocked():
    with pytest.raises(OSError):
        socket.create_connection(("1.1.1.1", 53), timeout=3)

def test_workspace_is_read_only():
    with pytest.raises(OSError):
        open("/work/pwned.txt", "w").write("x")

def test_root_filesystem_is_read_only():
    with pytest.raises(OSError):
        open("/usr/pwned.txt", "w").write("x")

def test_runs_unprivileged():
    assert os.getuid() == 65534

def test_no_host_secrets_in_environment():
    assert "BHARATCODE_API_KEY" not in os.environ

def test_process_limit_holds():
    procs = []
    with pytest.raises(Exception):
        for _ in range(400):
            procs.append(subprocess.Popen(["sleep", "5"]))
    for p in procs:
        p.kill()
'''


def test_restart_removes_containers_left_by_a_previous_process(docker_settings):
    import subprocess
    import uuid

    instance = f"test{uuid.uuid4().hex[:8]}"
    started = subprocess.run(
        ["docker", "run", "-d", "--rm", "--network", "none", "--label", f"patchproof.instance={instance}",
         docker_settings.sandbox_image, "python", "-c", "import time; time.sleep(300)"],
        capture_output=True, text=True, timeout=60)
    assert started.returncode == 0, started.stderr
    try:
        assert DockerSandbox(docker_settings.sandbox_image, instance="unrelated").remove_orphans() == 0
        assert DockerSandbox(docker_settings.sandbox_image, instance=instance).remove_orphans() == 1
        left = subprocess.run(["docker", "ps", "-aq", "--filter", f"label=patchproof.instance={instance}"],
                              capture_output=True, text=True, timeout=15)
        assert left.stdout.strip() == ""
    finally:
        subprocess.run(["docker", "rm", "-f", started.stdout.strip()], capture_output=True, timeout=60)


def test_private_github_repository_run_is_verified_in_the_sandbox(docker_settings, tmp_path):
    """Repository -> pinned commit -> sanitized workspace -> Docker baseline and candidate runs -> verdict.

    GitHub is the in-memory fake from tests/github_fake.py; everything after the archive is the real engine
    and the real Docker sandbox.
    """
    from urllib.parse import parse_qs, urlsplit

    from cryptography.fernet import Fernet
    from fastapi.testclient import TestClient
    from github_fake import FakeGitHub, Repo
    from test_github import BUGGY, FIX, RUN_GITHUB, wait

    from patchproof.app import create_app
    from patchproof.github import GitHubClient

    fake = FakeGitHub()
    fake.repos["alice/private-app"] = Repo(id=11, full_name="alice/private-app", private=True, files=BUGGY)
    fake.installed = {1: ["alice/private-app"]}
    s = dataclasses.replace(docker_settings, verification_runs=2, github_client_id="Iv1.test",
                            github_client_secret="test-secret", secret_key=Fernet.generate_key().decode(),
                            github_callback_url="http://testserver/api/auth/github/callback")
    app = create_app(s, github=GitHubClient("Iv1.test", "test-secret", transport=fake.transport()))
    with TestClient(app) as c:
        start = c.get("/api/auth/github/start", follow_redirects=False)
        state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
        c.get(f"/api/auth/github/callback?code=code-alice&state={state}", follow_redirects=False)
        csrf = c.get("/api/auth/me").json()["csrf"]
        run = wait(c, c.post("/api/github/runs", json=RUN_GITHUB, headers={"X-CSRF-Token": csrf}).json()["id"], timeout=240)
    assert run["sandbox"] == "docker" and run["sandbox_isolated"] is True
    assert run["verdict"]["status"] == "VERIFIED_FOR_TESTED_CASES", run["verdict"]
    assert run["attempts"][0]["patch"]["diff"] == FIX
    assert run["source"]["sha"] == "a" * 40


def project_with(tests: str, source: str = "def f():\n    return 1\n"):
    files = validate_files([("m.py", source.encode()), ("tests/test_hostile.py", tests.encode())])
    return build_project("hostile", "upload", files)


def test_sandbox_restrictions_hold(docker_settings):
    engine = Engine(docker_settings, None)
    project = project_with(HOSTILE_TESTS)
    result = engine.execute_tests({f.path: f.content for f in project.files},
                                  DockerSandbox(docker_settings.sandbox_image), "probe")
    assert result.health == RunHealth.ok, (result.detail, result.stderr)
    outcomes = {t.nodeid.split("::")[1]: t.outcome.value for t in result.tests}
    assert outcomes == dict.fromkeys(outcomes, "passed"), [t.message for t in result.tests if t.outcome.value != "passed"]
    assert len(outcomes) == 6


def test_wall_clock_timeout_kills_container(docker_settings):
    s = dataclasses.replace(docker_settings, run_timeout_s=8)
    engine = Engine(s, None)
    project = project_with("import time\ntime.sleep(60)\n\ndef test_x():\n    pass\n")
    started = time.monotonic()
    result = engine.execute_tests({f.path: f.content for f in project.files}, DockerSandbox(s.sandbox_image), "slow")
    assert result.health == RunHealth.timeout
    assert time.monotonic() - started < 30


def test_tampering_with_exit_is_not_trusted(docker_settings):
    engine = Engine(docker_settings, None)
    project = project_with("import os\n\ndef test_a():\n    os._exit(0)\n")
    result = engine.execute_tests({f.path: f.content for f in project.files},
                                  DockerSandbox(docker_settings.sandbox_image), "exit")
    assert result.health == RunHealth.report_missing


def test_per_test_timeout_fails_only_the_hanging_test(docker_settings):
    s = dataclasses.replace(docker_settings, per_test_timeout_s=2)
    project = project_with("def test_ok():\n    pass\n\ndef test_hang():\n    while True:\n        pass\n")
    result = Engine(s, None).execute_tests({f.path: f.content for f in project.files},
                                           DockerSandbox(s.sandbox_image), "hang")
    assert result.health == RunHealth.ok
    outcomes = {t.nodeid.split("::")[1]: t.outcome.value for t in result.tests}
    assert outcomes == {"test_ok": "passed", "test_hang": "failed"}


def test_docker_end_to_end_regression_trap(docker_settings):
    example, project = load_examples(docker_settings.examples_dir)["pagination-off-by-one"]
    patch = next(p for p in example.patches if p.id == "plausible-plus-one")
    run = Run(id="run_d", created_at=time.time(), state="queued", project_id=project.id,
              project_name=project.name, patch_source=PatchSource.scripted)
    Engine(docker_settings, None).execute(run, project, scripted=patch)
    assert run.sandbox == "docker" and run.sandbox_isolated
    assert run.verdict.status == VerdictStatus.failed
