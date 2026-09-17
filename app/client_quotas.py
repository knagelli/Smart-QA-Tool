# client_quotas.py
#
# Per-engagement test-case quota tracking for PAID clients (tiers 1/2 - never
# applies to trial codes, which have their own separate one-shot/word-count
# system in trial_signups.py).
#
# Design settled 2026-09-15 (see claude/billing-model-unbundled-generation-
# execution-2026-09-15.md in the project docs) - captures Kalyan's explicit
# regeneration/quota policy:
#   - A client "subscribes" for a specific test-case count upfront (set here
#     by Kalyan via the admin UI, per access code).
#   - Generation is blocked once EITHER 5 total generation attempts OR 1.5x
#     the subscribed count of KEPT test cases is reached, whichever comes
#     first.
#   - Consumption against the subscribed count is locked in the moment a
#     client confirms/keeps a generated set (see main.py's
#     /confirm-generated/{run_id}) - it can never be "returned" by a later
#     regeneration. Running generation again does not double-count or
#     retroactively invalidate an already-confirmed kept set from an earlier
#     run; it simply starts a new, separate run.
#   - A client with NO quota configured (get_quota returns None) is allowed
#     to generate without restriction - this is a deliberate fail-open
#     default so existing/legacy clients (onboarded before this system
#     existed) are unaffected until Kalyan explicitly sets a quota for them.
#     This is a default worth Kalyan revisiting once quotas are the norm
#     rather than the exception.
#
# Same file-based JSON + threading.Lock pattern as trial_signups.py, and the
# same single-process caveat applies: this store is only correct under
# Render's current single-worker deployment.

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_ROOT = Path(os.environ.get("DATA_DIR")) if os.environ.get("DATA_DIR") else BASE_DIR
STORE_PATH = DATA_ROOT / "client_quotas.json"

MAX_GENERATION_ATTEMPTS = 5
KEPT_CEILING_MULTIPLIER = 1.5  # of subscribed_count

_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load() -> dict:
    if not STORE_PATH.exists():
        return {}
    try:
        return json.loads(STORE_PATH.read_text())
    except Exception:
        return {}


def _save(data: dict) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(STORE_PATH)


def get_quota(access_code: str) -> dict | None:
    """Returns the quota record for this access code, or None if Kalyan
    hasn't configured one for it (legacy/unrestricted client)."""
    with _lock:
        data = _load()
        return data.get(access_code)


def set_quota(access_code: str, client_name: str, subscribed_count: int) -> dict:
    """Admin action: create or update a client's subscribed test-case count.
    Updating an existing client's subscribed_count does NOT reset their
    attempt_count or consumed_count - those persist across a quota change
    (e.g. Kalyan raising a client's subscription mid-engagement)."""
    with _lock:
        data = _load()
        existing = data.get(access_code, {})
        data[access_code] = {
            "client_name": client_name,
            "subscribed_count": subscribed_count,
            "attempt_count": existing.get("attempt_count", 0),
            "consumed_count": existing.get("consumed_count", 0),
            "created_at": existing.get("created_at", _now_iso()),
            "updated_at": _now_iso(),
        }
        _save(data)
        return dict(data[access_code])


def reset_attempts(access_code: str) -> bool:
    """Admin escape hatch: zero out the attempt counter (not consumed_count -
    that's a permanent, billable fact and is never reset by this)."""
    with _lock:
        data = _load()
        if access_code not in data:
            return False
        data[access_code]["attempt_count"] = 0
        data[access_code]["updated_at"] = _now_iso()
        _save(data)
        return True


def check_can_generate(access_code: str) -> tuple[bool, str | None]:
    """Returns (allowed, block_reason). block_reason is None when allowed.
    A client with no quota configured is unrestricted (see module docstring)."""
    quota = get_quota(access_code)
    if quota is None:
        return True, None

    if quota["attempt_count"] >= MAX_GENERATION_ATTEMPTS:
        return False, (
            f"This engagement has reached its limit of {MAX_GENERATION_ATTEMPTS} generation "
            "attempts. Please contact kalyan@req2qa.com to continue."
        )

    ceiling = quota["subscribed_count"] * KEPT_CEILING_MULTIPLIER
    if quota["consumed_count"] >= ceiling:
        return False, (
            f"This engagement has already used {quota['consumed_count']} of its "
            f"{ceiling:g}-test-case allowance. Please contact kalyan@req2qa.com to continue."
        )

    return True, None


def record_generation_attempt(access_code: str) -> None:
    """Called once per successful generation call (not per HTTP request) for
    a paid, quota-configured client. A no-op if no quota is configured."""
    with _lock:
        data = _load()
        if access_code not in data:
            return
        data[access_code]["attempt_count"] = data[access_code].get("attempt_count", 0) + 1
        data[access_code]["updated_at"] = _now_iso()
        _save(data)


def record_kept(access_code: str, kept_count: int) -> tuple[int, str | None]:
    """Called once a client confirms which generated test cases to keep.
    Permanently adds kept_count to consumed_count. Returns
    (new_consumed_count, warning_message_or_None) - the warning fires once
    consumed_count has reached or passed the base subscribed_count (not the
    1.5x ceiling), per Kalyan's transparency requirement."""
    with _lock:
        data = _load()
        if access_code not in data:
            return kept_count, None
        record = data[access_code]
        record["consumed_count"] = record.get("consumed_count", 0) + kept_count
        record["updated_at"] = _now_iso()
        _save(data)
        warning = None
        if record["consumed_count"] >= record["subscribed_count"]:
            warning = (
                f"Heads up: this engagement has now used {record['consumed_count']} of its "
                f"{record['subscribed_count']}-test-case subscribed allowance "
                f"(up to {record['subscribed_count'] * KEPT_CEILING_MULTIPLIER:g} total before "
                "further generation is blocked)."
            )
        return record["consumed_count"], warning


def set_execution_allowance(access_code: str, kept_count: int) -> None:
    """Called once per confirmed generation (right after record_kept), NOT an
    admin action - this is what implements the 2026-09-16 council verdict on
    the execution-limit question: the disclosed execution limit for an
    engagement is exactly the number of test cases the client kept, with NO
    hidden multiplier/buffer on top. A client who keeps 25 test cases has an
    execution allowance of exactly 25, told to them as such and enforced as
    such - not "25 disclosed, 31 actually allowed."

    A no-op if this access code has no quota configured at all (unrestricted
    legacy client, same fail-open default used everywhere else in this
    module). Updates subscribed_execution_count to the LATEST kept count
    (an engagement's scope moves with its most recent confirmed generation,
    consistent with the existing policy that a new generation supersedes the
    previous run's kept set) but deliberately does NOT reset
    consumed_execution_count - execution already run and billed against an
    earlier kept set stays counted; only the forward-looking ceiling moves.
    """
    with _lock:
        data = _load()
        if access_code not in data:
            return
        data[access_code]["subscribed_execution_count"] = kept_count
        data[access_code].setdefault("consumed_execution_count", 0)
        data[access_code]["updated_at"] = _now_iso()
        _save(data)


def check_can_execute(access_code: str, requested_count: int) -> tuple[bool, str | None]:
    """Returns (allowed, block_reason). block_reason is None when allowed.
    Deliberately has NO 1.5x/1.25x-style grace ceiling the way generation
    does - the council's verdict on 2026-09-16 was that a hidden buffer on
    execution is a real, undisclosed cost leak and creates a false-overage
    signal for the billing-visibility email, so this blocks exactly at the
    disclosed number, every time, no exceptions baked into the code. A
    client with no quota configured, or one whose execution allowance
    hasn't been set yet (no generation confirmed under this access code),
    is unrestricted - same fail-open default as the rest of this module."""
    quota = get_quota(access_code)
    if quota is None:
        return True, None
    subscribed = quota.get("subscribed_execution_count")
    if subscribed is None:
        return True, None
    consumed = quota.get("consumed_execution_count", 0)
    if consumed + requested_count > subscribed:
        remaining = max(0, subscribed - consumed)
        return False, (
            f"This engagement's execution allowance is {subscribed} test case(s) "
            f"(based on what was kept from generation), of which {consumed} have "
            f"already been run. This request would run {requested_count} more, but "
            f"only {remaining} remain. Please select {remaining} or fewer, or "
            "contact kalyan@req2qa.com to increase the allowance."
        )
    return True, None


def reserve_execution(access_code: str, requested_count: int) -> tuple[bool, str | None]:
    """Atomically checks AND reserves execution allowance in one step, inside
    the same lock the rest of this module uses - added 2026-09-17 to close a
    check-then-act race in the previous check_can_execute()-then-
    record_executed() flow: that flow only debited the allowance once a
    batch *finished*, so two submissions racing each other under the same
    access code (any number of tabs, browsers, or employees sharing that
    code - quota is per engagement/access code, not per person) could both
    see the same "not yet spent" number and both get approved, together
    exceeding what the client actually bought.

    Call this exactly once, right when a live-execution submission is fully
    validated and about to be handed to the background batch runner - NOT
    earlier (a reservation made before all other validation has passed
    would need to be unwound if a later check rejects the request) and NOT
    record_executed, which this replaces for the completion side (see
    reconcile_execution below).

    Returns (allowed, block_reason), same shape as check_can_execute (which
    remains as a read-only preview, e.g. for the admin view, but should no
    longer gate an actual execution request). Same fail-open default as the
    rest of this module: unconfigured/legacy access codes are unrestricted
    and nothing is reserved for them."""
    with _lock:
        data = _load()
        if access_code not in data:
            return True, None
        record = data[access_code]
        subscribed = record.get("subscribed_execution_count")
        if subscribed is None:
            return True, None
        consumed = record.get("consumed_execution_count", 0)
        if consumed + requested_count > subscribed:
            remaining = max(0, subscribed - consumed)
            return False, (
                f"This engagement's execution allowance is {subscribed} test case(s) "
                f"(based on what was kept from generation), of which {consumed} have "
                "already been run or are reserved by an in-progress run. This request "
                f"would run {requested_count} more, but only {remaining} remain. Please "
                f"select {remaining} or fewer, or contact kalyan@req2qa.com to increase "
                "the allowance."
            )
        record["consumed_execution_count"] = consumed + requested_count
        record["updated_at"] = _now_iso()
        _save(data)
        return True, None


def reconcile_execution(access_code: str, reserved_count: int, actual_count: int) -> None:
    """Trues up a reservation made by reserve_execution once a batch is done
    (success or crash) against how many test cases it actually attempted.
    Only ever releases allowance BACK (adjusts down) - never adjusts up -
    so a batch that errored out or crashed before attempting every reserved
    case doesn't leave the client permanently charged for cases that were
    reserved but never run. A no-op if actual_count >= reserved_count
    (nothing to release) or if this access code has no quota configured."""
    if actual_count >= reserved_count:
        return
    with _lock:
        data = _load()
        if access_code not in data:
            return
        record = data[access_code]
        if record.get("subscribed_execution_count") is None:
            return
        released = reserved_count - actual_count
        record["consumed_execution_count"] = max(0, record.get("consumed_execution_count", 0) - released)
        record["updated_at"] = _now_iso()
        _save(data)


def record_executed(access_code: str, executed_count: int) -> int | None:
    """Superseded 2026-09-17 by reserve_execution (at submission) +
    reconcile_execution (at completion) - see those for why. Kept only in
    case something outside main.py's live-execution flow still calls this;
    do not wire this back into that flow, since debiting only at completion
    reopens the exact race those two functions were added to close."""
    with _lock:
        data = _load()
        if access_code not in data:
            return None
        record = data[access_code]
        record["consumed_execution_count"] = record.get("consumed_execution_count", 0) + executed_count
        record["updated_at"] = _now_iso()
        _save(data)
        return record["consumed_execution_count"]


def list_all_quotas() -> list:
    """For the admin view."""
    with _lock:
        data = _load()
        return sorted(
            ({"access_code": code, **rec} for code, rec in data.items()),
            key=lambda r: r.get("updated_at", ""),
            reverse=True,
        )
