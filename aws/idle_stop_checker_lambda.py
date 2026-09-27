"""
req2qa auto-stop checker (2026-09-27).

Deploy as an AWS Lambda function, triggered on a schedule (EventBridge
Scheduler rule, e.g. every 5 minutes - see the deploy runbook,
claude/on-demand-ec2-auto-stop-implemented-2026-09-27.md, for the exact
console/CLI steps). This is deliberately NOT run in-process inside req2qa
itself: a process stopping the EC2 instance it is currently running on is
inherently racy (the stop call, systemd's restart policy, and the process
teardown could all be mid-flight simultaneously) - an external, independent
checker avoids that entirely.

What this does, every time it runs:
1. If the instance is not currently "running" (already stopped/stopping/
   pending), does nothing - there's nothing to check or stop.
2. Otherwise, calls GET https://<REQ2QA_HOST>/internal/idle-status with the
   shared internal token, and if the response says safe_to_stop=true, calls
   ec2:StopInstances.
3. Fails closed on any error (network failure, non-200 response, malformed
   JSON): never stops the instance based on incomplete information. A
   missed stop cycle costs a few minutes of extra idle compute; a wrongful
   stop while a client is actually using it is the failure mode this whole
   feature exists to avoid, so an error here always resolves to "leave it
   running, try again next cycle."

Required Lambda execution role permissions (IAM policy, minimum):
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["ec2:DescribeInstances", "ec2:StopInstances"],
      "Resource": "*",
      "Condition": {"StringEquals": {"ec2:ResourceTag/Name": "req2qa"}}
    }
  ]
}
(Tag the EC2 instance with Name=req2qa, or adjust the condition/Resource to
match its actual instance ID/tags - scoping this to exactly one instance,
rather than "*" unconditionally, matters here: this Lambda's only job is to
stop ONE specific instance, and a broader policy would be a blast-radius
risk with no corresponding benefit.)

Required Lambda environment variables:
  REQ2QA_INSTANCE_ID   - the EC2 instance ID (i-0123456789abcdef0)
  REQ2QA_HOST           - the app's hostname, e.g. "req2qa.example.com"
  REQ2QA_INTERNAL_TOKEN - must match the same env var set on the EC2
                          instance itself (main.py's /internal/idle-status
                          check) - generate one random secret and set it in
                          BOTH places, never commit it to git.

Requires the `requests` library OR boto3's bundled urllib3 - uses only the
Python standard library (urllib) below specifically so this can be
deployed as a zero-dependency Lambda with no packaging/layer step beyond
boto3, which every Lambda runtime already includes.
"""
import json
import os
import urllib.request
import urllib.error

import boto3

INSTANCE_ID = os.environ["REQ2QA_INSTANCE_ID"]
HOST = os.environ["REQ2QA_HOST"]
INTERNAL_TOKEN = os.environ["REQ2QA_INTERNAL_TOKEN"]

ec2 = boto3.client("ec2")


def _instance_state() -> str:
    resp = ec2.describe_instances(InstanceIds=[INSTANCE_ID])
    return resp["Reservations"][0]["Instances"][0]["State"]["Name"]


def _fetch_idle_status() -> dict:
    req = urllib.request.Request(
        f"https://{HOST}/internal/idle-status",
        headers={"X-Internal-Token": INTERNAL_TOKEN},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        if resp.status != 200:
            raise RuntimeError(f"idle-status returned HTTP {resp.status}")
        return json.loads(resp.read().decode("utf-8"))


def lambda_handler(event, context):
    state = _instance_state()
    if state != "running":
        return {"action": "none", "reason": f"instance state is '{state}', not 'running'"}

    try:
        status = _fetch_idle_status()
    except (urllib.error.URLError, TimeoutError, ValueError, RuntimeError) as e:
        # Fail closed - see module docstring. Logged (visible in
        # CloudWatch Logs for this Lambda) so a persistent failure to
        # reach the app is noticeable, without ever taking the stop
        # action on incomplete information.
        print(f"idle-status check failed, leaving instance running: {e}")
        return {"action": "none", "reason": f"idle-status check failed: {e}"}

    if not status.get("safe_to_stop"):
        return {"action": "none", "reason": "not safe to stop", "status": status}

    ec2.stop_instances(InstanceIds=[INSTANCE_ID])
    print(f"Stopping {INSTANCE_ID} - idle-status confirmed safe_to_stop: {status}")
    return {"action": "stop_instances", "status": status}
