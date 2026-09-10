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
import json
import logging
import os
import shutil
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path
from typing import List

from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .extract import extract_text
from .qa_engine import run_qa_analysis, run_qa_analysis_custom
from .report_builder import build_html, build_xlsx, build_html_custom, build_xlsx_custom
from .diagram_parser import parse_flow_diagrams

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
        "script-src 'self'; "
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

    try:
        data = run_qa_analysis(application, req_text, api_key)
    except Exception as e:
        ref = _log_and_ref(e, "run_qa_analysis failed in /analyze")
        return templates.TemplateResponse(
            "index.html", {"request": request, "error": GENERIC_ERROR_MESSAGE.format(ref=ref)}, status_code=502
        )

    data["requirements_source"] = requirements_file.filename
    data["run_date"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    data["baseline_version"] = baseline_version.strip()

    run_id = uuid.uuid4().hex[:12]
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    html_path = run_dir / "report.html"
    xlsx_path = run_dir / "data.xlsx"
    html_path.write_text(build_html(data))
    build_xlsx(data, str(xlsx_path))

    # Basic run log per client (manual-onboarding scale; swap for a real DB later)
    with open(RUNS_DIR / "run_log.csv", "a") as f:
        f.write(f"{run_id},{client_name},{data['run_date']},{application},{requirements_file.filename}\n")

    total_reqs = len(data.get("validation", []))
    flagged = sum(1 for v in data.get("validation", []) if not v.get("valid_for_app"))
    total_tcs = len(data.get("test_scenarios", []))

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

    with open(RUNS_DIR / "run_log.csv", "a") as f:
        f.write(f"{run_id},{pending['client_name']},{data['run_date']},{pending['application']},{pending['requirements_source']} (custom)\n")

    shutil.rmtree(PENDING_DIR / run_id, ignore_errors=True)

    total_reqs = len(data.get("validation", []))
    flagged = sum(1 for v in data.get("validation", []) if not v.get("testable"))
    total_tcs = len(data.get("test_scenarios", []))
    uncovered_steps = len(data.get("uncovered_flow_steps", []))

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


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
