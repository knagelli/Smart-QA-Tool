"""
Req2QA - Execution Report Builder

Builds the HTML report for one batch of live test-case executions:
verdict per case, plain-language step log, and the screenshot evidence
gallery (already masked for critical fields by execute_engine.py before
being saved to disk - this module never re-decides what's sensitive).
"""
import html
from datetime import datetime


def esc(s):
    return html.escape(str(s if s is not None else ""))


def _current_year() -> str:
    return str(datetime.now().year)


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
            _shot_html(src) for src in r.get("screenshots", [])
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
<title>Live Execution Report</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:wght@500;600;700&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/static/style.css">
<style>
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
.shot a{{display:block}}
.shot-hint{{font-size:11px;color:var(--muted);text-align:center;margin-top:4px}}
</style></head><body>
<div class="topbar">
  <div class="topbar-inner">
    <a class="topbar-brand" href="/">Req<span>2</span>QA</a>
    <button type="button" class="topbar-toggle" aria-label="Open menu" aria-expanded="false" aria-controls="topbar-nav">&#9776;</button>
    <nav class="topbar-nav" id="topbar-nav">
      <a href="/about#how-it-works">How it works</a>
      <a href="/import-tests">Import test cases</a>
      <a href="/history">Run history</a>
      <a href="/#faq">FAQ</a>
      <a href="/about">About</a>
      <a href="/security">Security</a>
    </nav>
    <a class="btn topbar-cta" href="/#chooser">Get Started</a>
  </div>
</div>
<script src="/static/topbar.js" defer></script>
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
</div>
{sections_html}
<div class="ft">Req2QA &mdash; Live Execution &bull; {esc(data.get('run_date',''))} &bull; sandbox/UAT environment only</div>
</div>
<footer class="site-ftr">
  <div class="site-ftr-inner">
    <div class="site-ftr-col site-ftr-brand">
      <div class="site-ftr-logo">Req<span>2</span>QA</div>
      <p class="site-ftr-tag">Requirements to Test Coverage</p>
      <p class="site-ftr-trust">Credentials are never stored or logged. Live execution only runs against sandbox/UAT environments &mdash; never production.</p>
    </div>
    <div class="site-ftr-col">
      <h4>Product</h4>
      <a href="/about#how-it-works">How it works</a>
      <a href="/import-tests">Import test cases</a>
      <a href="/history">Run history</a>
      <a href="/#faq">FAQ</a>
    </div>
    <div class="site-ftr-col">
      <h4>Company</h4>
      <a href="/about">About</a>
      <a href="/security">Security overview</a>
      <a href="mailto:kalyan@req2qa.com">Contact us</a>
    </div>
    <div class="site-ftr-col">
      <h4>Legal</h4>
      <a href="/privacy">Privacy Policy</a>
      <a href="/terms">Terms of Service</a>
    </div>
  </div>
  <div class="site-ftr-bottom">
    <span>&copy; {_current_year()} Req2QA. All rights reserved.</span>
  </div>
</footer>
</body></html>"""
