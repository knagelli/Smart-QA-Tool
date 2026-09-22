#!/usr/bin/env python3
"""
One-off diagnostic: measure how many output tokens Claude actually needs to
fully generate test cases for a given requirements document, instead of
guessing at a max_tokens value.

Why this exists: on 2026-09-22, req2qa's /analyze route failed repeatedly
against a real ServiceNow requirements document because the old
max_tokens=8000 cap cut Claude's JSON response off mid-string. The server's
error logs only ever captured the first 2000 characters of the response
(see qa_engine._parse_json_response), so the TRUE output length that
document needed was never actually observed - only that it exceeded 8000
tokens. QA_MAX_OUTPUT_TOKENS in app/qa_engine.py was raised to 64000 as an
interim safety margin, not a measured value. This script makes one real,
successful API call with a very high ceiling and reads back the actual
token usage the API reports (resp.usage.output_tokens) - that number is
ground truth, not an estimate.

Usage (run on the box with ANTHROPIC_API_KEY configured, e.g. the EC2 host
where req2qa itself runs - this makes one real, billed API call):

    cd /opt/req2qa
    python3 scripts/measure_qa_tokens.py "Service Now" /path/to/requirements.pdf

It reuses the exact SYSTEM_PROMPT / PROMPT_TEMPLATE / extract_text code
req2qa's own /analyze route uses, so the measurement reflects the real
production prompt - not a hand-rewritten approximation of it.

Output: the real output_tokens used, the stop_reason (should be "end_turn",
not "max_tokens" - if it's still "max_tokens" even at this script's high
ceiling, the document needs more than that ceiling and the ceiling itself
should be raised before trusting the number), whether the resulting JSON
actually parses, and a suggested max_tokens value (measured usage + a 30%
safety margin) that this specific document would need in production.

This is a read-only diagnostic: it does not write to any file req2qa uses,
does not touch client_quotas/trial state, and does not affect the live app
in any way. It costs one real Claude API call (typically well under $1 for
a document this size).
"""
import argparse
import os
import sys
from pathlib import Path

# Make the app package importable when run from the repo root (or from
# /opt/req2qa on the EC2 host, which mirrors this same layout).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# High enough that we should observe a natural finish (stop_reason ==
# "end_turn") rather than another artificial cutoff. If this script itself
# reports stop_reason == "max_tokens", this ceiling was still too low for
# the document - raise MEASURE_CEILING and rerun before trusting the number.
MEASURE_CEILING = 64000


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("application", help='Application name, e.g. "Service Now" (same value /analyze sends)')
    parser.add_argument("requirements_file", help="Path to the requirements document (.pdf/.docx/.txt/.csv)")
    parser.add_argument(
        "--ceiling", type=int, default=MEASURE_CEILING,
        help=f"max_tokens ceiling for this measurement call (default {MEASURE_CEILING})",
    )
    args = parser.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY is not set in this environment.", file=sys.stderr)
        print("Run this on the box where req2qa's own API key is configured "
              "(e.g. `sudo -E python3 scripts/measure_qa_tokens.py ...` on the EC2 host, "
              "or export the key in your current shell first).", file=sys.stderr)
        sys.exit(1)

    req_path = Path(args.requirements_file)
    if not req_path.exists():
        print(f"ERROR: file not found: {req_path}", file=sys.stderr)
        sys.exit(1)

    from app.extract import extract_text
    from app import ai_client
    from app.qa_engine import SYSTEM_PROMPT, PROMPT_TEMPLATE, _parse_json_response

    raw_bytes = req_path.read_bytes()
    req_text = extract_text(req_path.name, raw_bytes)
    if not req_text.strip():
        print("ERROR: no text could be extracted from that file - nothing to measure.", file=sys.stderr)
        sys.exit(1)

    prompt = PROMPT_TEMPLATE.format(
        application=args.application.strip(),
        requirements_text=req_text.strip()[:120000],
    )

    print(f"Requirements file: {req_path} ({len(raw_bytes)} bytes, {len(req_text)} extracted chars)")
    print(f"Application: {args.application}")
    print(f"Model: {ai_client.get_model_id()}")
    print(f"Measurement ceiling (max_tokens for this call): {args.ceiling}")
    print("Calling the Anthropic API - this is one real, billed request...")

    client = ai_client.get_client(api_key)
    resp = client.messages.create(
        model=ai_client.get_model_id(),
        max_tokens=args.ceiling,
        temperature=0,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )

    raw_text = "".join(block.text for block in resp.content if block.type == "text")

    print()
    print("=== RESULTS ===")
    print(f"stop_reason:        {resp.stop_reason}")
    print(f"input_tokens:       {resp.usage.input_tokens}")
    print(f"output_tokens:      {resp.usage.output_tokens}   <- the real number this document needed")
    print(f"raw response chars: {len(raw_text)}")

    if resp.stop_reason == "max_tokens":
        print()
        print(f"** WARNING: still hit the {args.ceiling}-token ceiling on THIS call. **")
        print("This document needs MORE than that - the output_tokens figure above is a")
        print("lower bound, not the true requirement. Rerun with a higher --ceiling before")
        print("trusting any suggested max_tokens value below.")
        sys.exit(2)

    try:
        _parse_json_response(raw_text)
        print("JSON parse:         OK - the full response is valid, well-formed JSON.")
    except Exception as e:
        print(f"JSON parse:         FAILED ({e})")
        print("Response finished naturally (stop_reason=end_turn) but still isn't valid JSON -")
        print("that's a different, separate problem from token truncation. Worth a look before")
        print("relying on this measurement to size max_tokens.")

    suggested = int(resp.usage.output_tokens * 1.3)
    # Round up to the nearest 1000 for a clean config value.
    suggested = ((suggested // 1000) + 1) * 1000
    print()
    print(f"Suggested max_tokens for this document (measured usage + 30% safety margin): {suggested}")
    print("This is ONE data point for ONE document. For a durable production value, run this")
    print("against your largest few real client documents and take the highest suggested value -")
    print("a single sample doesn't tell you the shape of your actual document-size distribution.")


if __name__ == "__main__":
    main()
