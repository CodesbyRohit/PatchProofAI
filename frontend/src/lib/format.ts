import type { PatchSource } from "../api/types";

export function formatTime(epochSeconds: number): string {
  return new Date(epochSeconds * 1000).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function formatDuration(seconds: number): string {
  if (seconds < 1) return `${Math.round(seconds * 1000)} ms`;
  if (seconds < 60) return `${seconds.toFixed(1)} s`;
  return `${Math.floor(seconds / 60)} min ${Math.round(seconds % 60)} s`;
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`;
}

/** "tests/test_pagination.py::test_first_page" -> { file: "tests/test_pagination.py", name: "test_first_page" } */
export function splitNodeId(nodeid: string): { file: string; name: string } {
  const i = nodeid.indexOf("::");
  return i === -1 ? { file: "", name: nodeid } : { file: nodeid.slice(0, i), name: nodeid.slice(i + 2) };
}

export const SOURCE_LABEL: Record<PatchSource, { short: string; long: string }> = {
  scripted: { short: "Demo · scripted patch", long: "Scripted demo patch written by the PatchProof maintainers. Not model output." },
  ai: { short: "Live model · BharatCode", long: "Diagnosis and patch requested from the BharatCode model API." },
  user: { short: "User-supplied patch", long: "A unified diff supplied by the user." },
};
