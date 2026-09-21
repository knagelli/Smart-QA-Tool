# req_history.py
#
# Durable requirement-version history per client (access_code), enabling
# impact analysis: "what changed between two requirement versions I've
# uploaded, and which of my existing test cases does that put at risk."
#
# RETENTION POLICY (decided 2026-09-21, must be stated plainly to clients -
# see the disclosure note at the bottom of this file):
#   - Up to MAX_VERSIONS_PER_CLIENT requirement versions are retained per
#     access code.
#   - The whole set expires HISTORY_RETENTION_SECONDS (90 days) after the
#     FIRST version in that set was uploaded - not a rolling per-version
#     clock. This is a deliberately simple, easy-to-disclose rule: "we keep
#     up to 5 requirement versions for 90 days from your first upload;
#     after that the record clears automatically and starts fresh on your
#     next upload." A per-item rolling expiry would be harder to state
#     accurately to a client and harder to reason about here.
#   - This is RAW REQUIREMENT TEXT ONLY, never full run artifacts
#     (screenshots, reports) - those remain on their existing, separate
#     7-day retention clock in main.py (RUN_RETENTION_SECONDS), unchanged
#     by this feature.
#   - Comparison is always an explicit, client-chosen pair of versions from
#     their retained set (see main.py's /requirement-history and
#     /analyze-impact) - never an automatic "always diff against whatever's
#     newest" behaviour, so nothing gets compared without the client
#     choosing exactly what to compare.
#
# Same file-based JSON + threading.Lock pattern as client_quotas.py and
# trial_signups.py, and the same single-process caveat applies: this store
# is only correct under a single-worker deployment (true of the current EC2
# setup - see req2qa-tier0-ec2-provisioning-walkthrough for why).
#
# Storage layout, all under DATA_ROOT / "req_history" / <access_code>/:
#   manifest.json   - {"next_seq": int, "versions": [version record, ...]}
#   v<N>.txt        - raw extracted requirement text for that version
#
# A version record: {
#   "version_id": "v3",                  # sequential per access_code,
#                                          # NEVER reused even after older
#                                          # versions are pruned/evicted -
#                                          # see _next_version_id
#   "label": "0.2" or "",                # optional client-supplied label
#                                          # (reuses the existing free-text
#                                          # baseline_version field already
#                                          # on the /analyze form)
#   "application": "...",
#   "uploaded_at": "<iso8601>",
#   "run_id": "<run id that generated test cases for this version, if any>",
#   "char_count": 12345,
#   "test_cases": [{"tc_id": "TC-001", "req_id": "REQ-001", "title": "..."}],
# }

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_ROOT = Path(os.environ.get("DATA_DIR")) if os.environ.get("DATA_DIR") else BASE_DIR
HISTORY_ROOT = DATA_ROOT / "req_history"

MAX_VERSIONS_PER_CLIENT = 5
HISTORY_RETENTION_SECONDS = 90 * 24 * 60 * 60  # 90 days from the FIRST retained version's upload

# Defensive cap only - not a real storage-cost concern (raw text is
# negligible in size, even at MAX_VERSIONS_PER_CLIENT x every client this
# tool has), just a guard against one runaway/abusive upload bloating a
# single client's history folder. Requirements documents in practice are a
# small fraction of this even at their largest (existing MAX_DOC_BYTES on
# the source *file* in main.py is 15 MB, but that's before text extraction
# strips formatting/markup - extracted text is reliably much smaller).
MAX_STORED_TEXT_CHARS = 2 * 1024 * 1024  # 2 MB of text

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
    # access_code is an opaque client-chosen/assigned string used as a path
    # component elsewhere in this codebase already (client_quotas.py,
    # trial_signups.py key their JSON stores by it directly, never as a
    # filesystem path) - here it BECOMES a directory name, which is new, so
    # it must be sanitized. Reject anything that isn't a simple token rather
    # than trying to escape it, since a legitimate access code has no reason
    # to contain path separators or dot-segments.
    if not access_code or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", access_code):
        raise ValueError("Invalid access_code for requirement history storage.")
    return HISTORY_ROOT / access_code


def _manifest_path(access_code: str) -> Path:
    return _client_dir(access_code) / "manifest.json"


def _empty_manifest() -> dict:
    return {"next_seq": 1, "versions": []}


def _load_manifest_raw(access_code: str) -> dict:
    path = _manifest_path(access_code)
    if not path.exists():
        return _empty_manifest()
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or "versions" not in data:
            return _empty_manifest()
        data.setdefault("next_seq", len(data.get("versions", [])) + 1)
        return data
    except Exception:
        # Corrupt manifest must never take down generation/impact-analysis
        # for a client - treat as "no history yet" rather than raising, the
        # same fail-open posture client_quotas.py takes on a load error.
        return _empty_manifest()


def _save_manifest_raw(access_code: str, manifest: dict) -> None:
    client_dir = _client_dir(access_code)
    client_dir.mkdir(parents=True, exist_ok=True)
    path = _manifest_path(access_code)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, indent=2))
    tmp.replace(path)


def _delete_version_file(access_code: str, version_id: str) -> None:
    try:
        (_client_dir(access_code) / f"{version_id}.txt").unlink(missing_ok=True)
    except Exception:
        pass


def _prune_expired_and_evicted(access_code: str, manifest: dict) -> dict:
    """Applies both retention rules and returns the (possibly changed)
    manifest, deleting any now-removed version's raw text file as it goes.
    Called on every read AND every write, so an expired/evicted version
    disappears from what a client can see or select even between writes,
    not only the next time save_version() happens to run."""
    versions = manifest.get("versions", [])
    if not versions:
        return manifest

    # Whole-set time expiry: 90 days from the FIRST retained version's
    # upload clears everything (see module docstring for why this is a
    # deliberately simple all-or-nothing rule, not per-item).
    first_uploaded = _parse_iso(versions[0]["uploaded_at"])
    if (_now() - first_uploaded).total_seconds() > HISTORY_RETENTION_SECONDS:
        for v in versions:
            _delete_version_file(access_code, v["version_id"])
        manifest["versions"] = []
        return manifest

    # Count cap: FIFO-evict the oldest until at most MAX_VERSIONS_PER_CLIENT
    # remain. version_ids are never reused (see _next_version_id) so an
    # evicted "v1" never collides with a future version.
    while len(versions) > MAX_VERSIONS_PER_CLIENT:
        oldest = versions.pop(0)
        _delete_version_file(access_code, oldest["version_id"])
    manifest["versions"] = versions
    return manifest


def _load_manifest(access_code: str) -> dict:
    manifest = _load_manifest_raw(access_code)
    pruned = _prune_expired_and_evicted(access_code, manifest)
    return pruned


def list_versions(access_code: str) -> list:
    """Metadata only (no raw text), oldest first - the order versions were
    uploaded in. Already pruned per the retention policy - never returns an
    expired or evicted version."""
    with _lock:
        manifest = _load_manifest(access_code)
        _save_manifest_raw(access_code, manifest)  # persist any pruning this read triggered
        return manifest["versions"]


def get_version(access_code: str, version_id: str) -> dict | None:
    for v in list_versions(access_code):
        if v["version_id"] == version_id:
            return v
    return None


def get_latest_version(access_code: str) -> dict | None:
    versions = list_versions(access_code)
    return versions[-1] if versions else None


def update_test_cases_by_run_id(access_code: str, run_id: str, test_cases: list) -> bool:
    """Patches in the FINAL, client-curated test-case set for the version
    that was created during generation run `run_id`, once curation confirms
    (see main.py's /confirm-generated). This two-step write exists because
    save_version() has to happen during /analyze, while the raw requirement
    text is still in memory (it is never persisted into data.json) - but at
    that point generation has only just produced its full candidate set, not
    yet the client's kept subset, which isn't known until a separate later
    request confirms curation. Storing the pre-curation full set as if it
    were final would let impact analysis later say "test case TC-014 may be
    stale" for a test case the client never actually kept or paid for -
    this patch step is what keeps the stored set honest. If curation is
    never confirmed (the client abandons the review screen), the version
    simply keeps its empty test_cases list - an accurate reflection of "no
    kept test cases exist for this version," not a bug.

    Returns False (no-op, not an error) if no version matches this run_id -
    expected for a run whose version has since expired/been evicted, or a
    pre-this-feature run_id with no version record at all."""
    if not run_id:
        return False
    with _lock:
        manifest = _load_manifest(access_code)
        for v in manifest["versions"]:
            if v.get("run_id") == run_id:
                v["test_cases"] = [
                    {
                        "tc_id": tc.get("tc_id", ""),
                        "req_id": tc.get("req_id", ""),
                        "title": tc.get("title", ""),
                    }
                    for tc in (test_cases or [])
                ]
                _save_manifest_raw(access_code, manifest)
                return True
        return False


def get_version_text(access_code: str, version_id: str) -> str | None:
    # Defends against a version_id that WAS valid but has since expired/been
    # evicted - get_version() re-checks the current retained set rather than
    # trusting a caller's possibly-stale version_id, so a stale link never
    # reads a file that (per the manifest) shouldn't exist anymore even if
    # the .txt happens to still be on disk for some reason.
    if get_version(access_code, version_id) is None:
        return None
    path = _client_dir(access_code) / f"{version_id}.txt"
    if not path.exists():
        return None
    return path.read_text()


def save_version(
    access_code: str,
    application: str,
    requirements_text: str,
    run_id: str = "",
    label: str = "",
    test_cases: list | None = None,
) -> dict:
    """Save a new requirement version snapshot for this client. Called once
    per successful generation run (see main.py's /analyze), never on a
    retry/failure - an aborted or rejected generation should not pollute a
    client's comparison history with a version nobody actually kept.

    test_cases should be the minimal summary (tc_id, req_id, title) needed
    to later say "this test case may now be stale" - never the full
    steps/expected-result text, which would duplicate report.html's content
    here for no purpose this feature needs.

    Applies the retention policy (90-day whole-set expiry, 5-version FIFO
    cap) BEFORE appending, so a save never transiently exceeds the cap and
    an already-expired set is cleared before the new version starts a fresh
    one - consistent with "90 days from your first upload, then it starts
    over" rather than the new upload extending a stale set's life."""
    text = requirements_text if len(requirements_text) <= MAX_STORED_TEXT_CHARS else requirements_text[:MAX_STORED_TEXT_CHARS]

    with _lock:
        manifest = _load_manifest(access_code)
        version_id = f"v{manifest['next_seq']}"
        manifest["next_seq"] += 1
        record = {
            "version_id": version_id,
            "label": (label or "").strip()[:100],
            "application": application,
            "uploaded_at": _now_iso(),
            "run_id": run_id,
            "char_count": len(text),
            "truncated": len(requirements_text) > MAX_STORED_TEXT_CHARS,
            "test_cases": [
                {
                    "tc_id": tc.get("tc_id", ""),
                    "req_id": tc.get("req_id", ""),
                    "title": tc.get("title", ""),
                }
                for tc in (test_cases or [])
            ],
        }
        client_dir = _client_dir(access_code)
        client_dir.mkdir(parents=True, exist_ok=True)
        (client_dir / f"{version_id}.txt").write_text(text)
        manifest["versions"].append(record)
        # Re-apply the count cap immediately after appending, in case this
        # save is what pushes the client over MAX_VERSIONS_PER_CLIENT -
        # _load_manifest above only prunes what was already stale/over cap
        # BEFORE this new version existed.
        manifest = _prune_expired_and_evicted(access_code, manifest)
        _save_manifest_raw(access_code, manifest)
        return record


# --------------------------------------------------------------------------
# CLIENT-FACING DISCLOSURE (must be stated plainly wherever this feature is
# offered - not buried in a ToS clause):
#
#   "For requirement impact analysis, Req2QA retains up to 5 of your most
#    recent requirement document versions (text only - no screenshots or
#    execution evidence) for up to 90 days from the first version in that
#    set. After 90 days, or once a 6th version is uploaded, the oldest
#    version(s) are automatically removed. Execution screenshots and
#    reports remain on the existing separate 7-day retention period,
#    unaffected by this feature."
#
# See claude/req2qa-impact-analysis-scoping-2026-09-21.md and the Australia
# DPA draft for where this needs to be reflected once the feature ships -
# this module enforces the policy; the privacy/terms pages and any
# in-product copy are what actually disclose it to a client, and enforcing
# it here does not substitute for stating it there.
# --------------------------------------------------------------------------
