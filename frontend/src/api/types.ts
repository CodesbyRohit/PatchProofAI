// Mirrors backend/src/patchproof/schemas.py and the response shapes in app.py.

export type VerdictStatus =
  | "VERIFIED_FOR_TESTED_CASES"
  | "FAILED_VERIFICATION"
  | "INCONCLUSIVE"
  | "BLOCKED_BY_SAFETY_POLICY";

export type PatchSource = "ai" | "user" | "scripted";
export type RunState = "queued" | "running" | "finished" | "error";
export type TestOutcome = "passed" | "failed" | "error" | "skipped";
export type RunHealth = "ok" | "no_tests" | "timeout" | "sandbox_error" | "report_missing" | "internal_error";
export type PatchProvenance = "live_model" | "user_supplied" | "scripted_example" | "benchmark_fixture";

export type ComparisonCategory =
  | "fixed"
  | "still_failing"
  | "regression"
  | "still_passing"
  | "newly_skipped"
  | "missing"
  | "new_test"
  | "unchanged_skip"
  | "flaky"
  | "environment";

export interface ProjectFile {
  path: string;
  content: string;
}

export interface ProjectSummary {
  file_count: number;
  total_bytes: number;
  source_files: string[];
  test_files: string[];
  other_files: string[];
  third_party_imports: string[];
  notes: string[];
}

export interface Project {
  id: string;
  name: string;
  origin: "example" | "upload" | "inline" | "github";
  trusted: boolean;
  source?: RepoSource | null;
  files: ProjectFile[];
  summary: ProjectSummary;
}

export interface ScriptedPatchInfo {
  id: string;
  label: string;
  description: string;
  expected_verdict: VerdictStatus | null;
}

export interface Example {
  id: string;
  title: string;
  scenario: string;
  description: string;
  issue: string;
  recommended_patch: string | null;
  project_id: string;
  patches: ScriptedPatchInfo[];
  summary: ProjectSummary;
}

export interface TestResult {
  nodeid: string;
  outcome: TestOutcome;
  duration_s: number;
  message: string | null;
  environment_issue: boolean;
}

export interface CollectionError {
  nodeid: string;
  message: string;
  kind: "code" | "environment";
}

export interface TestRun {
  label: string;
  sandbox: string;
  isolated: boolean;
  command: string[];
  health: RunHealth;
  exit_code: number | null;
  duration_s: number;
  tests: TestResult[];
  collection_errors: CollectionError[];
  stdout: string;
  stderr: string;
  detail: string | null;
}

export interface EvidenceRef {
  file: string;
  lines: string | null;
  observation: string;
}

export interface FileEdit {
  file: string;
  search: string;
  replace: string;
}

export interface Diagnosis {
  summary: string;
  root_cause: string;
  evidence: EvidenceRef[];
  rationale: string;
  potential_regressions: string[];
  additional_tests: string[];
  confidence: "low" | "medium" | "high";
  confidence_explanation: string;
}

export interface ModelCallInfo {
  model: string;
  latency_ms: number;
  request_id: string | null;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  total_tokens: number | null;
  finish_reason: string | null;
  http_attempts: number;
  purpose: string;
}

export interface PolicyFinding {
  rule: string;
  severity: "block" | "invalid";
  message: string;
  file: string | null;
}

export interface CandidatePatch {
  provenance: PatchProvenance;
  label: string;
  diff: string;
  changed_files: string[];
  edits: FileEdit[];
  policy_findings: PolicyFinding[];
  applied: boolean;
}

export interface TestComparison {
  nodeid: string;
  baseline: string;
  candidate: string;
  category: ComparisonCategory;
  is_target: boolean;
}

export interface Verdict {
  status: VerdictStatus;
  headline: string;
  reasons: string[];
  targets: string[];
  comparison: TestComparison[];
  decided_by: string;
}

export interface BaselineAnalysis {
  usable: boolean;
  targets: string[];
  passing: string[];
  flaky: string[];
  environment_failures: string[];
  reasons: string[];
}

export interface RunEvent {
  at: number;
  step: string;
  status: "started" | "completed" | "failed" | "skipped" | "info";
  message: string;
}

export interface Attempt {
  number: number;
  diagnosis: Diagnosis | null;
  diagnosis_source: PatchProvenance;
  patch: CandidatePatch | null;
  candidate_runs: TestRun[];
  verdict: Verdict | null;
  model_calls: ModelCallInfo[];
  error: string | null;
}

export interface Run {
  id: string;
  created_at: number;
  finished_at: number | null;
  state: RunState;
  project_id: string;
  project_name: string;
  source?: RepoSource | null;
  pull_request?: PullRequestRecord | null;
  patch_source: PatchSource;
  issue: string | null;
  sandbox: string | null;
  sandbox_isolated: boolean;
  events: RunEvent[];
  baseline_runs: TestRun[];
  baseline: BaselineAnalysis | null;
  attempts: Attempt[];
  verdict: Verdict | null;
  error: string | null;
  /** BharatCode error kind when the model step failed, e.g. "plan_required" (HTTP 402). */
  model_error_kind?: string | null;
  disclosures: string[];
}

export interface RunSummary {
  id: string;
  created_at: number;
  state: RunState;
  project_name: string;
  repository?: string | null;
  patch_source: PatchSource;
  verdict: VerdictStatus | null;
  headline: string | null;
}

export interface Status {
  version: string;
  model: {
    configured: boolean;
    model: string;
    base_url: string;
    catalog?: string[];
    catalog_source?: "catalog" | "documented";
    model_available?: boolean;
    error?: string;
  };
  sandbox: {
    backend: string;
    docker_available: boolean;
    detail: string;
    untrusted_execution: boolean;
    unsandboxed_override: boolean;
  };
  limits: {
    verification_runs: number;
    max_ai_attempts: number;
    run_timeout_s: number;
    per_test_timeout_s: number;
  };
}

export interface RunRequest {
  project_id?: string;
  example_id?: string;
  patch_source: PatchSource;
  scripted_patch_id?: string;
  user_patch?: string;
  issue?: string;
}

// -- GitHub -----------------------------------------------------------------------------------------

/** The exact repository revision a run was pinned to. */
export interface RepoSource {
  provider: "github";
  repo_id: number;
  full_name: string;
  private: boolean;
  ref: string | null;
  sha: string;
  html_url: string;
  skipped_count: number;
  skipped: string[];
}

export interface PullRequestRecord {
  state: "creating" | "created" | "failed";
  branch: string;
  base: string;
  patch_sha256: string;
  url: string | null;
  number: number | null;
  error: string | null;
  created_at: number;
}

export interface Me {
  authenticated: boolean;
  kind: "user" | "token" | "local" | null;
  login: string | null;
  csrf: string | null;
  github: { configured: boolean; install_url: string | null; pull_requests: boolean };
  token_sign_in: boolean;
}

export interface Installation {
  id: number;
  account: string;
  repository_selection: string | null;
}

export interface RepoSummary {
  id: number;
  full_name: string;
  private: boolean;
  description: string | null;
  default_branch: string | null;
  archived: boolean;
  html_url: string | null;
  pushed_at: string | null;
  /** "installed": readable through the PatchProof app; "public": a public repository, read without credentials. */
  access: "installed" | "public";
}

export interface RepoPage {
  repositories: RepoSummary[];
  total_count: number;
  page: number;
  has_more: boolean;
}

export interface Branch {
  name: string;
  sha: string;
  protected: boolean;
}

export interface CommitInfo {
  sha: string;
  message: string;
  author: string | null;
  date: string | null;
  html_url: string | null;
}

export interface GitHubRunRequest {
  repository: string;
  ref: string | null;
  sha: string;
  patch_source: "ai" | "user";
  user_patch?: string;
  issue?: string;
  allow_model_upload: boolean;
}

export interface PullRequestPreview {
  enabled: boolean;
  repository: string;
  source_sha: string;
  base: string | null;
  branch: string;
  changed_files: string[];
  diff: string;
  patch_sha256: string;
  existing: PullRequestRecord | null;
}
