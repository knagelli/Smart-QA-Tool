"""
Single place that decides how this app talks to Claude: either directly via
the Anthropic API (current, default behavior) or via AWS Bedrock's
Australia-only geographic inference profile (opt-in, for data-residency
compliant clients).

WHY THIS FILE EXISTS: before this, six separate call sites (four in
qa_engine.py, one in execute_engine.py) each constructed their own
`Anthropic(api_key=...)` client directly. Consolidating that into one
factory function means the provider decision, model ID, and AWS region are
each defined in exactly one place - easy to review, easy to audit, and
impossible for the six call sites to drift out of sync with each other.

DEFAULT BEHAVIOR IS UNCHANGED: if the AI_PROVIDER environment variable is
not set (or set to anything other than "bedrock"), this returns a plain
Anthropic client exactly as every call site did before this file existed.
Nothing about the running app's behavior changes until AI_PROVIDER=bedrock
is explicitly set - this is a refactor, not a migration, until that flag is
flipped.

ROLLBACK: if AI_PROVIDER=bedrock is set and something goes wrong, unsetting
it (or setting it back to "anthropic") on the next request reverts to the
original direct-Anthropic behavior. No code change or redeploy is needed -
this module re-reads the environment on every call rather than caching the
provider choice at import time, specifically so a live env var change takes
effect without a restart-dependent code path.
"""
import os

from anthropic import Anthropic, AnthropicBedrock

# The exact, verified Bedrock inference profile ID for Claude Sonnet 4.6,
# restricted to Australia (routes only to ap-southeast-2 Sydney and
# ap-southeast-4 Melbourne - confirmed directly in the AWS console on
# 2026-09-18, not assumed from documentation). Do not change this to
# "global.anthropic.claude-sonnet-4-6" or any other profile without
# re-confirming its destination regions - the whole point of this profile
# choice is that client data never leaves Australia.
BEDROCK_MODEL_ID = "au.anthropic.claude-sonnet-4-6"
BEDROCK_REGION = os.environ.get("AWS_REGION", "ap-southeast-2")


def get_client(api_key: str | None = None):
    """
    Returns an Anthropic-API-compatible client. `api_key` is accepted for
    backward compatibility with existing call sites (which currently read
    ANTHROPIC_API_KEY from main.py and pass it through) and is used only
    when the direct-Anthropic path is active; it is ignored entirely on the
    Bedrock path, which authenticates via standard AWS credential
    environment variables (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY),
    resolved automatically by boto3.
    """
    provider = os.environ.get("AI_PROVIDER", "anthropic").strip().lower()

    if provider == "bedrock":
        if not (os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY")):
            # Fail loudly and immediately rather than silently falling back
            # to direct Anthropic (which would defeat the entire purpose of
            # this migration - a client relying on AU-only processing must
            # never be silently routed elsewhere) or raising a confusing
            # error deep inside the AnthropicBedrock/boto3 call stack.
            raise RuntimeError(
                "AI_PROVIDER=bedrock is set, but AWS_ACCESS_KEY_ID and/or "
                "AWS_SECRET_ACCESS_KEY are not configured. Refusing to fall "
                "back to direct Anthropic for a request that expected "
                "AU-only Bedrock routing."
            )
        return AnthropicBedrock(aws_region=BEDROCK_REGION)

    return Anthropic(api_key=api_key)


def get_model_id() -> str:
    """
    Returns the model ID string each call site should pass to
    messages.create(model=...). On the Bedrock path this is the AU
    inference profile ID, not the bare model name - Bedrock requires the
    profile ARN/ID, not the underlying foundation-model ID, for this model.
    """
    provider = os.environ.get("AI_PROVIDER", "anthropic").strip().lower()
    if provider == "bedrock":
        return BEDROCK_MODEL_ID
    return os.environ.get("QA_MODEL", "claude-sonnet-4-6")
