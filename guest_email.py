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
import config
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
    """Consented, not unsubscribed, and not on the suppression list: an
    address that bounced or complained was still counted (and sent to) as a
    subscriber (MOD-EML-7)."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT id, name, email, email_token FROM guest_contacts "
            "WHERE restaurant_id=? AND email IS NOT NULL AND TRIM(email) != '' "
            "AND email_consent=1 AND email_unsubscribed=0 "
            "AND LOWER(TRIM(email)) NOT IN (SELECT email FROM email_suppressions "
            "    WHERE scope IS NULL OR scope = '' OR scope = 'guest')",
            (restaurant_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def subscriber_count(restaurant_id, db_path: str = DB_PATH) -> int:
    return len(subscribers(restaurant_id, db_path=db_path))


def restaurant_for_token(token, db_path: str = DB_PATH):
    """The restaurant name an unsubscribe token belongs to, or None — read
    only, for the confirmation page a GET shows (MOD-EML-5)."""
    if not token:
        return None
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT r.name FROM guest_contacts g JOIN restaurants r ON r.id = g.restaurant_id "
            "WHERE g.email_token=?", (token,),
        ).fetchone()
        return row["name"] if row else None
    finally:
        conn.close()


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


# Sent on the owner's request before it returns; the rest go out from the
# scheduler tick (run_newsletter_sends). One synchronous Resend call per
# subscriber inside the request held a web thread for minutes on a big list
# and ran into Resend's per-team rate limit with no resume (MOD-EML-3).
NEWSLETTER_INLINE_BATCH = 20
NEWSLETTER_TICK_SECONDS = 120
# The same subject and text pressed again inside this window is the same
# newsletter: it resumes, and nobody already mailed is mailed again.
NEWSLETTER_DEDUPE_HOURS = 24

NEEDS_ADDRESS = ("Add your restaurant's mailing address before sending — the law (CAN-SPAM) "
                 "requires a physical address at the bottom of every newsletter.")


def send_newsletter(restaurant_id, body, subject=None, db_path: str = DB_PATH,
                    mailing_address=None) -> dict:
    """Send a generated newsletter to this restaurant's consented subscribers.

    The email goes out FROM Cavnar AI's verified sending domain on the
    restaurant's behalf, which is the only option without per-restaurant
    domain verification — so the restaurant's name leads the subject and the
    reply-to is the restaurant's own address, and it reads as theirs.

    The newsletter and every recipient are recorded first (guest_newsletters,
    guest_newsletter_recipients); each recipient is claimed before its send.
    The first NEWSLETTER_INLINE_BATCH go out now and the scheduler sends the
    rest. Pressing send again with the same subject and text resumes the same
    newsletter rather than mailing everyone again (MOD-EML-3). A newsletter
    needs the restaurant's mailing address (MOD-EML-6).
    """
    import hashlib
    from models import get_restaurant, update_restaurant

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

    address = " ".join(str(mailing_address or "").split())[:200]
    if address:
        update_restaurant(restaurant_id, {"mailing_address": address})
    elif not (getattr(restaurant, "mailing_address", None) or "").strip():
        return {"ok": False, "error": NEEDS_ADDRESS, "needs_mailing_address": True}

    digest = hashlib.sha256(f"{subject}\n{body_text}".encode("utf-8")).hexdigest()

    conn = get_conn(db_path)
    try:
        # One writer at a time from here to the commit: two presses of Send
        # at once must find one newsletter, not create two.
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id FROM guest_newsletters WHERE restaurant_id=? AND content_hash=? "
            "AND created_at >= datetime('now', ?) ORDER BY id DESC LIMIT 1",
            (restaurant_id, digest, f"-{NEWSLETTER_DEDUPE_HOURS} hours")).fetchone()
        if row:
            newsletter_id = row["id"]
        else:
            newsletter_id = conn.execute(
                "INSERT INTO guest_newsletters (restaurant_id, subject, body, content_hash, total) "
                "VALUES (?,?,?,?,?)",
                (restaurant_id, subject, body_text, digest, len(people))).lastrowid
            conn.executemany(
                "INSERT OR IGNORE INTO guest_newsletter_recipients (newsletter_id, contact_id, email) "
                "VALUES (?,?,?)", [(newsletter_id, p["id"], p["email"]) for p in people])
            # Tokens are minted only where none exists. A plain UPDATE here let
            # two concurrent sends overwrite each other's tokens, killing the
            # other batch's unsubscribe links (MOD-EML-3).
            conn.executemany("UPDATE guest_contacts SET email_token=? WHERE id=? AND email_token IS NULL",
                             [(_token(), p["id"]) for p in people if not p.get("email_token")])
        conn.commit()
    finally:
        conn.close()

    result = _send_batch(newsletter_id, limit=NEWSLETTER_INLINE_BATCH, db_path=db_path)
    status = newsletter_status(newsletter_id, db_path=db_path)
    return {"ok": True, "newsletter_id": newsletter_id, "subject": subject,
            "sent": status["sent"], "failed": status["failed"], "total": status["total"],
            "queued": status["pending"], "this_batch": result["sent"]}


def newsletter_status(newsletter_id, db_path: str = DB_PATH) -> dict:
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT status, COUNT(*) AS n FROM guest_newsletter_recipients "
                            "WHERE newsletter_id=? GROUP BY status", (newsletter_id,)).fetchall()
    finally:
        conn.close()
    counts = {r["status"]: r["n"] for r in rows}
    return {"sent": counts.get("sent", 0), "failed": counts.get("failed", 0),
            "pending": counts.get("pending", 0) + counts.get("sending", 0),
            "total": sum(counts.values())}


def _render(restaurant, person, subject, paragraphs, base):
    import html as _html
    import emails as _emails
    unsub = f"{base}/e/{person['email_token']}"
    greeting = ""
    if (person.get("name") or "").strip():
        greeting = (f'<p style="font-size:15px;line-height:1.7;color:#1a1714;margin:0 0 14px">'
                    f'Hi {_html.escape(person["name"].split()[0])} —</p>')
    address = _html.escape((getattr(restaurant, "mailing_address", None) or "").strip())
    inner = (
        f'<p style="font-size:11px;letter-spacing:1px;text-transform:uppercase;color:#7a736a;margin:0 0 14px">'
        f'{_html.escape(restaurant.name)}</p>'
        + greeting + paragraphs +
        f'<hr style="border:none;border-top:1px solid #e0dbd0;margin:22px 0"/>'
        f'<p style="font-size:11px;color:#7a736a;margin:0">You get this because you joined '
        f'{_html.escape(restaurant.name)}\'s list. '
        f'<a href="{unsub}" style="color:#7a736a;text-decoration:underline">Unsubscribe</a>.</p>'
        f'<p style="font-size:11px;color:#7a736a;margin:6px 0 0">{_html.escape(restaurant.name)} · {address}</p>'
    )
    payload = {
        # display_from: a comma or quote in the name split this header into
        # two mailboxes (MOD-EML-2).
        "from": _emails.display_from(restaurant.name),
        "to": [person["email"]],
        "subject": subject,
        "html": _emails._branded_email(inner),
        "headers": {"List-Unsubscribe": f"<{unsub}>",
                    "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"},
    }
    if restaurant.owner_email:
        payload["reply_to"] = restaurant.owner_email
    return payload


def _send_batch(newsletter_id, limit=None, max_seconds=None, db_path: str = DB_PATH) -> dict:
    """Send pending recipients of one newsletter, each claimed first. A send
    that raises (the process going away) gives its claim back."""
    import html as _html
    import time as _time
    import emails as _emails
    from models import get_restaurant

    conn = get_conn(db_path)
    try:
        nl = conn.execute("SELECT * FROM guest_newsletters WHERE id=?", (newsletter_id,)).fetchone()
    finally:
        conn.close()
    if not nl:
        return {"sent": 0, "failed": 0}
    restaurant = get_restaurant(nl["restaurant_id"])
    if not restaurant:
        return {"sent": 0, "failed": 0}
    base = config.base_url()
    paragraphs = "".join(
        f'<p style="font-size:15px;line-height:1.7;color:#1a1714;margin:0 0 14px">{_html.escape(p)}</p>'
        for p in nl["body"].split("\n\n") if p.strip()
    )
    started = _time.monotonic()
    sent = failed = done = 0
    while limit is None or done < limit:
        if max_seconds is not None and _time.monotonic() - started > max_seconds:
            break
        conn = get_conn(db_path)
        try:
            row = conn.execute(
                "SELECT r.id, r.contact_id, r.email, g.name, g.email_token, g.email_unsubscribed "
                "FROM guest_newsletter_recipients r LEFT JOIN guest_contacts g ON g.id = r.contact_id "
                "WHERE r.newsletter_id=? AND r.status='pending' ORDER BY r.id LIMIT 1",
                (newsletter_id,)).fetchone()
            if row is None:
                conn.execute("UPDATE guest_newsletters SET completed_at=datetime('now') "
                             "WHERE id=? AND completed_at IS NULL", (newsletter_id,))
                conn.commit()
                break
            claimed = conn.execute(
                "UPDATE guest_newsletter_recipients SET status='sending', claimed_at=datetime('now') "
                "WHERE id=? AND status='pending'", (row["id"],)).rowcount == 1
            conn.commit()
        finally:
            conn.close()
        if not claimed:
            continue
        done += 1
        if row["email_unsubscribed"] or not row["email_token"]:
            # Unsubscribed (or deleted) since the newsletter was recorded.
            _finish_recipient(row["id"], "skipped", None, db_path)
            continue
        person = {"email": row["email"], "name": row["name"], "email_token": row["email_token"]}
        try:
            result = _emails.deliver(_render(restaurant, person, nl["subject"], paragraphs, base),
                                     restaurant_id=nl["restaurant_id"], email_type="guest_newsletter")
            ok = bool(getattr(result, "ok", result))
            err = None if ok else str(getattr(result, "error", "") or "")[:300]
        except Exception as e:
            log.warning("newsletter send failed for %s: %s", row["email"], e)
            ok, err = False, str(e)[:300]
        except BaseException:
            _finish_recipient(row["id"], "pending", None, db_path, only_if="sending")
            raise
        _finish_recipient(row["id"], "sent" if ok else "failed", err, db_path)
        if ok:
            sent += 1
        else:
            failed += 1
    return {"sent": sent, "failed": failed}


def _finish_recipient(recipient_id, status, error, db_path, only_if=None):
    conn = get_conn(db_path)
    try:
        sql = ("UPDATE guest_newsletter_recipients SET status=?, error=?, "
               "sent_at=CASE WHEN ?='sent' THEN datetime('now') ELSE sent_at END WHERE id=?")
        args = [status, error, status, recipient_id]
        if only_if:
            sql += " AND status=?"
            args.append(only_if)
        conn.execute(sql, args)
        conn.commit()
    finally:
        conn.close()


def run_newsletter_sends(db_path: str = DB_PATH, max_seconds=None) -> dict:
    """Scheduler tick: send what is left of every unfinished newsletter,
    bounded in wall-clock time. The recipient statuses are the cursor.

    A recipient left 'sending' for 30 minutes belonged to a process that died
    mid-send: it may have been delivered, so it is marked failed rather than
    sent twice."""
    import time as _time
    if max_seconds is None:
        max_seconds = NEWSLETTER_TICK_SECONDS
    conn = get_conn(db_path)
    try:
        conn.execute("UPDATE guest_newsletter_recipients SET status='failed', "
                     "error='interrupted while sending' "
                     "WHERE status='sending' AND claimed_at < datetime('now','-30 minutes')")
        conn.commit()
        open_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM guest_newsletters WHERE completed_at IS NULL ORDER BY id").fetchall()]
    finally:
        conn.close()
    started = _time.monotonic()
    totals = {"newsletters": 0, "sent": 0, "failed": 0}
    for newsletter_id in open_ids:
        left = max_seconds - (_time.monotonic() - started)
        if left <= 0:
            break
        out = _send_batch(newsletter_id, max_seconds=left, db_path=db_path)
        totals["newsletters"] += 1
        totals["sent"] += out["sent"]
        totals["failed"] += out["failed"]
    return totals
