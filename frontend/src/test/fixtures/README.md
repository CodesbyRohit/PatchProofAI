# Test fixtures

These JSON files are real responses from the PatchProof backend, captured on 2026-10-26 from a local
API server (`uvicorn patchproof.app:create_app --factory`) with the Docker sandbox and no API key:

| File | Request |
|---|---|
| `status.json` | `GET /api/status` |
| `examples.json` | `GET /api/examples` |
| `runs.json` | `GET /api/runs` after the runs below |
| `plausible.json` | `pagination-off-by-one`, scripted `plausible-plus-one` → `FAILED_VERIFICATION` |
| `minimal.json` | `pagination-off-by-one`, scripted `minimal-fix` → `VERIFIED_FOR_TESTED_CASES` |
| `blocked.json` | `pagination-off-by-one`, scripted `edit-the-test` → `BLOCKED_BY_SAFETY_POLICY` |
| `inconclusive.json` | `report-missing-dependency`, scripted `guard-empty` → `INCONCLUSIVE` |
| `ai_noconfig.json` | `pagination-off-by-one`, `patch_source: ai`, no key configured → `INCONCLUSIVE` |

`plan_required.json` is the one exception to the live capture: the real engine and Docker sandbox ran the
baseline, but the BharatCode client was replaced by the test suite's `FakeClient` raising the
`plan_required` error (HTTP 402) that the real client raises. No live 402 run record has been captured yet.
