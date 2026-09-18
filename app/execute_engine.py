"""
Req2QA - Live Test Execution Engine (Phase 2)

Runs an already-generated test case live against a client's SIT/UAT/sandbox
environment: signs in, drives the flow, and reports PASS/FAIL/BLOCKED with
screenshot evidence.

DESIGN PRINCIPLE - generic, not app-specific: this engine has no knowledge
of Humanforce, Salesforce, SuccessFactors, or any other named platform. It
reads the page's interactive elements by role/label (a DOM/accessibility-
tree snapshot) and drives them through a small, fixed toolset. This is what
lets the same engine run against any client's web app - "Humanforce
support" means validating this engine against Humanforce as the first real
target, not writing Humanforce-specific code. Anything genuinely specific
to a platform (its login shape, known quirky selectors) belongs in the
per-run inputs (role, element hints) supplied by the caller, never here.

CREDENTIAL HANDLING (hard constraint, do not relax):
- The client's test-user username/password exist only in this function's
  local memory for the duration of one run.
- They are substituted into the page by the SERVER, via `fill_login` -
  Claude only ever says which field (by ref) to put the username/password
  into; the actual string values are never sent to Claude as text, never
  logged, never written to disk, never included in any report.
- Nothing about them is retained after the run ends.

SANDBOX ONLY: this is designed to run against SIT/UAT/sandbox environments
supplied by the client, never production. The caller is responsible for
that being true; this module does not and cannot verify it.
"""
import base64
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Optional

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from . import ai_client
from .run_logger import hash_bytes

logger = logging.getLogger("req2qa.execute")

MODEL = os.environ.get("QA_MODEL", "claude-sonnet-4-6")
# Raised 30 -> 45 -> 60 (2026-09-14) - Phase B moved execution off the
# synchronous request/response cycle, so a longer-running test case no
# longer risks a platform request timeout the way it would have before.
# Still a hard cap, just a more realistic one for legitimate multi-page,
# multi-step flows that need more than a handful of actions even when
# nothing is going wrong. Safe to keep raising - the STALL_LIMIT check
# below (not this number) is what actually protects against a stuck test
# case burning through the budget uselessly, so a higher cap mainly means
# more headroom for real, correctly-progressing flows, not more time spent
# on cases that were never going to finish anyway.
MAX_AGENT_STEPS = 60
# If this many consecutive get_snapshot calls come back with the same URL
# and the same set of visible element labels - i.e. nothing the agent did
# in between actually changed the page - stop early with a clear BLOCKED
# verdict instead of grinding on to MAX_AGENT_STEPS. This is what actually
# protects clients from the "sits there until the step limit" experience;
# raising the cap alone would only make a genuine stall slower to report.
STALL_LIMIT = 3
NAV_TIMEOUT_MS = 20000
ACTION_TIMEOUT_MS = 10000

# Any interactive element whose visible label, name, id, or placeholder
# matches one of these (case-insensitive substring) gets masked (blacked
# out) in every screenshot it appears in - not only the screenshot taken
# because of it. Deliberately broad and platform-agnostic: this list is
# about sensitive *categories* of data (pay, identity, destructive
# actions), not about any specific app's field names.
CRITICAL_FIELD_KEYWORDS = [
    "salary", "wage", "pay rate", "payrate", "bank", "account number", "bsb",
    "tfn", "tax file", "super", "password", "ssn", "social security",
    "approve", "reject", "terminate", "termination", "payment", "payroll",
    "credit card", "routing number", "medicare", "passport", "license",
]

_MASK_CSS = """
.__req2qa_masked__ {
  background: #111 !important;
  color: transparent !important;
  border-radius: 3px;
}
"""


class ExecutionError(Exception):
    pass


def _snapshot_elements(page) -> list:
    """Build a DOM/accessibility-tree-ish snapshot: every interactive
    element with a stable ref, its role, and its best-available label.
    Claude sees this list (never a screenshot) to decide what to do next -
    cheaper and far more robust than pixel-based computer-use for the kind
    of dense, role/label-driven forms enterprise web apps are built from."""
    # Includes 'label' and [role="switch"] alongside the usual form controls -
    # many enterprise UI kits (OrangeHRM's toggle switches included) style a
    # checkbox input as visually-hidden/zero-size and put the actual visible,
    # clickable chrome on a wrapping <label> or a role-less <span> instead.
    # Without 'label' here, that toggle is entirely invisible to the agent
    # (the real <input> fails the visibility check, and a bare <span> isn't
    # matched at all) - it can see and fill every other field on the form but
    # has no way to ever act on the one control that reveals a later step
    # (e.g. OrangeHRM's "Create Login Details?" switch on Add Employee),
    # which was observed to loop until the step-limit BLOCKED cutoff.
    # Clicking a <label> (for= or wrapping) toggles its associated control
    # exactly like clicking the control itself, so this needs no new tool.
    elements = page.evaluate(
        """
        () => {
            const sel = 'input, textarea, select, button, a[href], label, [role="button"], [role="link"], [role="tab"], [role="checkbox"], [role="radio"], [role="switch"]';
            const nodes = Array.from(document.querySelectorAll(sel));
            return nodes.slice(0, 200).map((el, i) => {
                const rect = el.getBoundingClientRect();
                const visible = rect.width > 0 && rect.height > 0 &&
                    getComputedStyle(el).visibility !== 'hidden' &&
                    getComputedStyle(el).display !== 'none';
                const label = (
                    el.getAttribute('aria-label') ||
                    el.getAttribute('placeholder') ||
                    el.getAttribute('name') ||
                    el.innerText ||
                    el.value ||
                    el.id || ''
                ).trim().slice(0, 80);
                return {
                    ref: i,
                    tag: el.tagName.toLowerCase(),
                    type: el.getAttribute('type') || '',
                    role: el.getAttribute('role') || '',
                    label: label,
                    visible: visible,
                };
            }).filter(e => e.visible && e.label);
        }
        """
    )
    return elements


def _snapshot_fingerprint(page, elements: list) -> tuple:
    """A cheap signature of 'what the agent can currently see and do',
    used only to detect a stall (the page genuinely not changing across
    repeated snapshots) - not for anything functional."""
    return (page.url, tuple((e["tag"], e["role"], e["label"]) for e in elements))


def _element_locator(page, ref: int):
    sel = 'input, textarea, select, button, a[href], label, [role="button"], [role="link"], [role="tab"], [role="checkbox"], [role="radio"], [role="switch"]'
    all_matches = page.locator(sel)
    return all_matches.nth(ref)


def _is_critical_label(label: str) -> bool:
    low = (label or "").lower()
    return any(k in low for k in CRITICAL_FIELD_KEYWORDS)


def _mask_critical_fields(page):
    """Inject a class onto any currently-visible critical-category element
    so the next screenshot renders it as an opaque block, then return a
    cleanup function to remove that class again (so normal page behaviour
    for the agent's own next snapshot is unaffected)."""
    page.add_style_tag(content=_MASK_CSS)
    marked_count = page.evaluate(
        """
        (keywords) => {
            const sel = 'input, textarea, select, [role="textbox"]';
            const nodes = Array.from(document.querySelectorAll(sel));
            let n = 0;
            for (const el of nodes) {
                const label = (
                    (el.getAttribute('aria-label') || '') + ' ' +
                    (el.getAttribute('placeholder') || '') + ' ' +
                    (el.getAttribute('name') || '') + ' ' +
                    (el.id || '')
                ).toLowerCase();
                if (keywords.some(k => label.includes(k))) {
                    el.classList.add('__req2qa_masked__');
                    n++;
                }
            }
            return n;
        }
        """,
        CRITICAL_FIELD_KEYWORDS,
    )
    return marked_count


def _unmask(page):
    page.evaluate(
        """
        () => {
            document.querySelectorAll('.__req2qa_masked__').forEach(el => el.classList.remove('__req2qa_masked__'));
        }
        """
    )


def _capture_screenshot(page, shots_dir: Path, label: str, rl=None, step_num: Optional[int] = None) -> str:
    _mask_critical_fields(page)
    fname = f"{len(list(shots_dir.glob('*.png'))):03d}_{re.sub(r'[^a-zA-Z0-9_-]', '_', label)[:40]}.png"
    path = shots_dir / fname
    try:
        page.screenshot(path=str(path), timeout=ACTION_TIMEOUT_MS)
    finally:
        _unmask(page)
    # Log only the filename + a SHA-256 hash of the image, never the image
    # itself - lets a client-supplied copy be checked for authenticity later
    # (see run_logger.find_screenshot_hash_matches) without retaining the
    # image beyond the existing 7-day report/screenshot lifecycle.
    if rl is not None:
        try:
            rl.event("screenshot", {
                "step": step_num,
                "label": label,
                "filename": fname,
                "sha256": hash_bytes(path.read_bytes()),
            })
        except Exception:
            pass
    return fname


_ATTACHMENT_NOTE = "Test attachment generated by Req2QA - not a real document."


def _generate_attachment_file(dest_path: Path, file_type: str) -> None:
    """Create a small, clearly-synthetic placeholder file at dest_path for a
    test case whose steps involve attaching/uploading a file (see the
    optional attachment_filename/attachment_type fields qa_engine.py can put
    on a scenario). Deliberately generic content - this exists to test that
    the *system* correctly accepts/stores/displays an attached file, not to
    validate anything about the file's content, so it never invents
    realistic-looking personal, medical, or financial specifics. Falls back
    to a plain-text file for any type it doesn't specifically handle, so an
    unrecognized attachment_type can never itself block a run."""
    file_type = (file_type or "pdf").lower().lstrip(".")
    try:
        if file_type in ("doc", "docx"):
            from docx import Document as DocxDocument
            d = DocxDocument()
            d.add_paragraph(_ATTACHMENT_NOTE)
            d.save(str(dest_path))
        elif file_type in ("xlsx", "xlsm", "xls"):
            from openpyxl import Workbook
            wb = Workbook()
            wb.active["A1"] = _ATTACHMENT_NOTE
            wb.save(str(dest_path))
        elif file_type in ("csv", "txt"):
            dest_path.write_text(_ATTACHMENT_NOTE, encoding="utf-8")
        elif file_type in ("png", "jpg", "jpeg"):
            import fitz
            doc = fitz.open()
            page = doc.new_page(width=400, height=200)
            page.insert_text((20, 100), _ATTACHMENT_NOTE, fontsize=11)
            pix = page.get_pixmap()
            pix.save(str(dest_path))
            doc.close()
        else:
            import fitz
            doc = fitz.open()
            page = doc.new_page()
            page.insert_text((72, 72), _ATTACHMENT_NOTE, fontsize=12)
            doc.save(str(dest_path))
            doc.close()
    except Exception:
        # Last-resort fallback: whatever went wrong above (an unexpected
        # file_type, a library hiccup), a plain-text file with the same
        # name still lets the upload step proceed rather than turning a
        # file-generation edge case into a BLOCKED test run.
        dest_path.write_text(_ATTACHMENT_NOTE, encoding="utf-8")


TOOLS = [
    {
        "name": "get_snapshot",
        "description": "Get the current page's interactive elements (buttons, fields, links) with a ref number for each, plus the page title and URL. Call this whenever you need to see what's on screen, including right after any navigation or action.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "fill_login",
        "description": "Fill the username and password fields using the test credentials provided for this run. You do NOT provide the actual username/password - just say which element refs are the username field and password field; the real values are filled in securely on the server side.",
        "input_schema": {
            "type": "object",
            "properties": {
                "username_ref": {"type": "integer", "description": "ref of the username/email field"},
                "password_ref": {"type": "integer", "description": "ref of the password field"},
            },
            "required": ["username_ref", "password_ref"],
        },
    },
    {
        "name": "click",
        "description": "Click the element with the given ref.",
        "input_schema": {"type": "object", "properties": {"ref": {"type": "integer"}}, "required": ["ref"]},
    },
    {
        "name": "type_text",
        "description": "Type text into the element with the given ref (clears existing content first). Do not use this for the username/password fields - use fill_login for those.",
        "input_schema": {
            "type": "object",
            "properties": {"ref": {"type": "integer"}, "text": {"type": "string"}},
            "required": ["ref", "text"],
        },
    },
    {
        "name": "select_option",
        "description": "Select an option from a <select> dropdown with the given ref, by its visible label or value.",
        "input_schema": {
            "type": "object",
            "properties": {"ref": {"type": "integer"}, "value": {"type": "string"}},
            "required": ["ref", "value"],
        },
    },
    {
        "name": "wait_for_text",
        "description": "Wait (up to a few seconds) for the given text to appear anywhere on the page. Use this after an action that should produce a confirmation message.",
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}, "timeout_ms": {"type": "integer"}},
            "required": ["text"],
        },
    },
    {
        "name": "upload_file",
        "description": "Attach a file to a file-upload control (e.g. an 'Attach File', 'Choose File', or 'Upload' button/input) with the given ref. A suitable test file is generated and attached automatically - you do not choose or provide a filename, and you never need to interact with any native OS file-picker dialog yourself; this tool handles that.",
        "input_schema": {"type": "object", "properties": {"ref": {"type": "integer"}}, "required": ["ref"]},
    },
    {
        "name": "finish_test",
        "description": "End the test case with a final verdict. Call this once you have either confirmed the expected result, found it did not occur, or hit something that blocks further progress (e.g. an SSO redirect, an unexpected error page).",
        "input_schema": {
            "type": "object",
            "properties": {
                "verdict": {"type": "string", "enum": ["PASS", "FAIL", "BLOCKED"]},
                "notes": {"type": "string", "description": "Plain-language explanation of the outcome, for a non-technical reviewer."},
                "created_entity": {
                    "type": "object",
                    "description": "Only when this test case's job was to create a new reusable record (e.g. a new employee): the key identifying field values you actually entered/saved (e.g. first_name, last_name, employee_id), so a later test case can reuse this exact record. Omit entirely if this test case did not create a new reusable record.",
                },
            },
            "required": ["verdict", "notes"],
        },
        # Prompt caching: Anthropic caches everything up to and including the
        # block carrying cache_control, so putting it on the LAST tool here
        # caches the entire (fixed, ~7-tool) tools list in one breakpoint.
        # This list never changes within a run, so every step after the
        # first for a given cache window reads it at ~10% of input-token
        # price instead of paying full price every single step.
        "cache_control": {"type": "ephemeral"},
    },
]


def _system_prompt(application: str, role_label: str, module: str, test_case: dict, fixture_role: str = "") -> str:
    fixture_instruction = ""
    if fixture_role.startswith("creates:"):
        ftype = fixture_role.split(":", 1)[1]
        fixture_instruction = (
            f"\nThis test case's job is to create a new, reusable {ftype} record. If you succeed, call "
            f"finish_test with verdict PASS AND include created_entity: a JSON object with the key "
            f"identifying field values you actually entered and saved (e.g. first name, last name, any "
            f"ID the system generated or displayed), using the exact values you entered - a later test "
            f"case may reuse this record and needs these values to be accurate.\n"
        )
    return f"""You are driving a real web browser to execute one QA test case against a live test environment, using only the tools provided. You do not see a screenshot - you see a structured snapshot of the page's interactive elements (get_snapshot). Work step by step: get a snapshot, decide the next single action, act, then get a fresh snapshot before deciding again (the page may have changed).

Application under test: {application}
Test-user role for this run: {role_label}
Module/area: {module}

Test case to execute:
Title: {test_case.get('title', '')}
Precondition: {test_case.get('precondition', '')}
Steps: {test_case.get('steps', '')}
Expected result: {test_case.get('expected_result', '')}

Rules:
- You are already on the application's login page. Start with get_snapshot to see the login form, then use fill_login for the username/password fields (never type_text for those) followed by clicking the sign-in button.
- If you land on a screen that looks like a single sign-on redirect, an MFA/one-time-code prompt, or anything else you cannot complete with the tools available, call finish_test with verdict BLOCKED and explain what you saw - do not guess or force through it.
- Carry out the test steps exactly as written against the real UI. If the UI doesn't match what the steps describe, use your judgement to find the equivalent control, but call finish_test with FAIL (not BLOCKED) if the expected result genuinely does not occur.
- Call finish_test as soon as you have a clear verdict. Do not keep exploring after that.
- You have a limited number of actions for this run - be efficient, don't repeat get_snapshot without having taken an action in between unless the page just changed.
- After submitting a form, always take a fresh snapshot and check for an inline validation message (e.g. "should not exceed N characters", "already exists", "required") before deciding what to do next. If you see one, adapt the value you enter to satisfy it (e.g. shorten it, change it) - do not resubmit the exact same value again. If the same action fails validation twice in a row even after you've adapted the value, stop retrying it - call finish_test with FAIL or BLOCKED and quote the validation message in your notes, rather than repeating it for the rest of your available actions.
- A toggle/switch control (e.g. "Create Login Details?", "Enabled") is often a checkbox styled to look like a switch. If you don't see an element that looks directly clickable for it, look for a label with that same wording in the snapshot and click that instead - clicking a field's label toggles it exactly like clicking the control itself. If you still can't find any way to change it after one such attempt, don't keep retrying the same snapshot - call finish_test with BLOCKED and say which control you couldn't operate.
- If a step calls for attaching/uploading a file (e.g. "attach a supporting document", "upload a certificate"), use the upload_file tool on the ref of the attach/choose-file/upload control - do not try to click through to a native OS file dialog, and do not call finish_test with BLOCKED for a file-upload step; upload_file handles it.
{fixture_instruction}"""


def execute_test_case(
    *,
    application: str,
    role_label: str,
    module: str,
    test_case: dict,
    env_url: str,
    username: str,
    password: str,
    api_key: str,
    element_hints: str = "",
    fixture_role: str = "",
    shots_dir: Path,
    rl=None,
    on_step=None,
) -> dict:
    """Runs one test case against env_url using the given (sandbox-only,
    generic test-user) credentials. Returns a dict: verdict, notes,
    step_log (list of plain-language step descriptions - no credentials,
    ever), screenshots (list of filenames already saved under shots_dir),
    created_entity (dict or None - only set when fixture_role is
    "creates:<type>" and the agent reported one via finish_test; see
    app/fixtures.py for how the caller turns this into a reusable fixture).

    `test_case`'s steps/precondition are expected to already have any
    {{FIXTURE:...}} placeholders substituted by the caller before this is
    called - this function has no knowledge of the fixture registry itself.

    Raises ExecutionError on unrecoverable setup failures (bad URL, browser
    launch failure, etc.) - the caller is expected to catch this and show
    a generic error, per the app's existing error-handling convention.

    on_step, if given, is called after every agent step as
    on_step(step_num, MAX_AGENT_STEPS) - purely for the caller to publish
    live progress (see exec_status.py); never raises, and any exception it
    raises is swallowed so a status-tracking bug can never break a live run.
    """
    shots_dir.mkdir(parents=True, exist_ok=True)
    client = ai_client.get_client(api_key)
    step_log = []
    screenshots = []
    created_entity = None

    system_prompt = _system_prompt(application, role_label, module, test_case, fixture_role=fixture_role)
    if element_hints.strip():
        system_prompt += f"\nKnown element hints for this application (selectors/test-IDs your developers supplied - use as a starting point, not gospel): {element_hints.strip()}\n"

    messages = [{"role": "user", "content": "Begin the test case. Start by taking a snapshot of the login page."}]

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            page = browser.new_page()
            page.set_default_timeout(ACTION_TIMEOUT_MS)
            try:
                page.goto(env_url, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
            except PlaywrightTimeoutError as e:
                browser.close()
                raise ExecutionError(f"Could not reach the environment URL in time.") from e

            screenshots.append(_capture_screenshot(page, shots_dir, "start", rl=rl, step_num=0))
            verdict, notes = None, ""
            last_fingerprint = None
            stall_count = 0
            # Lazily created on the first upload_file call and reused for
            # any further ones in this same test case - one generated file
            # per run is all a single scenario needs.
            attachment_path: list = [None]

            for step_num in range(MAX_AGENT_STEPS):
                response = client.messages.create(
                    model=ai_client.get_model_id(),
                    max_tokens=1024,
                    # cache_control on the system block: the per-application
                    # system prompt is identical across every step of a test
                    # case (and often across test cases for the same app), so
                    # caching it here plus the tools list above means only
                    # the growing conversation history is billed at full
                    # input price - the static prefix is billed once per
                    # 5-minute cache window and read back at ~10% cost after.
                    system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
                    tools=TOOLS,
                    messages=messages,
                )
                messages.append({"role": "assistant", "content": response.content})

                tool_uses = [b for b in response.content if b.type == "tool_use"]
                if not tool_uses:
                    # Model produced only text (no tool call) - nudge it back on track.
                    messages.append({"role": "user", "content": "Please continue by calling one of the available tools."})
                    continue

                tool_results = []
                done = False
                for tu in tool_uses:
                    name, inp = tu.name, tu.input
                    try:
                        if name == "get_snapshot":
                            elements = _snapshot_elements(page)
                            fingerprint = _snapshot_fingerprint(page, elements)
                            if fingerprint == last_fingerprint:
                                stall_count += 1
                            else:
                                stall_count = 0
                            last_fingerprint = fingerprint
                            result_payload = {
                                "url": page.url, "title": page.title(), "elements": elements,
                                "steps_remaining": MAX_AGENT_STEPS - step_num - 1,
                            }
                            if stall_count > 0:
                                # Told to the agent, not just logged - a stall it isn't
                                # aware of just repeats; naming it here is what lets the
                                # "look for a label instead" system-prompt rule actually
                                # kick in before the hard STALL_LIMIT cutoff below.
                                result_payload["warning"] = (
                                    "This looks identical to the page you just saw - your last action(s) "
                                    "didn't change anything. Try a different element (e.g. a label for the "
                                    "same control) rather than repeating the same action."
                                )
                            step_log.append(f"Looked at the page ({page.url})")
                            if rl is not None:
                                rl.event("step", {"step": step_num, "action": "get_snapshot", "target": page.url, "result": "ok", "stall_count": stall_count})
                        elif name == "fill_login":
                            loc_u = _element_locator(page, inp["username_ref"])
                            loc_p = _element_locator(page, inp["password_ref"])
                            loc_u.fill(username)
                            loc_p.fill(password)
                            result_payload = {"ok": True}
                            step_log.append("Entered sign-in credentials")
                            if rl is not None:
                                # Deliberately no ref/value here - fill_login is the one
                                # tool that ever touches credential values, and the log
                                # must never carry them even indirectly.
                                rl.event("step", {"step": step_num, "action": "fill_login", "target": "login form", "result": "ok"})
                            screenshots.append(_capture_screenshot(page, shots_dir, "after_login_fill", rl=rl, step_num=step_num))
                        elif name == "click":
                            loc = _element_locator(page, inp["ref"])
                            label = (loc.get_attribute("aria-label") or loc.inner_text() or "an element").strip()[:60]
                            loc.click(timeout=ACTION_TIMEOUT_MS)
                            page.wait_for_load_state("domcontentloaded", timeout=ACTION_TIMEOUT_MS)
                            result_payload = {"ok": True}
                            safe_label = label if not _is_critical_label(label) else "a control"
                            step_log.append(f"Clicked: {safe_label}" if not _is_critical_label(label) else "Clicked a control")
                            if rl is not None:
                                rl.event("step", {"step": step_num, "action": "click", "target": safe_label, "result": "ok"})
                            screenshots.append(_capture_screenshot(page, shots_dir, f"after_click_{step_num}", rl=rl, step_num=step_num))
                        elif name == "type_text":
                            loc = _element_locator(page, inp["ref"])
                            loc.fill(inp["text"])
                            result_payload = {"ok": True}
                            step_log.append("Entered text into a field")
                            if rl is not None:
                                # action-type redaction: never log inp["text"], regardless
                                # of what the field is - see run_logger.py header notes.
                                rl.event("step", {"step": step_num, "action": "type", "target": f"field ref {inp['ref']}", "result": "ok"})
                            screenshots.append(_capture_screenshot(page, shots_dir, f"after_type_{step_num}", rl=rl, step_num=step_num))
                        elif name == "select_option":
                            loc = _element_locator(page, inp["ref"])
                            try:
                                loc.select_option(label=inp["value"])
                            except Exception:
                                loc.select_option(inp["value"])
                            result_payload = {"ok": True}
                            step_log.append(f"Selected an option: {inp['value']}")
                            if rl is not None:
                                rl.event("step", {"step": step_num, "action": "select_option", "target": f"field ref {inp['ref']}", "value": inp["value"], "result": "ok"})
                            screenshots.append(_capture_screenshot(page, shots_dir, f"after_select_{step_num}", rl=rl, step_num=step_num))
                        elif name == "wait_for_text":
                            timeout = inp.get("timeout_ms", 5000)
                            try:
                                page.get_by_text(inp["text"], exact=False).first.wait_for(timeout=timeout)
                                result_payload = {"found": True}
                            except PlaywrightTimeoutError:
                                result_payload = {"found": False}
                            step_log.append(f"Waited for confirmation text")
                            if rl is not None:
                                rl.event("step", {"step": step_num, "action": "wait_for_text", "target": inp.get("text", "")[:80], "result": result_payload})
                        elif name == "upload_file":
                            loc = _element_locator(page, inp["ref"])
                            if attachment_path[0] is None:
                                fname = (test_case.get("attachment_filename") or "test_attachment.pdf").strip() or "test_attachment.pdf"
                                fname = re.sub(r"[^a-zA-Z0-9._-]", "_", fname)
                                ftype = (test_case.get("attachment_type") or Path(fname).suffix.lstrip(".") or "pdf").strip()
                                uploads_dir = shots_dir / "_uploads"
                                uploads_dir.mkdir(parents=True, exist_ok=True)
                                path = uploads_dir / fname
                                _generate_attachment_file(path, ftype)
                                attachment_path[0] = path
                            with page.expect_file_chooser(timeout=ACTION_TIMEOUT_MS) as fc_info:
                                loc.click(timeout=ACTION_TIMEOUT_MS)
                            fc_info.value.set_files(str(attachment_path[0]))
                            result_payload = {"ok": True, "attached_filename": attachment_path[0].name}
                            step_log.append(f"Attached a file: {attachment_path[0].name}")
                            if rl is not None:
                                rl.event("step", {"step": step_num, "action": "upload_file", "target": f"field ref {inp['ref']}", "filename": attachment_path[0].name, "result": "ok"})
                            screenshots.append(_capture_screenshot(page, shots_dir, f"after_upload_{step_num}", rl=rl, step_num=step_num))
                        elif name == "finish_test":
                            verdict = inp["verdict"]
                            notes = inp["notes"]
                            if verdict == "PASS" and isinstance(inp.get("created_entity"), dict):
                                created_entity = inp["created_entity"]
                            result_payload = {"ok": True}
                            done = True
                            if rl is not None:
                                # created_entity is test-fixture data the automation itself
                                # generated (e.g. an auto-test employee name) - never a
                                # real person's data or a credential - safe to log for
                                # troubleshooting, same as notes/verdict above.
                                rl.event("step", {"step": step_num, "action": "finish_test", "verdict": verdict, "notes": notes, "created_entity": created_entity})
                        else:
                            result_payload = {"error": f"Unknown tool {name}"}
                    except Exception as e:
                        result_payload = {"error": f"Action failed: {type(e).__name__}"}
                        step_log.append(f"Action failed ({name})")
                        if rl is not None:
                            rl.event("error", {"step": step_num, "action": name, "message": f"{type(e).__name__}"})

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": json.dumps(result_payload),
                    })

                messages.append({"role": "user", "content": tool_results})

                if on_step is not None:
                    try:
                        on_step(step_num + 1, MAX_AGENT_STEPS)
                    except Exception:
                        pass

                if done:
                    break

                if stall_count >= STALL_LIMIT:
                    stuck_url = last_fingerprint[0] if last_fingerprint else page.url
                    verdict, notes = (
                        "BLOCKED",
                        f"Stopped after the page stopped changing across {STALL_LIMIT} consecutive attempts "
                        f"(last seen at {stuck_url}). This usually means a control needed for this test case "
                        f"couldn't be reached with the current toolset, rather than a slow-loading page.",
                    )
                    if rl is not None:
                        rl.event("error", {"message": "stalled - no page change", "stall_count": stall_count, "url": stuck_url})
                    break
            else:
                verdict, notes = "BLOCKED", f"Reached the {MAX_AGENT_STEPS}-action limit for this test case without a clear result."
                if rl is not None:
                    rl.event("error", {"message": "step limit reached", "max_steps": MAX_AGENT_STEPS})

            screenshots.append(_capture_screenshot(page, shots_dir, "final", rl=rl, step_num=MAX_AGENT_STEPS))
            browser.close()

    except ExecutionError:
        raise
    except Exception as e:
        raise ExecutionError(f"Browser automation failed: {type(e).__name__}") from e

    return {
        "verdict": verdict or "BLOCKED",
        "notes": notes or "No verdict was reached.",
        "step_log": step_log,
        "screenshots": screenshots,
        "created_entity": created_entity,
    }
