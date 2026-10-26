# PatchProof execution sandbox.
# Untrusted project code runs here with: no network, read-only root and workspace,
# no Linux capabilities, an unprivileged UID, and CPU/memory/PID/file-size limits
# (applied by the runner via `docker run` flags; see sandbox/docker_runner.py).
FROM python:3.12.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONHASHSEED=0 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/opt/patchproof

# Only the test runner is preinstalled. Projects that need other third-party packages
# are reported as environment failures (INCONCLUSIVE), never silently "fixed".
RUN pip install --no-cache-dir pytest==8.4.2 pytest-timeout==2.4.0 \
 && rm -rf /root/.cache

COPY patchproof_reporter.py /opt/patchproof/patchproof_reporter.py

USER 65534:65534
WORKDIR /work
