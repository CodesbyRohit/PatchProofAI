# Production readiness assessment

An audit of the application and the [runbook](README.md) for one Linux VM with Docker. **Status: not deployed anywhere.
Everything marked "tested" was tested locally on Windows with Docker Desktop, not on a Linux VM.** The Vercel site is a
UI preview; no API exists behind it.

## Findings

| Area | State | Evidence / what is missing |
|---|---|---|
| Sandbox isolation | tested | 7 hostile-code tests in the real Docker sandbox, run on every CI build: no network, read-only filesystem, unprivileged user, limits, no secrets |
| Restart recovery | tested | runs executing at a crash are marked as errors, never given a result; queued runs resume; leftover sandbox containers are removed on startup |
| Single process | enforced | hosted mode refuses a second process on the same data directory; run state and rate limits are in memory, so **one worker only** |
| Job durability | bounded | jobs run in threads in that one process. A crash loses in-flight runs (they are reported as errors). There is no distributed queue, so no second host or scaling out |
| Database | adequate for one host | SQLite on local disk, one connection with a lock. Needs persistent storage. Not suitable for several instances |
| Backup and restore | tested | SQLite's online backup is tested to restore runs and history; a restored database needs users to sign in again. No automated schedule exists; the runbook gives the command |
| Secrets | partial | GitHub tokens are encrypted at rest; the key and the app client secret live in `/etc/patchproof/patchproof.env`. Rotating the key forces everyone to sign in again (tested); there is no key-versioning or a secret manager |
| Sessions and CSRF | tested | HttpOnly SameSite=Lax cookie, hashed at rest, per-session CSRF token; Secure cookie enforced in hosted mode |
| Cross-user isolation | tested | every run, project, report and pull request is owner-scoped; other users get 404; mutation-checked |
| Data retention and deletion | tested | finished runs are deleted after 90 days, project copies after 24 hours, and users can delete a run or all their data. Operator backups and web-server logs are **not** covered |
| Rate limits and quotas | tested | per-principal run limit, bounded queue, 6 MB request limit, atomic monthly quota. There is no per-IP limit on sign-in or on the read endpoints |
| Denial of service | partial | each run is capped in CPU, memory, processes and time, and two run at once. Several users each starting runs can still queue the machine; the 25 MB archive cap is per request |
| TLS and origins | documented, not run | Caddy config and exact-origin CORS are written and validated, but no certificate has been issued |
| Same-site cookies | documented | the session cookie needs the site and API on one site (Vercel rewrite, or one parent domain) |
| GitHub App | **not validated** | tested only against a test double. Real permission behaviour, rate limits, installation flow and whether a user token can read public repositories where the app is not installed are unknown |
| Logging and monitoring | minimal | process logs go to the journal; `/api/health` and `/api/ready` exist; no metrics, alerts or log shipping. Access logs contain IPs and paths (including repository names) |
| Least-privilege GitHub permissions | documented | read-only Metadata and Contents; writes only with `PATCHPROOF_GITHUB_PR_WRITES=1` |
| Docker host exposure | inherent | the service user can start containers, which is root-equivalent on that host. Use a dedicated VM and treat it as the trust boundary |

## What to do before a controlled beta

1. Provision the VM and follow the runbook; run its section 7 end to end. This is the first test on Linux.
2. Register the GitHub App and run the real sign-in with one test account and a throwaway repository. Record what differs
   from the test double.
3. Add a scheduled backup (see below) and test a restore on a second machine.
4. Add an uptime check on `/api/ready`.
5. Fill in the bracketed items in [PRIVACY.md](../PRIVACY.md) and [TERMS.md](../TERMS.md) and have them reviewed. Enable a
   private security contact ([SECURITY.md](../SECURITY.md)).
6. Invite a handful of known users, with `PATCHPROOF_GITHUB_ALLOWED_LOGINS` set.

## Runbooks

**Backup (daily):** a cron entry for the `patchproof` user, then copy the file off the machine.

```bash
sqlite3 /var/lib/patchproof/data/patchproof.sqlite3 ".backup '/var/lib/patchproof/backup/patchproof-$(date +%F).sqlite3'"
find /var/lib/patchproof/backup -name '*.sqlite3' -mtime +14 -delete
```

Backups contain encrypted tokens, run records and the analytics salt. Protect them like the database, and delete them on
the schedule you promise in the privacy notice. Keep the secret key separately; a backup is not usable for tokens without it.

**Restore:** stop the service, replace `patchproof.sqlite3` with the backup, start the service, and check `/api/ready`. Users
sign in again.

**Monitoring:** poll `/api/ready` from outside; alert on non-200. Watch `journalctl -u patchproof` for
`removed … sandbox container(s)` (a crash happened), `retention: deleted`, and `unhandled error`. Check disk space on
`/var/lib/patchproof` and Docker's storage.

**Incident response (suspected sandbox escape, token leak or data exposure):**

1. Stop the service (`systemctl stop patchproof`) and, if the host may be compromised, the VM's network.
2. Revoke the GitHub App's client secret and generate a new one; rotate `PATCHPROOF_SECRET_KEY` (this invalidates stored
   tokens) and every access-token hash that may have leaked; rotate the BharatCode key.
3. Preserve logs and the database copy for analysis before cleaning up.
4. Rebuild the VM from the runbook rather than repairing it, and restore data from a backup you trust.
5. Tell affected users what was exposed, with the repository names where known, and what to do (revoke the app on GitHub,
   rotate any secret that was in an affected repository).
6. Record what happened and add a test for it.

**Rotating the secret key:** set a new `PATCHPROOF_SECRET_KEY` and restart. Stored GitHub tokens can no longer be decrypted,
so users sign in again. Rotate on schedule and after any suspected exposure.
