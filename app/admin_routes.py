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

import os
from pathlib import Path

from fastapi import APIRouter, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import run_logger
from . import mailer
from . import trial_signups

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
