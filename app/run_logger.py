# run_logger.py
#
# Troubleshooting log system for Req2QA.
# Captures a structured, step-by-step record of every generation run,
# import run, and live-execution run so that a client-reported issue can be
# diagnosed from the log alone, without re-running anything or touching a
# terminal - everything here is meant to be driven from the admin web page
# in admin_routes.py.
#
# Hard rules (do not relax without re-confirming with Kalyan):
#   - Never write credential VALUES to a log line. Only credential field
#     NAMES / whether one was supplied.
#   - Never write the VALUE typed/filled into any form field, regardless of
#     the field's name - text-entry actions are redacted by action type,
#     not by a keyword blocklist on the field name (a blocklist can miss a
#     field name it wasn't written to catch; action-type redaction can't).
#   - Never write full uploaded-file content, full page/DOM dumps, or the
#     screenshot image itself to a log line - only a SHA-256 hash of the
#     screenshot, so authenticity can be checked later without retaining
#     the image (screenshots keep the existing separate 7-day report
#     retention, unchanged by this system).
#   - Logs are for Kalyan's own troubleshooting only - never exposed to a
#     client, never linked from any client-facing page or download.
#   - Logs are swept on a 90-day retention sweep (cleanup_old_logs).

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

LOG_ROOT = Path(os.environ.get("RUN_LOG_DIR", "run_logs"))
LOG_ROOT.mkdir(parents=True, exist_ok=True)

RETENTION_DAYS = int(os.environ.get("RUN_LOG_RETENTION_DAYS", "90"))

# Field-name patterns that must never have their VALUE written to a log.
# This is a belt-and-suspenders backstop - the primary protection for
# typed/filled values is action-type redaction below, not this list.
_SENSITIVE_KEY_PATTERN = re.compile(
    r"(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?code|"
    r"authorization|auth[_-]?header|cookie|credential|value)",
    re.IGNORECASE,
)

# Any event whose action is one of these NEVER has its entered content
# logged, full stop - deny-by-default, regardless of what key it's under.
_TEXT_ENTRY_ACTIONS = {"type", "fill", "enter_text", "set_value", "paste"}


def _redact(value: Any, in_text_entry_event: bool = False) -> Any:
    """Recursively redact secrets by key name, and ALWAYS strip entered
    text content for text-entry actions regardless of key name."""
    if isinstance(value, dict):
        is_text_entry = str(value.get("action", "")).lower() in _TEXT_ENTRY_ACTIONS
        out = {}
        for k, v in value.items():
            if is_text_entry and k.lower() in ("value", "text", "input", "content"):
                out[k] = "***not logged (text-entry field)***"
            elif _SENSITIVE_KEY_PATTERN.search(str(k)):
                out[k] = "***redacted***" if v not in (None, "") else v
            else:
                out[k] = _redact(v, in_text_entry_event=is_text_entry)
        return out
    if isinstance(value, list):
        return [_redact(v, in_text_entry_event=in_text_entry_event) for v in value]
    return value


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def hash_bytes(data: bytes) -> str:
    """SHA-256 hex digest of raw file bytes - used both when a screenshot
    is captured, and when checking a client-supplied image against it."""
    return hashlib.sha256(data).hexdigest()


class RunLog:
    """
    One RunLog per generation run, import run, or execution batch.

        rl = RunLog.start(run_type="execute", run_id=run_id,
                           meta={"module": module, "role_label": role_label,
                                 "test_case_count": len(cases),
                                 "correlation_ref": None,  # filled on error
                                 "qa_model": os.environ.get("QA_MODEL"),
                                 "app_version": os.environ.get("APP_VERSION")})
        rl.event("step", {"step": 3, "action": "click", "target": "Submit",
                           "result": "ok"})
        rl.event("screenshot", {"step": 3, "filename": "step_03.png",
                                 "sha256": run_logger.hash_bytes(png_bytes)})
        rl.event("error", {"step": 7, "message": "element not found",
                            "correlation_ref": correlation_ref})
        rl.finish(status="fail", summary={"correlation_ref": correlation_ref})
    """

    def __init__(self, log_id: str, path: Path):
        self.log_id = log_id
        self.path = path

    @classmethod
    def start(cls, run_type: str, run_id: str, meta: Optional[dict] = None) -> "RunLog":
        log_id = f"{run_type}_{run_id}_{uuid.uuid4().hex[:8]}"
        path = LOG_ROOT / f"{log_id}.jsonl"
        rl = cls(log_id, path)
        rl._write(
            {
                "kind": "start",
                "ts": _now_iso(),
                "run_type": run_type,
                "run_id": run_id,
                "meta": _redact(meta or {}),
            }
        )
        return rl

    def event(self, event_type: str, data: Optional[dict] = None) -> None:
        self._write(
            {
                "kind": "event",
                "ts": _now_iso(),
                "event_type": event_type,
                "data": _redact(data or {}),
            }
        )

    def finish(self, status: str, summary: Optional[dict] = None) -> None:
        rec = {
            "kind": "finish",
            "ts": _now_iso(),
            "status": status,
            "summary": _redact(summary or {}),
        }
        self._write(rec)
        _append_manifest_entry(self.log_id, self.path, status, summary or {})

    def _write(self, record: dict) -> None:
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            # Logging must never break the actual run. Swallow and move on.
            pass


# ---------------------------------------------------------------------------
# Tamper-evidence manifest - one append-only line per completed run, written
# once at finish() time, never rewritten. Lets you prove a log wasn't
# altered after the fact: hash the .jsonl file again and compare.
# ---------------------------------------------------------------------------

MANIFEST_PATH = LOG_ROOT / "manifest.jsonl"


def _append_manifest_entry(log_id: str, log_path: Path, status: str, summary: dict) -> None:
    try:
        file_hash = hash_bytes(log_path.read_bytes())
    except Exception:
        file_hash = None
    entry = {
        "ts": _now_iso(),
        "log_id": log_id,
        "status": status,
        "correlation_ref": summary.get("correlation_ref"),
        "file_sha256": file_hash,
    }
    try:
        with MANIFEST_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def verify_log_integrity(log_id: str) -> Optional[bool]:
    """Re-hash a log file and compare to its manifest entry. True = intact,
    False = changed since finish(), None = no manifest entry found (e.g.
    run never finished, or predates this feature)."""
    if not MANIFEST_PATH.exists():
        return None
    path = LOG_ROOT / f"{log_id}.jsonl"
    if not path.exists():
        return None
    recorded_hash = None
    with MANIFEST_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                entry = json.loads(line)
            except Exception:
                continue
            if entry.get("log_id") == log_id:
                recorded_hash = entry.get("file_sha256")
    if recorded_hash is None:
        return None
    return hash_bytes(path.read_bytes()) == recorded_hash


# ---------------------------------------------------------------------------
# Admin read/search access - used only by the web dashboard in
# admin_routes.py. No caller here needs a terminal or raw JSON.
# ---------------------------------------------------------------------------

def _load_summary(p: Path) -> dict:
    first_line, last_line = None, None
    try:
        with p.open("r", encoding="utf-8") as f:
            lines = f.readlines()
            if lines:
                first_line = json.loads(lines[0])
                last_line = json.loads(lines[-1])
    except Exception:
        pass
    first_line = first_line or {}
    last_line = last_line or {}
    return {
        "log_id": p.stem,
        "run_type": first_line.get("run_type"),
        "run_id": first_line.get("run_id"),
        "started": first_line.get("ts"),
        "meta": first_line.get("meta", {}),
        "status": last_line.get("status", "in_progress"),
        "correlation_ref": (last_line.get("summary") or {}).get("correlation_ref"),
        "size_bytes": p.stat().st_size,
    }


def search_logs(
    query: Optional[str] = None,
    run_type: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 200,
) -> list[dict]:
    """Plain-language search across all logs, newest first. `query` matches
    against correlation_ref, run_id, and log_id substrings - this is the
    single search box the admin page exposes; no query syntax required."""
    results = []
    for p in sorted(LOG_ROOT.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
        if p.name == "manifest.jsonl":
            continue
        summary = _load_summary(p)
        if run_type and summary.get("run_type") != run_type:
            continue
        if status and summary.get("status") != status:
            continue
        if query:
            haystack = " ".join(
                str(summary.get(k, "")) for k in ("log_id", "run_id", "correlation_ref")
            ).lower()
            if query.lower() not in haystack:
                continue
        results.append(summary)
        if len(results) >= limit:
            break
    return results


def read_log(log_id: str) -> Optional[list[dict]]:
    """Return every record for one log_id, in order. None if not found."""
    if "/" in log_id or ".." in log_id:
        return None
    path = LOG_ROOT / f"{log_id}.jsonl"
    if not path.exists():
        return None
    records = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except Exception:
                    continue
    return records


def find_screenshot_hash_matches(log_id: str, uploaded_sha256: str) -> list[dict]:
    """Given a hash computed from a client-supplied image, return every
    screenshot event in this log whose recorded hash matches. Powers the
    'upload the image the client sent you' compare button - no terminal,
    no manual hash lookup."""
    records = read_log(log_id) or []
    matches = []
    for r in records:
        if r.get("kind") == "event" and r.get("event_type") == "screenshot":
            data = r.get("data", {})
            if data.get("sha256") == uploaded_sha256:
                matches.append({"ts": r.get("ts"), **data})
    return matches


def cleanup_old_logs(retention_days: int = RETENTION_DAYS) -> int:
    """Delete logs older than retention_days. Returns count deleted.
    Call this from the same sweep job that already handles run/report
    retention, on the same schedule. Manifest entries are kept regardless
    (they're tiny and are the historical record of what once existed)."""
    cutoff = time.time() - (retention_days * 86400)
    deleted = 0
    for p in LOG_ROOT.glob("*.jsonl"):
        if p.name == "manifest.jsonl":
            continue
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                deleted += 1
        except Exception:
            continue
    return deleted
