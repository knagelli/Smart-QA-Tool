# Council Debate: Best Way to Stand Up EC2 for "No Server-Down Errors"

**Date:** 2026-09-19 · Status: design/analysis only — no AWS resources created, no code changed

---

## The question the council actually debated

"Top notch, no server-down errors" sounds like it points straight at the ALB+ASG architecture from the last doc — but the council's first job was to challenge whether that's actually the right lever, given what req2qa really is: a low-traffic, single-operator B2B tool where requests are occasional form submissions and background status polling, not high-concurrency web traffic.

**AWS/reliability architect:** Redundancy (multiple instances + load balancer) protects against exactly one failure mode: an individual EC2 instance or Availability Zone going down. It does nothing for the failure modes that most commonly take down a small, single-maintainer app like this: the application process itself crashing or hanging (an unhandled exception, Playwright/Chromium running out of memory), or a bad deploy taking the service down. A second instance behind a load balancer doesn't save you from either of those — if the code is broken, it's broken on both instances.

**SRE/site-reliability engineer:** Agreed, and this matters for prioritization. Before spending 3-4x your current infrastructure cost on ALB+ASG, the cheaper and higher-leverage fixes are: (1) a process supervisor (systemd with `Restart=always`, or a simple watchdog) so a crashed app process comes back up in seconds without any human noticing; (2) **EC2 Auto Recovery** — a built-in, free CloudWatch alarm action that automatically recovers your instance (same instance ID, same IP, same EBS volume) if the underlying AWS hardware fails or the instance stops responding at the system level. This is a single-instance feature — no load balancer, no second instance, no extra monthly cost beyond the CloudWatch alarm (which is a few cents) — and it covers the "hardware died" failure mode that people usually reach for a whole second instance to solve. Most people don't know this exists and jump straight to horizontal scaling.

**Cost/budget strategist:** This directly matters given the cost sensitivity already on record this session. EC2 Auto Recovery + systemd restart + a CloudWatch health-check alarm costs close to nothing extra over the single-instance plan (~$20-30/month). Full ALB+ASG was estimated at ~$50-65/month. If the actual goal is "don't let my one paying-attention-to-this-tool self get a 3am server-down surprise," the cheap tier addresses the two most probable causes (app crash, hardware fault) for a fraction of the cost of the tier that only addresses a much rarer one (AZ-level outage).

**Deployment/DevOps engineer:** The failure mode nobody's mentioned yet, and in practice the most common one for a solo-maintained app: a bad deploy. Neither a second instance nor EC2 Auto Recovery protects you from shipping broken code — both would just run the broken code. What actually protects against this is a deploy process with a rollback path (keep the previous working version on disk, a one-command revert) and, ideally, testing the new build on a health-check endpoint before it takes real traffic. This is a process discipline question, not an infrastructure-spend question, and it's currently unaddressed in every version of this plan so far.

**Security engineer:** One thing worth flagging regardless of which tier is chosen: whichever design goes forward, the instance(s) need a security group that only opens 443 (and 22/SSH restricted to your own IP, not 0.0.0.0/0) — this is independent of the HA discussion but easy to get wrong when standing up new infrastructure quickly, and Render has been handling this for you invisibly until now.

**Program manager, synthesizing:** This argues for a tiered rollout rather than a single up-front decision between "single instance" and "full ALB+ASG":

- **Tier 0 (recommended starting point):** Single EC2 instance + systemd auto-restart on crash + EC2 Auto Recovery (hardware-fault protection) + a basic health-check endpoint + a CloudWatch alarm that emails/texts you if the instance fails its health check. Cost: essentially the original single-instance estimate (~$20-30/month), no ALB. This covers app-crash and hardware-failure causes of downtime, which are the most statistically likely causes for this specific tool's profile.
- **Tier 1 (if Tier 0 proves insufficient, or a client SLA demands it):** Add the ALB + second instance with sticky sessions from the previous doc, specifically for AZ-level outage protection and to allow zero-downtime deploys (route traffic to the new instance, drain the old one, instead of restarting in place). Cost: the ~$50-65/month estimate.
- **Tier 2 (only if a specific need justifies it, e.g. many concurrent clients or a contractual resilience requirement):** Shared/external state so in-flight runs survive an instance failure, not just app availability.

**Devil's advocate, closing:** Don't let "top notch, no server-down errors" become a blank check for infrastructure spend that doesn't match this tool's actual traffic and failure profile. The honest, rigorous answer to "how do I avoid server-down errors" for a low-traffic solo-operated tool is mostly about **crash resilience and deploy safety**, which are nearly free, before it's about **horizontal redundancy**, which is a real ongoing cost multiplier. Recommend starting at Tier 0, watching actual failure incidents (if any occur) over the next month or two of real usage, and moving to Tier 1 only if a real incident or a specific client requirement justifies the jump — not preemptively.

## What this means concretely, if you want to proceed with Tier 0

- systemd unit file for the app (`Restart=always`, restart on failure) — a small, low-risk config addition, not application code.
- A simple `/health` endpoint in `main.py` (doesn't exist today) — small code change, would need your sign-off before I touch it.
- EC2 Auto Recovery + CloudWatch alarm — pure AWS console/CLI configuration, no app code involved, something you'd set up yourself (or I can write the exact console steps / CLI commands for you to run, keeping credentials off my hands as before).
- A basic deploy script with a rollback step (keep last-known-good build, one command to revert) — worth designing once you're actually ready to move off Render's git-push-to-deploy convenience.

## Sign-off needed

Does Tier 0 match what you actually want as the starting point, with Tier 1/2 as explicit later escalations rather than default scope? If yes, next step is a concrete provisioning walkthrough for Tier 0 specifically — still no AWS resources created or code touched until you confirm.
