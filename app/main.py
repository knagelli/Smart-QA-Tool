"""
Smart QA Tool - Phase 1 web app
Requirements file + application name -> validation -> test scenarios ->
traceability matrix -> HTML report + Excel workbook.

Your Anthropic API key lives only in this server's environment (ANTHROPIC_API_KEY).
Clients never see it and never need their own Claude account.

Run locally:
    export ANTHROPIC_API_KEY=sk-ant-...
    export CLIENT_ACCESS_CODES=acme:letmein123,globex:hunter2   # client_name:code pairs
    uvicorn app.main:app --reload --port 8000

Deploy: any host that runs a Python ASGI app (Render, Railway, Fly.io, a VM).
See README.md for a walkthrough.
"""
import os
import uuid
import shutil
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .extract import extract_text
from .qa_engine import run_qa_analysis
from .report_builder import build_html, build_xlsx

BASE_DIR = Path(__file__).resolve().parent
RUNS_DIR = BASE_DIR / "runs"
RUNS_DIR.mkdir(exist_ok=True)

app = FastAPI(title="Smart QA Tool")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


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


def _check_access(code: str) -> str:
    codes = _load_access_codes()
    if not codes:
        # No codes configured -> open access (fine for local testing, NOT for
        # a public deployment). README calls this out explicitly.
        return "test-client"
    client_name = codes.get(code)
    if not client_name:
        raise HTTPException(status_code=401, detail="Invalid access code.")
    return client_name


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "error": None})


@app.post("/analyze", response_class=HTMLResponse)
async def analyze(
    request: Request,
    access_code: str = Form(...),
    application: str = Form(...),
    requirements_file: UploadFile = File(...),
):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return templates.TemplateResponse(
            "index.html",
            {"request": request, "error": "Server is not configured with an API key. Contact the operator."},
            status_code=500,
        )

    try:
        client_name = _check_access(access_code)
    except HTTPException as e:
        return templates.TemplateResponse(
            "index.html", {"request": request, "error": e.detail}, status_code=401
        )

    raw_bytes = await requirements_file.read()
    if not raw_bytes:
        return templates.TemplateResponse(
            "index.html", {"request": request, "error": "The uploaded file is empty."}, status_code=400
        )

    try:
        req_text = extract_text(requirements_file.filename, raw_bytes)
    except Exception as e:
        return templates.TemplateResponse(
            "index.html", {"request": request, "error": f"Could not read the file: {e}"}, status_code=400
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
        return templates.TemplateResponse(
            "index.html", {"request": request, "error": f"Analysis failed: {e}"}, status_code=502
        )

    data["requirements_source"] = requirements_file.filename
    data["run_date"] = datetime.now().strftime("%Y-%m-%d %H:%M")

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
            "client_name": client_name,
            "application": application,
            "total_reqs": total_reqs,
            "flagged": flagged,
            "total_tcs": total_tcs,
        },
    )


@app.get("/download/{run_id}/{kind}")
async def download(run_id: str, kind: str):
    # kind is "report" or "data"
    fname = {"report": "report.html", "data": "data.xlsx"}.get(kind)
    if not fname:
        raise HTTPException(status_code=404)
    # run_id is a hex token generated by us - safe to join directly
    if not run_id.isalnum():
        raise HTTPException(status_code=400)
    path = RUNS_DIR / run_id / fname
    if not path.exists():
        raise HTTPException(status_code=404, detail="Run not found or expired.")
    media = "text/html" if kind == "report" else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return FileResponse(path, media_type=media, filename=f"{run_id}_{fname}")


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
