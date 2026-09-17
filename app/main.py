"""
Req2QA - Phase 1 web app
Requirements file + application name -> validation -> test scenarios ->
traceability matrix -> HTML report + Excel workbook.

Your Anthropic API key lives only in this server's environment (ANTHROPIC_API_KEY).
Clients never see it and never need their own Claude account.

Run locally:
    export ANTHROPIC_API_KEY=sk-ant-...
    export CLIENT_ACCESS_CODES=acme:letmein123,globex:hunter2   # client_name:code pairs
    export ALLOW_OPEN_ACCESS=true   # ONLY for local dev with no codes set - see _check_access
    uvicorn app.main:app --reload --port 8000

Deploy: any host that runs a Python ASGI app (Render, Railway, Fly.io, a VM).
See README.md for a walkthrough.

SECURITY NOTES (see claude/ project docs for the full audit this responds to):
- Access control fails CLOSED: if CLIENT_ACCESS_CODES is unset/empty in a
  deployed environment, every request is rejected rather than silently
  allowed through. Local dev can opt into the old open-access behavior
  explicitly via ALLOW_OPEN_ACCESS=true - it is never the default.
- Download links carry a signed, time-limited token (see _make_download_token)
  instead of being reachable by anyone who has the URL indefinitely.
- User-facing error messages are always generic, with a short reference code;
  full exception details (which can otherwise leak library/vendor names) are
  logged server-side only, never sent to the browser.
- Basic in-memory rate limiting protects both the access-code check (against
  credential-guessing) and the expensive analysis endpoints (against a
  leaked/shared code being used to run up API costs). This is process-local
  state, adequate at "handful of clients, one Render instance" scale - swap
  for a shared store (Redis, etc.) if this ever runs multi-process.
"""
import asyncio
import hashlib
import hmac
import io
import ipaddress
import json
import logging
import os
import re
import shutil
import socket
import time
import uuid
import zipfile
from collections import defaultdict, deque
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import List
from urllib.parse import urlparse

from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, FileResponse, PlainTextResponse, StreamingResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .extract import extract_text
from .qa_engine import run_qa_analysis, run_qa_analysis_custom, structure_existing_test_cases, match_requirements_to_test_cases
from .report_builder import build_html, build_xlsx, build_html_custom, build_xlsx_custom
from .diagram_parser import parse_flow_diagrams
from .execute_engine import execute_test_case, ExecutionError
from .execution_report import build_execution_report
from .import_parser import try_parse_tabular
from . import run_logger
from . import admin_routes
from . import fixtures
from . import exec_status
from . import trial_signups
from . import mailer
from . import client_quotas

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("req2qa")

BASE_DIR = Path(__file__).resolve().parent
# DATA_DIR lets all persisted run/execution/history data live on a Render
# persistent disk mounted at any path you choose (e.g. /var/data), separate
# from the app's own code checkout - the code directory gets replaced on
# every deploy, so persisted data must never live inside it. Unset (local
# dev, or the free tier where nothing survives a restart anyway), this
# falls back to the previous behaviour unchanged.
DATA_ROOT = Path(os.environ.get("DATA_DIR")) if os.environ.get("DATA_DIR") else BASE_DIR
RUNS_DIR = DATA_ROOT / "runs"
RUNS_DIR.mkdir(parents=True, exist_ok=True)
PENDING_DIR = RUNS_DIR / "pending"  # Option B runs awaiting review-and-confirm of the parsed flow
PENDING_DIR.mkdir(exist_ok=True)
PENDING_MAX_AGE_SECONDS = 24 * 60 * 60  # abandoned review sessions are swept after this

# File-size guards (cost/DoS protection - a single request can otherwise
# attach arbitrarily large documents/images to a billed Anthropic API call).
MAX_DOC_BYTES = 15 * 1024 * 1024
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_DIAGRAM_FILES = 10

# Signing secret for download tokens. Set DOWNLOAD_SIGNING_SECRET explicitly
# to keep issued links valid across restarts/redeploys; otherwise a random
# one is generated per process (existing links break on restart, which is an
# acceptable tradeoff at this scale - documented, not silent).
SIGNING_SECRET = os.environ.get("DOWNLOAD_SIGNING_SECRET") or uuid.uuid4().hex
if not os.environ.get("DOWNLOAD_SIGNING_SECRET"):
    logger.warning(
        "DOWNLOAD_SIGNING_SECRET not set - using a random per-process secret. "
        "Existing download links will stop working after every restart/redeploy. "
        "Set DOWNLOAD_SIGNING_SECRET to a fixed random string to avoid this."
    )
DOWNLOAD_LINK_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 days

# Most clients are Australian, so every timestamp shown to a client (run
# history, execution reports) is rendered in Melbourne local time rather
# than the server's own clock (which runs UTC on Render) - a fixed,
# server-side timezone rather than detecting the viewer's browser, since a
# downloaded/emailed report or zip has no "current viewer" to detect from.
# %Z prints the correct AEST/AEDT abbreviation automatically across daylight
# saving changes.
MELBOURNE_TZ = ZoneInfo("Australia/Melbourne")


def _melbourne_now_str(fmt: str = "%Y-%m-%d %H:%M %Z") -> str:
    return datetime.now(MELBOURNE_TZ).strftime(fmt)

app = FastAPI(
    title="Req2QA — Requirements to Test Coverage",
    # This app has no public API for third-party integration - the
    # auto-generated Swagger UI / ReDoc / OpenAPI schema would otherwise be
    # reachable by anyone at /docs, /redoc, and /openapi.json, handing out a
    # complete map of every route, form field, and upload endpoint (including
    # the admin dashboard's routes) to any visitor or automated scanner, for
    # no benefit. Disabled outright rather than left exposed by default.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
# Troubleshooting-log dashboard - password-gated, never linked from any
# client-facing page. See app/run_logger.py and app/admin_routes.py.
app.include_router(admin_routes.router)


# --------------------------------------------------------------------------
# Security headers (baseline hardening: clickjacking, MIME-sniffing, referrer
# leakage). CSP allows only self + the Google Fonts hosts the UI actually uses.
# --------------------------------------------------------------------------
@app.middleware("http")
async def _security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    # Explicitly denies browser features this app never uses. Deliberately
    # does NOT restrict clipboard-write - the "Copy Report Link" button on
    # the results pages depends on it.
    response.headers["Permissions-Policy"] = (
        "geolocation=(), microphone=(), camera=(), payment=(), usb=(), "
        "magnetometer=(), gyroscope=(), accelerometer=(), interest-cohort=()"
    )
    # Render terminates TLS in front of this app - every response reaching a
    # real browser is already over HTTPS, so it's safe to always send HSTS
    # (no HTTP-only local-dev path serves this header to a real browser).
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    # script-src intentionally has NO 'unsafe-inline': the one inline script
    # this app used to ship lives in /static/chooser.js now, loaded normally.
    # style-src still allows 'unsafe-inline' because the generated HTML
    # reports (report_builder.py) are self-contained documents with their
    # styling in a <style> block - inline CSS can't execute script, so this
    # is a low-severity, deliberate tradeoff, not an oversight.
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        # plausible.io added for the cookieless analytics script in
        # _footer.html - script-src loads it, connect-src lets it send its
        # page-view beacon. See privacy.html for the corresponding disclosure.
        "script-src 'self' https://plausible.io; "
        "connect-src 'self' https://plausible.io; "
        "frame-ancestors 'none'"
    )
    # Belt-and-suspenders alongside the robots.txt Disallow: a compliant
    # crawler that somehow still fetches an /admin page is told directly not
    # to index it, rather than relying only on the separate robots.txt file.
    if request.url.path.startswith("/admin"):
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


# --------------------------------------------------------------------------
# Access control - fails CLOSED, not open, on misconfiguration.
# --------------------------------------------------------------------------
def _load_access_codes() -> dict:
    """CLIENT_ACCESS_CODES env var: 'client_name:code,client_name2:code2'
    Manual onboarding for a handful of clients - add a pair per new client,
    restart the server. No database needed at this scale."""
    raw = os.environ.get("CLIENT_ACCESS_CODES", "")
    codes = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        name, code = pair.split(":", 1)
        codes[code.strip()] = name.strip()
    return codes


def _lookup_client(codes: dict, submitted_code: str):
    """Constant-time-ish lookup: compares the submitted code against every
    known code with hmac.compare_digest instead of a dict .get(), so how
    quickly a request fails can't be used to infer how many characters of
    a guess were correct. A plain dict lookup short-circuits on the first
    mismatched character internally; iterating + compare_digest for every
    entry means the work done is the same whether the first or last code
    matches, or none does."""
    match = None
    for code, name in codes.items():
        if hmac.compare_digest(code, submitted_code):
            match = name
    return match


# Failed-attempt tracking per client IP (credential-guessing protection).
_failed_attempts: dict = defaultdict(list)
FAILED_ATTEMPT_WINDOW_SECONDS = 15 * 60
FAILED_ATTEMPT_MAX = 8

# By default we do NOT trust X-Forwarded-For. This app is a single Render
# web service; unconditionally trusting a client-suppliable header lets an
# attacker defeat the failed-attempt lockout below just by sending a fresh
# fake IP on every request. Only opt in (TRUST_PROXY_HEADERS=true) if this
# is deployed behind a proxy you control that overwrites/strips any inbound
# X-Forwarded-For before setting its own - never enable this otherwise.
_TRUST_PROXY_HEADERS = os.environ.get("TRUST_PROXY_HEADERS", "").lower() in ("1", "true", "yes")


def _client_ip(request: Request) -> str:
    if _TRUST_PROXY_HEADERS:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _purge_stale_keys(bucket: dict, window_seconds: int, now: float):
    """Drop entries whose window has fully expired. Without this, distinct
    IPs/codes seen over the life of the process accumulate in these dicts
    forever - a slow, unbounded memory leak, and a bigger one if the IP
    itself is attacker-controlled (see _client_ip above)."""
    stale = []
    for k, q in bucket.items():
        while q and now - q[0] > window_seconds:
            (q.popleft() if isinstance(q, deque) else q.pop(0))
        if not q:
            stale.append(k)
    for k in stale:
        del bucket[k]


def _check_rate_limit(bucket: dict, key: str, max_calls: int, window_seconds: int):
    now = time.time()
    if len(bucket) > 500:
        _purge_stale_keys(bucket, window_seconds, now)
    q = bucket.setdefault(key, deque())
    while q and now - q[0] > window_seconds:
        q.popleft()
    if len(q) >= max_calls:
        raise HTTPException(status_code=429, detail="Too many requests. Please wait a few minutes and try again.")
    q.append(now)


_analysis_calls: dict = defaultdict(deque)
ANALYSIS_RATE_MAX = 10
ANALYSIS_RATE_WINDOW_SECONDS = 10 * 60


def _check_access(request: Request, code: str) -> str:
    ip = _client_ip(request)
    now = time.time()
    if len(_failed_attempts) > 500:
        _purge_stale_keys(_failed_attempts, FAILED_ATTEMPT_WINDOW_SECONDS, now)
    attempts = _failed_attempts[ip]
    while attempts and now - attempts[0] > FAILED_ATTEMPT_WINDOW_SECONDS:
        attempts.pop(0)
    if len(attempts) >= FAILED_ATTEMPT_MAX:
        raise HTTPException(status_code=429, detail="Too many failed attempts. Please try again later.")

    codes = _load_access_codes()
    if not codes:
        if os.environ.get("ALLOW_OPEN_ACCESS", "").lower() in ("1", "true", "yes"):
            logger.warning("ALLOW_OPEN_ACCESS is set - running with no access control. Never use this in production.")
            return "test-client"
        # Fail CLOSED: an unset/empty CLIENT_ACCESS_CODES must never silently
        # mean "let everyone in" in a deployed environment.
        logger.error("CLIENT_ACCESS_CODES is not configured - refusing all access requests.")
        raise HTTPException(status_code=503, detail="This service is not yet available. Please contact the operator.")

    client_name = _lookup_client(codes, code)
    if not client_name:
        attempts.append(now)
        raise HTTPException(status_code=401, detail="Invalid access code.")

    # Per-client-code request throttling (protects against a leaked/shared
    # code being used to run up API costs, independent of failed-login abuse).
    _check_rate_limit(_analysis_calls, code, ANALYSIS_RATE_MAX, ANALYSIS_RATE_WINDOW_SECONDS)
    return client_name


# --------------------------------------------------------------------------
# Free-trial access codes (see app/trial_signups.py) - a separate, additive
# lookup that sits in front of the paid CLIENT_ACCESS_CODES check above.
# Generation routes use _check_access_for_generation (allows trial codes,
# one-shot). Live-execution and import-tests use _check_access_no_trial
# (trial codes are explicitly out of scope there - generation only).
# --------------------------------------------------------------------------
def _check_access_for_generation(request: Request, code: str):
    """Returns (client_name, trial_record_or_None). trial_record is the raw
    dict from trial_signups (status == 'unused' at this point) when the code
    is a trial code that hasn't been reserved/used yet - caller is
    responsible for calling trial_signups.reserve_trial() once it has
    decided the document is within budget, and mark_trial_used()/
    release_trial() afterward. Falls back to the existing paid-code check
    (with its own rate limiting) for any code that isn't a trial code."""
    trial = trial_signups.lookup_trial(code)
    if trial is not None:
        if trial["status"] == "used":
            raise HTTPException(
                status_code=403,
                detail="This free trial code has already been used. Please contact kalyan@req2qa.com to continue with a paid engagement.",
            )
        if trial["status"] == "reserved":
            raise HTTPException(
                status_code=429,
                detail=(
                    "This trial code is currently in use, or a previous attempt didn't finish cleanly. "
                    "It will free up automatically within 30 minutes - or email kalyan@req2qa.com "
                    "for an immediate fix."
                ),
            )
        return f"trial:{trial['company']}", trial
    return _check_access(request, code), None


def _check_access_no_trial(request: Request, code: str) -> str:
    """For live execution and test-case import - the free trial is
    generation-only, so a trial code is rejected outright here rather than
    silently falling through to the paid-code check (which would just say
    'invalid code' and confuse a trial user about why)."""
    if trial_signups.lookup_trial(code) is not None:
        raise HTTPException(
            status_code=403,
            detail="Your free trial covers test-case generation only. Live execution and test-case import require a paid engagement - contact kalyan@req2qa.com.",
        )
    return _check_access(request, code)


_signup_calls: dict = defaultdict(deque)
SIGNUP_RATE_MAX = 5
SIGNUP_RATE_WINDOW_SECONDS = 60 * 60


# --------------------------------------------------------------------------
# Safe error handling: log full details server-side, show a generic message
# with a short correlation reference to the user. Never leak exception text
# (which can name internal libraries/vendors) into the browser.
# --------------------------------------------------------------------------
def _log_and_ref(exc: Exception, context: str) -> str:
    ref = uuid.uuid4().hex[:8]
    logger.exception("[ref=%s] %s", ref, context)
    return ref


GENERIC_ERROR_MESSAGE = "Something went wrong on our end (reference: {ref}). Please try again, or contact support with this reference if it keeps happening."


# --------------------------------------------------------------------------
# Signed, time-limited download tokens - a report link works for a bounded
# window rather than indefinitely for anyone who has the URL.
# --------------------------------------------------------------------------
def _make_download_token(run_id: str, ttl_seconds: int = DOWNLOAD_LINK_TTL_SECONDS) -> str:
    expiry = int(time.time()) + ttl_seconds
    msg = f"{run_id}:{expiry}".encode()
    sig = hmac.new(SIGNING_SECRET.encode(), msg, hashlib.sha256).hexdigest()[:32]
    return f"{expiry}.{sig}"


def _verify_download_token(run_id: str, token: str) -> bool:
    if not token or "." not in token:
        return False
    expiry_str, _, sig = token.partition(".")
    try:
        expiry = int(expiry_str)
    except ValueError:
        return False
    if time.time() > expiry:
        return False
    msg = f"{run_id}:{expiry}".encode()
    expected = hmac.new(SIGNING_SECRET.encode(), msg, hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(expected, sig)


# --------------------------------------------------------------------------
# Client-facing run history - deliberately its own small store, separate
# from the 90-day internal troubleshooting log (run_logger.py). Holds only
# what a client needs to find a past run of their own: no step-by-step
# detail, no credentials, no document content - just enough to relist a run
# and re-mint a fresh download link for it while it's still inside its
# 7-day report window. Entries outlive that window (so a client can see
# "yes, this ran" and know to re-run it) but are pruned well before they'd
# become an unbounded record - see HISTORY_RETENTION_SECONDS.
# --------------------------------------------------------------------------
HISTORY_PATH = RUNS_DIR / "client_history.jsonl"
HISTORY_RETENTION_SECONDS = 30 * 24 * 60 * 60  # 30 days


def _append_history(kind: str, run_id: str, client_name: str, initials: str, application: str, run_date: str, exec_id: str = ""):
    try:
        with open(HISTORY_PATH, "a") as f:
            f.write(json.dumps({
                "logged_at": time.time(),
                "kind": kind,          # "report" (analyze/custom/import) or "execute"
                "run_id": run_id,
                "exec_id": exec_id,
                "client_name": client_name,
                "initials": (initials or "").strip()[:40],
                "application": application,
                "run_date": run_date,
            }) + "\n")
    except OSError:
        logger.exception("Failed to append to client_history.jsonl")


def _read_history(client_name: str):
    entries = []
    if not HISTORY_PATH.exists():
        return entries
    try:
        with open(HISTORY_PATH) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("client_name") == client_name:
                    entries.append(row)
    except OSError:
        return entries
    entries.sort(key=lambda r: r.get("logged_at", 0), reverse=True)
    return entries


def _prune_history():
    if not HISTORY_PATH.exists():
        return
    cutoff = time.time() - HISTORY_RETENTION_SECONDS
    try:
        kept_lines = []
        with open(HISTORY_PATH) as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    row = json.loads(stripped)
                except ValueError:
                    continue
                if row.get("logged_at", 0) >= cutoff:
                    kept_lines.append(stripped)
        with open(HISTORY_PATH, "w") as f:
            f.write("\n".join(kept_lines) + ("\n" if kept_lines else ""))
    except OSError:
        logger.exception("Failed to prune client_history.jsonl")


# --------------------------------------------------------------------------
# Upload content validation: a file's extension is what the user/browser
# claims it is, not what it actually is. Checking the real file signature
# (magic bytes) before handing it to python-docx/openpyxl/pypdf/PyMuPDF
# stops a mismatched or malformed file (deliberately renamed, or corrupt)
# from reaching those parsers as if it were trusted input of that type.
# --------------------------------------------------------------------------
def _sniff_kind(raw: bytes) -> str:
    if raw.startswith(b"%PDF-"):
        return "pdf"
    if raw.startswith(b"PK\x03\x04"):
        return "zip"  # .docx / .xlsx / .xlsm are all zip containers
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp"
    return "unknown"


_EXT_EXPECTED_KIND = {
    ".pdf": {"pdf"},
    ".docx": {"zip"},
    ".xlsx": {"zip"},
    ".xlsm": {"zip"},
    ".png": {"png"},
    ".jpg": {"jpeg"},
    ".jpeg": {"jpeg"},
    ".webp": {"webp"},
    # .txt / .csv have no reliable magic bytes - any content is plausible.
}


# --------------------------------------------------------------------------
# Field length caps (governance, not content shaping) - see
# claude/ux-investigation-required-field-markers-and-input-validation-2026-09-17.md
# and the follow-up cap-sizing discussion in that same thread. Every number
# here was picked to sit comfortably above the longest realistic value for
# the platforms this tool already targets (Salesforce, SuccessFactors,
# Workday, UKG, ServiceNow, Humanforce) - the goal is a backstop against
# genuinely oversized/abusive input, not a "clean round number" that would
# truncate normal client data. Enforced server-side here because a form's
# HTML maxlength is a client-side convenience only and does not stop a
# direct POST with a longer value.
FIELD_MAX_LENGTHS = {
    # short identifier / label
    "flow_name": 60, "screen_or_stage": 60, "tc_id": 60,
    # app / module / role names
    "application": 100, "module": 100, "role_label": 100, "baseline_version": 100,
    # test-case title
    "title": 150,
    # structured list field (comma-separated)
    "inputs": 300,
    # sandbox/UAT environment URL
    "env_url": 300,
    # login fields - generous on purpose (must never be the reason a real,
    # valid username/password is rejected); password cap is an abuse
    # ceiling only, per OWASP guidance against tight password-length caps
    "username": 150, "password": 128,
    # free-text / prose - generous, only guards against a genuinely
    # oversized paste, never meant to shape normal sentence-length content
    "decision_detail": 2000,
}


def _check_field_length(value: str, field_name: str) -> str | None:
    """Returns an error message if value exceeds FIELD_MAX_LENGTHS[field_name],
    else None. Unknown field_name is a programming error, not a user error -
    raises so a typo'd key is caught in testing rather than silently no-op'ing."""
    limit = FIELD_MAX_LENGTHS[field_name]
    if len(value) > limit:
        return f"{field_name.replace('_', ' ').capitalize()} must be {limit} characters or fewer (you entered {len(value)})."
    return None


def _validate_upload(filename: str, raw: bytes) -> bool:
    """Returns False if filename's extension implies a binary format whose
    signature doesn't match the actual bytes. Extensions with no reliable
    signature (.txt, .csv) always pass - there's nothing meaningful to check."""
    if not filename or "." not in filename:
        return True
    ext = "." + filename.rsplit(".", 1)[-1].lower()
    expected = _EXT_EXPECTED_KIND.get(ext)
    if not expected:
        return True
    return _sniff_kind(raw) in expected


# --------------------------------------------------------------------------
# Live execution (Phase 2): SSRF guard on the client-supplied environment
# URL. This feature makes the server itself issue an outbound request to
# whatever URL a caller provides - without a check, a caller who has a
# valid access code could point it at the server's own internal network
# (localhost, a cloud metadata endpoint, an internal admin panel) rather
# than the client's actual sandbox. Reject anything that isn't a plain
# http(s) URL resolving to a public address before ever launching a browser.
# NOTE: this resolves the hostname once at validation time; a DNS answer
# that changes between this check and Playwright's own connection (DNS
# rebinding) is a known, accepted residual risk at this scale, not a gap
# that's been silently ignored.
# --------------------------------------------------------------------------
def _is_private_or_reserved(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast


def _validate_env_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    try:
        resolved_ips = {info[4][0] for info in socket.getaddrinfo(parsed.hostname, None)}
    except socket.gaierror:
        return False
    if not resolved_ips:
        return False
    return not any(_is_private_or_reserved(ip) for ip in resolved_ips)


# Live execution is the most expensive and highest-risk endpoint (a real
# headless browser + multiple LLM calls + touches a client's environment,
# even if only a sandbox) - throttled more tightly than analysis, and each
# request is capped to a small number of test cases run sequentially in
# isolated browser sessions.
_execution_calls: dict = defaultdict(deque)
EXECUTION_RATE_MAX = 5
EXECUTION_RATE_WINDOW_SECONDS = 30 * 60
MAX_TEST_CASES_PER_EXECUTION = 25
EXECUTIONS_MAX_AGE_SECONDS = DOWNLOAD_LINK_TTL_SECONDS + 24 * 60 * 60

# Screenshots-per-test-case field on the execution form (2026-09-16) - shown
# to the client as an editable default, not a silent internal cap. See
# execution_report.py for the verdict-aware selection this feeds into.
DEFAULT_MAX_SCREENSHOTS_PER_TEST = 20
MAX_SCREENSHOTS_PER_TEST_CEILING = 50


def _sweep_pending():
    """Opportunistic cleanup of abandoned Option B review sessions (a flow
    parsed but never confirmed) so sensitive business documents don't sit on
    disk indefinitely. Called on every /analyze-custom call - cheap at this
    scale, no separate scheduler needed."""
    now = time.time()
    try:
        for entry in PENDING_DIR.iterdir():
            if not entry.is_dir():
                continue
            try:
                if now - entry.stat().st_mtime > PENDING_MAX_AGE_SECONDS:
                    shutil.rmtree(entry, ignore_errors=True)
            except OSError:
                continue
    except FileNotFoundError:
        pass


# Completed-run reports/workbooks used to sit in RUNS_DIR forever - a
# download link expires after DOWNLOAD_LINK_TTL_SECONDS, but the files it
# pointed to didn't. Sweep run directories older than the link's own TTL
# (plus a short grace window) so client documents don't outlive the last
# link that could reach them.
RUN_RETENTION_SECONDS = DOWNLOAD_LINK_TTL_SECONDS + 24 * 60 * 60


def _sweep_completed_runs():
    now = time.time()
    try:
        for entry in RUNS_DIR.iterdir():
            if not entry.is_dir() or entry.name == "pending":
                continue
            try:
                if now - entry.stat().st_mtime > RUN_RETENTION_SECONDS:
                    shutil.rmtree(entry, ignore_errors=True)
            except OSError:
                continue
    except FileNotFoundError:
        pass
    # Troubleshooting logs run on their own, longer (90-day) retention clock,
    # independent of report/screenshot retention above - same sweep cadence
    # (called on every request that already calls this) is fine at this scale.
    try:
        _prune_history()
    except Exception:
        logger.exception("_prune_history failed")
    try:
        run_logger.cleanup_old_logs()
    except Exception:
        pass


# --------------------------------------------------------------------------
# Canonical URL / sitemap (2026-09-17 - see claude/site-indexing-fix-...) -
# Search Console reported "Page with redirect" for pages discovered via a
# trailing-slash, http://, or www. variant, all of which correctly redirect
# to https://req2qa.com/<path> (confirmed live) but leave Google with no
# upfront signal of that canonical form. Fixing that is two additive pieces:
# a self-referencing <link rel="canonical"> on every page, and a sitemap.xml
# listing only the canonical URLs.
#
# SITE_BASE_URL has no trailing slash; every entry below is a bare path
# (leading slash, no trailing slash) - keep both conventions consistent with
# how the live redirects actually resolve (confirmed 2026-09-17: http,
# https+www, and any trailing slash all converge on this exact form).
#
# PUBLIC_PAGE_PATHS is a deliberate, hand-maintained allowlist - NOT derived
# from the route table - because this app also serves per-run report pages,
# downloads, and admin routes that are access-code-gated, client-specific,
# or time-limited, and must never appear in a sitemap. When a new static,
# publicly-crawlable page is added to the site, add its path here too.
SITE_BASE_URL = "https://req2qa.com"
PUBLIC_PAGE_PATHS = ["/", "/about", "/security", "/privacy", "/terms", "/trial-signup", "/import-tests"]


def _canonical_url(path: str) -> str:
    # The root path is the one exception to "no trailing slash": confirmed
    # live (2026-09-17) that https://req2qa.com resolves to https://req2qa.com/
    # (trailing slash), matching the existing og:url meta tag in index.html -
    # every other path resolves WITHOUT a trailing slash, per the same check.
    return f"{SITE_BASE_URL}/" if path == "/" else f"{SITE_BASE_URL}{path}"


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {"error": None, "canonical_url": _canonical_url("/")})


@app.get("/sitemap.xml", response_class=Response)
async def sitemap_xml():
    urls = "\n".join(f"  <url><loc>{_canonical_url(p)}</loc></url>" for p in PUBLIC_PAGE_PATHS)
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{urls}\n"
        "</urlset>\n"
    )
    return Response(content=xml, media_type="application/xml")


# Static trust pages - no access code required, nothing sensitive is served.
# Their presence (and the footer links to them) is itself part of what a
# regulated client's security/procurement review and some automated URL
# categorization scanners look for on a new vendor's site.
_TRUST_PAGE_UPDATED = "2026-09-09"


@app.get("/privacy", response_class=HTMLResponse)
async def privacy(request: Request):
    return templates.TemplateResponse(request, "privacy.html", {"updated_date": _TRUST_PAGE_UPDATED, "canonical_url": _canonical_url("/privacy")})


@app.get("/terms", response_class=HTMLResponse)
async def terms(request: Request):
    return templates.TemplateResponse(request, "terms.html", {"updated_date": _TRUST_PAGE_UPDATED, "canonical_url": _canonical_url("/terms")})


@app.get("/security", response_class=HTMLResponse)
async def security_page(request: Request):
    return templates.TemplateResponse(request, "security.html", {"updated_date": _TRUST_PAGE_UPDATED, "canonical_url": _canonical_url("/security")})


@app.get("/about", response_class=HTMLResponse)
async def about_page(request: Request):
    return templates.TemplateResponse(request, "about.html", {"canonical_url": _canonical_url("/about")})


# --------------------------------------------------------------------------
# Self-serve free-trial signup (see app/trial_signups.py for the full design
# rationale). No password/session is created here - a successful signup only
# emails a one-shot access code, which is then entered into the existing
# access-code field on the homepage like any other client.
# --------------------------------------------------------------------------
@app.get("/trial-signup", response_class=HTMLResponse)
async def trial_signup_form(request: Request, error: str = ""):
    return templates.TemplateResponse(request, "trial_signup.html", {"error": error, "canonical_url": _canonical_url("/trial-signup")})


@app.post("/trial-signup", response_class=HTMLResponse)
async def trial_signup_submit(
    request: Request,
    company: str = Form(...),
    contact_name: str = Form(...),
    email: str = Form(...),
    application: str = Form(""),
):
    ip = _client_ip(request)
    try:
        _check_rate_limit(_signup_calls, ip, SIGNUP_RATE_MAX, SIGNUP_RATE_WINDOW_SECONDS)
    except HTTPException as e:
        return templates.TemplateResponse(request, "trial_signup.html", {"error": e.detail}, status_code=e.status_code)

    ok, result = trial_signups.create_signup(company, contact_name, email, application)
    if not ok:
        return templates.TemplateResponse(request, "trial_signup.html", {"error": result}, status_code=400)

    code = result
    # Re-sanitize the same way create_signup did internally, so nothing
    # unsanitized reaches an email header/subject here either.
    company = trial_signups.sanitize_header_text(company.strip())
    contact_name = trial_signups.sanitize_header_text(contact_name.strip())
    application = trial_signups.sanitize_header_text(application.strip())

    sent, _msg = mailer.send_email(
        to_addr=email.strip(),
        subject="Your Req2QA free trial access code",
        body_text=(
            f"Hi {contact_name.strip()},\n\n"
            "Thanks for signing up for a Req2QA free trial. Your one-time access code is:\n\n"
            f"    {code}\n\n"
            "How to use it:\n"
            "1. Go to https://req2qa.com\n"
            "2. Upload your requirements document and enter this code as your access code.\n"
            "3. Your free trial covers a single generation run, up to about 10 test cases.\n\n"
            "Questions, or need a bigger engagement sized to your real test suite? "
            "Just reply to this email or reach out to kalyan@req2qa.com.\n\n"
            "— Req2QA"
        ),
    )
    # Also alert Kalyan directly to the new signup (separate from the BCC
    # every send already carries) so a new lead is never missed.
    mailer.send_email(
        to_addr="kalyan@req2qa.com",
        subject=f"New Req2QA trial signup: {company.strip()}",
        body_text=(
            f"Company: {company.strip()}\n"
            f"Contact: {contact_name.strip()}\n"
            f"Email: {email.strip()}\n"
            f"What they're testing: {application.strip() or '(not specified)'}\n"
            f"Code issued: {code}\n"
        ),
    )

    return templates.TemplateResponse(request, "trial_signup_sent.html", {"email_sent": sent})


@app.get("/robots.txt", response_class=PlainTextResponse)
async def robots_txt():
    # No robots.txt previously existed, which some crawlers/URL-categorization
    # scanners treat conservatively as "don't index anything" - an explicit,
    # permissive policy removes that ambiguity and helps legitimate crawlers
    # (Google, Bing, and vendor categorization bots) index and classify the
    # site correctly instead of leaving it "uncategorized."
    # /admin is password-gated regardless (this is presentation, not access
    # control) - excluding it just keeps the internal dashboard out of
    # search results rather than have "req2qa.com admin login" indexable.
    return f"User-agent: *\nAllow: /\nDisallow: /admin\n\nSitemap: {SITE_BASE_URL}/sitemap.xml\n"


@app.post("/analyze", response_class=HTMLResponse)
async def analyze(
    request: Request,
    access_code: str = Form(...),
    application: str = Form(...),
    requirements_file: UploadFile = File(...),
    baseline_version: str = Form(""),
    initials: str = Form(""),
    client_test_data: str = Form(""),
    process_diagram_file: UploadFile | None = File(None),
    process_description: str = Form(""),
    process_frame: str = Form(""),
):
    _sweep_completed_runs()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.error("ANTHROPIC_API_KEY is not configured.")
        return templates.TemplateResponse(request, "index.html",
            {"error": "This service is not yet available. Please contact the operator."},
            status_code=500,
        )

    try:
        client_name, trial = _check_access_for_generation(request, access_code)
    except HTTPException as e:
        return templates.TemplateResponse(request, "index.html", {"error": e.detail}, status_code=e.status_code
        )

    for field_name, field_value in (("application", application), ("baseline_version", baseline_version)):
        length_error = _check_field_length(field_value, field_name)
        if length_error:
            return templates.TemplateResponse(request, "index.html", {"error": length_error}, status_code=400)

    raw_bytes = await requirements_file.read()
    if not raw_bytes:
        return templates.TemplateResponse(request, "index.html", {"error": "The uploaded file is empty."}, status_code=400
        )
    if len(raw_bytes) > MAX_DOC_BYTES:
        return templates.TemplateResponse(request, "index.html",
            {"error": f"That file is too large (max {MAX_DOC_BYTES // (1024*1024)} MB)."},
            status_code=400,
        )
    if not _validate_upload(requirements_file.filename, raw_bytes):
        return templates.TemplateResponse(request, "index.html",
            {"error": "That file doesn't look like a valid document of its type. Please re-export and try again."},
            status_code=400,
        )

    try:
        req_text = extract_text(requirements_file.filename, raw_bytes)
    except Exception as e:
        ref = _log_and_ref(e, "extract_text failed in /analyze")
        return templates.TemplateResponse(request, "index.html", {"error": GENERIC_ERROR_MESSAGE.format(ref=ref)}, status_code=400
        )

    if not req_text.strip():
        return templates.TemplateResponse(request, "index.html",
            {"error": "No text could be extracted from that file."},
            status_code=400,
        )

    if trial is not None:
        if not trial_signups.check_word_count(req_text):
            return templates.TemplateResponse(request, "index.html",
                {"error": (
                    "This document looks larger than what the free trial supports "
                    f"(up to {trial_signups.TRIAL_MAX_REQ_WORDS} words / roughly 10 test cases). "
                    "Please contact kalyan@req2qa.com for a paid engagement sized to your document."
                )},
                status_code=400,
            )
        reserved, reserve_msg = trial_signups.reserve_trial(access_code)
        if not reserved:
            return templates.TemplateResponse(request, "index.html",
                {"error": reserve_msg or "This trial code is no longer available."},
                status_code=409,
            )
    else:
        # Paid-client quota check (see client_quotas.py) - a client with no
        # quota configured is unrestricted. Checked BEFORE calling the
        # Anthropic API so a blocked attempt never spends a token.
        allowed, block_reason = client_quotas.check_can_generate(access_code)
        if not allowed:
            return templates.TemplateResponse(request, "index.html", {"error": block_reason}, status_code=429
            )

    # Process Coverage Insights (Beta) - council-reviewed and copy-locked,
    # see claude/process-gap-analysis-design-consensus-2026-09-16.md and
    # claude/process-coverage-insights-final-copy-2026-09-16.md. Paid-tier
    # only (decision 6 of the consensus doc) - a trial run never builds a
    # process_context, regardless of what was submitted, mirroring the same
    # trial-exclusion pattern already used for client_test_data.
    process_context = None
    process_source_label = ""
    if trial is None:
        diagram_present = process_diagram_file is not None and bool(process_diagram_file.filename)
        description_text = process_description.strip()
        if diagram_present or description_text:
            frame = process_frame.strip().lower()
            if frame not in ("current", "target"):
                return templates.TemplateResponse(request, "index.html",
                    {"error": "Please choose whether your process diagram/description shows your current or target process."},
                    status_code=400,
                )
            if diagram_present:
                diagram_bytes = await process_diagram_file.read()
                if diagram_bytes and len(diagram_bytes) <= MAX_IMAGE_BYTES and _validate_upload(process_diagram_file.filename, diagram_bytes):
                    try:
                        flow = parse_flow_diagrams(application, [(process_diagram_file.filename, diagram_bytes)], api_key)
                        if flow.get("steps"):
                            process_context = {"frame": frame, "steps": flow["steps"]}
                            process_source_label = "diagram"
                    except Exception as e:
                        _log_and_ref(e, "parse_flow_diagrams failed for optional process diagram in /analyze")
                        # Non-fatal - Process Coverage Insights is additive; a
                        # failed diagram parse falls back to the description
                        # text below if present, or is silently skipped.
                if process_context is None and description_text:
                    process_context = {"frame": frame, "raw_text": description_text}
                    process_source_label = "description"
            elif description_text:
                process_context = {"frame": frame, "raw_text": description_text}
                process_source_label = "description"

    run_id = uuid.uuid4().hex[:12]
    rl = run_logger.RunLog.start("generate_a", run_id, {
        "application": application,
        "filename": requirements_file.filename,
        "file_size": len(raw_bytes),
        "qa_model": os.environ.get("QA_MODEL"),
        "process_context": process_source_label or None,
    })
    try:
        max_tcs = trial_signups.TRIAL_MAX_TEST_CASES if trial is not None else None
        data = run_qa_analysis(application, req_text, api_key, max_test_cases=max_tcs, process_context=process_context)
    except Exception as e:
        ref = _log_and_ref(e, "run_qa_analysis failed in /analyze")
        rl.finish("fail", {"correlation_ref": ref, "error": str(e)})
        if trial is not None:
            trial_signups.release_trial(access_code)
        return templates.TemplateResponse(request, "index.html", {"error": GENERIC_ERROR_MESSAGE.format(ref=ref)}, status_code=502
        )

    if trial is not None and len(data.get("test_scenarios", [])) > trial_signups.TRIAL_MAX_TEST_CASES:
        # Belt-and-suspenders: the prompt already asked for at most this many,
        # but never trust a model to strictly honor an instruction when real
        # cost/business logic depends on it - truncate the report itself so
        # the trial's hard cap holds even if the model overshot.
        data["test_scenarios"] = data["test_scenarios"][: trial_signups.TRIAL_MAX_TEST_CASES]

    data["application"] = application
    data["requirements_source"] = requirements_file.filename
    data["run_date"] = _melbourne_now_str()
    data["baseline_version"] = baseline_version.strip()
    data["initials"] = initials

    # Process Coverage Insights (Beta) - persist what the model returned so
    # the curation step (paid clients only) can show it for review/edit, and
    # the final report can render it. "process_steps"/"uncovered_process_steps"
    # come straight from the model's JSON when process_context was set above;
    # default to empty so nothing downstream needs a None-check.
    data["process_context_provided"] = process_context is not None
    data["process_frame"] = (process_context or {}).get("frame", "")
    data["process_source"] = process_source_label
    data.setdefault("process_steps", [])
    data.setdefault("uncovered_process_steps", [])

    # Client-supplied test data for custom workflows the automation cannot
    # create itself in one run (e.g. an employee who has already resigned
    # and been offboarded) - council-reviewed 2026-09-16, see
    # claude/council-review-client-supplied-test-data-devils-advocate.md and
    # app/fixtures.py. Scoped to this one run (not the cross-run registry);
    # the PII heuristic only ever warns, never blocks.
    #
    # Trial codes never reach live execution (_check_access_no_trial rejects
    # them outright), so this field is structurally unusable for a trial
    # run - it's already hidden client-side for a "TRIAL-..." code (a
    # founder-decided, presentation-only difference between the trial and
    # paid experience, 2026-09-16), and here on the server it's discarded
    # outright rather than merely ignored: no reason to retain 7 days of
    # whatever was pasted in if it can never be used, and this holds even if
    # the client-side hide is bypassed (view-source, JS disabled, etc.) -
    # the field is a no-op for trial regardless of how it's reached.
    if trial is not None:
        data["client_test_data"] = []
        data["client_test_data_pii_flags"] = []
    else:
        data["client_test_data"] = fixtures.parse_client_seed_text(client_test_data)
        data["client_test_data_pii_flags"] = fixtures.scan_for_pii_flags(client_test_data)

    total_reqs = len(data.get("validation", []))
    flagged = sum(1 for v in data.get("validation", []) if not v.get("valid_for_app"))
    total_tcs = len(data.get("test_scenarios", []))

    if trial is None:
        # Paid clients go through the keep/curation review step (see
        # claude/billing-model-unbundled-generation-execution-2026-09-15.md)
        # before anything is billed or finalized - nothing is written to
        # report.html/data.xlsx/run_log.csv yet, only a working data.json so
        # /confirm-generated/{run_id} can pick this run back up.
        client_quotas.record_generation_attempt(access_code)
        run_dir = RUNS_DIR / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        data["status"] = "awaiting_curation"
        (run_dir / "data.json").write_text(json.dumps(data))
        rl.finish("ok", {"test_case_count": total_tcs, "flagged_requirements": flagged, "status": "awaiting_curation"})

        quota = client_quotas.get_quota(access_code)
        quota_note = None
        if quota is not None:
            quota_note = (
                f"This engagement has kept {quota['consumed_count']} of its "
                f"{quota['subscribed_count']}-test-case subscribed allowance so far "
                f"({quota['attempt_count']}/{client_quotas.MAX_GENERATION_ATTEMPTS} generation attempts used)."
            )
        return templates.TemplateResponse(request, "review_generated.html",
            {
                "run_id": run_id,
                "application": application,
                "test_cases": data.get("test_scenarios", []),
                "quota_note": quota_note,
                "client_test_data_pii_flags": data.get("client_test_data_pii_flags", []),
                "process_context_provided": data.get("process_context_provided", False),
                "process_frame": data.get("process_frame", ""),
                "process_source": data.get("process_source", ""),
                "process_steps": data.get("process_steps", []),
                "uncovered_process_steps": data.get("uncovered_process_steps", []),
                "error": None,
                "hide_trial_cta": True,
            },
        )

    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    html_path = run_dir / "report.html"
    xlsx_path = run_dir / "data.xlsx"
    html_path.write_text(build_html(data))
    build_xlsx(data, str(xlsx_path))
    # Persisted so /execute/{run_id} can look up this run's generated test
    # cases later, without re-running generation.
    (run_dir / "data.json").write_text(json.dumps(data))

    # Basic run log per client (manual-onboarding scale; swap for a real DB later)
    with open(RUNS_DIR / "run_log.csv", "a") as f:
        f.write(f"{run_id},{client_name},{data['run_date']},{application},{requirements_file.filename}\n")
    _append_history("report", run_id, client_name, initials, application, data["run_date"])

    rl.finish("ok", {"test_case_count": total_tcs, "flagged_requirements": flagged})
    trial_signups.mark_trial_used(access_code, run_id, total_tcs)

    return templates.TemplateResponse(request, "result.html",
        {
            "run_id": run_id,
            "download_token": _make_download_token(run_id),
            "client_name": client_name,
            "application": application,
            "mode": "named",
            "total_reqs": total_reqs,
            "flagged": flagged,
            "total_tcs": total_tcs,
            "baseline_version": data["baseline_version"],
            "client_test_data_pii_flags": data.get("client_test_data_pii_flags", []),
            "hide_trial_cta": True,
        },
    )


@app.post("/confirm-generated/{run_id}", response_class=HTMLResponse)
async def confirm_generated(request: Request, run_id: str, access_code: str = Form(...)):
    """Finalizes a paid-client generation run after the keep/curation review
    step (see /analyze above and claude/billing-model-unbundled-generation-
    execution-2026-09-15.md). Only test cases the client kept are billed
    (consumed against their quota) and written into the final report/
    data.json - the discarded ones are never counted or persisted onward."""
    if not run_id.isalnum():
        raise HTTPException(status_code=400)
    data_path = RUNS_DIR / run_id / "data.json"
    if not data_path.exists():
        raise HTTPException(status_code=404, detail="This run was not found or has expired. Please run the analysis again.")

    try:
        data = json.loads(data_path.read_text())
    except Exception as e:
        ref = _log_and_ref(e, "failed to load data.json in /confirm-generated")
        raise HTTPException(status_code=500, detail=GENERIC_ERROR_MESSAGE.format(ref=ref))

    if data.get("status") != "awaiting_curation":
        raise HTTPException(status_code=409, detail="This run has already been confirmed or is not awaiting review.")

    def err(msg, code=400):
        return templates.TemplateResponse(request, "review_generated.html",
            {
                "run_id": run_id, "application": data.get("application", ""),
                "test_cases": data.get("test_scenarios", []), "quota_note": None, "error": msg,
                "process_context_provided": data.get("process_context_provided", False),
                "process_frame": data.get("process_frame", ""),
                "process_source": data.get("process_source", ""),
                "process_steps": data.get("process_steps", []),
                "uncovered_process_steps": data.get("uncovered_process_steps", []),
                "hide_trial_cta": True,
            },
            status_code=code,
        )

    try:
        client_name = _check_access_no_trial(request, access_code)
    except HTTPException as e:
        return err(e.detail, code=e.status_code)

    form = await request.form()
    all_cases = data.get("test_scenarios", [])
    kept_cases = [tc for tc in all_cases if form.get(f"keep_{tc.get('tc_id')}")]
    if not kept_cases:
        return err("Keep at least one test case, or start a new generation if none of these are usable.")

    data["test_scenarios"] = kept_cases
    data.pop("status", None)

    # Process Coverage Insights (Beta) - the curation screen is where the
    # client reviews/corrects the parsed process steps (per the consensus
    # design: reuse this existing screen rather than add a new confirm-step),
    # and where uncovered_process_steps gets recomputed against the KEPT test
    # cases only, since discarding a test case can turn a covered step back
    # into an uncovered one. A step is kept unless its checkbox was explicitly
    # unchecked - matching the "anything you remove won't be included" copy.
    # Editing a step's description/screen text is deliberately not offered
    # here (v1 scope) - only keep/remove, to bound the size of this change;
    # a misread step can still be discarded rather than corrected in place.
    if data.get("process_context_provided"):
        all_steps = data.get("process_steps", [])
        # A checkbox absent from the submitted form means the client
        # unchecked it (standard HTML checkbox semantics, same rule already
        # used for keep_<tc_id> above).
        kept_step_ids = {st.get("step_id") for st in all_steps if form.get(f"keep_process_{st.get('step_id')}")}
        kept_steps = [st for st in all_steps if st.get("step_id") in kept_step_ids]
        covered_step_ids = set()
        for tc in kept_cases:
            for sid in tc.get("flow_step_ids", []) or []:
                covered_step_ids.add(sid)
        data["process_steps"] = kept_steps
        data["uncovered_process_steps"] = [st.get("step_id") for st in kept_steps if st.get("step_id") not in covered_step_ids]

    run_dir = RUNS_DIR / run_id
    html_path = run_dir / "report.html"
    xlsx_path = run_dir / "data.xlsx"
    html_path.write_text(build_html(data))
    build_xlsx(data, str(xlsx_path))
    # Overwritten with the KEPT subset only - this is also what
    # /execute/{run_id} reads later, so execution selection naturally only
    # offers the cases the client actually kept.
    (run_dir / "data.json").write_text(json.dumps(data))

    kept_count = len(kept_cases)
    total_reqs = len(data.get("validation", []))
    flagged = sum(1 for v in data.get("validation", []) if not v.get("valid_for_app"))

    with open(RUNS_DIR / "run_log.csv", "a") as f:
        f.write(f"{run_id},{client_name},{data['run_date']},{data.get('application','')},{data.get('requirements_source','')}\n")
    _append_history("report", run_id, client_name, data.get("initials", ""), data.get("application", ""), data["run_date"])

    # Separate, admin-visible log of KEPT count - distinct from the raw
    # generated count already captured in run_logger, specifically so
    # generation can be invoiced on kept count (see client_quotas.py).
    with open(RUNS_DIR / "kept_log.csv", "a") as f:
        f.write(f"{run_id},{client_name},{access_code},{data['run_date']},{kept_count},{len(all_cases)}\n")

    new_consumed, quota_warning = client_quotas.record_kept(access_code, kept_count)
    # 2026-09-16 council verdict: the execution allowance for this engagement
    # is exactly the kept count, disclosed and enforced identically - no
    # hidden buffer. See client_quotas.set_execution_allowance.
    client_quotas.set_execution_allowance(access_code, kept_count)

    return templates.TemplateResponse(request, "result.html",
        {
            "run_id": run_id,
            "download_token": _make_download_token(run_id),
            "client_name": client_name,
            "application": data.get("application", ""),
            "mode": "named",
            "total_reqs": total_reqs,
            "flagged": flagged,
            "total_tcs": kept_count,
            "baseline_version": data.get("baseline_version", ""),
            "quota_warning": quota_warning,
            "hide_trial_cta": True,
        },
    )


# --------------------------------------------------------------------------
# Option B - Custom Application Mode
#
# Two-step flow, per the design's non-negotiable review-and-confirm
# checkpoint: /analyze-custom parses the uploaded diagram(s) and shows the
# extracted process flow back in plain language; nothing is generated until
# the user confirms (or corrects) it via /confirm-flow/{run_id}.
# --------------------------------------------------------------------------

@app.post("/analyze-custom", response_class=HTMLResponse)
async def analyze_custom(
    request: Request,
    access_code: str = Form(...),
    application: str = Form(...),
    baseline_version: str = Form(""),
    element_hints: str = Form(""),
    initials: str = Form(""),
    brief_file: UploadFile = File(...),
    diagram_files: List[UploadFile] = File(...),
    requirements_file: UploadFile = File(...),
):
    _sweep_pending()
    _sweep_completed_runs()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.error("ANTHROPIC_API_KEY is not configured.")
        return templates.TemplateResponse(request, "index.html",
            {"error": "This service is not yet available. Please contact the operator.", "active_tab": "custom"},
            status_code=500,
        )

    try:
        client_name, trial = _check_access_for_generation(request, access_code)
    except HTTPException as e:
        return templates.TemplateResponse(request, "index.html", {"error": e.detail, "active_tab": "custom"}, status_code=e.status_code
        )
    if trial is not None:
        # Custom Application Mode's cost is fundamentally hard to bound
        # before calling the API - it drives a vision-based diagram-parsing
        # call whose cost tracks image count/complexity, not word count, so
        # the same pre-call size guard used for text-only generation can't
        # meaningfully cap it. Rather than ship a weak, easily-bypassed
        # heuristic, the free trial is scoped to Option A (text requirements
        # only) - custom-application testing is a paid-tier feature.
        return templates.TemplateResponse(request, "index.html",
            {
                "error": (
                    "Free trials cover standard requirements-document generation only. "
                    "Custom Application Mode (diagram-based testing) is part of a paid engagement - "
                    "contact kalyan@req2qa.com to get set up."
                ),
                "active_tab": "custom",
            },
            status_code=403,
        )

    def err(msg, code=400):
        return templates.TemplateResponse(request, "index.html", {"error": msg, "active_tab": "custom"}, status_code=code
        )

    for field_name, field_value in (("application", application), ("baseline_version", baseline_version)):
        length_error = _check_field_length(field_value, field_name)
        if length_error:
            return err(length_error)

    brief_bytes = await brief_file.read()
    req_bytes = await requirements_file.read()
    diagram_reads = [(f.filename, await f.read()) for f in diagram_files if f.filename]

    if not brief_bytes:
        return err("The project brief file is empty.")
    if not req_bytes:
        return err("The requirements file is empty.")
    if not diagram_reads or not any(raw for _, raw in diagram_reads):
        return err("At least one process-flow diagram (image or PDF) is required for Custom Application Mode.")

    if len(brief_bytes) > MAX_DOC_BYTES or len(req_bytes) > MAX_DOC_BYTES:
        return err(f"One of your documents is too large (max {MAX_DOC_BYTES // (1024*1024)} MB each).")
    if len(diagram_reads) > MAX_DIAGRAM_FILES:
        return err(f"Please upload at most {MAX_DIAGRAM_FILES} diagram files at a time.")
    if any(len(raw) > MAX_IMAGE_BYTES for _, raw in diagram_reads):
        return err(f"One of your diagram files is too large (max {MAX_IMAGE_BYTES // (1024*1024)} MB each).")
    if not _validate_upload(brief_file.filename, brief_bytes) or not _validate_upload(requirements_file.filename, req_bytes):
        return err("One of your documents doesn't look like a valid file of its type. Please re-export and try again.")
    if any(not _validate_upload(name, raw) for name, raw in diagram_reads):
        return err("One of your diagram files doesn't look like a valid image/PDF. Please re-export and try again.")

    try:
        brief_text = extract_text(brief_file.filename, brief_bytes)
        requirements_text = extract_text(requirements_file.filename, req_bytes)
    except Exception as e:
        ref = _log_and_ref(e, "extract_text failed in /analyze-custom")
        return err(GENERIC_ERROR_MESSAGE.format(ref=ref))

    if not requirements_text.strip():
        return err("No text could be extracted from the requirements document.")

    try:
        flow = parse_flow_diagrams(application, diagram_reads, api_key)
    except Exception as e:
        ref = _log_and_ref(e, "parse_flow_diagrams failed in /analyze-custom")
        return err(GENERIC_ERROR_MESSAGE.format(ref=ref), code=502)

    if not flow.get("steps"):
        return err(
            "No steps could be extracted from the uploaded diagram(s). Try a clearer export "
            "(e.g. a higher-resolution image or a native PDF export rather than a photo)."
        )

    # Stash everything the confirm step will need. Nothing here is generated
    # or reported yet - it only becomes real once a human confirms the flow.
    # (Trial codes never reach here - Custom Application Mode is rejected
    # for trial codes earlier in this function, see the trial-mode check
    # right after access validation.)
    run_id = uuid.uuid4().hex[:12]
    pending_dir = PENDING_DIR / run_id
    pending_dir.mkdir(parents=True, exist_ok=True)
    (pending_dir / "pending.json").write_text(json.dumps({
        "client_name": client_name,
        "application": application,
        "baseline_version": baseline_version.strip(),
        "element_hints": element_hints,
        "initials": initials,
        "brief_text": brief_text,
        "requirements_text": requirements_text,
        "requirements_source": requirements_file.filename,
        "flow": flow,
    }))

    return templates.TemplateResponse(request, "review_flow.html",
        {
            "run_id": run_id,
            "application": application,
            "flow": flow,
            "hide_trial_cta": True,
        },
    )


@app.post("/confirm-flow/{run_id}", response_class=HTMLResponse)
async def confirm_flow(request: Request, run_id: str):
    if not run_id.isalnum():
        raise HTTPException(status_code=400)

    pending_path = PENDING_DIR / run_id / "pending.json"
    if not pending_path.exists():
        raise HTTPException(status_code=404, detail="This review session was not found or has expired. Please start again.")

    pending = json.loads(pending_path.read_text())

    form = await request.form()
    step_count = int(form.get("step_count", "0") or 0)

    flow_name_value = form.get("flow_name", pending["flow"].get("flow_name", "Main flow")).strip()
    length_error = _check_field_length(flow_name_value, "flow_name")
    if length_error:
        raise HTTPException(status_code=400, detail=length_error)

    confirmed_steps = []
    for i in range(step_count):
        # A step can be removed on the review screen (its "keep" checkbox unchecked)
        if not form.get(f"keep_{i}"):
            continue
        step_label = form.get(f"step_id_{i}", f"FLOW-{i+1:03d}")
        screen_value = form.get(f"screen_{i}", "").strip()
        inputs_raw = form.get(f"inputs_{i}", "")
        decision_detail_value = form.get(f"decision_detail_{i}", "").strip()
        for field_name, field_value in (
            ("screen_or_stage", screen_value), ("inputs", inputs_raw), ("decision_detail", decision_detail_value),
        ):
            length_error = _check_field_length(field_value, field_name)
            if length_error:
                raise HTTPException(status_code=400, detail=f"Step {step_label}: {length_error}")
        confirmed_steps.append({
            "step_id": step_label,
            "screen_or_stage": screen_value,
            "description": form.get(f"description_{i}", "").strip(),
            "inputs": [s.strip() for s in inputs_raw.split(",") if s.strip()],
            "decision_point": bool(form.get(f"decision_point_{i}")),
            "decision_detail": decision_detail_value,
        })

    if not confirmed_steps:
        raise HTTPException(status_code=400, detail="At least one confirmed flow step is required to generate test coverage.")

    confirmed_flow = {
        "flow_name": flow_name_value,
        "parse_notes": pending["flow"].get("parse_notes", ""),
        "steps": confirmed_steps,
    }

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.error("ANTHROPIC_API_KEY is not configured.")
        raise HTTPException(status_code=500, detail="This service is not yet available. Please contact the operator.")

    rl = run_logger.RunLog.start("generate_b", run_id, {
        "application": pending["application"],
        "flow_step_count": len(confirmed_steps),
        "qa_model": os.environ.get("QA_MODEL"),
    })
    try:
        data = run_qa_analysis_custom(
            application=pending["application"],
            brief_text=pending["brief_text"],
            flow=confirmed_flow,
            requirements_text=pending["requirements_text"],
            api_key=api_key,
            element_hints=pending.get("element_hints", ""),
        )
    except Exception as e:
        ref = _log_and_ref(e, "run_qa_analysis_custom failed in /confirm-flow")
        rl.finish("fail", {"correlation_ref": ref, "error": str(e)})
        raise HTTPException(status_code=502, detail=GENERIC_ERROR_MESSAGE.format(ref=ref))

    data["requirements_source"] = pending["requirements_source"]
    data["run_date"] = _melbourne_now_str()
    data["baseline_version"] = pending.get("baseline_version", "")

    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    html_path = run_dir / "report.html"
    xlsx_path = run_dir / "data.xlsx"
    html_path.write_text(build_html_custom(data, confirmed_flow))
    build_xlsx_custom(data, confirmed_flow, str(xlsx_path))
    (run_dir / "flow.json").write_text(json.dumps(confirmed_flow))
    (run_dir / "data.json").write_text(json.dumps({**data, "application": pending["application"]}))

    with open(RUNS_DIR / "run_log.csv", "a") as f:
        f.write(f"{run_id},{pending['client_name']},{data['run_date']},{pending['application']},{pending['requirements_source']} (custom)\n")
    _append_history("report", run_id, pending["client_name"], pending.get("initials", ""), pending["application"], data["run_date"])

    shutil.rmtree(PENDING_DIR / run_id, ignore_errors=True)

    total_reqs = len(data.get("validation", []))
    flagged = sum(1 for v in data.get("validation", []) if not v.get("testable"))
    total_tcs = len(data.get("test_scenarios", []))
    uncovered_steps = len(data.get("uncovered_flow_steps", []))
    rl.finish("ok", {"test_case_count": total_tcs, "flagged_requirements": flagged, "uncovered_steps": uncovered_steps})

    return templates.TemplateResponse(request, "result.html",
        {
            "run_id": run_id,
            "download_token": _make_download_token(run_id),
            "client_name": pending["client_name"],
            "application": pending["application"],
            "mode": "custom",
            "total_reqs": total_reqs,
            "flagged": flagged,
            "total_tcs": total_tcs,
            "total_flow_steps": len(confirmed_steps),
            "uncovered_steps": uncovered_steps,
            "baseline_version": data["baseline_version"],
            "hide_trial_cta": True,
        },
    )


# --------------------------------------------------------------------------
# Bring-your-own test cases (import path) - for a client who doesn't want
# generation at all, just execution of test cases they already have. Same
# non-negotiable review-and-confirm checkpoint as Option B's diagram
# parsing: nothing extracted here is executable until a human confirms it.
# Bounded to the same file types the rest of the app already supports and
# has validated (.docx, .xlsx/.xlsm, .pdf, .txt, .csv) - never "any format."
# --------------------------------------------------------------------------
IMPORT_PENDING_DIR = RUNS_DIR / "pending_import"
IMPORT_PENDING_DIR.mkdir(exist_ok=True)
MAX_IMPORTED_CASES = 100  # sanity cap - a single document producing more than this is almost certainly a parsing problem, not a real test suite
MAX_TEST_CASE_FILES = 5  # caps the number of AI-structuring calls one import request can trigger


def _sweep_pending_imports():
    now = time.time()
    try:
        for entry in IMPORT_PENDING_DIR.iterdir():
            if not entry.is_dir():
                continue
            try:
                if now - entry.stat().st_mtime > PENDING_MAX_AGE_SECONDS:
                    shutil.rmtree(entry, ignore_errors=True)
            except OSError:
                continue
    except FileNotFoundError:
        pass


@app.get("/import-tests", response_class=HTMLResponse)
async def import_tests_form(request: Request):
    return templates.TemplateResponse(request, "import_tests.html", {"error": None, "canonical_url": _canonical_url("/import-tests")})


@app.post("/import-tests", response_class=HTMLResponse)
async def import_tests(
    request: Request,
    access_code: str = Form(...),
    application: str = Form(...),
    initials: str = Form(""),
    test_cases_files: List[UploadFile] = File(...),
    requirements_file: UploadFile | None = File(None),
    diagram_files: List[UploadFile] = File([]),
):
    _sweep_pending_imports()
    _sweep_completed_runs()

    def err(msg, code=400):
        return templates.TemplateResponse(request, "import_tests.html", {"error": msg}, status_code=code)

    try:
        client_name = _check_access_no_trial(request, access_code)
    except HTTPException as e:
        return err(e.detail, code=e.status_code)

    length_error = _check_field_length(application, "application")
    if length_error:
        return err(length_error)

    test_cases_files = [f for f in test_cases_files if f.filename]
    if not test_cases_files:
        return err("Please attach at least one test cases file.")
    if len(test_cases_files) > MAX_TEST_CASE_FILES:
        return err(f"Please attach at most {MAX_TEST_CASE_FILES} test case files at a time.")

    reads = []
    for f in test_cases_files:
        raw = await f.read()
        if not raw:
            return err(f"'{f.filename}' is empty.")
        if len(raw) > MAX_DOC_BYTES:
            return err(f"'{f.filename}' is too large (max {MAX_DOC_BYTES // (1024*1024)} MB).")
        if not _validate_upload(f.filename, raw):
            return err(f"'{f.filename}' doesn't look like a valid document of its type. Please re-export and try again.")
        reads.append((f.filename, raw))

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.error("ANTHROPIC_API_KEY is not configured.")
        return err("This service is not yet available. Please contact the operator.", code=500)

    run_id = uuid.uuid4().hex[:12]
    rl = run_logger.RunLog.start("import", run_id, {
        "application": application,
        "filenames": [name for name, _ in reads],
        "file_count": len(reads),
    })

    # Parse each test-case file independently (deterministic tabular path
    # first, per file - a client mixing one clean spreadsheet with one prose
    # doc shouldn't have the whole batch fall back to AI structuring just
    # because one file needed it), then merge. tc_id collisions across files
    # are re-numbered so nothing silently overwrites another file's case.
    parsed_cases = []
    parser_paths_used = set()
    parse_notes_parts = []
    for filename, raw in reads:
        file_cases = try_parse_tabular(filename, raw)
        if file_cases is not None:
            parser_paths_used.add("deterministic_tabular")
        else:
            parser_paths_used.add("ai_structuring_fallback")
            try:
                raw_text = extract_text(filename, raw)
            except Exception as e:
                ref = _log_and_ref(e, "extract_text failed in /import-tests")
                rl.finish("fail", {"correlation_ref": ref, "error": str(e), "filename": filename})
                return err(GENERIC_ERROR_MESSAGE.format(ref=ref))
            if not raw_text.strip():
                continue
            try:
                structured = structure_existing_test_cases(application, raw_text, api_key)
            except Exception as e:
                ref = _log_and_ref(e, "structure_existing_test_cases failed in /import-tests")
                rl.finish("fail", {"correlation_ref": ref, "error": str(e), "filename": filename})
                return err(GENERIC_ERROR_MESSAGE.format(ref=ref), code=502)
            file_cases = structured.get("test_cases", [])
            if structured.get("parse_notes"):
                parse_notes_parts.append(f"{filename}: {structured['parse_notes']}")
        parsed_cases.extend(file_cases or [])

    # Re-number tc_ids sequentially across the merged set so files with
    # overlapping/default IDs (e.g. every file using "TC-001") don't collide.
    for i, tc in enumerate(parsed_cases):
        tc["tc_id"] = f"TC-{i+1:03d}"
    parser_path = "+".join(sorted(parser_paths_used)) or "none"
    parse_notes = " | ".join(parse_notes_parts)

    if not parsed_cases:
        rl.finish("fail", {"error": "no test cases identified", "parser_path": parser_path})
        return err(
            "No test cases could be identified in those file(s). "
            + (parse_notes or "Try a file with clearer per-case structure (a table, or one case per section).")
        )
    if len(parsed_cases) > MAX_IMPORTED_CASES:
        rl.finish("fail", {"error": "too many test cases", "count": len(parsed_cases), "parser_path": parser_path})
        return err(f"Those file(s) appear to contain more than {MAX_IMPORTED_CASES} test cases combined - please split into smaller batches.")

    # Optional requirements doc -> best-effort traceability matrix. Optional
    # diagram(s) -> reuses the same custom-app diagram parser (see
    # diagram_parser.py) to fold flow-doc content into the same traceability
    # pass for a custom-built application, rather than duplicating a second
    # vision-parsing path. Both are additive and never block the import if
    # they fail - traceability is a bonus, not a requirement to see your
    # imported test cases.
    traceability = None
    traceability_error = None
    requirements_text = ""
    if requirements_file is not None and requirements_file.filename:
        req_raw = await requirements_file.read()
        if req_raw and len(req_raw) > MAX_DOC_BYTES:
            traceability_error = f"Your requirements file is too large (max {MAX_DOC_BYTES // (1024*1024)} MB) - traceability was skipped, but your test cases were still processed below."
        elif req_raw:
            if not _validate_upload(requirements_file.filename, req_raw):
                traceability_error = "The requirements file didn't look like a valid document - traceability was skipped, but your test cases were still processed below."
            else:
                try:
                    requirements_text = extract_text(requirements_file.filename, req_raw)
                except Exception:
                    traceability_error = "Your requirements file couldn't be read - traceability was skipped, but your test cases were still processed below."

    diagram_notes = ""
    diagram_files = [f for f in diagram_files if f.filename]
    if len(diagram_files) > MAX_DIAGRAM_FILES:
        diagram_files = diagram_files[:MAX_DIAGRAM_FILES]
        traceability_error = (traceability_error or "") + f" Only the first {MAX_DIAGRAM_FILES} diagram files were used."
    if diagram_files and requirements_text.strip():
        diagram_reads = [(f.filename, await f.read()) for f in diagram_files]
        diagram_reads = [(n, b) for n, b in diagram_reads if b and len(b) <= MAX_IMAGE_BYTES]
        if diagram_reads:
            try:
                flow = parse_flow_diagrams(application, diagram_reads, api_key)
                steps_desc = "; ".join(
                    f"{s.get('step_id','')}: {s.get('description','')}" for s in flow.get("steps", [])
                )
                if steps_desc:
                    diagram_notes = f"\n\nAdditional process flow read from attached diagram(s):\n{steps_desc}"
            except Exception:
                traceability_error = (traceability_error or "") + " The attached diagram(s) couldn't be read and were skipped for traceability."

    if requirements_text.strip():
        try:
            traceability = match_requirements_to_test_cases(
                application, requirements_text + diagram_notes, parsed_cases, api_key
            )
        except Exception:
            traceability_error = (traceability_error or "") + " The traceability check couldn't be completed - your test cases were still processed below."

    rl.finish("ok", {"parser_path": parser_path, "test_case_count": len(parsed_cases), "traceability_attempted": requirements_text.strip() != ""})

    pending_dir = IMPORT_PENDING_DIR / run_id
    pending_dir.mkdir(parents=True, exist_ok=True)
    (pending_dir / "pending.json").write_text(json.dumps({
        "client_name": client_name,
        "application": application,
        "initials": initials,
        "source_filename": ", ".join(name for name, _ in reads),
        "parse_notes": parse_notes,
        "test_cases": parsed_cases,
        "traceability": traceability,
    }))

    return templates.TemplateResponse(request, "review_import.html",
        {
            "run_id": run_id,
            "application": application,
            "parse_notes": parse_notes,
            "test_cases": parsed_cases,
            "traceability": traceability,
            "traceability_error": traceability_error,
            "hide_trial_cta": True,
        },
    )


@app.post("/confirm-import/{run_id}", response_class=HTMLResponse)
async def confirm_import(request: Request, run_id: str):
    if not run_id.isalnum():
        raise HTTPException(status_code=400)

    pending_path = IMPORT_PENDING_DIR / run_id / "pending.json"
    if not pending_path.exists():
        raise HTTPException(status_code=404, detail="This review session was not found or has expired. Please start again.")
    pending = json.loads(pending_path.read_text())

    form = await request.form()
    case_count = int(form.get("case_count", "0") or 0)

    confirmed_cases = []
    for i in range(case_count):
        if not form.get(f"keep_{i}"):
            continue
        tc_id_value = form.get(f"tc_id_{i}", f"TC-{i+1:03d}").strip()
        title_value = form.get(f"title_{i}", "").strip()
        for field_name, field_value in (("tc_id", tc_id_value), ("title", title_value)):
            length_error = _check_field_length(field_value, field_name)
            if length_error:
                raise HTTPException(status_code=400, detail=f"Case {tc_id_value or i+1}: {length_error}")
        confirmed_cases.append({
            "tc_id": tc_id_value,
            "req_id": "",
            "title": title_value,
            "precondition": form.get(f"precondition_{i}", "").strip(),
            "steps": form.get(f"steps_{i}", "").strip(),
            "expected_result": form.get(f"expected_result_{i}", "").strip(),
        })

    if not confirmed_cases:
        raise HTTPException(status_code=400, detail="At least one confirmed test case is required.")

    data = {
        "application": pending["application"],
        "requirements_source": pending["source_filename"],
        "run_date": _melbourne_now_str(),
        "baseline_version": "",
        "validation": [],  # no requirements-validation step in this path - imported as-is
        "test_scenarios": confirmed_cases,
    }

    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "data.json").write_text(json.dumps(data))

    with open(RUNS_DIR / "run_log.csv", "a") as f:
        f.write(f"{run_id},{pending['client_name']},{data['run_date']},{pending['application']},{pending['source_filename']} (imported)\n")
    # Imported test cases have no report.html/data.xlsx of their own at this
    # point (that only exists after a live execution) - kept as its own
    # "imported" kind so Run History links to /execute rather than a
    # download that doesn't exist yet.
    _append_history("imported", run_id, pending["client_name"], pending.get("initials", ""), pending["application"], data["run_date"])

    shutil.rmtree(IMPORT_PENDING_DIR / run_id, ignore_errors=True)

    return templates.TemplateResponse(request, "import_result.html",
        {"run_id": run_id, "application": pending["application"], "total_tcs": len(confirmed_cases), "hide_trial_cta": True},
    )


# --------------------------------------------------------------------------
# Live Execution (Phase 2) - generic engine, not app-specific. Runs a
# selection of an already-generated run's test cases live against a
# client-supplied sandbox/UAT environment. See execute_engine.py for the
# credential-handling and SSRF-guard rationale.
# --------------------------------------------------------------------------
EXECUTIONS_DIRNAME = "executions"
FIXTURES_DIR = RUNS_DIR / "fixtures"


@app.get("/execute/{run_id}", response_class=HTMLResponse)
async def execute_select(request: Request, run_id: str):
    if not run_id.isalnum():
        raise HTTPException(status_code=400)
    data_path = RUNS_DIR / run_id / "data.json"
    if not data_path.exists():
        raise HTTPException(status_code=404, detail="This run was not found or has expired. Please run the analysis again.")
    try:
        data = json.loads(data_path.read_text())
    except Exception as e:
        ref = _log_and_ref(e, "failed to load data.json in /execute (GET)")
        raise HTTPException(status_code=500, detail=GENERIC_ERROR_MESSAGE.format(ref=ref))

    if data.get("status") == "awaiting_curation":
        raise HTTPException(status_code=409, detail="This run is still awaiting your review - please confirm which test cases to keep before running execution.")

    test_cases = data.get("test_scenarios", [])
    return templates.TemplateResponse(request, "execute_select.html",
        {
            "run_id": run_id,
            "application": data.get("application", ""),
            "test_cases": test_cases,
            "max_selectable": MAX_TEST_CASES_PER_EXECUTION,
            "max_screenshots_default": DEFAULT_MAX_SCREENSHOTS_PER_TEST,
            "max_screenshots_ceiling": MAX_SCREENSHOTS_PER_TEST_CEILING,
            "error": None,
            "hide_trial_cta": True,
        },
    )


@app.post("/execute/{run_id}", response_class=HTMLResponse)
async def execute_run(
    request: Request,
    background_tasks: BackgroundTasks,
    run_id: str,
    access_code: str = Form(...),
    role_label: str = Form(...),
    module: str = Form(...),
    env_url: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    initials: str = Form(""),
    max_screenshots: str = Form(str(DEFAULT_MAX_SCREENSHOTS_PER_TEST)),
):
    if not run_id.isalnum():
        raise HTTPException(status_code=400)
    data_path = RUNS_DIR / run_id / "data.json"
    if not data_path.exists():
        raise HTTPException(status_code=404, detail="This run was not found or has expired. Please run the analysis again.")

    def err(msg, code=400, test_cases=None, application=""):
        return templates.TemplateResponse(request, "execute_select.html",
            {
                "run_id": run_id, "application": application,
                "test_cases": test_cases or [], "max_selectable": MAX_TEST_CASES_PER_EXECUTION, "error": msg,
                "hide_trial_cta": True,
                "max_screenshots_default": DEFAULT_MAX_SCREENSHOTS_PER_TEST,
                "max_screenshots_ceiling": MAX_SCREENSHOTS_PER_TEST_CEILING,
            },
            status_code=code,
        )

    try:
        data = json.loads(data_path.read_text())
    except Exception as e:
        ref = _log_and_ref(e, "failed to load data.json in /execute (POST)")
        raise HTTPException(status_code=500, detail=GENERIC_ERROR_MESSAGE.format(ref=ref))

    if data.get("status") == "awaiting_curation":
        raise HTTPException(status_code=409, detail="This run is still awaiting your review - please confirm which test cases to keep before running execution.")

    application = data.get("application", "")
    all_test_cases = {tc.get("tc_id"): tc for tc in data.get("test_scenarios", [])}

    for field_name, field_value in (
        ("role_label", role_label), ("module", module), ("env_url", env_url),
        ("username", username), ("password", password),
    ):
        length_error = _check_field_length(field_value, field_name)
        if length_error:
            return err(length_error, test_cases=data.get("test_scenarios"), application=application)

    # Screenshot-count field: defaults to DEFAULT_MAX_SCREENSHOTS_PER_TEST
    # unless the client explicitly changes it; any value above
    # MAX_SCREENSHOTS_PER_TEST_CEILING is rejected with a clear message
    # rather than silently clamped, so the client knows why their number
    # wasn't honored. This only controls how many of the already-captured
    # screenshots are SHOWN in the report (see execution_report.py) - it
    # does not change what execute_engine.py captures internally.
    try:
        max_screenshots_val = int(str(max_screenshots).strip())
    except (TypeError, ValueError):
        return err(
            f"Screenshots per test case must be a whole number (default {DEFAULT_MAX_SCREENSHOTS_PER_TEST}).",
            test_cases=data.get("test_scenarios"), application=application,
        )
    if max_screenshots_val > MAX_SCREENSHOTS_PER_TEST_CEILING:
        return err(
            f"Screenshots per test case can't be more than {MAX_SCREENSHOTS_PER_TEST_CEILING}. "
            f"Please select a value less than {MAX_SCREENSHOTS_PER_TEST_CEILING}.",
            test_cases=data.get("test_scenarios"), application=application,
        )
    if max_screenshots_val < 1:
        return err(
            "Screenshots per test case must be at least 1.",
            test_cases=data.get("test_scenarios"), application=application,
        )

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.error("ANTHROPIC_API_KEY is not configured.")
        return err("This service is not yet available. Please contact the operator.", code=500, test_cases=data.get("test_scenarios"), application=application)

    try:
        client_name = _check_access_no_trial(request, access_code)
    except HTTPException as e:
        return err(e.detail, code=e.status_code, test_cases=data.get("test_scenarios"), application=application)

    try:
        _check_rate_limit(_execution_calls, access_code, EXECUTION_RATE_MAX, EXECUTION_RATE_WINDOW_SECONDS)
    except HTTPException as e:
        return err(e.detail, code=e.status_code, test_cases=data.get("test_scenarios"), application=application)

    form = await request.form()
    selected_ids = [tc_id for tc_id in all_test_cases if form.get(f"select_{tc_id}")]
    if not selected_ids:
        return err("Select at least one test case to run.", test_cases=data.get("test_scenarios"), application=application)
    if len(selected_ids) > MAX_TEST_CASES_PER_EXECUTION:
        return err(f"Please select at most {MAX_TEST_CASES_PER_EXECUTION} test cases per run.", test_cases=data.get("test_scenarios"), application=application)

    # Execution allowance check (2026-09-16 council verdict - see
    # client_quotas.set_execution_allowance/check_can_execute): the disclosed
    # execution limit for this engagement is exactly what was kept from
    # generation, enforced with no hidden buffer. A no-op for access codes
    # with no quota configured (unrestricted legacy clients).
    exec_allowed, exec_block_reason = client_quotas.check_can_execute(access_code, len(selected_ids))
    if not exec_allowed:
        return err(exec_block_reason, test_cases=data.get("test_scenarios"), application=application)

    if not _validate_env_url(env_url):
        return err(
            "That environment URL doesn't look reachable, or it isn't a public sandbox/UAT address. "
            "Double-check the URL your test environment gives you.",
            test_cases=data.get("test_scenarios"), application=application,
        )

    exec_id = uuid.uuid4().hex[:12]
    exec_dir = RUNS_DIR / run_id / EXECUTIONS_DIRNAME / exec_id
    exec_dir.mkdir(parents=True, exist_ok=True)

    # Phase C - test data fixture reuse (see app/fixtures.py and
    # claude/phase-c-test-data-fixture-reuse-design.md for the full design
    # and council review this implements).
    env_host = urlparse(env_url).hostname or "unknown"
    # Checkbox is checked by default in the form; its absence from the
    # submitted form means the client explicitly unchecked it.
    reuse_fixtures = form.get("reuse_fixtures") is not None

    # Schedule "creates:<type>" scenarios before "requires:<type>" scenarios
    # within this run (stable sort preserves original order otherwise), so a
    # client who selects a create-then-use pair in one run doesn't get
    # blocked just because of generation order.
    def _schedule_priority(tc_id):
        role = fixtures.parse_fixture_role(all_test_cases[tc_id].get("fixture_role", ""))
        return 0 if (role and role[0] == "creates") else 1
    scheduled_ids = sorted(selected_ids, key=_schedule_priority)

    # Phase B - run the batch in the background and hand the client an
    # immediate redirect to a live status page, instead of holding one HTTP
    # request open for the whole batch (which risked platform request
    # timeouts once the cap was raised from 5 to 25). Cases still run
    # sequentially, one Chromium at a time - same execution model as
    # before, just no longer blocking the request/response cycle.
    exec_token = _make_download_token(f"{run_id}/{exec_id}")
    exec_status.write_status(exec_dir, {
        "state": "running",
        "total": len(selected_ids),
        "completed": 0,
        "cases": [
            {"tc_id": t, "title": all_test_cases[t].get("title", ""), "status": "NOT_STARTED"}
            for t in selected_ids
        ],
    })
    background_tasks.add_task(
        _run_execution_batch,
        run_id=run_id, exec_id=exec_id, exec_dir=exec_dir, exec_token=exec_token,
        application=application, client_name=client_name, role_label=role_label,
        module=module, env_url=env_url, env_host=env_host, username=username,
        password=password, api_key=api_key, initials=initials,
        all_test_cases=all_test_cases, selected_ids=selected_ids,
        scheduled_ids=scheduled_ids, reuse_fixtures=reuse_fixtures,
        client_fixtures=data.get("client_test_data", []),
        access_code=access_code, max_screenshots=max_screenshots_val,
    )
    return RedirectResponse(
        url=f"/execute-status/{run_id}/{exec_id}?token={exec_token}",
        status_code=303,
    )


async def _run_execution_batch(*args, **kwargs):
    """Thin wrapper so a bug (or a crash) in the batch itself still flips
    the status page out of "running" instead of leaving a client staring
    at a progress bar that never moves and never explains why."""
    exec_dir = kwargs.get("exec_dir")
    try:
        await _run_execution_batch_impl(*args, **kwargs)
    except Exception as e:
        ref = _log_and_ref(e, "background execution batch crashed")
        if exec_dir is not None:
            exec_status.write_status(exec_dir, {
                "state": "error",
                "error": GENERIC_ERROR_MESSAGE.format(ref=ref),
            })


async def _run_execution_batch_impl(
    run_id, exec_id, exec_dir, exec_token, application, client_name, role_label,
    module, env_url, env_host, username, password, api_key, initials,
    all_test_cases, selected_ids, scheduled_ids, reuse_fixtures,
    access_code=None, max_screenshots=DEFAULT_MAX_SCREENSHOTS_PER_TEST,
    client_fixtures=None,
):
    """Runs one batch of live-execution test cases (moved out of the
    request/response cycle - see the Phase B redirect in execute_run above).
    Writes an incrementally-updated status.json throughout so
    /execute-status/{run_id}/{exec_id} can show live progress, then
    writes the final report.html and status exactly as the old synchronous
    flow did."""
    # Fixtures created earlier in THIS run - always usable by a later
    # "requires:" scenario in the same run regardless of the reuse toggle,
    # so selecting "add employee" + "update employee code" together always
    # works out of the box. Keyed by fixture type -> fixture record.
    run_fixtures = {}
    results_by_tc = {}
    total = len(selected_ids)

    def _push_status(current_tc_id=None, current_step=None, current_max_steps=None):
        # Reported in the client's original selection order (not the
        # internal creates-before-requires scheduling order) with every
        # selected case always present, so the client sees the full list
        # up front - not started, in progress (with a step count while
        # it's the one running), or its final PASS/FAIL/BLOCKED verdict.
        cases = []
        for t in selected_ids:
            if t in results_by_tc:
                cases.append({
                    "tc_id": t, "title": results_by_tc[t]["title"],
                    "status": results_by_tc[t]["verdict"],
                })
            elif t == current_tc_id:
                cases.append({
                    "tc_id": t, "title": all_test_cases[t].get("title", ""),
                    "status": "IN_PROGRESS",
                    "step": current_step, "max_steps": current_max_steps,
                })
            else:
                cases.append({"tc_id": t, "title": all_test_cases[t].get("title", ""), "status": "NOT_STARTED"})
        exec_status.write_status(exec_dir, {
            "state": "running",
            "total": total,
            "completed": len(results_by_tc),
            "cases": cases,
        })

    for tc_id in scheduled_ids:
        tc = all_test_cases[tc_id]
        role = fixtures.parse_fixture_role(tc.get("fixture_role", ""))
        shots_dir = exec_dir / f"shots_{tc_id}".replace("/", "_")
        _push_status(current_tc_id=tc_id)

        working_tc = dict(tc)
        fixture_note = ""
        skip_execution = False

        # Every scenario gets a real unique token substituted for any literal
        # {{UNIQUE}} placeholder in its steps/precondition - independent of
        # fixture_role, since most "creates new data" scenarios use this to
        # avoid colliding with data left behind by earlier runs against the
        # same shared environment (see the independence/data-safety prompt
        # instruction in qa_engine.py).
        # Kept short (6 chars) deliberately - a scenario's own literal text
        # around {{UNIQUE}} (e.g. "EMP_{{UNIQUE}}") often goes into a
        # short-format field (an ID/code column), and a longer token was
        # observed overflowing such a field's own length limit, causing the
        # very validation failure this mechanism exists to prevent.
        tc_digits = re.sub(r"\D", "", tc_id) or "0"
        unique_token = f"{exec_id[:3]}{tc_digits[-3:].zfill(3)}"
        working_tc["steps"] = fixtures.substitute_unique_token(working_tc.get("steps", ""), unique_token)
        working_tc["precondition"] = fixtures.substitute_unique_token(working_tc.get("precondition", ""), unique_token)

        if role and role[0] == "requires":
            ftype = role[1]
            fixture = run_fixtures.get(ftype)
            client_supplied = False
            if fixture is None:
                # Client-supplied data (attached to this run's requirements)
                # satisfies a "requires:" scenario regardless of the reuse
                # toggle - that toggle only ever governed *cross-run* reuse
                # of records the automation itself created; a client
                # explicitly supplying a record for this run is a different,
                # always-on source. See app/fixtures.py and
                # claude/council-review-client-supplied-test-data-devils-advocate.md.
                fixture = fixtures.lookup_client_fixture(client_fixtures, ftype)
                client_supplied = fixture is not None
            if fixture is None and reuse_fixtures:
                fixture = fixtures.get_fresh_fixture(FIXTURES_DIR, application, env_host, ftype)
            if fixture is None:
                skip_execution = True
                if not reuse_fixtures:
                    block_msg = (
                        f"This test case needs an existing {ftype} record, but reuse of existing test "
                        f"data is turned off and no {ftype} was created earlier in this run or supplied "
                        f"with the requirements. Include a test case that creates a {ftype} in this run, "
                        f"provide one when submitting requirements, or turn reuse back on."
                    )
                else:
                    block_msg = (
                        f"This test case needs an existing {ftype} record, and none is available - not "
                        f"created earlier in this run, not supplied with the requirements, and none on "
                        f"file yet for this environment. Run a test case that creates a {ftype} first, "
                        f"select both together in the same run, or provide one when submitting "
                        f"requirements (for a state the automation can't create itself, such as a "
                        f"resigned/offboarded employee)."
                    )
                results_by_tc[tc_id] = {
                    "tc_id": tc_id, "title": tc.get("title", ""), "verdict": "BLOCKED",
                    "notes": block_msg, "step_log": [], "screenshots": [],
                }
            else:
                working_tc["steps"] = fixtures.substitute_fixture_placeholders(working_tc.get("steps", ""), ftype, fixture)
                working_tc["precondition"] = fixtures.substitute_fixture_placeholders(working_tc.get("precondition", ""), ftype, fixture)
                if client_supplied:
                    fixture_note = f" (Ran against the {ftype} test record you supplied with the requirements.)"
                else:
                    created_by = fixture.get("created_by", {})
                    when_label = "earlier in this run" if created_by.get("exec_id") == exec_id else "by a previous run"
                    creator_tc = created_by.get("tc_id", "")
                    by_label = f" by {creator_tc}" if creator_tc else ""
                    fixture_note = f" (Ran against an existing {ftype} test record created {when_label}{by_label}.)"

        if skip_execution:
            continue

        # One log per test-case execution - env URL host only, never full
        # URL/credentials; expected_result recorded alongside the run so a
        # later dispute can be judged against what was actually expected,
        # not just what happened.
        rl = run_logger.RunLog.start("execute", f"{run_id}_{exec_id}_{tc_id}", {
            "application": application,
            "role_label": role_label,
            "module": module,
            "tc_id": tc_id,
            "title": tc.get("title", ""),
            "expected_result": tc.get("expected_result", ""),
            "env_url_host": env_host,
            "qa_model": os.environ.get("QA_MODEL"),
        })
        try:
            # execute_test_case uses Playwright's *sync* API internally
            # (sync_playwright()), which cannot run on a thread that has an
            # active asyncio event loop - this route handler is `async def`,
            # so calling it directly here raises "Playwright Sync API inside
            # the asyncio loop". Running it in a plain worker thread (no
            # event loop of its own) via asyncio.to_thread avoids that.
            outcome = await asyncio.to_thread(
                execute_test_case,
                application=application,
                role_label=role_label,
                module=module,
                test_case=working_tc,
                env_url=env_url,
                username=username,
                password=password,
                api_key=api_key,
                fixture_role=tc.get("fixture_role", ""),
                shots_dir=shots_dir,
                rl=rl,
                on_step=lambda step, max_steps, _tc_id=tc_id: _push_status(
                    current_tc_id=_tc_id, current_step=step, current_max_steps=max_steps,
                ),
            )
            notes = outcome["notes"] + fixture_note
            results_by_tc[tc_id] = {
                "tc_id": tc_id,
                "title": tc.get("title", ""),
                "verdict": outcome["verdict"],
                "notes": notes,
                "step_log": outcome["step_log"],
                "screenshots": [f"{shots_dir.name}/{fn}" for fn in outcome["screenshots"]],
            }
            rl.finish(outcome["verdict"].lower(), {"notes": notes, "step_count": len(outcome["step_log"])})

            # This scenario's job was to create a reusable record, and it
            # passed with the agent reporting what it created - save it for
            # this run's own later scenarios (always) and for future runs
            # against this environment (only when reuse is enabled - a
            # client who turned reuse off for this run still shouldn't have
            # it silently repopulate the cross-run registry for next time...
            # except that "next time" is a different run's own decision, so
            # persisting is harmless and arguably still useful; only
            # *consumption* is gated by the toggle, not creation).
            if role and role[0] == "creates" and outcome["verdict"] == "PASS" and outcome.get("created_entity"):
                saved = fixtures.save_fixture(
                    FIXTURES_DIR, application, env_host, role[1],
                    outcome["created_entity"],
                    {"run_id": run_id, "exec_id": exec_id, "tc_id": tc_id},
                )
                run_fixtures[role[1]] = saved
        except ExecutionError as e:
            ref = _log_and_ref(e, f"execute_test_case ExecutionError for {tc_id} in /execute")
            rl.finish("blocked", {"correlation_ref": ref, "error": str(e)})
            results_by_tc[tc_id] = {
                "tc_id": tc_id, "title": tc.get("title", ""), "verdict": "BLOCKED",
                "notes": f"Could not complete this test case: {e}", "step_log": [], "screenshots": [],
            }
        except Exception as e:
            ref = _log_and_ref(e, f"execute_test_case failed for {tc_id} in /execute")
            rl.finish("blocked", {"correlation_ref": ref, "error": type(e).__name__})
            results_by_tc[tc_id] = {
                "tc_id": tc_id, "title": tc.get("title", ""), "verdict": "BLOCKED",
                "notes": GENERIC_ERROR_MESSAGE.format(ref=ref), "step_log": [], "screenshots": [],
            }
        _push_status()
    # Credentials go out of scope here and are never referenced again in this
    # function - nothing below this point has access to `username`/`password`.

    # Report results in the client's original selection order, not the
    # creates-before-requires scheduling order used internally.
    results = [results_by_tc[tc_id] for tc_id in selected_ids if tc_id in results_by_tc]

    env_label = urlparse(env_url).hostname or "environment"
    run_date = _melbourne_now_str()
    report_data = {
        "application": application, "client_name": client_name, "run_date": run_date,
        "environment_label": env_label, "role_label": role_label, "results": results,
        "token": exec_token, "run_id": run_id, "exec_id": exec_id,
        "max_screenshots": max_screenshots,
    }
    (exec_dir / "report.html").write_text(build_execution_report(report_data))

    # Execution audit log - who ran what against which (sandbox) host, and
    # the outcome. Never the credentials, never the full URL (which could
    # contain a token/query string) - host label only.
    with open(RUNS_DIR / "execution_log.csv", "a") as f:
        verdicts = ";".join(f"{r['tc_id']}={r['verdict']}" for r in results)
        f.write(f"{run_id},{exec_id},{client_name},{run_date},{application},{role_label},{module},{env_label},{verdicts}\n")
    _append_history("execute", run_id, client_name, initials, application, run_date, exec_id=exec_id)

    # Execution allowance consumption (2026-09-16 council verdict) - a no-op
    # if this access code has no quota configured. Recorded here (batch
    # completion), not at selection time, so a batch that crashes partway
    # doesn't consume allowance for cases that were never actually run.
    if access_code:
        client_quotas.record_executed(access_code, len(results))

    summary_counts = {
        "total": len(results),
        "passed": sum(1 for r in results if r["verdict"] == "PASS"),
        "failed": sum(1 for r in results if r["verdict"] == "FAIL"),
        "blocked": sum(1 for r in results if r["verdict"] == "BLOCKED"),
    }

    # Final status - the status page's poller sees state == "done" and the
    # browser is sent on to the same execute-result view the old synchronous
    # flow returned directly.
    exec_status.write_status(exec_dir, {
        "state": "done",
        "total": summary_counts["total"],
        "completed": summary_counts["total"],
        "cases": [
            {"tc_id": r["tc_id"], "title": r["title"], "status": r["verdict"]} for r in results
        ],
        "result_page": {
            "run_id": run_id, "exec_id": exec_id, "application": application,
            "role_label": role_label, "module": module, "environment_label": env_label,
            "download_token": exec_token, "hide_trial_cta": True, **summary_counts,
        },
    })


@app.get("/download-exec/{run_id}/{exec_id}/{subpath:path}")
async def download_exec(run_id: str, exec_id: str, subpath: str, token: str = ""):
    if not run_id.isalnum() or not exec_id.isalnum():
        raise HTTPException(status_code=400)
    if not _verify_download_token(f"{run_id}/{exec_id}", token):
        raise HTTPException(status_code=403, detail="This link is invalid or has expired. Please re-run the execution to get a fresh link.")

    exec_dir = (RUNS_DIR / run_id / EXECUTIONS_DIRNAME / exec_id).resolve()
    requested = (exec_dir / subpath).resolve()
    # Guard against path traversal escaping exec_dir via a crafted subpath.
    if exec_dir not in requested.parents and requested != exec_dir:
        raise HTTPException(status_code=400)
    if not str(requested).startswith(str(exec_dir) + os.sep):
        raise HTTPException(status_code=400)
    if not requested.exists() or not requested.is_file():
        raise HTTPException(status_code=404, detail="File not found or expired.")

    media = "text/html" if requested.suffix == ".html" else ("image/png" if requested.suffix == ".png" else "application/octet-stream")
    return FileResponse(requested, media_type=media)


@app.get("/execute-status/{run_id}/{exec_id}", response_class=HTMLResponse)
async def execute_status_page(request: Request, run_id: str, exec_id: str, token: str = ""):
    if not run_id.isalnum() or not exec_id.isalnum():
        raise HTTPException(status_code=400)
    if not _verify_download_token(f"{run_id}/{exec_id}", token):
        raise HTTPException(status_code=403, detail="This link is invalid or has expired. Please re-run the execution to get a fresh link.")

    exec_dir = RUNS_DIR / run_id / EXECUTIONS_DIRNAME / exec_id
    status = exec_status.read_status(exec_dir)
    if status is None:
        raise HTTPException(status_code=404, detail="This execution was not found or has expired.")

    if status.get("state") == "done":
        return templates.TemplateResponse(request, "execute_result.html", status["result_page"])

    if status.get("state") == "error":
        raise HTTPException(status_code=500, detail=status.get("error", GENERIC_ERROR_MESSAGE.format(ref="n/a")))

    return templates.TemplateResponse(request, "execute_status.html",
        {
            "run_id": run_id, "exec_id": exec_id, "token": token,
            "total": status.get("total", 0), "completed": status.get("completed", 0),
            "hide_trial_cta": True,
        },
    )


@app.get("/execute-status-json/{run_id}/{exec_id}")
async def execute_status_json(run_id: str, exec_id: str, token: str = ""):
    if not run_id.isalnum() or not exec_id.isalnum():
        raise HTTPException(status_code=400)
    if not _verify_download_token(f"{run_id}/{exec_id}", token):
        raise HTTPException(status_code=403)

    exec_dir = RUNS_DIR / run_id / EXECUTIONS_DIRNAME / exec_id
    status = exec_status.read_status(exec_dir)
    if status is None:
        raise HTTPException(status_code=404)
    # Never echo credentials or the report/result payload in the poll
    # response - only what the progress UI needs.
    return {
        "state": status.get("state", "running"),
        "total": status.get("total", 0),
        "completed": status.get("completed", 0),
        "cases": status.get("cases", []),
    }


def _zip_directory(root_dir: Path, base_name_in_zip: str = "") -> io.BytesIO:
    """Zips every file under root_dir (recursively) into an in-memory buffer.
    Paths inside the zip are relative to root_dir, optionally nested under
    base_name_in_zip - keeps the same folder layout the report.html itself
    already relies on (shots_<tc_id>/*.png alongside report.html), so a
    downloaded run bundle still opens correctly offline."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(root_dir.rglob("*")):
            if path.is_file():
                arcname = path.relative_to(root_dir)
                if base_name_in_zip:
                    arcname = Path(base_name_in_zip) / arcname
                zf.write(path, arcname=str(arcname))
    buf.seek(0)
    return buf


@app.get("/download-exec-zip/{run_id}/{exec_id}/{tc_id}")
async def download_exec_case_zip(run_id: str, exec_id: str, tc_id: str, token: str = ""):
    """All screenshots for one test case in a single .zip - same signed-token
    gate as /download-exec, no new unguarded access path."""
    if not run_id.isalnum() or not exec_id.isalnum():
        raise HTTPException(status_code=400)
    if not _verify_download_token(f"{run_id}/{exec_id}", token):
        raise HTTPException(status_code=403, detail="This link is invalid or has expired. Please re-run the execution to get a fresh link.")

    exec_dir = (RUNS_DIR / run_id / EXECUTIONS_DIRNAME / exec_id).resolve()
    shots_dir_name = f"shots_{tc_id}".replace("/", "_")
    shots_dir = (exec_dir / shots_dir_name).resolve()
    if exec_dir not in shots_dir.parents or not shots_dir.is_dir():
        raise HTTPException(status_code=404, detail="No screenshots found for this test case, or the link has expired.")

    buf = _zip_directory(shots_dir)
    fname = f"{tc_id}_screenshots.zip"
    return StreamingResponse(
        buf, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.get("/download-exec-zip/{run_id}/{exec_id}")
async def download_exec_run_zip(run_id: str, exec_id: str, token: str = ""):
    """Everything for this execution (report.html + every test case's
    screenshots) in one .zip, preserving the same relative folder layout
    report.html already uses - so report.html inside the zip renders
    correctly even opened straight from disk, offline."""
    if not run_id.isalnum() or not exec_id.isalnum():
        raise HTTPException(status_code=400)
    if not _verify_download_token(f"{run_id}/{exec_id}", token):
        raise HTTPException(status_code=403, detail="This link is invalid or has expired. Please re-run the execution to get a fresh link.")

    exec_dir = (RUNS_DIR / run_id / EXECUTIONS_DIRNAME / exec_id).resolve()
    if not exec_dir.is_dir():
        raise HTTPException(status_code=404, detail="This execution was not found, or the link has expired.")

    buf = _zip_directory(exec_dir)
    fname = f"{run_id}_{exec_id}_execution.zip"
    return StreamingResponse(
        buf, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.get("/download/{run_id}/{kind}")
async def download(run_id: str, kind: str, token: str = ""):
    # kind is "report" or "data"
    fname = {"report": "report.html", "data": "data.xlsx"}.get(kind)
    if not fname:
        raise HTTPException(status_code=404)
    # run_id is a hex token generated by us - safe to join directly
    if not run_id.isalnum():
        raise HTTPException(status_code=400)
    if not _verify_download_token(run_id, token):
        raise HTTPException(status_code=403, detail="This link is invalid or has expired. Please re-run the analysis to get a fresh link.")
    path = RUNS_DIR / run_id / fname
    if not path.exists():
        raise HTTPException(status_code=404, detail="Run not found or expired.")
    media = "text/html" if kind == "report" else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return FileResponse(path, media_type=media, filename=f"{run_id}_{fname}")


@app.get("/history", response_class=HTMLResponse)
async def history_form(request: Request):
    return templates.TemplateResponse(request, "history.html", {"error": None, "entries": None})


@app.post("/history", response_class=HTMLResponse)
async def history_lookup(request: Request, access_code: str = Form(...), initials: str = Form("")):
    _sweep_completed_runs()

    try:
        client_name = _check_access(request, access_code)
    except HTTPException as e:
        return templates.TemplateResponse(request, "history.html", {"error": e.detail, "entries": None}, status_code=e.status_code
        )

    initials = initials.strip()
    all_entries = _read_history(client_name)

    now = time.time()
    rows = []
    for row in all_entries:
        expired = (now - row.get("logged_at", 0)) > RUN_RETENTION_SECONDS
        download_url = None
        data_url = None
        execute_url = None
        if row.get("kind") == "execute":
            exec_token_id = f"{row['run_id']}/{row['exec_id']}"
            download_url = None if expired else f"/download-exec/{row['run_id']}/{row['exec_id']}/report.html?token={_make_download_token(exec_token_id)}"
        elif row.get("kind") == "imported":
            # No report/workbook exists for this kind - the next step is
            # always to run the confirmed cases live, and that page's own
            # data lives on the same 7-day disk-retention clock.
            execute_url = None if expired else f"/execute/{row['run_id']}"
        else:
            download_url = None if expired else f"/download/{row['run_id']}/report?token={_make_download_token(row['run_id'])}"
            data_url = None if expired else f"/download/{row['run_id']}/data?token={_make_download_token(row['run_id'])}"
        rows.append({**row, "expired": expired, "download_url": download_url, "data_url": data_url, "execute_url": execute_url})

    my_rows = [r for r in rows if initials and r.get("initials", "").lower() == initials.lower()]
    show_mine_default = bool(initials and my_rows)

    return templates.TemplateResponse(request, "history.html",
        {
            "error": None, "entries": rows, "my_entries": my_rows,
            "initials": initials, "show_mine_default": show_mine_default,
            "hide_trial_cta": True,
        },
    )


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
