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
import threading
import time

from anthropic import Anthropic, AnthropicBedrock, RateLimitError

# ---------------------------------------------------------------------------
# REQUEST-RATE PACING (2026-09-24). The Bedrock quota that actually binds is
# requests per minute (account applied value: 10 for Claude Sonnet 4.6
# cross-region, vs AWS default 10,000; tokens/minute is 6,000,000 and is not
# the constraint). It is ONE limit for the whole AWS account, shared by every
# client, every test execution and every test generation. Before this gate,
# each caller fired requests independently and the SDK silently retried each
# 429 twice more, so throttling fed itself and whole batches ended BLOCKED.
#
# Every client returned by get_client() now shares one process-wide gate:
#   - at most REQ2QA_RPM_LIMIT requests per minute, evenly spaced (no bursts);
#   - requests wait in first-come-first-served order, so concurrent runs from
#     different clients share capacity fairly;
#   - priority "interactive" (test generation - a person is waiting on the
#     screen) goes ahead of "background" (test execution), but background is
#     guaranteed a turn after every INTERACTIVE_STREAK_MAX interactive grants;
#   - the SDK's hidden retries are disabled (max_retries=0) and 429s are
#     retried here instead, through the same gate, with a shared cool-down so
#     every waiting request backs off together instead of piling on.
# The gate is per PROCESS. req2qa runs as a single uvicorn process today; if
# it is ever run with N worker processes, set REQ2QA_RPM_LIMIT to the per-
# worker share (total / N) or the workers together will exceed the quota.
# REQ2QA_RPM_LIMIT=0 turns all of this off (original behaviour, SDK retries
# back on). Set it to ~80-90% of the applied quota; raise it when AWS does.
# ---------------------------------------------------------------------------
def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


# Stage 0 (2026-09-25): these were hardcoded constants; made env-configurable
# so behaviour can be tuned per-deployment (or per onboarding scale) without a
# code change. Defaults are UNCHANGED from the previous hardcoded values -
# this alone must not change any running behaviour.
INTERACTIVE_STREAK_MAX = _env_int("REQ2QA_INTERACTIVE_STREAK_MAX", 3)


def rpm_limit() -> int:
    return _env_int("REQ2QA_RPM_LIMIT", 8)


def pacing_enabled() -> bool:
    return rpm_limit() > 0


class _RequestGate:
    def __init__(self):
        self._cv = threading.Condition()
        self._queues = {"interactive": [], "background": []}
        self._next_ticket = 0
        self._next_slot = 0.0          # earliest time the next grant may happen
        self._interactive_streak = 0
        self.stats = {"grants": 0, "throttled": 0, "waited_s": 0.0}

    def _head_is(self, ticket, priority) -> bool:
        inter, back = self._queues["interactive"], self._queues["background"]
        if inter and back and self._interactive_streak >= INTERACTIVE_STREAK_MAX:
            return priority == "background" and back[0] == ticket
        if inter:
            return priority == "interactive" and inter[0] == ticket
        return priority == "background" and bool(back) and back[0] == ticket

    def acquire(self, priority: str = "background", clock=time) -> float:
        """Blocks until this request may be sent. Returns seconds waited."""
        limit = rpm_limit()
        if limit <= 0:
            return 0.0
        priority = priority if priority in self._queues else "background"
        start = clock.time()
        with self._cv:
            ticket = self._next_ticket; self._next_ticket += 1
            self._queues[priority].append(ticket)
            try:
                while True:
                    now = clock.time()
                    if self._head_is(ticket, priority) and now >= self._next_slot:
                        self._queues[priority].pop(0)
                        self._next_slot = max(now, self._next_slot) + 60.0 / limit
                        if priority == "interactive":
                            self._interactive_streak += 1
                        else:
                            self._interactive_streak = 0
                        self.stats["grants"] += 1
                        waited = now - start
                        self.stats["waited_s"] += waited
                        self._cv.notify_all()
                        return waited
                    timeout = max(0.05, self._next_slot - now) if self._head_is(ticket, priority) else 1.0
                    self._cv.wait(timeout=timeout)
            except BaseException:
                if ticket in self._queues[priority]:
                    self._queues[priority].remove(ticket)
                self._cv.notify_all()
                raise

    def throttled(self, cooldown_s: float, clock=time):
        """A 429 came back: push the next grant back for everyone."""
        with self._cv:
            self.stats["throttled"] += 1
            self._next_slot = max(self._next_slot, clock.time() + cooldown_s)
            self._cv.notify_all()


def _env_cooldowns(name: str, default: list) -> list:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        parsed = [float(x.strip()) for x in raw.split(",") if x.strip()]
        return parsed or default
    except ValueError:
        return default


_GATE = _RequestGate()
# Stage 0 (2026-09-25): env-configurable, defaults unchanged.
RATE_RETRY_ATTEMPTS = _env_int("REQ2QA_RATE_RETRY_ATTEMPTS", 6)      # sends per logical request, incl. the first
RATE_RETRY_COOLDOWNS = _env_cooldowns("REQ2QA_RATE_RETRY_COOLDOWNS", [8, 15, 30, 45, 60])   # seconds, applied to the whole gate


class _PacedMessages:
    def __init__(self, inner, priority):
        self._inner, self._priority = inner, priority

    def create(self, **kwargs):
        last_exc = None
        for attempt in range(RATE_RETRY_ATTEMPTS):
            _GATE.acquire(self._priority)
            try:
                return self._inner.create(**kwargs)
            except RateLimitError as e:
                last_exc = e
                if attempt < len(RATE_RETRY_COOLDOWNS):
                    _GATE.throttled(RATE_RETRY_COOLDOWNS[attempt])
        raise last_exc


class _PacedClient:
    """Wraps an Anthropic/AnthropicBedrock client; only messages.create is
    gated, everything else passes through untouched."""
    def __init__(self, inner, priority):
        self._inner = inner
        self.messages = _PacedMessages(inner.messages, priority)

    def __getattr__(self, name):
        return getattr(self._inner, name)

# The exact, verified Bedrock inference profile ID for Claude Sonnet 4.6,
# restricted to Australia (routes only to ap-southeast-2 Sydney and
# ap-southeast-4 Melbourne - confirmed directly in the AWS console on
# 2026-09-18, not assumed from documentation). Do not change this to
# "global.anthropic.claude-sonnet-4-6" or any other profile without
# re-confirming its destination regions - the whole point of this profile
# choice is that client data never leaves Australia.
BEDROCK_MODEL_ID = "au.anthropic.claude-sonnet-4-6"
BEDROCK_REGION = os.environ.get("AWS_REGION", "ap-southeast-2")


def get_client(api_key: str | None = None, priority: str = "interactive"):
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
        # Any valid AWS credential source is acceptable here - explicit
        # AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY env vars, OR an IAM role
        # attached to this EC2 instance (the preferred, keyless option:
        # short-lived, auto-rotating credentials fetched automatically from
        # the instance metadata service, nothing to leak via env/history).
        # Previously this only ever checked for explicit env vars, which
        # would incorrectly refuse to run on an instance authenticating via
        # an attached IAM role alone - boto3's default credential chain
        # (used internally by AnthropicBedrock) already knows how to find
        # both kinds, so ask it directly rather than re-implementing that
        # check narrowly here.
        import boto3
        session = boto3.Session(region_name=BEDROCK_REGION)
        creds = session.get_credentials()
        if creds is None:
            # Fail loudly and immediately rather than silently falling back
            # to direct Anthropic (which would defeat the entire purpose of
            # this migration - a client relying on AU-only processing must
            # never be silently routed elsewhere) or raising a confusing
            # error deep inside the AnthropicBedrock/boto3 call stack.
            raise RuntimeError(
                "AI_PROVIDER=bedrock is set, but no AWS credentials could be "
                "resolved (checked env vars, IAM role, and other standard "
                "boto3 credential sources). Refusing to fall back to direct "
                "Anthropic for a request that expected AU-only Bedrock "
                "routing."
            )
        if pacing_enabled():
            return _PacedClient(AnthropicBedrock(aws_region=BEDROCK_REGION, max_retries=0), priority)
        return AnthropicBedrock(aws_region=BEDROCK_REGION)

    if pacing_enabled():
        return _PacedClient(Anthropic(api_key=api_key, max_retries=0), priority)
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
