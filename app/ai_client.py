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


class GateCancelled(Exception):
    """Raised out of _RequestGate.acquire (2026-09-25, see the Run Control
    Center cancel mechanism) when the caller's own cancel_event is set while
    it is still waiting in line for its RPM turn - i.e. BEFORE it was ever
    granted a slot, so the request this would have paid for is never sent at
    all. This is strictly opt-in: acquire() only checks cancel_event when a
    caller explicitly passes one (only the live-execution path does today -
    see execute_engine.py); every other caller's behavior is completely
    unaffected, so this cannot regress generation or any execution run that
    doesn't pass a cancel_event. execute_engine.py catches this and re-raises
    its own ExecutionCancelled so main.py has one exception type to handle
    regardless of whether the cancellation was caught here (mid-queue) or
    in the step loop (before the next call is even attempted)."""
    pass


class _RequestGate:
    def __init__(self):
        self._cv = threading.Condition()
        # "interactive" stays a plain FIFO list of tickets (unchanged - see
        # the fairness note below for why only "background" needed this).
        # "background" entries are (ticket, client_key) tuples so the shared
        # execution capacity can be handed out fairly across clients rather
        # than strict first-come-first-served, which would let one client's
        # big batch push every other client's queued run to the back for the
        # whole batch's duration - see claude/council-review-worst-case-rpm-
        # mitigation-2026-09-25.md, item 3 ("per-client fairness"). A single
        # client (client_key == "" for every entry, e.g. execution requests
        # that don't pass one) degenerates back to exact FIFO, so this is a
        # no-behavior-change default when nothing supplies a client_key.
        self._queues = {"interactive": [], "background": []}
        self._last_background_key = None
        self._next_ticket = 0
        self._next_slot = 0.0          # earliest time the next grant may happen
        self._interactive_streak = 0
        self.stats = {"grants": 0, "throttled": 0, "waited_s": 0.0}

    def _background_head(self):
        """Round-robin fair pick for the background queue: cycle through the
        distinct client_keys currently waiting, starting just after whichever
        key was granted last, and within the next key in that rotation grant
        its EARLIEST-arrived ticket. Returns None if the background queue is
        empty. Falls back to plain FIFO when every waiting entry shares one
        client_key (including the common "no key supplied" case)."""
        back = self._queues["background"]
        if not back:
            return None
        keys_in_order = []
        for _, k in back:
            if k not in keys_in_order:
                keys_in_order.append(k)
        if len(keys_in_order) == 1:
            return back[0][0]
        if self._last_background_key in keys_in_order:
            start = keys_in_order.index(self._last_background_key)
            rotated = keys_in_order[start + 1:] + keys_in_order[:start + 1]
        else:
            rotated = keys_in_order
        next_key = rotated[0]
        candidates = [t for t, k in back if k == next_key]
        return min(candidates)

    def _head_is(self, ticket, priority) -> bool:
        inter, back = self._queues["interactive"], self._queues["background"]
        if inter and back and self._interactive_streak >= INTERACTIVE_STREAK_MAX:
            return priority == "background" and self._background_head() == ticket
        if inter:
            return priority == "interactive" and inter[0] == ticket
        return priority == "background" and self._background_head() == ticket

    def acquire(self, priority: str = "background", clock=time, client_key: str = "", cancel_event=None) -> float:
        """Blocks until this request may be sent. Returns seconds waited.
        client_key (e.g. an access code) only affects fairness within the
        "background" priority - see _background_head above; ignored for
        "interactive".

        cancel_event (2026-09-25, opt-in, see GateCancelled above): if given
        and set while this call is still waiting for its turn - i.e. before
        it has been granted a slot - raises GateCancelled instead of
        eventually sending the request. Checked on every wake of the wait
        loop below (at most ~1s apart), so a cancellation is caught promptly
        without adding any polling of its own. A caller that never passes
        cancel_event sees no behavior change whatsoever."""
        limit = rpm_limit()
        if limit <= 0:
            return 0.0
        priority = priority if priority in self._queues else "background"
        start = clock.time()
        with self._cv:
            ticket = self._next_ticket; self._next_ticket += 1
            if priority == "background":
                self._queues[priority].append((ticket, client_key or ""))
            else:
                self._queues[priority].append(ticket)
            try:
                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        raise GateCancelled()
                    now = clock.time()
                    if self._head_is(ticket, priority) and now >= self._next_slot:
                        if priority == "background":
                            self._queues[priority] = [e for e in self._queues[priority] if e[0] != ticket]
                            self._last_background_key = client_key or ""
                        else:
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
                    if cancel_event is not None:
                        # Bound how long a wake-up can be deferred so a
                        # cancellation is noticed promptly even though
                        # cancel_event.set() (called from job_registry, a
                        # different thread) has no way to wake this
                        # condition variable directly - polling at this
                        # interval is what catches it instead of waiting for
                        # the next natural wake (which could otherwise be
                        # the full remaining queue wait).
                        timeout = min(timeout, 0.5)
                    self._cv.wait(timeout=timeout)
            except BaseException:
                if priority == "background":
                    self._queues[priority] = [e for e in self._queues[priority] if e[0] != ticket]
                else:
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
    def __init__(self, inner, priority, client_key="", cancel_event=None):
        self._inner, self._priority, self._client_key = inner, priority, client_key
        self._cancel_event = cancel_event

    def create(self, **kwargs):
        last_exc = None
        for attempt in range(RATE_RETRY_ATTEMPTS):
            _GATE.acquire(self._priority, client_key=self._client_key, cancel_event=self._cancel_event)
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
    def __init__(self, inner, priority, client_key="", cancel_event=None):
        self._inner = inner
        self.messages = _PacedMessages(inner.messages, priority, client_key, cancel_event)

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


def get_client(api_key: str | None = None, priority: str = "interactive", client_key: str = "", cancel_event=None):
    """
    Returns an Anthropic-API-compatible client. `api_key` is accepted for
    backward compatibility with existing call sites (which currently read
    ANTHROPIC_API_KEY from main.py and pass it through) and is used only
    when the direct-Anthropic path is active; it is ignored entirely on the
    Bedrock path, which authenticates via standard AWS credential
    environment variables (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY),
    resolved automatically by boto3.

    client_key (Stage 0, 2026-09-25): only meaningful for priority=
    "background" (live execution) - see _RequestGate._background_head in
    this file for the per-client fairness this feeds. Pass the caller's
    access_code. Ignored for priority="interactive" and for the RPM-pacing-
    disabled (REQ2QA_RPM_LIMIT=0) path.

    cancel_event (2026-09-25): opt-in - see GateCancelled's docstring. Pass
    a threading.Event to let a queued (not-yet-sent) request be cancelled
    for real instead of only being caught on the following step. Omit it
    (the default) for no behavior change at all.
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
            return _PacedClient(AnthropicBedrock(aws_region=BEDROCK_REGION, max_retries=0), priority, client_key, cancel_event)
        return AnthropicBedrock(aws_region=BEDROCK_REGION)

    if pacing_enabled():
        return _PacedClient(Anthropic(api_key=api_key, max_retries=0), priority, client_key, cancel_event)
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
