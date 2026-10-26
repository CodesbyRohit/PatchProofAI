"""Hostile-code tests against the real Docker sandbox. Skipped when Docker is unavailable."""

import dataclasses
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
