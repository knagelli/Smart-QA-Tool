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
from anthropic import RateLimitError, PermissionDeniedError

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
# Feature flag for the navigation-guidance fix (2026-09-23) - see
# claude/[pending]-navigation-discovery-fix.md for the design/council
# writeup. Flip to False for an instant, code-free revert to the prior
# system-prompt behavior if this regresses anything in production; the
# .rollback-2026-09-23/ folder holds full pre-change file copies as a
# second, independent fallback.
ENABLE_NAV_DISCOVERY_V2 = True

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

# Bounded retry for Bedrock/Anthropic rate-limit throttling (2026-09-23 -
# root cause was an account-level Bedrock quota, not this code; see
# claude/[pending]-bedrock-rate-limit-and-error-hardening-2026-09-23.md).
# Kept deliberately short in total (well under 90s) per the council's
# explicit pushback: a long-retrying test case ties up a slot in the
# client's execution allowance without visible progress, which looks like
# a hang even when it's "working as intended." This is a safety net for a
# transient throttle, not a substitute for having enough quota headroom.
RATE_LIMIT_BACKOFF_SECONDS = [2, 4, 8, 16, 30]  # 5 retries after the initial attempt (6 total tries), ~60s of sleep

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

# (mask styling is now applied inline per-element in _mask_critical_fields -
# see its docstring for why a shared stylesheet class no longer works once
# shadow-DOM fields are in scope)


class ExecutionError(Exception):
    pass


class EnvironmentUnreachableError(ExecutionError):
    """Raised specifically when the client's own env_url didn't respond in
    time (2026-09-23 - see claude/[pending]-bedrock-rate-limit-and-error-
    hardening-2026-09-23.md for why this needed splitting out). Distinct
    from the base ExecutionError - which now means "our own automation/AI
    infrastructure broke" - because these two have OPPOSITE fault
    attribution and should be treated differently by the caller:
    - This one is plausibly the client's own environment being down or a
      wrong URL, not our fault - it should keep its original, specific,
      actionable message (not get replaced by a generic one) and should
      still count against the client's execution allowance, exactly as it
      always did.
    - The base ExecutionError (rate limit exhausted, permission denied,
      browser crash, anything unexpected) is unambiguously our fault, and
      main.py exempts it from the client's execution allowance.
    A caller distinguishes these via isinstance/except-ordering, not by
    matching on message text - matching strings is fragile and was
    explicitly rejected during this fix's review for exactly that reason."""
    pass


INTERACTIVE_SELECTOR = (
    'input, textarea, select, button, a[href], label, [role="button"], '
    '[role="link"], [role="tab"], [role="checkbox"], [role="radio"], [role="switch"]'
)

# Per-element JS run via Locator.evaluate (the element itself is `el` -
# no querySelectorAll here, see the shadow-DOM note on _snapshot_elements).
#
# value_hash (2026-09-23): a cheap, one-way, in-browser hash of the
# element's current value/checked state - added specifically to fix a
# confirmed stall-detection false positive (see
# claude/[pending]-bedrock-rate-limit-and-error-hardening-2026-09-23.md
# for the reproduction). _snapshot_fingerprint below used to key only on
# tag/role/label, and label is normally sourced from a STATIC attribute
# (placeholder/name/aria-label), not from the element's current value - so
# typing into several fields in a row, or toggling a checkbox, could look
# byte-for-byte identical across consecutive snapshots even though the
# agent made real progress, incorrectly tripping STALL_LIMIT. Verified by
# direct reproduction against this exact function (3 consecutive field
# fills -> stall_count reached STALL_LIMIT before this fix; 0 after).
# Deliberately never returns the actual text - only a non-reversible
# integer - so this cannot leak typed content (including credentials,
# though those are never typed via this path - see fill_login) into
# Python, logs, or reports. Scoped ONLY to input/textarea/select/
# checkbox/radio - not folded into every attribute, so a page with
# unrelated cosmetic dynamism (a rotating banner, a live clock) still
# correctly registers as "unchanged" for stall-detection purposes.
_ELEMENT_INFO_JS = """
(el) => {
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
    const tag = el.tagName.toLowerCase();
    const itype = (el.getAttribute('type') || '').toLowerCase();
    let value_hash = 0;
    if (tag === 'input' || tag === 'textarea' || tag === 'select') {
        const v = el.value || '';
        for (let i = 0; i < v.length; i++) {
            value_hash = ((value_hash << 5) - value_hash + v.charCodeAt(i)) | 0;
        }
    }
    if (itype === 'checkbox' || itype === 'radio') {
        value_hash = value_hash * 31 + (el.checked ? 1 : 0);
    }
    return {
        tag: tag,
        type: itype,
        role: el.getAttribute('role') || '',
        label: label || (tag === 'input' && itype === 'file' ? 'File upload' : ''),
        visible: visible,
        value_hash: value_hash,
    };
}
"""


# IFRAMES: see the module-level note above INTERACTIVE_SELECTOR's shadow-DOM
# history for the fuller story - see also
# claude/servicenow-iframe-fix-2026-09-22.md. Short version: a real
# ServiceNow PDI test run (2026-09-22) found the shadow-DOM fix below did not
# help at all, because ServiceNow's "Polaris" navigation shell renders the
# actual application (the Incident form, everything a test case needs)
# inside a classic-UI <iframe>, not in the top-level document and not behind
# a shadow root. Playwright's locator engine pierces OPEN shadow roots
# automatically for a plain CSS selector (that's what the shadow-DOM fix
# below relies on) but does NOT automatically reach into iframe content -
# an iframe is a genuinely separate frame in Playwright's model, and has to
# be walked explicitly. _visible_frames() below is that walk, shared by
# every function in this file that previously only ever looked at `page`
# directly (_snapshot_elements, _element_locator, _mask_critical_fields,
# _unmask, and the wait_for_text tool handler further down) - a single
# shared helper so all of them stay in lock-step the same way the
# shadow-DOM fix kept _snapshot_elements/_element_locator in lock-step.
# Cross-origin iframes are NOT a special case here - Playwright's Frame API
# works the same regardless of origin (unlike raw in-page JavaScript, which
# same-origin policy would block); no cross-origin workaround was needed.
GLOBAL_REF_FRAME_MULTIPLIER = 1000  # see _resolve_ref's docstring


def _visible_frames(page) -> list:
    """Every frame worth searching for interactive elements: the main frame,
    plus every child/nested frame that is currently visible (nonzero size,
    not display:none) on the page. `page.frames` already returns nested
    iframes flattened into one list (no manual recursion needed) in a
    stable order - main frame first, then children in DOM order - and that
    same order is what makes a ref computed here still resolve correctly
    later in _resolve_ref, as long as the frame tree hasn't changed shape in
    between (the same tolerance-for-staleness posture already used for
    individual elements below, just extended to frames).

    Deliberately no origin-based or URL-based filtering (e.g. skipping
    "known ad network" domains) - that would contradict this file's own
    stated design principle of staying generic and platform-agnostic, and
    it's a maintenance trap. An invisible or empty frame (a tracking pixel,
    a 0x0 iframe) naturally contributes zero elements once its own
    selector count is checked, so no special-case exclusion is needed for
    those either - the visibility check here is purely about not paying
    the cost of asking a frame that can't possibly matter."""
    frames = list(page.frames)
    if not frames:
        return []
    result = [frames[0]]  # the main frame is always included, unconditionally
    for frame in frames[1:]:
        try:
            el = frame.frame_element()
            box = el.bounding_box()
            if box and box["width"] > 0 and box["height"] > 0:
                result.append(frame)
        except Exception:
            # A frame that's been detached/replaced between the `page.frames`
            # read and this check (an SPA nav swapping an iframe's src, e.g.
            # ServiceNow's Polaris shell) - skip it rather than aborting the
            # whole walk over one frame.
            continue
    return result


def _resolve_ref(page, ref: int):
    """Turn a global ref (as handed back by _snapshot_elements/the agent)
    into (frame, raw_index_within_that_frame). Refs stay plain integers -
    no change to the tool schema the agent sees - by encoding the frame's
    position in _visible_frames()'s list in the ref's high digits:
    ref = frame_position * GLOBAL_REF_FRAME_MULTIPLIER + raw_index. 1000 is
    comfortably above the existing per-frame raw enumeration cap (200, see
    _snapshot_elements), so this can never collide. Raises IndexError if the
    frame at that position no longer exists (the frame tree changed shape
    since the ref was issued) - callers already wrap every tool call in a
    broad try/except (see the main step loop) that turns this into a normal
    "action failed" tool result the agent can react to with a fresh
    snapshot, the same tolerance already relied on for a single stale
    element disappearing."""
    frames = _visible_frames(page)
    frame_idx, raw_idx = divmod(ref, GLOBAL_REF_FRAME_MULTIPLIER)
    return frames[frame_idx], raw_idx


def _snapshot_elements(page) -> list:
    """Build a DOM/accessibility-tree-ish snapshot: every interactive
    element with a stable ref, its role, and its best-available label.
    Claude sees this list (never a screenshot) to decide what to do next -
    cheaper and far more robust than pixel-based computer-use for the kind
    of dense, role/label-driven forms enterprise web apps are built from.
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

    SHADOW DOM: this used to run a single `page.evaluate` doing a raw
    `document.querySelectorAll`, which cannot see into shadow DOM at all -
    any control rendered inside a shadow root (as most Salesforce Lightning
    Web Components and many ServiceNow/Angular-Material-style widgets do)
    was invisible to the agent, not just hard to act on. Playwright's own
    locator engine pierces OPEN shadow roots transparently for a plain CSS
    selector, so this enumerates via `<frame>.locator(INTERACTIVE_SELECTOR)`
    instead of a raw DOM query - same selector list, but shadow-DOM-inclusive
    - and `_element_locator`/`_resolve_ref` below index into the exact same
    locator, so they stay in lock-step. CLOSED shadow roots remain genuinely
    unreachable (true of every browser-automation tool, not a req2qa gap) -
    nothing here can or should try to defeat that encapsulation.

    IFRAMES: now walks every visible frame via _visible_frames(), not just
    the top-level page - see the note above _visible_frames for why. A
    global element budget of 200 (unchanged from the old single-frame cap)
    is shared across all frames, main frame first, so the overwhelmingly
    common single-frame case is completely unaffected (page.frames is just
    [main_frame] and this behaves identically to before) and iframe content
    only ever uses leftover budget rather than crowding out the main page.
    An element sourced from a non-main frame gets a `frame_url` hint so the
    agent's reasoning can tell it apart from the main page - added only for
    those elements, so a normal single-frame app's elements are byte-for-
    byte what they were before this change."""
    frames = _visible_frames(page)
    elements = []
    for frame_idx, frame in enumerate(frames):
        budget = 200 - len(elements)
        if budget <= 0:
            break
        try:
            locator = frame.locator(INTERACTIVE_SELECTOR)
            count = min(locator.count(), budget)
        except Exception:
            # A frame that's gone/unreachable by the time we query it - skip
            # it, same tolerance as a single stale element elsewhere here.
            continue
        for i in range(count):
            try:
                data = locator.nth(i).evaluate(_ELEMENT_INFO_JS)
            except Exception:
                # A node that disappeared/detached between count() and evaluate()
                # (rare, but real on a dynamically re-rendering SPA) - skip it
                # rather than aborting the whole snapshot over one stale ref.
                continue
            data["ref"] = frame_idx * GLOBAL_REF_FRAME_MULTIPLIER + i
            if frame_idx != 0:
                try:
                    data["frame_url"] = frame.url[:200]
                except Exception:
                    pass
            elements.append(data)
    if len(frames) > 1:
        # Observability, not functional - added 2026-09-22 alongside this
        # fix so a FUTURE site with some frame topology this fix didn't
        # anticipate fails with a visible diagnostic signal in the logs
        # (how many frames, whether any of them actually contributed
        # elements) instead of another silent, multi-day mystery
        # investigation like the one that found this gap in the first
        # place. See claude/servicenow-iframe-fix-2026-09-22.md.
        try:
            logger.info(
                "snapshot: %d visible frame(s), %d element(s) total, %d from non-main frames",
                len(frames), len(elements), sum(1 for e in elements if "frame_url" in e),
            )
        except Exception:
            pass
    # A <input type="file"> is kept even when CSS-hidden (display:none /
    # zero-size) - a very common enterprise-UI pattern is a styled,
    # visible wrapper (a photo-picker area, a "Choose File" button)
    # that sits on top of a visually-hidden native file input. Filtering
    # purely on 'visible' meant the agent could never get a ref to that
    # input at all and would loop clicking decoy elements nearby,
    # timing out every attempt (seen on OrangeHRM's profile-photo
    # upload). Kept it filtered for every other element type, since
    # that's still the right rule for everything that isn't a file input.
    return [e for e in elements if (e["visible"] and e["label"]) or (e["tag"] == "input" and e["type"] == "file")]


def _snapshot_fingerprint(page, elements: list) -> tuple:
    """A cheap signature of 'what the agent can currently see and do',
    used only to detect a stall (the page genuinely not changing across
    repeated snapshots) - not for anything functional. Still keyed off
    page.url (the top-level URL) even post-iframe-fix: an iframe swapping
    its internal content while the outer URL stays the same (exactly
    ServiceNow's Polaris pattern) is still caught, because the elements
    tuple itself changes when the iframe's content changes - the URL half
    of this signature was never doing the real work of stall detection,
    the element tuple was.

    value_hash (2026-09-23) is now part of this signature too - see the
    _ELEMENT_INFO_JS docstring note above for the confirmed false-positive
    this closes (typing into a field, or toggling a checkbox/radio, no
    longer looks identical to the previous snapshot just because the
    element's static label didn't change)."""
    return (page.url, tuple((e["tag"], e["role"], e["label"], e.get("value_hash", 0)) for e in elements))


def _element_locator(page, ref: int):
    # Must use the exact same selector/walk as _snapshot_elements
    # (INTERACTIVE_SELECTOR via _resolve_ref) so a ref handed back by the
    # agent resolves to the same element both times - Playwright's locator
    # pierces open shadow roots the same way in both places, and
    # _resolve_ref applies the same frame walk _snapshot_elements used to
    # build the ref in the first place.
    frame, raw_idx = _resolve_ref(page, ref)
    return frame.locator(INTERACTIVE_SELECTOR).nth(raw_idx)


def _is_critical_label(label: str) -> bool:
    low = (label or "").lower()
    return any(k in low for k in CRITICAL_FIELD_KEYWORDS)


_MASK_FIELD_SELECTOR = 'input, textarea, select, [role="textbox"]'


def _mask_critical_fields(page):
    """Opaquely mask any currently-visible critical-category field before a
    screenshot; call _unmask() again right after the screenshot is taken.

    Previously this added a class via a raw `document.querySelectorAll` and
    relied on an externally-injected <style> tag to render it opaque. Two
    problems, both fixed here: (1) the raw query couldn't see a field inside
    a shadow root at all - same class of gap as _snapshot_elements above -
    so a password/credential field rendered inside a shadow-DOM component
    would never be found or masked; (2) even if it HAD been found, shadow
    DOM's style encapsulation means a class fed by a light-DOM stylesheet
    does not reach into a shadow root, so the mask would silently fail to
    render even on a correctly-found element. Both are fixed by (a)
    enumerating via Playwright's locator engine, which pierces open shadow
    roots, and (b) setting the masked appearance as inline style directly on
    the element, which always applies regardless of shadow boundaries - no
    external stylesheet involved.

    IFRAMES: walks every visible frame via _visible_frames(), same as
    _snapshot_elements - this was the single most important place for that
    gap to exist, more so than the snapshot logic itself. A password field
    rendered inside an iframe (common for SSO logins embedded via an
    identity-provider iframe - Okta, Azure AD, etc. all do this) was
    previously invisible to this function exactly like a shadow-DOM field
    was, meaning it could render UNMASKED in a saved screenshot - a direct
    violation of the CREDENTIAL HANDLING contract at the top of this file.
    Deliberately no per-frame element cap here (unlike the 200-element
    budget in _snapshot_elements) and no origin filtering - masking is a
    security control, not an agent-facing convenience, so thoroughness
    matters more than staying under some prompt-size budget; there is no
    prompt here to bloat. See claude/servicenow-iframe-fix-2026-09-22.md."""
    marked = 0
    for frame in _visible_frames(page):
        try:
            locator = frame.locator(_MASK_FIELD_SELECTOR)
            count = min(locator.count(), 200)
        except Exception:
            continue
        for i in range(count):
            try:
                was_masked = locator.nth(i).evaluate(
                    """
                    (el, keywords) => {
                        const label = (
                            (el.getAttribute('aria-label') || '') + ' ' +
                            (el.getAttribute('placeholder') || '') + ' ' +
                            (el.getAttribute('name') || '') + ' ' +
                            (el.id || '')
                        ).toLowerCase();
                        if (keywords.some(k => label.includes(k))) {
                            el.dataset.req2qaPrevStyle = el.getAttribute('style') || '';
                            el.style.setProperty('background', '#111', 'important');
                            el.style.setProperty('color', 'transparent', 'important');
                            el.style.setProperty('border-radius', '3px', 'important');
                            return true;
                        }
                        return false;
                    }
                    """,
                    CRITICAL_FIELD_KEYWORDS,
                )
            except Exception:
                continue
            if was_masked:
                marked += 1
    return marked


def _unmask(page):
    # Must walk the same frames _mask_critical_fields did, for the same
    # reason _element_locator must match _snapshot_elements's walk - see
    # _mask_critical_fields's docstring for why iframes matter here.
    for frame in _visible_frames(page):
        try:
            locator = frame.locator(_MASK_FIELD_SELECTOR)
            count = min(locator.count(), 200)
        except Exception:
            continue
        for i in range(count):
            try:
                locator.nth(i).evaluate(
                    """
                    (el) => {
                        if (el.dataset.req2qaPrevStyle !== undefined) {
                            el.setAttribute('style', el.dataset.req2qaPrevStyle);
                            delete el.dataset.req2qaPrevStyle;
                        }
                    }
                    """
                )
            except Exception:
                continue


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
    nav_instruction = ""
    if ENABLE_NAV_DISCOVERY_V2:
        nav_instruction = (
            "\nFinding your way to a feature the test steps name (e.g. \"go to Incidents\", "
            "\"open the Leave module\") is a navigation problem, not a search problem:\n"
            "- Your FIRST attempt to get there must be an actual menu/nav trigger: a labeled "
            "menu/nav element (e.g. \"All\", \"Menu\", \"Apps\", a hamburger icon, a sidebar/"
            "top-bar icon, an employee/avatar/profile dropdown). Click that first, then look at "
            "the fresh snapshot it reveals for the specific module/feature named in the test "
            "steps - clicking through a menu like this often takes more than one step, which is "
            "expected. A magnifying-glass icon or a control literally labeled \"Search\" does "
            "NOT count as this kind of menu/nav trigger, even though it sits in the same toolbar "
            "- do not click it, and do not type into it, as your first move.\n"
            "- Only AFTER you have actually clicked into a real menu/nav trigger and it did not "
            "contain what you need may you fall back to a genuine keyword-search box - typing "
            "the feature/module/record name into it is a legitimate second attempt, not "
            "forbidden. What is never allowed is using a search box (clicking it open or typing "
            "into it) as a substitute for trying the menu first, and once you are already typing "
            "into a search box, repeatedly retyping the same or similar text into it without "
            "trying the menu will not get you anywhere new - if a snapshot looks unchanged after "
            "doing this, that is why.\n"
            "- Whichever path you used - menu or fallback search - check the fresh snapshot "
            "before proceeding with the test steps: does the page's title, URL, or a prominent "
            "heading actually look like the module/feature named in the test steps (given as "
            "\"Module/area\" above)? If instead you're looking at something generic or unrelated "
            "(e.g. a general homepage, dashboard, or promotional/marketing content), you have "
            "NOT arrived - do not proceed with the test steps on this page. Go back (e.g. reopen "
            "the menu, or return to the page you started from) and try again via a menu trigger "
            "you have not already tried. Only call finish_test with BLOCKED if a second such "
            "attempt also fails to reach a page matching the named module/feature.\n"
            "- If element hints below already tell you exactly which control to use, prefer "
            "that over guessing.\n"
            "- While exploring a menu to find your way, do not click anything whose label means "
            "signing out/ending the session (e.g. Log Out, Sign Out, End Session) or a "
            "destructive/irreversible action (e.g. Delete, Remove, Terminate) unless a test step "
            "explicitly calls for that action - if you're not sure whether a step calls for it, "
            "treat it as it does not.\n"
        )
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
{fixture_instruction}{nav_instruction}"""


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
                raise EnvironmentUnreachableError(
                    "Could not reach the environment URL in time. Please confirm the "
                    "sandbox/UAT environment is running and the URL is correct, then try again."
                ) from e

            screenshots.append(_capture_screenshot(page, shots_dir, "start", rl=rl, step_num=0))
            verdict, notes = None, ""
            last_fingerprint = None
            stall_count = 0
            # Lazily created on the first upload_file call and reused for
            # any further ones in this same test case - one generated file
            # per run is all a single scenario needs.
            attachment_path: list = [None]

            for step_num in range(MAX_AGENT_STEPS):
                # Rate-limit retry + distinct IAM/permission diagnostics
                # (2026-09-23) - see claude/[pending]-bedrock-rate-limit-
                # and-error-hardening-2026-09-23.md. Root cause of today's
                # incident was an account-level Bedrock quota (fixed via an
                # AWS quota increase, not code), but the SDK's own built-in
                # retry (2 attempts, well under 2s total backoff) is too
                # thin for genuine per-minute throttling - this gives a
                # transient throttle a real chance to clear before failing
                # the whole test case. PermissionDeniedError is NOT
                # retried (a permission problem doesn't get better by
                # waiting) - it's re-raised immediately with a message that
                # names the likely cause, so a future IAM/model-ID mismatch
                # (the exact defect this session spent significant time
                # diagnosing from a bare 403 traceback) is diagnosable in
                # seconds from the log, not by re-deriving it from scratch.
                for attempt, backoff in enumerate([0] + RATE_LIMIT_BACKOFF_SECONDS):
                    if backoff:
                        time.sleep(backoff)
                    try:
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
                        break
                    except PermissionDeniedError as e:
                        logger.error(
                            "Bedrock/Anthropic permission denied - likely an IAM policy or "
                            "model-ID mismatch (check the inference-profile ARN this app is "
                            "configured with against what the attached IAM role actually "
                            "permits): %s", e,
                        )
                        raise
                    except RateLimitError:
                        if attempt == len(RATE_LIMIT_BACKOFF_SECONDS):
                            raise
                        if rl is not None:
                            rl.event("warning", {
                                "step": step_num, "message": "rate limited, retrying",
                                "attempt": attempt + 1, "next_backoff_s": RATE_LIMIT_BACKOFF_SECONDS[attempt],
                            })
                        continue
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
                            # value_hash is internal-only bookkeeping for stall
                            # detection (see _ELEMENT_INFO_JS/_snapshot_fingerprint) -
                            # it must never be sent to the model: it's meaningless to
                            # the agent's reasoning, and since conversation history
                            # (unlike the system prompt/tools list) isn't under a
                            # prompt-cache breakpoint, leaving it in would silently
                            # inflate billed input tokens on every subsequent call
                            # for the rest of the test case, for every element, on
                            # every single snapshot. Strip it here, after it's
                            # already been used for the fingerprint above.
                            elements_for_model = [
                                {k: v for k, v in e.items() if k != "value_hash"} for e in elements
                            ]
                            result_payload = {
                                "url": page.url, "title": page.title(), "elements": elements_for_model,
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
                            # Waits on the SPECIFIC frame the clicked element
                            # belonged to, not always the top-level page - a
                            # click inside a child iframe (e.g. ServiceNow's
                            # Polaris shell, where the outer URL can stay
                            # identical while only the iframe's content
                            # navigates) previously waited on the wrong
                            # frame's load state, which returns instantly
                            # since the top page never left
                            # "domcontentloaded" - the very next snapshot
                            # could then race ahead of the iframe's new
                            # content finishing its load. Best-effort: a
                            # frame that's mid-navigation can itself throw
                            # here, which must never fail the click that
                            # already succeeded - the existing stall-
                            # detection loop is the safety net if this wait
                            # doesn't fully cover a given case.
                            clicked_frame, _ = _resolve_ref(page, inp["ref"])
                            try:
                                clicked_frame.wait_for_load_state("domcontentloaded", timeout=ACTION_TIMEOUT_MS)
                            except Exception:
                                pass
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
                            # IFRAMES: checks every visible frame, not just the
                            # top-level page (see _visible_frames' module note) -
                            # a confirmation message rendered inside an iframe
                            # (as ServiceNow's does) previously could never be
                            # found here, only ever on the outer page. The given
                            # timeout is split across frames rather than applied
                            # in full to each one in sequence, so the total
                            # worst-case wait when the text is genuinely absent
                            # stays close to the original budget regardless of
                            # frame count - for the common single-frame case
                            # (len(frames) == 1) this is identical to before.
                            timeout = inp.get("timeout_ms", 5000)
                            frames_to_check = _visible_frames(page)
                            per_frame_timeout = max(500, timeout // max(1, len(frames_to_check)))
                            found = False
                            for frame in frames_to_check:
                                try:
                                    frame.get_by_text(inp["text"], exact=False).first.wait_for(timeout=per_frame_timeout)
                                    found = True
                                    break
                                except PlaywrightTimeoutError:
                                    continue
                                except Exception:
                                    continue
                            result_payload = {"found": found}
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
                            # If the ref points straight at a native file input (the
                            # common case now that the snapshot surfaces it even when
                            # CSS-hidden - see _snapshot_elements above), set the file
                            # directly. This needs no visibility/click at all, unlike
                            # the file-chooser dance below, so it works reliably for a
                            # visually-hidden input behind a styled upload widget
                            # (added 2026-09-21, fixes a TimeoutError previously seen
                            # on OrangeHRM's profile-photo upload).
                            is_file_input = (
                                (loc.evaluate("el => el.tagName.toLowerCase()") or "") == "input"
                                and (loc.get_attribute("type") or "").lower() == "file"
                            )
                            if is_file_input:
                                loc.set_input_files(str(attachment_path[0]))
                            else:
                                # ref is a visible trigger (button/label/div) whose
                                # click pops the browser's native OS file dialog -
                                # intercept that dialog instead.
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
