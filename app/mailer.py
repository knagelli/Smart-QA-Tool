# mailer.py
#
# Minimal outbound-email helper for Req2QA (POC).
#
# Sends via Gmail/Google Workspace SMTP using an App Password. This module
# only ever opens an SMTP connection to send a message - it never reads,
# lists, or deletes anything from the mailbox, and never uses IMAP/POP.
#
# Required env vars (set as secrets in Render):
#   SMTP_USER  - the primary Workspace mailbox address the App Password
#                belongs to (e.g. kalyan@req2qa.com)
#   SMTP_PASS  - the 16-character Gmail App Password for that account
#
# The visible "From" address sent to recipients is fixed to
# support@req2qa.com (must be a verified "Send As" alias on that mailbox
# in Gmail settings, or Gmail will silently rewrite it back to SMTP_USER).
#
# This module fails closed and quietly: if SMTP_USER/SMTP_PASS are not
# configured, send_email() returns False and logs a warning rather than
# raising - so a missing/misconfigured mailer never crashes a request.

import logging
import os
import smtplib
from email.message import EmailMessage

logger = logging.getLogger("req2qa.mailer")

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
FROM_ADDR = "support@req2qa.com"
FROM_DISPLAY = f"Req2QA <{FROM_ADDR}>"

SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASS = os.environ.get("SMTP_PASS")

# Basic outbound throttle so a bug or abuse can't fan out mail unbounded.
# Process-local, resets on deploy/restart - good enough for a solo-operator
# POC; revisit if/when this needs to survive multiple instances.
_MAX_SENDS_PER_HOUR = 50
_send_timestamps: list[float] = []


def _rate_limited() -> bool:
    import time

    now = time.time()
    cutoff = now - 3600
    while _send_timestamps and _send_timestamps[0] < cutoff:
        _send_timestamps.pop(0)
    if len(_send_timestamps) >= _MAX_SENDS_PER_HOUR:
        return True
    _send_timestamps.append(now)
    return False


def mailer_configured() -> bool:
    return bool(SMTP_USER and SMTP_PASS)


def send_email(to_addr: str, subject: str, body_text: str) -> tuple[bool, str]:
    """Send a plain-text email. Returns (success, message) - message is a
    short, non-sensitive human-readable status, safe to show in an admin UI.
    Never raises; never logs the SMTP password."""
    if not mailer_configured():
        logger.warning("mailer: SMTP_USER/SMTP_PASS not configured - email not sent")
        return False, "Email is not configured (SMTP_USER/SMTP_PASS missing)."

    if not to_addr or "@" not in to_addr:
        return False, "Recipient address looks invalid."

    if _rate_limited():
        logger.warning("mailer: hourly send limit reached, dropping message to %s", to_addr)
        return False, "Hourly send limit reached - try again later."

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = FROM_DISPLAY
    msg["To"] = to_addr
    msg.set_content(body_text)

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        logger.info("mailer: sent email to %s", to_addr)
        return True, "Sent."
    except smtplib.SMTPAuthenticationError:
        logger.error("mailer: SMTP authentication failed")
        return False, "Authentication failed - check SMTP_USER/SMTP_PASS in Render."
    except Exception as exc:  # noqa: BLE001 - surface a generic status, log detail server-side
        logger.error("mailer: send failed: %s", exc)
        return False, "Send failed - see server logs for detail."
