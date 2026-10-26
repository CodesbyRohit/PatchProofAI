# Security policy

## Reporting a vulnerability

**Do not open a public issue for a security problem.**

> **Owner action needed before external users are invited:** enable *private vulnerability reporting* for this repository
> (Settings → Code security → Private vulnerability reporting) or add a monitored security email here. Until one of those
> exists, there is no confirmed private channel, and none is promised.

Please include the version or commit, what you did, what you expected, and what happened. Please do not access other
people's data, run denial-of-service tests against a hosted instance, or publish details before a fix.

There is no bug bounty. This is a small project with no dedicated security team, so no response time is guaranteed.

## What is in scope

PatchProof runs untrusted code, so the most important areas are:

- escaping the Docker sandbox, or reaching the host, the network, the GitHub token or the model key from test code;
- reading another user's runs, projects, reports or tokens;
- bypassing sign-in, the CSRF check, the quota, or the approval needed before a pull request;
- reading or writing a repository you did not authorize;
- path traversal, symlinks, archive bombs, or unexpected redirects during repository intake.

## What exists today

The controls and how they are tested are listed in the README ("Security model" and "GitHub repositories"). In particular,
`backend/scripts/mutation_check.py` disables 12 controls one at a time and requires a test to fail. **There has been no
independent security audit or penetration test**, and the GitHub integration has not yet been tested against real GitHub.

## Supported versions

Only the latest commit on `main` receives fixes.
