"""Reproducible mutation checks of PatchProof's security controls.

Each mutant disables one control in the source, runs the test that is meant to guard it, and requires that test
to FAIL. A mutant that survives means the control has no effective test. Source files are always restored.

    uv run python scripts/mutation_check.py
"""

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
# (name, file, original text, mutated text, guarding test)
MUTANTS = [
    ("CSRF check", "src/patchproof/middleware.py",
     'and not hmac.compare_digest(Headers(scope=scope).get("x-csrf-token", ""), principal.csrf or "")):',
     "and False):", "tests/test_github.py::test_session_writes_need_the_csrf_token"),
    ("run ownership", "src/patchproof/app.py",
     "if not run or run.owner != principal.owner:", "if not run:",
     "tests/test_github.py::test_users_cannot_see_each_others_runs_projects_or_reports"),
    ("browser-bound OAuth state", "src/patchproof/accounts.py",
     "if not cookie_state or not hmac.compare_digest(state, cookie_state):", "if False:",
     "tests/test_github.py::test_callback_requires_this_browsers_state_once"),
    ("single-use OAuth state", "src/patchproof/store.py",
     '            self._conn.execute("DELETE FROM oauth_states WHERE state_hash = ?", (state_hash,))\n            self._conn.commit()\n        if not row',
     "            self._conn.commit()\n        if not row",
     "tests/test_github.py::test_callback_requires_this_browsers_state_once"),
    ("pull-request patch approval", "src/patchproof/pull_requests.py",
     "if patch_sha256(patch.diff) != req.patch_sha256:", "if False:",
     "tests/test_github.py::test_pull_request_needs_the_feature_approval_and_the_exact_patch"),
    ("pull request needs a verified run", "src/patchproof/pull_requests.py",
     "if not run.verdict or run.verdict.status != VerdictStatus.verified:", "if False:",
     "tests/test_github.py::test_unverified_runs_never_become_pull_requests"),
    ("archive host allowlist", "src/patchproof/github.py",
     'if target.scheme != "https" or target.hostname != CODELOAD_HOST:', "if False:",
     "tests/test_github.py::test_archive_redirects_only_to_codeload"),
    ("atomic quota", "src/patchproof/store.py",
     "                if used >= limit:\n                    return used\n", "                pass\n",
     "tests/test_usage.py::test_parallel_requests_cannot_exceed_the_monthly_quota"),
    ("token refresh lock", "src/patchproof/accounts.py",
     "            return self._refresh_locks.setdefault(user_id, threading.Lock())",
     "            return threading.Lock()",
     "tests/test_github.py::test_concurrent_requests_refresh_a_user_token_only_once"),
    ("token encryption at rest", "src/patchproof/accounts.py",
     "return self._fernet.encrypt(value.encode()).decode()", "return value",
     "tests/test_github.py::test_sign_in_binds_the_github_identity_and_encrypts_tokens"),
]


def main() -> int:
    survived = []
    for name, rel, old, new, test in MUTANTS:
        path = ROOT / rel
        original = path.read_text(encoding="utf-8")
        if original.count(old) != 1:
            print(f"ERROR     {name}: pattern not found exactly once in {rel}")
            return 2
        path.write_text(original.replace(old, new), encoding="utf-8")
        try:
            result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", test], cwd=ROOT,
                                    capture_output=True, text=True)
        finally:
            path.write_text(original, encoding="utf-8")
        killed = result.returncode != 0
        print(f"{'KILLED  ' if killed else 'SURVIVED'}  {name}")
        if not killed:
            survived.append(name)
    print(f"\n{len(MUTANTS) - len(survived)} of {len(MUTANTS)} mutants killed" + (f"; survived: {survived}" if survived else ""))
    return 1 if survived else 0


if __name__ == "__main__":
    raise SystemExit(main())
