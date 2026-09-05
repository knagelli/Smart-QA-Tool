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
 
 
# --------------------------------------------------------------------------
# Option B - Custom Application Mode
#
# Unlike run_qa_analysis() above (which judges platform fit against a named
# product Claude has general knowledge of), a custom/proprietary app has no
# such prior knowledge to lean on. Here validation becomes a consistency and
# testability check against an ALREADY-APPROVED requirements document, and
# generation works from three inputs (brief + confirmed process flow +
# requirements) instead of one, tracing three ways: requirement <-> flow
# step <-> test case. See claude/council-review-custom-apps.md for the full
# design rationale. Option A above is left completely unchanged.
# --------------------------------------------------------------------------
 
CUSTOM_SYSTEM_PROMPT = (
    "You are an expert QA Analyst and Requirements Traceability specialist "
    "working from an ALREADY-APPROVED requirements document for a custom or "
    "proprietary application. You do not re-judge whether an approved "
    "requirement is a good idea - that decision has already been made. You "
    "check consistency and testability, generate structured test scenarios, "
    "and trace three ways: requirement <-> process-flow step <-> test case. "
    "You always respond with a single JSON object and nothing else - no "
    "markdown fences, no commentary before or after."
)
 
CUSTOM_PROMPT_TEMPLATE = """Application under test (custom/proprietary): {application}
 
Project brief (context only):
---
{brief_text}
---
 
Confirmed future-state process flow (already reviewed and confirmed by the client - treat step_id values as fixed identifiers, do not invent new ones):
---
{flow_json}
---
 
Approved requirements document (raw extracted text below - already signed off; do not re-judge whether these requirements are appropriate):
---
{requirements_text}
---
{hints_block}
TASK:
1. Identify each discrete requirement in the requirements document. Reuse
   existing reference codes (REQ-001, FR-1.2, etc.) if present, else assign
   REQ-001, REQ-002, ... in document order.
2. For each requirement, run a CONSISTENCY/TESTABILITY check only (never
   re-judge whether an approved requirement is appropriate):
   - testable: true/false - is it clear and specific enough to test as written?
   - flow_step_ids: list of step_id values (from the confirmed flow above)
     that this requirement relates to. Empty list if none match - that is a
     real coverage gap, surface it, do not force a match.
   - notes: brief explanation if testable is false or flow_step_ids is empty
     (ambiguous wording, contradiction, no matching flow step, etc.), else "".
3. For every requirement marked testable, write 1-3 test scenarios (happy
   path plus key negative/edge cases where relevant). Assign each scenario a
   unique tc_id (TC-001, TC-002, ... across the whole response). Each
   scenario needs: tc_id, req_id, flow_step_ids (copy from its requirement),
   title, precondition, steps (numbered, joined with \\n), expected_result.
4. Identify any confirmed flow step whose step_id is referenced by NO
   requirement - list these step_id values in "uncovered_flow_steps". This
   surfaces a process step nobody wrote a requirement for.
 
OUTPUT: respond with ONLY this JSON object (no other text):
{{
  "application": "{application}",
  "validation": [
    {{"req_id": "REQ-001", "requirement": "<verbatim or lightly cleaned requirement text>", "testable": true, "flow_step_ids": ["FLOW-001"], "notes": ""}}
  ],
  "test_scenarios": [
    {{"tc_id": "TC-001", "req_id": "REQ-001", "flow_step_ids": ["FLOW-001"], "title": "...", "precondition": "...", "steps": "1. ...\\n2. ...", "expected_result": "..."}}
  ],
  "uncovered_flow_steps": ["FLOW-004"]
}}
"""
 
 
def run_qa_analysis_custom(application: str, brief_text: str, flow: dict, requirements_text: str,
                            api_key: str, element_hints: str = "") -> dict:
    """Option B generation pass. `flow` is the human-confirmed dict produced by
    diagram_parser.parse_flow_diagrams and then edited/approved on the
    review-and-confirm screen - never the raw, unconfirmed parse."""
    client = Anthropic(api_key=api_key)
 
    # Context budget: three documents (brief + flow + requirements) can
    # exceed the single-document cap Option A uses, so each gets its own
    # smaller guard instead of one blowing the whole budget silently.
    hints_block = ""
    if element_hints.strip():
        hints_block = (
            "\nKnown element selectors/test-IDs for critical screens "
            "(optional, improves execution reliability only - not required):\n"
            f"{element_hints.strip()[:4000]}\n"
        )
 
    prompt = CUSTOM_PROMPT_TEMPLATE.format(
        application=application.strip(),
        brief_text=brief_text.strip()[:40000],
        flow_json=json.dumps(flow, indent=2)[:40000],
        requirements_text=requirements_text.strip()[:80000],
        hints_block=hints_block,
    )
 
    resp = client.messages.create(
        model=MODEL,
        max_tokens=8000,
        system=CUSTOM_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
 
    raw_text = "".join(block.text for block in resp.content if block.type == "text")
    return _parse_json_response(raw_text)
 


