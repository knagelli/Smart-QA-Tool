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
import threading
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

# PASS review gate (2026-09-23) - see claude/pass-review-gate-evidence-table-
# 2026-09-23.md. In the real TC-001 ServiceNow run the agent skipped two
# explicit steps (Category left wrong, Short description never filled) yet
# the design allowed it to declare PASS from memory alone. The FIRST
# finish_test(PASS) in a test case is therefore not accepted immediately:
# the harness replies with the original steps/expected result PLUS the
# actual current form field values read straight from the page, and only a
# second finish_test is final. Showing the agent reality (not asking it to
# recall) is what makes a reflexive rubber-stamp PASS hard to give.
# PASS_REVIEW_EXTRA_STEPS is a one-time bounded budget added only on that
# path, so a PASS given on the very last step can still be reviewed and
# corrected instead of being overwritten by the step-limit BLOCKED.
# Flip ENABLE_PASS_REVIEW to False for an instant code-free revert.
ENABLE_PASS_REVIEW = True
PASS_REVIEW_EXTRA_STEPS = 8
# One-time extra budget when a final PASS is re-checked because the form
# changed after the review (see Fix 3 in execute_test_case).
PASS_RECHECK_EXTRA_STEPS = 4

# Clause-evidence gate (2026-09-24, from the 11-case ServiceNow batch: TC-002
# PASSed by reasoning about how a different role "would" behave, TC-015 and
# TC-004 PASSed clauses that were never observed). A PASS must carry
# clause_evidence with every clause 'met' and a concrete observation. The
# harness rejects it otherwise; after CLAUSE_EVIDENCE_MAX_REJECTIONS the
# verdict becomes BLOCKED ("PASS could not be substantiated") rather than an
# unsupported PASS. Flip ENABLE_CLAUSE_EVIDENCE_GATE to False to revert.
ENABLE_CLAUSE_EVIDENCE_GATE = os.environ.get("REQ2QA_CLAUSE_EVIDENCE_GATE", "1").strip().lower() not in ("0", "false", "no", "off")

# ---------------------------------------------------------------------------
# Token efficiency (2026-09-24). Measured on a ServiceNow-shaped synthetic
# page, 32-call create-incident flow: the build before this change sent
# ~2.14M input tokens per test case (largest call ~124k) vs ~0.82M before
# the snapshot-budget fix - and Bedrock's cross-region tokens-per-minute
# quota started returning 429 for whole batches. Three levers, each with its
# own off-switch so any one can be reverted without touching the others:
#   COMPACT_SNAPSHOT_ENCODING - same information, fewer bytes (frame URL
#       sent once per snapshot instead of on every element; always-true /
#       empty keys dropped).
#   HISTORY_KEEP_SNAPSHOTS - only the latest N page snapshots are re-sent in
#       the conversation; older ones become a one-line note. All actions and
#       their results stay. 0 disables (every snapshot re-sent, old behavior).
#   TPM pacing - before each model call, wait if sending it would exceed a
#       tokens-per-minute budget shared by every run in this process.
# See claude/token-efficiency-impact-analysis-2026-09-24.md.
# ---------------------------------------------------------------------------
def _env_flag(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None or v.strip() == "" else v.strip().lower() not in ("0", "false", "no", "off")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


# Each lever can be switched off on the server without a code change
# (edit the systemd unit's Environment= lines, then restart):
#   REQ2QA_COMPACT_SNAPSHOTS=0, REQ2QA_HISTORY_KEEP_SNAPSHOTS=0, REQ2QA_TPM_BUDGET=0
COMPACT_SNAPSHOT_ENCODING = _env_flag("REQ2QA_COMPACT_SNAPSHOTS", True)
HISTORY_KEEP_SNAPSHOTS = _env_int("REQ2QA_HISTORY_KEEP_SNAPSHOTS", 2)
# 0 disables pacing. Set REQ2QA_TPM_BUDGET on the server to ~80% of the
# account's Bedrock tokens-per-minute quota for the model in use.
TPM_BUDGET = _env_int("REQ2QA_TPM_BUDGET", 0)
TPM_MAX_WAIT_SECONDS = 90
# Rough size of the fixed tools list in tokens, added to pacing estimates.
TOOLS_TOKEN_ESTIMATE = 2500
CLAUSE_EVIDENCE_MAX_REJECTIONS = 2
CLAUSE_EVIDENCE_EXTRA_STEPS = 3
# Wording that signals an assumption rather than an observation. Heuristic
# on purpose: a false hit only costs the agent one rewrite of the evidence.
_ASSUMPTION_MARKERS = ("would ", "assume", "presumably", "typically", "standard behavior",
                       "standard behaviour", "should be", "likely", "probably")
# Wording in an observation that says the clause was NOT seen to hold, while
# the agent marks it 'met' (e.g. TC-002: "readonly false" for a read-only
# clause). Word-boundary matched. Also heuristic: the agent is told exactly
# which words tripped it, so a genuine observation just gets reworded.
_CONTRADICTION_PATTERNS = (r"\bfalse\b", r"\bdid not\b", r"\bdidn't\b", r"\bnot verified\b",
                           r"\bunverified\b", r"\bunable to\b", r"\bcould not\b", r"\bcouldn't\b",
                           r"\bnot observed\b", r"\bnot confirmed\b")
# Evidence table limits: never capture more than this many fields, or more
# than this many characters of any one value.
EVIDENCE_MAX_FIELDS = 60
EVIDENCE_MAX_VALUE_CHARS = 120
# Extra label keywords masked in the evidence table only (on top of
# CRITICAL_FIELD_KEYWORDS): secret-like fields that the screenshot mask list
# was never designed around because screenshots of them were never taken.
EVIDENCE_SENSITIVE_KEYWORDS = [
    "token", "secret", "api key", "apikey", "private key", "otp", "one-time",
    "cvv", "card number", "pin number", "credential",
]
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


# 2026-09-23: broadened after a real ServiceNow run left 3 test cases
# BLOCKED - wait_for_text confirmed the text "All" was present and visible
# somewhere on the page, but no element in any snapshot ever matched this
# selector for it, so the agent never had a ref to click. The original
# selector only covered elements that declare an explicit interactive
# TAG or a well-known WAI-ARIA interactive ROLE. Many component
# frameworks (ServiceNow's "Now Experience" custom elements included, but
# this is common across Angular/React component libraries generally, not
# ServiceNow-specific - kept here as a platform-agnostic heuristic per
# this file's stated design principle, not a vendor-specific patch) mark
# an element as keyboard-interactive purely via a `tabindex` attribute,
# or use ARIA roles from the wider "composite widget" set (menuitem,
# option, treeitem, ...) that the original list didn't include. Both
# additions below are standard, well-established accessibility signals
# for "this is something a user can act on" (the same signal set
# accessibility-testing tools like axe-core use to define "focusable"),
# not a guess at ServiceNow's specific markup - deliberately still
# generic rather than hard-coding anything ServiceNow-shaped.
INTERACTIVE_SELECTOR = (
    'input, textarea, select, button, a[href], label, summary, '
    '[tabindex]:not([tabindex="-1"]), '
    '[role="button"], [role="link"], [role="tab"], [role="checkbox"], '
    '[role="radio"], [role="switch"], [role="menuitem"], '
    '[role="menuitemcheckbox"], [role="menuitemradio"], [role="option"], '
    '[role="treeitem"], [role="gridcell"]'
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
    const tag = el.tagName.toLowerCase();
    const itype = (el.getAttribute('type') || '').toLowerCase();
    // 2026-09-23: a password input's value must never become its label -
    // previously an unlabeled password field (no aria-label/placeholder/
    // name) put the typed password itself into the model context and the
    // run log via this fallback. Found by the PASS-review synthetic test.
    const label = (
        el.getAttribute('aria-label') ||
        el.getAttribute('placeholder') ||
        el.getAttribute('name') ||
        el.innerText ||
        // 2026-09-24: a field's typed value is no longer used as its label
        // (it leaked whatever was typed - tokens, names - into the run log).
        // Values now travel separately in 'value' (masked in Python). Only
        // button-type inputs keep it, since there the value IS the caption.
        (['submit', 'button', 'reset'].includes(itype) ? el.value : '') ||
        ((tag === 'input' || tag === 'textarea' || tag === 'select') && el.labels && el.labels.length ? el.labels[0].innerText : '') ||
        el.id || (itype === 'password' ? 'Password' : '')
    ).trim().slice(0, 80);
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
    // 2026-09-24 (TC-002 false PASS): the agent could not see a field's
    // current value, so it could not tell that its typing into a supposedly
    // read-only field had been accepted. Current value (select -> visible
    // option text) and read-only state are now read here. Password, hidden
    // and file inputs are never read. Python masks sensitive labels before
    // the model sees it, and the run log never carries it.
    let value = null;
    let options = null;
    if (tag === 'select') {
        const o = el.options[el.selectedIndex]; value = o ? o.text : '';
        // 2026-09-24 (TC-008/TC-011): the agent could only see the selected
        // option, so "verify the dropdown contains X" was checked by trial
        // and error. Capped; masked in Python for sensitive fields.
        options = Array.from(el.options).slice(0, 40).map(op => (op.text || '').trim().slice(0, 60));
        if (el.options.length > 40) options.push('(+' + (el.options.length - 40) + ' more)');
    } else if (itype === 'checkbox' || itype === 'radio') {
        value = el.checked ? 'checked' : 'unchecked';
    } else if ((tag === 'input' && !['password', 'hidden', 'file', 'submit', 'button', 'reset', 'image'].includes(itype)) || tag === 'textarea') {
        value = el.value || '';
    }
    let field_label = '';
    try {
        if (el.labels && el.labels.length) field_label = el.labels[0].innerText;
        if (!field_label && el.getAttribute('aria-labelledby')) {
            field_label = el.getAttribute('aria-labelledby').split(/\\s+/)
                .map(id => { const n = document.getElementById(id); return n ? n.innerText : ''; }).join(' ');
        }
    } catch (e) {}
    field_label = (field_label || '').replace(/\\s+/g, ' ').replace(/^[*\\s]+/, '').trim().slice(0, 80);
    const readonly = !!(el.readOnly || el.disabled || el.getAttribute('aria-readonly') === 'true' || el.getAttribute('aria-disabled') === 'true');
    let pointer_locked = false;
    try { pointer_locked = getComputedStyle(el).pointerEvents === 'none'; } catch (e) {}
    const editable_kind = (value !== null && !['checkbox', 'radio'].includes(itype) && tag !== 'select') || el.isContentEditable;
    return {
        tag: tag,
        type: itype,
        role: el.getAttribute('role') || '',
        label: label || (tag === 'input' && itype === 'file' ? 'File upload' : ''),
        visible: visible,
        value_hash: value_hash,
        value: value,
        readonly: readonly,
        field_label: field_label,
        pointer_locked: pointer_locked,
        text_entry: !!editable_kind,
        options: options,
        multiple: !!el.multiple,
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
# 2026-09-24: raised from 1000. With the snapshot-budget fix the raw scan
# per frame is no longer capped at 200, and ServiceNow-style forms carry
# hundreds of hidden inputs; a 1000-wide range would have forced a raw cap
# of 999 that a hidden-heavy frame can exhaust before reaching any visible
# control (reproduced in the synthetic test). Refs are per-snapshot only and
# never persisted, so the larger range changes nothing else.
GLOBAL_REF_FRAME_MULTIPLIER = 100000  # see _resolve_ref's docstring


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
    ref = frame_position * GLOBAL_REF_FRAME_MULTIPLIER + raw_index. The
    multiplier is kept above SNAPSHOT_RAW_PER_FRAME_CAP (asserted in
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


# Snapshot budgets (2026-09-24) - see claude/snapshot-budget-fix-2026-09-24.md.
# Real TC-001 run: the old 200 cap counted RAW selector matches (hidden
# inputs, display:none tips, ServiceNow's many hidden fields) BEFORE the
# visible/labelled filter, so the incident form was silently cut off at the
# "Impact" label - Short description, Description and the bottom of the form
# never reached the agent in any snapshot. The cap now counts only elements
# the agent can actually use. The raw scan per frame stays below
# GLOBAL_REF_FRAME_MULTIPLIER so a raw index can never spill into the next
# frame's ref range. Any truncation is now reported to the agent and logged.
SNAPSHOT_KEPT_BUDGET = 200
SNAPSHOT_RAW_PER_FRAME_CAP = 20000  # must stay < GLOBAL_REF_FRAME_MULTIPLIER

# Runs the per-element info + keep filter inside the browser in ONE call per
# frame and returns only kept elements (with their raw index, which is what
# the ref encodes) plus how many usable ones were over budget - so a frame
# with thousands of hidden inputs costs one round trip and a small payload.
# Keep rule: visible AND labelled, or any <input type=file> (see the note at
# the end of _snapshot_elements for why hidden file inputs are kept).
_ELEMENT_INFO_ALL_JS = (
    "([cap, budget]) => { const info = (" + _ELEMENT_INFO_JS.strip() + "); "
    "return (els) => { const kept = []; let omitted = 0; const n = Math.min(els.length, cap); "
    "for (let i = 0; i < n; i++) { let d; try { d = info(els[i]); } catch (e) { continue; } "
    "const keep = (d.visible && d.label) || (d.tag === 'input' && d.type === 'file'); "
    "if (!keep) continue; if (kept.length >= budget) { omitted++; continue; } "
    "d.raw_index = i; kept.push(d); } "
    "return {kept: kept, omitted: omitted, raw_total: els.length}; }; }"
)


def _snapshot_elements(page, stats: Optional[dict] = None) -> list:
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
    assert SNAPSHOT_RAW_PER_FRAME_CAP < GLOBAL_REF_FRAME_MULTIPLIER
    frames = _visible_frames(page)
    elements = []
    omitted_keepable = 0      # usable elements dropped because the kept budget ran out
    raw_capped_frames = 0     # frames with more raw matches than we can scan
    # Pass 1: each frame reports up to SNAPSHOT_KEPT_BUDGET usable elements
    # (one browser round trip per frame, not one per element).
    per_frame = []  # (frame_idx, frame, kept_list, total_usable)
    for frame_idx, frame in enumerate(frames):
        try:
            locator = frame.locator(INTERACTIVE_SELECTOR)
            fn = f"(els) => ({_ELEMENT_INFO_ALL_JS})([{SNAPSHOT_RAW_PER_FRAME_CAP}, {SNAPSHOT_KEPT_BUDGET}])(els)"
            res = locator.evaluate_all(fn)
        except Exception:
            # A frame that's gone/unreachable by the time we query it - skip
            # it, same tolerance as a single stale element elsewhere here.
            continue
        if res.get("raw_total", 0) > SNAPSHOT_RAW_PER_FRAME_CAP:
            raw_capped_frames += 1
        kept = res.get("kept", [])
        per_frame.append((frame_idx, frame, kept, len(kept) + res.get("omitted", 0)))
    # Pass 2: split the global budget fairly across frames (water-filling),
    # so one large frame listed first (e.g. a big nav frame) can never starve
    # a later frame (e.g. the form) - found by the synthetic test.
    alloc = {}
    remaining = SNAPSHOT_KEPT_BUDGET
    order = sorted(range(len(per_frame)), key=lambda k: len(per_frame[k][2]))
    for pos, k in enumerate(order):
        share = remaining // (len(order) - pos)
        alloc[k] = min(len(per_frame[k][2]), share)
        remaining -= alloc[k]
    for k, (frame_idx, frame, kept, total_usable) in enumerate(per_frame):
        omitted_keepable += total_usable - alloc[k]
        for data in kept[:alloc[k]]:
            data["ref"] = frame_idx * GLOBAL_REF_FRAME_MULTIPLIER + data.pop("raw_index")
            if frame_idx != 0:
                try:
                    data["frame_url"] = frame.url[:200]
                except Exception:
                    pass
            elements.append(data)
    if stats is not None:
        stats["omitted_keepable"] = omitted_keepable
        stats["raw_capped_frames"] = raw_capped_frames
    if omitted_keepable or raw_capped_frames:
        logger.warning(
            "snapshot truncated: %d usable element(s) omitted over budget %d; %d frame(s) over raw cap %d",
            omitted_keepable, SNAPSHOT_KEPT_BUDGET, raw_capped_frames, SNAPSHOT_RAW_PER_FRAME_CAP,
        )
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
    # (filtering now happens in-browser in _ELEMENT_INFO_ALL_JS so the budget
    # above counts only kept elements - see SNAPSHOT_KEPT_BUDGET note.)
    return elements


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


# Evidence table (2026-09-23): reads every visible form field in one frame
# as {label, value, kind}. This is deliberately the ONLY place raw field
# values are ever read by the harness; the redaction rules are applied
# here in-browser (password/hidden/file inputs never leave the page) and
# again in Python (_capture_form_state masks CRITICAL_FIELD_KEYWORDS
# labels), so a sensitive value is never returned, logged or reported.
# Select fields report the visible option TEXT ("Inquiry / Help"), not the
# stored option value ("inquiry"), because the evidence is read by a
# human and by the agent against human-written steps.
_FORM_STATE_JS = """
(root) => {
    const out = [];
    const walk = (node) => {
        const els = node.querySelectorAll('input, textarea, select, [role="combobox"]:not(input)');
        for (const el of els) out.push(el);
        for (const h of node.querySelectorAll('*')) { if (h.shadowRoot) walk(h.shadowRoot); }
    };
    walk(root);
    const labelFor = (el) => {
        let t = '';
        if (el.labels && el.labels.length) t = el.labels[0].innerText;
        if (!t && el.getAttribute('aria-labelledby')) {
            t = el.getAttribute('aria-labelledby').split(/\\s+/)
                .map(id => { const n = document.getElementById(id); return n ? n.innerText : ''; }).join(' ');
        }
        t = t || el.getAttribute('aria-label') || el.getAttribute('placeholder') ||
            el.getAttribute('title') || el.getAttribute('name') || el.id || '';
        return t.replace(/\\s+/g, ' ').replace(/^[*\\s]+/, '').trim();
    };
    const res = [];
    for (const el of out) {
        const tag = el.tagName.toLowerCase();
        const type = (el.getAttribute('type') || '').toLowerCase();
        if (tag === 'input' && ['password', 'hidden', 'file', 'submit', 'button', 'reset', 'image'].includes(type)) continue;
        const r = el.getBoundingClientRect();
        const cs = getComputedStyle(el);
        if (r.width === 0 || r.height === 0 || cs.visibility === 'hidden' || cs.display === 'none') continue;
        let value, kind;
        if (tag === 'select') {
            const o = el.options[el.selectedIndex];
            value = o ? o.text : ''; kind = 'select';
        } else if (type === 'checkbox' || type === 'radio') {
            value = el.checked ? 'checked' : 'unchecked'; kind = type;
        } else if (tag === 'input' || tag === 'textarea') {
            value = el.value || ''; kind = tag === 'textarea' ? 'textarea' : 'text';
        } else {
            value = (el.innerText || el.getAttribute('aria-valuetext') || '').trim(); kind = 'combobox';
        }
        res.push({label: labelFor(el), value: value, kind: kind,
                  readonly: !!(el.readOnly || el.disabled || el.getAttribute('aria-readonly') === 'true')});
    }
    return res;
}
"""


def _capture_form_state(page) -> list:
    """Evidence table: visible form fields and their CURRENT values across
    every visible frame. Never raises - evidence is a best-effort aid and
    must never fail a test case. Sensitive values are masked (see
    _FORM_STATE_JS note); fields with no label are dropped since they
    can't be matched to a step by a human or the agent anyway."""
    fields = []
    try:
        frames = _visible_frames(page)
    except Exception:
        frames = [page.main_frame]
    for frame in frames:
        try:
            rows = frame.evaluate(f"({_FORM_STATE_JS})(document)")
        except Exception:
            continue
        for row in rows or []:
            label = (row.get("label") or "").strip()[:80]
            if not label:
                continue
            value = str(row.get("value") or "")
            low = label.lower()
            if _is_critical_label(label) or any(k in low for k in EVIDENCE_SENSITIVE_KEYWORDS):
                value = "[hidden - sensitive field]"
            elif len(value) > EVIDENCE_MAX_VALUE_CHARS:
                value = value[:EVIDENCE_MAX_VALUE_CHARS] + "..."
            fields.append({"label": label, "value": value if value else "(empty)",
                           "kind": row.get("kind", ""), "readonly": bool(row.get("readonly"))})
            if len(fields) >= EVIDENCE_MAX_FIELDS:
                return fields
    return fields


# A <label> targeted by type_text/select_option is retargeted by Playwright
# to its control; read-back and the read-only check must follow it too
# (found by the TC-002 synthetic test: evaluating the label itself returned
# no value and readonly=false, so both checks silently did nothing).
_CONTROL_INFO_JS = (
    "(el) => { const t = (el.tagName === 'LABEL' && el.control) ? el.control : el; return ("
    + _ELEMENT_INFO_JS.strip() + ")(t); }"
)


def _safe_value_for_model(label: str, value) -> Optional[str]:
    """Same masking rules as the evidence table (_capture_form_state)."""
    if value is None:
        return None
    low = (label or "").lower()
    if _is_critical_label(label) or any(k in low for k in EVIDENCE_SENSITIVE_KEYWORDS):
        return "[hidden - sensitive field]"
    value = str(value)
    return value[:EVIDENCE_MAX_VALUE_CHARS] + "..." if len(value) > EVIDENCE_MAX_VALUE_CHARS else value


def _compact_snapshot_elements(elements: list) -> tuple:
    """COMPACT_SNAPSHOT_ENCODING: returns (elements, frames). Every element
    of a non-main frame used to carry the full frame URL (~140 chars on
    ServiceNow, ~47% of a snapshot); it now carries a short frame index and
    the URLs are listed once in `frames`. 'visible' (always true for kept
    elements) and empty 'type'/'role' are dropped. No information is lost:
    the frame index is also recoverable from ref // GLOBAL_REF_FRAME_MULTIPLIER."""
    frames = {}
    out = []
    for e in elements:
        m = {k: v for k, v in e.items() if not (k == "visible" or (k in ("type", "role") and not v))}
        fu = m.pop("frame_url", None)
        if fu is not None:
            idx = str(e.get("ref", 0) // GLOBAL_REF_FRAME_MULTIPLIER)
            frames[idx] = fu
            m["frame"] = int(idx)
        out.append(m)
    return out, frames


def _snapshot_stub(payload: dict) -> str:
    """What an older page snapshot is replaced with in the conversation
    re-sent to the model (HISTORY_KEEP_SNAPSHOTS)."""
    # Filled field values survive (already masked for the model) so a test
    # that compares values across pages - e.g. TC-003's two incident numbers -
    # keeps what it saw earlier. Found by the round-2 devil's advocate.
    filled = []
    for e in payload.get("elements", []) or []:
        v = e.get("value")
        if v in (None, "", "(empty)"):
            continue
        name = e.get("field_label") or e.get("label") or ""
        filled.append(f"{str(name)[:40]}: {str(v)[:60]}")
        if len(filled) >= 30:
            break
    return json.dumps({
        "older_snapshot_omitted": True,
        "url": str(payload.get("url", ""))[:200],
        "title": str(payload.get("title", ""))[:120],
        "element_count": len(payload.get("elements", []) or []),
        "filled_fields_seen": filled,
        "note": ("Superseded by a newer snapshot. Element refs from this snapshot may no longer "
                 "be valid - call get_snapshot if you need to see this page again."),
    })


def _messages_for_model(messages: list, keep: int) -> list:
    """HISTORY_KEEP_SNAPSHOTS: a copy of the conversation in which only the
    latest `keep` get_snapshot results are sent in full. The stored
    conversation is never modified. tool_use/tool_result pairing and order
    are preserved exactly (only the result *content* string changes)."""
    if not keep or keep <= 0:
        return messages
    snap_positions = []  # (msg_index, block_index)
    for mi, m in enumerate(messages):
        if m["role"] != "user" or not isinstance(m["content"], list):
            continue
        for bi, b in enumerate(m["content"]):
            if isinstance(b, dict) and b.get("type") == "tool_result":
                c = b.get("content")
                if isinstance(c, str) and c.startswith("{") and '"elements"' in c:
                    try:
                        is_snap = isinstance(json.loads(c).get("elements"), list)
                    except Exception:
                        is_snap = False
                    if is_snap:
                        snap_positions.append((mi, bi))
    stale = set(snap_positions[:-keep])
    if not stale:
        return messages
    out = []
    for mi, m in enumerate(messages):
        if any(p[0] == mi for p in stale):
            blocks = []
            for bi, b in enumerate(m["content"]):
                if (mi, bi) in stale:
                    try:
                        payload = json.loads(b["content"])
                    except Exception:
                        payload = {}
                    b = dict(b); b["content"] = _snapshot_stub(payload)
                blocks.append(b)
            out.append({"role": m["role"], "content": blocks})
        else:
            out.append(m)
    return out


class _TpmPacer:
    """Process-wide tokens-per-minute pacing (TPM_BUDGET). Before a model
    call, wait until the estimated input tokens of the last 60s plus this
    call fit the budget. Estimates from request size (~4 bytes/token) and is
    corrected with the real usage AWS returns. Never waits longer than
    TPM_MAX_WAIT_SECONDS for one call; a call bigger than the whole budget is
    sent after the window clears rather than blocking forever."""
    def __init__(self):
        self._lock = threading.Lock()
        self._events = []  # (timestamp, tokens)

    def _used(self, now):
        self._events = [(t, n) for t, n in self._events if now - t < 60]
        return sum(n for _, n in self._events)

    def wait_turn(self, est_tokens: int, budget: int) -> float:
        if budget <= 0:
            return 0.0
        waited = 0.0
        while True:
            with self._lock:
                now = time.time()
                used = self._used(now)
                if used + est_tokens <= budget or not self._events or waited >= TPM_MAX_WAIT_SECONDS:
                    self._events.append((now, est_tokens))
                    return waited
                oldest = min(t for t, _ in self._events)
                pause = max(0.5, min(60 - (now - oldest) + 0.1, TPM_MAX_WAIT_SECONDS - waited))
            time.sleep(pause)
            waited += pause

    def correct(self, est_tokens: int, actual_tokens: int):
        if not actual_tokens:
            return
        with self._lock:
            for i in range(len(self._events) - 1, -1, -1):
                if self._events[i][1] == est_tokens:
                    self._events[i] = (self._events[i][0], actual_tokens)
                    break


_TPM_PACER = _TpmPacer()


def _elements_for_model(elements: list) -> list:
    """Strip internal bookkeeping and mask values before the model sees a
    snapshot. value/readonly are only included when meaningful, to keep the
    per-step token cost down (most elements are buttons/links with neither)."""
    out = []
    for e in elements:
        m = {k: v for k, v in e.items() if k not in ("value_hash", "value", "readonly", "field_label", "pointer_locked", "text_entry", "options", "multiple")}
        fl = e.get("field_label") or ""
        if fl and fl != e.get("label"):
            # e.g. label "incident.number" (from name=) -> field_label "Number"
            m["field_label"] = fl
        v = _safe_value_for_model((e.get("label", "") + " " + fl).strip(), e.get("value"))
        if v is not None:
            m["value"] = v if v != "" else "(empty)"
        if e.get("readonly"):
            m["readonly"] = True
        if e.get("options") is not None:
            masked = _safe_value_for_model((e.get("label", "") + " " + fl).strip(), "x") != "x"
            m["options"] = ["[hidden - sensitive field]"] if masked else e["options"]
        if e.get("multiple"):
            m["multiple"] = True
        out.append(m)
    return out


def _read_back_value(loc, label: str) -> Optional[str]:
    """Fix 1 (TC-002): after typing/selecting, report what the field
    actually contains now, so the agent can't assume an edit was rejected
    (or accepted) without evidence. Never raises."""
    try:
        info = loc.evaluate(_CONTROL_INFO_JS)
        return _safe_value_for_model(" ".join(x for x in (label, info.get("label", ""), info.get("field_label", "")) if x), info.get("value"))
    except Exception:
        return None


def _form_diff(before: list, after: list) -> list:
    """Fields whose value differs between two _capture_form_state results,
    matched by label and position among same-label fields."""
    def keyed(rows):
        seen, out = {}, {}
        for r in rows:
            n = seen.get(r["label"], 0); seen[r["label"]] = n + 1
            out[(r["label"], n)] = r["value"]
        return out
    b, a = keyed(before), keyed(after)
    changes = []
    for k in list(b.keys()) + [k for k in a if k not in b]:
        if b.get(k) != a.get(k):
            changes.append({"field": k[0], "value_when_you_were_shown_it": b.get(k, "(not present)"), "value_now": a.get(k, "(not present)")})
    return changes


def _inside_test_text(text: str, start: int, end: int, test_text: str) -> bool:
    """True when the flagged words at text[start:end] are part of a longer
    phrase copied verbatim from the test case (its steps / expected result /
    precondition) - e.g. test data like 'AutoTest_22f001 - Unable to access
    email'. Found live on ServiceNow TC-001 (2026-09-24): quoting that
    Short description tripped the 'unable to' contradiction marker, and a
    genuine PASS became BLOCKED because the agent could not reword test data.
    Deliberately NOT a blanket "ignore quoted text" rule - that would let an
    agent hide 'it would be read-only' in quotes; only text that really is in
    the test case is exempt."""
    if not test_text:
        return False
    low_text, low_test = text.lower(), test_text.lower()
    min_len = (end - start) + 8   # the flagged words plus real surrounding context
    for length in range(min_len, min(len(low_text), min_len + 40) + 1):
        for s0 in range(max(0, end - length), min(start, len(low_text) - length) + 1):
            if low_text[s0:s0 + length] in low_test:
                return True
    return False


def _clause_evidence_problems(ce, test_text: str = "") -> list:
    """Problems that stop a PASS being accepted, as plain sentences for the
    agent. Empty list = acceptable. Checks structure and wording only; it
    cannot prove an observation is true (that is what the evidence table
    and screenshots in the report are for)."""
    if not isinstance(ce, list) or not ce:
        return ["clause_evidence is missing: list each clause of the expected result with what you observed."]
    problems = []
    for i, item in enumerate(ce, 1):
        if not isinstance(item, dict):
            problems.append(f"entry {i} is not a clause/observed/status object")
            continue
        clause = str(item.get("clause", "")).strip() or f"entry {i}"
        observed = str(item.get("observed", "")).strip()
        status = item.get("status")
        if status != "met":
            problems.append(f"'{clause[:80]}' is {status or 'missing a status'} - a PASS needs every clause met")
        if len(observed) < 4:
            problems.append(f"'{clause[:80]}' has no concrete observation")
        else:
            low = observed.lower()
            hits = [m.strip() for m in _ASSUMPTION_MARKERS
                    if any(not _inside_test_text(observed, x.start(), x.end(), test_text)
                           for x in re.finditer(re.escape(m), low))]
            if hits:
                problems.append(f"'{clause[:80]}' evidence reads as an assumption ({', '.join(hits)}) - state what you actually saw")
            contra = [pat.replace("\\b", "") for pat in _CONTRADICTION_PATTERNS
                      if any(not _inside_test_text(observed, x.start(), x.end(), test_text)
                             for x in re.finditer(pat, low))]
            if status == "met" and contra:
                problems.append(f"'{clause[:80]}' is marked met but the observation says otherwise ({', '.join(contra)}) - "
                                "if the clause did not hold, the verdict is FAIL; if it did, describe what you saw that shows it")
    return problems


def _pass_review_message(test_case: dict, form_fields: list) -> dict:
    return {
        "ok": False,
        "review_required": True,
        "message": (
            "Before PASS is accepted, check it against the ORIGINAL test case and the page AS IT IS "
            "RIGHT NOW (not what you remember doing). Go through each numbered step and expected result "
            "below one by one. Compare each value the steps call for with 'current_form_fields', which "
            "was read directly from the page just now. If any step was skipped, any value is wrong, or "
            "a required field is '(empty)', go back and complete it with the normal tools now - you have "
            "a few extra actions reserved for this. Then call finish_test again. Only call PASS if every "
            "step and the expected result are genuinely satisfied, with clause_evidence giving what you "
            "observed for each clause; otherwise call FAIL or BLOCKED and name the specific step or unmet "
            "precondition. This check happens once; your next finish_test is final."
        ),
        "original_precondition": test_case.get("precondition", ""),
        "original_steps": test_case.get("steps", ""),
        "original_expected_result": test_case.get("expected_result", ""),
        "current_form_fields": form_fields,
        "note_on_form_fields": (
            "If the form has already been submitted and you are now on a list/confirmation page, these "
            "fields describe that page instead - in that case verify the record against the steps "
            "(e.g. open it or check the confirmation) before finishing."
        ),
    }


_MASK_FIELD_SELECTOR = 'input, textarea, select, [role="textbox"]'


# 2026-09-24 (TC-002 run: one screenshot took 13m52s). Masking used to
# query each field ONE AT A TIME (locator.nth(i).evaluate, up to 200 per
# frame, then the same again to unmask), each call inheriting the page's
# 10s default wait. Right after Submit the page navigated away, every
# counted field had vanished, and each call waited its full 10s before
# giving up. It is now ONE evaluate_all call per frame for masking and one
# for unmasking: evaluate_all never waits for elements, so a navigating page
# costs milliseconds, not minutes. Detection is also stronger: besides the
# attribute keywords it now checks the field's visible <label> /
# aria-labelledby text and always masks type=password.
_MASK_ALL_JS = """
(els, keywords) => {
    let n = 0;
    for (const el of els) {
        try {
            let text = [el.getAttribute('aria-label'), el.getAttribute('placeholder'),
                        el.getAttribute('name'), el.id].join(' ');
            if (el.labels) for (const l of el.labels) text += ' ' + l.innerText;
            const lb = el.getAttribute('aria-labelledby');
            if (lb) for (const id of lb.split(/\\s+/)) {
                const node = document.getElementById(id); if (node) text += ' ' + node.innerText;
            }
            text = text.toLowerCase();
            const isPw = (el.getAttribute('type') || '').toLowerCase() === 'password';
            if (isPw || keywords.some(k => text.includes(k))) {
                if (el.dataset.req2qaPrevStyle === undefined) el.dataset.req2qaPrevStyle = el.getAttribute('style') || '';
                el.style.setProperty('background', '#111', 'important');
                el.style.setProperty('color', 'transparent', 'important');
                el.style.setProperty('border-radius', '3px', 'important');
                n++;
            }
        } catch (e) {}
    }
    return n;
}
"""

_UNMASK_ALL_JS = """
(els) => {
    for (const el of els) {
        try {
            if (el.dataset.req2qaPrevStyle !== undefined) {
                if (el.dataset.req2qaPrevStyle === '') el.removeAttribute('style');
                else el.setAttribute('style', el.dataset.req2qaPrevStyle);
                delete el.dataset.req2qaPrevStyle;
            }
        } catch (e) {}
    }
}
"""


def _mask_critical_fields(page):
    """Opaquely mask every visible critical-category field before a
    screenshot; call _unmask() right after. Walks every visible frame
    (iframes, e.g. SSO/IdP logins) and pierces open shadow roots via
    Playwright's locator engine; the mask is inline style so it also
    renders inside shadow DOM. See the note above _MASK_ALL_JS for why this
    is a single call per frame. No element cap: masking is a security
    control, not an agent-facing budget. Never raises."""
    marked = 0
    for frame in _visible_frames(page):
        try:
            marked += frame.locator(_MASK_FIELD_SELECTOR).evaluate_all(_MASK_ALL_JS, CRITICAL_FIELD_KEYWORDS + EVIDENCE_SENSITIVE_KEYWORDS)
        except Exception:
            # A frame navigating away / detached - nothing left to mask there.
            continue
    return marked


def _unmask(page):
    for frame in _visible_frames(page):
        try:
            frame.locator(_MASK_FIELD_SELECTOR).evaluate_all(_UNMASK_ALL_JS)
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
                "clause_evidence": {
                    "type": "array",
                    "description": (
                        "REQUIRED for PASS. Split the expected result into its individual checkable clauses and give one "
                        "entry per clause: the clause, what you actually OBSERVED on the page (a value, message, option "
                        "list or screen state you saw - not an assumption or how the system 'would' behave), and status. "
                        "PASS is only accepted when every clause is 'met' with observed evidence."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "clause": {"type": "string"},
                            "observed": {"type": "string"},
                            "status": {"type": "string", "enum": ["met", "not_met", "not_checked"]},
                        },
                        "required": ["clause", "observed", "status"],
                    },
                },
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
- Verdicts must rest on what you OBSERVED, never on assumptions. If the test's precondition cannot be met in this environment (e.g. it requires a different user role than the one you are logged in as, or data that does not exist), call finish_test with BLOCKED and name the unmet precondition - never PASS by reasoning about how the system "would" behave for a different role or setup.
- If you use a different control or method than the steps specify (e.g. type-ahead instead of a lookup popup), say so in your notes. That step only counts as met if your substitute exercises the same behavior the step is testing; otherwise the verdict cannot be PASS.
- For PASS, fill clause_evidence: one entry per clause of the expected result, each with what you actually observed. A clause you did not directly observe is 'not_checked', and then the verdict is not PASS. Dropdowns in the snapshot list their options - use those to verify option lists rather than trial-and-error selection.
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
            notes_for_log = None
            usage_totals = {"calls": 0, "input": 0, "cache_read": 0, "cache_write": 0, "output": 0, "max_call_input": 0}
            last_fingerprint = None
            stall_count = 0
            # Lazily created on the first upload_file call and reused for
            # any further ones in this same test case - one generated file
            # per run is all a single scenario needs.
            attachment_path: list = [None]
            # PASS review gate state - see ENABLE_PASS_REVIEW note at the top.
            # step_limit only ever grows, once, on the PASS-review path.
            step_limit = MAX_AGENT_STEPS
            pass_review_done = False
            pass_review_step = None
            first_pass_notes = None
            # Fix 3 (TC-002, 2026-09-24): the evidence shown at the first PASS
            # can go stale if the agent keeps acting afterwards (in TC-002 it
            # typed "EDITED" into Number AFTER being shown Number=INC0010006,
            # then PASSed claiming nothing changed). The harness - not the
            # model - now diffs the live form against what was last shown.
            last_shown_fields = None
            pass_recheck_done = False
            recheck_changes = []
            clause_rejections = 0
            # Test-case text used to exempt quoted test data from the wording checks.
            tc_text = " ".join(str(test_case.get(k, "")) for k in ("title", "precondition", "steps", "expected_result"))
            accepted_clause_evidence = None

            for step_num in range(MAX_AGENT_STEPS + PASS_REVIEW_EXTRA_STEPS):
                if step_num >= step_limit:
                    break
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
                messages_to_send = _messages_for_model(messages, HISTORY_KEEP_SNAPSHOTS)
                est_tokens = 0
                if TPM_BUDGET > 0:
                    try:
                        est_tokens = (len(system_prompt) + len(json.dumps(messages_to_send, default=str))) // 4 + TOOLS_TOKEN_ESTIMATE
                    except Exception:
                        est_tokens = 0
                    paced = _TPM_PACER.wait_turn(est_tokens, TPM_BUDGET)
                    if paced and rl is not None:
                        rl.event("step", {"step": step_num, "action": "tpm_pacing", "waited_s": round(paced, 1),
                                          "est_tokens": est_tokens, "budget": TPM_BUDGET})
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
                            messages=messages_to_send,
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
                # Real token usage as reported by the provider - the measurement
                # every efficiency change is judged on. Never raises.
                try:
                    u = getattr(response, "usage", None)
                    if u is not None:
                        in_t = int(getattr(u, "input_tokens", 0) or 0)
                        c_read = int(getattr(u, "cache_read_input_tokens", 0) or 0)
                        c_write = int(getattr(u, "cache_creation_input_tokens", 0) or 0)
                        out_t = int(getattr(u, "output_tokens", 0) or 0)
                        usage_totals["input"] += in_t; usage_totals["cache_read"] += c_read
                        usage_totals["cache_write"] += c_write; usage_totals["output"] += out_t
                        usage_totals["calls"] += 1
                        usage_totals["max_call_input"] = max(usage_totals["max_call_input"], in_t + c_read + c_write)
                        if TPM_BUDGET > 0 and est_tokens:
                            _TPM_PACER.correct(est_tokens, in_t + c_read + c_write)
                        if rl is not None:
                            rl.event("usage", {"step": step_num, "input": in_t, "cache_read": c_read,
                                               "cache_write": c_write, "output": out_t})
                except Exception:
                    pass
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
                            snap_stats = {}
                            elements = _snapshot_elements(page, stats=snap_stats)
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
                            elements_for_model = _elements_for_model(elements)
                            snapshot_frames = None
                            if COMPACT_SNAPSHOT_ENCODING:
                                elements_for_model, snapshot_frames = _compact_snapshot_elements(elements_for_model)
                            result_payload = {
                                "url": page.url, "title": page.title(),
                                "elements": elements_for_model,
                                "steps_remaining": step_limit - step_num - 1,
                            }
                            if snapshot_frames:
                                # frame index -> URL, listed once (COMPACT_SNAPSHOT_ENCODING)
                                result_payload["frames"] = snapshot_frames
                            if snap_stats.get("omitted_keepable") or snap_stats.get("raw_capped_frames"):
                                # Never truncate silently again: the agent must know
                                # the page has more than it is being shown.
                                result_payload["truncated"] = (
                                    "This page has more controls than could be listed "
                                    f"({snap_stats.get('omitted_keepable', 0)} usable ones omitted"
                                    + (", and part of at least one frame was too large to scan" if snap_stats.get("raw_capped_frames") else "")
                                    + "). If the "
                                    "field you need is missing, it exists but is not shown - say so "
                                    "in finish_test rather than guessing a ref."
                                )
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
                                # 2026-09-23: previously this event only recorded the
                                # URL and stall_count - when 3 test cases later went
                                # BLOCKED, the actual element list the agent was
                                # working from at each step was gone, and the only way
                                # to find out what was (or wasn't) clickable required a
                                # brand-new live re-run against the real instance. That
                                # is a bad position to be in for a customer-reported
                                # BLOCKED result too - a customer shouldn't have to
                                # reproduce a live run just so we can see what their
                                # page looked like. Logging a compact per-element
                                # summary (tag/role/label/frame, not the full snapshot
                                # payload sent to the model) makes every future run
                                # diagnosable from its own log alone.
                                rl.event("step", {
                                    "step": step_num, "action": "get_snapshot", "target": page.url,
                                    "result": "ok", "stall_count": stall_count,
                                    "element_count": len(elements_for_model),
                                    "omitted_keepable": snap_stats.get("omitted_keepable", 0),
                                    "raw_capped_frames": snap_stats.get("raw_capped_frames", 0),
                                    "elements_summary": [
                                        {
                                            "ref": e.get("ref"),
                                            "tag": e.get("tag"),
                                            "role": e.get("role"),
                                            "label": (e.get("label") or "")[:80],
                                            "frame_url": e.get("frame_url"),
                                        }
                                        for e in elements  # full list: the model's copy no longer carries frame_url
                                    ],
                                })
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
                            try:
                                pre = loc.evaluate(_CONTROL_INFO_JS)
                            except Exception:
                                pre = {}
                            if pre and not pre.get("text_entry") and not pre.get("readonly"):
                                # e.g. a read-only value rendered as plain text (common on
                                # ServiceNow for users without write access) - previously a
                                # vague "Action failed: Error".
                                result_payload = {
                                    "ok": False, "not_editable": True,
                                    "note": "This element is not a text field (it may be a read-only value shown as plain text); nothing was typed.",
                                }
                            elif pre.get("readonly"):
                                # Explicit, instant answer for "try to edit a read-only
                                # field" steps - previously fill() waited out the full
                                # action timeout and returned a vague TimeoutError.
                                result_payload = {
                                    "ok": False, "field_is_readonly": True,
                                    "value_now": _safe_value_for_model(pre.get("label", "") + " " + pre.get("field_label", ""), pre.get("value")),
                                    "note": "The field is read-only/disabled; nothing was typed.",
                                }
                            else:
                                loc.fill(inp["text"])
                                result_payload = {"ok": True, "value_now": _read_back_value(loc, pre.get("field_label", ""))}
                                if pre.get("pointer_locked"):
                                    # Not refused: some UIs overlay a custom widget on a
                                    # pointer-events:none input and fill() through it is the
                                    # normal path. The agent judges against the steps.
                                    result_payload["note"] = ("This field cannot be clicked by a mouse user (pointer-events: none); "
                                                              "the automation typed into it directly. Judge any read-only step with that in mind.")
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
                            result_payload = {"ok": True, "value_now": _read_back_value(loc, "")}
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
                        elif (name == "finish_test" and ENABLE_PASS_REVIEW and pass_review_done
                              and pass_review_step == step_num):
                            # A second finish_test in the SAME model response as the
                            # one just intercepted - the agent hasn't seen the review
                            # yet, so it can't count as the confirming call. Found by
                            # the synthetic test (two PASS calls in one response
                            # bypassed the gate entirely).
                            result_payload = {"ok": False, "error": "Ignored: review the reply to your previous finish_test first, then call finish_test once."}
                        elif name == "finish_test" and ENABLE_PASS_REVIEW and inp.get("verdict") == "PASS" and not pass_review_done:
                            # First PASS: not accepted yet. Reply with the original
                            # steps + live evidence table and grant the one-time
                            # extra budget. verdict stays None, so if the budget is
                            # then exhausted the normal step-limit BLOCKED applies.
                            pass_review_done = True
                            pass_review_step = step_num
                            first_pass_notes = inp.get("notes", "")
                            step_limit = max(step_limit, step_num + 1 + PASS_REVIEW_EXTRA_STEPS)
                            form_fields = _capture_form_state(page)
                            last_shown_fields = form_fields
                            result_payload = _pass_review_message(test_case, form_fields)
                            step_log.append("Asked to re-check the PASS against the original steps and the live form")
                            if rl is not None:
                                # Labels + empty/filled only in the log - values stay
                                # in the report/agent context, never the run log.
                                rl.event("step", {
                                    "step": step_num, "action": "pass_review", "first_pass_notes": first_pass_notes,
                                    "new_step_limit": step_limit,
                                    "fields": [{"label": f["label"], "filled": f["value"] != "(empty)"} for f in form_fields],
                                })
                        elif (name == "finish_test" and ENABLE_PASS_REVIEW and inp.get("verdict") == "PASS"
                              and pass_review_done and not pass_recheck_done and last_shown_fields is not None
                              and _form_diff(last_shown_fields, _capture_form_state(page))):
                            # The form changed after the agent was last shown it -
                            # show exactly what changed, once, before accepting PASS.
                            now_fields = _capture_form_state(page)
                            recheck_changes = _form_diff(last_shown_fields, now_fields)
                            last_shown_fields = now_fields
                            pass_recheck_done = True
                            step_limit = max(step_limit, step_num + 1 + PASS_RECHECK_EXTRA_STEPS)
                            result_payload = {
                                "ok": False, "recheck_required": True,
                                "message": (
                                    "PASS not accepted yet: these form fields CHANGED after you were shown the "
                                    "form (values read directly from the page by the harness, not from memory). "
                                    "Check each change against the original steps and expected result. If a change "
                                    "means a step or expected result was NOT met (e.g. a field that should be "
                                    "read-only accepted an edit), call finish_test with FAIL and say so. Your next "
                                    "finish_test is final; if you still call PASS, these changes will be listed in "
                                    "the report next to your verdict."
                                ),
                                "changed_fields": recheck_changes,
                                "original_steps": test_case.get("steps", ""),
                                "original_expected_result": test_case.get("expected_result", ""),
                            }
                            step_log.append("Asked to re-check a PASS because form fields changed after the review")
                            if rl is not None:
                                # Field names only - values never go to the run log.
                                rl.event("step", {"step": step_num, "action": "pass_recheck",
                                                  "changed_fields": [c["field"] for c in recheck_changes]})
                        elif (name == "finish_test" and ENABLE_CLAUSE_EVIDENCE_GATE and inp.get("verdict") == "PASS"
                              and clause_rejections < CLAUSE_EVIDENCE_MAX_REJECTIONS
                              and _clause_evidence_problems(inp.get("clause_evidence"), tc_text)):
                            problems = _clause_evidence_problems(inp.get("clause_evidence"), tc_text)
                            clause_rejections += 1
                            step_limit = max(step_limit, step_num + 1 + CLAUSE_EVIDENCE_EXTRA_STEPS)
                            result_payload = {
                                "ok": False, "evidence_required": True,
                                "problems": problems,
                                "message": (
                                    "PASS not accepted: it is not backed clause by clause by what you observed. Fix "
                                    "the problems listed (check anything you have not actually seen, using the tools), "
                                    "or call finish_test with FAIL or BLOCKED if a clause is not met or cannot be "
                                    f"verified. Attempts left before this becomes BLOCKED: {CLAUSE_EVIDENCE_MAX_REJECTIONS - clause_rejections}."
                                ),
                                "original_expected_result": test_case.get("expected_result", ""),
                            }
                            step_log.append("PASS rejected: expected result not backed clause by clause")
                            if rl is not None:
                                # Problem text so a rejection can be diagnosed from the log
                                # (2026-09-24: TC-001's BLOCKED needed guesswork from counts).
                                # Built from clause wording + flagged marker words only.
                                rl.event("step", {"step": step_num, "action": "clause_evidence_rejected",
                                                  "attempt": clause_rejections, "problem_count": len(problems),
                                                  "problems": [p_[:200] for p_ in problems[:6]]})
                        elif name == "finish_test":
                            verdict = inp["verdict"]
                            notes = inp["notes"]
                            if (verdict == "PASS" and ENABLE_CLAUSE_EVIDENCE_GATE
                                    and _clause_evidence_problems(inp.get("clause_evidence"), tc_text)):
                                # Out of rejections and still unsupported: an honest
                                # BLOCKED beats an unsupported PASS.
                                verdict = "BLOCKED"
                                notes = ("req2qa could not accept this PASS: after repeated requests the agent did not "
                                         "back every clause of the expected result with an observation. Remaining issues: "
                                         + "; ".join(_clause_evidence_problems(inp.get("clause_evidence"), tc_text)[:5])
                                         + ". Agent's notes: " + notes)
                            if verdict == "PASS":
                                accepted_clause_evidence = inp.get("clause_evidence")
                            notes_for_log = notes
                            if verdict == "PASS" and pass_review_done and last_shown_fields is not None:
                                # Anything that changed and was PASSed over - including
                                # changes already shown at the recheck - is recorded
                                # next to the verdict so a false PASS can't look clean.
                                late = _form_diff(last_shown_fields, _capture_form_state(page))
                                flagged = recheck_changes + late
                                if flagged:
                                    notes += "\n\n[req2qa harness note: after the PASS review these form fields changed, and the agent then confirmed PASS - " + "; ".join(
                                        f"{c['field']}: '{c['value_when_you_were_shown_it']}' -> '{c['value_now']}'" for c in flagged
                                    ) + ". Check these values match the test steps.]"
                                    notes_for_log += "\n\n[req2qa harness note: fields changed after PASS review: " + ", ".join(
                                        c["field"] for c in flagged) + "]"
                            if verdict == "PASS" and isinstance(inp.get("created_entity"), dict):
                                created_entity = inp["created_entity"]
                            result_payload = {"ok": True}
                            done = True
                            if rl is not None:
                                # created_entity is test-fixture data the automation itself
                                # generated (e.g. an auto-test employee name) - never a
                                # real person's data or a credential - safe to log for
                                # troubleshooting, same as notes/verdict above.
                                rl.event("step", {"step": step_num, "action": "finish_test", "verdict": verdict, "notes": notes_for_log, "created_entity": created_entity})
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
                        on_step(step_num + 1, step_limit)
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
                    if pass_review_done:
                        notes += (
                            f" This happened while re-checking a PASS against the original steps; the "
                            f"agent's first (unconfirmed) PASS said: {first_pass_notes}"
                        )
                    if rl is not None:
                        rl.event("error", {"message": "stalled - no page change", "stall_count": stall_count, "url": stuck_url})
                    break
            if verdict is None:
                # Loop ran out of budget (normal or PASS-review-extended)
                # without an accepted verdict.
                if pass_review_done:
                    notes = (
                        f"Reached the action limit while re-checking a PASS against the original steps. "
                        f"The agent's first (unconfirmed) PASS said: {first_pass_notes}"
                    )
                else:
                    notes = f"Reached the {MAX_AGENT_STEPS}-action limit for this test case without a clear result."
                verdict = "BLOCKED"
                if rl is not None:
                    rl.event("error", {"message": "step limit reached", "max_steps": step_limit})

            # Evidence table for the report: always captured, whatever the
            # verdict, so a human can compare steps vs actual page state.
            evidence_fields = _capture_form_state(page)

            screenshots.append(_capture_screenshot(page, shots_dir, "final", rl=rl, step_num=MAX_AGENT_STEPS))
            browser.close()

    except ExecutionError:
        raise
    except Exception as e:
        raise ExecutionError(f"Browser automation failed: {type(e).__name__}") from e

    return {
        "verdict": verdict or "BLOCKED",
        "notes": notes or "No verdict was reached.",
        "usage": usage_totals,
        # Same as notes, minus any field values the harness appended (run-log copy).
        "notes_for_log": notes_for_log if notes_for_log is not None else (notes or "No verdict was reached."),
        "step_log": step_log,
        "screenshots": screenshots,
        "created_entity": created_entity,
        "evidence": {
            "steps": test_case.get("steps", ""),
            "expected_result": test_case.get("expected_result", ""),
            "form_fields": evidence_fields,
            "pass_reviewed": pass_review_done,
            "clause_evidence": accepted_clause_evidence,
        },
    }
