"""
Req2QA - Phase 1 web app
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
import json
import os
import uuid
import shutil
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .extract import extract_text
from .qa_engine import run_qa_analysis, run_qa_analysis_custom
from .report_builder import build_html, build_xlsx, build_html_custom, build_xlsx_custom
from .diagram_parser import parse_flow_diagrams

BASE_DIR = Path(__file__).resolve().parent
RUNS_DIR = BASE_DIR / "runs"
RUNS_DIR.mkdir(exist_ok=True)
PENDING_DIR = RUNS_DIR / "pending"  # Option B runs awaiting review-and-confirm of the parsed flow
PENDING_DIR.mkdir(exist_ok=True)

app = FastAPI(title="Req2QA — Requirements to Test Coverage")
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
    baseline_version: str = Form(""),
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
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return templates.TemplateResponse(
            "index.html",
            {"request": request, "error": "Server is not configured with an API key. Contact the operator.", "active_tab": "custom"},
            status_code=500,
        )

    try:
        client_name = _check_access(access_code)
    except HTTPException as e:
        return templates.TemplateResponse(
            "index.html", {"request": request, "error": e.detail, "active_tab": "custom"}, status_code=401
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

    try:
        brief_text = extract_text(brief_file.filename, brief_bytes)
        requirements_text = extract_text(requirements_file.filename, req_bytes)
    except Exception as e:
        return err(f"Could not read the brief or requirements file: {e}")

    if not requirements_text.strip():
        return err("No text could be extracted from the requirements document.")

    try:
        flow = parse_flow_diagrams(application, diagram_reads, api_key)
    except Exception as e:
        return err(f"Could not parse the process-flow diagram(s): {e}", code=502)

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
        raise HTTPException(status_code=500, detail="Server is not configured with an API key. Contact the operator.")

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
        raise HTTPException(status_code=502, detail=f"Analysis failed: {e}")

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
