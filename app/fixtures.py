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
