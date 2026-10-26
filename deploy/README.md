# Deploying the PatchProof API

This runbook puts the PatchProof API, run store and Docker sandbox on a Linux VM and connects the Vercel-hosted
frontend to it. The frontend alone (see the main README, "Deployment") is only a UI preview.

**Status:** these files and steps have been validated locally on Windows with Docker Desktop: hosted mode, tokens,
CORS from a second origin, the Docker sandbox, a crash and restart, and a browser run of a bundled example. They
have **not** yet been run on a Linux VM. Treat the first deployment as the test of this runbook.

## What you need to decide or provide

| Item | Notes |
|---|---|
| A Linux VM | Ubuntu 24.04 LTS, 2 vCPU, 4 GB RAM and 20 GB disk is enough for the defaults (2 concurrent runs, each sandbox capped at 512 MB and 1 CPU). It must run Docker Engine with a normal daemon: not a container platform without Docker access, and not Docker-in-Docker. |
| A DNS name for the API | For example `api.<your-domain>`, with an A/AAAA record pointing at the VM. |
| Access tokens | One per person who may use the instance. |
| BharatCode API key (optional) | AI mode only, and only with a plan that includes API use. Without it, bundled examples, uploads with your own diff, and scripted patches still work. |

## 1. Prepare the host

```bash
# Docker Engine from Docker's apt repository: https://docs.docker.com/engine/install/ubuntu/
sudo apt-get update && sudo apt-get install -y git sqlite3 caddy   # Caddy: https://caddyserver.com/docs/install

sudo useradd --system --home-dir /var/lib/patchproof --shell /usr/sbin/nologin patchproof
sudo usermod -aG docker patchproof            # Docker access is root-equivalent: dedicated VM only
sudo install -d -o patchproof -g patchproof -m 0750 /var/lib/patchproof /var/lib/patchproof/data /var/lib/patchproof/work
sudo install -d -o root -g patchproof -m 0750 /etc/patchproof

# Firewall: SSH, HTTP and HTTPS only. Port 8000 stays on localhost.
sudo ufw allow OpenSSH && sudo ufw allow 80,443/tcp && sudo ufw enable
```

## 2. Install PatchProof

```bash
sudo git clone https://github.com/CodesbyRohit/PatchProofAI.git /opt/patchproof
cd /opt/patchproof && sudo git checkout <commit-or-tag-you-reviewed>

# uv, and a Python 3.13 that lives under /opt (the service cannot read home directories).
curl -LsSf https://astral.sh/uv/install.sh | sudo env UV_INSTALL_DIR=/usr/local/bin sh
cd /opt/patchproof/backend
sudo env UV_PYTHON_INSTALL_DIR=/opt/patchproof/python uv sync --locked --no-dev
sudo chown -R root:patchproof /opt/patchproof && sudo chmod -R g+rX,o-rwx /opt/patchproof

# Sandbox image (rebuild whenever backend/src/patchproof/sandbox/image changes).
sudo sh /opt/patchproof/scripts/build-sandbox.sh
```

## 3. Configure

```bash
cd /opt/patchproof/backend
sudo .venv/bin/python -m patchproof.access new      # once per person; give each token to its owner privately
sudo cp /opt/patchproof/deploy/patchproof.env.example /etc/patchproof/patchproof.env
sudo chown root:patchproof /etc/patchproof/patchproof.env && sudo chmod 0640 /etc/patchproof/patchproof.env
sudoedit /etc/patchproof/patchproof.env              # hashes, CORS origin, optional BharatCode key
```

`PATCHPROOF_HOSTED=1` makes the server refuse to start unless access tokens are configured, the Docker sandbox
is required, the unsandboxed override is off, and the data directory is set. A malformed CORS origin or token hash
also stops startup, with the reason in the log.

## 4. Run the service

```bash
sudo cp /opt/patchproof/deploy/patchproof.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now patchproof
systemctl status patchproof
curl -s localhost:8000/api/health    # {"ok":true,...}: the process answers
curl -s localhost:8000/api/ready     # {"ready":true,"checks":{"store":true,"docker_sandbox":true}}
```

## 5. HTTPS

```bash
sudo cp /opt/patchproof/deploy/Caddyfile /etc/caddy/Caddyfile
echo 'PATCHPROOF_API_HOST=api.<your-domain>' | sudo tee /etc/default/caddy
sudo systemctl edit caddy   # add:  [Service]  EnvironmentFile=/etc/default/caddy
sudo systemctl restart caddy
curl -s https://api.<your-domain>/api/ready
curl -s -o /dev/null -w '%{http_code}\n' https://api.<your-domain>/api/status   # 401 without a token
```

## 6. Connect the Vercel frontend

Choose one.

**A. Direct calls (recommended).** The browser calls the API host. The backend sees each visitor's address, so
rate limits are per visitor, and requests do not pass through Vercel's proxy limits.

1. Backend: `PATCHPROOF_CORS_ORIGINS=https://patch-proof-ai.vercel.app` (exact origin), then
   `sudo systemctl restart patchproof`.
2. Vercel: Project, Settings, Environment Variables: add `VITE_API_BASE_URL=https://api.<your-domain>` for
   Production. It is compiled into the JavaScript bundle and is public. It is an address, never a secret.
3. **Redeploy** the frontend: build-time variables only take effect in a new build.
4. Preview deployments have their own origins. Either add a specific preview origin to
   `PATCHPROOF_CORS_ORIGINS`, or do not set `VITE_API_BASE_URL` for Preview and leave previews unconnected.

**B. Same-origin proxy.** Add the rewrite from the main README ("Connecting the backend") to
`frontend/vercel.json`. No CORS setting is needed, but the backend sees Vercel's addresses (visitors share one
rate-limit budget), and Vercel's limits for proxied requests apply.

## 7. Verify the deployed system

1. `curl https://api.<your-domain>/api/ready` returns `"ready": true`.
2. Open the Vercel site. The workspace asks for an access token; enter one.
3. The status strip shows the Docker sandbox as available.
4. Run **Pagination drops the last page → Minimal fix: ceiling division**. The run page follows the real run to
   **Verified for tested cases**.
5. Download `report.md` from the run page.
6. `sudo systemctl restart patchproof`, reload the run page: the run and its verdict are still there.
7. Browser developer tools: no CORS errors in the console. API requests go to the API host (option A) or to
   `/api` on the Vercel domain (option B).

## Optional: GitHub sign-in and repositories

With a GitHub App, people sign in with GitHub and verify fixes on their own repositories. Public repositories
can be used without installing the app. Private ones need the app installed on them. PatchProof uses only the
signed-in user's token, which reaches just what both the user and the app can access, so no app private key is
needed.

**1. Register the app** (GitHub → Settings → Developer settings → GitHub Apps → New GitHub App):

| Field | Value |
|---|---|
| Callback URL | `https://<site>/api/auth/github/callback` (the address the browser uses for the API; see below) |
| Expire user authorization tokens | On (8-hour tokens with refresh tokens) |
| Request user authorization (OAuth) during installation | Optional |
| Webhook | Off (PatchProof does not use webhooks) |
| Where can this app be installed | "Only on this account" for a private instance, "Any account" for a public one |

Repository permissions (no organization or account permissions):

| Permission | Access | Why |
|---|---|---|
| Metadata | Read-only | Required by GitHub; repository names and visibility |
| Contents | Read-only | Branches, commits and the archive of the pinned commit |
| Contents | Read and write (only with pull requests) | Create the `patchproof/<run>` branch and its single commit |
| Pull requests | Read and write (only with pull requests) | Open the draft pull request |

Leave writes off unless you set `PATCHPROOF_GITHUB_PR_WRITES=1`; patches can always be downloaded.

**2. Configure the API host** (`/etc/patchproof/patchproof.env`):

```bash
GITHUB_APP_CLIENT_ID=Iv1....                 # the app's Client ID (not the App ID)
GITHUB_APP_CLIENT_SECRET=...                 # generate a client secret on the app page; a secret
GITHUB_APP_SLUG=your-app-name                # from the app's public URL; used for the install link
PATCHPROOF_GITHUB_CALLBACK_URL=https://<site>/api/auth/github/callback
PATCHPROOF_APP_URL=https://<site>            # where the browser returns after signing in
PATCHPROOF_SECRET_KEY=...                    # .venv/bin/python -m patchproof.accounts new-key; encrypts stored tokens
PATCHPROOF_GITHUB_ALLOWED_LOGINS=you,colleague   # who may sign in (required in hosted mode without access tokens)
PATCHPROOF_COOKIE_SECURE=1
# PATCHPROOF_GITHUB_PR_WRITES=1              # only if the app has the write permissions above
```

**3. Keep the site and the API same-site.** The session is an HttpOnly cookie. Browsers block cookies set by a
different site (for example an API on `api.example.com` called from `*.vercel.app`). Use either:

- the Vercel rewrite of `/api/*` (main README, "Connecting the backend"), so `<site>` is the Vercel domain; or
- your own domain for both, such as `app.example.com` (Vercel) and `api.example.com` (API), with
  `VITE_API_BASE_URL=https://api.example.com` and `PATCHPROOF_CORS_ORIGINS=https://app.example.com`.

**4. Verify:** open `<site>/github`, sign in, pick a repository and branch, run a verification with your own diff, and
check that another GitHub account cannot open the run's URL.

What the integration enforces:

- **Sign-in:** a single-use `state` bound to the browser by a cookie, plus PKCE. The identity comes from
  GitHub's `/user`, and an optional login allowlist applies.
- **Sessions:** only a hash of the session cookie is stored, and every state-changing request needs the
  session's CSRF token.
- **Tokens:** GitHub tokens are encrypted at rest and refreshed on the server. They never reach the browser,
  the sandbox or the model.
- **Isolation:** every run, project, report and pull request is owned by one user, and other users get 404.
- **Runs:** each run is pinned to one commit SHA. The archive is fetched only from `codeload.github.com`, is
  size-limited, and is filtered with the same path validation as uploads (symlinks, binaries, oversized files
  and Git LFS pointers are skipped and listed).
- **AI on private code:** AI mode on a private repository requires explicit consent to send selected files to
  the model API.
- **Pull requests:** only for runs verified for the tested cases, after the user approves the exact diff
  (checked by hash). Each one gets a new branch from the pinned commit and is opened as a draft. Retries reuse
  the same branch and pull request.

## Operations

- **Logs:** `journalctl -u patchproof -f`. Startup problems (invalid configuration, a second process on the
  same data directory) are logged with the exact reason. Removed leftover sandbox containers are logged at startup.
- **Backup:** `sudo -u patchproof sqlite3 /var/lib/patchproof/data/patchproof.sqlite3 ".backup '/var/lib/patchproof/backup-$(date +%F).sqlite3'"`
  (safe while the service runs). Copy backups off the host.
- **Restore:** stop the service, replace `patchproof.sqlite3` with a backup, start the service.
- **Upgrade:** `git fetch && git checkout <new commit>`, `uv sync --locked --no-dev` (with the same
  `UV_PYTHON_INSTALL_DIR`), rebuild the sandbox image if it changed, `systemctl restart patchproof`, then repeat
  the checks in section 7. A restart marks runs that were in progress as interrupted; it never invents results.
- **Rollback:** `git checkout <previous commit>`, `uv sync --locked --no-dev`, rebuild the sandbox image if it
  differs, restart. On Vercel, promote the previous deployment.
- **Revoke or rotate a token:** remove its hash from `PATCHPROOF_ACCESS_TOKEN_HASHES` (add the new one), restart.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `/api/ready` 503 with `docker_sandbox: false` | The service user cannot use Docker (`sudo -u patchproof docker version`), or the sandbox image is not built. |
| Every run reports a sandbox or mount error | Workspaces are not on a host path the Docker daemon can see. Keep `TMPDIR=/var/lib/patchproof/work` and `PrivateTmp=no`. |
| Browser console: CORS error | `PATCHPROOF_CORS_ORIGINS` does not exactly match the page origin (scheme, host, port, no trailing slash). |
| Workspace asks for a token again | The token is kept per browser tab; a new tab or a revoked token needs it again. |
| 413 on upload | The archive is over 4 MB; the request limit is 6 MB at both Caddy and the API. |

## Security scope

- Access tokens are bearer secrets shared with trusted people, not user accounts. Every token holder sees every
  run, including uploaded code and reports. Use this for a private demo or a small team, not public sign-up.
- Uploaded code and model patches only run in the Docker sandbox (no network, read-only root, all capabilities
  dropped, unprivileged user, CPU, memory, PID, file and time limits). Hosted mode cannot be started without it.
- The API process itself is not sandboxed from the host. It never executes project code, but it can start
  containers, so the VM should hold nothing else of value.
