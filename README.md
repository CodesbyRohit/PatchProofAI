# PatchProof AI

[![CI](https://github.com/CodesbyRohit/PatchProofAI/actions/workflows/ci.yml/badge.svg)](https://github.com/CodesbyRohit/PatchProofAI/actions/workflows/ci.yml)

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

1. **Baseline.** The original test suite runs twice in the sandbox. Tests that fail in *both* runs become the reproduction targets. Tests that change outcome between identical runs are marked flaky: they are excluded from the comparison, and because the suite did not behave reproducibly, the run as a whole can at best be INCONCLUSIVE.
2. **Diagnosis.** BharatCode (`qwen-3.8-27b` by default) receives only the relevant source files, the failing test output, and the list of tests that must keep passing. It returns a structured JSON diagnosis: root cause, cited evidence with line ranges, exact search/replace edits, possible regressions, and its confidence.
3. **Validation.** Before anything runs, the backend checks the patch: it may only change existing `.py` source files, never tests, `conftest.py`, or build/CI config. It may not escape the project, must parse, must stay under size limits, and may not add new process, file, network, or interpreter-control capabilities (`os`, `subprocess`, `open`, `eval`, `__import__`, …).
4. **Verification.** The patched copy runs twice. Every test is compared with its baseline outcome.
5. **Verdict.** Backend rules decide the result, never the model. If the patch fails, the real failure output goes back to the model for one more attempt.

### Verdicts

| Verdict | Meaning |
|---|---|
| `VERIFIED_FOR_TESTED_CASES` | Every reproduction target failed at baseline and passed in every candidate run, and every test that passed at baseline still passes. This is evidence for the tested cases, **not** a proof of correctness. |
| `FAILED_VERIFICATION` | Observed failure evidence: a target still fails, a previously passing test now fails (regression), a test disappeared, the patch broke test collection, or the patch could not be applied. |
| `INCONCLUSIVE` | No failure was observed, but the evidence is incomplete: environment errors (e.g. a missing dependency), any flaky test (even one unrelated to the targets), skipped targets, timeouts, crashes or resource-limit kills (e.g. the sandbox's memory limit), no reproducible failure, or no usable model response. |
| `BLOCKED_BY_SAFETY_POLICY` | The patch broke the safety policy (e.g. it edited a test), or no sandbox was available to run untrusted code. Nothing was executed. |

Precedence is conservative: any failure evidence makes the verdict FAILED. Otherwise any gap in the evidence makes it INCONCLUSIVE. Only a clean, complete, repeated pass is VERIFIED. Tests that already failed before the patch are not counted as regressions.

A patched run killed by a resource limit is a gap, not failure evidence: the process can die before the runner records which tests passed or failed, so the patch was not shown to be wrong, only not verifiable. A hang that the per-test timeout catches is different: the runner records the timed-out test as failed, so that is failure evidence. Resource limits never produce BLOCKED_BY_SAFETY_POLICY, which is reserved for patches rejected by a safety rule before execution.

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

**Prerequisites:** Python 3.12+, [uv](https://docs.astral.sh/uv/), Node.js 22.12+ or 24 with npm (for the web interface), and Docker (Docker Desktop on Windows/macOS). Without Docker, only bundled examples with their scripted patches can run. Uploads and AI-generated patches are blocked.

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

Set `PATCHPROOF_BACKEND_URL` to proxy to a different API address. This variable is read only by the Vite dev server; it has no effect on a production build or on Vercel (see [Deployment](#deployment)).

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

## Benchmark

`benchmark/` holds 29 deterministic bug-fixing cases: correct fixes, plausible patches that regress, fixes that leave a target failing, prohibited edits, invalid patches, a collection break, cases that must stay inconclusive, and flaky tests. Each case is a small project with one **scripted** patch, run through the real verification engine and Docker sandbox. No model is called, and the patches are labelled `benchmark_fixture`, never as AI output. The benchmark measures the verifier, not a model's ability to fix bugs. Details, scope and limitations are in [benchmark/README.md](benchmark/README.md).

```bash
cd backend
uv run python -m patchproof.benchmark     # Docker sandbox required; writes benchmark/results/
```

**Measured result: 29 of 29 cases matched in two complete runs of the benchmark** (2 baseline + 2 patched runs per case, Docker sandbox):

| Run | Environment | Result | Benchmark time |
|---|---|---|---|
| [GitHub Actions 40282328735](https://github.com/CodesbyRohit/PatchProofAI/actions/runs/40282328735) | Ubuntu 24.04 runner | 29 of 29 | 117 s |
| Local, 2026-10-26 | Windows 11, Docker Desktop 29.9.2 | 29 of 29 | 292 s |

| Measure | Result (both runs) |
|---|---|
| Correct fixes verified | 8 of 8 |
| Plausible regressing patches rejected, with the exact regression count | 5 of 5 |
| Fixes leaving a target failing, invalid patches, and the collection break rejected | 5 of 5 |
| Prohibited edits blocked | 5 of 5 |
| Missing dependency, no reproducible failure, skipped target, and flaky cases kept INCONCLUSIVE | 5 of 5 |
| Memory-exhausting patch: run actually killed (exit 137) and reported INCONCLUSIVE | 1 of 1 |
| Patches verified that should not have been | 0 |

Expected outcomes were written from the verdict rules below before the first run. The first full runs, before the fixes described next, matched 27 of 29 cases. The two mismatches were investigated rather than tuned away:

- **`fl-flaky-bystander`:** a correct fix next to an unrelated flaky test was VERIFIED, contradicting the documented policy. **The engine was fixed:** a flaky baseline test now makes the run INCONCLUSIVE (failure evidence still takes precedence), with regression tests in `backend/tests/test_verdict.py`.
- **`rt-digits-infinite-loop`:** a patch that loops forever while growing a list is killed by the sandbox's 512 MB memory limit before any per-test result exists. Per the policy below, that is INCONCLUSIVE, not FAILED, so **the expectation was corrected** and the case now also requires the kill to actually happen. A CPU-only hang (`rt-power-of-two-hang`) is caught by the per-test timeout and is FAILED with 2 regressions.

CI runs the full benchmark on every push and publishes the per-case table in the job summary and as the `benchmark-results` artifact. These results measure the verifier on scripted patches only; they say nothing about the quality of model-generated fixes, and no live BharatCode run has been observed yet.

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

## Deployment

PatchProof has two parts with different hosting needs:

| Part | Where it can run | Why |
|---|---|---|
| Web interface (`frontend/`) | Vercel, or any static host | It is a static Vite build. It calls `/api/...` on its own origin, or on the API address set at build time in `VITE_API_BASE_URL`, and holds no secrets. |
| API, run store and sandbox (`backend/`) | A Linux host with Docker Engine | Every untrusted test run is a `docker run` against the host's Docker daemon. Runs are long-lived background jobs in the server process, and history is a SQLite file on local disk. Vercel Functions have no Docker daemon and no persistent local disk, so the backend **cannot** run on Vercel. |

```
browser ──► Vercel (static frontend)
   └──── HTTPS + access token ────► API host: Caddy (TLS) ─► uvicorn ─► Docker sandbox per test run
```

The browser reaches the API either directly (`VITE_API_BASE_URL` plus a CORS allowlist on the backend) or through a Vercel rewrite of `/api/*`. [deploy/README.md](deploy/README.md) is the step-by-step runbook for the API host, including both options.

### Frontend on Vercel

> **A frontend-only deployment is a UI preview, not a working verification service.** Until a backend is connected (below), the site can show its pages but cannot list examples, run or verify anything, or generate patches.

`frontend/vercel.json` holds the build settings, the client-side routing fallback, and the API caching opt-out. Create the Vercel project from this repository with these settings:

| Setting | Value |
|---|---|
| Root Directory | `frontend` (required: `vercel.json` and `package.json` live there) |
| Framework Preset | Vite |
| Install Command | `npm ci` |
| Build Command | `npm run build` (`tsc --noEmit && vite build`) |
| Output Directory | `dist` |
| Node.js Version | 22.x or 24.x (`engines` in `package.json`) |
| Environment variables | Only `VITE_API_BASE_URL`, when the browser should call the API host directly (see below). It is compiled into public JavaScript: an address, never a secret. Add no other `VITE_*` variables. |

Routing in `vercel.json`:

- Files that exist in `dist` (hashed `/assets/*`, `favicon.svg`) are served directly, with Vercel's default caching. A long-lived `Cache-Control` on `/assets/*` is deliberately not set: Vercel would attach it to 404s for missing assets too, and browsers would keep those 404s.
- Any other path except `/api` and `/assets/*` is rewritten to `index.html`, so reloading `/history` or `/runs/<id>` works.
- `/api` and `/api/*` are **never** rewritten to `index.html`. Until the backend rewrite below is added, they return Vercel's 404, and the workspace says "/api is not routed to a PatchProof backend" instead of rendering broken data.
- `x-vercel-enable-rewrite-caching: 0` tells Vercel's CDN not to cache proxied API responses, which would freeze run polling. This is Vercel's documented behaviour. Confirm it on the live site once the backend rewrite exists: polling a running run must show its state change.

After deploying, check the routing on the live site. Use the production domain: preview URLs are usually behind Vercel Authentication, which makes `curl` see a 401.

```bash
for p in /history /runs/example /favicon.svg /assets/missing.js /api/status; do
  curl -s -o /dev/null -w "%{http_code} %{content_type}  $p\n" "https://<your-vercel-domain>$p"; done
```

Expected: 200 with `text/html` for the first two, 200 for the favicon, and 404 for the last two. With the backend connected, `/api/status` returns 200 with JSON instead.

### Connecting the backend

Once the API host answers on HTTPS ([deploy/README.md](deploy/README.md)), pick one:

- **Direct (recommended).** In Vercel, set `VITE_API_BASE_URL=https://<your-api-host>` for Production and **redeploy** (build-time variables only reach a new build). On the backend, set `PATCHPROOF_CORS_ORIGINS=https://<your-vercel-domain>`: exact origins only, no wildcards. The backend then sees each visitor's address for rate limiting. The build fails on a malformed address, and on a non-HTTPS address other than localhost.
- **Same-origin rewrite.** Add this rule as the **first** entry of `rewrites` in `frontend/vercel.json` and redeploy. No CORS setting is needed, but visitors share one rate-limit budget, and Vercel's limits for proxied requests apply.

  ```json
  { "source": "/api/:path*", "destination": "https://<your-api-host>/api/:path*" }
  ```

Verify through the site, not only the API host: open the workspace, enter an access token, run a bundled example to its verdict, and download its report. `/api/health` (liveness) and `/api/ready` (store writable, Docker sandbox usable) are public probes; every other API path needs a token.

### Hosted mode and access control

A reachable backend must run with `PATCHPROOF_HOSTED=1`. It then **refuses to start** unless:

- access tokens are configured (`PATCHPROOF_ACCESS_TOKEN_HASHES`);
- the Docker sandbox is required (`PATCHPROOF_SANDBOX=docker`);
- the unsandboxed override is off;
- `PATCHPROOF_DATA_DIR` is set.

It also refuses a second server process on the same data directory.

- **Tokens.** `python -m patchproof.access new` prints a random token for one person and the SHA-256 hash to add to the server's list. The server stores only hashes, and removing one revokes that token.
  - The browser asks for the token when the API answers 401, and keeps it only in that tab's `sessionStorage`. It is never part of the build.
  - Every API request, report download included, sends it as `Authorization: Bearer`.
  - Tokens are shared secrets for a private demo or a small team, not user accounts: every token holder sees every run.
- **Abuse limits.** These are in addition to the existing per-client rate limit and bounded run queue:
  - request bodies over 6 MB are refused with 413, whether or not `Content-Length` is sent;
  - `POST /api/runs` honours an `Idempotency-Key`, so a retried start can't create a second run;
  - sandbox containers left behind by a crashed server are removed when it restarts.

| Backend variable | Value for a hosted backend |
|---|---|
| `PATCHPROOF_HOSTED` | `1` |
| `PATCHPROOF_ACCESS_TOKEN_HASHES` | Comma-separated SHA-256 hashes from `python -m patchproof.access new` |
| `PATCHPROOF_CORS_ORIGINS` | The exact frontend origin(s), for the direct option |
| `PATCHPROOF_SANDBOX` | `docker` |
| `PATCHPROOF_ALLOW_UNSANDBOXED` | Unset or `0` |
| `PATCHPROOF_DATA_DIR` | A persistent directory for the SQLite run history |
| `BHARATCODE_API_KEY` | Secret. Needed for AI patch generation only. |

These live on the API host (`deploy/patchproof.env.example`), never in Vercel or `vercel.json`. Run exactly one uvicorn worker behind a TLS reverse proxy. The host's Docker access is root-equivalent, so use a dedicated VM. `deploy/` has a systemd unit and a Caddy configuration.

### What works where

| Feature | Vercel frontend alone | With the backend connected |
|---|---|---|
| Pages, navigation, deep links | Yes | Yes |
| Status, examples, history, runs, uploads, downloads | No: the workspace shows that `/api` is not routed | Yes, after entering an access token |
| Sandboxed verification of uploads and AI patches | No | Yes, only if Docker is available on the backend host. Otherwise these runs are blocked, never run on the host. |
| AI patch generation | No | Needs a BharatCode plan with API access. Live generation has not been observed yet; see BharatCode integration. |

### Known deployment limitations

- **No API host exists yet.** The runbook and hosted mode were validated locally on Windows with Docker Desktop: tokens, CORS from a second origin, the Docker sandbox, a crash and restart, and a browser run of a bundled example. They have not been run on a Linux VM.
- **Access control is shared tokens, not accounts.** Without hosted mode, the API is open to anyone who can reach it (fine on your own machine only), and it logs a warning at startup saying so.
- **Rate limiting with the rewrite option.** Behind Vercel's proxy, the backend sees Vercel's addresses, so visitors share one rate-limit budget. That is stricter, not looser. The direct option gives per-visitor limits: the reverse proxy on the API host forwards the real address, and uvicorn trusts it only from localhost.
- **Not yet observed live:** Vercel's limits for proxied requests (rewrite option). Test an upload near the 4 MB cap if you use it. All API calls are short: runs are started with one request and then polled.

## GitHub repositories

With a GitHub App configured (setup: [deploy/README.md](deploy/README.md), "Optional: GitHub sign-in and repositories"):

1. Sign in with GitHub on `/github`.
2. Pick a repository: an installed one (private or public), or any public one by `owner/name`.
3. Pick a branch. PatchProof resolves it to a commit and **pins the run to that SHA**.
4. Describe the bug, then ask BharatCode for a patch or paste your own diff.

The run goes through the same engine and Docker sandbox as uploads. The result page shows the source commit, which files were not used, and the evidence. If the instance allows it, a verified patch can become a **draft** pull request on a new branch, after you review the exact diff.

- **Repository runs are limited to:** Python with pytest, up to 300 supported files and 4 MB of them, and a 25 MB archive by default. Files PatchProof does not use (binaries, symlinks, Git LFS pointers, other file types) are skipped and listed. Dependencies are not installed.
- **Security controls are mutation-checked:** `backend/scripts/mutation_check.py` (also a CI step) disables 10 controls one at a time and requires the guarding test to fail. 12 of 12 are caught. The script found one test that did not guard its control (the refresh lock); that test was fixed.
- **Browser journey:** `e2e/` (harness plus `journey.mjs`) drives sign-in, a verified private-repository run, a draft pull request, another user being refused, and sign-out against the real API and Docker sandbox, with GitHub replaced by the test double.
- **Tested** with a GitHub test double (`backend/tests/github_fake.py`) that implements the OAuth, REST, archive and Git Data calls PatchProof makes:
  - 32 API tests;
  - a run from a private repository verified in the real Docker sandbox;
  - a browser journey: sign-in, repository, branch, verified run, draft pull request, another user refused, sign-out.
- **Not yet tested against real GitHub.** That needs a registered GitHub App. In particular, it is not yet confirmed whether a GitHub App user token can read public repositories where the app is not installed. PatchProof falls back to anonymous access for public repositories either way.

## Repair evaluation

[evaluation/README.md](evaluation/README.md) describes a separate harness that asks whether PatchProof's verify-and-revise workflow fixes bugs better than a single model attempt, scored with hidden tests that no arm sees. **It is built and tested, but no model results exist yet** (the development account cannot generate), and its four tasks are synthetic. Nothing here claims PatchProof outperforms anything.

## Privacy, retention and policies

- Finished runs (logs, diffs, issue text) are deleted after 90 days (`PATCHPROOF_RUN_RETENTION_DAYS`, 0 keeps them), and fetched or uploaded project copies after 24 hours. A user can delete a run (`DELETE /api/runs/{id}`) or everything stored about them ("Delete my data" on `/github`, `DELETE /api/account`), which also revokes the GitHub authorization. Operator backups and web-server logs are not covered.
- Drafts that **need legal review before external users are invited:** [PRIVACY.md](PRIVACY.md) and [TERMS.md](TERMS.md). [SECURITY.md](SECURITY.md) says how to report a vulnerability, and needs a private channel enabled first. [CHANGELOG.md](CHANGELOG.md) lists changes.
- [deploy/READINESS.md](deploy/READINESS.md) is the production-readiness audit, with what is and is not tested, and the backup, monitoring and incident-response runbooks.

## Usage, quotas and analytics

- **Accounting.** Every run gets a usage row: owner, origin, sandbox seconds, model calls and tokens, duration and verdict. It never holds issue text, diffs or file contents.
- **Quotas** are enforced on the server before a run starts: 30 verifications per UTC month on the free plan (`PATCHPROOF_FREE_MONTHLY_RUNS`). The bundled demo does not count, and the open single-user local mode has no quota. An operator assigns the `member` plan (no monthly limit) with `python -m patchproof.usage set-plan user:<github id> member`. Nothing the browser sends can change a plan or a count.
- **Billing is not implemented.** There is no payment provider, and `/api/account/usage` reports `billing.enabled: false`. No prices are set.
- **Events** are first-party and allowlisted: sign-in started or completed, repository selected, run submitted or completed, downloads and pull requests. Each stores a keyed hash of the owner, a few enumerated values and no content, for 90 days. `PATCHPROOF_ANALYTICS=0` turns them off. The browser can send only `landing_viewed` and `repository_selected`, and no frontend code calls the events endpoint yet.
- **Operator report.** `python -m patchproof.usage report` shows the funnel, completion and verdict counts, median time to result, and returning users. Cost per run and per verified run are computed only after you set `PATCHPROOF_COST_PER_SANDBOX_HOUR` and `PATCHPROOF_COST_PER_1K_MODEL_TOKENS` to your real prices. **Unit economics are unmeasured today.**

## Security model

Uploaded code and model-generated patches are treated as hostile.

- **Isolation boundary: Docker.** Each test run uses a fresh container with `--network none`, a read-only root filesystem, a read-only workspace mount, `--cap-drop ALL`, `no-new-privileges`, UID `65534`, and limits on memory (512 MB), CPU (1), processes (128), open files, and file size. A wall-clock timeout kills the container, and a per-test timeout catches infinite loops. Nothing from the host environment, including the API key, is passed in. These properties are checked by `tests/test_docker_sandbox.py`, which runs hostile tests inside the real container.
- **No sandbox, no execution.** If Docker is unavailable, uploads and AI patches are **blocked**, not run on the host. The only exception is the explicit developer override `PATCHPROOF_ALLOW_UNSANDBOXED=1`, which is clearly labelled in every report.
- **Input validation.** ZIP uploads reject path traversal, absolute paths, symlinks, encrypted entries, binary files, unsupported file types, and oversized or decompression-bomb archives (limits: 4 MB archive, 2 MB extracted, 200 files, 256 KB per file).
- **Patch policy.** Tests are the evidence, so patches can't touch them. The capability scan is a guardrail against "fixes" that tamper with the test runner. It is **not** the isolation boundary; the container is.
- **Prompt injection.** Project files, logs, and the issue text are wrapped as untrusted data, and the system prompt tells the model to ignore any instructions inside them. More importantly, the model has no tools, and its output is only ever parsed as data: it can't execute anything or influence the verdict.
- **Secrets.** The API key stays on the server and is redacted from logs, errors, and reports. Host paths are removed from recorded commands.
- **Abuse limits.** Rate limiting per client, a bounded run queue, bounded model retries, a 6 MB request-body limit, and idempotent run creation.
- **Access control (hosted mode).** Bearer access tokens, of which the server stores only SHA-256 hashes. Hosted mode will not start without tokens, the Docker sandbox, and a data directory; see [Hosted mode and access control](#hosted-mode-and-access-control).

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
uv run pytest -q          # 267 tests (Docker tests are skipped if Docker is unavailable)
uv run ruff check src tests

cd ../frontend
npm run lint
npm run typecheck
npm test                  # Vitest + Testing Library, using real captured API responses
npm run build
```

The frontend tests render real run records captured from the API (`frontend/src/test/fixtures/`, provenance in its README): the four verdicts, a missing API key, a simulated HTTP 402, polling, upload rejection, unreachable backend, timeouts and malformed responses.

### Continuous integration

`.github/workflows/ci.yml` runs on pushes to `main` and `feat/**`, on pull requests, and on manual dispatch. It has a read-only token, actions pinned to commit SHAs, and no deploy step.

- **Backend** (Ubuntu runner): `uv sync --locked`, Ruff, a build of the sandbox image, and the full pytest suite against the real Docker daemon. `PATCHPROOF_REQUIRE_DOCKER=1` turns a missing sandbox into a test failure instead of a skip, and a final step fails the job unless every test in `tests/test_docker_sandbox.py` (at least 9) actually ran and passed. It then runs the benchmark, which fails the job if any case differs from its expectation, and writes the results table to the job summary.
- **Frontend:** `npm ci`, then lint, type-check, tests and the production build.

CI never needs a BharatCode key. Model behaviour, including the HTTP 402 `plan_required` path, is covered by scripted fake clients and captured fixtures, so no provider calls are made. To reproduce the Docker requirement locally:

```bash
cd backend
PATCHPROOF_REQUIRE_DOCKER=1 uv run pytest -q -rs     # PowerShell: $env:PATCHPROOF_REQUIRE_DOCKER=1; uv run pytest -q -rs
```

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
| Benchmark | 29 scripted cases through the real engine and Docker sandbox; 29 of 29 in two complete runs (CI and local), run on every push |
| CI workflow | Added (GitHub Actions: backend with the real Docker sandbox, frontend); see Continuous integration |
| GitHub integration | Implemented and tested against a GitHub test double and the real Docker sandbox; not yet run against real GitHub (needs a registered GitHub App) |
| Hosted deployment | Web interface live on Vercel as a UI preview. The API needs a separate Docker host, which is not yet set up. Hosted mode (tokens, CORS, readiness) and its runbook ([deploy/README.md](deploy/README.md)) were validated locally, not yet on a VM. |

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
benchmark/
  cases/                    one folder per case: case.json (patch, expected outcome) and project/
examples/                   bundled demo projects with scripted patches
scripts/                    sandbox image build scripts
```

## License

[MIT](LICENSE) © 2026 Rohit Srivastava (Team LoneWolf)
