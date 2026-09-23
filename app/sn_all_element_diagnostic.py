#!/usr/bin/env python3
"""
Zero-cost ServiceNow DOM diagnostic: find out exactly what element renders
the text "All", whether it lives inside an iframe (or a shadow root), and
whether req2qa's existing INTERACTIVE_SELECTOR would ever match it.

Why this exists
----------------
The 2026-09-22 iframe fix made the execution engine walk every visible
frame for interactive elements (see app/execute_engine.py, _visible_frames /
_snapshot_elements / INTERACTIVE_SELECTOR). A later 50-step run still never
clicked "All" even though wait_for_text confirmed the text "All" is present
somewhere on the page. wait_for_text does a text-only search across every
frame - it doesn't care whether the matching node is clickable. get_snapshot
only returns elements matching INTERACTIVE_SELECTOR:

    input, textarea, select, button, a[href], label,
    [role="button"], [role="link"], [role="tab"], [role="checkbox"],
    [role="radio"], [role="switch"]

The working hypothesis: "All" is visible text, but whatever element
actually renders it is not one of those tag/role types, or it's inside
something the frame-walk doesn't reach (a CLOSED shadow root - genuinely
unreachable by any browser-automation tool - or an iframe that fails the
0x0/display:none visibility check _visible_frames uses).

This script makes zero Anthropic/Bedrock calls. It logs in with Playwright,
and for every visible frame on the page:
  1. Walks the DOM (including OPEN shadow roots) looking for any element
     whose own direct text is exactly "All" (trimmed) - not a descendant's
     text, so we find the actual tag rendering the label, not an ancestor
     container that merely contains it somewhere inside.
  2. Also flags custom-element ancestors that expose no open shadowRoot
     (i.e. a *closed* shadow root: content Playwright/JS cannot see at all)
     so a "found nothing" result can be told apart from "the frame-walk
     genuinely cannot reach this text."
  3. For every genuine match, reports: frame URL, frame visibility
     (bounding box), tag name, id/class/role/aria-* attributes, whether it
     is a custom element (tag contains a hyphen - typical of ServiceNow's
     Now Experience UI web components), and whether
     `element.matches(INTERACTIVE_SELECTOR)` is true - the exact test that
     decides whether get_snapshot would ever have surfaced it.
  4. Cross-checks with Playwright's own locator engine directly:
     `frame.locator(INTERACTIVE_SELECTOR).filter(has_text="All")` - the
     literal call get_snapshot makes - so the report includes the real
     "would our existing selector ever match this" answer, not just a
     JS-side approximation.

Nothing here executes any action beyond login + read-only inspection - no
form submission, no clicking, no data mutation.

Credential handling
--------------------
This script never receives credentials as arguments or in code - it reads
them from environment variables set in the shell that runs it, on EC2 (or
wherever you choose to run this), exactly like req2qa's own per-run,
client-entered, in-memory credential model. I (the assistant) never see
these values.

Required environment variables:
    SN_URL        e.g. https://dev404193.service-now.com
    SN_USERNAME
    SN_PASSWORD

Optional:
    SN_LOGIN_PATH     default: /login.do
    SN_POST_LOGIN_WAIT_MS   default: 4000  (extra settle time after login,
                            since ServiceNow's Polaris shell loads its
                            iframe content asynchronously after the outer
                            chrome appears)
    SN_HEADLESS       default: "1" (set to "0" to watch it run, e.g. over
                      VNC/X11 on EC2 - not required)
    SN_OUTPUT         default: ./sn_all_diagnostic_report.json

Usage (on the box with network access to the PDI):
    pip install playwright
    playwright install chromium --with-deps
    SN_URL=https://dev404193.service-now.com \\
    SN_USERNAME=... \\
    SN_PASSWORD=... \\
    python3 sn_all_element_diagnostic.py

Output: a JSON report written to SN_OUTPUT, plus a short human-readable
summary printed to stdout. No screenshot is saved by default (the report
already contains everything needed and this avoids any chance of a
credential-bearing frame being captured mid-login); pass --screenshot to
also save one taken well after login settles.
"""
import json
import os
import sys
import time

INTERACTIVE_SELECTOR = (
    'input, textarea, select, button, a[href], label, [role="button"], '
    '[role="link"], [role="tab"], [role="checkbox"], [role="radio"], [role="switch"]'
)

# Injected into each frame. Walks the light DOM and any OPEN shadow roots
# looking for elements whose *own* direct text (not a descendant's) is
# exactly "All" once trimmed. Also records custom-element nodes that have
# no open shadowRoot, anywhere in the walk, as candidates for "this might
# be hiding the real element behind a closed shadow root."
_WALKER_JS = r"""
(selector) => {
    const target = "All";
    const matches = [];
    const closedShadowCandidates = [];
    const seen = new Set();

    function ownDirectText(el) {
        let text = "";
        for (const node of el.childNodes) {
            if (node.nodeType === Node.TEXT_NODE) {
                text += node.textContent;
            }
        }
        return text.trim();
    }

    function describe(el) {
        const attrs = {};
        for (const a of el.attributes || []) {
            attrs[a.name] = a.value;
        }
        let matchesSelector = false;
        try {
            matchesSelector = el.matches(selector);
        } catch (e) {
            matchesSelector = false;
        }
        let parentChain = [];
        let p = el.parentElement;
        for (let i = 0; i < 4 && p; i++) {
            parentChain.push(p.tagName.toLowerCase() + (p.id ? "#" + p.id : ""));
            p = p.parentElement;
        }
        let rect = null;
        try {
            const r = el.getBoundingClientRect();
            rect = { width: r.width, height: r.height, visible: r.width > 0 && r.height > 0 };
        } catch (e) {}
        return {
            tag: el.tagName.toLowerCase(),
            is_custom_element: el.tagName.toLowerCase().includes("-"),
            attributes: attrs,
            matches_interactive_selector: matchesSelector,
            parent_chain: parentChain,
            rect,
            outer_html_snippet: (el.outerHTML || "").slice(0, 300),
        };
    }

    function walk(root) {
        const all = root.querySelectorAll ? root.querySelectorAll("*") : [];
        for (const el of all) {
            if (seen.has(el)) continue;
            seen.add(el);

            const txt = ownDirectText(el);
            if (txt === target) {
                matches.push(describe(el));
            }

            if (el.shadowRoot) {
                // OPEN shadow root - keep walking inside it.
                walk(el.shadowRoot);
            } else if (el.tagName.toLowerCase().includes("-")) {
                // Custom element with no *visible* shadowRoot. This is
                // ambiguous from script - it could be a closed shadow
                // root (genuinely unreachable) or simply a custom element
                // that doesn't use shadow DOM at all. Recorded as a
                // candidate only; not proof either way.
                closedShadowCandidates.push({
                    tag: el.tagName.toLowerCase(),
                    id: el.id || null,
                    class: el.className || null,
                    text_content_trimmed_100: (el.textContent || "").trim().slice(0, 100),
                });
            }
        }
    }

    walk(document);
    return { matches, closedShadowCandidates: closedShadowCandidates.slice(0, 50) };
}
"""


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("ERROR: playwright is not installed. Run:\n"
              "    pip install playwright\n"
              "    playwright install chromium --with-deps",
              file=sys.stderr)
        sys.exit(1)

    sn_url = os.environ.get("SN_URL")
    sn_username = os.environ.get("SN_USERNAME")
    sn_password = os.environ.get("SN_PASSWORD")
    if not (sn_url and sn_username and sn_password):
        print("ERROR: set SN_URL, SN_USERNAME, SN_PASSWORD environment "
              "variables before running this script.", file=sys.stderr)
        sys.exit(1)

    login_path = os.environ.get("SN_LOGIN_PATH", "/login.do")
    post_login_wait_ms = int(os.environ.get("SN_POST_LOGIN_WAIT_MS", "4000"))
    headless = os.environ.get("SN_HEADLESS", "1") != "0"
    output_path = os.environ.get("SN_OUTPUT", "./sn_all_diagnostic_report.json")
    take_screenshot = "--screenshot" in sys.argv

    base = sn_url.rstrip("/")
    login_url = base + login_path

    report = {
        "instance": base,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()) + " UTC",
        "interactive_selector_used_by_req2qa": INTERACTIVE_SELECTOR,
        "frames": [],
    }

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context()
        page = context.new_page()

        print(f"[1/4] Navigating to {login_url} ...")
        page.goto(login_url, wait_until="domcontentloaded", timeout=30000)

        # Standard ServiceNow login form. Field ids can vary slightly by
        # release/theme; try the common ones and fall back to name-based
        # locators before giving up.
        print("[2/4] Logging in ...")
        user_field = None
        for sel in ["#user_name", 'input[name="user_name"]', "#username"]:
            try:
                if page.locator(sel).count() > 0:
                    user_field = sel
                    break
            except Exception:
                continue
        pass_field = None
        for sel in ["#user_password", 'input[name="user_password"]', "#password"]:
            try:
                if page.locator(sel).count() > 0:
                    pass_field = sel
                    break
            except Exception:
                continue

        if not (user_field and pass_field):
            print("ERROR: could not find a recognizable ServiceNow login "
                  "form on this page. Dumping page title/url for debugging "
                  "(no credentials included):", file=sys.stderr)
            print(f"  url={page.url!r} title={page.title()!r}", file=sys.stderr)
            browser.close()
            sys.exit(2)

        page.fill(user_field, sn_username)
        page.fill(pass_field, sn_password)
        page.click('#sysverb_login, button[type="submit"], input[type="submit"]')
        page.wait_for_load_state("domcontentloaded", timeout=30000)
        page.wait_for_timeout(post_login_wait_ms)
        print(f"[3/4] Post-login URL: {page.url}")

        if take_screenshot:
            shot_path = "./sn_all_diagnostic_post_login.png"
            page.screenshot(path=shot_path, full_page=False)
            print(f"      Screenshot saved: {shot_path}")

        print("[4/4] Walking all frames for elements whose text is exactly 'All' ...")
        for frame in page.frames:
            frame_report = {
                "frame_url": frame.url,
                "is_main_frame": frame == page.main_frame,
            }
            try:
                el = frame.frame_element()
                box = el.bounding_box()
                frame_report["frame_visible"] = bool(box and box["width"] > 0 and box["height"] > 0)
                frame_report["frame_bounding_box"] = box
            except Exception as e:
                frame_report["frame_visible"] = "main_frame_or_unavailable"
                frame_report["frame_element_error"] = str(e)

            try:
                result = frame.evaluate(_WALKER_JS, INTERACTIVE_SELECTOR)
                frame_report["text_all_matches"] = result["matches"]
                frame_report["ambiguous_custom_elements_no_open_shadow_root"] = result["closedShadowCandidates"]
            except Exception as e:
                frame_report["walk_error"] = str(e)

            try:
                loc = frame.locator(INTERACTIVE_SELECTOR).filter(has_text="All")
                frame_report["interactive_selector_filter_has_text_All_count"] = loc.count()
            except Exception as e:
                frame_report["interactive_selector_filter_error"] = str(e)

            report["frames"].append(frame_report)

        browser.close()

    with open(output_path, "w") as f:
        json.dump(report, f, indent=2)

    # Human-readable summary
    print("\n" + "=" * 72)
    print("SUMMARY")
    print("=" * 72)
    total_matches = 0
    total_selector_hits = 0
    for fr in report["frames"]:
        n_matches = len(fr.get("text_all_matches", []))
        n_selector_hits = fr.get("interactive_selector_filter_has_text_All_count", 0)
        total_matches += n_matches
        total_selector_hits += n_selector_hits if isinstance(n_selector_hits, int) else 0
        if n_matches or n_selector_hits:
            print(f"\nFrame: {fr['frame_url']}  (visible={fr.get('frame_visible')})")
            for m in fr.get("text_all_matches", []):
                print(f"  <{m['tag']}> custom_element={m['is_custom_element']} "
                      f"role={m['attributes'].get('role')} "
                      f"matches_INTERACTIVE_SELECTOR={m['matches_interactive_selector']}")
                print(f"    attrs: {m['attributes']}")
                print(f"    parents: {' > '.join(reversed(m['parent_chain']))}")
            print(f"  INTERACTIVE_SELECTOR.filter(has_text='All') count in this frame: {n_selector_hits}")

    print(f"\nTotal elements found with own-text=='All' across all frames: {total_matches}")
    print(f"Total of those matched by req2qa's INTERACTIVE_SELECTOR (any frame): {total_selector_hits}")
    if total_matches and not total_selector_hits:
        print("\n=> CONFIRMS the hypothesis: 'All' text exists but nothing rendering it "
              "matches INTERACTIVE_SELECTOR anywhere - get_snapshot could never have "
              "surfaced a clickable ref for it, regardless of what the agent tried.")
    elif total_selector_hits:
        print("\n=> DOES NOT confirm the hypothesis as stated: something rendering 'All' "
              "DOES match INTERACTIVE_SELECTOR. Check its frame/visibility fields above - "
              "it may be in a frame _visible_frames() would exclude (0x0/display:none), "
              "which is a narrower, different bug than 'wrong element type'.")
    if any(fr.get("ambiguous_custom_elements_no_open_shadow_root") for fr in report["frames"]):
        print("\nNote: some frames contain custom elements with no OPEN shadow root "
              "exposed to script - these MAY be closed shadow roots (genuinely "
              "unreachable) or plain custom elements with no shadow DOM at all; "
              "this script cannot tell the two apart from outside. See the "
              "'ambiguous_custom_elements_no_open_shadow_root' list per frame in "
              f"{output_path} if the summary above didn't find 'All' anywhere.")

    print(f"\nFull report written to: {output_path}")


if __name__ == "__main__":
    main()
