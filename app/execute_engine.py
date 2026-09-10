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

from anthropic import Anthropic
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

logger = logging.getLogger("req2qa.execute")

MODEL = os.environ.get("QA_MODEL", "claude-sonnet-4-6")
MAX_AGENT_STEPS = 30  # hard cap on tool calls per test case - cost/runaway-loop guard
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
    elements = page.evaluate(
        """
        () => {
            const sel = 'input, textarea, select, button, a[href], [role="button"], [role="link"], [role="tab"], [role="checkbox"], [role="radio"]';
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


def _element_locator(page, ref: int):
    sel = 'input, textarea, select, button, a[href], [role="button"], [role="link"], [role="tab"], [role="checkbox"], [role="radio"]'
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


def _capture_screenshot(page, shots_dir: Path, label: str) -> str:
    _mask_critical_fields(page)
    fname = f"{len(list(shots_dir.glob('*.png'))):03d}_{re.sub(r'[^a-zA-Z0-9_-]', '_', label)[:40]}.png"
    path = shots_dir / fname
    try:
        page.screenshot(path=str(path), timeout=ACTION_TIMEOUT_MS)
    finally:
        _unmask(page)
    return fname


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
        "name": "finish_test",
        "description": "End the test case with a final verdict. Call this once you have either confirmed the expected result, found it did not occur, or hit something that blocks further progress (e.g. an SSO redirect, an unexpected error page).",
        "input_schema": {
            "type": "object",
            "properties": {
                "verdict": {"type": "string", "enum": ["PASS", "FAIL", "BLOCKED"]},
                "notes": {"type": "string", "description": "Plain-language explanation of the outcome, for a non-technical reviewer."},
            },
            "required": ["verdict", "notes"],
        },
    },
]


def _system_prompt(application: str, role_label: str, module: str, test_case: dict) -> str:
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
"""


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
    shots_dir: Path,
) -> dict:
    """Runs one test case against env_url using the given (sandbox-only,
    generic test-user) credentials. Returns a dict: verdict, notes,
    step_log (list of plain-language step descriptions - no credentials,
    ever), screenshots (list of filenames already saved under shots_dir).

    Raises ExecutionError on unrecoverable setup failures (bad URL, browser
    launch failure, etc.) - the caller is expected to catch this and show
    a generic error, per the app's existing error-handling convention.
    """
    shots_dir.mkdir(parents=True, exist_ok=True)
    client = Anthropic(api_key=api_key)
    step_log = []
    screenshots = []

    system_prompt = _system_prompt(application, role_label, module, test_case)
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

            screenshots.append(_capture_screenshot(page, shots_dir, "start"))
            verdict, notes = None, ""

            for step_num in range(MAX_AGENT_STEPS):
                response = client.messages.create(
                    model=MODEL,
                    max_tokens=1024,
                    system=system_prompt,
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
                            result_payload = {"url": page.url, "title": page.title(), "elements": elements}
                            step_log.append(f"Looked at the page ({page.url})")
                        elif name == "fill_login":
                            loc_u = _element_locator(page, inp["username_ref"])
                            loc_p = _element_locator(page, inp["password_ref"])
                            loc_u.fill(username)
                            loc_p.fill(password)
                            result_payload = {"ok": True}
                            step_log.append("Entered sign-in credentials")
                            screenshots.append(_capture_screenshot(page, shots_dir, "after_login_fill"))
                        elif name == "click":
                            loc = _element_locator(page, inp["ref"])
                            label = (loc.get_attribute("aria-label") or loc.inner_text() or "an element").strip()[:60]
                            loc.click(timeout=ACTION_TIMEOUT_MS)
                            page.wait_for_load_state("domcontentloaded", timeout=ACTION_TIMEOUT_MS)
                            result_payload = {"ok": True}
                            step_log.append(f"Clicked: {label}" if not _is_critical_label(label) else "Clicked a control")
                            screenshots.append(_capture_screenshot(page, shots_dir, f"after_click_{step_num}"))
                        elif name == "type_text":
                            loc = _element_locator(page, inp["ref"])
                            loc.fill(inp["text"])
                            result_payload = {"ok": True}
                            step_log.append("Entered text into a field")
                            screenshots.append(_capture_screenshot(page, shots_dir, f"after_type_{step_num}"))
                        elif name == "select_option":
                            loc = _element_locator(page, inp["ref"])
                            try:
                                loc.select_option(label=inp["value"])
                            except Exception:
                                loc.select_option(inp["value"])
                            result_payload = {"ok": True}
                            step_log.append(f"Selected an option: {inp['value']}")
                            screenshots.append(_capture_screenshot(page, shots_dir, f"after_select_{step_num}"))
                        elif name == "wait_for_text":
                            timeout = inp.get("timeout_ms", 5000)
                            try:
                                page.get_by_text(inp["text"], exact=False).first.wait_for(timeout=timeout)
                                result_payload = {"found": True}
                            except PlaywrightTimeoutError:
                                result_payload = {"found": False}
                            step_log.append(f"Waited for confirmation text")
                        elif name == "finish_test":
                            verdict = inp["verdict"]
                            notes = inp["notes"]
                            result_payload = {"ok": True}
                            done = True
                        else:
                            result_payload = {"error": f"Unknown tool {name}"}
                    except Exception as e:
                        result_payload = {"error": f"Action failed: {type(e).__name__}"}
                        step_log.append(f"Action failed ({name})")

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": json.dumps(result_payload),
                    })

                messages.append({"role": "user", "content": tool_results})

                if done:
                    break
            else:
                verdict, notes = "BLOCKED", f"Reached the {MAX_AGENT_STEPS}-action limit for this test case without a clear result."

            screenshots.append(_capture_screenshot(page, shots_dir, "final"))
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
    }
