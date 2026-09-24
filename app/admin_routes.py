# admin_routes.py
#
# A plain web page for Kalyan to search logs and check a client-supplied
# screenshot against a stored hash - no terminal, no raw JSON, no commands.
# Everything is a form: type in a search box, click a log to open it, or
# upload an image and click "Check this image".
#
# Add to main.py:
#     from . import admin_routes
#     app.include_router(admin_routes.router)
#
# Add this env var in Render's dashboard (Environment tab):
#     ADMIN_LOG_KEY = <a long random string you generate yourself>
#
# Nothing here is linked from any client-facing page, and a client's own
# access code does not grant entry - the only way in is the one password
# on the login page below.

import csv
import io
import os
import secrets
import zipfile
from pathlib import Path

from fastapi import APIRouter, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from . import run_logger
from . import mailer
from . import trial_signups
from . import client_quotas

router = APIRouter()
BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

ADMIN_LOG_KEY = os.environ.get("ADMIN_LOG_KEY")
COOKIE_NAME = "req2qa_admin_session"


def _is_authed(request: Request) -> bool:
    if not ADMIN_LOG_KEY:
        return False
    return request.cookies.get(COOKIE_NAME) == ADMIN_LOG_KEY


@router.get("/admin/login", response_class=HTMLResponse)
async def login_form(request: Request, error: str = ""):
    return templates.TemplateResponse(
        request, "admin_login.html", {"error": error}
    )


@router.post("/admin/login")
async def login_submit(request: Request, password: str = Form(...)):
    if not ADMIN_LOG_KEY or password != ADMIN_LOG_KEY:
        return RedirectResponse(url="/admin/login?error=1", status_code=303)
    resp = RedirectResponse(url="/admin/logs", status_code=303)
    resp.set_cookie(COOKIE_NAME, ADMIN_LOG_KEY, httponly=True, secure=True, samesite="strict")
    return resp


@router.get("/admin/logout")
async def logout():
    resp = RedirectResponse(url="/admin/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME)
    return resp


@router.get("/admin/logs", response_class=HTMLResponse)
async def logs_search(
    request: Request, q: str = "", run_type: str = "", status: str = ""
):
    if not _is_authed(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    results = run_logger.search_logs(
        query=q or None, run_type=run_type or None, status=status or None
    )
    return templates.TemplateResponse(
        request,
        "admin_logs_list.html",
        {"results": results, "q": q, "run_type": run_type, "status": status},
    )


# These two must stay registered BEFORE /admin/logs/{log_id} below - FastAPI
# matches routes in registration order, and "/admin/logs/export.csv" has the
# same path shape as "/admin/logs/{log_id}" (log_id would just become the
# literal string "export.csv"), so the export routes would never be reached
# if they came after the catch-all one.
@router.get("/admin/logs/export.csv")
async def logs_export_csv(request: Request, q: str = "", run_type: str = "", status: str = ""):
    """One row per matching run: its search-page summary plus token usage
    and an estimated USD cost. Built so a cost or troubleshooting sweep
    across many runs doesn't mean opening each one by hand - the same
    q/run_type/status filters as the search box narrow this export too."""
    if not _is_authed(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    rows = run_logger.export_csv_rows(query=q or None, run_type=run_type or None, status=status or None)
    buf = io.StringIO()
    fieldnames = [
        "log_id", "run_type", "run_id", "correlation_ref", "status", "started",
        "input_tokens", "output_tokens", "cache_write_tokens", "cache_read_tokens",
        "estimated_cost_usd",
    ]
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=req2qa_logs_export.csv"},
    )


@router.get("/admin/logs/export.zip")
async def logs_export_zip(request: Request, q: str = "", run_type: str = "", status: str = ""):
    """The raw .jsonl for every matching run, zipped, for a deeper dive than
    the CSV summary supports (e.g. reading full step-by-step events across
    several runs at once instead of clicking into each one)."""
    if not _is_authed(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    summaries = run_logger.search_logs(query=q or None, run_type=run_type or None, status=status or None, limit=10_000)
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as zf:
        for s in summaries:
            log_id = s["log_id"]
            path = run_logger.LOG_ROOT / f"{log_id}.jsonl"
            if path.exists():
                zf.write(path, arcname=f"{log_id}.jsonl")
    mem.seek(0)
    return StreamingResponse(
        mem,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=req2qa_logs_export.zip"},
    )


@router.get("/admin/logs/{log_id}", response_class=HTMLResponse)
async def log_detail(request: Request, log_id: str, match_info: str = ""):
    if not _is_authed(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    records = run_logger.read_log(log_id)
    integrity = run_logger.verify_log_integrity(log_id)
    return templates.TemplateResponse(
        request,
        "admin_log_detail.html",
        {
            "log_id": log_id,
            "records": records or [],
            "found": records is not None,
            "integrity": integrity,
            "match_info": match_info,
        },
    )


@router.post("/admin/logs/{log_id}/compare-screenshot", response_class=HTMLResponse)
async def compare_screenshot(request: Request, log_id: str, image: UploadFile = File(...)):
    """The 'upload the image the client sent you' button. Hashes the
    uploaded file server-side and reports in plain language whether it
    matches a screenshot this run actually produced - no manual hashing,
    no terminal."""
    if not _is_authed(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    contents = await image.read()
    uploaded_hash = run_logger.hash_bytes(contents)
    matches = run_logger.find_screenshot_hash_matches(log_id, uploaded_hash)
    if matches:
        match_info = f"MATCH - this is the authentic, unmodified image from step {matches[0].get('step', '?')}."
    else:
        match_info = (
            "NO MATCH - either this file was re-saved/re-compressed/edited since capture, "
            "or it did not come from this run. Ask for the original file as an attachment "
            "(not pasted into an email/chat) and try again before treating this as evidence "
            "of anything."
        )
    return RedirectResponse(
        url=f"/admin/logs/{log_id}?match_info={match_info}", status_code=303
    )


@router.get("/admin/trials", response_class=HTMLResponse)
async def trials_list(request: Request):
    if not _is_authed(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    signups = trial_signups.list_all_signups()
    return templates.TemplateResponse(request, "admin_trials.html", {"signups": signups})


@router.post("/admin/trials/reset")
async def trials_reset(request: Request, domain: str = Form(...)):
    """Recovery path for a domain locked out by a wrong/typo'd/adversarial
    signup - wipes that domain's trial record so it can sign up again.
    There was previously no way to undo a bad signup short of hand-editing
    the JSON store on the server."""
    if not _is_authed(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    trial_signups.reset_domain(domain)
    return RedirectResponse(url="/admin/trials", status_code=303)


@router.get("/admin/quotas", response_class=HTMLResponse)
async def quotas_list(request: Request, new_code: str = "", new_client: str = "", error: str = ""):
    if not _is_authed(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    quotas = client_quotas.list_all_quotas()
    # Legacy/unconfigured-codes visibility (2026-09-21 - see council-review-
    # entitlement-confidence-uplift-and-onboarding-2026-09-21.md): an env-var
    # access code with no matching quota row here fail-opens to unrestricted
    # ("both") and is otherwise invisible on this page - surface it loudly
    # instead of leaving it silently absent from the table.
    configured_codes = {q["access_code"] for q in quotas}
    from . import main as _main_module  # local import - avoids a circular import at module load time
    unconfigured = sorted(
        {code: name for code, name in _main_module._load_access_codes().items() if code not in configured_codes}.items()
    )
    return templates.TemplateResponse(request, "admin_quotas.html", {
        "quotas": quotas, "unconfigured": unconfigured,
        "new_code": new_code, "new_client": new_client, "error": error,
        "hide_trial_cta": True, "active_admin_nav": "quotas",
    })


@router.post("/admin/quotas/set")
async def quotas_set(request: Request, access_code: str = Form(...), client_name: str = Form(...), subscribed_count: int = Form(...), service_type: str = Form(...), subscribed_execution_count: str = Form("")):
    """Updates an EXISTING client's subscribed count and/or service
    entitlement. service_type is now required with no default (2026-09-21) -
    an admin submitting this form must actively choose, rather than a silent
    "both" going through unnoticed. For onboarding a brand-new client, use
    "Add Client" below instead, which also generates the access code itself.

    subscribed_execution_count is OPTIONAL and blank by default - left blank,
    it does not touch whatever execution allowance already exists (still
    auto-set to "however many were kept" for generation-then-execute
    clients). Filled in, it's an explicit admin override - see
    client_quotas.set_quota for why this exists (execution-only clients who
    never generate through this tool otherwise never get one set at all)."""
    if not _is_authed(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    if service_type not in client_quotas.SERVICE_TYPES:
        return RedirectResponse(url="/admin/quotas?error=Please+choose+a+service+entitlement.", status_code=303)
    exec_count = None
    if subscribed_execution_count.strip():
        try:
            exec_count = int(subscribed_execution_count.strip())
        except ValueError:
            return RedirectResponse(url="/admin/quotas?error=Execution+limit+must+be+a+whole+number.", status_code=303)
    client_quotas.set_quota(access_code.strip(), client_name.strip(), subscribed_count, service_type.strip(), exec_count)
    return RedirectResponse(url="/admin/quotas", status_code=303)


@router.post("/admin/clients/add")
async def clients_add(request: Request, client_name: str = Form(...), subscribed_count: int = Form(...), service_type: str = Form(...), access_code: str = Form(""), subscribed_execution_count: str = Form("")):
    """The single onboarding action for a brand-new paid client (2026-09-21
    - see council-review-entitlement-confidence-uplift-and-onboarding-2026-
    09-21.md). Creates the access code (auto-generated if left blank -
    secrets.token_urlsafe, not a Kalyan-chosen memorable string), the quota,
    AND the service entitlement in ONE action - no server restart, no
    separate env-var edit, no second step to forget. service_type is
    required with no default, same as quotas_set above.

    _load_access_codes() in main.py merges client_quotas.json into the
    valid-access-code list, so a client created here can log in and use the
    tool immediately - CLIENT_ACCESS_CODES (the env var) is no longer the
    only way to onboard someone, just the original/legacy one."""
    if not _is_authed(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    if service_type not in client_quotas.SERVICE_TYPES:
        return RedirectResponse(url="/admin/quotas?error=Please+choose+a+service+entitlement.", status_code=303)
    exec_count = None
    if subscribed_execution_count.strip():
        try:
            exec_count = int(subscribed_execution_count.strip())
        except ValueError:
            return RedirectResponse(url="/admin/quotas?error=Execution+limit+must+be+a+whole+number.", status_code=303)
    code = access_code.strip() or secrets.token_urlsafe(9)
    client_quotas.set_quota(code, client_name.strip(), subscribed_count, service_type.strip(), exec_count)
    from urllib.parse import quote
    return RedirectResponse(
        url=f"/admin/quotas?new_code={quote(code)}&new_client={quote(client_name.strip())}",
        status_code=303,
    )


@router.post("/admin/quotas/reset-attempts")
async def quotas_reset_attempts(request: Request, access_code: str = Form(...)):
    if not _is_authed(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    client_quotas.reset_attempts(access_code.strip())
    return RedirectResponse(url="/admin/quotas", status_code=303)


@router.get("/admin/test-email", response_class=HTMLResponse)
async def test_email_form(request: Request, result: str = "", ok: str = ""):
    if not _is_authed(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    return templates.TemplateResponse(
        request,
        "admin_test_email.html",
        {
            "result": result,
            "ok": ok == "1",
            "configured": mailer.mailer_configured(),
        },
    )


@router.post("/admin/test-email")
async def test_email_send(request: Request, to_addr: str = Form(...)):
    if not _is_authed(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    from starlette.concurrency import run_in_threadpool

    success, message = await run_in_threadpool(
        mailer.send_email,
        to_addr=to_addr.strip(),
        subject="Req2QA test email",
        body_text=(
            "This is a test email sent from the Req2QA admin panel to confirm "
            "the SMTP integration is working.\n\nIf you received this, sending "
            "is configured correctly."
        ),
    )
    from urllib.parse import quote

    return RedirectResponse(
        url=f"/admin/test-email?result={quote(message)}&ok={'1' if success else '0'}",
        status_code=303,
    )
