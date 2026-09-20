# req2qa Tier 0 EC2 Setup — Console-Only, Most-Secure Secrets Handling

**Date:** 2026-09-19 · For you to run yourself in the AWS web console — no credentials shared with me, nothing done on my end, no SSH/terminal needed, secrets never typed into any terminal-like window

**Secrets approach: AWS Secrets Manager + IAM role** (chosen over Session Manager) — your API key and other secrets are typed once into a structured console form, encrypted at rest, access-logged, and pulled by the instance automatically at boot using narrowly-scoped IAM permissions. No secret value ever appears in User data, in a shell, or in scrollback.

---

## How this avoids the terminal

The original walkthrough had you SSH into the instance and type install commands by hand. This version moves every one of those commands into a single **EC2 "User data" script** — a box you paste text into during Launch Instance in the console. AWS automatically runs that script the moment the instance boots, before you ever need to connect to it. You click through the console UI for everything else (security group, storage, CloudWatch alarms).

You will not need to open Terminal, iTerm, PowerShell, or any command-line app for the setup itself. The only remaining terminal-shaped step is *optional*: if something doesn't work and you want to look at logs, that requires SSH — but that's troubleshooting, not setup, and you can instead just re-launch a fresh instance if something goes wrong (they're cheap and disposable at this stage).

## What you're building

Same as before: one EC2 instance (`t4g.small`) running req2qa exactly as Render does today, with automatic process restart on crash, automatic instance recovery on hardware failure, and an alert to you if either happens. No load balancer, no second instance.

## Step 1 — Create the secret in Secrets Manager (do this first, console only)

1. Secrets Manager console → **Store a new secret**.
2. Secret type: **Other type of secret**.
3. Add three key/value pairs directly in the console form: `ANTHROPIC_API_KEY`, `CLIENT_ACCESS_CODES`, `DOWNLOAD_SIGNING_SECRET` — paste each real value into its own field.
4. Encryption key: leave the default (`aws/secretsmanager`) unless you have a specific KMS key you prefer.
5. Name the secret something recognizable, e.g. `req2qa/tier0/env`.
6. Skip rotation configuration for now (optional, can add later) → **Store**.
7. Open the secret you just created and note its **ARN** (starts `arn:aws:secretsmanager:...`) — you'll paste this into the IAM policy in Step 1b and into the User data script in Step 2.

## Step 1b — Create an IAM role that can read only that one secret (console only)

1. IAM console → **Roles** → **Create role**.
2. Trusted entity type: **AWS service** → Use case: **EC2** → Next.
3. Skip attaching a managed policy for now → Next → name it `req2qa-tier0-instance-role` → **Create role**.
4. Open the new role → **Add permissions** → **Create inline policy** → JSON tab, paste:
   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Effect": "Allow",
         "Action": "secretsmanager:GetSecretValue",
         "Resource": "arn:aws:secretsmanager:ap-southeast-2:<your-account-id>:secret:req2qa/tier0/env-XXXXXX"
       }
     ]
   }
   ```
   Replace the `Resource` value with the exact ARN you copied in Step 1.7. This scopes the instance to read *only* this one secret — nothing else in your AWS account.
5. Name the policy `req2qa-tier0-read-secret` → **Create policy**.

This role is what makes the "IAM role" half of the plan work: the instance authenticates as this role automatically (no key, no credential file anywhere on disk), and the role can do exactly one thing — read this one secret.

## Step 2 — Launch the instance (console clicks only)

1. EC2 console → **Launch Instance**, region **ap-southeast-2 (Sydney)**.
2. Name it something recognizable, e.g. `req2qa-tier0`.
3. AMI: **Amazon Linux 2023 (arm64)** — matches `t4g.small`'s Graviton architecture.
4. Instance type: `t4g.small`.
5. Key pair: **create one anyway and download it**, even though you won't use it for initial setup — keep it safe in case you ever need emergency SSH access later. (If you truly never want an SSH-capable key to exist, you can select "Proceed without a key pair," but that permanently forecloses console-free troubleshooting later — I'd recommend creating the key and just never using it.)
6. Network settings → Edit:
   - Allow HTTPS traffic (port 443) from the internet — check this box.
   - Allow SSH traffic — you can leave this **unchecked** entirely if you don't want port 22 open at all, since you won't be SSHing in for setup. (If you keep the key pair from step 5 for future emergencies, you'd need to open this later via **Security Groups → Edit inbound rules**, restricted to your own IP — but that's a future click, not needed now.)
7. Storage: default root volume is fine. Click **Add new volume** to attach a second EBS volume (1–2 GB, gp3) for `/var/data`.
8. Expand **Advanced details** at the bottom:
   - **IAM instance profile** — select the `req2qa-tier0-instance-role` you created in Step 1b. This is the step that connects the instance to its secret-reading permission.
   - Scroll to **User data** — paste the script from Step 3 below into it.
   - Scroll up slightly to **Auto-recover** — set this if the console offers it directly here; otherwise it's a one-time console step in Step 5 below.
9. Click **Launch instance**.

## Step 3 — The User Data script (paste this into the console, don't run it yourself)

This is the install/config work packaged as a script AWS executes automatically at boot — including pulling your secrets from Secrets Manager via the IAM role, so no secret is ever typed into a terminal, a browser shell, or User data itself. Paste this whole block into the **User data** field in Step 2.8:

```bash
#!/bin/bash
set -e

# Install dependencies (AWS CLI is preinstalled on Amazon Linux 2023, jq is not)
dnf update -y
dnf install -y python3.11 python3.11-pip git jq

# Clone the app
git clone <your repo URL> /opt/req2qa
cd /opt/req2qa
python3.11 -m pip install -r requirements.txt
python3.11 -m playwright install chromium

# Format and mount the second EBS volume at /var/data
# (device name is typically /dev/nvme1n1 on Nitro/Graviton instances like t4g —
#  the older /dev/xvdb naming from AWS docs doesn't apply here)
mkfs -t xfs /dev/nvme1n1 || true
mkdir -p /var/data
mount /dev/nvme1n1 /var/data || true
echo "/dev/nvme1n1 /var/data xfs defaults,nofail 0 2" >> /etc/fstab

mkdir -p /var/data/run_logs

# Pull secrets from Secrets Manager using the instance's IAM role — no key,
# no password, no manual typing. Replace the --secret-id with your secret's
# name or ARN from Step 1.
SECRET_JSON=$(aws secretsmanager get-secret-value \
  --region ap-southeast-2 \
  --secret-id req2qa/tier0/env \
  --query SecretString --output text)

{
  echo "ANTHROPIC_API_KEY=$(echo "$SECRET_JSON" | jq -r .ANTHROPIC_API_KEY)"
  echo "CLIENT_ACCESS_CODES=$(echo "$SECRET_JSON" | jq -r .CLIENT_ACCESS_CODES)"
  echo "DOWNLOAD_SIGNING_SECRET=$(echo "$SECRET_JSON" | jq -r .DOWNLOAD_SIGNING_SECRET)"
} > /opt/req2qa/.env
chmod 600 /opt/req2qa/.env

# systemd unit — the crash-restart piece
cat > /etc/systemd/system/req2qa.service <<'EOF'
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
EOF

systemctl daemon-reload
systemctl enable --now req2qa
```

Because the `.env` file is now generated automatically at boot from Secrets Manager, the service can start immediately in this same script — there's no separate manual step to populate secrets afterward. `chmod 600` restricts the file to the root user, matching the file-permission hygiene from the original walkthrough, done automatically rather than by you typing a command.

**Note on the EBS device name (`/dev/nvme1n1`):** on `t4g` (Nitro-based) instances, the second volume shows up as `/dev/nvme1n1`. If the mount step silently fails (the `|| true` prevents it from blocking the rest of the script), the instance still boots and runs — you'd just be missing persistent `/var/data` storage, recoverable by re-launching or by a later console-based EBS troubleshooting pass.

## Step 4 — Confirm it's running (console only)

1. EC2 console → your instance → **Status checks** tab: wait for both checks to show "passed" (a few minutes after launch — the User data script, including the Secrets Manager fetch, runs during this window).
2. The service starts itself automatically as the last line of the script. To check, since Step 6 below adds TLS via a reverse proxy, you can check `http://<instance public IP>:8000/healthz` directly in the meantime (that port isn't in your security group by default — temporarily add an inbound rule for port 8000 from your own IP if you want to check before Step 6 is done, then remove it after).

## Step 5 — EC2 Auto Recovery + alert (console only, exactly as before)

1. CloudWatch console → **Alarms** → **Create alarm**.
2. Metric: `StatusCheckFailed_System` for your instance.
3. Alarm action: **"Recover this instance"**.
4. Add a second alarm action: SNS notification to your email/phone.
5. Optionally: Route 53 health check or CloudWatch Synthetics canary pinging `/healthz`, with an SNS alert on failure — all console-configured.

## Step 6 — TLS/domain (console-leaning, one small script addition)

Point your domain at the instance's IP (Route 53 or your existing DNS provider's console). For HTTPS termination, the cleanest console-only-adjacent option is adding Caddy to the same User data script — Caddy auto-provisions Let's Encrypt certificates with zero manual certificate handling:

```bash
dnf install -y 'dnf-command(copr)'
dnf copr enable -y @caddy/caddy
dnf install -y caddy
cat > /etc/caddy/Caddyfile <<'EOF'
your-domain.com {
    reverse_proxy localhost:8000
}
EOF
systemctl enable --now caddy
```

This can be appended to the Step 3 User data script directly, so it's still zero manual typing — Caddy handles the certificate automatically once your DNS points at the instance.

## Step 7 — Parallel-run and cut over

Same as before: keep Render live on your real domain while validating this instance on its temporary IP/subdomain, then switch DNS once a real generate+execute test passes end-to-end.

## What's genuinely different from the original walkthrough

| Step | Original | This version |
|---|---|---|
| Install packages, clone repo, playwright install | Typed over SSH | Runs automatically via User data at boot |
| systemd unit file | Typed over SSH with a text editor | Embedded in User data, created automatically |
| Mount EBS volume | Typed over SSH | Automated in User data (device-name caveat above) |
| Secrets (.env) | Typed over SSH into a file | Pasted once into a Secrets Manager console form; instance retrieves them automatically at boot via IAM role — no typing on the instance, ever |
| Security group, CloudWatch, Auto Recovery | Console clicks | Unchanged — console clicks |
| New one-time setup this version adds | — | Creating the secret (Step 1) and the IAM role (Step 1b) — both console forms, a few minutes total |

## Why this is the most secure of the options considered

- The secret values themselves never appear in a terminal, a browser-based shell, shell history, or User data (User data is visible to anyone with `ec2:DescribeInstanceAttribute` permission on the account — a real exposure path the original walkthrough's `.env`-over-SSH approach didn't have, but that this Secrets Manager version avoids entirely).
- Secrets Manager encrypts the values at rest with KMS and logs every access via CloudTrail, giving you an audit trail if you ever need to check who/what read a secret.
- The IAM role is scoped with `secretsmanager:GetSecretValue` on exactly one secret ARN — if the instance were ever compromised, the blast radius is that one secret, not broader account access.
- Rotating a credential later is a one-time paste into the Secrets Manager console followed by a `systemctl restart req2qa` (via one Session Manager command, or simply rebooting the instance so User data re-runs) rather than reconnecting and hand-editing a file.

## What Tier 0 still deliberately doesn't do

No load balancer, no second instance, no protection for an in-flight test run if this instance's hardware fails (Auto Recovery reboots the same instance rather than losing it, but a run mid-flight at that exact moment is interrupted). Same honest limitation as before.

## Nothing has been done on my end

I haven't touched your AWS account or created any resource — this is still a walkthrough for you to execute. One open item: if your repo is private, `git clone` in the User data script will fail without credentials — let me know and I can extend the same Secrets Manager pattern to include a deploy token (a fourth key/value pair in the same secret), so that stays zero-typing too.
