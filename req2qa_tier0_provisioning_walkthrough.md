# req2qa Tier 0 EC2 Setup — Step-by-Step Walkthrough

**Date:** 2026-09-19 · For you to run yourself in the AWS console/CLI — no credentials shared with me, nothing done on my end

---

## Good news first: no app code changes needed

I checked `main.py` and `render.yaml` before writing this. `/healthz` already exists (returns `{"status": "ok"}`) and `render.yaml` already documents your exact build/start commands and environment variables — Tier 0 reuses all of it as-is. Nothing in `app/` needs to change for this step.

## What you're building

One EC2 instance (`t4g.small` per the earlier cost estimate) running req2qa exactly as Render does today, plus: automatic process restart if the app crashes, automatic instance recovery if the underlying AWS hardware fails, and an alert to you if either happens. No load balancer, no second instance — that's Tier 1, deliberately deferred.

## Step 1 — Launch the instance

1. EC2 console → Launch Instance, region **ap-southeast-2 (Sydney)**.
2. AMI: Amazon Linux 2023 (arm64, to match `t4g.small`'s Graviton architecture).
3. Instance type: `t4g.small` (2 vCPU, 2 GB RAM).
4. Key pair: create/select one for SSH access.
5. Network: default VPC is fine for now; **security group** — only open:
   - Port 443 (HTTPS) from `0.0.0.0/0`
   - Port 22 (SSH) restricted to **your own IP only**, not `0.0.0.0/0` (the security engineer's flag from the council debate — easy to get wrong when moving quickly)
6. Storage: root volume is fine at default; attach a separate EBS volume (1-2 GB, gp3) for `/var/data`, matching your current Render disk's role.
7. Under **Advanced details → "Auto-recover"** (or configure via CloudWatch after launch, step 4 below) — this is the EC2 Auto Recovery feature the council recommended.

## Step 2 — Install dependencies (SSH into the instance)

```bash
sudo dnf update -y
sudo dnf install -y python3.11 python3.11-pip git
git clone <your repo> /opt/req2qa
cd /opt/req2qa
python3.11 -m pip install -r requirements.txt
python3.11 -m playwright install chromium
```

Mount the EBS volume at `/var/data` (format it first if new), matching `DATA_DIR=/var/data` from `render.yaml`.

## Step 3 — systemd unit (the crash-restart piece)

Create `/etc/systemd/system/req2qa.service`:

```ini
[Unit]
Description=req2qa FastAPI app
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/req2qa
Environment="DATA_DIR=/var/data"
Environment="RUN_LOG_DIR=/var/data/run_logs"
Environment="PLAYWRIGHT_BROWSERS_PATH=0"
EnvironmentFile=/opt/req2qa/.env
ExecStart=/usr/bin/python3.11 -m uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Put your real secrets (`ANTHROPIC_API_KEY`, `CLIENT_ACCESS_CODES`, `DOWNLOAD_SIGNING_SECRET`) in `/opt/req2qa/.env` (KEY=value lines), `chmod 600` it — same secrets Render's dashboard holds today, just in a file only root can read instead of Render's env-var UI.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now req2qa
sudo systemctl status req2qa   # confirm it's running
```

`Restart=always` is the whole crash-resilience piece — if the process dies for any reason, systemd brings it back within 5 seconds, no human involved.

## Step 4 — EC2 Auto Recovery + alert (the hardware-failure + notify-you piece)

1. CloudWatch console → Alarms → Create alarm.
2. Metric: `StatusCheckFailed_System` for your instance (this is the metric that reflects underlying AWS hardware/hypervisor problems, distinct from your app's own health).
3. Alarm action: **"Recover this instance"** (built-in EC2 action) — AWS automatically recovers the instance (same instance ID, same IP, same attached EBS volume) if this fires.
4. Add a second alarm action: SNS notification to your email/phone, so you know it happened even though it self-healed.
5. Separately, create a lightweight external check on `/healthz` — the simplest option is a CloudWatch Synthetics canary or even just Route 53 health check pinging `https://<your-domain>/healthz` every 1-5 minutes, with an SNS alert on failure. This catches the case systemd can't: the process is "running" but hung/unresponsive rather than crashed.

## Step 5 — TLS/domain

Point your domain at the instance (or, more simply, keep it fronted by something handling HTTPS termination — a bare EC2 instance doesn't have this by default the way Render does). The simplest Tier-0-consistent option: install Caddy or nginx with Let's Encrypt on the instance itself for automatic HTTPS, proxying to uvicorn on port 8000. This is a small additional step Render was doing invisibly for you.

## Step 6 — Parallel-run and cut over

Keep Render live and serving your real domain while you validate this instance on a temporary URL/IP. Once you've run a real generate + execute test end-to-end against it successfully, switch DNS, then decommission Render.

## What Tier 0 deliberately does NOT do

No load balancer, no second instance, no protection for an in-flight test run if this specific instance's hardware fails (Auto Recovery replaces the hardware but the instance does briefly reboot — a run in progress at that exact moment would be interrupted, though the app itself comes back up automatically afterward). That's the honest, stated limitation from the council debate — Tier 1 is the answer if that specific gap ever matters enough to justify the added cost.

## Nothing has been done on my end

I haven't touched your AWS account, created any resource, or changed any code — this is a walkthrough for you to execute. Let me know if you want me to also prepare this as a single provisioning script (e.g., a `cloud-init`/user-data script that automates steps 2-3) instead of manual steps, or if you'd rather do it manually the first time to understand each piece.
