#!/usr/bin/env python3
"""
build_test_scenarios_output.py

Turns requirement-validation + test-scenario-generation results into:
  1. A styled HTML report (validation summary, test scenarios, traceability matrix)
  2. An Excel workbook (.xlsx) with 3 sheets: Validation, Test Scenarios, Traceability Matrix

This is the output stage of the "Req2QA" workflow:
  application name + requirements file (docx/xlsx/pdf)
    -> Claude validates requirements against the named application
    -> Claude generates test scenarios per valid requirement
    -> this script renders both deliverables

No API key is required anywhere in this workflow - the validation and
generation reasoning is done by Claude directly in the chat session, not
via a separate API call. This script only formats the results Claude
already produced.

USAGE
-----
    python3 build_test_scenarios_output.py data.json report_out.html data_out.xlsx

INPUT data.json SHAPE
----------------------
{
  "application": "Salesforce Sales Cloud",
  "requirements_source": "requirements.docx",
  "run_date": "2026-09-02 15:10",
  "validation": [
    {
      "req_id": "REQ-001",
      "requirement": "System shall allow login via SSO",
      "valid_for_app": true,
      "notes": ""
    },
    {
      "req_id": "REQ-007",
      "requirement": "System shall support offline mode for 30 days",
      "valid_for_app": false,
      "notes": "Salesforce Sales Cloud has no native 30-day offline mode; needs clarification or a mobile-offline add-on."
    }
  ],
  "test_scenarios": [
    {
      "tc_id": "TC-001",
      "req_id": "REQ-001",
      "title": "Verify SSO login succeeds with valid identity provider session",
      "precondition": "User has an active SSO session with the configured IdP",
      "steps": "1. Navigate to login URL\\n2. Click 'Login with SSO'\\n3. Complete IdP redirect",
      "expected_result": "User is authenticated and lands on their home page"
    }
  ]
}

Requirements with valid_for_app = false are excluded from test generation
by design (flagged, not scenario'd) but still appear in the Validation
sheet/section and in the traceability matrix as NOT_TESTABLE.
"""
import json
import re
import zlib
import sys
import html
from collections import defaultdict
from pathlib import Path

try:
    # Normal case: imported as part of the app package (main.py does this).
    from .report_theme import (
        PALETTE_CSS, FONT_LINKS, BODY_FONT_CSS, HEADING_FONT_CSS, CHROME_CSS,
        report_topbar_html, report_footer_html,
    )
except ImportError:
    # This file is also documented as a standalone CLI script (see the
    # module docstring's USAGE section) - a relative import breaks that
    # invocation, so fall back to a plain import for that case.
    from report_theme import (
        PALETTE_CSS, FONT_LINKS, BODY_FONT_CSS, HEADING_FONT_CSS, CHROME_CSS,
        report_topbar_html, report_footer_html,
    )

try:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
except ImportError:
    print("openpyxl is required: pip install openpyxl --break-system-packages")
    raise


def esc(s):
    return html.escape(str(s if s is not None else ""))


# Excel/CSV formula injection guard: a cell value that starts with =, +, -,
# @, tab or CR is interpreted by Excel/Sheets/LibreOffice as a formula, not
# text, when the sheet is opened. Requirement text, test-step text and
# other fields below can come from a client-supplied document or from
# Claude's regenerated text, so anything landing in a cell is sanitized
# before being written rather than trusted as plain data.
_FORMULA_LEAD_CHARS = ("=", "+", "-", "@", "\t", "\r")


def xl_safe(v):
    """Prefix a leading formula-trigger character with a straight quote so
    spreadsheet apps render it as literal text. Leaves non-strings (ints,
    None) untouched; only applied to values headed into an .xlsx cell."""
    if isinstance(v, str) and v.startswith(_FORMULA_LEAD_CHARS):
        return "'" + v
    return v


def xl_row(values):
    """Apply xl_safe across a full row before ws.append()."""
    return [xl_safe(v) for v in values]


def nl2br(s):
    return esc(s).replace("\\n", "<br>").replace("\n", "<br>")


# Regexes for the two literal placeholder tokens qa_engine.py's generation
# prompt instructs the model to embed in a scenario's steps text (see
# PROMPT_TEMPLATE/CUSTOM_PROMPT_TEMPLATE there): "AutoTest_{{UNIQUE}}" (or
# similar - any text immediately followed by the {{UNIQUE}} token) and
# "{{FIXTURE:<type>.<attr>}}". Both are resolved into real values only at
# live-execution time, by execute_engine.py, which pattern-matches on these
# EXACT strings - this function must never be applied to the stored
# data.json steps string itself, only to a copy used for human display.
# Captures an optional immediately-surrounding quote character on each side
# (group 1 / group 3) so the replacement can tell a token that stands alone
# in its own quotes (e.g. 'AutoTest_{{UNIQUE}}') - the common case - apart
# from one glued into a larger literal with no quote boundary right at the
# token (e.g. user_{{UNIQUE}}@test.com, inside its own outer quotes).
_UNIQUE_TOKEN_RE = re.compile(r"(['\"])?[\w-]*\{\{UNIQUE\}\}(['\"])?")
_FIXTURE_TOKEN_RE = re.compile(r"\{\{FIXTURE:([\w.]+)\}\}")


_SAMPLE_TOKEN_WORDS = ["Jordan", "Avery", "Rowan", "Kalyani", "Devon", "Priya", "Reeve", "Sasha"]

# A name-shaped sample word (e.g. "Rowan") is wrong for a field that clearly
# isn't a name - an Employee ID, a reference/ticket/account number, a SKU,
# a code. Detecting the exact field type from arbitrary generator text was
# rejected earlier as too unreliable (there's no controlled vocabulary), but
# this is a narrower, safer check: does the ONE sentence containing the
# token mention "ID"/"code"/"SKU"/"number" as a whole word at all - a coarse
# binary signal, not a precise field-name match. When it does, use a
# code-shaped sample value instead of a name; the default (no match) stays
# a name, which is still correct for the common case (first/last name,
# email, department, etc). This is a heuristic with a safe default, not a
# guarantee - flag any other field-type mismatch you spot so the keyword
# list below can grow to cover it.
_ID_LIKE_CONTEXT_RE = re.compile(r"\b(id|code|sku|number)\b", re.IGNORECASE)
_CODE_SAMPLE_WORDS = ["EMP-1042", "REF-2087", "ID-3159", "COD-4821", "SKU-5230", "NUM-6104", "KEY-7288", "TAG-8317"]


def humanize_steps(steps_text, seed=""):
    """Display-only rewrite of a test scenario's steps text: replaces the
    raw {{UNIQUE}}/{{FIXTURE:...}} placeholder tokens (meaningful to
    execute_engine.py, meaningless-looking to a human reviewer - see
    claude/council-brainstorm-human-readable-placeholder-tokens-2026-09-19.md)
    with plain-language text, without changing anything else in the steps.
    Called at every point steps text is rendered for a human to read
    (review_generated.html, review_import.html is deliberately excluded -
    editable textarea, and all four report/xlsx builders below) - one
    function, reused everywhere, so the wording only ever needs to be right
    in one place.

    {{UNIQUE}} (always attached to some generator-invented prefix, e.g.
    "AutoTest_{{UNIQUE}}") is replaced with a plain sample value - no
    number/suffix beyond what the value itself carries (an earlier version
    appended a number to a name, e.g. "Jordan214", which is itself a
    giveaway the value is machine-generated) and no clause describing the
    generation mechanism. The sample is drawn from one of two pools -
    name-like words, or code-like values (see _ID_LIKE_CONTEXT_RE above)
    for a field whose enclosing sentence reads as an ID/code/number, so an
    Employee ID field doesn't get a person's name as its example value.
    Word/value choice is explained further under "seed" below.

    Critically, the replacement must not read as a literal instruction to
    type that exact word - a reader (especially in a downloaded report,
    where there's no separate UI element to explain this) could otherwise
    reasonably assume "Jordan" is mandatory. So whenever the token stands
    alone in its own quotes - the common case the generator produces, e.g.
    'AutoTest_{{UNIQUE}}' - the quotes are dropped and replaced with an
    inline "e.g." right at the value: 'Jordan' becomes (e.g. "Jordan"),
    so the step reads "Enter (e.g. "Jordan") in the First Name field."
    signaling "example" exactly where the reader is looking, not in a
    disconnected note elsewhere (an earlier version put a single clarifying
    sentence at the end of the whole steps text instead - dropped because
    a reader scanning line-by-line in a long downloaded report, especially
    a wrapped Excel cell, could easily miss a note that far from the
    specific line it applies to).

    When the token is instead glued into a larger literal with no quote
    boundary right at the token itself (e.g. user_{{UNIQUE}}@test.com),
    inserting "(e.g. ...)" there would break the literal, so it falls back
    to a bare word substitution in that narrower case only (tested
    explicitly below/in the test suite - this was a real bug in an earlier
    version, caught before shipping).

    {{FIXTURE:<type>.<attr>}} is replaced with a phrase describing which
    earlier record's data is being reused. When two or more FIXTURE tokens
    appear back-to-back (only whitespace between them, e.g. first_name and
    last_name of the same record), only the first gets the full "of the
    existing <type> used earlier in this batch" phrase - later adjacent
    ones are joined with "and" and just name the attribute, avoiding the
    run-on "...used earlier in this batch the last name of the existing
    ...used earlier in this batch" that a naive per-token substitution
    produces.

    Returns plain, unescaped text in all cases - callers are responsible for
    escaping/formatting downstream (nl2br()/esc() for the HTML report
    builders, Jinja's own autoescaping for the two templates), exactly as
    they already do for any other field. Never mutates or returns anything
    that gets written back to data.json - execute_engine.py must keep
    seeing the original, unmodified tokens.

    seed should be the scenario's tc_id, passed identically by every caller
    that humanizes more than one field (precondition/steps/expected_result)
    for the same test case. Word selection for {{UNIQUE}} is a deterministic
    function of (seed, the exact matched prefix+token text) rather than a
    per-call counter, because execute_engine.py's substitute_unique_token()
    substitutes the SAME real value for every {{UNIQUE}} occurrence sharing
    that literal text across an entire scenario - including across its
    different fields, not just within one field's text. A real example that
    exposed this: a scenario had 'EMP-AUTO-{{UNIQUE}}' appear identically in
    its precondition, steps, and expected_result (verifying an overridden
    Employee ID echoes back correctly) - an earlier version of this function
    picked an independent sample word for each field, so the precondition,
    steps, and expected result each showed a DIFFERENT example value for
    what is actually the same real value, which is more misleading than
    showing the raw token. Keying on the literal prefix+token text (not
    just the seed) still lets genuinely different fields in the same
    scenario - e.g. 'AutoTest_{{UNIQUE}}' for First Name vs
    'LastAutoTest_{{UNIQUE}}' for Last Name - get different sample words,
    since their literal text differs."""
    if not steps_text:
        return steps_text

    def _unique_repl(m):
        # The one sentence containing this token - used only to check for an
        # ID/code/number-like context, never to infer the exact field name.
        sent_start = steps_text.rfind(".", 0, m.start())
        sent_start = sent_start + 1 if sent_start != -1 else 0
        sent_end = steps_text.find(".", m.end())
        sent_end = sent_end if sent_end != -1 else len(steps_text)
        sentence = steps_text[sent_start:sent_end]

        pool = _CODE_SAMPLE_WORDS if _ID_LIKE_CONTEXT_RE.search(sentence) else _SAMPLE_TOKEN_WORDS
        key = f"{seed}|{m.group(0)}"
        idx = zlib.crc32(key.encode("utf-8")) % len(pool)
        word = pool[idx]
        leading_quote, trailing_quote = m.group(1), m.group(2)
        if leading_quote and trailing_quote:
            # Token stands alone in its own quotes - safe to drop them and
            # signal "example" right here.
            return f'(e.g. "{word}")'
        # Glued into a larger literal (e.g. an email local-part) - adding
        # words here would corrupt that literal, so keep it a bare word and
        # preserve whichever single quote character was actually present.
        return (leading_quote or "") + word + (trailing_quote or "")

    text = _UNIQUE_TOKEN_RE.sub(_unique_repl, steps_text)

    _last_fixture_end = [None]

    def _fixture_repl(m):
        type_attr = m.group(1)
        if "." in type_attr:
            type_name, attr_name = type_attr.split(".", 1)
        else:
            type_name, attr_name = type_attr, ""
        attr_label = attr_name.replace("_", " ")

        # Adjacent to the previous FIXTURE match (only whitespace between)?
        # Give it the short, joined form instead of repeating the full phrase.
        gap = text[_last_fixture_end[0]:m.start()] if _last_fixture_end[0] is not None else None
        adjacent = gap is not None and gap.strip() == ""
        _last_fixture_end[0] = m.end()

        if adjacent and attr_label:
            return f"and {attr_label}"
        if attr_label:
            return f"the {attr_label} of the existing {type_name} used earlier in this batch"
        return f"the existing {type_name} used earlier in this batch"

    text = _FIXTURE_TOKEN_RE.sub(_fixture_repl, text)
    return text


# --------------------------------------------------------------------------- HTML
def build_html(data):
    validation = data.get("validation", [])
    scenarios = data.get("test_scenarios", [])

    total_reqs = len(validation)
    valid_reqs = sum(1 for v in validation if v.get("valid_for_app"))
    flagged_reqs = total_reqs - valid_reqs
    total_tcs = len(scenarios)

    # traceability: req_id -> list of tc_ids
    trace = defaultdict(list)
    for s in scenarios:
        trace[s.get("req_id", "")].append(s.get("tc_id", ""))

    val_rows = []
    for v in validation:
        ok = v.get("valid_for_app")
        badge = '<span class="badge valid">VALID</span>' if ok else '<span class="badge flagged">FLAGGED</span>'
        row_cls = "row-valid" if ok else "row-flagged"
        val_rows.append(
            f'<tr class="{row_cls}"><td><strong>{esc(v.get("req_id",""))}</strong></td>'
            f'<td>{esc(v.get("requirement",""))}</td><td>{badge}</td>'
            f'<td>{esc(v.get("notes",""))}</td></tr>'
        )
    val_html = "\n".join(val_rows) if val_rows else '<tr><td colspan="4" class="empty">No requirements found.</td></tr>'

    tc_rows = []
    for s in scenarios:
        tc_rows.append(
            f'<tr><td><strong>{esc(s.get("tc_id",""))}</strong></td>'
            f'<td>{esc(s.get("req_id",""))}</td>'
            f'<td>{esc(s.get("title",""))}</td>'
            f'<td>{esc(humanize_steps(s.get("precondition", ""), s.get("tc_id", "")))}</td>'
            f'<td>{nl2br(humanize_steps(s.get("steps", ""), s.get("tc_id", "")))}</td>'
            f'<td>{esc(humanize_steps(s.get("expected_result", ""), s.get("tc_id", "")))}</td></tr>'
        )
    tc_html = "\n".join(tc_rows) if tc_rows else '<tr><td colspan="6" class="empty">No test scenarios generated.</td></tr>'

    trace_rows = []
    for v in validation:
        rid = v.get("req_id", "")
        tcs = trace.get(rid, [])
        if not v.get("valid_for_app"):
            status = '<span class="badge flagged">NOT TESTABLE</span>'
        elif tcs:
            status = '<span class="badge valid">COVERED</span>'
        else:
            status = '<span class="badge gap">GAP</span>'
        trace_rows.append(
            f'<tr><td><strong>{esc(rid)}</strong></td><td>{esc(v.get("requirement",""))}</td>'
            f'<td class="tc-ids">{esc(", ".join(tcs) if tcs else "-")}</td><td>{status}</td></tr>'
        )
    trace_html = "\n".join(trace_rows) if trace_rows else '<tr><td colspan="4" class="empty">No traceability data.</td></tr>'

    # Process Coverage Insights (Beta) - see claude/process-coverage-insights-
    # final-copy-2026-09-16.md for the locked wording this reproduces
    # verbatim. Only present when the client supplied a process diagram or
    # description alongside their requirements (paid-tier only - see
    # main.py's /analyze, where trial runs never set process_context).
    process_steps = data.get("process_steps") or []
    uncovered_process_steps = set(data.get("uncovered_process_steps", []))
    process_section = ""
    process_stat_card = ""
    if process_steps:
        frame = data.get("process_frame", "current")
        frame_word = "target" if frame == "target" else "current"
        proc_rows = []
        for st in process_steps:
            sid = st.get("step_id", "")
            gap = sid in uncovered_process_steps
            badge = '<span class="badge gap">NO TEST COVERAGE</span>' if gap else '<span class="badge valid">COVERED</span>'
            proc_rows.append(
                f'<tr><td><strong>{esc(sid)}</strong></td>'
                f'<td>{esc(st.get("screen_or_stage",""))}</td>'
                f'<td>{esc(st.get("description",""))}</td><td>{badge}</td></tr>'
            )
        proc_html = "\n".join(proc_rows) if proc_rows else '<tr><td colspan="4" class="empty">No process steps.</td></tr>'
        process_section = f"""
<h2 class="st">Process Coverage Insights <span class="pill beta">BETA</span></h2>
<div class="callout">
<strong>&#9888; Process Coverage Insights (Beta)</strong> &mdash; These are process steps we found no test
coverage for, based on the {esc(data.get("process_source","diagram/description"))} you provided as
your {frame_word} process. Some of these may be intentional &mdash; handled manually, by policy, or
by a system outside this run &mdash; so please confirm before treating any of these as a real gap.
This reflects only the process shown in what you provided, not your full production process, and
it's meant to support your own QA review, not replace it.
</div>
<div class="tw"><table><thead><tr>
<th style="width:90px">Step ID</th><th style="width:20%">Screen / Stage</th>
<th>Description</th><th style="width:160px">Coverage</th>
</tr></thead><tbody>
{proc_html}
</tbody></table></div>
"""
        process_stat_card = (
            f'<div class="card beta"><div class="n">{len(uncovered_process_steps)}</div>'
            f'<div class="l">Process Steps to Review<br><span style="font-size:10px">'
            f'of {len(process_steps)} total &mdash; see Process Coverage Insights below</span></div></div>'
        )

    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Test Scenarios &amp; Traceability Report</title>
{FONT_LINKS}
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
{PALETTE_CSS}
body{{{BODY_FONT_CSS}background:var(--bg);color:var(--text);font-size:14px;line-height:1.6}}
{CHROME_CSS}
.wrap{{max-width:1300px;margin:0 auto;padding:32px 24px 64px}}
.hdr{{background:var(--navy);color:#fff;border-radius:12px;padding:36px 40px;margin-bottom:28px}}
.hdr h1{{{HEADING_FONT_CSS}font-size:24px;font-weight:700}}
.hdr .sub{{color:#C7CEBB;font-size:13px;margin-top:6px}}
.hdr .meta{{display:flex;gap:32px;margin-top:20px;flex-wrap:wrap}}
.mi{{font-size:12px;color:#C7CEBB}}.mi strong{{display:block;color:#fff;font-size:13px;margin-bottom:2px}}
.sc{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:14px;margin-bottom:32px}}
.card{{background:var(--cream-card);border:1px solid var(--border);border-radius:10px;padding:20px 14px;text-align:center}}
.card .n{{font-size:34px;font-weight:700;line-height:1.1}}.card .l{{font-size:12px;color:var(--muted);margin-top:4px}}
.card.good .n{{color:var(--green)}}.card.bad .n{{color:var(--red)}}
.card.warn .n{{color:var(--amber)}}.card.info .n{{color:var(--blue)}}
.st{{{HEADING_FONT_CSS}font-size:16px;font-weight:700;color:var(--navy);margin:36px 0 14px;padding-bottom:10px;
border-bottom:2px solid var(--border);display:flex;align-items:center;gap:10px}}
.pill{{font-family:'Inter',sans-serif;font-size:11px;font-weight:600;background:var(--sky);color:var(--blue);padding:2px 10px;border-radius:20px}}
.tw{{overflow-x:auto;border-radius:10px;border:1px solid var(--border);margin-bottom:8px}}
table{{width:100%;border-collapse:collapse;background:var(--cream-card);font-size:13px}}
thead th{{background:var(--navy);color:#fff;padding:11px 14px;text-align:left;font-weight:600;font-size:12px;white-space:nowrap}}
thead th:first-child{{border-radius:9px 0 0 0}}thead th:last-child{{border-radius:0 9px 0 0}}
tbody tr{{border-bottom:1px solid var(--border)}}tbody tr:last-child{{border-bottom:none}}
tbody tr:hover{{background:var(--sky)}}tbody td{{padding:10px 14px;vertical-align:top}}
.row-valid{{background:var(--gbg)}}.row-flagged{{background:var(--abg)}}
.tc-ids{{font-family:monospace;font-size:12px;color:var(--blue)}}
.empty{{text-align:center;color:var(--muted);padding:24px;font-style:italic}}
.badge{{display:inline-block;font-size:11px;font-weight:600;padding:2px 9px;border-radius:4px;white-space:nowrap}}
.badge.valid{{background:var(--gbg);color:var(--green)}}.badge.flagged{{background:var(--abg);color:var(--amber)}}
.badge.gap{{background:var(--rbg);color:var(--red)}}
.ft{{margin-top:48px;text-align:center;font-size:12px;color:var(--muted);
border-top:1px solid var(--border);padding-top:20px}}
.callout{{background:var(--pbg);border-left:3px solid var(--purple);border-radius:8px;padding:14px 18px;
font-size:12.5px;color:var(--text);margin-bottom:14px;line-height:1.55}}
.pill.beta{{background:var(--pbg);color:var(--purple)}}
.card.beta .n{{color:var(--purple)}}
@media print{{.tw{{overflow:visible}}body{{background:#fff}}}}
</style></head><body>
{report_topbar_html()}
<div class="wrap">
<header class="hdr"><h1>Test Scenarios &amp; Requirements Traceability Report</h1>
<p class="sub">Application-aware requirement validation and scenario generation</p>
<div class="meta">
<div class="mi"><strong>Application</strong>{esc(data.get("application",""))}</div>
<div class="mi"><strong>Requirements Source</strong>{esc(data.get("requirements_source",""))}</div>
<div class="mi"><strong>Run Date</strong>{esc(data.get("run_date",""))}</div>
{f'<div class="mi"><strong>Baseline</strong>{esc(data.get("baseline_version"))}</div>' if data.get("baseline_version") else ""}
</div></header>

<div class="sc">
<div class="card info"><div class="n">{total_reqs}</div><div class="l">Total Requirements</div></div>
<div class="card good"><div class="n">{valid_reqs}</div><div class="l">Valid for Application</div></div>
<div class="card warn"><div class="n">{flagged_reqs}</div><div class="l">Flagged / Needs Review</div></div>
<div class="card good"><div class="n">{total_tcs}</div><div class="l">Test Scenarios Generated</div></div>
{process_stat_card}
</div>
{process_section}
<h2 class="st">Requirement Validation <span class="pill">TABLE 1</span></h2>
<div class="tw"><table><thead><tr>
<th style="width:100px">Req ID</th><th style="width:40%">Requirement</th>
<th style="width:110px">Status</th><th>Notes</th>
</tr></thead><tbody>
{val_html}
</tbody></table></div>

<h2 class="st">Generated Test Scenarios <span class="pill">TABLE 2</span></h2>
<div class="tw"><table><thead><tr>
<th style="width:90px">TC ID</th><th style="width:90px">Req ID</th>
<th style="width:22%">Title</th><th style="width:18%">Precondition</th>
<th>Steps</th><th style="width:18%">Expected Result</th>
</tr></thead><tbody>
{tc_html}
</tbody></table></div>

<h2 class="st">Requirement &harr; Test Case Traceability Matrix <span class="pill">TABLE 3</span></h2>
<div class="tw"><table><thead><tr>
<th style="width:100px">Req ID</th><th style="width:35%">Requirement</th>
<th style="width:20%">Linked TC IDs</th><th style="width:130px">Status</th>
</tr></thead><tbody>
{trace_html}
</tbody></table></div>

<div class="ft">Req2QA &mdash; Requirements to Test Coverage &bull; {esc(data.get("run_date",""))}</div>
</div>
{report_footer_html()}
</body></html>"""


# --------------------------------------------------------------------------- XLSX
HEADER_FILL = PatternFill("solid", fgColor="2B3A2A")  # matches --navy in report_theme.py/style.css (was 1F4E79, an off-brand generic navy)
HEADER_FONT = Font(bold=True, color="FFFFFF")
GOOD_FILL = PatternFill("solid", fgColor="DCFCE7")
WARN_FILL = PatternFill("solid", fgColor="FEF3C7")
BAD_FILL = PatternFill("solid", fgColor="FEE2E2")


def style_header(ws, ncols):
    for c in range(1, ncols + 1):
        cell = ws.cell(row=1, column=c)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.row_dimensions[1].height = 22
    ws.freeze_panes = "A2"


def autosize(ws, widths):
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w


def build_xlsx(data, out_path):
    wb = Workbook()

    # Sheet 1: Validation
    ws1 = wb.active
    ws1.title = "Validation"
    ws1.append(["Req ID", "Requirement", "Valid for Application", "Notes"])
    for v in data.get("validation", []):
        ok = v.get("valid_for_app")
        ws1.append(xl_row([v.get("req_id", ""), v.get("requirement", ""), "YES" if ok else "FLAGGED", v.get("notes", "")]))
        r = ws1.max_row
        fill = GOOD_FILL if ok else WARN_FILL
        for c in range(1, 5):
            ws1.cell(row=r, column=c).fill = fill
            ws1.cell(row=r, column=c).alignment = Alignment(wrap_text=True, vertical="top")
    style_header(ws1, 4)
    autosize(ws1, [12, 55, 20, 45])

    # Sheet 2: Test Scenarios
    ws2 = wb.create_sheet("Test Scenarios")
    ws2.append(["TC ID", "Req ID", "Title", "Precondition", "Steps", "Expected Result"])
    for s in data.get("test_scenarios", []):
        ws2.append(xl_row([
            s.get("tc_id", ""), s.get("req_id", ""), s.get("title", ""),
            humanize_steps(s.get("precondition", ""), s.get("tc_id", "")), humanize_steps(s.get("steps", ""), s.get("tc_id", "")).replace("\\n", "\n"),
            humanize_steps(s.get("expected_result", ""), s.get("tc_id", "")),
        ]))
        r = ws2.max_row
        for c in range(1, 7):
            ws2.cell(row=r, column=c).alignment = Alignment(wrap_text=True, vertical="top")
    style_header(ws2, 6)
    autosize(ws2, [10, 10, 32, 28, 45, 32])

    # Sheet 3: Traceability Matrix
    ws3 = wb.create_sheet("Traceability Matrix")
    ws3.append(["Req ID", "Requirement", "Linked TC IDs", "Status"])
    trace = defaultdict(list)
    for s in data.get("test_scenarios", []):
        trace[s.get("req_id", "")].append(s.get("tc_id", ""))
    for v in data.get("validation", []):
        rid = v.get("req_id", "")
        tcs = trace.get(rid, [])
        if not v.get("valid_for_app"):
            status, fill = "NOT_TESTABLE", WARN_FILL
        elif tcs:
            status, fill = "COVERED", GOOD_FILL
        else:
            status, fill = "GAP", BAD_FILL
        ws3.append(xl_row([rid, v.get("requirement", ""), ", ".join(tcs), status]))
        r = ws3.max_row
        for c in range(1, 5):
            ws3.cell(row=r, column=c).fill = fill
            ws3.cell(row=r, column=c).alignment = Alignment(wrap_text=True, vertical="top")
    style_header(ws3, 4)
    autosize(ws3, [12, 55, 25, 16])

    # Sheet 4 (optional): Process Coverage Insights (Beta)
    process_steps = data.get("process_steps") or []
    if process_steps:
        uncovered_process_steps = set(data.get("uncovered_process_steps", []))
        ws4 = wb.create_sheet("Process Coverage (Beta)")
        ws4.append(["Step ID", "Screen / Stage", "Description", "Coverage"])
        for st in process_steps:
            sid = st.get("step_id", "")
            gap = sid in uncovered_process_steps
            status, fill = ("NO TEST COVERAGE", WARN_FILL) if gap else ("COVERED", GOOD_FILL)
            ws4.append(xl_row([sid, st.get("screen_or_stage", ""), st.get("description", ""), status]))
            r = ws4.max_row
            for c in range(1, 5):
                ws4.cell(row=r, column=c).fill = fill
                ws4.cell(row=r, column=c).alignment = Alignment(wrap_text=True, vertical="top")
        style_header(ws4, 4)
        autosize(ws4, [12, 25, 55, 20])

    wb.save(out_path)


# --------------------------------------------------------------------------- Option B (custom apps): 3-way traceability
def build_html_custom(data, flow):
    """Custom Application Mode report: consistency/testability validation
    plus three-way traceability (requirement <-> process step <-> test
    case), using the same generation output run_qa_analysis_custom()
    produces. Kept as a separate function from build_html() rather than
    branching inside it, per the design decision to keep Option A untouched."""
    validation = data.get("validation", [])
    scenarios = data.get("test_scenarios", [])
    flow_steps = flow.get("steps", [])
    uncovered_flow_steps = set(data.get("uncovered_flow_steps", []))

    total_reqs = len(validation)
    testable_reqs = sum(1 for v in validation if v.get("testable"))
    flagged_reqs = total_reqs - testable_reqs
    total_tcs = len(scenarios)
    total_flow_steps = len(flow_steps)

    trace = defaultdict(list)  # req_id -> [tc_id]
    for s in scenarios:
        trace[s.get("req_id", "")].append(s.get("tc_id", ""))

    val_rows = []
    for v in validation:
        ok = v.get("testable")
        badge = '<span class="badge valid">TESTABLE</span>' if ok else '<span class="badge flagged">FLAGGED</span>'
        row_cls = "row-valid" if ok else "row-flagged"
        step_ids = ", ".join(v.get("flow_step_ids", [])) or "-"
        val_rows.append(
            f'<tr class="{row_cls}"><td><strong>{esc(v.get("req_id",""))}</strong></td>'
            f'<td>{esc(v.get("requirement",""))}</td><td>{badge}</td>'
            f'<td class="tc-ids">{esc(step_ids)}</td><td>{esc(v.get("notes",""))}</td></tr>'
        )
    val_html = "\n".join(val_rows) if val_rows else '<tr><td colspan="5" class="empty">No requirements found.</td></tr>'

    tc_rows = []
    for s in scenarios:
        tc_rows.append(
            f'<tr><td><strong>{esc(s.get("tc_id",""))}</strong></td>'
            f'<td>{esc(s.get("req_id",""))}</td>'
            f'<td class="tc-ids">{esc(", ".join(s.get("flow_step_ids", [])) or "-")}</td>'
            f'<td>{esc(s.get("title",""))}</td>'
            f'<td>{esc(humanize_steps(s.get("precondition", ""), s.get("tc_id", "")))}</td>'
            f'<td>{nl2br(humanize_steps(s.get("steps", ""), s.get("tc_id", "")))}</td><td>{esc(humanize_steps(s.get("expected_result", ""), s.get("tc_id", "")))}</td></tr>'
        )
    tc_html = "\n".join(tc_rows) if tc_rows else '<tr><td colspan="7" class="empty">No test scenarios generated.</td></tr>'

    # Three-way trace: one row per requirement plus one row per otherwise-uncovered flow step
    trace_rows = []
    for v in validation:
        rid = v.get("req_id", "")
        tcs = trace.get(rid, [])
        step_ids = v.get("flow_step_ids", [])
        if not v.get("testable"):
            status = '<span class="badge flagged">NOT TESTABLE</span>'
        elif not step_ids:
            status = '<span class="badge gap">NO FLOW STEP</span>'
        elif tcs:
            status = '<span class="badge valid">COVERED</span>'
        else:
            status = '<span class="badge gap">GAP</span>'
        trace_rows.append(
            f'<tr><td><strong>{esc(rid)}</strong></td><td>{esc(v.get("requirement",""))}</td>'
            f'<td class="tc-ids">{esc(", ".join(step_ids) or "-")}</td>'
            f'<td class="tc-ids">{esc(", ".join(tcs) if tcs else "-")}</td><td>{status}</td></tr>'
        )
    step_lookup = {st.get("step_id", ""): st for st in flow_steps}
    for step_id in sorted(uncovered_flow_steps):
        st = step_lookup.get(step_id, {})
        trace_rows.append(
            f'<tr class="row-flagged"><td>-</td>'
            f'<td><em>{esc(st.get("screen_or_stage", step_id))}</em> - flow step with no requirement</td>'
            f'<td class="tc-ids">{esc(step_id)}</td><td class="tc-ids">-</td>'
            f'<td><span class="badge gap">UNREQUESTED STEP</span></td></tr>'
        )
    trace_html = "\n".join(trace_rows) if trace_rows else '<tr><td colspan="5" class="empty">No traceability data.</td></tr>'

    flow_rows = []
    for st in flow_steps:
        gap_badge = '<span class="badge gap">NO REQUIREMENT</span>' if st.get("step_id") in uncovered_flow_steps else '<span class="badge valid">COVERED</span>'
        flow_rows.append(
            f'<tr><td><strong>{esc(st.get("step_id",""))}</strong></td>'
            f'<td>{esc(st.get("screen_or_stage",""))}</td>'
            f'<td>{esc(st.get("description",""))}</td>'
            f'<td>{"Yes - " + esc(st.get("decision_detail","")) if st.get("decision_point") else "No"}</td>'
            f'<td>{gap_badge}</td></tr>'
        )
    flow_html = "\n".join(flow_rows) if flow_rows else '<tr><td colspan="5" class="empty">No process flow steps.</td></tr>'

    baseline_html = f'<div class="mi"><strong>Baseline</strong>{esc(data.get("baseline_version"))}</div>' if data.get("baseline_version") else ""

    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Custom Application - Test Coverage Report</title>
{FONT_LINKS}
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
{PALETTE_CSS}
body{{{BODY_FONT_CSS}background:var(--bg);color:var(--text);font-size:14px;line-height:1.6}}
{CHROME_CSS}
.wrap{{max-width:1300px;margin:0 auto;padding:32px 24px 64px}}
.hdr{{background:var(--navy);color:#fff;border-radius:12px;padding:36px 40px;margin-bottom:28px}}
.hdr h1{{{HEADING_FONT_CSS}font-size:24px;font-weight:700}}
.hdr .sub{{color:#C7CEBB;font-size:13px;margin-top:6px}}
.hdr .meta{{display:flex;gap:32px;margin-top:20px;flex-wrap:wrap}}
.mi{{font-size:12px;color:#C7CEBB}}.mi strong{{display:block;color:#fff;font-size:13px;margin-bottom:2px}}
.sc{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:14px;margin-bottom:32px}}
.card{{background:var(--cream-card);border:1px solid var(--border);border-radius:10px;padding:20px 14px;text-align:center}}
.card .n{{font-size:34px;font-weight:700;line-height:1.1}}.card .l{{font-size:12px;color:var(--muted);margin-top:4px}}
.card.good .n{{color:var(--green)}}.card.bad .n{{color:var(--red)}}
.card.warn .n{{color:var(--amber)}}.card.info .n{{color:var(--blue)}}
.st{{{HEADING_FONT_CSS}font-size:16px;font-weight:700;color:var(--navy);margin:36px 0 14px;padding-bottom:10px;
border-bottom:2px solid var(--border);display:flex;align-items:center;gap:10px}}
.pill{{font-family:'Inter',sans-serif;font-size:11px;font-weight:600;background:var(--sky);color:var(--blue);padding:2px 10px;border-radius:20px}}
.tw{{overflow-x:auto;border-radius:10px;border:1px solid var(--border);margin-bottom:8px}}
table{{width:100%;border-collapse:collapse;background:var(--cream-card);font-size:13px}}
thead th{{background:var(--navy);color:#fff;padding:11px 14px;text-align:left;font-weight:600;font-size:12px;white-space:nowrap}}
thead th:first-child{{border-radius:9px 0 0 0}}thead th:last-child{{border-radius:0 9px 0 0}}
tbody tr{{border-bottom:1px solid var(--border)}}tbody tr:last-child{{border-bottom:none}}
tbody tr:hover{{background:var(--sky)}}tbody td{{padding:10px 14px;vertical-align:top}}
.row-valid{{background:var(--gbg)}}.row-flagged{{background:var(--abg)}}
.tc-ids{{font-family:monospace;font-size:12px;color:var(--blue)}}
.empty{{text-align:center;color:var(--muted);padding:24px;font-style:italic}}
.badge{{display:inline-block;font-size:11px;font-weight:600;padding:2px 9px;border-radius:4px;white-space:nowrap}}
.badge.valid{{background:var(--gbg);color:var(--green)}}.badge.flagged{{background:var(--abg);color:var(--amber)}}
.badge.gap{{background:var(--rbg);color:var(--red)}}
.ft{{margin-top:48px;text-align:center;font-size:12px;color:var(--muted);
border-top:1px solid var(--border);padding-top:20px}}
.callout{{background:var(--pbg);border-left:3px solid var(--purple);border-radius:8px;padding:14px 18px;
font-size:12.5px;color:var(--text);margin-bottom:14px;line-height:1.55}}
.pill.beta{{background:var(--pbg);color:var(--purple)}}
.card.beta .n{{color:var(--purple)}}
@media print{{.tw{{overflow:visible}}body{{background:#fff}}}}
</style></head><body>
{report_topbar_html()}
<div class="wrap">
<header class="hdr"><h1>Custom Application &mdash; Test Coverage Report</h1>
<p class="sub">Consistency/testability check &amp; three-way traceability (requirement &harr; process step &harr; test case)</p>
<div class="meta">
<div class="mi"><strong>Application</strong>{esc(data.get("application",""))}</div>
<div class="mi"><strong>Process Flow</strong>{esc(flow.get("flow_name","Main flow"))}</div>
<div class="mi"><strong>Run Date</strong>{esc(data.get("run_date",""))}</div>
{baseline_html}
</div></header>

<div class="sc">
<div class="card info"><div class="n">{total_reqs}</div><div class="l">Total Requirements</div></div>
<div class="card good"><div class="n">{testable_reqs}</div><div class="l">Testable</div></div>
<div class="card warn"><div class="n">{flagged_reqs}</div><div class="l">Flagged / Needs Review</div></div>
<div class="card info"><div class="n">{total_flow_steps}</div><div class="l">Process Flow Steps</div></div>
<div class="card good"><div class="n">{total_tcs}</div><div class="l">Test Scenarios Generated</div></div>
<div class="card bad"><div class="n">{len(uncovered_flow_steps)}</div><div class="l">Flow Steps With No Requirement</div></div>
</div>

<h2 class="st">Confirmed Process Flow <span class="pill">REFERENCE</span></h2>
<div class="tw"><table><thead><tr>
<th style="width:90px">Step ID</th><th style="width:18%">Screen / Stage</th>
<th>Description</th><th style="width:22%">Decision Point</th><th style="width:140px">Coverage</th>
</tr></thead><tbody>
{flow_html}
</tbody></table></div>

<h2 class="st">Requirement Validation (Consistency &amp; Testability) <span class="pill">TABLE 1</span></h2>
<div class="tw"><table><thead><tr>
<th style="width:100px">Req ID</th><th style="width:35%">Requirement</th>
<th style="width:110px">Status</th><th style="width:120px">Flow Step(s)</th><th>Notes</th>
</tr></thead><tbody>
{val_html}
</tbody></table></div>

<h2 class="st">Generated Test Scenarios <span class="pill">TABLE 2</span></h2>
<div class="tw"><table><thead><tr>
<th style="width:90px">TC ID</th><th style="width:90px">Req ID</th><th style="width:100px">Flow Step(s)</th>
<th style="width:20%">Title</th><th style="width:16%">Precondition</th>
<th>Steps</th><th style="width:16%">Expected Result</th>
</tr></thead><tbody>
{tc_html}
</tbody></table></div>

<h2 class="st">Three-Way Traceability &mdash; Requirement &harr; Flow Step &harr; Test Case <span class="pill">TABLE 3</span></h2>
<div class="tw"><table><thead><tr>
<th style="width:100px">Req ID</th><th style="width:30%">Requirement</th>
<th style="width:110px">Flow Step(s)</th><th style="width:110px">Linked TC IDs</th><th style="width:160px">Status</th>
</tr></thead><tbody>
{trace_html}
</tbody></table></div>

<div class="ft">Req2QA &mdash; Custom Application Mode &bull; {esc(data.get("run_date",""))}</div>
</div>
{report_footer_html()}
</body></html>"""


def build_xlsx_custom(data, flow, out_path):
    wb = Workbook()
    flow_steps = flow.get("steps", [])
    uncovered_flow_steps = set(data.get("uncovered_flow_steps", []))

    # Sheet 1: Process Flow (reference)
    ws0 = wb.active
    ws0.title = "Process Flow"
    ws0.append(["Step ID", "Screen / Stage", "Description", "Inputs", "Decision Point", "Decision Detail", "Coverage"])
    for st in flow_steps:
        covered = "NO REQUIREMENT" if st.get("step_id") in uncovered_flow_steps else "COVERED"
        ws0.append(xl_row([
            st.get("step_id", ""), st.get("screen_or_stage", ""), st.get("description", ""),
            ", ".join(st.get("inputs", [])), "YES" if st.get("decision_point") else "NO",
            st.get("decision_detail", ""), covered,
        ]))
        r = ws0.max_row
        fill = BAD_FILL if covered == "NO REQUIREMENT" else GOOD_FILL
        for c in range(1, 8):
            ws0.cell(row=r, column=c).fill = fill
            ws0.cell(row=r, column=c).alignment = Alignment(wrap_text=True, vertical="top")
    style_header(ws0, 7)
    autosize(ws0, [12, 22, 40, 25, 14, 30, 16])

    # Sheet 2: Validation
    ws1 = wb.create_sheet("Validation")
    ws1.append(["Req ID", "Requirement", "Testable", "Flow Step(s)", "Notes"])
    for v in data.get("validation", []):
        ok = v.get("testable")
        ws1.append(xl_row([
            v.get("req_id", ""), v.get("requirement", ""), "YES" if ok else "FLAGGED",
            ", ".join(v.get("flow_step_ids", [])), v.get("notes", ""),
        ]))
        r = ws1.max_row
        fill = GOOD_FILL if ok else WARN_FILL
        for c in range(1, 6):
            ws1.cell(row=r, column=c).fill = fill
            ws1.cell(row=r, column=c).alignment = Alignment(wrap_text=True, vertical="top")
    style_header(ws1, 5)
    autosize(ws1, [12, 50, 14, 18, 40])

    # Sheet 3: Test Scenarios
    ws2 = wb.create_sheet("Test Scenarios")
    ws2.append(["TC ID", "Req ID", "Flow Step(s)", "Title", "Precondition", "Steps", "Expected Result"])
    for s in data.get("test_scenarios", []):
        ws2.append(xl_row([
            s.get("tc_id", ""), s.get("req_id", ""), ", ".join(s.get("flow_step_ids", [])),
            s.get("title", ""), humanize_steps(s.get("precondition", ""), s.get("tc_id", "")),
            humanize_steps(s.get("steps", ""), s.get("tc_id", "")).replace("\\n", "\n"), humanize_steps(s.get("expected_result", ""), s.get("tc_id", "")),
        ]))
        r = ws2.max_row
        for c in range(1, 8):
            ws2.cell(row=r, column=c).alignment = Alignment(wrap_text=True, vertical="top")
    style_header(ws2, 7)
    autosize(ws2, [10, 10, 16, 30, 26, 45, 30])

    # Sheet 4: Three-Way Traceability Matrix
    ws3 = wb.create_sheet("Traceability Matrix")
    ws3.append(["Req ID", "Requirement", "Flow Step(s)", "Linked TC IDs", "Status"])
    trace = defaultdict(list)
    for s in data.get("test_scenarios", []):
        trace[s.get("req_id", "")].append(s.get("tc_id", ""))
    for v in data.get("validation", []):
        rid = v.get("req_id", "")
        tcs = trace.get(rid, [])
        step_ids = v.get("flow_step_ids", [])
        if not v.get("testable"):
            status, fill = "NOT_TESTABLE", WARN_FILL
        elif not step_ids:
            status, fill = "NO_FLOW_STEP", BAD_FILL
        elif tcs:
            status, fill = "COVERED", GOOD_FILL
        else:
            status, fill = "GAP", BAD_FILL
        ws3.append(xl_row([rid, v.get("requirement", ""), ", ".join(step_ids), ", ".join(tcs), status]))
        r = ws3.max_row
        for c in range(1, 6):
            ws3.cell(row=r, column=c).fill = fill
            ws3.cell(row=r, column=c).alignment = Alignment(wrap_text=True, vertical="top")
    step_lookup = {st.get("step_id", ""): st for st in flow_steps}
    for step_id in sorted(uncovered_flow_steps):
        st = step_lookup.get(step_id, {})
        ws3.append(xl_row(["-", f"{st.get('screen_or_stage', step_id)} - flow step with no requirement", step_id, "-", "UNREQUESTED_STEP"]))
        r = ws3.max_row
        for c in range(1, 6):
            ws3.cell(row=r, column=c).fill = BAD_FILL
            ws3.cell(row=r, column=c).alignment = Alignment(wrap_text=True, vertical="top")
    style_header(ws3, 5)
    autosize(ws3, [12, 45, 16, 20, 18])

    wb.save(out_path)


def main():
    if len(sys.argv) != 4:
        print("Usage: python3 build_test_scenarios_output.py data.json report_out.html data_out.xlsx")
        sys.exit(1)
    data = json.loads(Path(sys.argv[1]).read_text())
    Path(sys.argv[2]).write_text(build_html(data))
    build_xlsx(data, sys.argv[3])
    print(f"HTML report -> {sys.argv[2]}")
    print(f"Excel data  -> {sys.argv[3]}")


if __name__ == "__main__":
    main()
