"""
Core QA engine: sends requirements + application name to Claude via the
Anthropic API (server-side key, never exposed to the client browser),
asks for validation + test scenario generation in one structured pass,
and parses the response into the JSON shape report_builder.py expects.
"""
import json
import os
import re
from anthropic import Anthropic

MODEL = os.environ.get("QA_MODEL", "claude-sonnet-4-6")

SYSTEM_PROMPT = (
    "You are an expert QA Analyst and Requirements Traceability specialist. "
    "You validate whether requirements make sense for a named target application, "
    "and generate structured test scenarios for every requirement that is valid. "
    "You always respond with a single JSON object and nothing else - no markdown "
    "fences, no commentary before or after."
)

PROMPT_TEMPLATE = """Application under test: {application}

Requirements document (raw extracted text below):
---
{requirements_text}
---

TASK:
1. Identify each discrete requirement in the document. If the document already
   has reference codes (e.g. REQ-001, FR-1.2), reuse them as req_id. Otherwise
   assign REQ-001, REQ-002, ... in document order.
2. For each requirement, decide if it is valid/testable for "{application}" as
   you understand that platform. Mark valid_for_app true or false. If false,
   explain briefly in "notes" why it doesn't fit (wrong module, capability the
   platform doesn't have, ambiguous/contradictory, etc). Do NOT block the rest
   of the run - keep validating everything, and only generate scenarios for the
   ones marked valid.
3. For every requirement marked valid, write 1-3 test scenarios covering the
   discrete rules in it (happy path plus key negative/edge cases where relevant).
   Assign each scenario a unique tc_id (TC-001, TC-002, ... across the whole
   response, not per requirement). Each scenario needs: tc_id, req_id, title,
   precondition, steps (numbered, joined with \\n), expected_result.

OUTPUT: respond with ONLY this JSON object (no other text):
{{
  "application": "{application}",
  "validation": [
    {{"req_id": "REQ-001", "requirement": "<verbatim or lightly cleaned requirement text>", "valid_for_app": true, "notes": ""}}
  ],
  "test_scenarios": [
    {{"tc_id": "TC-001", "req_id": "REQ-001", "title": "...", "precondition": "...", "steps": "1. ...\\n2. ...", "expected_result": "..."}}
  ]
}}
"""


def run_qa_analysis(application: str, requirements_text: str, api_key: str) -> dict:
    client = Anthropic(api_key=api_key)

    prompt = PROMPT_TEMPLATE.format(
        application=application.strip(),
        requirements_text=requirements_text.strip()[:120000],  # guard against runaway input
    )

    resp = client.messages.create(
        model=MODEL,
        max_tokens=8000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )

    raw_text = "".join(block.text for block in resp.content if block.type == "text")
    return _parse_json_response(raw_text)


def _parse_json_response(raw_text: str) -> dict:
    """Claude is asked for pure JSON, but strip code fences defensively if present."""
    text = raw_text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        # Try to salvage by extracting the outermost {...} block
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass
        raise ValueError(f"Could not parse Claude's response as JSON: {e}\n\nRaw response:\n{raw_text[:2000]}")
