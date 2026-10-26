# Repair evaluation

The [verifier benchmark](../benchmark/README.md) tests PatchProof's verdict logic with scripted patches and no model.
It says nothing about whether PatchProof helps a model fix real bugs. This harness is for that question.

**Status: the harness is built and tested. No model results have been produced.** The BharatCode account used for
development returns HTTP 402 for generation, so no real model run has happened. Nothing in this repository claims
that PatchProof fixes more bugs than a single model attempt.

## What is measured

Two arms get the same model, the same task and the same prompt inputs (baseline test evidence, issue text, files):

| Arm | What it does |
|---|---|
| `baseline` | One model attempt. The patch passes PatchProof's patch rules but is never executed or revised. |
| `patchproof` | The full engine: run the baseline, generate a patch, run it in the Docker sandbox, revise once if it fails. |

The `patchproof` arm may use up to `PATCHPROOF_MAX_AI_ATTEMPTS` model attempts and the baseline gets one, so part of any
difference is extra model calls. The report shows token counts so that is visible.

A separate **scorer** decides correctness. It does not look at an arm's workspace or verdict:

1. It re-applies the arm's final diff to the **pristine** visible project through the same patch policy (tests,
   `conftest.py` and configuration files cannot be edited; only existing source files can).
2. Only then does it add the **hidden tests**, which no arm ever sees.
3. It runs the whole suite twice in the Docker sandbox. A patch is **correct** only if every visible and hidden test
   passes, and every test the reference fix runs also ran (nothing skipped or missing).

Reported for each arm: independently correct results (with a 95% Wilson interval), incorrect ones, regressions
(a test that passed before now fails), no patch / rejected patches, unscored results (sandbox or infrastructure
problems, listed, never counted as failures or successes), median time and model tokens. For `patchproof` it also
reports **false verifications**: runs the engine called `VERIFIED_FOR_TESTED_CASES` that the scorer says are not
correct. This is the number that shows how far the visible tests can be trusted.

## Tasks

`tasks/<id>/` holds `task.json` (id, title, `kind`, issue, licence), `project/` (what a user would upload),
`hidden/` (held-out tests), and `reference.diff`. `python -m patchproof.evaluation validate-tasks` checks every task in
the Docker sandbox: the original must fail a visible test, and the reference fix must pass every visible and hidden test.

The four tasks present are all `synthetic`: small functions with a planted bug, written for this repository, with weak
visible tests and stronger hidden ones. They show the harness works. **They cannot show how PatchProof performs on real
projects.** The `kind: real` category exists but is empty.

### Adding real tasks

Do not add one unless all of these hold, and record them in `task.json`:

- the project is under a licence that allows redistribution of the files you copy, and you keep its licence text;
- the fix is a real commit in the project's history, and the buggy commit is pinned;
- the hidden tests come from that fix commit (or are written by someone who has not seen any patch), not from the model's
  output or this tool;
- the project needs only pytest and the standard library, because dependencies are not installed in the sandbox;
- `validate-tasks` passes.

Expand the set slowly and report exclusions. With fewer than a few dozen tasks, results are anecdotes, and the intervals
treat repeats of one task as independent, which understates the uncertainty.

## Running it

```bash
cd backend
uv run python -m patchproof.evaluation validate-tasks                    # Docker; no model needed
BHARATCODE_API_KEY=... uv run python -m patchproof.evaluation run --repeats 3 --out ../evaluation/results/<name>
uv run python -m patchproof.evaluation report ../evaluation/results/<name>/results.json
```

Without a model key, `run` stops and writes nothing. The output is `results.json` (every result and summary) and
`report.md`. Results record outcomes, timings, token counts and a hash of each patch, never source code or issue text.
Do not commit results without recording the model, date and exact configuration they came from.

## How the harness protects its own validity

Each of these has a test, and the first two are also in `backend/scripts/mutation_check.py`, which disables the control and
requires the test to fail:

- **Hidden tests stay hidden.** Every request to the model goes through a guard that refuses it if it contains a hidden
  path or a long line of a hidden test. The real prompts for every task are tested to contain neither.
- **Scoring is independent.** A patch that passes the visible tests but fails a hidden one is scored incorrect, in the real
  sandbox, even when PatchProof verified it (`test_evaluation_catches_a_false_verification_in_the_real_sandbox`).
- **Tests cannot be edited into passing.** Patches that touch tests, hidden paths or `conftest.py` are rejected, and a diff
  that does not apply to the pristine project is rejected.
- **A broken task cannot slip in.** Tasks whose original passes, whose reference fails, or whose hidden tests overlap visible
  files are refused.

## Known limits

- The model prompt is built by PatchProof's engine, so the baseline arm is "one attempt with PatchProof's prompt and
  evidence", not "a model used some other way".
- Tasks and hidden tests were written by the same people who built the tool.
- Runs against a hosted model vary from run to run; use repeats and report the spread.
- Only Python with pytest is supported. Cost covers model tokens only.
