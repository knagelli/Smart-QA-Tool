"""
req2qa wake-on-demand Lambda (2026-09-27).

Deploy behind an API Gateway HTTP API (a single POST /wake route with a
Lambda proxy integration - see the deploy runbook for exact console/CLI
steps). Invoked by the static "warming up" page's own client-side
JavaScript (see warming_up_page.html in this same directory) when a client
lands on it because Route53 failover has routed them there instead of the
real app (i.e. the real EC2 instance's health check is currently failing -
either because it's stopped, or genuinely down for an unrelated reason).

What this does: starts the EC2 instance if it isn't already running or
already starting, and returns its current state so the page can show an
accurate, honest status rather than a generic "please wait." Idempotent by
design - calling this repeatedly (e.g. the page's own auto-retry) while the
instance is already "pending" or "running" is a safe no-op, not a repeated
StartInstances call each time.

Required Lambda execution role permissions (IAM policy, minimum) - same
instance-scoping rationale as idle_stop_checker_lambda.py:
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["ec2:DescribeInstances", "ec2:StartInstances"],
      "Resource": "*",
      "Condition": {"StringEquals": {"ec2:ResourceTag/Name": "req2qa"}}
    }
  ]
}

Required Lambda environment variables:
  REQ2QA_INSTANCE_ID - the EC2 instance ID (same value as the stop
                       checker's REQ2QA_INSTANCE_ID)

No internal-token auth on THIS endpoint (unlike /internal/idle-status) -
it's meant to be reachable by any client who lands on the warming-up page,
which by definition happens exactly when they can't reach the real app to
authenticate against anything. The only action it can take is "start an
instance that's already stopped or already starting" - low-risk, and rate-
limited naturally by API Gateway's own default throttling; add a WAF rule
or per-IP rate limit here only if abuse is actually observed, not
preemptively.
"""
import json
import os

import boto3

INSTANCE_ID = os.environ["REQ2QA_INSTANCE_ID"]

ec2 = boto3.client("ec2")

_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}


def lambda_handler(event, context):
    resp = ec2.describe_instances(InstanceIds=[INSTANCE_ID])
    state = resp["Reservations"][0]["Instances"][0]["State"]["Name"]

    if state == "stopped":
        ec2.start_instances(InstanceIds=[INSTANCE_ID])
        state = "pending"  # reflects the transition just triggered, not a re-query

    return {
        "statusCode": 200,
        "headers": {**_CORS_HEADERS, "Content-Type": "application/json"},
        "body": json.dumps({"instance_state": state}),
    }
