"""guest_email.py — the newsletter channel `weekly_email` was always writing for.

marketing.py has generated a "Weekly email" content type since this product
existed: a real newsletter, with two subject-line options and a first-name
sign-off. There was no list to send it to and no way to send it, so the only
thing an owner could do with a generated newsletter was select it and paste it
into their own mail client.

Consent mirrors the SMS side exactly, and for the same reason. An owner
typing a guest's address in is not that guest agreeing to a newsletter — only
the guest submitting the join page themselves sets email_consent. The one
difference is that email carries its own per-guest unsubscribe token, because
CAN-SPAM requires the link to work without the recipient identifying
themselves first.
"""
import logging
import os
import re
import secrets

from models import get_conn, DB_PATH

log = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]+$")


def valid_email(value) -> str:
    value = (value or "").strip().lower()
    return value if _EMAIL_RE.match(value) else ""


def _token() -> str:
    return secrets.token_urlsafe(16)


def set_guest_email(contact_id, restaurant_id, email, consent=False, db_path: str = DB_PATH) -> bool:
    """Attach an address to an existing guest. `consent=True` only ever comes
    from the guest's own opt-in submission."""
    email = valid_email(email)
    if not email:
        return False
    from time_utils import restaurant_now_by_id
    now_iso = restaurant_now_by_id(restaurant_id, naive=True).isoformat()
    conn = get_conn(db_path)
    try:
        if consent:
            conn.execute(
                "UPDATE guest_contacts SET email=?, email_consent=1, "
                "email_consent_at=COALESCE(email_consent_at,?), email_unsubscribed=0, "
                "email_token=COALESCE(email_token,?) WHERE id=? AND restaurant_id=?",
                (email, now_iso, _token(), contact_id, restaurant_id),
            )
        else:
            conn.execute(
                "UPDATE guest_contacts SET email=? WHERE id=? AND restaurant_id=?",
                (email, contact_id, restaurant_id),
            )
        conn.commit()
        return True
    finally:
        conn.close()


def subscribers(restaurant_id, db_path: str = DB_PATH) -> list:
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT id, name, email, email_token FROM guest_contacts "
            "WHERE restaurant_id=? AND email IS NOT NULL AND TRIM(email) != '' "
            "AND email_consent=1 AND email_unsubscribed=0",
            (restaurant_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def subscriber_count(restaurant_id, db_path: str = DB_PATH) -> int:
    return len(subscribers(restaurant_id, db_path=db_path))


def unsubscribe(token, db_path: str = DB_PATH):
    """Returns the restaurant name for the confirmation page, or None."""
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT g.id, r.name FROM guest_contacts g JOIN restaurants r ON r.id = g.restaurant_id "
            "WHERE g.email_token=?", (token,),
        ).fetchone()
        if not row:
            return None
        conn.execute("UPDATE guest_contacts SET email_unsubscribed=1 WHERE id=?", (row["id"],))
        conn.commit()
        return row["name"]
    finally:
        conn.close()


def _split_generated(body: str):
    """Split marketing.py's `weekly_email` output into (subject, body).

    That prompt asks for "SUBJECT LINE: (2 options)" and then "BODY:", so the
    raw generation is a working document, not an email — sending it verbatim
    mails guests the words "SUBJECT LINE:" and both candidate subjects. The
    first option becomes the subject, everything after the BODY marker becomes
    the letter, and the rest is dropped.

    Falls back to (no subject, whole text) when the markers aren't there,
    rather than guessing which line was meant to be the subject.
    """
    lines = (body or "").splitlines()
    body_at = None
    for i, line in enumerate(lines):
        if line.strip().upper().startswith("BODY"):
            body_at = i
            break
    if body_at is None:
        return "", (body or "").strip()

    subject = ""
    for line in lines[:body_at]:
        stripped = line.strip()
        if not stripped or stripped.upper().startswith("SUBJECT"):
            continue
        # "1. Truffle season starts Friday" / "- Truffle season..." / quoted
        candidate = re.sub(r"^[-*\d.)\s]+", "", stripped).strip().strip('"').strip("'")
        if candidate:
            subject = candidate
            break

    tail = lines[body_at].strip()
    rest = [tail.split(":", 1)[1].strip()] if ":" in tail and tail.split(":", 1)[1].strip() else []
    rest += lines[body_at + 1:]
    return subject[:140], "\n".join(rest).strip()


def send_newsletter(restaurant_id, body, subject=None, db_path: str = DB_PATH) -> dict:
    """Send a generated newsletter to this restaurant's consented subscribers.

    The email goes out FROM Cavnar AI's verified sending domain on the
    restaurant's behalf, which is the only option without per-restaurant
    domain verification — so the restaurant's name leads the subject and the
    reply-to is the restaurant's own address, and it reads as theirs.
    """
    import html as _html
    from models import get_restaurant
    import emails as _emails

    restaurant = get_restaurant(restaurant_id)
    if not restaurant:
        return {"ok": False, "error": "Restaurant not found."}

    auto_subject, body_text = _split_generated(body)
    subject = (subject or auto_subject or f"News from {restaurant.name}").strip()[:140]
    if not body_text:
        return {"ok": False, "error": "There's no newsletter text to send."}

    people = subscribers(restaurant_id, db_path=db_path)
    if not people:
        return {"ok": False, "error": "Nobody has opted in to email yet — the join page collects it."}

    base = (os.getenv("BASE_URL") or "https://dashboard.cavnar.ai").rstrip("/")
    paragraphs = "".join(
        f'<p style="font-size:15px;line-height:1.7;color:#1a1714;margin:0 0 14px">{_html.escape(p)}</p>'
        for p in body_text.split("\n\n") if p.strip()
    )

    sent, failed = 0, 0
    for person in people:
        token = person.get("email_token")
        if not token:
            token = _token()
            conn = get_conn(db_path)
            conn.execute("UPDATE guest_contacts SET email_token=? WHERE id=?", (token, person["id"]))
            conn.commit()
            conn.close()
        unsub = f"{base}/e/{token}"
        greeting = ""
        if (person.get("name") or "").strip():
            greeting = (f'<p style="font-size:15px;line-height:1.7;color:#1a1714;margin:0 0 14px">'
                        f'Hi {_html.escape(person["name"].split()[0])} —</p>')
        inner = (
            f'<p style="font-size:11px;letter-spacing:1px;text-transform:uppercase;color:#7a736a;margin:0 0 14px">'
            f'{_html.escape(restaurant.name)}</p>'
            + greeting + paragraphs +
            f'<hr style="border:none;border-top:1px solid #e0dbd0;margin:22px 0"/>'
            f'<p style="font-size:11px;color:#7a736a;margin:0">You get this because you joined '
            f'{_html.escape(restaurant.name)}\'s list. '
            f'<a href="{unsub}" style="color:#7a736a;text-decoration:underline">Unsubscribe</a>.</p>'
        )
        payload = {
            "from": f"{restaurant.name} <{_emails._from_email()}>",
            "to": [person["email"]],
            "subject": subject,
            "html": _emails._branded_email(inner),
            "headers": {"List-Unsubscribe": f"<{unsub}>",
                        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"},
        }
        if restaurant.owner_email:
            payload["reply_to"] = restaurant.owner_email
        try:
            result = _emails.deliver(payload, restaurant_id=restaurant_id,
                                     email_type="guest_newsletter")
            if getattr(result, "ok", bool(result)):
                sent += 1
            else:
                failed += 1
        except Exception as e:
            log.warning("newsletter send failed for %s: %s", person["email"], e)
            failed += 1

    return {"ok": True, "sent": sent, "failed": failed,
            "total": len(people), "subject": subject}
