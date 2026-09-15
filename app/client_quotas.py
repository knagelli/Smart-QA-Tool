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


def list_all_quotas() -> list:
    """For the admin view."""
    with _lock:
        data = _load()
        return sorted(
            ({"access_code": code, **rec} for code, rec in data.items()),
            key=lambda r: r.get("updated_at", ""),
            reverse=True,
        )
