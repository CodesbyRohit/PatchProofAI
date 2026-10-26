// Explanations for BharatCode failures recorded on a run (Run.model_error_kind).
// The kinds come from backend/src/patchproof/agent/client.py (ErrorKind).

export interface ModelErrorInfo {
  title: string;
  /** Whether this is an account/configuration problem rather than anything about the user's code. */
  access: boolean;
  explanation: string;
  steps: string[];
}

export const MODEL_ERRORS: Record<string, ModelErrorInfo> = {
  plan_required: {
    title: "Model access issue, not a code failure",
    access: true,
    explanation:
      "BharatCode accepted the API key but answered HTTP 402: this account's plan does not include API generation. No patch was produced, so nothing about your code was judged. The baseline test evidence on this page is still real.",
    steps: [
      "Open your BharatCode account page and check the plan and student-verification status.",
      "Model generation needs a Lite or Pro plan, or a verified student account.",
      "If the status looks right but generation still returns 402, contact BharatCode support with the HTTP status, x-request-id and timestamp. Never send them the API key.",
      "Meanwhile, the bundled demo examples run with scripted patches and need no API access.",
    ],
  },
  not_configured: {
    title: "No model configured on the server",
    access: true,
    explanation:
      "The server has no BharatCode API key, so the AI step was skipped. The baseline test evidence on this page is still real.",
    steps: [
      "Set BHARATCODE_API_KEY in backend/.env and restart the API server.",
      "Or use demo mode: bundled examples with scripted patches need no key.",
    ],
  },
  auth: {
    title: "The API key was rejected",
    access: true,
    explanation: "BharatCode answered HTTP 401. The configured key is invalid or revoked. This says nothing about your code.",
    steps: ["Check BHARATCODE_API_KEY in backend/.env and restart the API server."],
  },
  forbidden_model: {
    title: "The model is not available to this account",
    access: true,
    explanation: "BharatCode answered HTTP 403 for the configured model. This says nothing about your code.",
    steps: ["Set BHARATCODE_MODEL=qwen-3.8-27b, or check which models your plan includes."],
  },
  unknown_model: {
    title: "The configured model is not in the catalog",
    access: true,
    explanation: "The model in BHARATCODE_MODEL was not found in BharatCode's model catalog.",
    steps: ["Check BHARATCODE_MODEL against GET /models (see the status panel)."],
  },
  rate_limited: {
    title: "Rate limited by BharatCode",
    access: false,
    explanation: "BharatCode answered HTTP 429 after PatchProof's bounded retries.",
    steps: ["Wait a minute and start the run again."],
  },
  unavailable: {
    title: "BharatCode was unavailable",
    access: false,
    explanation: "BharatCode reported the model as busy or unavailable after bounded retries.",
    steps: ["Try again shortly."],
  },
  timeout: {
    title: "The model request timed out",
    access: false,
    explanation: "PatchProof does not retry read timeouts, to avoid resending large prompts.",
    steps: ["Try again, or reduce the project to the files relevant to the bug."],
  },
  network: {
    title: "Could not reach BharatCode",
    access: false,
    explanation: "The API server could not connect to the BharatCode API.",
    steps: ["Check the server's internet connection and try again."],
  },
  truncated: {
    title: "The model ran out of output budget",
    access: false,
    explanation: "The response stopped before the answer was complete (finish_reason: length).",
    steps: ["Raise BHARATCODE_MAX_OUTPUT_TOKENS on the server, or retry."],
  },
  malformed: {
    title: "The model's answer could not be used",
    access: false,
    explanation: "The response could not be parsed into a diagnosis and patch, even after one correction request.",
    steps: ["Retry the run. The model is not trusted to decide anything; malformed output is simply discarded."],
  },
  bad_request: {
    title: "BharatCode rejected the request",
    access: false,
    explanation: "BharatCode answered HTTP 400.",
    steps: ["Check the server logs for the provider's message."],
  },
};

export function modelErrorInfo(kind: string | null | undefined): ModelErrorInfo | null {
  return kind ? (MODEL_ERRORS[kind] ?? null) : null;
}
