"""
Req2QA - Shared brand theme for standalone HTML report builders.

execution_report.py and report_builder.py both hand-build complete,
self-contained HTML documents (not Jinja2 templates) because they must
remain fully viewable offline - downloaded, emailed, or opened from a
.zip with no server running. That constraint is legitimate and is not
being removed here.

What was wrong: each of those files had its own copy-pasted <style>
block, hardcoded to the site's OLD pre-rebrand blue palette, and each
had silently dropped the actual logo (an <img src="/static/logo.svg">
would 404 the moment someone opens the report offline). Three separate
copies of the same mistake, found only after a client-facing near-miss
(see claude/execution-report-brand-audit-2026-09-21.md for the full
audit). This module is the fix: ONE definition of the current palette
(mirrored exactly from style.css's :root - see the comment there for
why --green/--amber exist), ONE inlined copy of the real logo SVGs (safe
offline, since inlined SVG markup has no external request at all, unlike
an <img src>), and ONE topbar/footer HTML builder, imported by both
report generators instead of each hardcoding its own.

If the site's colors or nav links change again, this is the one place
that needs updating for every downloadable report to follow along -
that single-source-of-truth property is the actual fix, not just the
corrected colors themselves.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

# Mirrors style.css's :root exactly. Keep these two in sync by hand - see
# the module docstring above for why a single source of truth matters here.
PALETTE_CSS = """
:root{--navy:#2B3A2A;--blue:#5B7A52;--sky:#E9EFE3;--border:#E6DFCF;--text:#2B2B25;
--muted:#5B5646;--bg:#FBF7ED;--red:#A13D2B;--rbg:#F6E3DA;
--purple:#B5652E;--pbg:#F3E4D3;--cream-card:#FFFFFF;
--green:#3F6B3F;--gbg:#E3EEDD;--amber:#9C6B1F;--abg:#F3E9D3}
"""

# Real fonts, actually applied (the pre-fix versions linked these Google
# Fonts in <head> and then never referenced them in any font-family rule -
# a wasted network request that also meant headings rendered in the
# browser default instead of the site's actual Fraunces/Inter pairing).
FONT_LINKS = """
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:wght@500;600;700&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
"""
BODY_FONT_CSS = "font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;"
HEADING_FONT_CSS = "font-family:'Fraunces',Georgia,serif;"

# Logos inlined as raw <svg> markup (not <img src="...">) so they render
# correctly with zero network dependency - required for a report opened
# offline from a downloaded .zip. Copied verbatim from
# app/static/logo.svg and app/static/logo-light.svg; update both places
# together if the mark itself ever changes.
LOGO_SVG_DARK = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 70 940 285" role="img" aria-label="Req2QA" style="display:block;height:22px;width:auto">
  <title>Req2QA</title>
  <text x="0" y="300" font-family="Arial, Helvetica, sans-serif" font-weight="800" font-size="220"
        fill="#2B3A2A" letter-spacing="-4">req</text>
  <g transform="translate(400,300) scale(0.82)">
    <path d="M0 -95 L75 -35 L0 25 L-8 25 L-8 -20 L58 -35 L-8 -50 L-8 -95 Z" fill="#5B7A52"/>
    <path d="M95 -95 L170 -35 L95 25 L87 25 L87 -20 L153 -35 L87 -50 L87 -95 Z" fill="#5B7A52"/>
  </g>
  <text x="595" y="300" font-family="Arial, Helvetica, sans-serif" font-weight="800" font-size="220"
        fill="#2B3A2A" letter-spacing="-4">qa</text>
  <path d="M535 190 L590 250 L695 110"
        fill="none" stroke="#ffffff" stroke-width="46" stroke-linecap="round" stroke-linejoin="round"/>
  <path d="M535 190 L590 250 L695 110"
        fill="none" stroke="#5B7A52" stroke-width="30" stroke-linecap="round" stroke-linejoin="round"/>
  <text x="855" y="150" font-family="Arial, Helvetica, sans-serif" font-weight="700" font-size="80"
        fill="#5B7A52">&#8482;</text>
</svg>"""

LOGO_SVG_LIGHT = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 70 940 285" role="img" aria-label="Req2QA" style="display:block;height:20px;width:auto">
  <title>Req2QA</title>
  <text x="0" y="300" font-family="Arial, Helvetica, sans-serif" font-weight="800" font-size="220"
        fill="#ffffff" letter-spacing="-4">req</text>
  <g transform="translate(400,300) scale(0.82)">
    <path d="M0 -95 L75 -35 L0 25 L-8 25 L-8 -20 L58 -35 L-8 -50 L-8 -95 Z" fill="#9FC98A"/>
    <path d="M95 -95 L170 -35 L95 25 L87 25 L87 -20 L153 -35 L87 -50 L87 -95 Z" fill="#9FC98A"/>
  </g>
  <text x="595" y="300" font-family="Arial, Helvetica, sans-serif" font-weight="800" font-size="220"
        fill="#ffffff" letter-spacing="-4">qa</text>
  <path d="M535 190 L590 250 L695 110"
        fill="none" stroke="#2B3A2A" stroke-width="46" stroke-linecap="round" stroke-linejoin="round"/>
  <path d="M535 190 L590 250 L695 110"
        fill="none" stroke="#9FC98A" stroke-width="30" stroke-linecap="round" stroke-linejoin="round"/>
  <text x="855" y="150" font-family="Arial, Helvetica, sans-serif" font-weight="700" font-size="80"
        fill="#9FC98A">&#8482;</text>
</svg>"""

# Shared CSS for the topbar/footer markup below - kept here rather than
# duplicated per-caller, same single-source-of-truth reasoning as the
# palette itself.
CHROME_CSS = """
.rpt-topbar{background:#fff;border-bottom:1px solid var(--border);padding:14px 24px}
.rpt-topbar-inner{max-width:1100px;margin:0 auto;display:flex;align-items:center;justify-content:space-between}
.rpt-topbar-brand{display:inline-flex;align-items:center;text-decoration:none}
.rpt-topbar-cta{font-size:12.5px;font-weight:600;color:#fff;background:var(--blue);padding:8px 16px;
  border-radius:8px;text-decoration:none}
.site-ftr{background:var(--navy);color:#EDEFE9;margin-top:48px}
.site-ftr-inner{max-width:1100px;margin:0 auto;padding:36px 24px 24px;display:flex;flex-wrap:wrap;gap:32px;justify-content:space-between}
.site-ftr-brand{max-width:260px}
.site-ftr-tag{font-size:12px;color:#B9C2AE;margin-top:8px;letter-spacing:.03em;text-transform:uppercase}
.site-ftr-trust{font-size:12px;color:#C7CEBB;margin-top:14px;line-height:1.6}
.site-ftr-col h4{font-size:12px;color:#9FA98F;text-transform:uppercase;letter-spacing:.06em;margin-bottom:10px}
.site-ftr-col a{display:block;color:#DEE2D4;text-decoration:none;font-size:13px;margin-bottom:7px}
.site-ftr-col a:hover{color:#fff;text-decoration:underline}
.site-ftr-bottom{border-top:1px solid rgba(255,255,255,.12);padding:16px 24px;text-align:center;font-size:12px;color:#9FA98F}
.site-ftr-abn{display:block;margin-top:4px;font-size:11px;color:#8A9480}
"""


def current_year() -> str:
    # Melbourne time, consistent with every other timestamp in the app.
    return str(datetime.now(ZoneInfo("Australia/Melbourne")).year)


def report_topbar_html() -> str:
    """A simplified, self-contained topbar for a downloadable report: the
    real logo (inlined, offline-safe) and a single CTA. Full nav links are
    left out here deliberately - unlike the live site's topbar, these
    links would be dead weight in an offline-opened document with no
    server to navigate to, and a bare logo + one clear next step reads
    better in a document context than a full nav bar does. When the
    report is viewed online (the common case, via the token-gated
    /download-exec or /download route) the CTA link still works normally."""
    return f"""<div class="rpt-topbar"><div class="rpt-topbar-inner">
<a class="rpt-topbar-brand" href="https://req2qa.com" aria-label="Req2QA">{LOGO_SVG_DARK}</a>
<a class="rpt-topbar-cta" href="https://req2qa.com/trial-signup">Start Free Trial</a>
</div></div>"""


def report_footer_html() -> str:
    """Mirrors templates/_footer.html's brand column (logo, tagline, trust
    line, trademark, ABN) so a downloaded report carries the same legal/
    brand footer as every live page, instead of the bare one-line text
    footer these reports used to have."""
    return f"""<footer class="site-ftr"><div class="site-ftr-inner">
<div class="site-ftr-col site-ftr-brand">
  <div>{LOGO_SVG_LIGHT}</div>
  <p class="site-ftr-tag">Requirements to Test Coverage</p>
  <p class="site-ftr-trust">Credentials are never stored or logged. Live execution only runs against sandbox/UAT environments &mdash; never production.</p>
</div>
</div>
<div class="site-ftr-bottom">
<span>&copy; {current_year()} Req2QA&trade;. All rights reserved.</span>
<span class="site-ftr-abn">REQ2QA is a registered business name of Kalyan Nagelli, ABN 56 181 932 896.</span>
</div>
</footer>"""
