"""
Core QA engine: sends requirements + application name to Claude via the
Anthropic API (server-side key, never exposed to the client browser),
asks for validation + test scenario generation in one structured pass,
and parses the response into the JSON shape report_builder.py expects.
"""
import json
import os
import re

from . import ai_client

# MODEL is kept only for any external reference; call sites now use
# ai_client.get_model_id() so the model ID stays correct whether this is
# running against direct Anthropic or the Bedrock AU profile (see
# ai_client.py for why this indirection exists).
MODEL = os.environ.get("QA_MODEL", "claude-sonnet-4-6")

# Output cap for every structured-JSON call in this file (all five below).
# Was 8000 - too low for a genuinely detailed requirements/test-case document:
# a 2026-09-22 run against a real ServiceNow Incident-form requirements doc
# (7 well-developed sections) truncated Claude's JSON response mid-string
# every single time at ~27,000 characters, well past the true midpoint of
# the intended output, which _parse_json_response cannot recover from (the
# cut happens inside a string value, not just a missing closing brace) -
# see claude/servicenow-max-tokens-truncation-fix-2026-09-22.md for the
# incident, the reproduced traceback (ref=c2ab8f47 and others), and the
# council review that set this value.
#
# Set to 64000 (interim, pending measurement) rather than a scientifically
# derived number: the server logs only capture the first 2000 characters of
# each failed response (see _parse_json_response's error message below), so
# the TRUE output length this document needed was never actually observed -
# only that it exceeded 8000 tokens. 64000 is comfortably within Claude
# Sonnet 4.6's output range and removes the immediate failure, but is still
# a margin-of-safety choice, not a measured one. scripts/measure_qa_tokens.py
# (added alongside this change) makes one real API call against a real
# requirements document with a very high ceiling and reads back
# resp.usage.output_tokens - the actual ground truth for what a given
# document needs - so this constant can be replaced with a properly
# measured value (plus a deliberate safety multiplier) once that's run.
# Raising this alone has no billing impact for any request that already
# completed under the old cap - Anthropic bills actual tokens generated, not
# the max_tokens ceiling itself - it only lets a request that was already
# trying to produce more output finish doing so instead of failing outright
# (and today, a truncated request already paid for the ~8000 output tokens
# it wasted).
QA_MAX_OUTPUT_TOKENS = 64000

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
   precondition, steps (numbered, joined with \\n), expected_result, fixture_role.
3b. If a scenario's steps involve attaching/uploading a file (e.g. "attach a
   medical certificate", "upload a supporting document"), also include
   attachment_filename (a realistic filename matching what the requirement
   describes, e.g. "medical_certificate.pdf") and attachment_type (the file
   extension without a dot, e.g. "pdf", "docx", "png" - pick "pdf" if the
   requirement doesn't imply a specific format). A generic test file with
   this name is generated automatically at execution time - never write
   steps that assume a specific real file already exists on disk. Omit both
   fields entirely for scenarios that don't involve a file attachment.
4. Independence & data safety (important): each scenario must be self-contained
   and independently runnable - never write steps that assume data created by
   another scenario in this same batch or by a previous run (e.g. do not write
   "edit the employee created in TC-001"). If a scenario needs an existing
   record to act on, use fixture_role "requires:<type>" (see below) instead of
   assuming one exists. Where a scenario creates new data (e.g. a new
   employee), its steps should specify a distinguishing/unique value (e.g. a
   name containing "AutoTest_{{{{UNIQUE}}}}" literally, as a token to be
   substituted later) so repeated runs against the same shared environment
   don't collide with data left behind by earlier runs.
5. fixture_role classifies whether a scenario creates or depends on reusable
   test data, so live execution can safely reuse records instead of creating
   new ones every run:
   - "creates:<type>" (e.g. "creates:employee") - this scenario's job is to
     create a new record of that type.
   - "requires:<type>" (e.g. "requires:employee") - this scenario needs an
     EXISTING record of that type to act on (e.g. "update", "edit", "delete",
     "view" scenarios). Its steps must reference that record using the
     literal placeholder {{{{FIXTURE:<type>.<attr>}}}} (e.g.
     {{{{FIXTURE:employee.first_name}}}}) instead of inventing a name/ID
     yourself - this gets substituted with a real, existing record's data at
     execution time.
   - "none" - the default; use this for scenarios that don't create or depend
     on this kind of reusable record (most validation/negative-path scenarios).

OUTPUT: respond with ONLY this JSON object (no other text):
{{
  "application": "{application}",
  "validation": [
    {{"req_id": "REQ-001", "requirement": "<verbatim or lightly cleaned requirement text>", "valid_for_app": true, "notes": ""}}
  ],
  "test_scenarios": [
    {{"tc_id": "TC-001", "req_id": "REQ-001", "title": "...", "precondition": "...", "steps": "1. ...\\n2. ...", "expected_result": "...", "fixture_role": "none"}},
    {{"tc_id": "TC-002", "req_id": "REQ-002", "title": "...", "precondition": "...", "steps": "1. ...\\n2. ...", "expected_result": "...", "fixture_role": "none", "attachment_filename": "medical_certificate.pdf", "attachment_type": "pdf"}}
  ]
}}
(attachment_filename/attachment_type are optional - include them only on a
scenario that actually involves a file upload, as in the TC-002 example above.)
"""


# --- Process Coverage Insights (Beta) - added 2026-09-17, see
# claude/process-gap-analysis-design-consensus-2026-09-16.md and
# claude/process-coverage-insights-final-copy-2026-09-16.md for the full
# design and locked copy this implements. Deliberately additive: when
# process_context is None (the default, and the only path free-trial runs
# ever take - see main.py), the prompt and output shape are byte-for-byte
# what they were before this feature existed. No regression risk for
# existing callers. -----------------------------------------------------
PROCESS_CONTEXT_ADDENDUM = """

PROCESS CONTEXT (optional, provided by the client alongside their requirements - {frame_label}):
{process_body}

ADDITIONAL TASK - Process Coverage Insights (Beta):
6. Treat the process above as a list of real steps the client's actual
   process goes through - it may include steps no requirement mentions at
   all, and that is an expected, useful finding, not an error.
   {steps_instruction}
7. For every test scenario, add a "flow_step_ids" field: the process step_id
   value(s) (from the list above) that scenario actually exercises. Empty
   list if the scenario doesn't relate to a listed step.
8. After generating all scenarios, list every process step_id from above
   that is referenced by NO scenario's flow_step_ids in "uncovered_process_steps"
   (a list of step_id values only, e.g. ["P-003"]). Do not editorialize or
   guess at why a step has no coverage - simply report which step_ids have
   none; a human will review whether that's a real gap. Also include
   "process_steps": the same list of steps from PROCESS CONTEXT above,
   unchanged (echoed back so they can be reviewed/corrected before this
   result is finalized).

Also add "flow_step_ids": [] to every test scenario in the OUTPUT shape below
(empty list when process context doesn't apply to that scenario), and add
top-level "process_steps" and "uncovered_process_steps" as described above.
"""


def _build_process_context_block(process_context: dict) -> str:
    """process_context shape: {"frame": "current"|"target", "steps": [{"step_id","screen_or_stage","description",...}, ...] | None,
    "raw_text": "<free-text description, when no diagram was parsed>" | None}.
    Exactly one of "steps" or "raw_text" is expected to be populated by the
    caller (main.py) - both is harmless (steps taking precedence for the
    "process above" reference), neither should happen (main.py only builds
    this dict when at least one is present)."""
    frame = (process_context.get("frame") or "current").strip().lower()
    frame_label = (
        "this is the client's CURRENT (as-is) process - a step with no test "
        "coverage is a gap in testing how things work today"
        if frame != "target" else
        "this is the client's TARGET (to-be) process - a step with no test "
        "coverage may simply mean that part isn't built/testable yet, not "
        "necessarily a testing gap"
    )
    steps = process_context.get("steps")
    if steps:
        body = json.dumps(steps)
        steps_instruction = (
            "The steps above already have fixed step_id values from a parsed "
            "diagram - reuse them exactly, do not renumber or invent new ones."
        )
    else:
        raw_text = (process_context.get("raw_text") or "").strip()[:4000]
        body = f'(client\'s own description, not yet broken into steps): "{raw_text}"'
        steps_instruction = (
            "Break the description above into a short, ordered list of discrete "
            "steps yourself, assigning step_id values as P-001, P-002, ... in "
            "the order they occur - this is what becomes \"process_steps\" below."
        )
    return PROCESS_CONTEXT_ADDENDUM.format(
        frame_label=frame_label, process_body=body, steps_instruction=steps_instruction,
    )


def run_qa_analysis(application: str, requirements_text: str, api_key: str, max_test_cases: int | None = None,
                     process_context: dict | None = None) -> dict:
    client = ai_client.get_client(api_key)

    prompt = PROMPT_TEMPLATE.format(
        application=application.strip(),
        requirements_text=requirements_text.strip()[:120000],  # guard against runaway input
    )
    if process_context:
        prompt += _build_process_context_block(process_context)
    if max_test_cases is not None:
        # Used for the free trial (see app/trial_signups.py): the word-count
        # guard in main.py is only a proxy for "will this document produce
        # far more than N test cases" - a short but dense requirements list
        # can still slip past it and generate well more than N. This
        # instruction bounds the actual output count requested from the
        # model, and main.py additionally truncates the result as a
        # belt-and-suspenders safety net in case the model overshoots it.
        prompt += (
            f"\n\nIMPORTANT: Generate AT MOST {max_test_cases} test scenarios in total, "
            "not per requirement. If there are more valid requirements than this limit "
            "allows covering individually, prioritize the most critical/highest-risk "
            "requirements first and cover the rest with fewer or combined scenarios "
            f"rather than exceeding {max_test_cases} scenarios."
        )

    resp = client.messages.create(
        model=ai_client.get_model_id(),
        max_tokens=QA_MAX_OUTPUT_TOKENS,
        temperature=0,
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
   title, precondition, steps (numbered, joined with \\n), expected_result,
   fixture_role.
4. Identify any confirmed flow step whose step_id is referenced by NO
   requirement - list these step_id values in "uncovered_flow_steps". This
   surfaces a process step nobody wrote a requirement for.
5. Independence & data safety (important): each scenario must be self-contained
   and independently runnable - never write steps that assume data created by
   another scenario in this same batch or by a previous run (e.g. do not write
   "edit the employee created in TC-001"). If a scenario needs an existing
   record to act on, use fixture_role "requires:<type>" (see below) instead of
   assuming one exists. Where a scenario creates new data (e.g. a new
   employee), its steps should specify a distinguishing/unique value (e.g. a
   name containing "AutoTest_{{{{UNIQUE}}}}" literally, as a token to be
   substituted later) so repeated runs against the same shared environment
   don't collide with data left behind by earlier runs.
6. fixture_role classifies whether a scenario creates or depends on reusable
   test data, so live execution can safely reuse records instead of creating
   new ones every run:
   - "creates:<type>" (e.g. "creates:employee") - this scenario's job is to
     create a new record of that type.
   - "requires:<type>" (e.g. "requires:employee") - this scenario needs an
     EXISTING record of that type to act on (e.g. "update", "edit", "delete",
     "view" scenarios). Its steps must reference that record using the
     literal placeholder {{{{FIXTURE:<type>.<attr>}}}} (e.g.
     {{{{FIXTURE:employee.first_name}}}}) instead of inventing a name/ID
     yourself - this gets substituted with a real, existing record's data at
     execution time.
   - "none" - the default; use this for scenarios that don't create or depend
     on this kind of reusable record (most validation/negative-path scenarios).

OUTPUT: respond with ONLY this JSON object (no other text):
{{
  "application": "{application}",
  "validation": [
    {{"req_id": "REQ-001", "requirement": "<verbatim or lightly cleaned requirement text>", "testable": true, "flow_step_ids": ["FLOW-001"], "notes": ""}}
  ],
  "test_scenarios": [
    {{"tc_id": "TC-001", "req_id": "REQ-001", "flow_step_ids": ["FLOW-001"], "title": "...", "precondition": "...", "steps": "1. ...\\n2. ...", "expected_result": "...", "fixture_role": "none"}}
  ],
  "uncovered_flow_steps": ["FLOW-004"]
}}
"""


def run_qa_analysis_custom(application: str, brief_text: str, flow: dict, requirements_text: str,
                            api_key: str, element_hints: str = "") -> dict:
    """Option B generation pass. `flow` is the human-confirmed dict produced by
    diagram_parser.parse_flow_diagrams and then edited/approved on the
    review-and-confirm screen - never the raw, unconfirmed parse."""
    client = ai_client.get_client(api_key)

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
        model=ai_client.get_model_id(),
        max_tokens=QA_MAX_OUTPUT_TOKENS,
        temperature=0,
        system=CUSTOM_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )

    raw_text = "".join(block.text for block in resp.content if block.type == "text")
    return _parse_json_response(raw_text)


# --------------------------------------------------------------------------
# Bring-your-own test cases (import path) - a client who doesn't want
# generation at all, just execution of test cases they already wrote.
#
# IMPORTANT distinction from everything above: this is STRUCTURING, not
# GENERATION. Claude is explicitly instructed to extract only what's
# already there, never invent additional steps, cases, or content, and to
# flag anything ambiguous rather than guess it away silently - because the
# output goes straight to a human review-and-confirm screen (same
# non-negotiable checkpoint as Option B's diagram parsing) before it's ever
# executable, per the council review that scoped this feature.
# --------------------------------------------------------------------------

IMPORT_SYSTEM_PROMPT = (
    "You extract already-written test cases from a document into a structured "
    "format. You do NOT invent, add, or improve test cases - only structure "
    "what is genuinely already present in the text. If a case is missing a "
    "field (e.g. no explicit expected result), leave it blank rather than "
    "inventing one, and note it in that case's 'notes' field. You always "
    "respond with a single JSON object and nothing else - no markdown fences, "
    "no commentary before or after."
)

IMPORT_PROMPT_TEMPLATE = """The following text was extracted from a document a client says already
contains their test cases for "{application}". It may be a table, a list, or
prose describing each test - it was NOT written by you, don't judge its
formatting, just extract what's genuinely there.

Document text:
---
{raw_text}
---

TASK: identify every distinct test case in the text and extract it. For each,
capture whatever the document actually provides:
- tc_id: reuse an existing ID if the document has one (e.g. TC-01, "Test 3");
  otherwise assign TC-001, TC-002, ... in document order.
- title: a short name/summary for the case.
- precondition: setup/starting state, if stated (empty string if not).
- steps: the step-by-step actions, numbered, joined with \\n.
- expected_result: what should happen, if stated (empty string if not).
- notes: flag anything ambiguous, incomplete, or that you're unsure how to
  split (e.g. "expected result not explicitly stated - inferred from context
  is NOT included, left blank"). Empty string if there's nothing to flag.

If you cannot confidently identify any distinct test cases at all (e.g. the
document is clearly not test cases), return an empty "test_cases" list and
explain why in "parse_notes".

OUTPUT: respond with ONLY this JSON object (no other text):
{{
  "parse_notes": "",
  "test_cases": [
    {{"tc_id": "TC-001", "title": "...", "precondition": "...", "steps": "1. ...\\n2. ...", "expected_result": "...", "notes": ""}}
  ]
}}
"""


def structure_existing_test_cases(application: str, raw_text: str, api_key: str) -> dict:
    """Used only as a fallback when a tabular file (.xlsx/.csv) doesn't have
    recognizable column headers, and always for prose formats (.docx/.txt/
    .pdf) - see main.py's import-tests route for the deterministic tabular
    path this sits behind."""
    client = ai_client.get_client(api_key)

    prompt = IMPORT_PROMPT_TEMPLATE.format(
        application=application.strip(),
        raw_text=raw_text.strip()[:100000],
    )

    resp = client.messages.create(
        model=ai_client.get_model_id(),
        max_tokens=QA_MAX_OUTPUT_TOKENS,
        temperature=0,
        system=IMPORT_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = "".join(block.text for block in resp.content if block.type == "text")
    return _parse_json_response(raw)


TRACEABILITY_SYSTEM_PROMPT = (
    "You match client-supplied test cases against a requirements document to build a "
    "best-effort traceability matrix. This is inherently heuristic, not deterministic - "
    "the test cases were never written with requirement IDs attached, so you are inferring "
    "a likely match, not verifying a ground truth. Be conservative: mark a requirement as "
    "covered only if a test case plausibly, substantively addresses it. When genuinely "
    "unsure, mark it uncovered rather than guessing a match - a false 'covered' is worse "
    "than an honest 'not sure'. Never invent test cases or requirements that aren't present."
)

TRACEABILITY_PROMPT_TEMPLATE = """REQUIREMENTS DOCUMENT for "{application}":
{requirements_text}

CLIENT-SUPPLIED TEST CASES (already written, do not modify or improve them):
{test_cases_json}

For each requirement, identify which of the above test case IDs (if any) plausibly cover
it. A requirement can be covered by zero, one, or multiple test cases. A test case can
cover more than one requirement.

Respond with ONLY this JSON object (no other text):
{{
  "validation": [
    {{"req_id": "REQ-001", "requirement": "<verbatim or lightly cleaned requirement text>", "matched_tc_ids": ["TC-001"], "covered": true}}
  ]
}}
"""


def match_requirements_to_test_cases(application: str, requirements_text: str, test_cases: list, api_key: str) -> dict:
    """Best-effort traceability matrix for tier 3 (bring-your-own test cases):
    matches a client's already-written test cases against their requirements
    document. Unlike run_qa_analysis's req_id linkage (which the model assigns
    itself as it writes each case, so it's deterministic by construction),
    this is inferring a match after the fact for cases we never wrote - it is
    a genuinely heuristic best-effort pass, not an authoritative result, and
    must always be presented to the client with that caveat (see
    review_import.html)."""
    client = ai_client.get_client(api_key)

    tc_summary = [
        {"tc_id": tc.get("tc_id", ""), "title": tc.get("title", ""), "steps": tc.get("steps", "")[:500]}
        for tc in test_cases
    ]
    prompt = TRACEABILITY_PROMPT_TEMPLATE.format(
        application=application.strip(),
        requirements_text=requirements_text.strip()[:100000],
        test_cases_json=json.dumps(tc_summary),
    )

    resp = client.messages.create(
        model=ai_client.get_model_id(),
        max_tokens=QA_MAX_OUTPUT_TOKENS,
        temperature=0,
        system=TRACEABILITY_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = "".join(block.text for block in resp.content if block.type == "text")
    return _parse_json_response(raw)


# --------------------------------------------------------------------------- Impact analysis (requirement version diff)
IMPACT_SYSTEM_PROMPT = (
    "You analyze the difference between two versions of a requirements document for the "
    "same application, and the effect that difference has on an existing set of test "
    "cases written against the OLDER version. Be conservative and literal, the same way "
    "a careful human QA lead reviewing a diff would be:\n"
    "- Ignore purely cosmetic changes (rewording, reordering, typo fixes, formatting) that "
    "do not change what the system must do - do not report these as changes.\n"
    "- Only report a requirement as MODIFIED when its actual behaviour/scope changed.\n"
    "- Only mark an existing test case as impacted when the requirement it was written "
    "against was modified or removed - never guess an impact on a test case whose "
    "requirement is unchanged, even if it happens to sit near a change in the document.\n"
    "- When genuinely unsure whether a change is substantive, say so in the notes rather "
    "than silently deciding either way - a false 'no impact' is worse than an honest "
    "'review this one manually'.\n"
    "- Never invent requirements, requirement IDs, or test cases that are not present in "
    "the input."
)

IMPACT_PROMPT_TEMPLATE = """APPLICATION: {application}

OLDER REQUIREMENTS VERSION:
{old_text}

NEWER REQUIREMENTS VERSION:
{new_text}

LINE-LEVEL DIFF (for reference only - reason about substance, not line noise):
{unified_diff}

EXISTING TEST CASES written against the OLDER version (do not modify or rewrite these):
{old_test_cases_json}

Identify, for each requirement present in either version:
- "added": present only in the newer version
- "removed": present only in the older version
- "modified": present in both, but its behaviour/scope substantively changed
- "unchanged": present in both with no substantive change (omit these from the output -
  only list requirements that actually changed in some way)

Then identify which of the existing test cases are impacted (their req_id was modified or
removed) and give a short reason and a recommendation for each.

Respond with ONLY this JSON object (no other text):
{{
  "requirement_changes": [
    {{"req_id": "REQ-003", "change_type": "modified", "summary": "<what changed, one sentence>"}}
  ],
  "impacted_test_cases": [
    {{"tc_id": "TC-005", "req_id": "REQ-003", "reason": "<why this test case is now in question>", "recommendation": "re-review before next run"}}
  ],
  "new_gaps": ["REQ-010 is new and has no existing test case coverage"]
}}
"""


def line_diff(old_text: str, new_text: str, context_lines: int = 2) -> str:
    """Cheap, deterministic, zero-AI-cost first pass: a standard unified
    diff between two requirement versions. Computed unconditionally (it's
    nearly free) and handed to the AI as reference context alongside the
    full text of both versions - the AI still reasons over the actual
    requirement wording, this just points it at where the changes are
    rather than making it re-discover them by comparing two full documents
    from scratch."""
    import difflib

    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    diff = difflib.unified_diff(
        old_lines, new_lines,
        fromfile="previous version", tofile="new version",
        n=context_lines,
    )
    # Capped - a very large diff would blow the prompt budget for no benefit;
    # the AI still has the full old_text/new_text above to reason over even
    # if the diff itself is truncated.
    text = "".join(diff)
    return text[:20000]


def analyze_requirements_impact(
    application: str,
    old_text: str,
    new_text: str,
    old_test_cases: list,
    api_key: str,
) -> dict:
    """Compares two requirement versions for the same client/application and
    flags which of the OLDER version's test cases are now in question. This
    never re-generates or modifies test cases itself - it is a review aid,
    the same "heuristic, not deterministic, be conservative" posture as
    match_requirements_to_test_cases above, for the same reason: it is
    inferring semantic impact after the fact, not verifying a ground truth."""
    client = ai_client.get_client(api_key)

    diff_text = line_diff(old_text, new_text)
    tc_summary = [
        {"tc_id": tc.get("tc_id", ""), "req_id": tc.get("req_id", ""), "title": tc.get("title", "")}
        for tc in old_test_cases
    ]
    prompt = IMPACT_PROMPT_TEMPLATE.format(
        application=application.strip(),
        old_text=old_text.strip()[:60000],
        new_text=new_text.strip()[:60000],
        unified_diff=diff_text or "(no line-level differences detected)",
        old_test_cases_json=json.dumps(tc_summary),
    )

    resp = client.messages.create(
        model=ai_client.get_model_id(),
        max_tokens=QA_MAX_OUTPUT_TOKENS,
        temperature=0,
        system=IMPACT_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = "".join(block.text for block in resp.content if block.type == "text")
    result = _parse_json_response(raw)
    # Defensive shape guarantee - callers (main.py, report rendering) should
    # never need a try/except around missing keys just because the model's
    # JSON happened to omit an empty list.
    result.setdefault("requirement_changes", [])
    result.setdefault("impacted_test_cases", [])
    result.setdefault("new_gaps", [])
    return result
