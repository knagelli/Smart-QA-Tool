"""
Req2QA - Execution Report Builder

Builds the HTML report for one batch of live test-case executions:
verdict per case, plain-language step log, and the screenshot evidence
gallery (already masked for critical fields by execute_engine.py before
being saved to disk - this module never re-decides what's sensitive).
"""
import html


def esc(s):
    return html.escape(str(s if s is not None else ""))


VERDICT_BADGE = {
    "PASS": ('<span class="badge valid">PASS</span>', "row-valid"),
    "FAIL": ('<span class="badge gap">FAIL</span>', "row-flagged"),
    "BLOCKED": ('<span class="badge flagged">BLOCKED</span>', "row-flagged"),
}


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
    # The report itself is served through a token-gated route; each
    # screenshot is a separate request under the same route, so the same
    # token needs to travel with each relative image URL.
    token_qs = f"?token={data['token']}" if data.get("token") else ""

    sections = []
    for r in results:
        badge, row_cls = VERDICT_BADGE.get(r.get("verdict"), ('<span class="badge">UNKNOWN</span>', ""))
        step_items = "".join(f"<li>{esc(s)}</li>" for s in r.get("step_log", [])) or "<li class='empty'>No steps recorded.</li>"
        shot_items = "".join(
            f'<div class="shot"><img src="{esc(src)}{token_qs}" loading="lazy" alt="Execution screenshot"></div>'
            for src in r.get("screenshots", [])
        ) or "<p class='empty'>No screenshots captured.</p>"
        sections.append(f"""
<div class="exec-case {row_cls}">
  <div class="exec-case-hdr">
    <div><strong>{esc(r.get('tc_id',''))}</strong> &mdash; {esc(r.get('title',''))}</div>
    {badge}
  </div>
  <p class="exec-notes">{esc(r.get('notes',''))}</p>
  <details>
    <summary>Step log ({len(r.get('step_log', []))} actions)</summary>
    <ol class="exec-steps">{step_items}</ol>
  </details>
  <div class="shot-gallery">{shot_items}</div>
</div>""")

    sections_html = "\n".join(sections) if sections else '<p class="empty">No test cases were executed in this run.</p>'

    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Live Execution Report</title><style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
:root{{--navy:#0F2044;--blue:#1A5EA8;--sky:#E8F1FB;--green:#15803D;--gbg:#DCFCE7;
--red:#B91C1C;--rbg:#FEE2E2;--amber:#B45309;--abg:#FEF3C7;--border:#E2E8F0;--text:#1E293B;--muted:#64748B;--bg:#F8FAFC}}
body{{font-family:system-ui,sans-serif;background:var(--bg);color:var(--text);font-size:14px;line-height:1.6}}
.wrap{{max-width:1100px;margin:0 auto;padding:32px 24px 64px}}
.hdr{{background:var(--navy);color:#fff;border-radius:12px;padding:32px 36px;margin-bottom:24px}}
.hdr h1{{font-size:22px;font-weight:700}}
.hdr .sub{{color:#94A3B8;font-size:13px;margin-top:6px}}
.sc{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:14px;margin-bottom:28px}}
.card{{background:#fff;border:1px solid var(--border);border-radius:10px;padding:18px 14px;text-align:center}}
.card .n{{font-size:28px;font-weight:700}}.card .l{{font-size:12px;color:var(--muted);margin-top:4px}}
.card.good .n{{color:var(--green)}}.card.bad .n{{color:var(--red)}}.card.warn .n{{color:var(--amber)}}
.exec-case{{background:#fff;border:1px solid var(--border);border-left:4px solid var(--border);border-radius:10px;padding:18px 20px;margin-bottom:16px}}
.exec-case.row-valid{{border-left-color:var(--green)}}
.exec-case.row-flagged{{border-left-color:var(--red)}}
.exec-case-hdr{{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;gap:12px}}
.exec-notes{{color:var(--muted);margin-bottom:10px}}
.exec-steps{{margin:8px 0 0 20px;font-size:13px;color:var(--muted)}}
.badge{{display:inline-block;font-size:11px;font-weight:700;padding:3px 10px;border-radius:4px;white-space:nowrap}}
.badge.valid{{background:var(--gbg);color:var(--green)}}.badge.flagged{{background:var(--abg);color:var(--amber)}}
.badge.gap{{background:var(--rbg);color:var(--red)}}
.shot-gallery{{display:flex;flex-wrap:wrap;gap:10px;margin-top:12px}}
.shot img{{max-width:220px;border:1px solid var(--border);border-radius:6px;display:block}}
.empty{{color:var(--muted);font-style:italic}}
.ft{{margin-top:36px;text-align:center;font-size:12px;color:var(--muted);border-top:1px solid var(--border);padding-top:16px}}
</style></head><body><div class="wrap">
<header class="hdr">
  <h1>Live Execution Report</h1>
  <p class="sub">{esc(data.get('application',''))} &bull; role: {esc(data.get('role_label',''))} &bull; environment: {esc(data.get('environment_label',''))} &bull; {esc(data.get('run_date',''))}</p>
</header>
<div class="sc">
  <div class="card"><div class="n">{total}</div><div class="l">Test Cases Run</div></div>
  <div class="card good"><div class="n">{passed}</div><div class="l">Passed</div></div>
  <div class="card bad"><div class="n">{failed}</div><div class="l">Failed</div></div>
  <div class="card warn"><div class="n">{blocked}</div><div class="l">Blocked</div></div>
</div>
{sections_html}
<div class="ft">Req2QA &mdash; Live Execution &bull; {esc(data.get('run_date',''))} &bull; sandbox/UAT environment only</div>
</div></body></html>"""
