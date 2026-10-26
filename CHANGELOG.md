# Changelog

All notable changes. Versions follow `backend/pyproject.toml`. Dates are when the change was made, not when it was
released: nothing here has been deployed beyond the static frontend preview.

## Unreleased

### Added
- GitHub App sign-in, repository, branch and commit picking, and runs pinned to one commit SHA (not yet tested against
  real GitHub).
- Optional draft pull requests for verified patches, after approval of the exact diff.
- Per-user ownership of projects, runs and reports; CSRF protection; encrypted GitHub tokens.
- Monthly free-plan quota, usage accounting and an operator report. Billing is not implemented.
- Run deletion, "delete my data", and 90-day retention of finished runs.
- Hosted mode (`PATCHPROOF_HOSTED=1`), access tokens, an exact-origin CORS list, `/api/ready`, and a deployment runbook.
- Repair-evaluation harness with hidden tests and four synthetic tasks. No model results exist yet.
- Security mutation checks (`backend/scripts/mutation_check.py`) in CI.
- Draft privacy notice and terms (need legal review) and a security policy.

### Fixed
- Parallel requests could exceed the monthly quota; the check and the reservation are now one atomic step.
- Runs that errored before a verdict used up quota; they no longer do.
- A security test did not guard the token-refresh lock; it now does.

## 0.1.0

First public version: the verification engine, Docker sandbox, web interface and the 29-case verifier benchmark.
