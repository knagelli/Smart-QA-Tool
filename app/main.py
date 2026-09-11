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
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import shutil
import socket
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path
from typing import List
from urllib.parse import urlparse

from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .extract import extract_text
from .qa_engine import run_qa_analysis, run_qa_analysis_custom, structure_existing_test_cases
from .report_builder import build_html, build_xlsx, build_html_custom, build_xlsx_custom
from .diagram_parser import parse_flow_diagrams
from .execute_engine import execute_test_case, ExecutionError
from .execution_report import build_execution_report
from .import_parser import try_parse_tabular
from . import run_logger
from . import admin_routes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("req2qa")

BASE_DIR = Path(__file__).resolve().parent
RUNS_DIR = BASE_DIR / "runs"
RUNS_DIR.mkdir(exist_ok=True)
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

app = FastAPI(title="Req2QA — Requirements to Test Coverage")
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
MAX_TEST_CASES_PER_EXECUTION = 5
EXECUTIONS_MAX_AGE_SECONDS = DOWNLOAD_LINK_TTL_SECONDS + 24 * 60 * 60


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


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "error": None})


# Static trust pages - no access code required, nothing sensitive is served.
# Their presence (and the footer links to them) is itself part of what a
# regulated client's security/procurement review and some automated URL
# categorization scanners look for on a new vendor's site.
_TRUST_PAGE_UPDATED = "2026-09-09"


@app.get("/privacy", response_class=HTMLResponse)
async def privacy(request: Request):
    return templates.TemplateResponse("privacy.html", {"request": request, "updated_date": _TRUST_PAGE_UPDATED})


@app.get("/terms", response_class=HTMLResponse)
async def terms(request: Request):
    return templates.TemplateResponse("terms.html", {"request": request, "updated_date": _TRUST_PAGE_UPDATED})


@app.get("/security", response_class=HTMLResponse)
async def security_page(request: Request):
    return templates.TemplateResponse("security.html", {"request": request, "updated_date": _TRUST_PAGE_UPDATED})


@app.get("/robots.txt", response_class=PlainTextResponse)
async def robots_txt():
    # No robots.txt previously existed, which some crawlers/URL-categorization
    # scanners treat conservatively as "don't index anything" - an explicit,
    # permissive policy removes that ambiguity and helps legitimate crawlers
    # (Google, Bing, and vendor categorization bots) index and classify the
    # site correctly instead of leaving it "uncategorized."
    return "User-agent: *\nAllow: /\n"


@app.post("/analyze", response_class=HTMLResponse)
async def analyze(
    request: Request,
    access_code: str = Form(...),
    application: str = Form(...),
    requirements_file: UploadFile = File(...),
    baseline_version: str = Form(""),
    initials: str = Form(""),
):
    _sweep_completed_runs()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.error("ANTHROPIC_API_KEY is not configured.")
        return templates.TemplateResponse(
            "index.html",
            {"request": request, "error": "This service is not yet available. Please contact the operator."},
            status_code=500,
        )

    try:
        client_name = _check_access(request, access_code)
    except HTTPException as e:
        return templates.TemplateResponse(
            "index.html", {"request": request, "error": e.detail}, status_code=e.status_code
        )

    raw_bytes = await requirements_file.read()
    if not raw_bytes:
        return templates.TemplateResponse(
            "index.html", {"request": request, "error": "The uploaded file is empty."}, status_code=400
        )
    if len(raw_bytes) > MAX_DOC_BYTES:
        return templates.TemplateResponse(
            "index.html",
            {"request": request, "error": f"That file is too large (max {MAX_DOC_BYTES // (1024*1024)} MB)."},
            status_code=400,
        )
    if not _validate_upload(requirements_file.filename, raw_bytes):
        return templates.TemplateResponse(
            "index.html",
            {"request": request, "error": "That file doesn't look like a valid document of its type. Please re-export and try again."},
            status_code=400,
        )

    try:
        req_text = extract_text(requirements_file.filename, raw_bytes)
    except Exception as e:
        ref = _log_and_ref(e, "extract_text failed in /analyze")
        return templates.TemplateResponse(
            "index.html", {"request": request, "error": GENERIC_ERROR_MESSAGE.format(ref=ref)}, status_code=400
        )

    if not req_text.strip():
        return templates.TemplateResponse(
            "index.html",
            {"request": request, "error": "No text could be extracted from that file."},
            status_code=400,
        )

    run_id = uuid.uuid4().hex[:12]
    rl = run_logger.RunLog.start("generate_a", run_id, {
        "application": application,
        "filename": requirements_file.filename,
        "file_size": len(raw_bytes),
        "qa_model": os.environ.get("QA_MODEL"),
    })
    try:
        data = run_qa_analysis(application, req_text, api_key)
    except Exception as e:
        ref = _log_and_ref(e, "run_qa_analysis failed in /analyze")
        rl.finish("fail", {"correlation_ref": ref, "error": str(e)})
        return templates.TemplateResponse(
            "index.html", {"request": request, "error": GENERIC_ERROR_MESSAGE.format(ref=ref)}, status_code=502
        )

    data["application"] = application
    data["requirements_source"] = requirements_file.filename
    data["run_date"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    data["baseline_version"] = baseline_version.strip()

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

    total_reqs = len(data.get("validation", []))
    flagged = sum(1 for v in data.get("validation", []) if not v.get("valid_for_app"))
    total_tcs = len(data.get("test_scenarios", []))
    rl.finish("ok", {"test_case_count": total_tcs, "flagged_requirements": flagged})

    return templates.TemplateResponse(
        "result.html",
        {
            "request": request,
            "run_id": run_id,
            "download_token": _make_download_token(run_id),
            "client_name": client_name,
            "application": application,
            "mode": "named",
            "total_reqs": total_reqs,
            "flagged": flagged,
            "total_tcs": total_tcs,
            "baseline_version": data["baseline_version"],
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
        return templates.TemplateResponse(
            "index.html",
            {"request": request, "error": "This service is not yet available. Please contact the operator.", "active_tab": "custom"},
            status_code=500,
        )

    try:
        client_name = _check_access(request, access_code)
    except HTTPException as e:
        return templates.TemplateResponse(
            "index.html", {"request": request, "error": e.detail, "active_tab": "custom"}, status_code=e.status_code
        )

    def err(msg, code=400):
        return templates.TemplateResponse(
            "index.html", {"request": request, "error": msg, "active_tab": "custom"}, status_code=code
        )

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

    return templates.TemplateResponse(
        "review_flow.html",
        {
            "request": request,
            "run_id": run_id,
            "application": application,
            "flow": flow,
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

    confirmed_steps = []
    for i in range(step_count):
        # A step can be removed on the review screen (its "keep" checkbox unchecked)
        if not form.get(f"keep_{i}"):
            continue
        inputs_raw = form.get(f"inputs_{i}", "")
        confirmed_steps.append({
            "step_id": form.get(f"step_id_{i}", f"FLOW-{i+1:03d}"),
            "screen_or_stage": form.get(f"screen_{i}", "").strip(),
            "description": form.get(f"description_{i}", "").strip(),
            "inputs": [s.strip() for s in inputs_raw.split(",") if s.strip()],
            "decision_point": bool(form.get(f"decision_point_{i}")),
            "decision_detail": form.get(f"decision_detail_{i}", "").strip(),
        })

    if not confirmed_steps:
        raise HTTPException(status_code=400, detail="At least one confirmed flow step is required to generate test coverage.")

    confirmed_flow = {
        "flow_name": form.get("flow_name", pending["flow"].get("flow_name", "Main flow")).strip(),
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
    data["run_date"] = datetime.now().strftime("%Y-%m-%d %H:%M")
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

    return templates.TemplateResponse(
        "result.html",
        {
            "request": request,
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
    return templates.TemplateResponse("import_tests.html", {"request": request, "error": None})


@app.post("/import-tests", response_class=HTMLResponse)
async def import_tests(
    request: Request,
    access_code: str = Form(...),
    application: str = Form(...),
    initials: str = Form(""),
    test_cases_file: UploadFile = File(...),
):
    _sweep_pending_imports()
    _sweep_completed_runs()

    def err(msg, code=400):
        return templates.TemplateResponse("import_tests.html", {"request": request, "error": msg}, status_code=code)

    try:
        client_name = _check_access(request, access_code)
    except HTTPException as e:
        return err(e.detail, code=e.status_code)

    raw_bytes = await test_cases_file.read()
    if not raw_bytes:
        return err("The uploaded file is empty.")
    if len(raw_bytes) > MAX_DOC_BYTES:
        return err(f"That file is too large (max {MAX_DOC_BYTES // (1024*1024)} MB).")
    if not _validate_upload(test_cases_file.filename, raw_bytes):
        return err("That file doesn't look like a valid document of its type. Please re-export and try again.")

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.error("ANTHROPIC_API_KEY is not configured.")
        return err("This service is not yet available. Please contact the operator.", code=500)

    run_id = uuid.uuid4().hex[:12]
    rl = run_logger.RunLog.start("import", run_id, {
        "application": application,
        "filename": test_cases_file.filename,
        "file_size": len(raw_bytes),
    })

    # Deterministic path first (free, instant, can't misread a clean table) -
    # only falls through to AI structuring if headers aren't confidently
    # recognized, or the file isn't tabular at all (.docx/.txt/.pdf).
    parsed_cases = try_parse_tabular(test_cases_file.filename, raw_bytes)
    parser_path = "deterministic_tabular"
    parse_notes = ""
    if parsed_cases is None:
        parser_path = "ai_structuring_fallback"
        try:
            raw_text = extract_text(test_cases_file.filename, raw_bytes)
        except Exception as e:
            ref = _log_and_ref(e, "extract_text failed in /import-tests")
            rl.finish("fail", {"correlation_ref": ref, "error": str(e), "parser_path": parser_path})
            return err(GENERIC_ERROR_MESSAGE.format(ref=ref))
        if not raw_text.strip():
            rl.finish("fail", {"error": "no text extracted", "parser_path": parser_path})
            return err("No text could be extracted from that file.")
        try:
            structured = structure_existing_test_cases(application, raw_text, api_key)
        except Exception as e:
            ref = _log_and_ref(e, "structure_existing_test_cases failed in /import-tests")
            rl.finish("fail", {"correlation_ref": ref, "error": str(e), "parser_path": parser_path})
            return err(GENERIC_ERROR_MESSAGE.format(ref=ref), code=502)
        parsed_cases = structured.get("test_cases", [])
        parse_notes = structured.get("parse_notes", "")

    if not parsed_cases:
        rl.finish("fail", {"error": "no test cases identified", "parser_path": parser_path})
        return err(
            "No test cases could be identified in that file. "
            + (parse_notes or "Try a file with clearer per-case structure (a table, or one case per section).")
        )
    if len(parsed_cases) > MAX_IMPORTED_CASES:
        rl.finish("fail", {"error": "too many test cases", "count": len(parsed_cases), "parser_path": parser_path})
        return err(f"That file appears to contain more than {MAX_IMPORTED_CASES} test cases - please split it into smaller batches.")

    rl.finish("ok", {"parser_path": parser_path, "test_case_count": len(parsed_cases)})

    pending_dir = IMPORT_PENDING_DIR / run_id
    pending_dir.mkdir(parents=True, exist_ok=True)
    (pending_dir / "pending.json").write_text(json.dumps({
        "client_name": client_name,
        "application": application,
        "initials": initials,
        "source_filename": test_cases_file.filename,
        "parse_notes": parse_notes,
        "test_cases": parsed_cases,
    }))

    return templates.TemplateResponse(
        "review_import.html",
        {"request": request, "run_id": run_id, "application": application, "parse_notes": parse_notes, "test_cases": parsed_cases},
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
        confirmed_cases.append({
            "tc_id": form.get(f"tc_id_{i}", f"TC-{i+1:03d}").strip(),
            "req_id": "",
            "title": form.get(f"title_{i}", "").strip(),
            "precondition": form.get(f"precondition_{i}", "").strip(),
            "steps": form.get(f"steps_{i}", "").strip(),
            "expected_result": form.get(f"expected_result_{i}", "").strip(),
        })

    if not confirmed_cases:
        raise HTTPException(status_code=400, detail="At least one confirmed test case is required.")

    data = {
        "application": pending["application"],
        "requirements_source": pending["source_filename"],
        "run_date": datetime.now().strftime("%Y-%m-%d %H:%M"),
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

    return templates.TemplateResponse(
        "import_result.html",
        {"request": request, "run_id": run_id, "application": pending["application"], "total_tcs": len(confirmed_cases)},
    )


# --------------------------------------------------------------------------
# Live Execution (Phase 2) - generic engine, not app-specific. Runs a
# selection of an already-generated run's test cases live against a
# client-supplied sandbox/UAT environment. See execute_engine.py for the
# credential-handling and SSRF-guard rationale.
# --------------------------------------------------------------------------
EXECUTIONS_DIRNAME = "executions"


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

    test_cases = data.get("test_scenarios", [])
    return templates.TemplateResponse(
        "execute_select.html",
        {
            "request": request,
            "run_id": run_id,
            "application": data.get("application", ""),
            "test_cases": test_cases,
            "max_selectable": MAX_TEST_CASES_PER_EXECUTION,
            "error": None,
        },
    )


@app.post("/execute/{run_id}", response_class=HTMLResponse)
async def execute_run(
    request: Request,
    run_id: str,
    access_code: str = Form(...),
    role_label: str = Form(...),
    module: str = Form(...),
    env_url: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    initials: str = Form(""),
):
    if not run_id.isalnum():
        raise HTTPException(status_code=400)
    data_path = RUNS_DIR / run_id / "data.json"
    if not data_path.exists():
        raise HTTPException(status_code=404, detail="This run was not found or has expired. Please run the analysis again.")

    def err(msg, code=400, test_cases=None, application=""):
        return templates.TemplateResponse(
            "execute_select.html",
            {
                "request": request, "run_id": run_id, "application": application,
                "test_cases": test_cases or [], "max_selectable": MAX_TEST_CASES_PER_EXECUTION, "error": msg,
            },
            status_code=code,
        )

    try:
        data = json.loads(data_path.read_text())
    except Exception as e:
        ref = _log_and_ref(e, "failed to load data.json in /execute (POST)")
        raise HTTPException(status_code=500, detail=GENERIC_ERROR_MESSAGE.format(ref=ref))

    application = data.get("application", "")
    all_test_cases = {tc.get("tc_id"): tc for tc in data.get("test_scenarios", [])}

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.error("ANTHROPIC_API_KEY is not configured.")
        return err("This service is not yet available. Please contact the operator.", code=500, test_cases=data.get("test_scenarios"), application=application)

    try:
        client_name = _check_access(request, access_code)
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

    if not _validate_env_url(env_url):
        return err(
            "That environment URL doesn't look reachable, or it isn't a public sandbox/UAT address. "
            "Double-check the URL your test environment gives you.",
            test_cases=data.get("test_scenarios"), application=application,
        )

    exec_id = uuid.uuid4().hex[:12]
    exec_dir = RUNS_DIR / run_id / EXECUTIONS_DIRNAME / exec_id
    exec_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for tc_id in selected_ids:
        tc = all_test_cases[tc_id]
        shots_dir = exec_dir / f"shots_{tc_id}".replace("/", "_")
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
            "env_url_host": urlparse(env_url).hostname,
            "qa_model": os.environ.get("QA_MODEL"),
        })
        try:
            outcome = execute_test_case(
                application=application,
                role_label=role_label,
                module=module,
                test_case=tc,
                env_url=env_url,
                username=username,
                password=password,
                api_key=api_key,
                shots_dir=shots_dir,
                rl=rl,
            )
            results.append({
                "tc_id": tc_id,
                "title": tc.get("title", ""),
                "verdict": outcome["verdict"],
                "notes": outcome["notes"],
                "step_log": outcome["step_log"],
                "screenshots": [f"{shots_dir.name}/{fn}" for fn in outcome["screenshots"]],
            })
            rl.finish(outcome["verdict"].lower(), {"notes": outcome["notes"], "step_count": len(outcome["step_log"])})
        except ExecutionError as e:
            ref = _log_and_ref(e, f"execute_test_case ExecutionError for {tc_id} in /execute")
            rl.finish("blocked", {"correlation_ref": ref, "error": str(e)})
            results.append({
                "tc_id": tc_id, "title": tc.get("title", ""), "verdict": "BLOCKED",
                "notes": f"Could not complete this test case: {e}", "step_log": [], "screenshots": [],
            })
        except Exception as e:
            ref = _log_and_ref(e, f"execute_test_case failed for {tc_id} in /execute")
            rl.finish("blocked", {"correlation_ref": ref, "error": type(e).__name__})
            results.append({
                "tc_id": tc_id, "title": tc.get("title", ""), "verdict": "BLOCKED",
                "notes": GENERIC_ERROR_MESSAGE.format(ref=ref), "step_log": [], "screenshots": [],
            })
    # Credentials go out of scope here and are never referenced again in this
    # function - nothing below this point has access to `username`/`password`.

    env_label = urlparse(env_url).hostname or "environment"
    run_date = datetime.now().strftime("%Y-%m-%d %H:%M")
    exec_token = _make_download_token(f"{run_id}/{exec_id}")
    report_data = {
        "application": application, "client_name": client_name, "run_date": run_date,
        "environment_label": env_label, "role_label": role_label, "results": results,
        "token": exec_token,
    }
    (exec_dir / "report.html").write_text(build_execution_report(report_data))

    # Execution audit log - who ran what against which (sandbox) host, and
    # the outcome. Never the credentials, never the full URL (which could
    # contain a token/query string) - host label only.
    with open(RUNS_DIR / "execution_log.csv", "a") as f:
        verdicts = ";".join(f"{r['tc_id']}={r['verdict']}" for r in results)
        f.write(f"{run_id},{exec_id},{client_name},{run_date},{application},{role_label},{module},{env_label},{verdicts}\n")
    _append_history("execute", run_id, client_name, initials, application, run_date, exec_id=exec_id)

    summary_counts = {
        "total": len(results),
        "passed": sum(1 for r in results if r["verdict"] == "PASS"),
        "failed": sum(1 for r in results if r["verdict"] == "FAIL"),
        "blocked": sum(1 for r in results if r["verdict"] == "BLOCKED"),
    }

    return templates.TemplateResponse(
        "execute_result.html",
        {
            "request": request, "run_id": run_id, "exec_id": exec_id, "application": application,
            "role_label": role_label, "module": module, "environment_label": env_label,
            "download_token": exec_token,
            **summary_counts,
        },
    )


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
    return templates.TemplateResponse("history.html", {"request": request, "error": None, "entries": None})


@app.post("/history", response_class=HTMLResponse)
async def history_lookup(request: Request, access_code: str = Form(...), initials: str = Form("")):
    _sweep_completed_runs()

    try:
        client_name = _check_access(request, access_code)
    except HTTPException as e:
        return templates.TemplateResponse(
            "history.html", {"request": request, "error": e.detail, "entries": None}, status_code=e.status_code
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

    return templates.TemplateResponse(
        "history.html",
        {
            "request": request, "error": None, "entries": rows, "my_entries": my_rows,
            "initials": initials, "show_mine_default": show_mine_default,
        },
    )


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
