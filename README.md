# PatchProof AI

**Don't just suggest a fix. Prove what works, expose what fails, and show the evidence.**

PatchProof AI is a debugging agent for Python projects built on the [BharatCode](https://bharatcode.ai/build) model API. It doesn't stop at proposing a patch. It runs your tests before and after the change in an isolated sandbox, compares the results test by test, and reports a verdict based only on what actually happened.

> Built by **Team LoneWolf** for the BharatCode Build League (Coding & developer tools).

---

## Why

AI coding assistants produce convincing explanations and plausible patches, but a plausible patch is not a working one. The most common failure is a fix that makes the reported test pass while quietly breaking something that used to work.

PatchProof makes that gap visible. In the bundled pagination example, the "obvious" one-line fix (`total_items // per_page + 1`) makes both failing tests pass and still gets **rejected**, because it breaks three tests that passed before:

```
FAILED_VERIFICATION: Rejected: the patch fixes 2 of 2 target tests but breaks 3 tests that passed before.
```

The minimal correct fix (`(total_items + per_page - 1) // per_page`) on the same project:

```
VERIFIED_FOR_TESTED_CASES: 2 failing tests now pass and 5 previously passing tests still pass, across 2 candidate runs.
```

Both results above are real output from the Docker sandbox, not mock-ups.

## How it works

```
 project ──► baseline runs ──► reproduce failures ──► AI diagnosis ──► patch validation ──► candidate runs ──► verdict
 (zip or     (original code,    (tests that fail      (BharatCode,       (policy + syntax,     (patched copy,     (deterministic
  example)    run twice)         every time)           structured JSON)    no test edits)        run twice)         backend rules)
                                                              ▲                                        │
                                                              └──── bounded repair: real failure ◄─────┘
                                                                    evidence sent back (max 2 attempts)
```

1. **Baseline.** The original test suite runs twice in the sandbox. Tests that fail in *both* runs become the reproduction targets. Tests that change outcome between identical runs are marked flaky and excluded.
2. **Diagnosis.** BharatCode (`qwen-3.8-27b` by default) receives only the relevant source files, the failing test output, and the list of tests that must keep passing. It returns a structured JSON diagnosis: root cause, cited evidence with line ranges, exact search/replace edits, possible regressions, and its confidence.
3. **Validation.** Before anything runs, the backend checks the patch: it may only change existing `.py` source files, never tests, `conftest.py`, or build/CI config. It may not escape the project, must parse, must stay under size limits, and may not add new process, file, network, or interpreter-control capabilities (`os`, `subprocess`, `open`, `eval`, `__import__`, …).
4. **Verification.** The patched copy runs twice. Every test is compared with its baseline outcome.
5. **Verdict.** Backend rules decide the result, never the model. If the patch fails, the real failure output goes back to the model for one more attempt.

### Verdicts

| Verdict | Meaning |
|---|---|
| `VERIFIED_FOR_TESTED_CASES` | Every reproduction target failed at baseline and passed in every candidate run, and every test that passed at baseline still passes. This is evidence for the tested cases, **not** a proof of correctness. |
| `FAILED_VERIFICATION` | Observed failure evidence: a target still fails, a previously passing test now fails (regression), a test disappeared, the patch broke test collection, or the patch could not be applied. |
| `INCONCLUSIVE` | No failure was observed, but the evidence is incomplete: environment errors (e.g. a missing dependency), flaky tests, skipped targets, timeouts, crashes, no reproducible failure, or no usable model response. |
| `BLOCKED_BY_SAFETY_POLICY` | The patch broke the safety policy (e.g. it edited a test), or no sandbox was available to run untrusted code. Nothing was executed. |

Precedence is conservative: any failure evidence makes the verdict FAILED. Otherwise any gap in the evidence makes it INCONCLUSIVE. Only a clean, complete, repeated pass is VERIFIED. Tests that already failed before the patch are not counted as regressions.

## Bundled examples

The examples are trusted code shipped with PatchProof. Each comes with **scripted patches**, written by the maintainers and labelled as such (never presented as model output), so the verification engine can be demonstrated without an API key. The verdicts below are the observed results from the test suite (`tests/test_engine.py`) and from the Docker sandbox.

| Example | Patch | Observed verdict |
|---|---|---|
| `pagination-off-by-one` | Plausible fix: `// per_page + 1` | **FAILED_VERIFICATION** (fixes 2 targets, breaks 3 tests) |
| `pagination-off-by-one` | Minimal fix: ceiling division | **VERIFIED_FOR_TESTED_CASES** |
| `pagination-off-by-one` | Unsafe: edit the test's expectation | **BLOCKED_BY_SAFETY_POLICY** |
| `cart-shared-state` | Plausible fix: always start with an empty list | **FAILED_VERIFICATION** (fixes 2 targets, breaks 1 test) |
| `cart-shared-state` | Minimal fix: `None` default plus a copy | **VERIFIED_FOR_TESTED_CASES** |
| `report-missing-dependency` | Correct fix for the reproducible bug | **INCONCLUSIVE** (PyYAML is not installed, so part of the suite cannot run) |

You can also run any example in AI mode (`patch_source: "ai"`) to have BharatCode diagnose and patch it.

## Quick start

**Prerequisites:** Python 3.12+, [uv](https://docs.astral.sh/uv/), Node.js 20+ with npm (for the web interface), and Docker (Docker Desktop on Windows/macOS). Without Docker, only bundled examples with their scripted patches can run. Uploads and AI-generated patches are blocked.

```bash
git clone https://github.com/CodesbyRohit/PatchProofAI.git
cd PatchProofAI

# 1. Build the execution sandbox image
./scripts/build-sandbox.sh            # PowerShell: .\scripts\build-sandbox.ps1

# 2. Configure the BharatCode key (optional; needed for AI mode only)
cp .env.example backend/.env           # then set BHARATCODE_API_KEY in backend/.env

# 3. Build the web interface (the API server serves frontend/dist when it exists)
cd frontend
npm ci
npm run build
cd ..

# 4. Install and start the API server
cd backend
uv sync
uv run uvicorn patchproof.app:create_app --factory --port 8000
```

Open **http://localhost:8000**. The workspace shows the sandbox and model status, the bundled demo examples, and the project upload.

### Web interface

The frontend (`frontend/`, React + TypeScript + Vite) talks only to the PatchProof API. The BharatCode key never reaches the browser: there are no `VITE_*` secrets, and model calls happen only on the server.

- **Workspace:** backend status (Docker sandbox, model configuration), bundled demo examples with their scripted patches, and `.zip` upload. After an upload you can ask BharatCode for a patch or paste your own unified diff.
- **Run page:** a stamped verdict with the backend's reasons; a test ledger showing each test's outcome in both baseline runs and both patched runs (regressions first; expand a row for the real failure message); the diagnosis, policy findings and exact diff; the pipeline built from the run's recorded events; raw test output; and downloads of `patch.diff`, `report.md` and `report.json`. Runs in progress are polled until the backend reports them finished.
- **History:** every run, with links back to its evidence.
- **Demo mode:** scripted patches are labelled as maintainer-written demo data on every screen, never as model output. The test runs and verdicts they produce are real.
- **Model failures:** an HTTP 402 from BharatCode (plan without API generation) is shown as an access issue, not a code failure, along with the recorded provider message and request ID.

For frontend development with hot reload, run the API server on port 8000 and, in a second terminal:

```bash
cd frontend
npm run dev                            # http://localhost:5173, proxies /api to http://127.0.0.1:8000
```

Set `PATCHPROOF_BACKEND_URL` to proxy to a different API address.

### Demo walkthrough

1. Open the workspace and pick **Pagination drops the last page** (marked "Start here").
2. Run **Plausible fix: add one page**. The ledger shows both target tests fixed and three previously passing tests broken: **FAILED_VERIFICATION**.
3. From the run page, run **Minimal fix: ceiling division**: **VERIFIED_FOR_TESTED_CASES**, with every previously passing test still green.
4. Run **Unsafe: change the test instead**: **BLOCKED_BY_SAFETY_POLICY**, with the policy finding shown and nothing executed.
5. Download the diff and the Markdown report from the run page.

### Using the API directly

Check what's configured:

```bash
curl http://localhost:8000/api/status
```

Run the regression-trap demo (no API key needed):

```bash
curl -X POST http://localhost:8000/api/runs -H "Content-Type: application/json" \
  -d '{"example_id": "pagination-off-by-one", "patch_source": "scripted", "scripted_patch_id": "plausible-plus-one"}'
# => {"id": "run_...", "state": "queued", ...}

curl http://localhost:8000/api/runs/<run_id>               # poll until "state": "finished"
curl http://localhost:8000/api/runs/<run_id>/report.md     # human-readable verification report
```

PowerShell equivalent:

```powershell
$run = Invoke-RestMethod -Method Post http://localhost:8000/api/runs -ContentType 'application/json' `
  -Body '{"example_id":"pagination-off-by-one","patch_source":"scripted","scripted_patch_id":"plausible-plus-one"}'
Invoke-RestMethod "http://localhost:8000/api/runs/$($run.id)"
```

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/status` | Model configuration and catalog check, sandbox availability, limits |
| `GET` | `/api/examples` | Bundled examples and their scripted patches |
| `POST` | `/api/projects/upload` | Upload a `.zip` Python project (multipart field `file`) |
| `POST` | `/api/projects/inline` | Submit files as JSON: `{"files": [{"path": "...", "content": "..."}]}` |
| `GET` | `/api/projects/{id}` | Project files and structure summary |
| `POST` | `/api/runs` | Start a run: `project_id` or `example_id`, plus `patch_source` of `ai`, `user`, or `scripted` |
| `GET` | `/api/runs` | Run history |
| `GET` | `/api/runs/{id}` | Full run: timeline, baseline and candidate runs, diagnosis, patch, verdict |
| `GET` | `/api/runs/{id}/patch.diff` | The exact unified diff that was applied |
| `GET` | `/api/runs/{id}/report.md` | Verification report (Markdown) |
| `GET` | `/api/runs/{id}/report.json` | Complete run record, including raw test logs |

**Bring your own patch:** set `"patch_source": "user"` and pass a unified diff in `user_patch` to verify a fix from any tool (Copilot, ChatGPT, a colleague) against the same rules.

## Security model

Uploaded code and model-generated patches are treated as hostile.

- **Isolation boundary: Docker.** Each test run uses a fresh container with `--network none`, a read-only root filesystem, a read-only workspace mount, `--cap-drop ALL`, `no-new-privileges`, UID `65534`, and limits on memory (512 MB), CPU (1), processes (128), open files, and file size. A wall-clock timeout kills the container, and a per-test timeout catches infinite loops. Nothing from the host environment, including the API key, is passed in. These properties are checked by `tests/test_docker_sandbox.py`, which runs hostile tests inside the real container.
- **No sandbox, no execution.** If Docker is unavailable, uploads and AI patches are **blocked**, not run on the host. The only exception is the explicit developer override `PATCHPROOF_ALLOW_UNSANDBOXED=1`, which is clearly labelled in every report.
- **Input validation.** ZIP uploads reject path traversal, absolute paths, symlinks, encrypted entries, binary files, unsupported file types, and oversized or decompression-bomb archives (limits: 4 MB archive, 2 MB extracted, 200 files, 256 KB per file).
- **Patch policy.** Tests are the evidence, so patches can't touch them. The capability scan is a guardrail against "fixes" that tamper with the test runner. It is **not** the isolation boundary; the container is.
- **Prompt injection.** Project files, logs, and the issue text are wrapped as untrusted data, and the system prompt tells the model to ignore any instructions inside them. More importantly, the model has no tools, and its output is only ever parsed as data: it can't execute anything or influence the verdict.
- **Secrets.** The API key stays on the server and is redacted from logs, errors, and reports. Host paths are removed from recorded commands.
- **Abuse limits.** Rate limiting per client, a bounded run queue, and bounded model retries.

There has been no professional security audit.

## BharatCode integration

- **Endpoint:** `POST https://bharatcode.ai/api/model/v1/chat/completions` (OpenAI-compatible), stateless, key sent server-side as `Authorization: Bearer`.
- **Model:** `qwen-3.8-27b` by default (`BHARATCODE_MODEL`). It's validated against the live `GET /models` catalog before use, falling back to the documented model list if the catalog can't be read.
- **Error handling:** 401 (bad key), 402 (plan doesn't include API use), 403 (model not available to the account), and 400 fail fast with a clear message. 429 and 5xx get at most two retries with backoff (honouring `Retry-After`). Read timeouts are not retried, to avoid resending large prompts. `finish_reason: "length"` is detected as truncation, and malformed or empty responses are rejected. Unparseable output gets one bounded correction request. Every model call records latency, token usage, and `x-request-id`.
- **Disclosure:** each AI run lists exactly which files were sent to the API.

**Verification status (2026-10-26):** with the team's key, authentication and the live model catalog work (`GET /models` returned `qwen-3.8-27b` and `deepseek-v4.1-flash`). Generation requests return **HTTP 402**: the account's plan doesn't yet include API use (BharatCode requires Lite/Pro, or a verified student account). PatchProof reports this correctly as `INCONCLUSIVE` with the provider's message, records `model_error_kind: "plan_required"` on the run, and the web interface explains it as an access issue rather than a code failure. **A complete live diagnosis-and-patch run has therefore not yet been observed.** The AI loop is covered by integration tests with a scripted client (`tests/test_engine.py`), which are not a substitute for a live run.

## Development

```bash
cd backend
uv run pytest -q          # 127 tests (Docker tests are skipped if Docker is unavailable)
uv run ruff check src tests

cd ../frontend
npm run lint
npm run typecheck
npm test                  # Vitest + Testing Library, using real captured API responses
npm run build
```

The frontend tests render real run records captured from the API (`frontend/src/test/fixtures/`, provenance in its README): the four verdicts, a missing API key, a simulated HTTP 402, polling, upload rejection, unreachable backend, timeouts and malformed responses.

The suite covers patch parsing and policy, ZIP and path attacks, verdict rules (regressions, flaky tests, environment errors, timeouts, collection errors), result parsing, model-client failure modes (auth, plan, rate limit, outage, timeout, malformed and truncated output), secret redaction, the end-to-end engine loop, the HTTP API, and hostile code inside the real Docker sandbox.

## Project status

| Area | Status |
|---|---|
| Verification engine, patch policy, verdict rules | Implemented and tested |
| Docker sandbox | Implemented; isolation properties tested on Docker 29.9.2 (Windows host, Linux engine) |
| HTTP API, run history, patch and report downloads | Implemented and tested |
| BharatCode client | Implemented and tested with mocks; live auth and catalog confirmed; live generation blocked by account plan (HTTP 402) |
| Bundled demo examples | 3 projects, 6 scripted patches, verdicts as listed above |
| Web interface | Implemented and tested: workspace, live run view, test ledger, evidence, history, report downloads |
| Benchmark (20+ cases) | Planned |
| CI workflow | Planned |
| Hosted deployment | None. The sandbox needs a Docker host, so PatchProof currently runs locally. |

## Limitations

- **Python and pytest only.** The sandbox provides pytest and pytest-timeout and nothing else. Projects that need other third-party packages get `INCONCLUSIVE` for the affected tests. Dependency installation isn't supported yet.
- **The verdict is only as strong as the tests.** "Verified" means "verified for the tested cases". A weak test suite can let a wrong fix through.
- **The test process is shared with the code under test.** Code inside the container could, in principle, interfere with the in-process result reporter. Mitigations: tests and config can't be patched, dangerous capabilities are blocked in patches, exit codes are cross-checked, and runs that end abnormally are never trusted. The container still bounds the damage.
- **Patches can only modify existing source files.** Creating, deleting, or renaming files isn't supported.
- **Non-reproducible failures can't be verified.** A failure must reproduce in both baseline runs. Flaky failures are reported, not "fixed".

## Repository layout

```
BharatCode.txt              Build League submission file
backend/
  src/patchproof/
    agent/                  BharatCode client, prompts, diagnosis parser
    verification/           patching, policy, result parsing, verdict rules, engine, reports
    sandbox/                Docker and local runners; image/ holds the Dockerfile and pytest reporter plugin
    app.py                  FastAPI application
  tests/                    backend test suite
frontend/
  src/api/                  typed API client and response types
  src/components/           verdict stamp, test ledger, pipeline, diff, evidence
  src/pages/                workspace, run, history
  src/test/                 Vitest suites and captured API fixtures
examples/                   bundled demo projects with scripted patches
scripts/                    sandbox image build scripts
```

## License

[MIT](LICENSE) © 2026 Rohit Srivastava (Team LoneWolf)
