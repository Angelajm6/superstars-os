"""#276 — internal notification sends (blueprint §5 transition rule): on assignment /
stage handoff the assigned ESTIMATOR gets an email with a deep link into the app; on
sent_to_vp the VP (active c_suite) gets one. INTERNAL ONLY — no client-facing email
in this build (client packets are Build D, VP-Gmail-draft-first by decision).

STUB/RECORD PATTERN (the gate never depends on a live provider): every send is a
notification_log ROW first. A real SendGrid call happens ONLY when SENDGRID_API_KEY
is present in the environment (vaulted per the CLAUDE.md secrets rule — never on
disk); success flips sent=1, failure records error and the app carries on (a dead
mail provider must never block a stage click).

PII: subject/body carry the estimate CODE + ADDRESS only — never client names,
amounts, rates, or paths. Recipient email comes from users.email (internal staff).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime

_FROM = "dashboard@superstarscontracting.com"
_SENDGRID_URL = "https://api.sendgrid.com/v3/mail/send"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _base_url() -> str:
    return (os.environ.get("APP_BASE_URL") or "http://127.0.0.1:5050").rstrip("/")


def _live_key():
    return (os.environ.get("SENDGRID_API_KEY") or "").strip() or None


def _send_live(to_email, subject, body, html=None, *, from_name="SSC Dashboard",
               track_clicks=True, reply_to=None) -> tuple:
    """(sent, error). Only called when the key is present.

    `html` adds a text/html alternative next to the plain-text body (the plain
    part always goes first so text-only clients get it). `track_clicks=False`
    turns off SendGrid's link rewriting for transactional mail whose URL must
    stay exactly as sent (#301 password-reset links)."""
    try:
        import requests
        content = [{"type": "text/plain", "value": body}]
        if html:
            content.append({"type": "text/html", "value": html})
        payload = {
            "personalizations": [{"to": [{"email": to_email}]}],
            "from": {"email": _FROM, "name": from_name},
            "subject": subject,
            "content": content,
        }
        if reply_to:   # Claude's client emails: replies go to the person who approved the send
            payload["reply_to"] = {"email": reply_to}
        if not track_clicks:
            payload["tracking_settings"] = {"click_tracking": {"enable": False, "enable_text": False}}
        r = requests.post(
            _SENDGRID_URL,
            headers={"Authorization": f"Bearer {_live_key()}",
                     "Content-Type": "application/json"},
            json=payload, timeout=10)
        if r.status_code in (200, 202):
            return 1, None
        return 0, f"sendgrid {r.status_code}"
    except Exception as e:                      # never let mail break the request
        return 0, f"{type(e).__name__}"


def notify_user(conn, *, kind, user_id, subject, estimate_code=None, path="/estimating",
                extra_line="") -> int:
    """Log (and, when configured, send) one internal notification to `user_id`.
    Returns the notification_log row id. The row is the test-visible record."""
    u = conn.execute("SELECT email, COALESCE(display_name, full_name, email) AS nm "
                     "FROM users WHERE id=? AND is_active=1", (user_id,)).fetchone()
    email = u["email"] if u else None
    link = _base_url() + path + (f"#{estimate_code}" if estimate_code else "")
    body = (f"{subject}\n\n{extra_line}\n" if extra_line else f"{subject}\n\n") + \
        f"Open it here: {link}\n\n— SSC Dashboard (internal notification)"
    sent, error = (0, None)
    if email and _live_key():
        sent, error = _send_live(email, subject, body)
    elif not email:
        error = "no recipient"
    conn.execute(
        "INSERT INTO notification_log (kind, recipient_user_id, recipient_email, subject, "
        "deep_link, estimate_code, created_at, sent, error) VALUES (?,?,?,?,?,?,?,?,?)",
        (kind, user_id, email, subject, link, estimate_code, _now(), sent, error))
    conn.commit()
    logging.info(f"notify: kind={kind} user_id={user_id} sent={sent} err={error or '-'}")
    return conn.execute("SELECT MAX(id) AS m FROM notification_log").fetchone()["m"]


def notify_vps(conn, *, kind, subject, estimate_code=None, path="/", extra_line="") -> list:
    """Notify every ACTIVE c_suite user (the VP tier). Returns log row ids."""
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM users WHERE role='c_suite' AND status='active' AND is_active=1"
    ).fetchall()]
    return [notify_user(conn, kind=kind, user_id=uid, subject=subject,
                        estimate_code=estimate_code, path=path, extra_line=extra_line)
            for uid in ids]
