import type { ComparisonCategory, VerdictStatus } from "../api/types";

export interface VerdictMeta {
  tone: "verified" | "failed" | "inconclusive" | "blocked";
  stampTop: string;
  stampBottom: string;
  label: string;
  /** What the verdict means, as defined by the backend's rules (README "Verdicts"). */
  meaning: string;
}

export const VERDICTS: Record<VerdictStatus, VerdictMeta> = {
  VERIFIED_FOR_TESTED_CASES: {
    tone: "verified",
    stampTop: "Verified",
    stampBottom: "for tested cases",
    label: "Verified for tested cases",
    meaning:
      "Every reproduced failure passed in every patched run, and every test that passed before still passes. This is evidence for the tested cases, not a proof of correctness.",
  },
  FAILED_VERIFICATION: {
    tone: "failed",
    stampTop: "Failed",
    stampBottom: "verification",
    label: "Failed verification",
    meaning:
      "Failure was observed: a target still fails, a previously passing test now fails, a test disappeared, collection broke, or the patch could not be applied.",
  },
  INCONCLUSIVE: {
    tone: "inconclusive",
    stampTop: "Inconclusive",
    stampBottom: "evidence incomplete",
    label: "Inconclusive",
    meaning:
      "No failure was observed, but the evidence has gaps: environment errors, flaky or skipped tests, timeouts, no reproducible failure, or no usable model response.",
  },
  BLOCKED_BY_SAFETY_POLICY: {
    tone: "blocked",
    stampTop: "Blocked",
    stampBottom: "by safety policy",
    label: "Blocked by safety policy",
    meaning:
      "The patch broke the safety policy (for example, it edited a test), or no sandbox was available for untrusted code. Nothing from the patch was executed.",
  },
};

export function verdictMeta(status: VerdictStatus | null | undefined): VerdictMeta | null {
  return status ? (VERDICTS[status] ?? null) : null;
}

export const CATEGORY_LABEL: Record<ComparisonCategory, string> = {
  fixed: "Fixed",
  still_failing: "Still failing",
  regression: "Regression",
  still_passing: "Still passing",
  newly_skipped: "Newly skipped",
  missing: "Missing after patch",
  new_test: "New test",
  unchanged_skip: "Skipped both times",
  flaky: "Flaky",
  environment: "Environment issue",
};

/** Lower sorts first: the rows a reviewer must look at come to the top of the ledger. */
export const CATEGORY_ORDER: Record<ComparisonCategory, number> = {
  regression: 0,
  missing: 1,
  still_failing: 2,
  newly_skipped: 3,
  environment: 4,
  flaky: 5,
  fixed: 6,
  new_test: 7,
  unchanged_skip: 8,
  still_passing: 9,
};

export type CategoryTone = "bad" | "warn" | "good" | "quiet";

export const CATEGORY_TONE: Record<ComparisonCategory, CategoryTone> = {
  regression: "bad",
  missing: "bad",
  still_failing: "bad",
  newly_skipped: "warn",
  environment: "warn",
  flaky: "warn",
  fixed: "good",
  new_test: "quiet",
  unchanged_skip: "quiet",
  still_passing: "quiet",
};
