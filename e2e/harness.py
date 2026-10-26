"""Start the real PatchProof API (Docker sandbox, built frontend) with GitHub replaced by the in-memory test double
from backend/tests/github_fake.py. No network and no credentials. Not part of the product.

    cd backend && uv run python ../e2e/harness.py <data-dir> [port]
"""

import dataclasses
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend" / "tests"))

import uvicorn  # noqa: E402
from cryptography.fernet import Fernet  # noqa: E402
from github_fake import FakeGitHub, Repo  # noqa: E402
from test_github import BUGGY  # noqa: E402

from patchproof.app import create_app  # noqa: E402
from patchproof.config import load_settings  # noqa: E402
from patchproof.github import GitHubClient  # noqa: E402

data_dir = pathlib.Path(sys.argv[1])
port = int(sys.argv[2]) if len(sys.argv) > 2 else 8000
fake = FakeGitHub()
fake.repos["alice/private-app"] = Repo(id=11, full_name="alice/private-app", private=True, files=BUGGY, push={1: True})
fake.repos["octo/public-lib"] = Repo(id=12, full_name="octo/public-lib", private=False, files=BUGGY)
fake.installed = {1: ["alice/private-app"], 2: []}
settings = dataclasses.replace(
    load_settings(), sandbox_backend="docker", db_path=data_dir / "patchproof.sqlite3", api_key=None,
    github_client_id="Iv1.e2e", github_client_secret="e2e-secret",  # noqa: S106 - fake test credential, never real
    secret_key=Fernet.generate_key().decode(),
    github_callback_url=f"http://127.0.0.1:{port}/api/auth/github/callback", github_pr_writes=True, cookie_secure=False)
app = create_app(settings, github=GitHubClient("Iv1.e2e", "e2e-secret", transport=fake.transport()))
uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
