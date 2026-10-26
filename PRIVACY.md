# PatchProof AI: privacy notice (DRAFT)

> **Draft for review by a qualified lawyer before external users are invited.** It describes what the software does
> today. It is not legal advice and makes no claim of compliance with GDPR, CCPA or any other law or standard. Replace the
> bracketed items.

**Operator:** [name and contact of the person or company running this instance]

PatchProof is software you can run yourself. This notice applies to an instance operated by the party above. If you run
your own instance, you are the operator and this describes what the software does.

## What PatchProof processes and why

| Data | Purpose | Where it goes | How long it is kept |
|---|---|---|---|
| GitHub account id and login | sign you in, attribute your runs | operator's database | until you delete your data |
| GitHub access and refresh tokens | read repositories you authorize; open a pull request if you approve one | operator's database, **encrypted** with the instance's secret key; sent only to GitHub | until you disconnect, delete your data, or they expire (8 hours / 6 months) |
| Source code fetched from a repository, or uploaded | run its tests before and after a patch | a temporary copy in the operator's database, and the test sandbox | project copies: 24 hours |
| Run records: test output, diffs, the issue text you typed, verdicts, file lists | show you the evidence | operator's database | 90 days by default (`PATCHPROOF_RUN_RETENTION_DAYS`), or until you delete them |
| Usage records: run counts, sandbox seconds, model tokens, verdicts | quotas and operating costs | operator's database | 90 days, or until you delete your data |
| Product events: a keyed hash of your account, event name, a few fixed values | understand use of the product | operator's database | 90 days, or until you delete your data |
| Server logs: IP address, request path and query (which can include repository names), time | security and debugging | operator's server and reverse proxy | as configured by the operator; **not** managed by PatchProof |

PatchProof does **not** store your repository's full history. It fetches one commit as a ZIP archive and keeps only the
supported Python and text files from it, within size limits.

## What is sent to others

- **GitHub** receives your sign-in, the requests needed to list repositories, branches and commits, and, only if you
  approve it, the branch and draft pull request.
- **The model provider (BharatCode)**, only in AI mode: the failing tests, the source files they reference (within a size
  budget), the test failure output and the issue text you wrote. The run page lists exactly which files were sent. For a
  **private repository**, PatchProof asks for your explicit consent first. GitHub tokens and secrets are never sent. The
  provider has its own policies, which apply to what it receives.
- No data is sold or sent to advertisers. There are no third-party analytics or tracking scripts.

## Untrusted code

Code from repositories runs only inside a disposable Docker container with no network access. It cannot read your GitHub
token or the operator's secrets.

## Cookies and browser storage

A session cookie (`pp_session`, HttpOnly, SameSite=Lax, Secure over HTTPS) keeps you signed in, and a short-lived cookie
(`pp_oauth_state`) protects the sign-in. In access-token mode, the token is kept in `sessionStorage` for the tab. There are
no advertising or analytics cookies.

## Your choices

- **Delete a run:** `DELETE /api/runs/{id}` (finished runs only).
- **Delete everything stored about you, and revoke PatchProof's GitHub access:** "Delete my data" on the GitHub page.
  It does not delete anything on GitHub, including a pull request already opened.
- **Sign out or disconnect GitHub:** on the GitHub page. You can also revoke the app on GitHub itself.
- **Access or export:** a run's `report.json` contains its full record. Requests the software cannot serve itself go to the
  operator at the contact above.

## Limits of this notice

- Backups the operator makes of the database keep data until those backups expire; deletion does not reach them.
- Secret redaction in logs and reports is best-effort pattern matching and cannot catch every secret.
- There has been no professional security audit.

*Items to settle before launch: the operator's identity and contact, the legal basis for processing where required,
international transfers (GitHub and the model provider are outside most operators' jurisdictions), a retention period for
operator-side logs and backups, a children's-data position, and a process for handling data requests.*
