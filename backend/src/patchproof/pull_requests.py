"""Open a draft pull request for a verified patch, only after the user approved that exact patch.

Safety rules:
- only runs with a VERIFIED_FOR_TESTED_CASES verdict, only with PATCHPROOF_GITHUB_PR_WRITES=1;
- the user approves a SHA-256 of the diff they saw; if the run's patch differs, nothing is written;
- push permission is re-checked with the user's own token right before writing;
- the patch is re-applied to the pinned source commit through the same policy, and must reproduce the
  verified diff exactly;
- a new branch `patchproof/<run id>` is created from the source commit; the default branch is never written;
- repeating the request returns the same branch and pull request instead of creating new ones.
"""

from __future__ import annotations

import hashlib
import time

from .github import GitHubClient, GitHubError
from .projects import ProjectRejected
from .repo_intake import encode_like, raw_entries, read_repository_zip
from .schemas import PullRequestCreate, PullRequestRecord, Run, VerdictStatus
from .verification.patching import prepare_candidate


class PullRequestRefused(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def verified_patch(run: Run):
    if not run.verdict or run.verdict.status != VerdictStatus.verified:
        raise PullRequestRefused(409, "Only runs verified for the tested cases can become a pull request.")
    patch = next((a.patch for a in reversed(run.attempts) if a.patch and a.patch.applied and a.patch.diff), None)
    if patch is None:
        raise PullRequestRefused(409, "This run has no applied patch.")
    return patch


def patch_sha256(diff: str) -> str:
    return hashlib.sha256(diff.encode("utf-8")).hexdigest()


def create_pull_request(github: GitHubClient, token: str, run: Run, req: PullRequestCreate,
                        max_archive_bytes: int) -> PullRequestRecord:
    if run.source is None:
        raise PullRequestRefused(409, "Only runs on a GitHub repository can become a pull request.")
    patch = verified_patch(run)
    if patch_sha256(patch.diff) != req.patch_sha256:
        raise PullRequestRefused(409, "The patch differs from the one you approved. Review it again.")
    if run.pull_request and run.pull_request.state == "created":
        return run.pull_request  # idempotent

    source = run.source
    repo = github.repository(source.full_name, token)
    if not repo.get("permissions", {}).get("push"):
        raise PullRequestRefused(403, "Your GitHub account cannot push to this repository, so PatchProof cannot "
                                      "open a pull request for you. Download the patch instead.")
    base = source.ref or repo["default_branch"]
    branch = f"patchproof/{run.id}"
    record = PullRequestRecord(state="creating", branch=branch, base=base, patch_sha256=req.patch_sha256,
                               created_at=time.time())

    existing = github.get_ref(source.full_name, branch, token)
    if existing is None:
        # Rebuild the patched files from the pinned commit and prove they give the verified diff.
        archive = github.download_archive(source.full_name, source.sha, token if source.private else None,
                                          max_archive_bytes)
        try:
            files = {f.path: f.content for f in read_repository_zip(archive).files}
        except ProjectRejected as exc:
            raise PullRequestRefused(409, f"The source commit can no longer be read: {exc}") from exc
        candidate, new_files = prepare_candidate(files, provenance=patch.provenance, label=patch.label, diff=patch.diff)
        if new_files is None or candidate.diff != patch.diff or any(f.severity == "block" for f in candidate.policy_findings):
            raise PullRequestRefused(409, "Re-applying the patch to the source commit did not reproduce the verified "
                                          "change, so nothing was written.")
        originals = raw_entries(archive, set(candidate.changed_files))
        tree_sha = github.commit(source.full_name, source.sha, token)["commit"]["tree"]["sha"]
        entries = []
        for path in candidate.changed_files:
            blob = github.create_blob(source.full_name, encode_like(originals.get(path), new_files[path]), token)
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob})
        new_tree = github.create_tree(source.full_name, tree_sha, entries, token)
        message = (f"{req.title}\n\nPatch verified by PatchProof AI (run {run.id}) against {source.sha[:12]}: "
                   f"{run.verdict.headline if run.verdict else ''}")
        new_commit = github.create_commit(source.full_name, message, new_tree, source.sha, token)
        github.create_ref(source.full_name, branch, new_commit, token)
    else:
        # A retry after a partial failure: reuse the branch only if it is our single commit on the source commit.
        head = github.commit(source.full_name, existing["object"]["sha"], token)
        parents = [p.get("sha") for p in head.get("parents", [])]
        if parents != [source.sha]:
            raise PullRequestRefused(409, f"Branch {branch} already exists with other changes. Delete it and try again.")

    pull = github.find_pull(source.full_name, branch, token)
    if pull is None:
        try:
            pull = github.create_pull(source.full_name, req.title, req.body, branch, base, token)
        except GitHubError as err:
            record.state, record.error = "failed", err.message
            return record
    record.state, record.url, record.number = "created", pull.get("html_url"), pull.get("number")
    return record
