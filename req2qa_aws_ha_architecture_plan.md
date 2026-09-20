# req2qa on AWS: High-Availability Architecture (Before Bedrock Cutover)

**Date:** 2026-09-19 · Status: design/analysis only — no AWS resources created, no code changed

---

## 1. Why this is a bigger step than the original plan

The migration plan on file (`render-to-bedrock-australia-migration-plan-2026-09-18.md`) recommended a **single EC2 instance** for the first migration — lowest complexity, closest to how Render runs today. Real HA (multiple instances, auto-recovery, survives an instance failure) is a genuinely different architecture, not a bigger version of the same one. Worth being upfront: this adds real ongoing cost and, more importantly, a real engineering problem that a single instance never has to solve — **state**.

## 2. The core problem HA introduces: where does a running test's state live?

Today, a single Render/EC2 instance handles everything itself: when you kick off a live-execution run, that instance's own local disk (`DATA_DIR`) tracks its status, screenshots, and results while it runs in the background. With one instance, that's fine — there's only one place a run could be.

With multiple instances behind a load balancer, a real question appears: if instance A starts a long-running test execution, and a later status-check request gets routed by the load balancer to instance B, instance B has no idea that run exists. This isn't a hypothetical edge case — it's the normal behavior of a load balancer distributing requests, and it would break req2qa's live-status-polling UI on day one if not handled deliberately.

Two ways to resolve this, genuinely different in cost and effort:

### Option A — Sticky sessions (smaller change, real limitation)
The load balancer is configured to always route a given client's requests to the same instance for the life of a session (ALB's built-in sticky-session cookie support). Each instance keeps working exactly as it does today — no code changes to how `execute_engine.py` stores state.
- **Pros:** minimal engineering change, cheapest, fastest to stand up.
- **Real limitation, stated plainly:** if the specific instance handling a run crashes or is replaced mid-run, that run is lost — the "availability" HA is meant to provide doesn't actually protect an in-flight test execution, only protects the *website being reachable* for new requests and other users. This is a legitimate, honest partial-HA: it protects against "the app is down," not "this specific test run died."

### Option B — Shared/external state (the real fix, bigger build)
Run state, status, and results move off each instance's local disk onto shared storage every instance can read/write — either Amazon EFS (a shared network filesystem, closest to today's `DATA_DIR` pattern, smallest code change) or a proper job queue + database (bigger re-architecture, more correct long-term, most effort). With this, any instance can serve a status check for any run, and a run surviving an instance failure becomes possible (though "possible" still requires the run's own background process to be resumable, which `execute_engine.py` doesn't do today either — this would need its own follow-up look before promising full resilience for in-flight runs).
- **Pros:** genuine HA, including for in-flight runs (with further work).
- **Cost:** EFS has its own pricing (pay for storage + throughput, typically a few dollars/month at this scale, but non-zero); more importantly, this touches `execute_engine.py`'s and `main.py`'s file I/O assumptions, which is real code change and real testing, not just infrastructure.

**Recommendation:** Option A (sticky sessions) as the first HA step, paired with an honest caveat to yourself about what it does and doesn't protect. Option B is a legitimate later phase once you've decided the additional engineering effort is worth it for a specific reason (e.g., a client SLA that requires it), not a default to build into v1 of the AWS move.

## 3. Components needed (Option A path)

- **VPC with at least 2 subnets across 2 Availability Zones** in `ap-southeast-2` (Sydney) — AWS requires 2+ AZs for a load balancer and an Auto Scaling Group to actually provide redundancy; one AZ isn't real HA.
- **Application Load Balancer (ALB)** — routes traffic to healthy instances, terminates HTTPS, does the sticky-session cookie.
- **Auto Scaling Group (ASG)**, minimum 2 instances, spread across the 2 AZs — if one instance fails a health check, AWS automatically replaces it.
- **Target group + health check** — a simple HTTP health endpoint the ALB polls to know an instance is alive (req2qa doesn't currently have a dedicated health-check route; a trivial one would need adding — small code change, low risk).
- **Shared EBS or EFS for static assets/fixtures** that don't need per-run write access (lower priority, can defer).
- **Route 53** (optional) — only needed if you want AWS-side DNS health-check failover in addition to the ALB; likely unnecessary at this scale since the ALB itself already handles instance-level failover.

## 4. Updated cost estimate

On top of the original plan's single-instance estimate (~$20-30/month before AI usage):
- **Second EC2 instance**: roughly doubles the compute line — from ~$15-20/month to ~$30-40/month for two `t4g.small` instances.
- **Application Load Balancer**: a fixed hourly charge plus a small per-request/data-processed charge — typically **$16-20/month** minimum at low traffic, even with almost no requests, since ALB has a base hourly cost regardless of usage.
- **Total realistic estimate for Option A**: roughly **$50-65/month** before AI usage, versus ~$20-30/month for the single-instance plan and ~$10-15/month on Render today. This is a real, multi-times increase from where you are now — worth deciding deliberately rather than defaulting into it.

## 5. Sequencing relative to the Bedrock cutover

Standing up HA infrastructure is independent of the `AI_PROVIDER=bedrock` flip (same as Phase 1 of the AI-QA-platform work was independent of hosting) — the EC2 instances would call Bedrock or direct Anthropic identically either way. So the order you asked for (EC2/HA first, Bedrock flip after) is entirely workable: stand up the HA architecture serving today's direct-Anthropic behavior first, confirm it's stable, then flip the provider once, on infrastructure you've already validated — rather than changing two things in the same window.

## 6. What I'd need from you before any of this is built

1. Confirm Option A (sticky sessions, cheaper, honest partial-HA) versus committing to Option B (shared state, real in-flight-run resilience, bigger build) now.
2. Confirm the updated cost range above is acceptable before I turn this into a concrete build/provisioning plan.
3. As with the IAM/Bedrock setup, I won't touch your AWS account directly — the next step would be a step-by-step console guide or infrastructure-as-code script (e.g., a CloudFormation template) for you to run yourself, keeping credentials off my hands as we've done throughout.
