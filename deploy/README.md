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
