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

Consent and opt-out belong to the ADDRESS, per restaurant (fix round B,
9/28/26: MB-12, CS-2, CS-11). An unsubscribe writes the lower-cased address
to guest_email_optouts for that restaurant, and every contact row carrying
the address honours it — the same address on two rows (a couple with two
phones) got two copies and kept getting mail after one unsubscribe. The
public form never clears an opt-out, never moves a consented newsletter to a
new address without the box ticked, and a new address ticked in gets its own
consent stamp and its own unsubscribe token. A complaint's cross-restaurant
guest suppression (email_suppressions, scope 'guest') is unchanged.
"""
import json
import logging
import config
import re
import secrets

import models as _models_mod
from models import DB_PATH
from ai_utils import create_with_retry, extract_text, get_client, model_for

log = logging.getLogger(__name__)


def get_conn(db_path=None):
    """models.get_conn, resolved at call time — CLAUDE.md's bound-import
    hazard: the Resend webhook reaches unsubscribe_address with no db_path,
    and a bound copy would never see a patched models.get_conn."""
    return _models_mod.get_conn(db_path) if db_path is not None else _models_mod.get_conn()


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]+$")


def valid_email(value) -> str:
    value = (value or "").strip().lower()
    return value if _EMAIL_RE.match(value) else ""


def _token() -> str:
    return secrets.token_urlsafe(16)


def _addr(email) -> str:
    """The form an address is compared and suppressed in."""
    return (email or "").strip().lower()


def _opted_out(conn, restaurant_id, email) -> bool:
    return conn.execute("SELECT 1 FROM guest_email_optouts WHERE restaurant_id=? AND email=? LIMIT 1",
                        (restaurant_id, _addr(email))).fetchone() is not None


def set_guest_email(contact_id, restaurant_id, email, consent=False, db_path: str = DB_PATH) -> bool:
    """Attach an address to an existing guest. `consent=True` only ever comes
    from the guest's own opt-in submission (the public join form).

    What the form may do, and what it may not (MB-12 / CS-11):
      - It never clears an opt-out. A ticked box on an address this
        restaurant's guests unsubscribed (guest_email_optouts, or the row's
        own flag) leaves it unsubscribed: anyone can type anyone's address.
      - An unticked box never moves a consented newsletter to a new address:
        the row keeps the address that consented and the form's address is
        dropped. An unticked address on a row with no live consent is kept,
        unconsented, and never mailed.
      - A ticked box on a NEW address consents that address only: a fresh
        email_consent_at and a fresh unsubscribe token, so the old address's
        links (which resolve through guest_newsletter_recipients.email_token)
        can never unsubscribe, or reach, the new one.
      - The same address ticked again keeps its first consent stamp."""
    email = valid_email(email)
    if not email:
        return False
    from time_utils import restaurant_now_by_id
    now_iso = restaurant_now_by_id(restaurant_id, naive=True).isoformat()
    conn = get_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT email, email_consent, email_unsubscribed, email_token FROM guest_contacts "
                           "WHERE id=? AND restaurant_id=?", (contact_id, restaurant_id)).fetchone()
        if not row:
            conn.rollback()
            return False
        out = _opted_out(conn, restaurant_id, email)
        current = _addr(row["email"])
        if current == email:
            if consent and not row["email_consent"] and not row["email_unsubscribed"] and not out:
                conn.execute(
                    "UPDATE guest_contacts SET email_consent=1, email_consent_at=?, "
                    "email_token=COALESCE(email_token,?) WHERE id=? AND restaurant_id=?",
                    (now_iso, _token(), contact_id, restaurant_id))
            elif out and not row["email_unsubscribed"]:
                conn.execute("UPDATE guest_contacts SET email_unsubscribed=1 WHERE id=? AND restaurant_id=?",
                             (contact_id, restaurant_id))
        else:
            live = bool(current and row["email_consent"] and not row["email_unsubscribed"]
                        and not _opted_out(conn, restaurant_id, current))
            if live and not consent:
                conn.rollback()
                return True
            granted = bool(consent) and not out
            conn.execute(
                "UPDATE guest_contacts SET email=?, email_consent=?, email_consent_at=?, "
                "email_unsubscribed=?, email_token=? WHERE id=? AND restaurant_id=?",
                (email, 1 if granted else 0, now_iso if granted else None, 1 if out else 0,
                 _token(), contact_id, restaurant_id))
        conn.commit()
        return True
    finally:
        conn.close()


def _subscriber_rows(restaurant_id, db_path: str = DB_PATH) -> list:
    """Every consented row: not unsubscribed, not opted out by address at
    this restaurant, and not on the suppression list (a bounce, or a guest
    complaint anywhere: MOD-EML-7). One address can be on several rows."""
    from models import suppressed_scope_sql
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT id, name, email, email_token, last_visit, visit_count FROM guest_contacts "
            "WHERE restaurant_id=? AND email IS NOT NULL AND TRIM(email) != '' "
            "AND email_consent=1 AND COALESCE(email_unsubscribed,0)=0 "
            "AND LOWER(TRIM(email)) NOT IN (SELECT email FROM email_suppressions "
            "    WHERE " + suppressed_scope_sql("guest") + ") "
            "AND LOWER(TRIM(email)) NOT IN (SELECT email FROM guest_email_optouts WHERE restaurant_id=?) "
            "ORDER BY id",
            (restaurant_id, restaurant_id),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _one_per_address(people) -> list:
    """The first row per lower-cased address: one address, one copy."""
    seen, out = set(), []
    for p in people:
        key = _addr(p.get("email"))
        if key and key not in seen:
            seen.add(key)
            out.append(p)
    return out


def subscribers(restaurant_id, segment=None, db_path: str = DB_PATH) -> list:
    """Who a newsletter goes to: consented rows (_subscriber_rows), narrowed
    to `segment` the way a text's audience is (guest_marketing.filter_segment)
    and THEN one per address, so a row in the audience is never dropped for a
    same-address row outside it (CS-2)."""
    people = _subscriber_rows(restaurant_id, db_path=db_path)
    if segment and segment != "all":
        from guest_marketing import filter_segment
        people = filter_segment(restaurant_id, people, segment)
    return _one_per_address(people)


def subscriber_count(restaurant_id, db_path: str = DB_PATH) -> int:
    return len(subscribers(restaurant_id, db_path=db_path))


def segment_counts(restaurant_id, db_path: str = DB_PATH) -> dict:
    """Each audience's size by email, beside guest_marketing.segment_counts'
    by text: the Campaign Studio sends both to one audience. Addresses, not
    rows, from one read of the list."""
    from guest_marketing import SEGMENTS, filter_segment
    people = _subscriber_rows(restaurant_id, db_path=db_path)
    return {key: len(_one_per_address(filter_segment(restaurant_id, people, key))) for key in SEGMENTS}


def _token_target(conn, token):
    """(restaurant_id, restaurant name, address) an unsubscribe token stands
    for, or None. The address an email actually went to wins
    (guest_newsletter_recipients.email_token): a contact whose address
    changed has a new token, and the old email's link still names the old
    address — and still works after the contact row is deleted (EML-20)."""
    if not token:
        return None
    row = conn.execute(
        "SELECT n.restaurant_id, r.email, x.name FROM guest_newsletter_recipients r "
        "JOIN guest_newsletters n ON n.id = r.newsletter_id JOIN restaurants x ON x.id = n.restaurant_id "
        "WHERE r.email_token=? ORDER BY r.id DESC LIMIT 1", (token,)).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT g.restaurant_id, g.email, x.name FROM guest_contacts g "
            "JOIN restaurants x ON x.id = g.restaurant_id WHERE g.email_token=?", (token,)).fetchone()
    if row is None or not _addr(row["email"]):
        return None
    return row["restaurant_id"], row["name"], _addr(row["email"])


def restaurant_for_token(token, db_path: str = DB_PATH):
    """The restaurant name an unsubscribe token belongs to, or None — read
    only, for the confirmation page a GET shows (MOD-EML-5)."""
    conn = get_conn(db_path)
    try:
        found = _token_target(conn, token)
        return found[1] if found else None
    finally:
        conn.close()


def unsubscribe_address(restaurant_id, email, source: str = "link", db_path: str = DB_PATH) -> bool:
    """This address gets no more of this restaurant's newsletters, whichever
    contact row carries it now or later: the opt-out is written by address
    (guest_email_optouts) and mirrored onto every row with it, which is what
    the lists and counts read. Idempotent. `source`: link / one_click /
    complaint."""
    addr = _addr(email)
    if not (restaurant_id and addr):
        return False
    conn = get_conn(db_path)
    try:
        conn.execute("INSERT OR IGNORE INTO guest_email_optouts (restaurant_id, email, source) VALUES (?,?,?)",
                     (restaurant_id, addr, (source or "")[:20] or None))
        conn.execute("UPDATE guest_contacts SET email_unsubscribed=1 "
                     "WHERE restaurant_id=? AND LOWER(TRIM(email))=?", (restaurant_id, addr))
        conn.commit()
        return True
    finally:
        conn.close()


def unsubscribe(token, source: str = "link", db_path: str = DB_PATH):
    """The /e/ link and the List-Unsubscribe one-click: the ADDRESS the
    token's email went to is unsubscribed from this restaurant (CS-2), not
    one contact row. Returns the restaurant name for the confirmation page,
    or None."""
    conn = get_conn(db_path)
    try:
        found = _token_target(conn, token)
    finally:
        conn.close()
    if not found:
        return None
    restaurant_id, name, addr = found
    unsubscribe_address(restaurant_id, addr, source=source, db_path=db_path)
    return name


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


# ── Campaign Studio (9/28/26): the email drafted, designed and previewed ──
# One prompt drafts a text, an email and a social post. The email carries a
# headline, a photo from the restaurant's own media library and one button,
# and the page shows it through preview(): the same _render the send uses,
# so what the owner approves is what guests get.

_URL_RE = re.compile(r"^https?://[^\s<>\"']+$", re.I)


def clean_design(design, restaurant_id, db_path: str = DB_PATH) -> dict:
    """What an email carries besides its text, in the shapes the frame
    renders: a headline, a preheader, a button (its label, and a link only
    when it is http(s)) and a photo from THIS restaurant's media library by
    id - never an image URL from the request, which could be anyone's
    picture or a tracking pixel. Anything else is dropped."""
    d = design if isinstance(design, dict) else {}

    def line(key, n):
        return " ".join(str(d.get(key) or "").split())[:n]

    out = {"headline": line("headline", 120), "preheader": line("preheader", 140),
           "button_label": line("button_label", 40), "button_url": "", "image_media_id": None}
    url = str(d.get("button_url") or "").strip()[:500]
    if url and _URL_RE.match(url):
        out["button_url"] = url
    try:
        mid = int(d.get("image_media_id")) if d.get("image_media_id") not in (None, "") else None
    except (TypeError, ValueError):
        mid = None
    if mid:
        from marketing_media import get_media_token
        if get_media_token(mid, restaurant_id, db_path=db_path):
            out["image_media_id"] = mid
    return out


def _image_url(design, restaurant_id, base, db_path: str = DB_PATH) -> str:
    if not (design or {}).get("image_media_id"):
        return ""
    from marketing_media import get_media_token, media_url
    token = get_media_token(design["image_media_id"], restaurant_id, db_path=db_path)
    return media_url(base, token) if token else ""


def _paragraphs(body_text: str) -> str:
    import html as _html
    from emails import BRAND
    return "".join(
        f'<p style="font-size:16px;line-height:1.7;color:{BRAND["body"]};margin:0 0 16px">'
        f'{_html.escape(p.strip()).replace(chr(10), "<br>")}</p>'
        for p in (body_text or "").split("\n\n") if p.strip())


_NEWSLETTER_KEYS = ("subject", "preheader", "headline", "body", "button")


def draft_newsletter(restaurant, goal: str = "", topic: str = "") -> dict:
    """AI-drafts the Campaign Studio's email from the owner's goal: subject,
    preheader, headline, a short letter and the button's words. The same
    guards as the guest text (guest_marketing.draft_campaign_message): no
    offer the owner never wrote (invented_offers), and every field through
    the Response Validation Layer on a public surface - a refusal names why,
    "newsletter copy rejected: ...". Raises ValueError when the model's JSON
    can't be read."""
    from marketing import get_profile_for_restaurant, refusal_detail
    from guest_marketing import invented_offers
    import data_health

    p = get_profile_for_restaurant(restaurant.id)
    never = f" Never use these words or phrases: {p['never_say']}." if p.get("never_say") else ""
    menu = (f" Menu & current specials: {p['menu_notes']}. Reference something specific when it fits."
            if p.get("menu_notes") else "")
    goal_clause = f"What the owner wants this email to do, in their words: {goal}.\n" if goal else ""
    topic_clause = f"Topic/specifics to include: {topic}.\n" if topic else ""
    prompt = (
        f"Write a short email from {p['name']}, a {p['vibe']} in {p['neighborhood']}, to guests who joined its list.\n"
        f"Brand voice: {p['voice']}. Known for: {p['known_for']}.{never}{menu}\n"
        f"{goal_clause}{topic_clause}\n"
        "Return ONLY a JSON object, no markdown, with exactly these keys:\n"
        '  "subject": the subject line, under 8 words, no emoji\n'
        '  "preheader": one line under 90 characters that adds to the subject (the inbox shows it after the subject)\n'
        '  "headline": the email\'s big line, under 8 words\n'
        '  "body": 2 or 3 short paragraphs separated by a blank line, under 80 words in all, as if the owner wrote '
        "it between shifts. No greeting (one is added), no links\n"
        '  "button": 2 to 4 words for its one button, like "Book a table" or "See the menu"\n'
        "\nHard rules. A draft that breaks one is thrown away:\n"
        "1. No offer (a discount, percentage or dollars off, a free item, half price, buy-one-get-one, anything "
        "on the house) unless the owner's words or the menu above say it, in those words.\n"
        "2. No dish, drink, event, date, price or detail of the room that is not written above.\n"
        # Drafts told guests "Tuesdays have been quiet", "someone asked about
        # you the other day" and "happy hour is back": a slow day, a thing
        # nobody said and a change nobody made, under the owner's name.
        "3. No story: nothing anyone said, asked, noticed or did, and nothing about how busy or slow it is. "
        "The owner's goal is theirs, not the guest's: never say or hint that a night is slow or quiet, or that "
        "the restaurant wants to fill tables (\"Let's fill our Tuesday tables\" is exactly what not to write).\n"
        "4. Nothing is new, back, started, better or changed unless the owner's words say so.\n"
        "5. It goes to many guests at once: never say how long it has been since a guest's visit.\n"
        "6. Give no reason or cause for anything: no 'because', 'due to', 'thanks to' or 'since'.\n"
        "7. No phone numbers or links."
    )
    message = create_with_retry(
        get_client(),
        model=model_for("guest_marketing"),
        max_tokens=700,
        messages=[{"role": "user", "content": prompt}],
        restaurant_id=restaurant.id,
        action="guest_newsletter_draft",
        # Rests on no data source: a guest email drafted from the owner's goal.
        readiness=data_health.NOT_APPLICABLE,
    )
    if getattr(message, "stop_reason", None) == "max_tokens":
        raise ValueError("newsletter copy was truncated")
    raw = extract_text(message).strip()
    found = re.search(r"\{.*\}", raw, re.S)
    try:
        data = json.loads(found.group(0)) if found else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        # Billed, and useless: the ledger says so (fix round G, #52).
        from ai_utils import mark_outcome
        mark_outcome(message, "unparseable", reason="newsletter copy was not JSON")
        raise ValueError("newsletter copy was unreadable")
    fields = {k: str(data.get(k) or "").strip() for k in _NEWSLETTER_KEYS}
    for k in ("subject", "preheader", "headline", "button"):
        fields[k] = " ".join(fields[k].split())
    fields["subject"], fields["preheader"] = fields["subject"][:140], fields["preheader"][:140]
    fields["headline"], fields["button"] = fields["headline"][:120], fields["button"][:40]
    fields["body"] = re.sub(r"\n{3,}", "\n\n", fields["body"].replace("\r", "")).strip()[:1500]
    if not (fields["subject"] and fields["body"]):
        from ai_utils import mark_outcome
        mark_outcome(message, "unparseable", reason="newsletter copy had no subject or body")
        raise ValueError("newsletter copy was unreadable")

    offer_source = f"{topic} {goal} {p.get('menu_notes') or ''}"
    offers = invented_offers(" ".join(fields.values()), offer_source)
    if offers:
        raise ValueError("newsletter copy rejected: it offers " + ", ".join(offers[:3])
                         + ", which nobody told Cavnar AI the restaurant is running")
    owner_words = f"{goal} {topic}".strip()
    for k in _NEWSLETTER_KEYS:
        if not fields[k]:
            continue
        unasked = _unasked_contact_details(fields[k], owner_words)
        if unasked:
            raise ValueError(f"newsletter copy rejected: the copy contains {unasked}, which the owner never wrote")
        checked = _validate_copy(fields[k], restaurant.id, p, owner_words)
        if checked.verdict is not None and checked.verdict.verdict == "refuse":
            raise ValueError(f"newsletter copy rejected: {refusal_detail(checked.verdict)}")
        fields[k] = str(checked)
    return {"subject": fields["subject"], "preheader": fields["preheader"], "headline": fields["headline"],
            "body": fields["body"], "button_label": fields["button"]}


def _contact_details(text) -> list:
    """(kind, span) for every link, email address and phone number in
    `text`, by the same patterns the public-reply guard refuses them with."""
    from ai_guard import _URL_RE as _link, _EMAIL_RE as _mail, _PHONE_RE as _phone
    out = []
    for m in re.finditer(r"\S+", text or ""):
        word = m.group(0)
        if _link.search(word):
            out.append(("a link", word.rstrip(".,;:!?)")))
        elif _mail.search(word):
            out.append(("an email address", _mail.search(word).group(0)))
    out += [("a phone number", m.group(0).strip()) for m in _phone.finditer(text or "")]
    return out


def _unasked_contact_details(text, owner_words) -> str:
    """What kind of contact detail the drafted `text` carries that the
    owner's own words don't ("a link", "a phone number"), or "". The prompt
    says "No phone numbers or links"; this is what holds it (CS-10) — one
    the owner typed may appear, as they typed it."""
    def plain(s):
        s = re.sub(r"https?://|www\.", "", (s or "").lower())
        return re.sub(r"[^0-9a-z@./]", "", s).rstrip("/")

    said, said_digits = plain(owner_words), re.sub(r"\D", "", owner_words or "")
    for kind, span in _contact_details(text):
        if kind == "a phone number":
            digits = re.sub(r"\D", "", span)
            if digits and digits[-10:] in said_digits:
                continue
        elif plain(span) and plain(span) in said:
            continue
        return kind
    return ""


def _validate_copy(text, restaurant_id, profile, source):
    """One field of the drafted email after the Response Validation Layer,
    with marketing's context: the owner's words (`source`) as the offer
    source, the never-say list, the restaurant's own names and no other
    tenant's.

    On the guest_sms surface — a guest's inbox, held to the guest text's
    rules, not a caption's: social_post let a link, a phone number and an
    email address through, so the prompt's "No phone numbers or links" was
    never enforced (CS-10). A field whose only contact details are the
    owner's own (_unasked_contact_details has cleared them) goes on
    social_post, which differs from guest_sms only in letting those through."""
    import response_validation as rv
    from marketing import marketing_context
    surface = "social_post" if _contact_details(text) else "guest_sms"
    return rv.enforce(text or "", marketing_context(restaurant_id, surface, profile, topic=source,
                                                    action="guest_newsletter_draft"), marker=False)


# The greeting the preview shows. Never a real guest's name: the preview is
# on screen for anyone with Marketing view, and it read every subscriber per
# keystroke to find one (CS-19).
PREVIEW_FIRST_NAME = "Alex"


def preview(restaurant_id, subject="", body="", design=None, base=None, db_path: str = DB_PATH) -> dict:
    """The email exactly as a guest gets it - _render, the frame, the
    preheader - for the page's live preview. The greeting uses a stand-in
    first name (PREVIEW_FIRST_NAME), as a named guest's copy would."""
    import emails as _emails
    from models import get_restaurant
    restaurant = get_restaurant(restaurant_id)
    if not restaurant:
        return {"ok": False, "error": "Restaurant not found."}
    auto_subject, body_text = _split_generated(body or "")
    subject = (subject or auto_subject or f"News from {restaurant.name}").strip()[:140]
    d = clean_design(design, restaurant_id, db_path=db_path)
    base = (base or config.base_url()).rstrip("/")
    person = {"email": "", "name": PREVIEW_FIRST_NAME, "email_token": "preview"}
    payload = _render(restaurant, person, subject, _paragraphs(body_text), base, d,
                      _image_url(d, restaurant_id, base, db_path))
    html = payload["html"]
    if payload.get("preheader"):
        html = _emails.with_preheader(html, payload["preheader"])
    return {"ok": True, "html": html, "subject": subject, "body": body_text, "preheader": d["preheader"],
            "image_url": _image_url(d, restaurant_id, base, db_path),
            "mailing_address": bool((getattr(restaurant, "mailing_address", None) or "").strip())}


# Sent on the owner's request before it returns; the rest go out from the
# scheduler tick (run_newsletter_sends). One synchronous Resend call per
# subscriber inside the request held a web thread for minutes on a big list
# and ran into Resend's per-team rate limit with no resume (MOD-EML-3). Twenty
# inline, at up to 18s each through a Resend brownout, still held one of four
# threads for ~6 minutes (CS-18): now at most three, and none started after
# NEWSLETTER_INLINE_SECONDS.
NEWSLETTER_INLINE_BATCH = 3
NEWSLETTER_INLINE_SECONDS = 5.0
NEWSLETTER_TICK_SECONDS = 120
# The same subject and text pressed again inside this window is the same
# newsletter: it resumes, and nobody already mailed is mailed again.
NEWSLETTER_DEDUPE_HOURS = 24

NEEDS_ADDRESS = ("Add your restaurant's mailing address before sending — the law (CAN-SPAM) "
                 "requires a physical address at the bottom of every newsletter.")
# No Resend key: every recipient used to be claimed and failed at once, for
# good (CS-3). The send is refused before anything is recorded.
NOT_CONFIGURED = ("Email sending isn't set up on this account yet, so nothing was sent. "
                  "Contact Cavnar AI support to turn it on.")


def _sending_configured() -> bool:
    import emails as _emails
    return bool(_emails._resend_key())


def _sent_on(restaurant, created_at) -> str:
    """A newsletter's day, M/D/YY in the restaurant's own zone."""
    from time_utils import local_iso, mdy
    return mdy(local_iso(created_at, getattr(restaurant, "timezone", None)))


def _unmailed(conn, newsletter_id, people) -> list:
    """`people` whose address this newsletter has no recipient row for."""
    mailed = {_addr(r["email"]) for r in conn.execute(
        "SELECT email FROM guest_newsletter_recipients WHERE newsletter_id=?", (newsletter_id,)).fetchall()}
    return [p for p in people if _addr(p["email"]) not in mailed]


def _add_recipients(conn, newsletter_id, people) -> int:
    """Record `people` on the newsletter (one row per contact) and mint an
    unsubscribe token where a contact has none. Tokens are minted only where
    none exists: a plain UPDATE let two concurrent sends overwrite each
    other's tokens, killing the other batch's unsubscribe links (MOD-EML-3)."""
    added = 0
    for p in people:
        added += conn.execute(
            "INSERT OR IGNORE INTO guest_newsletter_recipients (newsletter_id, contact_id, email) VALUES (?,?,?)",
            (newsletter_id, p["id"], p["email"])).rowcount
    conn.executemany("UPDATE guest_contacts SET email_token=? WHERE id=? AND email_token IS NULL",
                     [(_token(), p["id"]) for p in people if not p.get("email_token")])
    return added


def send_newsletter(restaurant_id, body, subject=None, db_path: str = DB_PATH,
                    mailing_address=None, design=None, segment=None) -> dict:
    """Send a generated newsletter to this restaurant's consented subscribers.

    The email goes out FROM Cavnar AI's verified sending domain on the
    restaurant's behalf, which is the only option without per-restaurant
    domain verification — so the restaurant's name leads the subject and the
    reply-to is the restaurant's own address, and it reads as theirs.

    The newsletter and every recipient are recorded first (guest_newsletters,
    guest_newsletter_recipients), one per address; each recipient is claimed
    before its send. The first NEWSLETTER_INLINE_BATCH go out now and the
    scheduler sends the rest. Pressing send again with the same subject and
    text resumes the same newsletter rather than mailing everyone again
    (MOD-EML-3) — and once it has finished, says so instead of reporting a
    send that mailed nobody (CS-15): {"already_sent": True, "sent_on",
    "new_subscribers"}, which send_to_new_subscribers answers. A newsletter
    needs the restaurant's mailing address (MOD-EML-6) and a configured
    sender (NOT_CONFIGURED). `design` is the Campaign Studio's look
    (clean_design) and `segment` its audience; both are part of what makes a
    press "the same newsletter".

    The reply reports sent, failed (and how many of those a retry can reach),
    skipped and queued separately — never the total as sent (CS-3).
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

    from guest_marketing import SEGMENTS
    segment = (segment or "all").strip().lower()
    if segment not in SEGMENTS:
        segment = "all"
    people = subscribers(restaurant_id, segment=segment, db_path=db_path)
    if not people:
        if segment != "all" and subscribers(restaurant_id, db_path=db_path):
            return {"ok": False, "error": "Nobody in that audience is on your email list yet."}
        return {"ok": False, "error": "Nobody has opted in to email yet — the join page collects it."}
    d = clean_design(design, restaurant_id, db_path=db_path)
    design_json = json.dumps(d, sort_keys=True) if any(v for v in d.values()) else None

    address = " ".join(str(mailing_address or "").split())[:200]
    if not address and not (getattr(restaurant, "mailing_address", None) or "").strip():
        return {"ok": False, "error": NEEDS_ADDRESS, "needs_mailing_address": True}
    if not _sending_configured():
        return {"ok": False, "error": NOT_CONFIGURED, "not_configured": True}
    if address:
        update_restaurant(restaurant_id, {"mailing_address": address})

    digest = hashlib.sha256(f"{subject}\n{body_text}\n{design_json or ''}\n{segment}".encode("utf-8")).hexdigest()

    resumed = False
    conn = get_conn(db_path)
    try:
        # One writer at a time from here to the commit: two presses of Send
        # at once must find one newsletter, not create two.
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id, created_at FROM guest_newsletters WHERE restaurant_id=? AND content_hash=? "
            "AND created_at >= datetime('now', ?) ORDER BY id DESC LIMIT 1",
            (restaurant_id, digest, f"-{NEWSLETTER_DEDUPE_HOURS} hours")).fetchone()
        if row:
            newsletter_id, resumed = row["id"], True
            open_left = conn.execute(
                "SELECT COUNT(*) FROM guest_newsletter_recipients WHERE newsletter_id=? "
                "AND status IN ('pending','sending')", (newsletter_id,)).fetchone()[0]
            if not open_left:
                new = len(_unmailed(conn, newsletter_id, people))
                conn.commit()
                status = newsletter_status(newsletter_id, db_path=db_path)
                on = _sent_on(restaurant, row["created_at"])
                since = ("no new subscribers since" if not new
                         else f"{new} new subscriber{'' if new == 1 else 's'} since")
                return {"ok": False, "already_sent": True, "newsletter_id": newsletter_id, "subject": subject,
                        "segment": segment, "sent_on": on, "new_subscribers": new,
                        "error": f"Already sent on {on} — {since}.", **_counts(status)}
        else:
            newsletter_id = conn.execute(
                "INSERT INTO guest_newsletters (restaurant_id, subject, body, content_hash, total, design, "
                "segment, segment_label) VALUES (?,?,?,?,?,?,?,?)",
                (restaurant_id, subject, body_text, digest, len(people), design_json, segment,
                 SEGMENTS[segment]["label"])).lastrowid
            _add_recipients(conn, newsletter_id, people)
        conn.commit()
    finally:
        conn.close()

    result = _send_batch(newsletter_id, limit=NEWSLETTER_INLINE_BATCH, max_seconds=NEWSLETTER_INLINE_SECONDS,
                         db_path=db_path)
    status = newsletter_status(newsletter_id, db_path=db_path)
    return {"ok": True, "newsletter_id": newsletter_id, "subject": subject, "segment": segment,
            "resumed": resumed, "this_batch": result["sent"], **_counts(status)}


def _counts(status) -> dict:
    return {"sent": status["sent"], "failed": status["failed"], "retryable": status["retryable"],
            "skipped": status["skipped"], "total": status["total"], "queued": status["pending"]}


def _owned_newsletter(conn, restaurant_id, newsletter_id):
    return conn.execute("SELECT * FROM guest_newsletters WHERE id=? AND restaurant_id=?",
                        (newsletter_id, restaurant_id)).fetchone()


def retry_failed(restaurant_id, newsletter_id, db_path: str = DB_PATH) -> dict:
    """Send again to this newsletter's recipients whose send failed for a
    reason a retry can fix — Resend down or rate-limiting, a timeout, no key
    at the time — and never to an address Resend rejected, a suppressed one
    or an unsubscribed one (CS-3). Idempotent: a retried recipient is
    pending, then sent or failed again, never mailed twice; a second press
    finds nothing to retry. The first few go now, the scheduler sends the rest."""
    conn = get_conn(db_path)
    try:
        nl = _owned_newsletter(conn, restaurant_id, newsletter_id)
    finally:
        conn.close()
    if not nl:
        return {"ok": False, "status": 404, "error": "That email isn't in your history."}
    if not _sending_configured():
        return {"ok": False, "status": 400, "error": NOT_CONFIGURED, "not_configured": True}
    conn = get_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        retried = conn.execute(
            "UPDATE guest_newsletter_recipients SET status='pending', error=NULL, claimed_at=NULL "
            "WHERE newsletter_id=? AND status='failed' AND retryable=1", (newsletter_id,)).rowcount
        if retried:
            conn.execute("UPDATE guest_newsletters SET completed_at=NULL WHERE id=?", (newsletter_id,))
        conn.commit()
    finally:
        conn.close()
    if retried:
        _send_batch(newsletter_id, limit=NEWSLETTER_INLINE_BATCH, max_seconds=NEWSLETTER_INLINE_SECONDS,
                    db_path=db_path)
    status = newsletter_status(newsletter_id, db_path=db_path)
    return {"ok": True, "newsletter_id": newsletter_id, "retried": retried, **_counts(status)}


def send_to_new_subscribers(restaurant_id, newsletter_id, db_path: str = DB_PATH) -> dict:
    """The same newsletter to its audience's subscribers it never reached —
    the guests who joined (or joined that audience) since it went out
    (CS-15). They are added to it, so it stays one newsletter in history and
    nobody it already mailed is mailed again; a second press adds nobody."""
    from models import get_restaurant
    restaurant = get_restaurant(restaurant_id)
    conn = get_conn(db_path)
    try:
        nl = _owned_newsletter(conn, restaurant_id, newsletter_id)
    finally:
        conn.close()
    if not (nl and restaurant):
        return {"ok": False, "status": 404, "error": "That email isn't in your history."}
    if not (getattr(restaurant, "mailing_address", None) or "").strip():
        return {"ok": False, "status": 400, "error": NEEDS_ADDRESS, "needs_mailing_address": True}
    if not _sending_configured():
        return {"ok": False, "status": 400, "error": NOT_CONFIGURED, "not_configured": True}
    people = subscribers(restaurant_id, segment=nl["segment"] or "all", db_path=db_path)
    conn = get_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        added = _add_recipients(conn, newsletter_id, _unmailed(conn, newsletter_id, people))
        if added:
            conn.execute("UPDATE guest_newsletters SET total=total+?, completed_at=NULL WHERE id=?",
                         (added, newsletter_id))
        conn.commit()
    finally:
        conn.close()
    if added:
        _send_batch(newsletter_id, limit=NEWSLETTER_INLINE_BATCH, max_seconds=NEWSLETTER_INLINE_SECONDS,
                    db_path=db_path)
    status = newsletter_status(newsletter_id, db_path=db_path)
    return {"ok": True, "newsletter_id": newsletter_id, "added": added, **_counts(status)}


def newsletter_status(newsletter_id, db_path: str = DB_PATH) -> dict:
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT status, COUNT(*) AS n, SUM(CASE WHEN retryable=1 THEN 1 ELSE 0 END) AS r "
                            "FROM guest_newsletter_recipients WHERE newsletter_id=? GROUP BY status",
                            (newsletter_id,)).fetchall()
    finally:
        conn.close()
    counts = {r["status"]: r["n"] for r in rows}
    retryable = sum(int(r["r"] or 0) for r in rows if r["status"] == "failed")
    return {"sent": counts.get("sent", 0), "failed": counts.get("failed", 0), "retryable": retryable,
            "skipped": counts.get("skipped", 0),
            "pending": counts.get("pending", 0) + counts.get("sending", 0),
            "total": sum(counts.values())}


def _render(restaurant, person, subject, paragraphs, base, design=None, image_url=""):
    """One guest's newsletter: the restaurant's own frame
    (emails.guest_newsletter_email) with the design's headline, photo and
    button, the greeting, the letter and the CAN-SPAM footer."""
    import html as _html
    import emails as _emails
    B = _emails.BRAND
    d = design or {}
    unsub = f"{base}/e/{person['email_token']}"
    greeting = ""
    if (person.get("name") or "").strip():
        greeting = (f'<p style="font-size:16px;line-height:1.7;color:{B["ink"]};margin:0 0 16px">'
                    f'Hi {_html.escape(person["name"].split()[0])} —</p>')
    name = _html.escape(restaurant.name)
    address = _html.escape((getattr(restaurant, "mailing_address", None) or "").strip())
    footer = (f'You get this because you joined {name}\'s list. '
              f'<a href="{unsub}" style="color:{B["muted"]};text-decoration:underline">Unsubscribe</a>.'
              f'<br>{name}' + (f' · {address}' if address else ''))
    html = _emails.guest_newsletter_email(
        restaurant.name, greeting + paragraphs, headline=d.get("headline") or "", image_url=image_url or "",
        button_label=d.get("button_label") or "", button_url=d.get("button_url") or "", footer_html=footer)
    payload = {
        # display_from: a comma or quote in the name split this header into
        # two mailboxes (MOD-EML-2).
        "from": _emails.display_from(restaurant.name),
        "to": [person["email"]],
        "subject": subject,
        "html": html,
        "headers": {"List-Unsubscribe": f"<{unsub}>",
                    "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"},
    }
    if restaurant.owner_email:
        payload["reply_to"] = restaurant.owner_email
    if d.get("preheader"):
        payload["preheader"] = d["preheader"]
    return payload


def _failure(result):
    """(status, error, retryable) for a send deliver() did not accept. A
    suppressed address is a skip, not a failure: it was never going to be
    sent. A retry can fix Resend being down or rate-limiting, a timeout, a
    missing key or the flood guard; it cannot fix an address Resend
    rejected (any other 4xx)."""
    import emails as _emails
    err = str(getattr(result, "error", "") or "")[:300]
    code = getattr(result, "status_code", None)
    attempts = getattr(result, "attempts", 1)
    if err.startswith("recipient suppressed"):
        return "skipped", "suppressed", None
    if not attempts:
        return "failed", err, 1
    if code is None or code in _emails._RETRY_STATUS or code >= 500:
        return "failed", err, 1
    return "failed", err, 0


def _send_batch(newsletter_id, limit=None, max_seconds=None, db_path: str = DB_PATH) -> dict:
    """Send pending recipients of one newsletter, each claimed first. A send
    that raises (the process going away) gives its claim back.

    Nothing is claimed while no Resend key is configured: the recipients
    stay pending for when one is, rather than failing, all of them, for
    good (CS-3). A recipient is skipped, not sent, when its contact has
    since unsubscribed, lost consent, been deleted or changed address, or
    its address is opted out at this restaurant (CS-2)."""
    import time as _time
    import emails as _emails
    from models import get_restaurant

    if not _sending_configured():
        log.warning("newsletter %s held: RESEND_API_KEY is not set", newsletter_id)
        return {"sent": 0, "failed": 0, "held": True}
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
    base = config.base_url().rstrip("/")
    paragraphs = _paragraphs(nl["body"])
    try:
        design = json.loads(nl["design"] or "{}") if "design" in nl.keys() else {}
    except ValueError:
        design = {}
    image_url = _image_url(design, nl["restaurant_id"], base, db_path)
    started = _time.monotonic()
    sent = failed = done = 0
    while limit is None or done < limit:
        if max_seconds is not None and _time.monotonic() - started > max_seconds:
            break
        conn = get_conn(db_path)
        try:
            row = conn.execute(
                "SELECT r.id, r.contact_id, r.email, g.name, g.email_token, g.email_unsubscribed, "
                "       g.email_consent, g.email AS current_email, "
                "       EXISTS(SELECT 1 FROM guest_email_optouts o WHERE o.restaurant_id=? "
                "              AND o.email=LOWER(TRIM(r.email))) AS opted_out "
                "FROM guest_newsletter_recipients r LEFT JOIN guest_contacts g ON g.id = r.contact_id "
                "WHERE r.newsletter_id=? AND r.status='pending' ORDER BY r.id LIMIT 1",
                (nl["restaurant_id"], newsletter_id)).fetchone()
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
        if (row["email_unsubscribed"] or not row["email_token"] or not row["email_consent"]
                or row["opted_out"] or _addr(row["current_email"]) != _addr(row["email"])):
            # Unsubscribed, deleted or moved to another address since the
            # newsletter was recorded.
            _finish_recipient(row["id"], "skipped", None, db_path)
            continue
        person = {"email": row["email"], "name": row["name"], "email_token": row["email_token"]}
        message_id, retryable = None, None
        try:
            result = _emails.deliver(_render(restaurant, person, nl["subject"], paragraphs, base, design, image_url),
                                     restaurant_id=nl["restaurant_id"], email_type="guest_newsletter")
            ok = bool(getattr(result, "ok", result))
            status, err = "sent", None
            if not ok:
                status, err, retryable = _failure(result)
            message_id = getattr(result, "message_id", None)
        except Exception as e:
            log.warning("newsletter send failed for %s: %s", row["email"], e)
            status, err, retryable = "failed", str(e)[:300], 1
        except BaseException:
            _finish_recipient(row["id"], "pending", None, db_path, only_if="sending")
            raise
        _finish_recipient(row["id"], status, err, db_path, message_id=message_id,
                          email_token=row["email_token"], retryable=retryable)
        if status == "sent":
            sent += 1
        elif status == "failed":
            failed += 1
    return {"sent": sent, "failed": failed}


def _finish_recipient(recipient_id, status, error, db_path, only_if=None, message_id=None,
                      email_token=None, retryable=None):
    conn = get_conn(db_path)
    try:
        # message_id ties the recipient to email_log, where Resend's open and
        # click events land (newsletter_history); email_token is the
        # unsubscribe link this email carried (_token_target).
        sql = ("UPDATE guest_newsletter_recipients SET status=?, error=?, message_id=COALESCE(?, message_id), "
               "email_token=COALESCE(?, email_token), retryable=?, "
               "sent_at=CASE WHEN ?='sent' THEN datetime('now') ELSE sent_at END WHERE id=?")
        args = [status, error, message_id, email_token, retryable, status, recipient_id]
        if only_if:
            sql += " AND status=?"
            args.append(only_if)
        conn.execute(sql, args)
        conn.commit()
    finally:
        conn.close()


def opens_tracked(db_path: str = DB_PATH) -> bool:
    """Whether Resend's open events reach this platform at all. None in 90
    days means open tracking is off on the Resend side, and a newsletter's
    opens are then unknown - never zero."""
    conn = get_conn(db_path)
    try:
        return conn.execute("SELECT 1 FROM email_log WHERE opened_at IS NOT NULL "
                            "AND sent_at >= datetime('now','-90 days') LIMIT 1").fetchone() is not None
    finally:
        conn.close()


def newsletter_history(restaurant_id, limit: int = 20, db_path: str = DB_PATH) -> list:
    """Every newsletter, newest first: its subject, look, audience, how many
    were sent, failed (and how many of those a retry can reach: `retryable`),
    skipped and still queued, and - where open tracking is on and the send
    kept its message id - the opens and clicks RECORDED. Recorded, not read
    and not a floor (CS-7): Apple Mail opens mail nobody read (so opens run
    high), a reader with images off never registers, and a link scanner can
    click. Unsubscribe-link clicks are not counted (webhook_routes). None
    when unknown."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT n.id, n.subject, n.body, n.design, n.segment, n.segment_label, n.total, n.created_at, "
            "       n.completed_at, "
            "       SUM(CASE WHEN r.status='sent' THEN 1 ELSE 0 END) AS sent, "
            "       SUM(CASE WHEN r.status='failed' THEN 1 ELSE 0 END) AS failed, "
            "       SUM(CASE WHEN r.status='failed' AND r.retryable=1 THEN 1 ELSE 0 END) AS retryable, "
            "       SUM(CASE WHEN r.status='skipped' THEN 1 ELSE 0 END) AS skipped, "
            "       SUM(CASE WHEN r.status IN ('pending','sending') THEN 1 ELSE 0 END) AS pending, "
            "       SUM(CASE WHEN r.message_id IS NOT NULL THEN 1 ELSE 0 END) AS tracked, "
            "       SUM(CASE WHEN e.opened_at IS NOT NULL THEN 1 ELSE 0 END) AS opened, "
            "       SUM(CASE WHEN e.clicked_at IS NOT NULL THEN 1 ELSE 0 END) AS clicked "
            "FROM guest_newsletters n "
            "LEFT JOIN guest_newsletter_recipients r ON r.newsletter_id = n.id "
            "LEFT JOIN email_log e ON r.message_id IS NOT NULL AND e.message_id = r.message_id "
            "WHERE n.restaurant_id=? GROUP BY n.id ORDER BY n.id DESC LIMIT ?",
            (restaurant_id, limit)).fetchall()
    finally:
        conn.close()
    tracking = opens_tracked(db_path) if rows else False
    out = []
    for r in rows:
        item = dict(r)
        try:
            item["design"] = json.loads(item.get("design") or "{}")
        except ValueError:
            item["design"] = {}
        measured = tracking and (item.get("tracked") or 0) > 0
        item["opened"] = int(item["opened"] or 0) if measured else None
        item["clicked"] = int(item["clicked"] or 0) if measured else None
        for k in ("sent", "failed", "retryable", "skipped", "pending", "tracked"):
            item[k] = int(item.get(k) or 0)
        out.append(item)
    return out


def run_newsletter_sends(db_path: str = DB_PATH, max_seconds=None) -> dict:
    """Scheduler tick: send what is left of every unfinished newsletter,
    bounded in wall-clock time. The recipient statuses are the cursor.

    A recipient left 'sending' for 30 minutes belonged to a process that died
    mid-send: it may have been delivered, so it is marked failed (and not
    retryable) rather than sent twice."""
    import time as _time
    if max_seconds is None:
        max_seconds = NEWSLETTER_TICK_SECONDS
    conn = get_conn(db_path)
    try:
        conn.execute("UPDATE guest_newsletter_recipients SET status='failed', "
                     "error='interrupted while sending', retryable=0 "
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
        if out.get("held"):
            break
        totals["newsletters"] += 1
        totals["sent"] += out["sent"]
        totals["failed"] += out["failed"]
    return totals
