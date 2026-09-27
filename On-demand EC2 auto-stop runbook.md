# On-demand EC2 auto-stop - implementation + deploy runbook (2026-09-27)

## Policy (Kalyan's explicit requirement, 2026-09-27)
Stop the EC2 instance automatically when idle to minimize cost. A 1-2
minute restart is acceptable. BUT: run the instance 24/7 unconditionally
once there are 3 or more paying (tier 1/2) clients configured - below that,
apply idle-detection auto-stop.

This directly follows the council + deep-research report
`claude/on-demand-ec2-wake-architecture-deep-research-2026-09-27.md` from
earlier the same day, which established: a genuinely "few seconds" wake
from a fully stopped EC2 instance is not achievable on any standard AWS
building block (real floor is 30s-2min+); the safe (never-stop-mid-use)
half of this feature is nearly free to build since job_registry.py already
tracks everything needed; and the honest, buildable wake-side pattern is a
Route53-failover static holding page + wake Lambda, not a transparent
request-buffering proxy (no credible production pattern for that exists).

## What's implemented and tested (this sandbox - see Testing below)

### 1. `app/client_quotas.py`: `count_paying_clients()`
Returns `len(_load())` - every key in `client_quotas.json` is a real,
admin-configured tier 1/2 client (fail-closed entitlement as of
2026-09-21 means there's no more "unconfigured code that still counts"
ambiguity).

### 2. `app/main.py`: idle tracking + `GET /internal/idle-status`
- `_track_last_request` middleware (registered right after the existing
  `_security_headers` middleware): stamps a module-level `_last_request_at`
  timestamp on every request except `/internal/idle-status` and
  `/health*` themselves (so polling the status endpoint never looks like
  real traffic).
- `GET /internal/idle-status`: requires an `X-Internal-Token` header
  matching the `REQ2QA_INTERNAL_TOKEN` env var (fails closed - 503 if that
  env var is unset, 401 on a wrong/missing token). Returns:
  ```json
  {
    "active_jobs": 0,
    "last_request_age_seconds": 932.4,
    "paying_client_count": 1,
    "idle_quiet_threshold_seconds": 900,
    "safe_to_stop": true
  }
  ```
  `safe_to_stop` = `paying_client_count < 3 AND active_jobs == 0 AND
  last_request_age_seconds >= REQ2QA_IDLE_QUIET_SECONDS` (default 900s /
  15 minutes - matches the deep-research report's recommended quiet
  window, chosen to safely exceed the results page's normal 2.5s/4s
  polling cadence while allowing room for a backgrounded tab or a short
  break). `active_jobs` comes from `job_registry.REGISTRY.snapshot()`,
  which already correctly counts QUEUED (not just RUNNING) as active.

### 3. `aws/idle_stop_checker_lambda.py` (new directory: `aws/`)
A small, dependency-free (stdlib + boto3 only) Lambda meant to run on a
schedule (e.g. every 5 minutes). Checks the instance's actual EC2 state
first (does nothing if it's not currently "running"), then polls
`/internal/idle-status`, and calls `ec2:StopInstances` only if
`safe_to_stop` is true. Fails CLOSED on any error reaching the app - an
unreachable/erroring idle-status check always means "leave it running,"
never "assume it's safe to stop."

### 4. `aws/wake_lambda.py`
A second small Lambda, meant to sit behind an API Gateway HTTP API route.
Checks the instance state and calls `ec2:StartInstances` if it's
"stopped" (idempotent - a repeat call while already "pending"/"running"
is a no-op). No internal-token auth on this one deliberately - it's
reachable specifically by a client who can't reach the real app.

### 5. `aws/warming_up_page.html`
A static holding page (deploy to S3 static website hosting or behind
CloudFront) that Route53 failover routing shows a client when the real
instance's health check is failing (stopped, or genuinely down for any
other reason - this page can't and doesn't claim to distinguish those).
Calls the wake Lambda once, then polls the real app's homepage every 8s
and redirects there the moment it responds. **Before deploying, replace
its two placeholders** (`WAKE_LAMBDA_URL`, `APP_HOME_URL`) with the real
values.

## Testing
`app/test_idle_status.py` - 14 unit tests, all passing, run against the
REAL `app/main.py` via FastAPI's `TestClient` (not a reimplementation):
auth (missing/wrong/correct token, endpoint disabled when the token env
var is unset), the `paying_client_count < 3` boundary (confirmed 2 clients
still allows stopping, 3 does not - matches Kalyan's "minimum of three"
wording exactly), QUEUED-counts-as-active, terminal jobs don't block idle,
a recent request resets the quiet timer, and polling `/internal/idle-
status` itself never counts as activity.

## What YOU need to do on AWS (I have no AWS credentials/CLI access in this
sandbox to do any of this myself - confirmed: `aws` CLI is not present
here). This is a real, if moderate, infra project - budget an hour or two,
not five minutes.

### Step 0 - one-time prep
- Generate a random secret for `REQ2QA_INTERNAL_TOKEN` (e.g. `openssl rand
  -hex 32`). Set it as an environment variable on the EC2 instance (add to
  the systemd unit's `Environment=` line or an `.env`-style file the
  service already loads, matching however `DATA_DIR`/`RUN_LOG_DIR` are
  currently set per the existing `systemctl show req2qa -p Environment`
  output from earlier this session) AND note it down for Step 2's Lambda
  env var - they must match exactly.
- Tag the EC2 instance `Name=req2qa` (or note its actual tags/instance ID
  to adjust the IAM policy conditions below to match).
- Deploy the updated `app/main.py`/`app/client_quotas.py` (git pull +
  restart, same as every other deploy this session) BEFORE wiring up the
  Lambdas, so `/internal/idle-status` actually exists to poll.

### Step 1 - IAM
Create two IAM roles (or one shared role, if you prefer fewer moving
parts) for Lambda execution, each with the `AWSLambdaBasicExecutionRole`
managed policy (for CloudWatch Logs) plus the specific EC2 policy quoted
in each Lambda file's own docstring above (`ec2:StopInstances` for the
checker, `ec2:StartInstances` for the wake Lambda, both scoped to
`ec2:ResourceTag/Name = req2qa`).

### Step 2 - the stop-checker Lambda
- Create a Lambda function (Python 3.12+ runtime), paste in
  `aws/idle_stop_checker_lambda.py`.
- Set its environment variables: `REQ2QA_INSTANCE_ID`, `REQ2QA_HOST`
  (e.g. `req2qa.example.com` - no `https://` prefix), `REQ2QA_INTERNAL_TOKEN`
  (must match Step 0's value exactly).
- Attach the IAM role from Step 1.
- Create an EventBridge Scheduler rule (or a classic EventBridge
  "schedule" rule) firing every 5 minutes, targeting this Lambda. (5
  minutes keeps the worst-case "should have stopped by now but hasn't
  yet" window small relative to the 15-minute quiet threshold, without
  invoking the Lambda so often it matters for cost - Lambda invocations
  at this rate cost cents/month.)

### Step 3 - the wake Lambda + API Gateway
- Create a second Lambda function, paste in `aws/wake_lambda.py`.
- Set its `REQ2QA_INSTANCE_ID` environment variable and attach its IAM
  role from Step 1.
- Create an API Gateway HTTP API (cheaper/simpler than REST API for this)
  with a single `POST /wake` route, Lambda proxy integration to this
  function. Enable CORS on the route (`Access-Control-Allow-Origin: *` is
  fine here - see wake_lambda.py's docstring for why this endpoint is
  intentionally low-risk to leave open). Note the API's invoke URL for
  Step 5.

### Step 4 - the warming-up page
- Edit `aws/warming_up_page.html`: replace `WAKE_LAMBDA_URL` with Step
  3's invoke URL (plus `/wake`) and `APP_HOME_URL` with the real app's
  homepage URL.
- Host it: simplest is an S3 bucket with static website hosting enabled,
  this file uploaded as e.g. `index.html`. (A CloudFront distribution in
  front of the bucket is optional polish, not required for this to work.)

### Step 5 - Route53 health check + failover
- Create a Route53 health check against the real app's own health
  endpoint (e.g. `https://req2qa.example.com/` or a dedicated `/health`
  route if one exists - check main.py for what's already there before
  assuming) - this is what detects "the instance is stopped/unreachable"
  automatically, no polling from your side needed.
- Change the existing DNS record for req2qa's domain from a simple record
  to a **failover routing policy**: a PRIMARY record (the existing
  A/ALIAS to the EC2 instance's Elastic IP) associated with that health
  check, and a SECONDARY record pointing at the S3 bucket's/CloudFront's
  endpoint from Step 4. Route53 will serve the primary whenever its health
  check passes, and automatically fail over to the warming-up page the
  moment it starts failing (i.e. the instant the instance is stopped) -
  and fail back the moment the real app is healthy again.

### After it's all wired up - test it end to end before trusting it
- Manually stop the instance yourself once and confirm: (a) the domain
  serves the warming-up page within the health check's failure-detection
  window (typically under a minute, depending on the check interval/
  failure threshold you configure), (b) the page's wake call actually
  starts the instance (check the EC2 console), and (c) the page
  redirects to the real app once it's back up. This is the one part of
  this whole feature that's genuinely worth a real, deliberate test run
  before relying on it - a failover DNS misconfiguration could otherwise
  leave the domain stuck on the warming-up page even after the instance
  is back.
- Separately confirm the auto-stop side by watching CloudWatch Logs for
  the checker Lambda over a real idle period, rather than assuming the
  unit tests (which run against a synthetic in-memory state, not real
  wall-clock idle time) prove the live schedule works.

## Open follow-up, not done here
- The exact "3 paying clients" threshold and 15-minute quiet window are
  both simple constants (`< 3` in main.py's `safe_to_stop` computation,
  `REQ2QA_IDLE_QUIET_SECONDS` env var) - easy to tune later without a code
  redeploy for the quiet window (just change the env var and restart), or
  a small code edit for the client threshold if it ever needs to change.
- No alerting/notification was built for when the auto-stop Lambda
  actually stops the instance, or when the wake Lambda is triggered -
  worth adding (e.g. an SNS topic + email) if you want visibility into
  how often this is actually firing, rather than only finding out via
  CloudWatch Logs if you go looking.
