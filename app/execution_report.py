"""
Req2QA - Execution Report Builder

Builds the HTML report for one batch of live test-case executions:
verdict per case, plain-language step log, and the screenshot evidence
gallery (already masked for critical fields by execute_engine.py before
being saved to disk - this module never re-decides what's sensitive).
"""
import html

from .report_theme import (
    PALETTE_CSS, FONT_LINKS, BODY_FONT_CSS, HEADING_FONT_CSS, CHROME_CSS,
    report_topbar_html, report_footer_html, current_year,
)


def esc(s):
    return html.escape(str(s if s is not None else ""))


VERDICT_BADGE = {
    "PASS": ('<span class="badge valid">PASS</span>', "row-valid"),
    "FAIL": ('<span class="badge gap">FAIL</span>', "row-flagged"),
    "BLOCKED": ('<span class="badge flagged">BLOCKED</span>', "row-flagged"),
    # 2026-09-25 (Finding 1): a client-requested stop, never a test outcome -
    # deliberately its own neutral style, not reused from FAIL/BLOCKED, so a
    # cancelled case is never mistaken for a defect the tool found.
    "CANCELLED": ('<span class="badge">CANCELLED</span>', "row-cancelled"),
}

# Fraction of the screenshot cap reserved for early-run context on a
# FAIL/BLOCKED test case, before the rest goes to a tail window ending at
# the final screenshot. See _select_screenshots below.
_FAIL_CONTEXT_FRACTION = 0.2
_FAIL_CONTEXT_MIN = 2


def _even_sample_indices(total: int, k: int) -> list:
    """k evenly-spaced indices across range(total), always including the
    first and last. Used for PASS verdicts, where breadth of coverage
    across the whole run is what a reviewer wants."""
    if k <= 1:
        return [total - 1]
    step = (total - 1) / (k - 1)
    seen = []
    for i in range(k):
        idx = round(i * step)
        if idx not in seen:
            seen.append(idx)
    return seen


def _select_screenshots(screenshots: list, verdict: str, cap: int | None) -> tuple[list, str | None]:
    """Returns (selected_screenshots, disclosure_note_or_None).

    Verdict-aware, per the 2026-09-16 council review (see
    claude/... council debate on the screenshot-cap idea): even-sampling
    is right for a PASS (breadth of coverage matters), but for a FAIL or
    BLOCKED, evidentiary value concentrates near the end of the run - a
    small early-context slice plus a majority tail window ending at the
    final screenshot serves a reviewer far better than a uniform spread
    that dilutes exactly the moments that explain what went wrong.

    Purely a display filter - does not touch what execute_engine.py
    actually captured, and does not change execution cost."""
    total = len(screenshots)
    if not cap or total <= cap:
        return screenshots, None

    if verdict == "PASS":
        indices = _even_sample_indices(total, cap)
        note = f"{len(indices)} of {total} actions shown, evenly sampled across the run."
        return [screenshots[i] for i in indices], note

    # FAIL / BLOCKED (or an unrecognized verdict, treated the same way,
    # since a missing/unknown verdict is more likely an error case than a
    # clean success): small head for orientation, majority tail leading up
    # to and including the final screenshot.
    head_n = min(max(_FAIL_CONTEXT_MIN, round(cap * _FAIL_CONTEXT_FRACTION)), cap - 1)
    tail_n = cap - head_n
    head_indices = list(range(0, head_n))
    tail_start = max(head_n, total - tail_n)
    tail_indices = list(range(tail_start, total))
    indices = sorted(set(head_indices + tail_indices))
    reason = "failure" if verdict == "FAIL" else "block"
    note = f"{len(indices)} of {total} actions shown, focused on the sequence leading up to the {reason}."
    return [screenshots[i] for i in indices], note


def _evidence_html(ev) -> str:
    """Steps-vs-actual evidence table (2026-09-23, PASS review gate). Shown
    for every verdict so a reviewer can spot a skipped step in seconds.
    Values arrive already redacted by execute_engine._capture_form_state."""
    if not ev:
        return ""
    fields = ev.get("form_fields") or []
    rows = "".join(
        f'<tr><td>{esc(f.get("label",""))}</td>'
        f'<td class="{"ev-empty" if f.get("value") == "(empty)" else ""}">{esc(f.get("value",""))}</td></tr>'
        for f in fields
    ) or '<tr><td colspan="2" class="empty">No form fields visible at the end of the run.</td></tr>'
    ce = ev.get("clause_evidence") or []
    ce_html = ""
    if ce:
        ce_rows = "".join(
            f'<tr><td>{esc(str(c.get("clause","")))}</td><td>{esc(str(c.get("observed","")))}</td></tr>'
            for c in ce if isinstance(c, dict)
        )
        ce_html = f'<h4>Expected result, clause by clause (what was observed)</h4><table class="ev-table"><tr><th>Clause</th><th>Observed</th></tr>{ce_rows}</table>'
    reviewed = ('<p class="ev-note">The agent was made to re-check its PASS against these steps and the live form before it was accepted.</p>'
                if ev.get("pass_reviewed") else "")
    return f"""<details class="evidence">
    <summary>Evidence: original steps vs. what the page showed at the end</summary>
    {reviewed}
    <div class="ev-grid">
      <div><h4>Test steps</h4><pre class="ev-steps">{esc(ev.get("steps",""))}</pre>
      <h4>Expected result</h4><pre class="ev-steps">{esc(ev.get("expected_result",""))}</pre></div>
      <div>{ce_html}<h4>Form fields at end of run</h4><table class="ev-table"><tr><th>Field</th><th>Value</th></tr>{rows}</table></div>
    </div>
  </details>"""


def build_execution_report(data: dict) -> str:
    """data shape:
    {
      "application": str, "client_name": str, "run_date": str,
      "environment_label": str,  # e.g. host/domain only, never full URL with query params
      "role_label": str,
      "results": [
        {"tc_id": str, "title": str, "verdict": "PASS"/"FAIL"/"BLOCKED",
         "notes": str, "step_log": [str], "screenshots": [relative_url, ...]}
      ]
    }
    """
    results = data.get("results", [])
    total = len(results)
    passed = sum(1 for r in results if r.get("verdict") == "PASS")
    failed = sum(1 for r in results if r.get("verdict") == "FAIL")
    blocked = sum(1 for r in results if r.get("verdict") == "BLOCKED")
    cancelled = sum(1 for r in results if r.get("verdict") == "CANCELLED")
    # The report itself is served through a token-gated route; each
    # screenshot is a separate request under the same route, so the same
    # token needs to travel with each relative image URL.
    token_qs = f"?token={data['token']}" if data.get("token") else ""
    run_id, exec_id = data.get("run_id", ""), data.get("exec_id", "")
    can_zip = bool(run_id and exec_id and data.get("token"))
    run_zip_url = f"/download-exec-zip/{esc(run_id)}/{esc(exec_id)}{token_qs}" if can_zip else ""

    max_screenshots = data.get("max_screenshots")

    sections = []
    for r in results:
        badge, row_cls = VERDICT_BADGE.get(r.get("verdict"), ('<span class="badge">UNKNOWN</span>', ""))
        step_items = "".join(f"<li>{esc(s)}</li>" for s in r.get("step_log", [])) or "<li class='empty'>No steps recorded.</li>"
        shown_screenshots, sample_note = _select_screenshots(
            r.get("screenshots", []), r.get("verdict"), max_screenshots
        )
        def _shot_html(src: str) -> str:
            full_url = f"{esc(src)}{token_qs}"
            dl_name = esc(f'{r.get("tc_id","")}_{src.rsplit("/", 1)[-1]}')
            return (
                f'<div class="shot">'
                f'<a href="{full_url}" target="_blank" rel="noopener">'
                f'<img src="{full_url}" loading="lazy" alt="Execution screenshot - click to open full size"></a>'
                f'<div class="shot-hint">'
                f'<a href="{full_url}" target="_blank" rel="noopener">Open full size</a>'
                f' &bull; '
                f'<a href="{full_url}" download="{dl_name}">Download</a>'
                f'</div>'
                f'</div>'
            )

        shot_items = "".join(
            _shot_html(src) for src in shown_screenshots
        ) or "<p class='empty'>No screenshots captured.</p>"
        sample_note_html = f'<p class="shot-sample-note">{esc(sample_note)}</p>' if sample_note else ""
        tc_zip_url = f"/download-exec-zip/{esc(run_id)}/{esc(exec_id)}/{esc(r.get('tc_id',''))}{token_qs}" if can_zip and r.get("screenshots") else ""
        tc_zip_html = f'<a class="btn-zip" href="{tc_zip_url}">Download all screenshots (.zip)</a>' if tc_zip_url else ""
        sections.append(f"""
<div class="exec-case {row_cls}">
  <div class="exec-case-hdr">
    <div><strong>{esc(r.get('tc_id',''))}</strong> &mdash; {esc(r.get('title',''))}</div>
    {badge}
  </div>
  <p class="exec-notes">{esc(r.get('notes',''))}</p>
  {_evidence_html(r.get("evidence"))}
  <details>
    <summary>Step log ({len(r.get('step_log', []))} actions)</summary>
    <ol class="exec-steps">{step_items}</ol>
  </details>
  {sample_note_html}
  <div class="shot-gallery">{shot_items}</div>
  {tc_zip_html}
</div>""")

    sections_html = "\n".join(sections) if sections else '<p class="empty">No test cases were executed in this run.</p>'

    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Live Execution Report</title>
{FONT_LINKS}
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
{PALETTE_CSS}
body{{{BODY_FONT_CSS}background:var(--bg);color:var(--text);font-size:14px;line-height:1.6}}
{CHROME_CSS}
.wrap{{max-width:1100px;margin:0 auto;padding:32px 24px 64px}}
.hdr{{background:var(--navy);color:#fff;border-radius:12px;padding:32px 36px;margin-bottom:24px}}
.hdr h1{{{HEADING_FONT_CSS}font-size:22px;font-weight:700}}
.hdr .sub{{color:#C7CEBB;font-size:13px;margin-top:6px}}
.sc{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:14px;margin-bottom:28px}}
.card{{background:var(--cream-card);border:1px solid var(--border);border-radius:10px;padding:18px 14px;text-align:center}}
.card .n{{font-size:28px;font-weight:700}}.card .l{{font-size:12px;color:var(--muted);margin-top:4px}}
.card.good .n{{color:var(--green)}}.card.bad .n{{color:var(--red)}}.card.warn .n{{color:var(--amber)}}
.exec-case{{background:var(--cream-card);border:1px solid var(--border);border-left:4px solid var(--border);border-radius:10px;padding:18px 20px;margin-bottom:16px}}
.exec-case.row-valid{{border-left-color:var(--green)}}
.exec-case.row-flagged{{border-left-color:var(--red)}}
.exec-case.row-cancelled{{border-left-color:var(--border)}}
.exec-case-hdr{{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;gap:12px}}
.exec-notes{{color:var(--muted);margin-bottom:10px}}
.evidence{{margin:6px 0 10px}}.ev-grid{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}@media(max-width:700px){{.ev-grid{{grid-template-columns:1fr}}}}
.ev-steps{{white-space:pre-wrap;font:inherit;font-size:.9em;margin:0 0 8px}}.ev-table{{border-collapse:collapse;width:100%;font-size:.9em}}
.ev-table td,.ev-table th{{border:1px solid #ddd;padding:4px 6px;text-align:left;vertical-align:top}}.ev-empty{{color:#b42318;font-weight:600}}.ev-note{{font-size:.85em;color:var(--muted)}}
.exec-steps{{margin:8px 0 0 20px;font-size:13px;color:var(--muted)}}
.badge{{display:inline-block;font-size:11px;font-weight:700;padding:3px 10px;border-radius:4px;white-space:nowrap}}
.badge.valid{{background:var(--gbg);color:var(--green)}}.badge.flagged{{background:var(--abg);color:var(--amber)}}
.badge.gap{{background:var(--rbg);color:var(--red)}}
.shot-gallery{{display:flex;flex-wrap:wrap;gap:10px;margin-top:12px}}
.shot-sample-note{{font-size:11.5px;color:var(--muted);font-style:italic;margin-top:10px}}
.shot img{{max-width:220px;border:1px solid var(--border);border-radius:6px;display:block}}
.empty{{color:var(--muted);font-style:italic}}
.ft{{margin-top:36px;text-align:center;font-size:12px;color:var(--muted);border-top:1px solid var(--border);padding-top:16px}}
.shot a{{display:block}}
.shot-hint{{font-size:11px;color:var(--muted);text-align:center;margin-top:4px}}
.btn-zip{{display:inline-block;margin-top:14px;font-size:12.5px;font-weight:600;color:var(--blue);
  background:var(--sky);border:1px solid var(--border);border-radius:8px;padding:8px 14px;text-decoration:none}}
.btn-zip:hover{{filter:brightness(0.97)}}
.run-zip-row{{margin-bottom:24px;text-align:right}}
</style></head><body>
{report_topbar_html()}
<div class="wrap">
<header class="hdr">
  <h1>Live Execution Report</h1>
  <p class="sub">{esc(data.get('application',''))} &bull; role: {esc(data.get('role_label',''))} &bull; environment: {esc(data.get('environment_label',''))} &bull; {esc(data.get('run_date',''))}</p>
</header>
<div class="sc">
  <div class="card"><div class="n">{total}</div><div class="l">Test Cases Run</div></div>
  <div class="card good"><div class="n">{passed}</div><div class="l">Passed</div></div>
  <div class="card bad"><div class="n">{failed}</div><div class="l">Failed</div></div>
  <div class="card warn"><div class="n">{blocked}</div><div class="l">Blocked</div></div>
  {f'<div class="card"><div class="n">{cancelled}</div><div class="l">Cancelled</div></div>' if cancelled else ''}
</div>
{f'<div class="run-zip-row"><a class="btn-zip" href="{run_zip_url}">Download everything for this run (.zip)</a></div>' if run_zip_url else ''}
{sections_html}
<div class="ft">Req2QA &mdash; Live Execution &bull; {esc(data.get('run_date',''))} &bull; sandbox/UAT environment only</div>
</div>
{report_footer_html()}
</body></html>"""
