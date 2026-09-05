"""
Diagram vision-parsing for Req2QA Custom Application Mode (Option B).

Turns one or more uploaded future-state process-flow diagrams (PNG/JPG
screenshots or PDF exports of Visio/draw.io/BPMN/whiteboard flows) into a
structured, ordered list of steps via Claude's vision capability, so a
client's bespoke web app - which Claude has no platform knowledge of - can
still be understood well enough to review, confirm, and generate tests
against.

This is deliberately a separate, narrow module: diagram parsing is the
highest-risk step in the custom-app pipeline (a misread diagram silently
produces wrong test cases), so its output always goes through a human
review-and-confirm screen (see main.py: /analyze-custom -> review_flow.html
-> /confirm-flow/{run_id}) before qa_engine.run_qa_analysis_custom ever
sees it.
"""
import base64
import json
import os
import re
from pathlib import Path

from anthropic import Anthropic

MODEL = os.environ.get("QA_MODEL", "claude-sonnet-4-6")
MAX_PAGES_PER_PDF = 8  # guard against a runaway multi-page Visio/PDF export

SYSTEM_PROMPT = (
    "You are an expert business analyst reading a future-state process-flow "
    "diagram (Visio, draw.io, BPMN, a whiteboard photo, or similar export). "
    "Extract every discrete step, screen, and decision point you can see, in "
    "the order the flow implies. You always respond with a single JSON "
    "object and nothing else - no markdown fences, no commentary before or "
    "after."
)

PROMPT = """Look at the attached process-flow diagram image(s) for the application "{application}".

TASK: extract a structured, ordered list of every step/screen in the flow.
For each step capture:
- step_id: FLOW-001, FLOW-002, ... in flow order (continue numbering across
  multiple images/pages as one flow unless the images clearly show unrelated
  flows, in which case say so in "parse_notes").
- screen_or_stage: the screen/stage name as labelled (or your best short
  label if unlabelled).
- description: one or two plain-English sentences describing what happens
  at this step.
- inputs: any fields/inputs mentioned or implied at this step (list of short
  strings, empty list if none).
- decision_point: true/false - is this a branch/decision node?
- decision_detail: if decision_point is true, the branch conditions/outcomes
  in plain English; otherwise an empty string.

Also include "flow_name" (a short label for the overall flow, or "Main flow"
if there is only one) and "parse_notes" (a short plain-English note flagging
anything ambiguous, illegible, low-confidence, or assumed - empty string if
there is nothing to flag). Be honest in parse_notes: this extraction will be
shown back to a human to confirm before anything else runs, so under-claiming
confidence is far safer than over-claiming it.

OUTPUT: respond with ONLY this JSON object (no other text):
{{
  "flow_name": "...",
  "parse_notes": "",
  "steps": [
    {{"step_id": "FLOW-001", "screen_or_stage": "...", "description": "...",
      "inputs": [], "decision_point": false, "decision_detail": ""}}
  ]
}}
"""


def _media_type_for(filename: str) -> str:
    ext = Path(filename).suffix.lower().lstrip(".")
    return {
        "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "webp": "image/webp", "gif": "image/gif",
    }.get(ext, "image/png")


def _pdf_to_images(raw_bytes: bytes) -> list:
    """Render each page of a diagram PDF to a PNG for vision input.
    Diagram PDFs (Visio/draw.io exports) are usually visual, not
    text-extractable, so this always rasterizes rather than trying
    extract.py's text path first."""
    import fitz  # PyMuPDF

    doc = fitz.open(stream=raw_bytes, filetype="pdf")
    pages = []
    for i, page in enumerate(doc):
        if i >= MAX_PAGES_PER_PDF:
            break
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))  # ~144dpi - legible for diagram text
        pages.append(pix.tobytes("png"))
    doc.close()
    return pages


def _files_to_image_blocks(files) -> list:
    """files: list of (filename, raw_bytes). Returns Anthropic image content blocks."""
    blocks = []
    for filename, raw_bytes in files:
        ext = Path(filename).suffix.lower().lstrip(".")
        if ext == "pdf":
            for png_bytes in _pdf_to_images(raw_bytes):
                blocks.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": base64.b64encode(png_bytes).decode("ascii"),
                    },
                })
        else:
            blocks.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": _media_type_for(filename),
                    "data": base64.b64encode(raw_bytes).decode("ascii"),
                },
            })
    return blocks


def parse_flow_diagrams(application: str, files, api_key: str) -> dict:
    """files: list of (filename, raw_bytes) for one or more diagram uploads.
    Returns {"flow_name": ..., "parse_notes": ..., "steps": [...]}.
    Raises ValueError if nothing could be read or parsed."""
    if not files:
        raise ValueError("No diagram files provided.")

    client = Anthropic(api_key=api_key)
    image_blocks = _files_to_image_blocks(files)
    if not image_blocks:
        raise ValueError("Could not read any pages/images from the uploaded diagram(s).")

    content = image_blocks + [
        {"type": "text", "text": PROMPT.format(application=application.strip())}
    ]

    resp = client.messages.create(
        model=MODEL,
        max_tokens=8000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
    )

    raw_text = "".join(block.text for block in resp.content if block.type == "text")
    flow = _parse_json_response(raw_text)
    flow.setdefault("flow_name", "Main flow")
    flow.setdefault("parse_notes", "")
    flow.setdefault("steps", [])
    return flow


def _parse_json_response(raw_text: str) -> dict:
    """Claude is asked for pure JSON, but strip code fences defensively if present."""
    text = raw_text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass
        raise ValueError(f"Could not parse Claude's diagram response as JSON: {e}\n\nRaw response:\n{raw_text[:2000]}")
