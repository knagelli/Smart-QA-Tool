# trial_signups.py
#
# Self-serve free-trial signup + one-shot metered access codes.
#
# Design (settled 2026-09-15, see claude/self-serve-trial-and-tiered-pricing-
# design-2026-09-15.md in the project docs): a signup form auto-generates and
# emails a metered access code - no password, no session, no login page.
# This stays inside the existing (twice security-audited) access-code
# architecture rather than adding real authentication.
#
# Key decisions baked into this module (confirmed with Kalyan):
#   - One free trial per BUSINESS email domain (personal providers blocked
#     outright), not per email address.
#   - One-shot trial: a code is valid for exactly one generation run,
#     whatever test-case count that run produces. There is no mid-run
#     truncation - instead /main.py enforces a pre-call document-size guard
#     (see TRIAL_MAX_REQ_WORDS) so an oversized document is rejected BEFORE
#     any Anthropic API call, so an over-budget attempt never spends a token.
#   - The generated code is never shown on screen at signup - it is only
#     emailed. Showing it immediately would let anyone claim a company's one
#     trial by typing that domain, without ever needing to control that
#     inbox - denying the real company their trial. Emailing it (even to an
#     address the requester doesn't fully control) at least requires the
#     domain to be a real, deliverable mailbox and leaves an audit trail.
#   - A code is "reserved" the moment a generation attempt begins (so a
#     concurrent second attempt can't double-spend the same one-shot trial),
#     and "used" only once that generation actually succeeds. A reservation
#     that never completes (abandoned custom-mode review step, a crashed
#     request) auto-releases back to "unused" after RESERVE_TTL_SECONDS, so
#     a technical failure or an abandoned attempt never permanently burns a
#     company's one trial.

# CONCURRENCY NOTE: the one-shot reservation guarantee (reserve_trial()
# below) depends on this app running as a SINGLE process/worker. The
# threading.Lock here only serializes access within one process; it does
# NOT protect against two separate worker processes both reading
# status="unused" for the same code before either writes "reserved" back.
# Render currently runs plain `uvicorn app.main:app` (single worker) - if
# that is ever changed to multiple workers/processes for throughput, this
# store needs to move to something that supports real atomic
# compare-and-swap across processes (a database row, a file lock via
# fcntl, etc.) or a trial code could be used more than once.

import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_ROOT = Path(os.environ.get("DATA_DIR")) if os.environ.get("DATA_DIR") else BASE_DIR
STORE_PATH = DATA_ROOT / "trial_signups.json"

# A reservation older than this is treated as abandoned and released back to
# "unused" automatically. Generation (including custom-mode's two-step
# review) normally completes in well under this window.
RESERVE_TTL_SECONDS = 30 * 60

# Conservative proxy for "will this document likely produce far more than 10
# test cases." This is a word-count estimate, not a guarantee - it exists to
# bound Anthropic API cost on an oversized document BEFORE any API call is
# made, per Kalyan's explicit cost-minimization instruction. Tunable without
# a code change via the env var below; revisit once real trial-run data
# (actual word count vs. actual test-case count) exists to calibrate it.
TRIAL_MAX_REQ_WORDS = int(os.environ.get("TRIAL_MAX_REQ_WORDS", "1200"))

# Common free/personal email providers. Not exhaustive - anything not on this
# list is treated as a business domain. Extend without a code change via
# TRIAL_BLOCKED_DOMAINS_EXTRA="foo.com,bar.com" in Render.
_PERSONAL_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "yahoo.co.in",
    "outlook.com", "hotmail.com", "hotmail.co.uk", "live.com", "msn.com",
    "icloud.com", "me.com", "mac.com", "aol.com", "protonmail.com", "proton.me",
    "zoho.com", "gmx.com", "gmx.net", "mail.com", "yandex.com", "yandex.ru",
    "rediffmail.com", "qq.com", "163.com", "126.com", "naver.com",
    "fastmail.com", "hey.com", "pm.me", "tutanota.com", "inbox.com",
    "rocketmail.com", "ymail.com", "bigpond.com", "optusnet.com.au",
    "internode.on.net", "iinet.net.au",
}
_extra = os.environ.get("TRIAL_BLOCKED_DOMAINS_EXTRA", "")
for _d in _extra.split(","):
    _d = _d.strip().lower()
    if _d:
        _PERSONAL_EMAIL_DOMAINS.add(_d)

_lock = threading.Lock()

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def domain_of(email: str) -> str:
    return email.strip().lower().rsplit("@", 1)[-1]


def is_valid_email(email: str) -> bool:
    return bool(_EMAIL_RE.match(email.strip()))


def is_business_domain(email: str) -> bool:
    return domain_of(email) not in _PERSONAL_EMAIL_DOMAINS


def _load() -> dict:
    if not STORE_PATH.exists():
        return {}
    try:
        return json.loads(STORE_PATH.read_text())
    except Exception:
        return {}


def _save(data: dict) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(STORE_PATH)


def _release_stale_reservations(data: dict) -> bool:
    """Auto-releases any reservation older than RESERVE_TTL_SECONDS back to
    'unused'. Returns True if it changed anything (caller should save)."""
    changed = False
    now = time.time()
    for record in data.values():
        if record.get("status") == "reserved":
            reserved_at = record.get("reserved_at_epoch", 0)
            if now - reserved_at > RESERVE_TTL_SECONDS:
                record["status"] = "unused"
                record["reserved_at_epoch"] = None
                record["reserved_at"] = None
                changed = True
    return changed


def sanitize_header_text(s: str) -> str:
    """Strips characters that have no business in an email header or a
    single-line display field. This value ends up in an email Subject line
    (the new-signup notification to Kalyan) and in the admin view, so a
    stray CR/LF must never reach it - defense in depth alongside whatever
    EmailMessage itself already guards against."""
    return s.replace("\r", " ").replace("\n", " ").strip()


def create_signup(company: str, contact_name: str, email: str, application: str = ""):
    """Returns (ok, code_or_error_message). Does not send email - caller does
    that so this module has no mailer dependency."""
    company = sanitize_header_text(company.strip())
    contact_name = sanitize_header_text(contact_name.strip())
    application = sanitize_header_text(application.strip())
    email = email.strip()

    if not company or not contact_name:
        return False, "Please fill in your name and company."
    if not is_valid_email(email):
        return False, "That doesn't look like a valid email address."
    if not is_business_domain(email):
        return False, (
            "Free trials are available to business email addresses only. "
            "Please reach out directly to kalyan@req2qa.com and we'll get you set up."
        )

    domain = domain_of(email)

    with _lock:
        data = _load()
        if _release_stale_reservations(data):
            pass  # will be saved below regardless
        existing = data.get(domain)
        if existing:
            return False, (
                "A free trial has already been claimed for this company's domain. "
                "Please contact kalyan@req2qa.com to continue with a paid engagement."
            )

        # Generate a code, guarding (extremely unlikely) collision against
        # every code already issued.
        existing_codes = {rec["code"] for rec in data.values()}
        code = f"TRIAL-{secrets.token_hex(4).upper()}"
        while code in existing_codes:
            code = f"TRIAL-{secrets.token_hex(4).upper()}"

        data[domain] = {
            "company": company,
            "contact_name": contact_name,
            "email": email,
            "domain": domain,
            "application": application.strip(),
            "code": code,
            "status": "unused",
            "created_at": _now_iso(),
            "reserved_at": None,
            "reserved_at_epoch": None,
            "used_at": None,
            "run_id": None,
            "test_case_count": None,
        }
        _save(data)
        return True, code


def _find_by_code(data: dict, code: str):
    """Constant-time-ish lookup across all issued codes, matching the
    existing paid-access-code lookup pattern in main.py (_lookup_client)."""
    import hmac
    match = None
    for record in data.values():
        if hmac.compare_digest(record["code"], code):
            match = record
    return match


def lookup_trial(code: str):
    """Returns the trial record for this code (any status), or None. Read-
    only - does not reserve or mutate anything."""
    with _lock:
        data = _load()
        if _release_stale_reservations(data):
            _save(data)
        return _find_by_code(data, code)


def reserve_trial(code: str):
    """Atomically reserves an unused trial code for a generation attempt
    about to begin. Returns (ok, record_or_message)."""
    with _lock:
        data = _load()
        if _release_stale_reservations(data):
            pass
        record = _find_by_code(data, code)
        if not record:
            _save(data)
            return False, None
        if record["status"] == "used":
            _save(data)
            return False, "This trial code has already been used."
        if record["status"] == "reserved":
            _save(data)
            return False, (
                "This trial code is currently in use, or a previous attempt didn't finish cleanly. "
                "It will free up automatically within 30 minutes - or email kalyan@req2qa.com "
                "for an immediate fix."
            )
        record["status"] = "reserved"
        record["reserved_at"] = _now_iso()
        record["reserved_at_epoch"] = time.time()
        _save(data)
        return True, dict(record)


def mark_trial_used(code: str, run_id: str, test_case_count: int) -> None:
    with _lock:
        data = _load()
        record = _find_by_code(data, code)
        if not record:
            return
        record["status"] = "used"
        record["used_at"] = _now_iso()
        record["run_id"] = run_id
        record["test_case_count"] = test_case_count
        _save(data)


def release_trial(code: str) -> None:
    """Reverts a reservation back to 'unused' - called when the generation
    attempt itself fails (extraction error, API error), so a technical
    failure never permanently costs a company its one trial."""
    with _lock:
        data = _load()
        record = _find_by_code(data, code)
        if record and record["status"] == "reserved":
            record["status"] = "unused"
            record["reserved_at"] = None
            record["reserved_at_epoch"] = None
            _save(data)


def check_word_count(text: str) -> bool:
    """True if the text is within the trial's document-size guard."""
    return len(text.split()) <= TRIAL_MAX_REQ_WORDS


def reset_domain(domain: str) -> bool:
    """Admin-only escape hatch (see /admin/trials in admin_routes.py): wipes
    a domain's trial record entirely so that domain can sign up again. For
    when a signup was made with a wrong/typo'd/adversarial email and the
    real company would otherwise be permanently locked out of ever getting
    their one free trial - there was no recovery path for this before.
    Returns True if a record existed and was removed."""
    domain = domain.strip().lower()
    with _lock:
        data = _load()
        if domain in data:
            del data[domain]
            _save(data)
            return True
        return False


def list_all_signups() -> list:
    """For the admin visibility view. Newest first."""
    with _lock:
        data = _load()
        if _release_stale_reservations(data):
            _save(data)
        return sorted(data.values(), key=lambda r: r.get("created_at", ""), reverse=True)
