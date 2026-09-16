"""
Req2QA - Test Data Fixture Registry (Phase C)

A small per-(application, environment) JSON file remembering records the
live-execution automation itself has created (e.g. an employee added by an
earlier test case), so a later test case that needs an *existing* record
can reuse it instead of the client having to run a create-step every time,
and instead of every run silently leaving more dummy data behind.

Deliberately NOT a database - matches the rest of this app's all-file
persistence pattern (runs/, executions/, history). Deliberately never
deletes anything in the live target environment itself - only forgets its
own cached knowledge of a fixture once it expires. See
claude/phase-c-test-data-fixture-reuse-design.md for the full design and
council review this implements.
"""
import json
import re
import time
from pathlib import Path
from typing import Optional

# "For the day" per Kalyan's framing - a rolling window rather than a strict
# calendar-day cutoff (avoids timezone ambiguity: whose midnight?). Single
# named constant so it's a one-line change once real usage tells us whether
# 20 hours is too short or too long.
FIXTURE_TTL_HOURS = 20

_SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9_.-]")
_PLACEHOLDER_RE = re.compile(r"\{\{FIXTURE:([a-zA-Z0-9_]+)\.([a-zA-Z0-9_]+)\}\}")


def parse_fixture_role(role: str) -> Optional[tuple]:
    """'creates:employee' -> ('creates', 'employee'); 'requires:employee' ->
    ('requires', 'employee'); anything else (missing, 'none', malformed) ->
    None. Generation is expected to write one of these three shapes, but
    this never raises on unexpected input - a scenario with an unparseable
    fixture_role is just treated as if it were 'none'."""
    if not role or ":" not in role:
        return None
    action, _, ftype = role.partition(":")
    action, ftype = action.strip().lower(), ftype.strip().lower()
    if action in ("creates", "requires") and ftype:
        return (action, ftype)
    return None


def _registry_path(fixtures_dir: Path, application: str, env_host: str) -> Path:
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    safe_app = _SAFE_NAME_RE.sub("_", (application or "app").strip().lower())[:60]
    safe_host = _SAFE_NAME_RE.sub("_", (env_host or "unknown").strip().lower())[:80]
    return fixtures_dir / f"{safe_app}__{safe_host}.json"


def _load(path: Path) -> list:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text()).get("fixtures", [])
    except Exception:
        return []


def _save(path: Path, fixture_list: list) -> None:
    path.write_text(json.dumps({"fixtures": fixture_list}, indent=2))


def _is_fresh(fixture: dict) -> bool:
    created_at = fixture.get("created_at_epoch")
    return bool(created_at) and (time.time() - created_at) < FIXTURE_TTL_HOURS * 3600


def get_fresh_fixture(fixtures_dir: Path, application: str, env_host: str, fixture_type: str) -> Optional[dict]:
    """The freshest unexpired persisted fixture of this type for this
    (application, environment), or None. This only looks at the
    cross-run registry file - a fixture created moments ago by an earlier
    test case in the *same* run is handled separately by the caller
    (main.py keeps an in-run dict for that), so a "requires:" scenario
    paired with a "creates:" scenario in the same run always works
    regardless of the client's reuse-existing-data toggle."""
    path = _registry_path(fixtures_dir, application, env_host)
    matches = [f for f in _load(path) if f.get("type") == fixture_type and _is_fresh(f)]
    if not matches:
        return None
    return max(matches, key=lambda f: f.get("created_at_epoch", 0))


def save_fixture(fixtures_dir: Path, application: str, env_host: str, fixture_type: str,
                  attrs: dict, created_by: dict) -> dict:
    """Records a newly-created fixture for cross-run reuse, pruning any
    already-expired entries while the file is open anyway (cheap,
    incremental cleanup - no separate background job needed at this
    scale). Returns the fixture record that was saved."""
    path = _registry_path(fixtures_dir, application, env_host)
    fixture_list = [f for f in _load(path) if _is_fresh(f)]
    record = {
        "type": fixture_type,
        "attrs": attrs or {},
        "created_at_epoch": time.time(),
        "created_by": created_by or {},
    }
    fixture_list.append(record)
    _save(path, fixture_list)
    return record


def substitute_fixture_placeholders(text: str, fixture_type: str, fixture: dict) -> str:
    """Replaces every {{FIXTURE:<type>.<attr>}} token in text with the real
    value from `fixture`'s attrs, for tokens matching fixture_type. A token
    naming an attr that isn't present, or naming a different fixture type,
    is left as-is (a visibly-broken placeholder the client can report beats
    a silently wrong substitution)."""
    attrs = (fixture or {}).get("attrs", {})

    def _replace(m):
        ftype, attr = m.group(1), m.group(2)
        if ftype != fixture_type:
            return m.group(0)
        return str(attrs.get(attr, m.group(0)))

    return _PLACEHOLDER_RE.sub(_replace, text or "")



# --- Client-supplied test data (council-reviewed 2026-09-16, see
# claude/council-review-client-supplied-test-data-devils-advocate.md) -------
#
# Some scenarios reference a data state the automation has no legitimate way
# to create itself in a single run - e.g. "the audit trail for an employee
# who resigned last quarter" needs a hire, a period of activity, and a
# separate offboarding action, not something a live-execution run should
# ever try to fabricate against a client's real environment. For those, the
# client supplies a small seed record alongside their requirements
# (deliberately a short textarea, not a file upload, on the requirements
# form) rather than the automation guessing or silently inventing one.
#
# Design decisions from the council review, all deliberate:
#  - This is scoped to ONE run (kept in that run's data.json, threaded
#    through to execution), not the cross-run FIXTURES_DIR registry above -
#    the client is supplying this for the requirements they just submitted,
#    not registering it as reusable indefinitely. If it turns out clients
#    want it to persist across runs too, that is a separate, later decision.
#  - The intake copy asks for anonymized/synthetic data shaped like the real
#    thing (an ID and a status, not a real person's name) - never real
#    personal data about a real employee. scan_for_pii_flags is a best-effort
#    heuristic warning, not a hard block: false negatives are expected (it
#    cannot catch every way a name could appear), and it must never falsely
#    block a submission that flags real generation. Its job is to add one
#    factual, visible warning back to whoever submitted it, so the choice to
#    proceed anonymized-or-not stays informed and stays theirs.
_CLIENT_SEED_LINE_RE = re.compile(r"^\s*([a-zA-Z0-9_]+)\s*:\s*(.+)$")
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_PHONE_RE = re.compile(r"(?<!\d)(\+?\d[\d\-\s()]{7,}\d)(?!\d)")


def parse_client_seed_text(text: str) -> list:
    """Parses the requirements form's optional "test data you can provide"
    textarea. One fixture per line, deliberately simple since the person
    filling this in is a client, not a developer:

        employee: employee_id=E-0452, full_name=Test Offboardee, status=terminated
        invoice: invoice_number=INV-9001, status=overdue

    Any line that doesn't match "<type>: key=value, key=value, ..." is
    skipped rather than raising - a client's imperfect formatting shouldn't
    fail the whole requirements submission; it just means that one line's
    data isn't available for reuse; the field is only supplementary, and
    the associated scenario is still explicitly BLOCKED, never a fabricated
    value in place of it. Duplicate types keep the LAST occurrence, on the
    assumption that a client fixing a typo re-pastes the corrected line."""
    fixtures_by_type = {}
    for raw_line in (text or "").splitlines():
        m = _CLIENT_SEED_LINE_RE.match(raw_line)
        if not m:
            continue
        ftype, rest = m.group(1).strip().lower(), m.group(2)
        attrs = {}
        for pair in rest.split(","):
            if "=" not in pair:
                continue
            k, _, v = pair.partition("=")
            k, v = k.strip(), v.strip()
            if k:
                attrs[k] = v
        if ftype and attrs:
            fixtures_by_type[ftype] = {"type": ftype, "attrs": attrs, "source": "client_supplied"}
    return list(fixtures_by_type.values())


def scan_for_pii_flags(text: str) -> list:
    """Best-effort, deliberately conservative heuristic: flags anything that
    LOOKS like an email address or a phone number in the client-supplied
    test-data text, so the intake can show one plain warning ("this looks
    like it might contain a real email address or phone number - please use
    anonymized placeholder values instead") rather than silently accepting
    real personal data through a channel that was never built to handle it.
    Never used to block the submission - only to warn. Returns a list of
    plain-English labels for what was found (e.g. ["an email address"]),
    not the matched values themselves (no reason to echo the flagged text
    back, even in a warning message)."""
    flags = []
    if _EMAIL_RE.search(text or ""):
        flags.append("an email address")
    if _PHONE_RE.search(text or ""):
        flags.append("a phone number")
    return flags


def lookup_client_fixture(client_fixtures: list, fixture_type: str) -> Optional[dict]:
    """Finds a client-supplied seed of this type from the current run's own
    data.json (see parse_client_seed_text) - checked ahead of the cross-run
    registry so a client-provided record always satisfies the scenario it
    was supplied for, regardless of the "reuse existing test data" toggle
    (that toggle only ever governed *cross-run* reuse of automation-created
    records, a different thing from data the client explicitly attached to
    this run)."""
    for f in client_fixtures or []:
        if f.get("type") == fixture_type:
            return f
    return None


def substitute_unique_token(text: str, token: str) -> str:
    """Replaces every literal '{{UNIQUE}}' occurrence in text with a real,
    execution-specific token, so a scenario writing e.g. 'AutoTest_{{UNIQUE}}'
    for a name or ID it needs to be unique doesn't collide with data left
    behind by an earlier run against the same shared environment. Applied to
    every scenario, independent of fixture_role - most scenarios that create
    any new data should use this, not only ones tagged creates:<type>."""
    return (text or "").replace("{{UNIQUE}}", token)
