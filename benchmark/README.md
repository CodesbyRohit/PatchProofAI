# PatchProof benchmark

A deterministic set of Python bug-fixing cases, each with one scripted candidate patch, run through the
real PatchProof verification engine and Docker sandbox. It measures whether the **verifier** reaches the
right verdict. It does not measure how good a model is at writing fixes: no model is called, and every
patch is labelled `benchmark_fixture`, never as AI output.

```bash
cd backend
uv run python -m patchproof.benchmark                           # all cases; Docker sandbox required
uv run python -m patchproof.benchmark --only rt-clamp-max-only   # selected cases
uv run python -m patchproof.benchmark --out /tmp/bench           # write results elsewhere
```

Results go to `benchmark/results/results.json` (full record per case: verdict, reasons, per-run counts and
exit codes, per-test comparison, resource-limit events) and `results.md` (summary table, resource-limit
warnings, mismatch details). The command exits `0` when every case matches its expectation, `1` when any
case does not, and `2` when the benchmark cannot run (for example, Docker is unavailable). `--allow-local`
runs without Docker as a local subprocess; results are labelled "NOT isolated" and can differ, because
they depend on the packages installed on the host.

## Cases

Each `cases/<id>/` holds `case.json` (category, patch, expected outcome and the reason for it) and
`project/` (source plus a pytest suite). Expected outcomes were written from the verdict rules in the
main README before the cases were run. When a run disagrees, the mismatch is reported with the engine's
reasons; expectations are only changed when the written policy justifies it (see "Findings" below).

| Category | Cases | What it checks |
|---|---|---|
| `correct_fix` | 8 | A minimal correct fix is VERIFIED, with no regressions |
| `regression_trap` | 5 | A plausible fix that breaks previously passing tests is FAILED, with the exact regression count. Includes a CPU-only infinite loop caught by the per-test timeout |
| `incomplete_fix` | 2 | A fix that leaves a target failing is FAILED |
| `prohibited_edit` | 5 | Editing tests, `conftest.py` or `pytest.ini`, adding an `os` capability, or escaping the project is BLOCKED |
| `invalid_patch` | 2 | A patch that does not apply or does not parse is FAILED |
| `collection_break` | 1 | A patch that breaks test collection is FAILED |
| `inconclusive` | 3 | Missing dependency, no reproducible failure, and a patch that makes the target skip are INCONCLUSIVE |
| `flaky` | 2 | A failure that does not reproduce, and a correct fix next to a flaky test, are INCONCLUSIVE |
| `resource_limit` | 1 | A patch that exhausts the sandbox memory limit is INCONCLUSIVE, and the run must actually be killed (exit 137) |

### Explicit runner outcomes

Two kinds of case depend on how the sandbox behaves at runtime, so each also checks that the intended
runner event happened, instead of only checking the verdict:

- **Resource limits.** `expected.resource_limit_kill` states whether a patched run must end killed by a
  resource limit (`rt-digits-infinite-loop`: yes; `rt-power-of-two-hang`: no, its hang must be caught by
  the per-test timeout). If the event does not happen as stated, the case is a mismatch even when the
  verdict happens to agree. Every kill is also listed as a resource-limit warning in the report.
- **Flakiness.** A flaky test needs two identical baseline runs to disagree, which random failures
  cannot guarantee. Flaky cases use a time-dependent test (a common real cause of flakiness): the test
  file contains `__PATCHPROOF_FLAKE_DEADLINE__`, which the harness replaces with an absolute time
  `flaky_deadline_s` (15 s) after the case starts. Before the deadline the test sleeps past it and fails;
  after it, the test passes. So baseline run 1 fails it, and baseline run 2, which can only start after
  run 1 has finished, passes it. The only assumption is that the first baseline container starts its
  tests within 15 s. These cases raise their per-test timeout to 30 s (`per_test_timeout_s`) so the sleep
  is never cut short. If the test does not flip, the case reports "precondition not met" rather than a
  misleading verdict mismatch.

## Measured results

Results are not committed. Run the command above to produce them locally, or open the latest CI run:
the backend job writes the results table to its job summary and uploads `results.json` and `results.md`
as the `benchmark-results` artifact. Numbers are only ever taken from an actual run.

## Findings

The first runs (2026-10-26) produced two mismatches. Both are resolved, and the history is kept here:

- **`fl-flaky-bystander` (engine changed).** A correct fix next to a flaky, unrelated test was VERIFIED.
  The engine excluded the flaky test but did not treat it as missing evidence, while the README's
  verdict table listed flaky tests as a reason for INCONCLUSIVE. Decision: the documented policy stands.
  `decide()` now records baseline flaky tests as an evidence gap, so such runs are INCONCLUSIVE (failure
  evidence still takes precedence). Regression tests: `test_flaky_bystander_makes_a_clean_fix_inconclusive`
  and `test_failure_evidence_still_wins_over_a_flaky_bystander` in `backend/tests/test_verdict.py`.
- **`rt-digits-infinite-loop` (expectation changed).** The patched loop never ends *and* grows a list,
  so the container hits the 512 MB memory limit and is killed (exit 137) before the 10 s per-test
  timeout. The original expectation (FAILED, 2 regressions) wrongly assumed a CPU-only hang. Decision: a
  run killed by a resource limit produces no reliable per-test evidence, so INCONCLUSIVE is correct, and
  the kill is reported as a serious warning. The case now expects INCONCLUSIVE plus an actual kill.
  `rt-power-of-two-hang`, a CPU-only hang added with its expectation set before it ran, covers the
  per-test-timeout path and is FAILED with 2 regressions.

## Scope and limitations

- The cases are small and synthetic: single-module projects with a few tests each. They exercise the
  verdict rules, patch policy and sandbox, not realistic codebases or model behaviour.
- A VERIFIED verdict here means the same as everywhere in PatchProof: verified for the tested cases only.
- Two cases depend on runtime behaviour: the flaky deadline (the first container must start within
  15 s) and the memory case (the 512 MB limit must be reached before the 10 s per-test timeout; the
  loop fills it in a few seconds on the machines tested). Both check that the intended runner event
  actually happened, so a slow or unusual host shows up as a named mismatch, not a silently different result.
- The sandbox ships only pytest and pytest-timeout, so dependency behaviour is tested only as
  "missing" (`in-missing-dependency`).
- Live model runs are out of scope; a live BharatCode diagnosis-and-patch run has still not been observed.
