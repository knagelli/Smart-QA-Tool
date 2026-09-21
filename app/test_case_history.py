# test_case_history.py
#
# Durable, cross-run test-case history per client (access_code), so a client
# who generated (and paid for, Tier 1/2) test cases can come back later -
# after the normal run/execution artifacts (RUNS_DIR/<run_id>, an 8-day
# clock: RUN_RETENTION_SECONDS in main.py) have expired - and still select
# from what they kept, to run execution (Tier 3) on it without regenerating
# or re-uploading anything.
#
# RETENTION POLICY (decided 2026-09-21, deliberately mirrors req_history.py's
# already-adopted, already-disclosed rule, so there is ONE retention story to
# explain to a client instead of several different numbers):
#   - Up to MAX_SETS_PER_CLIENT kept-test-case sets are retained per access
#     code.
#   - The whole set expires HISTORY_RETENTION_SECONDS (90 days) after the
#     FIRST set in that history was saved - not a rolling per-set clock.
#     Same whole-set-expiry rationale as req_history.py: simple to state
#     accurately, simple to reason about here.
#   - This is TEST CASE TEXT ONLY (titles, steps, expected results) - never
#     execution evidence. Screenshots and execution reports remain on their
#     existing, separate 7-day retention clock in main.py
#     (RUN_RETENTION_SECONDS), completely unaffected by this feature.
#   - A set is only ever saved from a run the client actually confirmed/kept
#     (see main.py's /confirm-generated) - an abandoned or rejected
#     generation never enters this history.
#
# Same file-based JSON + threading.Lock pattern as req_history.py,
# client_quotas.py and trial_signups.py - single-process caveat applies.
#
# Storage layout, all under DATA_ROOT / "test_case_history" / <access_code>/:
#   manifest.json     - {"next_seq": int, "sets": [set record, ...]}
#   <set_id>.json     - full kept test_scenarios list for that set (the same
#                        shape main.py's RUNS_DIR/<run_id>/data.json stores
#                        under "test_scenarios" - complete steps/expected
#                        results, since this is what a later execution run
#                        actually needs, unlike req_history.py's minimal
#                        tc_id/req_id/title summary which only needs to
#                        answer "might this be stale", not "run this").
#
# A set record: {
#   "set_id": "s3",            # sequential per access_code, never reused
#   "run_id": "<original run_id this came from, for rehydration>",
#   "application": "...",
#   "run_date": "...",
#   "saved_at": "<iso8601>",
#   "case_count": 12,
# }

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_ROOT = Path(os.environ.get("DATA_DIR")) if os.environ.get("DATA_DIR") else BASE_DIR
HISTORY_ROOT = DATA_ROOT / "test_case_history"

MAX_SETS_PER_CLIENT = 5
HISTORY_RETENTION_SECONDS = 90 * 24 * 60 * 60  # 90 days from the FIRST retained set's save

_lock = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _client_dir(access_code: str) -> Path:
    # Same path-traversal defense as req_history._client_dir - access_code
    # becomes a directory name here, so it must be a simple token.
    if not access_code or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", access_code):
        raise ValueError("Invalid access_code for test-case history storage.")
    return HISTORY_ROOT / access_code


def _manifest_path(access_code: str) -> Path:
    return _client_dir(access_code) / "manifest.json"


def _empty_manifest() -> dict:
    return {"next_seq": 1, "sets": []}


def _load_manifest_raw(access_code: str) -> dict:
    path = _manifest_path(access_code)
    if not path.exists():
        return _empty_manifest()
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or "sets" not in data:
            return _empty_manifest()
        data.setdefault("next_seq", len(data.get("sets", [])) + 1)
        return data
    except Exception:
        # Fail open - a corrupt manifest must never break generation or
        # execution, same posture as every other store in this codebase.
        return _empty_manifest()


def _save_manifest_raw(access_code: str, manifest: dict) -> None:
    client_dir = _client_dir(access_code)
    client_dir.mkdir(parents=True, exist_ok=True)
    path = _manifest_path(access_code)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, indent=2))
    tmp.replace(path)


def _delete_set_file(access_code: str, set_id: str) -> None:
    try:
        (_client_dir(access_code) / f"{set_id}.json").unlink(missing_ok=True)
    except Exception:
        pass


def _prune_expired_and_evicted(access_code: str, manifest: dict) -> dict:
    sets = manifest.get("sets", [])
    if not sets:
        return manifest

    first_saved = _parse_iso(sets[0]["saved_at"])
    if (_now() - first_saved).total_seconds() > HISTORY_RETENTION_SECONDS:
        for s in sets:
            _delete_set_file(access_code, s["set_id"])
        manifest["sets"] = []
        return manifest

    while len(sets) > MAX_SETS_PER_CLIENT:
        oldest = sets.pop(0)
        _delete_set_file(access_code, oldest["set_id"])
    manifest["sets"] = sets
    return manifest


def _load_manifest(access_code: str) -> dict:
    manifest = _load_manifest_raw(access_code)
    return _prune_expired_and_evicted(access_code, manifest)


def list_sets(access_code: str) -> list:
    """Metadata only (no test-case bodies), oldest first. Already pruned -
    never returns an expired or evicted set."""
    with _lock:
        manifest = _load_manifest(access_code)
        _save_manifest_raw(access_code, manifest)
        return manifest["sets"]


def get_set(access_code: str, set_id: str) -> dict | None:
    for s in list_sets(access_code):
        if s["set_id"] == set_id:
            return s
    return None


def get_set_test_cases(access_code: str, set_id: str) -> list | None:
    # Re-validates via get_set() first, same stale-reference defense as
    # req_history.get_version_text - a set_id that's since expired/been
    # evicted must never read a file that (per the manifest) shouldn't
    # exist anymore.
    if get_set(access_code, set_id) is None:
        return None
    path = _client_dir(access_code) / f"{set_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def save_case_set(access_code: str, run_id: str, application: str, test_cases: list, run_date: str = "") -> dict:
    """Save the client's final KEPT test-case set for later, cross-run
    execution selection. Called once, from main.py's /confirm-generated,
    right after curation is finalized - same "only what was actually kept"
    posture as req_history.update_test_cases_by_run_id, and best-effort/
    non-fatal at the call site, same as every other history write in this
    codebase."""
    with _lock:
        manifest = _load_manifest(access_code)
        set_id = f"s{manifest['next_seq']}"
        manifest["next_seq"] += 1
        record = {
            "set_id": set_id,
            "run_id": run_id,
            "application": application,
            "run_date": run_date,
            "saved_at": _now_iso(),
            "case_count": len(test_cases or []),
        }
        client_dir = _client_dir(access_code)
        client_dir.mkdir(parents=True, exist_ok=True)
        (client_dir / f"{set_id}.json").write_text(json.dumps(test_cases or []))
        manifest["sets"].append(record)
        manifest = _prune_expired_and_evicted(access_code, manifest)
        _save_manifest_raw(access_code, manifest)
        return record


def rehydrate_run_dir(access_code: str, set_id: str, runs_dir: Path) -> str | None:
    """If a persisted set's original run_id/data.json has since aged off
    RUNS_DIR (the normal 8-day run-artifact clock in main.py), reconstruct a
    minimal data.json under runs_dir/<run_id>/ from this 90-day history so
    the existing, unmodified /execute/{run_id} flow (execute_select.html,
    execute_run) can run against it exactly as it would for a fresh run.

    Deliberately reuses the ORIGINAL run_id rather than minting a new one -
    the execution engine, quota tracking, and report links are all already
    keyed by run_id, and a returning client should land on execution results
    that trace back to the same generation run they originally paid for.

    Returns the run_id to redirect to, or None if the set can't be found.
    Never overwrites an existing, still-live data.json - if the run is still
    within its normal retention window, this is a no-op and that data is
    used as-is (it may already reflect a discard/curation state this history
    snapshot doesn't need to reproduce)."""
    record = get_set(access_code, set_id)
    if record is None:
        return None
    run_id = record.get("run_id") or ""
    if not run_id or not run_id.isalnum():
        return None

    run_dir = runs_dir / run_id
    data_path = run_dir / "data.json"
    if data_path.exists():
        return run_id  # still live under the normal 8-day clock - nothing to do

    test_cases = get_set_test_cases(access_code, set_id)
    if test_cases is None:
        return None

    run_dir.mkdir(parents=True, exist_ok=True)
    minimal_data = {
        "run_id": run_id,
        "application": record.get("application", ""),
        "run_date": record.get("run_date", ""),
        "test_scenarios": test_cases,
        "access_code": access_code,
    }
    data_path.write_text(json.dumps(minimal_data))
    return run_id


# --------------------------------------------------------------------------
# CLIENT-FACING DISCLOSURE (state plainly wherever this feature is offered,
# same standard as req_history.py's disclosure - not buried in a ToS clause):
#
#   "For execution on previously generated test cases, Req2QA retains up to
#    5 of your most recent kept test-case sets (test case text only - no
#    screenshots or execution evidence) for up to 90 days from the first set
#    in that history. After 90 days, or once a 6th set is saved, the oldest
#    set(s) are automatically removed. Execution screenshots and reports
#    remain on the existing separate 7-day retention period, unaffected by
#    this feature. This history is available on paid engagements only, not
#    during a free trial."
#
# Enforcing the policy here does not substitute for stating it in the
# privacy/terms pages and in-product copy - see requirement_history.html for
# the equivalent disclosure already shipped for requirement versions, and
# test_case_history.html for this feature's copy.
# --------------------------------------------------------------------------
