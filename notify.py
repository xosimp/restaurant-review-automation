"""
notify.py — alert system via Twilio (SMS) and Resend (email).
Both channels use the same 6 alert toggles; delivery is controlled
by urgent_via_sms and urgent_via_email per restaurant.
"""
import os
import re
import config
import html as _html
import requests
import models
from models import DB_PATH

# get_conn is looked up on the models module at call time
# (models.get_conn(...)), never imported by name — see
# value_delivered.py's header comment for why: a bound
# `from models import get_conn` here would silently escape every
# test's `monkeypatch.setattr(models, "get_conn", ...)` redirection,
# which is exactly what was happening — every push_notification test
# was quietly reading and writing the developer's own local
# reviews.db instead of the test's isolated fixture database, and
# crashed with "no such table: alert_log" on a clean checkout with
# no such file (confirmed live on GitHub Actions, Sep 7 2026).


from emails import html_document as _html_doc  # one definition; emails reads its env lazily

TWILIO_SID     = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN   = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM    = os.getenv("TWILIO_FROM_NUMBER", "")
# A2P 10DLC campaigns are registered against a Messaging Service, not a bare
# number — sending via MessagingServiceSid is what lets Twilio route the
# message through the number's approved campaign rather than as a plain
# long-code send. Optional: falls back to TWILIO_FROM (still Twilio's
# documented fallback pairing on the number itself) when unset, so a
# deployment that hasn't added this yet keeps working exactly as before.
TWILIO_MESSAGING_SERVICE_SID = os.getenv("TWILIO_MESSAGING_SERVICE_SID", "")
# A Messaging Service maps to exactly one A2P 10DLC Campaign, and a phone
# number can only belong to one Campaign at a time — so a genuinely separate
# use case (staff signup OTP vs. the owner alert campaign) needs its own
# number and its own Messaging Service, not just its own campaign form.
# Optional; falls back to TWILIO_MESSAGING_SERVICE_SID (and from there to
# TWILIO_FROM) when unset, so nothing breaks before this is provisioned.
TWILIO_OTP_MESSAGING_SERVICE_SID = os.getenv("TWILIO_OTP_MESSAGING_SERVICE_SID", "")
# Guest-facing texts (campaigns, opt-in invites, review requests) are a third
# use case: marketing to diners, not alerts to owners. Riding the owner-alert
# campaign mixed the two, so a carrier filtering the promos would also filter
# owners' health alerts (MOD-MKT-11). Provisioning this service is what moves
# them; until it is set, guest texts keep the alert service they have always
# used (a plain From on the same number is the same campaign, only less
# reliably routed), and the mismatch is logged once per process.
TWILIO_GUEST_MESSAGING_SERVICE_SID = os.getenv("TWILIO_GUEST_MESSAGING_SERVICE_SID", "")
_guest_service_warned = False
from emails import _resend_key

def emails_sender(kind="client"):
    """One sender identity for everything an owner receives — see
    emails.SENDERS. Imported lazily for the same reason _html_doc is."""
    from emails import sender
    return sender(kind)

HEALTH_KEYWORDS = [
    "food poison", "food poisoning", "foodborne", "sick after", "got sick",
    "felt sick", "vomit", "threw up", "throw up", "diarrhea", "nausea after",
    "ill after", "hospital", "health department", "health inspector",
    "cockroach", "roach", "rat ", "rats ", "rodent", "bug in ", "insect in",
    "foreign object", "glass in", "metal in", "hair in", "mold", "mouldy",
    "raw chicken", "raw meat", "undercooked chicken", "salmonella", "ecoli", "e. coli",
]


_PHONE_EXTENSION = re.compile(r"\s*(?:x|ext\.?|extension|#)\s*\d+\s*$", re.IGNORECASE)


def _normalize_phone(phone: str) -> str:
    # A text cannot reach an extension, and its digits appended to the number
    # made "(630) 555-0123 x45" into +630555012345, someone else's number
    # abroad (MOD-A6-optin-17).
    phone = _PHONE_EXTENSION.sub("", phone or "")
    digits = "".join(c for c in phone if c.isdigit() or c == "+")
    if digits.startswith("+"):
        return digits
    digits_only = "".join(c for c in phone if c.isdigit())
    if len(digits_only) == 10:
        return "+1" + digits_only
    if len(digits_only) == 11 and digits_only.startswith("1"):
        return "+" + digits_only
    return "+" + digits_only


def validate_twilio_signature(url: str, post_params: dict, signature: str) -> bool:
    """Verify an inbound webhook really came from Twilio.

    The inbound SMS route is public and unauthenticated — without this,
    anyone who knows the URL could forge a STOP (silencing a guest) or a
    YES (granting marketing consent on a guest's behalf, which is exactly
    what guest_marketing.py's consent model exists to prevent).

    Twilio's scheme: HMAC-SHA1 over the full URL followed by each POST
    field sorted by key and concatenated as key+value, keyed by the
    account auth token, base64-encoded.
    """
    import hmac, hashlib, base64
    if not TWILIO_TOKEN or not signature:
        return False
    payload = url + "".join(k + str(post_params[k]) for k in sorted(post_params))
    digest = hmac.new(TWILIO_TOKEN.encode("utf-8"),
                      payload.encode("utf-8"), hashlib.sha1).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature)


def send_sms(to_phone: str, message: str, use_case: str = "alert") -> bool:
    """Send a single SMS via Twilio. Returns True on success.

    Sends via MessagingServiceSid rather than a bare From number — the path
    Twilio's A2P 10DLC campaigns are registered against. A message sent with
    only From can still be carrier-filtered as unregistered traffic even once
    the number is attached to an approved campaign; routing through the
    service is what reliably resolves the number to its campaign.

    use_case picks WHICH service, because a Messaging Service maps to exactly
    one Campaign: "alert" (default) is the owner review/health/labor alert
    campaign on TWILIO_MESSAGING_SERVICE_SID; "otp" is the staff-signup
    verification-code campaign on TWILIO_OTP_MESSAGING_SERVICE_SID, a
    genuinely separate number and service; "guest" is guest marketing
    (campaigns, opt-in invites, review requests) on
    TWILIO_GUEST_MESSAGING_SERVICE_SID. Sending OTP traffic through the
    alert service (or vice versa) is exactly the "mixed use case on one
    campaign" pattern carriers filter hardest.
    """
    if not all([TWILIO_SID, TWILIO_TOKEN, TWILIO_FROM]):
        print(f"[notify] Twilio not configured — would send to {to_phone}: {message[:80]}")
        return False
    phone = _normalize_phone(to_phone)
    data = {"To": phone, "Body": message}
    # No fallback from "otp" to the alert service on a missing OTP SID —
    # that would put verification-code traffic on the wrong campaign, which
    # is worse than the plain-From send this falls back to instead (the
    # behavior every send already had before TWILIO_MESSAGING_SERVICE_SID
    # existed).
    if use_case == "otp":
        service_sid = TWILIO_OTP_MESSAGING_SERVICE_SID
    elif use_case == "guest" and TWILIO_GUEST_MESSAGING_SERVICE_SID:
        service_sid = TWILIO_GUEST_MESSAGING_SERVICE_SID
    else:
        if use_case == "guest":
            global _guest_service_warned
            if not _guest_service_warned:
                _guest_service_warned = True
                print("[notify] TWILIO_GUEST_MESSAGING_SERVICE_SID unset: guest texts are "
                      "going out on the owner-alert messaging service (MOD-MKT-11)")
        service_sid = TWILIO_MESSAGING_SERVICE_SID
    if service_sid:
        data["MessagingServiceSid"] = service_sid
    else:
        data["From"] = TWILIO_FROM
    # One retry for a failure Twilio says is transient (429, 5xx) or a
    # connection that failed before a response (AI-27): a single blip lost an
    # owner's health alert text. NOT for a read timeout — Twilio's Messages
    # API has no idempotency key, and a request it accepted but did not answer
    # in time would be sent twice. A 4xx is permanent and is not retried.
    for attempt in (1, 2):
        try:
            r = requests.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json",
                auth=(TWILIO_SID, TWILIO_TOKEN),
                data=data,
                timeout=10,
            )
            if r.status_code == 201:
                return True
            print(f"[notify] Twilio error {r.status_code}: {r.text[:200]}")
            if not (r.status_code == 429 or r.status_code >= 500):
                return False
        except requests.exceptions.ConnectionError as e:
            print(f"[notify] SMS send failed: {e}")
        except Exception as e:
            print(f"[notify] SMS send failed: {e}")
            return False
        if attempt == 1:
            import time as _time
            _time.sleep(1.0)
    return False


def send_2fa_sms(to_phone: str, restaurant_name: str, code: str) -> bool:
    """The SMS side of 2FA delivery — a client can choose email or text
    when they enable two-factor (see mobile_api.py's
    account/2fa/send-test and .../verify). Plain send_sms under the hood;
    exists mainly so the 6 call sites that send a 2FA code don't each
    hand-roll their own message copy."""
    return send_sms(to_phone, f"Cavnar AI verification code for {restaurant_name}: {code}. Expires in 10 minutes. If you didn't request this, someone may have your password — contact will@cavnar.ai.")


def alert_recipients(owner_email: str, restaurant_id: int = None, db_path: str = DB_PATH) -> list:
    """owner_email plus THIS restaurant's alert_extra_emails (Account ->
    Alerts -> 'Also email').

    restaurant_id is required to add the extra addresses. It used to fall
    back to `WHERE owner_email=? LIMIT 1`, which is a lookup on a column
    that is deliberately NOT unique: every location a multi-location owner
    runs shares one owner_email, so LIMIT 1 with no ORDER BY returned an
    arbitrary location — in practice the oldest. A Dallas 1-star alert was
    CC'd to the Chicago GM's address, complete with the guest's review text,
    and the Dallas GM never got it.

    Without a restaurant_id there is no honest answer, so the owner alone
    gets the mail rather than a guessed location's CC list.
    """
    out = [owner_email] if owner_email else []
    if not restaurant_id:
        return out
    try:
        conn = models.get_conn(db_path)
        row = conn.execute("SELECT alert_extra_emails FROM restaurants WHERE id=?", (restaurant_id,)).fetchone()
        conn.close()
        if row and row["alert_extra_emails"]:
            for e in row["alert_extra_emails"].split(","):
                e = e.strip().lower()
                if "@" in e and e not in out:
                    out.append(e)
    except Exception:
        pass
    return out


def _send_alert_email(owner_email: str, subject: str, html: str, restaurant_id: int = None) -> bool:
    """Send an alert email. Returns True when at least one copy went out.

    Goes through emails.deliver() — the single choke point — rather than
    calling the Resend SDK directly, which is what this did for the life of
    the alert system. Three things were missing as a result, all of them the
    reason that choke point exists: the SUPPRESSION list (a hard-bounced or
    complained address kept receiving alerts indefinitely, which is how a
    sending domain's reputation goes and takes every other email with it),
    RETRY on a transient Resend failure, and an email_log row — so "did my
    1-star alert actually send?" had no answer anywhere in the product.

    One send per recipient rather than one with several To: addresses:
    suppression, the flood guard and the log are all per-recipient, and a
    multi-address payload only ever gets checked against the first.
    """
    if not owner_email:
        return False
    from emails import deliver as _deliver
    sent = 0
    for address in alert_recipients(owner_email, restaurant_id):
        result = _deliver(email_type="alert", restaurant_id=restaurant_id, payload={
            "from": emails_sender("client"),
            "to": [address],
            "subject": subject,
            "html": _html_doc(html),
        })
        if getattr(result, "ok", False):
            sent += 1
    return sent > 0


# Which dashboard tab an alert is actually about. The keys are the web tab
# ids (switchTab / ?tab=), so an alert email's button can land on the screen
# that answers it. Every alert email used to link to the dashboard ROOT,
# which put the owner back at Home to go and find the thing themselves — the
# opposite of what the morning brief's own "Ask about this →" links do.
ALERT_TAB = {
    "health": "reviews", "1star": "reviews", "2star": "reviews", "3star": "reviews",
    "5star": "reviews", "any_review": "reviews", "neg_spike": "reviews",
    "edit_downgrade": "reviews", "resp_approved": "reviews", "unresponded": "reviews",
    "no_response": "reviews", "negative_trend": "reviews", "rating_threshold": "reviews",
    "labor_over": "labor", "schedule_drafted": "labor", "coverage": "labor", "schedule_publish_pending": "labor",
    "food_waste": "inventory", "critical_low": "inventory", "price_spike": "inventory", "order_send_pending": "inventory",
    "order_send_voided": "inventory",
    "ai_visibility_drop": "competitor",
    "login": "account", "staff_signin": "account", "connection_lost": "account",
    "while_away": "reviews",
}

# ── the briefing budget ───────────────────────────────────────────────────────
# Alerts have quiet hours, a per-type toggle, an owner cap and a hard
# ceiling. Briefings — the morning brief, the pre-dinner pulse, the closing
# summary, a drafted schedule, a coverage gap, a demand opportunity — had
# none of it: NON_ALERT_TYPES exempts them from the ceiling, and the
# retention audit counted six a day for a POS-connected owner. One setting,
# three levels, enforced at every briefing send.
BRIEFING_ALWAYS = frozenset({"morning_brief", "outcome_achieved", "milestone", "while_away",
                             "connection_lost", "monthly_review",
                             # A promised supplier order that did not go out:
                             # a delivery that will not come (MOD-FC-10).
                             "order_send_voided"})
BRIEFING_CALM = BRIEFING_ALWAYS | {"closing_summary", "schedule_drafted", "schedule_publish_pending", "order_send_pending"}
BRIEFING_NORMAL_PER_DAY = 4


# ── thresholds from the restaurant's own band ────────────────────────────────
# A 4.0★ floor and a 30% labor target were the defaults for every
# restaurant, so a 4.7★ place never heard about a slide to 4.2 and a 24%
# operation was "under target" all the way to 29%. When the owner has not
# set one, the default is derived from their own last eight weeks; the
# owner's explicit setting always wins.

def baseline_rating_floor(restaurant_id, db_path=DB_PATH):
    try:
        import metrics
        v = metrics.trailing(restaurant_id, "avg_rating", days=56, db_path=db_path)["value"]
        if v is None:
            return None
        return round(min(4.8, max(3.0, float(v) - 0.2)), 1)
    except Exception:
        return None


def baseline_labor_target(restaurant_id, db_path=DB_PATH):
    try:
        import metrics
        v = metrics.trailing(restaurant_id, "labor_pct", days=56, db_path=db_path)["value"]
        if v is None:
            return None
        return round(min(45.0, max(15.0, float(v) + 2.0)), 1)
    except Exception:
        return None


def briefing_allowed(restaurant_id: int, alert_type: str, db_path: str = DB_PATH) -> bool:
    """Whether one more briefing may go to this owner today.

    calm    — the daily brief, results, milestones, the close, a drafted
              schedule. Nothing during service.
    normal  — everything, at most BRIEFING_NORMAL_PER_DAY a day; the
              always-set never counts against it.
    all     — everything, no budget (what shipped before this existed).

    Fails OPEN like the alert ceiling: a bookkeeping error must not silence
    the morning brief.
    """
    if alert_type in BRIEFING_ALWAYS:
        return True
    try:
        from models import get_restaurant, count_briefings_today
        r = get_restaurant(restaurant_id, db_path)
        level = (getattr(r, "briefing_level", None) or "normal").lower()
        if level == "all":
            return True
        if level == "calm":
            return alert_type in BRIEFING_CALM
        n = count_briefings_today(restaurant_id, db_path)
        if n >= BRIEFING_NORMAL_PER_DAY:
            print(f"[notify] rid={restaurant_id} {alert_type} held — briefing budget "
                  f"({n}/{BRIEFING_NORMAL_PER_DAY}) reached")
            return False
        return True
    except Exception as e:
        print(f"[notify] briefing budget check failed for rid={restaurant_id}: {e}")
        return True


def alert_url(alert_type=None, review_id=None) -> str:
    """The dashboard URL that answers this alert."""
    base = config.base_url()
    tab = ALERT_TAB.get(alert_type or "")
    if not tab:
        return base
    url = f"{base}/?tab={tab}"
    if review_id and tab == "reviews":
        url += f"&review={int(review_id)}"
    return url


# The CTA href is stamped in at DELIVERY, not at build: the alert type and
# review id are known there and only there, and an alert held through a rush
# is stored as html in alert_holds and resolved when it is finally released.
CTA_PLACEHOLDER = "https://cavnar.invalid/cta"


def _resolve_cta(html: str, alert_type: str = None, review_id: int = None) -> str:
    return (html or "").replace(CTA_PLACEHOLDER, alert_url(alert_type, review_id))


def _alert_email_html(restaurant_name: str, headline: str, body_lines: list, cta_label: str = "View on dashboard", restaurant_id: int = None, cta_url: str = None) -> str:
    def _safe(s): return _html.escape(str(s)) if s else ""
    # Light, always. This used to read restaurants.email_theme — a column the
    # web dashboard silently POSTs its OWN dark-mode switch into on every page
    # load (/api/theme), so a client who preferred a dark dashboard started
    # getting dark alert emails they never asked for, while every other email
    # Cavnar AI sends stayed a light card. A UI preference is not an email
    # design decision; the column is left alone, it just no longer steers this.
    page_bg, card_bg, card_border = "#f7f4ef", "#ffffff", "rgba(0,0,0,.08)"
    text_primary, text_body, header_sub = "#1a1410", "rgba(0,0,0,.65)", "#7a6f65"
    footer_border, footer_text = "rgba(0,0,0,.08)", "#9a8f85"
    body_html = "".join(f'<p style="font-size:14px;color:{text_body};line-height:1.6;margin:0 0 10px">{l}</p>' for l in body_lines)
    return f"""
<div style="background:{page_bg};padding:24px 0">
<div style="font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;max-width:560px;margin:0 auto;background:{card_bg};border:1px solid {card_border};border-radius:12px;padding:28px;color:{text_primary}">
  <div style="border-top:3px solid #c84b2f;padding-top:20px;margin-bottom:20px">
    <img src="https://dashboard.cavnar.ai/static/brand/wordmark-dark-email.png" width="150" height="26" alt="Cavnar AI" style="display:block;width:150px;height:26px;border:0;outline:none;margin:0 0 6px">
    <p style="font-size:11px;color:{header_sub};margin:0;letter-spacing:1px;text-transform:uppercase">Alert &mdash; {_safe(restaurant_name)}</p>
  </div>
  <h3 style="font-size:16px;font-weight:600;margin:0 0 12px;color:{text_primary}">{_safe(headline)}</h3>
  {body_html}
  <div style="margin-top:20px">
    <a href="{_html.escape(cta_url or CTA_PLACEHOLDER, quote=True)}"
       style="display:inline-block;background:#c84b2f;color:white;padding:11px 22px;
              border-radius:8px;text-decoration:none;font-size:13px;font-weight:600">
      {cta_label} &#8594;
    </a>
  </div>
  <hr style="border:none;border-top:1px solid {footer_border};margin:24px 0"/>
  <p style="font-size:12px;color:{footer_text};margin:0">
    Cavnar AI &middot;
    <a href="https://cavnar.ai" style="color:#c84b2f;text-decoration:none">cavnar.ai</a>
    &middot; Reply to this email or log in to manage alert settings.
  </p>
</div>
</div>"""


def send_test_sms(restaurant_id: int) -> dict:
    """Send a test SMS to all consented contacts for a restaurant."""
    contacts = get_alert_contacts(restaurant_id, sms_consent_only=True)
    if not contacts:
        return {"ok": False, "error": "No alert contacts configured"}
    from models import get_restaurant
    restaurant = get_restaurant(restaurant_id)
    name = restaurant.name if restaurant else f"Restaurant {restaurant_id}"
    msg = f"✓ Test alert from Cavnar AI\n{name} — SMS alert system is active and working."
    sent, errors = 0, []
    for c in contacts:
        ok = send_sms(c["phone"], msg)
        if ok:
            sent += 1
        else:
            errors.append(c["phone"])
    return {"ok": sent > 0, "sent": sent, "errors": errors}


# ── Contact CRUD ───────────────────────────────────────────────

def get_alert_contacts(restaurant_id: int, sms_consent_only: bool = False, db_path: str = DB_PATH) -> list:
    """sms_consent_only=True is the enforcement point for actually sending
    SMS — only contacts whose number's owner personally checked the consent
    box get texted. Management/display UI wants sms_consent_only=False (the
    owner should see and be able to remove any contact, consented or not)."""
    conn = models.get_conn(db_path)
    query = "SELECT id, name, phone, sms_consent FROM alert_contacts WHERE restaurant_id=?"
    if sms_consent_only:
        query += " AND sms_consent=1"
    rows = conn.execute(query + " ORDER BY id", (restaurant_id,)).fetchall()
    conn.close()
    return [{"id": r["id"], "name": r["name"] or "", "phone": r["phone"],
             "sms_consent": bool(r["sms_consent"])} for r in rows]


def add_alert_contact(restaurant_id: int, name: str, phone: str,
                      sms_consent: bool = False, db_path: str = DB_PATH) -> int:
    """sms_consent must only be True when the number's own owner affirmatively
    checked the consent box in their own authenticated session — never set it
    True on someone else's behalf (e.g. an admin adding a contact for a
    client). A contact added without consent still appears in the account's
    contact list and can still receive email, it just never gets SMS."""
    consent_at = None
    if sms_consent:
        from time_utils import restaurant_now_by_id
        consent_at = restaurant_now_by_id(restaurant_id, naive=True).isoformat()
    conn = models.get_conn(db_path)
    cur = conn.execute(
        "INSERT INTO alert_contacts (restaurant_id, name, phone, sms_consent, sms_consent_at) VALUES (?,?,?,?,?)",
        (restaurant_id, name.strip(), phone.strip(), int(sms_consent), consent_at),
    )
    conn.commit()
    contact_id = cur.lastrowid
    conn.close()
    return contact_id


def delete_alert_contact(contact_id: int, db_path: str = DB_PATH):
    conn = models.get_conn(db_path)
    conn.execute("DELETE FROM alert_contacts WHERE id=?", (contact_id,))
    conn.commit()
    conn.close()


# ── Alert helpers ─────────────────────────────────────────────

# A restaurant cannot be sent more than this many alerts in one local day,
# whatever its settings say.
#
# "Max alerts/day" offers Unlimited (stored as 0) and that is a real choice an
# owner can make, so it is respected — but Unlimited was never meant to mean
# "however many a bad day produces". Audit #4 traced one review fetch of a
# Google-Business-connected restaurant to 180 notifications: 20 reviews in a
# single pass, each fanning out to every contact, inbox and device, with the
# only brake switched off by default. This is the backstop under all of it.
# High enough that a genuinely busy day never touches it, low enough that a
# runaway stops being the owner's problem.
ALERT_HARD_CEILING_PER_DAY = 50

# A weekly average is only a trend if the week has enough reviews behind it.
# Shared by the daily negative-trend alert here and the Reviews module's own
# trend surfaces (models.get_topic_heatmap, client_api._do_review_insight),
# so "how much data makes a direction real" is one number, in one place.
MIN_TREND_REVIEWS_PER_WEEK = 3


def _over_alert_ceiling(restaurant_id: int, db_path: str = DB_PATH) -> bool:
    try:
        from models import count_alerts_today
        n = count_alerts_today(restaurant_id, db_path)
        if n >= ALERT_HARD_CEILING_PER_DAY:
            print(f"[notify] rid={restaurant_id} SUPPRESSED — hit the {ALERT_HARD_CEILING_PER_DAY}/day ceiling ({n} sent)")
            if n == ALERT_HARD_CEILING_PER_DAY:
                try:
                    import ops
                    ops.capture(RuntimeError(f"rid={restaurant_id} hit the daily alert ceiling ({n})"),
                                job="alert_ceiling", context=f"restaurant_id={restaurant_id}")
                except Exception:
                    pass
            return True
    except Exception as e:
        # Fail CLOSED is wrong here — a bookkeeping error must not silence a
        # 1-star alert — but say so rather than passing silently.
        print(f"[notify] ceiling check failed for rid={restaurant_id}: {e}")
    return False


def _daily_alert_suppressed(restaurant_id: int, alert_type: str, db_path: str = DB_PATH) -> bool:
    """Quiet hours + the owner's daily cap + the hard ceiling, for the alert
    types that run out of the daily jobs rather than out of blast().

    check_daily_alerts and check_extra_daily_alerts built their own _fire()
    closures and consulted none of the three, so an owner who set "2 per day"
    could still receive a labor alert, a trend alert, a threshold alert, a
    food-waste alert and a visibility alert on top of their two. They run at
    10am so quiet hours rarely bit, but "rarely" is not a design.
    """
    try:
        from models import is_in_quiet_hours, count_alerts_today, get_restaurant
        if is_in_quiet_hours(restaurant_id, db_path):
            print(f"[notify] rid={restaurant_id} {alert_type} suppressed — quiet hours")
            return True
        r = get_restaurant(restaurant_id, db_path)
        cap = int(getattr(r, "alert_max_per_day", 0) or 0)
        if cap > 0 and count_alerts_today(restaurant_id, db_path) >= cap:
            print(f"[notify] rid={restaurant_id} {alert_type} suppressed — daily cap {cap} reached")
            return True
    except Exception as e:
        print(f"[notify] daily-alert DND check failed for rid={restaurant_id}: {e}")
    return _over_alert_ceiling(restaurant_id, db_path)


def _is_health_alert(text: str, urgency: str = None, processed: bool = None) -> bool:
    """Whether a new review warrants the health/safety alert.

    The analyser's own urgency classification decides this when the review
    was actually analysed. It reads the review in context and covers the
    same ground the keyword list does — food safety, illness, injury, legal
    threats, staff misconduct — without matching a bare substring.

    The keyword list is the fallback for a review that could NOT be
    analysed, and only the fallback. Used on its own it fired a 🚨 HEALTH
    ALERT on a five-star review reading "no roach problem here, unlike the
    place down the road": "roach" is in the list and negation is invisible
    to a substring match.

    `processed` is what decides which branch runs, because `urgency` cannot.
    Review.urgency defaults to "normal" (models.py) and the column is
    `DEFAULT 'normal'` — never null, never empty — so the old `if urgency:`
    test was always true and the fallback below was unreachable code. The
    effect was that a review whose analysis failed (API timeout, bad JSON,
    or the per-restaurant AI budget cap) carried urgency='normal' and its
    health alert silently never fired, which is exactly the case the
    fallback was written to cover: the backstop went missing precisely when
    the AI layer was failing.
    """
    if processed is True:
        # Analysed: the analyser read it in context and its answer stands,
        # including when that answer is "normal" (see the negated-mention
        # case above — keywords would fire on that, the analyser does not).
        return str(urgency or "").strip().lower() == "high"
    if processed is False:
        # Explicitly NOT analysed — the only case the keyword list exists
        # for, and the case that was unreachable before.
        pass
    elif urgency:
        # Caller didn't say either way: preserve the old contract rather
        # than risk a keyword false positive on an analysed review.
        return str(urgency).strip().lower() == "high"
    import unicodedata
    t = unicodedata.normalize("NFKC", text or "").lower().strip()
    return any(kw in t for kw in HEALTH_KEYWORDS)


def _sms_safe_excerpt(raw_text: str, limit: int = 60) -> tuple:
    """A short, carrier-filter-safer excerpt of a customer's own review
    text, for the one channel a review's unmoderated wording actually
    matters on: SMS. Email and push keep the full html-escaped quote
    (preview/ellipsis below) — they aren't subject to carrier content
    filtering the way A2P 10DLC SMS is, and a stranger's review can
    contain a URL, a phone number, or characters a filter reads as spam
    signals without Cavnar AI ever moderating it first.

    This narrows exposure, it doesn't eliminate it — there is no keyword
    list here for profanity or the like, deliberately: that needs an
    actual moderation call on every alert's send path, which is a
    different, larger change than trimming what SMS carries. What this
    does: drop anything that looks like a URL or a phone number, keep
    only letters/digits/basic sentence punctuation, collapse whitespace,
    and truncate well short of the 120-char email preview — a shorter
    quote is still enough to say "which review is this."
    """
    import re as _re_sms
    cleaned = raw_text or ""
    cleaned = _re_sms.sub(r"https?://\S+|www\.\S+", "", cleaned)
    cleaned = _re_sms.sub(r"\+?\d[\d\-.\s()]{6,}\d", "", cleaned)  # phone-number-shaped runs
    cleaned = _re_sms.sub(r"[^\w\s.,!?'\-]", "", cleaned, flags=_re_sms.UNICODE)
    cleaned = _re_sms.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > limit:
        return cleaned[:limit].rstrip(), "…"
    return cleaned, ""


_CTA_TOKENS = ("dashboard.cavnar.ai", "cavnar.ai")


def _squash(s: str) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def push_body(sms_text: str, subject: str = None) -> str:
    """The SMS copy, minus the parts that exist only because it is an SMS.

    Push carried sms_text verbatim, so a lock screen read

        🔴 1★ review — Simple EJ's                    <- title
        🔴 1★ Review — Simple EJ's                    <- body, line 1
        Sarah: "waited 45 minutes and the food was cold"
        Respond now · dashboard.cavnar.ai

    — the restaurant named three times and a web address offered to a phone
    that already has the app. A text message needs both because it has no
    title and nowhere to tap; a notification has both.
    """
    lines = [l.strip() for l in (sms_text or "").splitlines() if l.strip()]
    head = _squash(subject)[:16]
    out = []
    for i, line in enumerate(lines):
        for token in _CTA_TOKENS:
            line = line.replace(token, "")
        line = line.strip().strip("·-—").strip()
        if not line:
            continue
        if i == 0 and head and _squash(line)[:16] == head:
            continue
        out.append(line)
    return " ".join(out) or (subject or "")


def _neg_spike_count(restaurant_id: int, db_path: str = DB_PATH) -> int:
    """Negative reviews a guest actually WROTE in the last seven days.

    Was filtered on fetched_at, which is when Cavnar AI pulled the review.
    On a first connect an entire review history arrives in one batch, so a
    restaurant's opening day on the product produced an SMS reading "4
    negative reviews in the last 7 days" about reviews up to three years
    old. Measured before the fix: 4 counted, newest 54 days old.

    Soft-deleted rows are excluded too — a review the owner removed still
    counted toward the spike that texts them about it.
    """
    conn = models.get_conn(db_path)
    count = conn.execute("""
        SELECT COUNT(*) FROM reviews
        WHERE restaurant_id=? AND sentiment='negative'
          AND deleted_at IS NULL
          AND COALESCE(NULLIF(review_date,''), fetched_at) >= datetime('now', '-7 days')
    """, (restaurant_id,)).fetchone()[0]
    conn.close()
    return count


def _neg_spike_window_total(restaurant_id: int, db_path: str = DB_PATH) -> int:
    """Every review a guest wrote in the same seven days _neg_spike_count
    measures negatives over — the denominator the alert needs to say
    anything about a trend.

    Three negatives is a crisis for a restaurant that gets five reviews a
    week and noise for one that gets two hundred. The alert fired on the
    absolute count alone, so both got the identical "trending issue" SMS.
    """
    conn = models.get_conn(db_path)
    count = conn.execute("""
        SELECT COUNT(*) FROM reviews
        WHERE restaurant_id=? AND deleted_at IS NULL
          AND COALESCE(NULLIF(review_date,''), fetched_at) >= datetime('now', '-7 days')
    """, (restaurant_id,)).fetchone()[0]
    conn.close()
    return count


# A spike needs both: enough negatives to be real, and a big enough share of
# the week's reviews to be a signal rather than the ordinary background rate.
NEG_SPIKE_MIN_COUNT = 3
NEG_SPIKE_MIN_SHARE = 0.20


def _already_alerted_spike(restaurant_id: int, db_path: str = DB_PATH) -> bool:
    conn = models.get_conn(db_path)
    row = conn.execute("""
        SELECT id FROM alert_log
        WHERE restaurant_id=? AND alert_type='neg_spike'
        AND fired_at >= datetime('now', '-24 hours')
    """, (restaurant_id,)).fetchone()
    conn.close()
    return row is not None


def _log_alert(restaurant_id: int, alert_type: str, review_id: int = None, db_path: str = DB_PATH,
               value: float = None, priority: int = None):
    """One row in the notification history, which is also what the daily cap
    and the hard ceiling count.

    `value` records the figure the alert fired on, so a later run can ask
    whether the condition actually worsened instead of re-firing on the same
    standing level (see _waste_alert_worsened). `priority` is the executive
    tier (push.PRIORITY) — stored so both notification centers can rank and
    filter without re-deriving it, and so a later change to the map doesn't
    silently rewrite history.
    """
    if priority is None:
        from push import priority_of
        priority = priority_of(alert_type)
    sql = ("INSERT INTO alert_log (restaurant_id, alert_type, review_id, value, priority) "
           "VALUES (?,?,?,?,?)")
    args = (restaurant_id, alert_type, review_id, value, priority)
    conn = models.get_conn(db_path)
    try:
        try:
            conn.execute(sql, args)
        except Exception:
            # init_db owns these columns now; this is the self-healing retry
            # for a database opened before it ran (ai_utils._ensure_usage_schema
            # is the same pattern). No DDL on the happy path.
            conn.execute(sql, args)
        conn.commit()
    finally:
        conn.close()


# How many of one type an owner has to have received, over how long, before
# there is anything to say about whether they read them. Deliberately large:
# this drives a suggestion an owner can act on, and a suggestion made on thin
# evidence is worse than none.
ENGAGEMENT_WINDOW_DAYS = 60
ENGAGEMENT_MIN_DELIVERED = 10

# Never suggested away, whatever the numbers say. An owner who has not opened
# a health alert in sixty days has had a good sixty days.
ENGAGEMENT_NEVER_QUIET = {"health", "coverage", "critical_low", "issue",
                          "issue_escalated", "morning_brief"}

# The per-type push column an owner would switch off, so the suggestion has
# somewhere to land.
ENGAGEMENT_PUSH_COLUMN = {
    "1star": "al_1star_push", "2star": "al_2star_push", "5star": "al_5star_push",
    "neg_spike": "al_spike_push", "no_response": "al_unres_push",
    "unresponded": "al_unres_push", "health": "al_health_push",
}


def engagement_report(restaurant_id: int, db_path: str = DB_PATH) -> list:
    """Notification types this restaurant receives a lot of and never opens.

    [{alert_type, label, delivered, days, push_column}] — the raw material
    for ONE sentence in Account: "34 five-star alerts in the last 60 days,
    none opened. Move them into the morning brief?"

    Deliberately a suggestion and not an automatic downgrade. Reading a
    banner on a lock screen is engagement and leaves no tap behind, so
    switching alerts off on tap data alone would quietly silence alerts an
    owner reads every single day. The owner decides; the product just
    notices and says what it noticed.
    """
    from models import notification_engagement
    out = []
    for row in notification_engagement(restaurant_id, ENGAGEMENT_WINDOW_DAYS, db_path):
        alert_type = row["alert_type"]
        if alert_type in ENGAGEMENT_NEVER_QUIET:
            continue
        if row["opened"] or row["delivered"] < ENGAGEMENT_MIN_DELIVERED:
            continue
        column = ENGAGEMENT_PUSH_COLUMN.get(alert_type)
        if not column:
            continue
        out.append({"alert_type": alert_type, "delivered": row["delivered"],
                    "days": ENGAGEMENT_WINDOW_DAYS, "push_column": column})
    return out


def record_notification(restaurant_id: int, alert_type: str, review_id: int = None,
                        db_path: str = DB_PATH, value: float = None):
    """History row for a notification sent OUTSIDE the alert layer.

    The morning brief, the pre-dinner pulse, the drafted schedule, an issue
    and a coverage gap all push straight through push.fire_push and wrote no
    alert_log row — so the product's single best notifications were the ones
    an owner could never find again. Both notification centers already
    carried labels and routing rules for them, matching nothing.

    These do not count toward the daily cap (models.NON_ALERT_TYPES).
    """
    try:
        _log_alert(restaurant_id, alert_type, review_id, db_path=db_path, value=value)
    except Exception as e:
        print(f"[notify] could not record {alert_type} for rid={restaurant_id}: {e}")


def _waste_alert_worsened(restaurant_id: int, total: float, db_path: str = DB_PATH,
                          min_increase_pct: float = 10.0) -> bool:
    """True when this week's flagged waste is meaningfully worse than the
    figure the last food-waste alert fired on.

    A restaurant chronically over its tolerance bands used to receive the
    identical SMS + email + push every 7 days indefinitely, which trains an
    owner to ignore the channel entirely. Alerting on deterioration keeps the
    signal meaningful; the first alert (no prior figure) always fires.
    """
    conn = models.get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT value FROM alert_log WHERE restaurant_id=? AND alert_type='food_waste' "
            "AND value IS NOT NULL ORDER BY id DESC LIMIT 1",
            (restaurant_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row or row["value"] is None:
        return True
    prior = float(row["value"])
    if prior <= 0:
        return True
    return ((total - prior) / prior * 100.0) >= min_increase_pct


def send_login_alert(restaurant_id: int, restaurant_name: str, owner_email: str,
                      ip: str, user_agent: str, db_path: str = DB_PATH, report_url: str = None):
    """The one 'Sign-in notifications' toggle used to mean email-only —
    audited on device feedback ("does an alert pop up? is it sent to the
    bell? does it show on a locked phone?") and the honest answer for all
    three was no. This gives login the same three-channel shape every
    other alert type here already has (see fire_review_alerts): email
    (unchanged), a push via fire_push so it actually reaches a locked
    phone, and an alert_log row so it shows in the notifications bell.
    All three still gate on the SAME login_notify flag the one toggle
    controls — callers check that (and owner_email) before calling this,
    same as they always have for the email alone."""
    from emails import send_login_notification
    try:
        _tz = getattr(models.get_restaurant(restaurant_id), "timezone", None)
    except Exception:
        _tz = None
    send_login_notification(owner_email, restaurant_name, ip, user_agent, report_url=report_url, tz=_tz)
    try:
        from push import fire_push
        fire_push(
            restaurant_id, "login",
            "New sign-in",
            f"{restaurant_name} — signed in from {ip}",
            data={"alert_type": "login"},
            db_path=db_path,
        )
    except Exception as e:
        print(f"[LoginAlert] push error: {e}")
    try:
        _log_alert(restaurant_id, "login", db_path=db_path)
    except Exception as e:
        print(f"[LoginAlert] alert_log error: {e}")


def send_staff_signin_alert(restaurant_id: int, restaurant_name: str, owner_email: str,
                            employee_name: str, ip: str, db_path: str = DB_PATH):
    """Tell an owner an employee opened the staff portal.

    The console has had sign-in notifications since there was one account to
    notify about. The staff portal added a second, much larger set of
    sign-ins — on shared devices, from a link anyone with a phone can save —
    and none of them were visible to the owner at all. Same three channels
    and the same gate shape as send_login_alert; callers check the toggle.

    Push only, plus the bell: an email per employee per shift would be forty
    emails a day at one restaurant, which is how an alert becomes noise and
    then gets switched off entirely.
    """
    try:
        from push import fire_push
        fire_push(
            restaurant_id, "staff_signin",
            "Staff sign-in",
            f"{employee_name or 'Someone'} opened the staff portal",
            data={"alert_type": "staff_signin"},
            db_path=db_path,
        )
    except Exception as e:
        print(f"[StaffSignIn] push error: {e}")
    try:
        _log_alert(restaurant_id, "staff_signin", db_path=db_path)
    except Exception as e:
        print(f"[StaffSignIn] alert_log error: {e}")


# ── Main alert dispatch ───────────────────────────────────────


# ── Service-hours holding ───────────────────────────────────────────────────
# Reviews are fetched at 8am, noon, 4pm and 8pm (scheduler.py), so two of the
# four slots land inside a rush. Quiet hours do not cover this: they are
# unset for most restaurants (models.is_in_quiet_hours returns False with no
# window configured) and they exist for the night, not for service. A
# two-star review at 12:15 cannot be acted on until the rush is over, and
# buzzing a manager on the floor teaches them to ignore the phone. Held
# alerts go out as soon as the rush ends (release_due_alerts, every tick).
RUSH_WINDOWS = (("11:30", "13:30"), ("17:30", "20:30"))
# The one alert worth interrupting service for.
RUSH_EXEMPT_TYPES = {"health"}


def _hhmm(text):
    h, m = str(text).split(":")
    return int(h), int(m)


def _parse_setting_time(value):
    """'11:00am' / '9:30pm' / '17:30' -> (hour, minute), or None."""
    raw = str(value or "").strip().lower().replace(" ", "")
    if not raw:
        return None
    ampm = None
    for suffix in ("am", "pm"):
        if raw.endswith(suffix):
            ampm, raw = suffix, raw[:-2]
            break
    parts = raw.split(":")
    try:
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError):
        return None
    if ampm == "pm" and hour < 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    return (hour, minute) if 0 <= hour <= 23 and 0 <= minute <= 59 else None


def _open_window(restaurant, weekday_name):
    """((open_h, open_m), (close_h, close_m)) for this weekday, or None when
    the restaurant has not configured hours."""
    import json as _json

    def _load(raw):
        try:
            return _json.loads(raw) if raw else {}
        except Exception:
            return {}

    opens = _parse_setting_time(_load(getattr(restaurant, "open_times_json", None)).get(weekday_name))
    closes = _parse_setting_time(_load(getattr(restaurant, "close_times_json", None)).get(weekday_name))
    return (opens, closes) if (opens or closes) else None


def rush_release_at(restaurant_id, alert_type=None, db_path: str = DB_PATH, now_local=None):
    """When this alert should be delivered instead of now (UTC), or None to
    send immediately. Fails open — a failure here sends now, which is what
    every restaurant had before holding existed."""
    if alert_type in RUSH_EXEMPT_TYPES:
        return None
    try:
        from datetime import time as _time, timezone as _timezone
        from time_utils import restaurant_now, restaurant_tz
        r = models.get_restaurant(restaurant_id, db_path)
        if r is not None and not bool(getattr(r, "alert_hold_during_service", 1)):
            return None
        local = now_local or restaurant_now(r, naive=True)
        window = _open_window(r, local.strftime("%A")) if r is not None else None
        for start, end in RUSH_WINDOWS:
            s_h, s_m = _hhmm(start)
            e_h, e_m = _hhmm(end)
            begins = local.replace(hour=s_h, minute=s_m, second=0, microsecond=0)
            ends = local.replace(hour=e_h, minute=e_m, second=0, microsecond=0)
            if not (begins <= local < ends):
                continue
            if window:
                opens, closes = window
                # Closed through this window: not a rush.
                if opens and local.time() < _time(*opens):
                    continue
                if closes and local.time() >= _time(*closes):
                    continue
            return ends.replace(tzinfo=restaurant_tz(r)).astimezone(_timezone.utc)
        return None
    except Exception as e:
        print(f"[notify] rush check failed for rid={restaurant_id}: {e}")
        return None


def hold_alert(restaurant_id, alert_type, sms_text, subject, html, release_at,
               review_id=None, db_path: str = DB_PATH, value: float = None):
    """Queue an alert for after the rush.

    Deduped against what is already waiting: the alert_log row that normally
    stops a repeat is only written when an alert is DELIVERED, so a
    condition still true at the next fetch (a negative-review spike, say)
    would queue a second copy of itself and both would arrive together.
    """
    conn = models.get_conn(db_path)
    try:
        existing = conn.execute(
            "SELECT 1 FROM alert_holds WHERE restaurant_id=? AND alert_type=? AND sent_at IS NULL "
            "AND COALESCE(review_id, -1)=COALESCE(?, -1)",
            (restaurant_id, alert_type, review_id)).fetchone()
        if existing:
            print(f"[notify] rid={restaurant_id} {alert_type} already waiting — not queued twice")
            return
        conn.execute(
            "INSERT INTO alert_holds (restaurant_id, alert_type, subject, html, sms_text, "
            "review_id, release_at, value) VALUES (?,?,?,?,?,?,?,?)",
            (restaurant_id, alert_type, subject, html, sms_text, review_id,
             release_at.strftime("%Y-%m-%d %H:%M:%S"), value))
        conn.commit()
    finally:
        conn.close()
    print(f"[notify] rid={restaurant_id} {alert_type} held until {release_at:%H:%M} UTC — mid-service")


# Most a single restaurant gets in one release pass. A bad lunch can hold
# several alerts; releasing them all at 13:30 is the burst this feature
# exists to avoid. The rest go out on the following ticks, minutes apart.
MAX_RELEASE_PER_RESTAURANT = 3
# A hold this far past its release is stale — an alert from yesterday's
# lunch arriving tomorrow morning (a scheduler outage) is noise, and its
# content is in the brief by then.
HOLD_MAX_LATE_HOURS = 12


def release_due_alerts(db_path: str = DB_PATH, now_utc=None):
    """Scheduler entry point: send everything whose rush has ended. A hold is
    marked sent whether or not delivery worked, so a failing channel can't
    replay the same alert every five minutes."""
    from datetime import datetime as _dt, timedelta as _td, timezone as _timezone
    now_utc = now_utc or _dt.now(_timezone.utc)
    now_s = now_utc.strftime("%Y-%m-%d %H:%M:%S")
    stale_before = (now_utc - _td(hours=HOLD_MAX_LATE_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    conn = models.get_conn(db_path)
    try:
        # Holds too late to be worth sending are retired in one statement.
        dropped = conn.execute("UPDATE alert_holds SET sent_at=datetime('now') WHERE sent_at IS NULL "
                               "AND release_at < ?", (stale_before,)).rowcount
        conn.commit()
        # A fair slice per restaurant: the first 200 by id used to be one
        # restaurant's backlog, so every other restaurant's held alerts waited
        # behind it indefinitely (MOD-NOT-2).
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM (SELECT h.*, ROW_NUMBER() OVER (PARTITION BY restaurant_id ORDER BY id) AS rn "
            "FROM alert_holds h WHERE sent_at IS NULL AND release_at <= ?) WHERE rn <= ? ORDER BY id LIMIT 500",
            (now_s, MAX_RELEASE_PER_RESTAURANT)).fetchall()]
    finally:
        conn.close()
    if dropped:
        print(f"[notify] {dropped} hold(s) dropped — {HOLD_MAX_LATE_HOURS}h past their release")
    sent = 0
    for h in rows:
        rid = h["restaurant_id"]
        # Claim before delivering: marking it sent AFTER delivery meant a
        # failing write re-delivered the same alert every five minutes, and
        # two runners both delivered it (DATA-5, DATA-4). At most once.
        if not _claim_hold(h["id"], db_path):
            continue
        # A held alert is still an alert: the owner's daily cap and the hard
        # ceiling apply when it is released, or 60 one-stars held through a
        # rush all arrived at once (MOD-NOT-1).
        if _release_suppressed(rid, db_path):
            continue
        try:
            deliver_alert(h["restaurant_id"], h["alert_type"], h["sms_text"], h["subject"],
                          h["html"], review_id=h["review_id"], db_path=db_path,
                          value=h.get("value"))
            sent += 1
        except Exception as e:
            print(f"[notify] held alert {h['id']} failed: {e}")
            try:
                import ops
                ops.capture(e, job="release_held_alerts", context=f"hold_id={h['id']}")
            except Exception:
                pass
    return {"released": sent, "dropped_stale": dropped}


def _claim_hold(hold_id, db_path: str = DB_PATH) -> bool:
    try:
        conn = models.get_conn(db_path)
        try:
            got = conn.execute("UPDATE alert_holds SET sent_at=datetime('now') WHERE id=? AND sent_at IS NULL",
                               (hold_id,)).rowcount
            conn.commit()
            return got == 1
        finally:
            conn.close()
    except Exception as e:
        print(f"[notify] could not claim hold {hold_id}: {e}")
        return False


def _release_suppressed(restaurant_id, db_path: str = DB_PATH) -> bool:
    try:
        from models import count_alerts_today, get_restaurant
        r = get_restaurant(restaurant_id, db_path)
        cap = int(getattr(r, "alert_max_per_day", 0) or 0)
        if cap > 0 and count_alerts_today(restaurant_id, db_path) >= cap:
            return True
    except Exception as e:
        print(f"[notify] cap check failed for rid={restaurant_id}: {e}")
    return _over_alert_ceiling(restaurant_id, db_path)


def _mark_sent(hold_id, db_path: str = DB_PATH):
    conn = models.get_conn(db_path)
    try:
        conn.execute("UPDATE alert_holds SET sent_at=datetime('now') WHERE id=?", (hold_id,))
        conn.commit()
    finally:
        conn.close()



# Alert types the morning brief's own lines already cover. When the brief
# reached the owner's phone this morning, these skip EMAIL only — the owner
# still gets the push and any SMS they turned on, and their inbox gets one
# Cavnar email in the morning instead of three.
BRIEF_COVERED_TYPES = {"labor_over", "food_waste", "negative_trend",
                       "rating_threshold", "ai_visibility_drop", "unresponded"}


def brief_pushed_today(restaurant_id, db_path: str = DB_PATH):
    """True when this restaurant's morning brief was delivered by push today
    (its own local date) — the owner has the app and has already read today's
    numbers there."""
    try:
        from time_utils import restaurant_now_by_id
        day = restaurant_now_by_id(restaurant_id, naive=True).date().isoformat()
        conn = models.get_conn(db_path)
        try:
            row = conn.execute(
                "SELECT 1 FROM push_deliveries WHERE restaurant_id=? AND alert_type='morning_brief' "
                "AND ok=1 AND date(created_at) >= ? LIMIT 1", (restaurant_id, day)).fetchone()
        finally:
            conn.close()
        return bool(row)
    except Exception:
        return False


def _email_alert(restaurant_id, owner_email, subject, html, alert_type, db_path: str = DB_PATH,
                 review_id: int = None):
    """Send an alert email unless the morning brief already covered it on the
    owner's phone today. Returns True when it sent.

    Also the one place the CTA button's href is resolved — see
    CTA_PLACEHOLDER. Every email path reaches this function, including a
    held alert released hours later, so there is no way to send one that
    still points at the dashboard root."""
    if alert_type in BRIEF_COVERED_TYPES and brief_pushed_today(restaurant_id, db_path):
        print(f"[notify] rid={restaurant_id} {alert_type} email folded into today's brief")
        return False
    resolved = _resolve_cta(html, alert_type, review_id)
    return _send_alert_email(owner_email, subject, resolved, restaurant_id=restaurant_id)


def deliver_alert(restaurant_id: int, alert_type: str, sms_text: str, subject: str,
                  html: str, review_id: int = None, db_path: str = DB_PATH,
                  value: float = None):
    """Send one alert on whichever channels this restaurant has on for that
    type, log it, and fire the webhook. The single delivery path: an alert
    raised now goes straight here, and one held through a rush comes here
    when the rush ends (release_due_alerts).

    The daily operational alerts (labor, waste, stock, price, visibility,
    trend, threshold) reach this too, through raise_alert() below. They used
    to run two bespoke _fire() closures instead, which is why they were the
    only alerts with no rush holding, no resolved CTA link and no priority.

    Deliberately does NOT re-check quiet hours or the daily cap. Those are
    checked when the alert is RAISED; re-checking at release would drop a
    held alert whose release happens to land in a window it was never
    subject to.
    """
    conn = models.get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM restaurants WHERE id=?", (restaurant_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return
    global_sms = bool(row["urgent_via_sms"])
    global_email = bool(row["urgent_via_email"])
    contacts = get_alert_contacts(restaurant_id, sms_consent_only=True, db_path=db_path) if global_sms else []
    owner_email = row["owner_email"] or ""

    def _col(name, default=1):
        try:
            v = row[name]
            return 1 if v is None else int(v)
        except Exception:
            return default

        return
    # Per-type channel flags (fall back to 1 so old data keeps working)
    type_map = {
        "health":    ("al_health_sms",  "al_health_email",  "al_health_push"),
        "1star":     ("al_1star_sms",   "al_1star_email",   "al_1star_push"),
        "2star":     ("al_2star_sms",   "al_2star_email",   "al_2star_push"),
        "3star":     ("al_3star_sms",   "al_3star_email",   "al_3star_push"),
        # "any review" and the approval confirmation are low-stakes
        # informational types — they ride the 1-star channel matrix
        # rather than adding two more toggle triplets to the settings
        # screen for something the owner opted into by name already.
        "any_review":   ("al_1star_sms", "al_1star_email", "al_1star_push"),
        "resp_approved":("al_1star_sms", "al_1star_email", "al_1star_push"),
        "edit_downgrade":("al_1star_sms", "al_1star_email", "al_1star_push"),
        "5star":     ("al_5star_sms",   "al_5star_email",   "al_5star_push"),
        "neg_spike": ("al_spike_sms",   "al_spike_email",   "al_spike_push"),
        "unresponded":("al_unres_sms",  "al_unres_email",   "al_unres_push"),
        # check_no_response_alerts pushes and logs "no_response" while
        # _email_alert is handed "unresponded" — the same alert under two
        # names. Both map here so neither path can fall through to the
        # health default below.
        "no_response":("al_unres_sms",  "al_unres_email",   "al_unres_push"),
        # The daily operational alerts have no per-type channel columns —
        # the owner opted in by alert_negative_trend / alert_labor_over /
        # alert_food_waste / alert_ai_visibility_drop, and the channel
        # question is answered by the global SMS and email switches alone.
        # None means "no per-type gate", not "off".
        "negative_trend":     (None, None, None),
        "rating_threshold":   (None, None, None),
        "labor_over":         (None, None, None),
        "food_waste":         (None, None, None),
        "critical_low":       (None, None, None),
        "price_spike":        (None, None, None),
        "ai_visibility_drop": (None, None, None),
        # The combined morning batch folds the daily operational alerts into
        # one message. It fell through to the unresponded triplet below, so
        # an owner with "no reply after 48h" reminders off heard about none
        # of the morning's labor or rating news (MOD-NOT-3). The items inside
        # were each opted into already.
        "daily_briefing":     (None, None, None),
    }
    # An unmapped type used to land on the HEALTH triplet — the most
    # permissive channel set in the system, and the one the quiet-hours
    # bypass is attached to. A type nobody has classified should not
    # inherit the loudest settings an owner has; the unresponded triplet is
    # the ordinary-alert default.
    sms_col, email_col, push_col = type_map.get(
        alert_type, ("al_unres_sms", "al_unres_email", "al_unres_push")
    )
    def _on(column, default):
        return True if column is None else bool(_col(column, default))

    via_sms   = global_sms   and _on(sms_col,   0)
    via_email = global_email and _on(email_col, 1)
    # Push has no global on/off switch the way SMS/email do (urgent_via_sms/
    # urgent_via_email exist because those channels cost money per message;
    # push doesn't, so the per-type toggle alone is the gate) — and it's a
    # no-op anyway if the owner never registered a device.
    via_push  = _on(push_col, 1)
    if via_sms and contacts:
        for c in contacts:
            send_sms(c["phone"], sms_text)
    if via_email and owner_email:
        _email_alert(restaurant_id, owner_email, subject, html, alert_type, db_path,
                     review_id=review_id)
    # Logged BEFORE the push, not after: the push payload now carries the
    # app-icon badge, which is this login's unread count over alert_log. A
    # push sent first badges the phone with a number that excludes the very
    # alert it is announcing.
    #
    # db_path from the enclosing fire_review_alerts() call — this used
    # to fall back to _log_alert's own stale default, silently logging (or,
    # on a machine/CI runner with no local reviews.db, crashing) against
    # the wrong database regardless of what db_path the caller actually
    # passed in.
    _log_alert(restaurant_id, alert_type, review_id, db_path=db_path, value=value)
    if via_push:
        try:
            from push import fire_push as _fp
            _fp(restaurant_id, alert_type, subject, push_body(sms_text, subject),
                data={"alert_type": alert_type, "review_id": review_id}, db_path=db_path)
        except Exception:
            pass
    try:
        from webhooks import fire_webhook as _fw
        _fw(restaurant_id, "alert.fired", {"alert_type": alert_type, "review_id": review_id}, db_path)
        if alert_type == "labor_over":
            _fw(restaurant_id, "labor.over_target", {"alert_type": alert_type}, db_path)
    except Exception:
        pass


# ── The morning batch ───────────────────────────────────────────────────────
#
# At 10am local, run_daily_alert_checks runs three functions in sequence that
# between them can raise eight alert types. Each used to send its own SMS, its
# own email and its own push. A single week of understaffing — labor over
# target, waste up, an ingredient climbing, the rating slipping, two reviews
# unanswered — arrived as five notifications describing one story.
#
# An advisor sends one. These types are collected across the whole pass and
# delivered together, worst first; each still writes its own alert_log row, so
# the 7-day repeat windows and the history are unchanged.
DAILY_BATCH_TYPES = {
    "negative_trend", "rating_threshold", "labor_over", "food_waste",
    "critical_low", "price_spike", "ai_visibility_drop", "no_response",
}

_batch = None


class _Pending:
    __slots__ = ("alert_type", "sms_text", "subject", "lines", "value")

    def __init__(self, alert_type, sms_text, subject, lines, value):
        self.alert_type, self.sms_text = alert_type, sms_text
        self.subject, self.lines, self.value = subject, lines or [], value


# The once-a-day claims a batched pass has EARNED, written only when the batch
# is flushed. _gated_out used to claim each restaurant's day as it collected,
# so a deploy between collecting and flushing lost that day's alerts with the
# claims already spent (MOD-NOT-16); now the next hourly pass re-collects.
_batch_claims = []


def begin_daily_batch():
    """Start collecting. Idempotent, and safe to call when one is already
    open (the scheduler runs one pass at a time behind the lease)."""
    global _batch, _batch_claims
    _batch = {}
    _batch_claims = []


def flush_daily_batch(db_path: str = DB_PATH):
    """Send what was collected: one notification per restaurant when more
    than one thing fired, otherwise exactly what would have gone before."""
    global _batch, _batch_claims
    pending, _batch = (_batch or {}), None
    claims, _batch_claims = _batch_claims, []
    out = {"restaurants": 0, "combined": 0, "single": 0}
    for rid, items in pending.items():
        if not items:
            continue
        out["restaurants"] += 1
        try:
            if len(items) == 1:
                out["single"] += 1
                _deliver_pending(rid, items[0], db_path)
            else:
                out["combined"] += 1
                _deliver_combined(rid, items, db_path)
        except Exception as e:
            print(f"[notify] daily batch failed for rid={rid}: {e}")
            try:
                import ops
                ops.capture(e, job="daily_batch", context=f"restaurant_id={rid}", db_path=db_path)
            except Exception:
                pass
    if claims:
        import ops
        for job, period in claims:
            ops.claim_period(job, period)
    return out


def _deliver_pending(restaurant_id, item, db_path):
    html = _alert_email_html(_restaurant_name(restaurant_id), item.subject, item.lines,
                             restaurant_id=restaurant_id)
    _deliver_or_hold(restaurant_id, item.alert_type, item.sms_text, item.subject, html,
                     db_path=db_path, value=item.value)


def _restaurant_name(restaurant_id):
    try:
        r = models.get_restaurant(restaurant_id)
        return (r.location_name or r.name) if r else "your restaurant"
    except Exception:
        return "your restaurant"


def _deliver_combined(restaurant_id, items, db_path):
    """One notification for the whole morning, worst first."""
    from push import priority_of
    items.sort(key=lambda i: (priority_of(i.alert_type), i.alert_type))
    name = _restaurant_name(restaurant_id)
    lead = items[0]
    n = len(items)
    subject = f"{n} things to look at this morning — {name}"
    # The lead item leads: an owner reading only the banner should still come
    # away with the most expensive thing on the list.
    sms_text = (f"Cavnar AI · {name}: {n} things this morning. "
                f"First: {(lead.lines[0] if lead.lines else lead.subject)}")
    lines = []
    for item in items:
        headline = _html.escape(item.subject)
        detail = item.lines[0] if item.lines else ""
        lines.append(f"<strong>{headline}</strong>" + (f"<br>{detail}" if detail else ""))
    lines.append("Everything above is from your own data over the last week. "
                 "Open Cavnar AI and ask about any of it.")
    html = _alert_email_html(name, f"Your morning, in one place", lines,
                             cta_label="Open the dashboard", restaurant_id=restaurant_id)
    _deliver_or_hold(restaurant_id, "daily_briefing", _strip_tags(sms_text), subject, html,
                     db_path=db_path)
    # Each folded type still records itself, so next week's repeat windows
    # (_already_alerted / _recent) and the history behave exactly as before.
    for item in items:
        _log_alert(restaurant_id, item.alert_type, db_path=db_path, value=item.value)


def _strip_tags(text: str) -> str:
    import re as _re
    return _html.unescape(_re.sub(r"<[^>]+>", "", text or "")).strip()


def _deliver_or_hold(restaurant_id, alert_type, sms_text, subject, html,
                     db_path=DB_PATH, value=None, review_id=None):
    release_at = rush_release_at(restaurant_id, alert_type, db_path)
    if release_at is not None:
        hold_alert(restaurant_id, alert_type, sms_text, subject, html, release_at,
                   review_id=review_id, db_path=db_path, value=value)
        return
    deliver_alert(restaurant_id, alert_type, sms_text, subject, html,
                  review_id=review_id, db_path=db_path, value=value)


def raise_alert(restaurant_id: int, alert_type: str, sms_text: str, subject: str,
                html: str = None, review_id: int = None, db_path: str = DB_PATH,
                value: float = None, lines: list = None) -> bool:
    """Raise one alert: quiet hours / daily cap / hard ceiling, then either
    collect it into this morning's batch, hold it through a rush, or deliver
    it now. Returns False when it was suppressed outright.

    blast() inside fire_review_alerts() is the review-side twin of this; the
    daily jobs call this one. Both end at deliver_alert."""
    if _daily_alert_suppressed(restaurant_id, alert_type, db_path):
        return False
    if _batch is not None and alert_type in DAILY_BATCH_TYPES:
        _batch.setdefault(restaurant_id, []).append(
            _Pending(alert_type, sms_text, subject, lines, value))
        return True
    if html is None:
        html = _alert_email_html(_restaurant_name(restaurant_id), subject, lines or [],
                                 restaurant_id=restaurant_id)
    _deliver_or_hold(restaurant_id, alert_type, sms_text, subject, html,
                     db_path=db_path, value=value, review_id=review_id)
    return True


# A review older than this is history, not news (MOD-REV-6). The first Google
# connect of an established restaurant inserts years of reviews in one pass;
# every one of them reached the alert loop, so 300 reviews from 2019 were 300
# "respond now" alerts — capped at the 50/day ceiling, which then suppressed
# the genuine alerts that day. A week covers Google's own delay in surfacing a
# review and a restaurant whose fetch was down for a few days.
REVIEW_NEWS_MAX_AGE_DAYS = int(os.getenv("REVIEW_NEWS_MAX_AGE_DAYS", "7"))


def is_recent_review(review, max_age_days: int = None) -> bool:
    """Whether a newly stored review was WRITTEN recently enough to announce
    (alerts, review.received webhooks). No usable date reads as recent: the
    row is new to us, and staying silent about a real review is worse."""
    from datetime import datetime as _dt, timedelta as _td
    days = REVIEW_NEWS_MAX_AGE_DAYS if max_age_days is None else max_age_days
    stamp = (getattr(review, "review_date", None) or "")[:19]
    if not stamp:
        return True
    try:
        written = _dt.fromisoformat(stamp.replace("Z", ""))
    except ValueError:
        return True
    return written >= _dt.now() - _td(days=days, hours=14)   # hours: any zone's "today"


def fire_review_alerts(restaurant_id: int, restaurant_name: str, new_reviews: list,
                       db_path: str = DB_PATH, edited_reviews: list = None):
    """
    Check newly saved reviews against per-restaurant alert toggles.
    Fires SMS (if urgent_via_sms) and/or email (if urgent_via_email).
    Call this after save_reviews() with the list of newly inserted Review objects.

    `edited_reviews` carries reviews whose author lowered their own rating
    (save_reviews' `downgrades` out-parameter). They get their own alert
    type at the end, through the same blast() gating as everything else —
    an edit is not a new row, so before this it produced no alert at all
    and a five-star quietly becoming a one-star was invisible.

    A newly inserted review written more than REVIEW_NEWS_MAX_AGE_DAYS ago is
    a history import, stored silently (MOD-REV-6). An edit is always news:
    the guest changed it now, whenever they first wrote it.
    """
    new_reviews = [r for r in (new_reviews or []) if is_recent_review(r)]
    if not new_reviews and not edited_reviews:
        return

    conn = models.get_conn(db_path)
    row = conn.execute("""
        SELECT alert_1star, alert_2star, alert_3star, alert_health, alert_5star,
               alert_neg_spike, alert_negative_trend, alert_no_response,
               alert_any_review,
               urgent_via_sms, urgent_via_email, owner_email,
               alert_max_per_day,
               al_health_email, al_health_sms, al_health_push,
               al_1star_email,  al_1star_sms,  al_1star_push,
               al_2star_email,  al_2star_sms,  al_2star_push,
               al_3star_email,  al_3star_sms,  al_3star_push,
               al_5star_email,  al_5star_sms,  al_5star_push,
               al_spike_email,  al_spike_sms,  al_spike_push,
               al_unres_email,  al_unres_sms,  al_unres_push,
               billing_status
        FROM restaurants WHERE id=?
    """, (restaurant_id,)).fetchone()
    conn.close()

    if not row:
        return
    # A cancelled customer is not texted, emailed or pushed about reviews
    # (MOD-REV-2, merged MOD-BIL-8) — this never read billing_status.
    if not models.in_service(row):
        return

    # Global SMS/email on switches (must be on for any SMS/email to fire).
    # Push has no equivalent global switch (see blast() below) — so this
    # early-return only short-circuits the whole function when EVERY channel,
    # push included, is off for this restaurant; otherwise a restaurant that
    # disabled both global SMS and email but still wants push would never
    # even reach blast() to get it.
    global_sms   = bool(row["urgent_via_sms"])
    global_email = bool(row["urgent_via_email"])
    _push_cols = ("al_health_push", "al_1star_push", "al_2star_push",
                  "al_3star_push", "al_5star_push", "al_spike_push", "al_unres_push")
    any_push = any(row[c] for c in _push_cols if c in row.keys())
    if not global_sms and not global_email and not any_push:
        return

    contacts    = get_alert_contacts(restaurant_id, sms_consent_only=True, db_path=db_path) if global_sms else []
    owner_email = row["owner_email"] or ""
    if global_email and not owner_email:
        print(f"[notify] rid={restaurant_id} has email alerts on but no owner_email — email suppressed")

    def _col(name, default=1):
        try:
            v = row[name]
            return 1 if v is None else int(v)
        except Exception:
            return default

    # Load DND / daily cap settings
    _dnd_row = conn.execute(
        "SELECT alert_quiet_start, alert_quiet_end, alert_max_per_day FROM restaurants WHERE id=?",
        (restaurant_id,)
    ).fetchone() if False else None  # loaded lazily below

    def _check_dnd(alert_type: str = None):
        try:
            from models import is_in_quiet_hours, count_alerts_today
            if is_in_quiet_hours(restaurant_id, db_path):
                # Health/safety mentions can opt out of quiet hours (Account
                # -> Alerts -> "Health alerts ignore quiet hours") — the one
                # alert an owner would rather be woken for.
                if alert_type == "health" and health_bypasses_quiet_hours(restaurant_id, db_path):
                    print(f"[notify] rid={restaurant_id} quiet hours active — health alert allowed through")
                else:
                    print(f"[notify] rid={restaurant_id} suppressed — quiet hours active")
                    return True
            cap = row["alert_max_per_day"] if "alert_max_per_day" in row.keys() else 0
            if cap and cap > 0 and count_alerts_today(restaurant_id, db_path) >= cap:
                print(f"[notify] rid={restaurant_id} suppressed — daily cap {cap} reached")
                return True
        except Exception as _de:
            print(f"[notify] DND check error: {_de}")
        # Outside the try above so a failure in the owner's own settings can
        # never skip the ceiling — that is the one check that has to hold.
        if _over_alert_ceiling(restaurant_id, db_path):
            return True
        return False

    def blast(sms_text: str, subject: str, html: str, alert_type: str, review_id: int = None):
        if _check_dnd(alert_type):
            return
        # Mid-service? Hold it until the rush ends instead of buzzing a
        # manager on the floor. The DND and cap checks above have already
        # run, so a held alert is one that WOULD have gone out.
        release_at = rush_release_at(restaurant_id, alert_type, db_path)
        if release_at is not None:
            hold_alert(restaurant_id, alert_type, sms_text, subject, html, release_at,
                       review_id=review_id, db_path=db_path)
            return
        deliver_alert(restaurant_id, alert_type, sms_text, subject, html,
                      review_id=review_id, db_path=db_path)

    for review in new_reviews:
        rating   = review.rating or 0
        text     = review.text or ""
        # .split()[0] on an empty author raised IndexError and took down
        # the alert loop for every remaining review in the batch.
        _author_parts = (review.author or "").split()
        author   = _html.escape(_author_parts[0]) if _author_parts else ""
        platform = _html.escape((review.platform or "Google").title())
        preview  = _html.escape(text[:120].strip())
        ellipsis = "…" if len(text) > 120 else ""
        # SMS gets its own, shorter, filter-safer excerpt — see
        # _sms_safe_excerpt's docstring. Email/push keep preview/ellipsis
        # above, unchanged.
        sms_preview, sms_ellipsis = _sms_safe_excerpt(text)

        # Health alert — highest priority
        if row["alert_health"] and _is_health_alert(text, getattr(review, "urgency", None),
                                                    processed=bool(getattr(review, "processed", False))):
            sms = (
                f"🚨 HEALTH ALERT — {restaurant_name}\n"
                f"{rating}★ {platform}: \"{sms_preview}{sms_ellipsis}\"\n"
                f"Requires immediate response · dashboard.cavnar.ai"
            )
            html = _alert_email_html(
                restaurant_name,
                f"🚨 Health/safety mention in a new review",
                [
                    f"A <strong>{rating}★ review</strong> on {platform} contains a health or safety mention.",
                    f'<em>"{preview}{ellipsis}"</em>',
                    "This requires an immediate response.",
                ],
                cta_label="Respond now",
                restaurant_id=restaurant_id,
            )
            blast(sms, f"Health or safety mention — {restaurant_name}", html, "health", review.id)
            continue

        # 1★ alert
        if rating == 1 and row["alert_1star"]:
            who  = f"{author}: " if author else ""
            sms  = (
                f"🔴 1★ Review — {restaurant_name}\n"
                f"{who}\"{sms_preview}{sms_ellipsis}\"\n"
                f"Respond now · dashboard.cavnar.ai"
            )
            html = _alert_email_html(
                restaurant_name,
                f"🔴 1★ review received on {platform}",
                [
                    f'<strong>{author}</strong> left a 1-star review on {platform}:' if author else f"A 1-star review was posted on {platform}:",
                    f'<em>"{preview}{ellipsis}"</em>',
                ],
                cta_label="Respond now",
                restaurant_id=restaurant_id,
            )
            blast(sms, f"1-star review — {restaurant_name}", html, "1star", review.id)

        # 5★ alert
        elif rating == 5 and row["alert_5star"]:
            who  = f"{author}" if author else "A guest"
            sms  = (
                f"⭐ 5★ Review — {restaurant_name}\n"
                f"{who} on {platform}: \"{sms_preview}{sms_ellipsis}\"\n"
                f"dashboard.cavnar.ai"
            )
            html = _alert_email_html(
                restaurant_name,
                f"⭐ 5★ review on {platform}",
                [
                    f'<strong>{author}</strong> left a 5-star review on {platform}:' if author else f"A 5-star review was posted on {platform}:",
                    f'<em>"{preview}{ellipsis}"</em>',
                    "Consider thanking them — a response to a great review builds loyalty.",
                ],
                cta_label="View & respond",
                restaurant_id=restaurant_id,
            )
            blast(sms, f"5-star review — {restaurant_name}", html, "5star", review.id)

        # 3★ alert — the one rating with no path at all before. A 3-star
        # "waited 45 minutes and the food was cold" is the review an owner
        # most often never hears about and most easily could have saved.
        elif rating == 3 and _col("alert_3star", 0):
            who  = f"{author}: " if author else ""
            sms  = (
                f"🟡 3★ Review — {restaurant_name}\n"
                f"{who}\"{sms_preview}{sms_ellipsis}\"\n"
                f"dashboard.cavnar.ai"
            )
            html = _alert_email_html(
                restaurant_name,
                f"🟡 3★ review received on {platform}",
                [
                    f'<strong>{author}</strong> left a 3-star review on {platform}:' if author else f"A 3-star review was posted on {platform}:",
                    f'<em>"{preview}{ellipsis}"</em>',
                    "Middling reviews usually name something specific and fixable.",
                ],
                restaurant_id=restaurant_id,
            )
            blast(sms, f"3-star review — {restaurant_name}", html, "3star", review.id)

        # 2★ alert
        elif rating == 2 and row["alert_2star"]:
            who  = f"{author}: " if author else ""
            sms  = (
                f"🟠 2★ Review — {restaurant_name}\n"
                f"{who}\"{sms_preview}{sms_ellipsis}\"\n"
                f"dashboard.cavnar.ai"
            )
            html = _alert_email_html(
                restaurant_name,
                f"🟠 2★ review received on {platform}",
                [
                    f'<strong>{author}</strong> left a 2-star review on {platform}:' if author else f"A 2-star review was posted on {platform}:",
                    f'<em>"{preview}{ellipsis}"</em>',
                ],
                restaurant_id=restaurant_id,
            )
            blast(sms, f"2-star review — {restaurant_name}", html, "2star", review.id)

        # Any review — the catch-all toggle. Settable in Account → Alerts
        # (client_api.py) since it shipped, and read by nothing until now,
        # so an owner who asked to hear about every review heard about none
        # outside the rating-specific types above.
        elif _col("alert_any_review", 0):
            who  = f"{author}: " if author else ""
            sms  = (
                f"💬 {rating}★ Review — {restaurant_name}\n"
                f"{who}\"{sms_preview}{sms_ellipsis}\"\n"
                f"dashboard.cavnar.ai"
            )
            html = _alert_email_html(
                restaurant_name,
                f"💬 New {rating}★ review on {platform}",
                [
                    f'<strong>{author}</strong> left a {rating}-star review on {platform}:' if author else f"A {rating}-star review was posted on {platform}:",
                    f'<em>"{preview}{ellipsis}"</em>',
                ],
                restaurant_id=restaurant_id,
            )
            blast(sms, f"New review — {restaurant_name}", html, "any_review", review.id)

    # A guest lowered their own rating — every bit as much a reputation
    # event as a new bad review, and previously silent.
    for review in (edited_reviews or []):
        was = getattr(review, "previous_rating", None)
        now = review.rating or 0
        moved = f"{was}★ → {now}★" if was else f"now {now}★"
        _p, _e = _sms_safe_excerpt(review.text or "")
        _author_parts_e = (review.author or "").split()
        _author_e = _html.escape(_author_parts_e[0]) if _author_parts_e else ""
        who = f"{_author_e} " if _author_e else ""
        sms = (
            f"📉 Review edited down — {restaurant_name}\n"
            f"{who}changed their review: {moved}\n"
            f"\"{_p}{_e}\" · dashboard.cavnar.ai"
        )
        html = _alert_email_html(
            restaurant_name,
            "📉 A guest lowered their review",
            [
                f"<strong>{_author_e or 'A guest'}</strong> edited their review: <strong>{moved}</strong>.",
                f'<em>"{_html.escape((review.text or "")[:120].strip())}"</em>',
                "Your existing reply, if any, is still published against it.",
            ],
            cta_label="Open the review",
            restaurant_id=restaurant_id,
        )
        blast(sms, f"A guest lowered their review — {restaurant_name}", html, "edit_downgrade", review.id)

    # Negative spike — once per batch, 24h dedup
    if row["alert_neg_spike"] and not _already_alerted_spike(restaurant_id, db_path):
        count = _neg_spike_count(restaurant_id, db_path)
        total = _neg_spike_window_total(restaurant_id, db_path)
        share = (count / total) if total else 0
        if count >= NEG_SPIKE_MIN_COUNT and share >= NEG_SPIKE_MIN_SHARE:
            pct = round(share * 100)
            sms  = (
                f"⚠️ {restaurant_name}: {count} of {total} reviews in the last 7 days "
                f"were negative ({pct}%).\n"
                f"Trending issue — check your dashboard · dashboard.cavnar.ai"
            )
            html = _alert_email_html(
                restaurant_name,
                f"⚠️ Negative review spike detected",
                [
                    f"<strong>{count} of {total} reviews</strong> in the last 7 days were negative "
                    f"(<strong>{pct}%</strong>).",
                    "This may indicate a recurring issue worth investigating.",
                ],
                restaurant_id=restaurant_id,
            )
            blast(sms, f"Negative review spike — {restaurant_name}", html, "neg_spike")


def fire_response_approved_alert(restaurant_id: int, review_id: int,
                                 posted: bool = False, db_path: str = DB_PATH):
    """Confirmation that a reply was approved (and, when Google is
    connected, published).

    `alert_resp_approved` has been a settable toggle in Account -> Alerts
    since it shipped and was read by no alert code at all, so an owner who
    asked to be told when a reply went out was told nothing. Email + push
    only, deliberately: a confirmation of something the owner just did does
    not warrant a text message, and SMS costs money per send.

    Gated through _daily_alert_suppressed like every other non-blast()
    alert type, so quiet hours, the owner's daily cap and the hard ceiling
    all still apply.
    """
    try:
        conn = models.get_conn(db_path)
        r = conn.execute(
            "SELECT name, owner_email, alert_resp_approved, al_1star_email, al_1star_push "
            "FROM restaurants WHERE id=?", (restaurant_id,)).fetchone()
        review = conn.execute(
            "SELECT author, rating, platform FROM reviews WHERE id=? AND restaurant_id=?",
            (review_id, restaurant_id)).fetchone()
        conn.close()
        if not r or not r["alert_resp_approved"]:
            return
        if _daily_alert_suppressed(restaurant_id, "resp_approved", db_path):
            return
        name = r["name"] or "your restaurant"
        author = (review["author"] or "a guest") if review else "a guest"
        rating = (review["rating"] if review else None) or ""
        platform = ((review["platform"] or "google") if review else "google").title()
        verb = f"published to {platform}" if posted else "approved"
        subject = f"Reply {verb} — {name}"
        body = f"Your reply to {author}'s {rating}★ review was {verb}."
        html = _alert_email_html(
            name, f"✅ Reply {verb}",
            [body, "No action needed — this is a confirmation."],
            cta_label="See the review", restaurant_id=restaurant_id,
        )
        if r["owner_email"] and (r["al_1star_email"] if "al_1star_email" in r.keys() else 1):
            _email_alert(restaurant_id, r["owner_email"], subject, html, "resp_approved",
                         db_path, review_id=review_id)
        _log_alert(restaurant_id, "resp_approved", review_id, db_path=db_path)
        try:
            from push import fire_push as _fp
            _fp(restaurant_id, "resp_approved", subject, body,
                data={"alert_type": "resp_approved", "review_id": review_id}, db_path=db_path)
        except Exception:
            pass
    except Exception as e:
        print(f"[notify] resp_approved alert error rid={restaurant_id}: {e}")



def _gated_out(restaurant_id, local_hour, claim_key, db_path: str = DB_PATH, until=14):
    """True when this restaurant should be skipped this pass.

    `local_hour=None` means "no gate" — the function runs for everyone, which
    is what a direct call (a test, an admin re-run) wants. The scheduler
    passes an hour instead: it attempts these checks every hour, and each
    restaurant is served at that hour in ITS OWN timezone, once per local
    day. Before this they all fired at 10am Chicago, so a Pacific client was
    alerted at 8am and an Eastern one at 11am.
    """
    if local_hour is None:
        return False
    try:
        import ops
        from time_utils import restaurant_now_by_id
        local = restaurant_now_by_id(restaurant_id, naive=True)
        if not (local_hour <= local.hour < until):
            return True
        job, period = f"{claim_key}:{restaurant_id}", local.date().isoformat()
        if _batch is not None:
            # Inside the morning batch the day is claimed at flush, once the
            # alerts collected here have actually gone out (MOD-NOT-16).
            if ops.period_claimed(job, period):
                return True
            _batch_claims.append((job, period))
            return False
        return not ops.claim_period(job, period)
    except Exception:
        # Fail open: alert rather than silently skip a day.
        return False


# One hourly pass stops taking on restaurants after this long. With the
# morning batch a restaurant's day is claimed only once it was checked and
# flushed, so whoever this pass did not reach is picked up by the next hourly
# pass inside the 10am-2pm window — the claims are the cursor (MOD-NOT-11).
DAILY_ALERT_PASS_SECONDS = int(os.getenv("DAILY_ALERT_PASS_SECONDS", "600"))


def _pass_deadline(local_hour):
    import time as _time
    return None if local_hour is None else _time.monotonic() + DAILY_ALERT_PASS_SECONDS


def _past(deadline):
    import time as _time
    return deadline is not None and _time.monotonic() > deadline


def _in_window_only(rows, id_key, local_hour, until=14):
    """The rows whose restaurant is inside its local window this hour, read
    from the timezone each row already carries. The scheduler runs these
    checks every hour; asking every restaurant "is it 10am there?" cost a
    query each, forever, to skip nearly all of them (MOD-NOT-11)."""
    if local_hour is None:
        return list(rows)
    from time_utils import known_timezones, restaurant_now_by_id
    keep = []
    with known_timezones({r[id_key]: r["timezone"] for r in rows}):
        for r in rows:
            try:
                hour = restaurant_now_by_id(r[id_key], naive=True).hour
            except Exception:
                keep.append(r)          # fail open, as _gated_out does
                continue
            if local_hour <= hour < until:
                keep.append(r)
    return keep


def check_no_response_alerts(db_path: str = DB_PATH, local_hour: int = None):
    """
    Called daily by the scheduler. Fires alerts for restaurants with negative
    reviews unresponded for 48+ hours. Fires both email and SMS per restaurant flags.
    """
    conn = models.get_conn(db_path)
    # 'drafted' as well as 'pending': auto-drafting makes a draft waiting on
    # the owner the normal state of an unanswered review, so counting only
    # 'pending' meant this nudge almost never fired. A soft-deleted
    # (retention-purged) review is not actionable and never counts
    # (MOD-NOT-9).
    rows = conn.execute("""
        SELECT r.restaurant_id, rest.name, rest.owner_email, rest.timezone,
               rest.urgent_via_sms, rest.urgent_via_email,
               rest.al_unres_sms, rest.al_unres_email, rest.al_unres_push,
               COUNT(*) as overdue_count
        FROM reviews r
        JOIN restaurants rest ON rest.id = r.restaurant_id
        WHERE r.sentiment='negative'
          AND r.response_status IN ('pending', 'drafted')
          AND r.deleted_at IS NULL
          AND r.fetched_at <= datetime('now', '-48 hours')
          AND """ + models.in_service_sql("rest.billing_status") + """
          AND rest.alert_no_response = 1
          AND (rest.urgent_via_sms = 1 OR rest.urgent_via_email = 1 OR rest.al_unres_push = 1)
        GROUP BY r.restaurant_id
    """).fetchall()
    conn.close()

    deadline = _pass_deadline(local_hour)
    for row in _in_window_only(rows, "restaurant_id", local_hour):
        if _past(deadline):
            break
        rid         = row["restaurant_id"]
        if _gated_out(rid, local_hour, "no_response_alerts", db_path):
            continue
        name        = row["name"]
        n           = row["overdue_count"]
        # 24h dedup
        conn2 = models.get_conn(db_path)
        already = conn2.execute("""
            SELECT id FROM alert_log
            WHERE restaurant_id=? AND alert_type='no_response'
            AND fired_at >= datetime('now', '-24 hours')
        """, (rid,)).fetchone()
        conn2.close()
        if already:
            continue

        review_word = "reviews" if n > 1 else "review"
        sms = (
            f"⏰ {name}: {n} negative {review_word} with no response for 48+ hours.\n"
            f"dashboard.cavnar.ai"
        )
        # Through raise_alert like every other non-review alert: it was the
        # last one still hand-rolling its own SMS/email/push/log/webhook, and
        # so the last one with no quiet-hours check, no cap, no ceiling and
        # no rush hold. Its channel gates (al_unres_*) are in deliver_alert's
        # type_map under both the names this alert has been called.
        raise_alert(rid, "no_response", sms, f"Reviews waiting on a reply — {name}", lines=[
            f"<strong>{n} negative {review_word}</strong> have been waiting for a "
            f"response for over 48 hours.",
            "Responding promptly helps protect your rating.",
        ], db_path=db_path)


# A labor snapshot older than this is history, not news. The alert used to
# take the most recently SAVED row with no bound on the period it covered.
LABOR_ALERT_MAX_PERIOD_AGE_DAYS = 21


def _short_period(start, end) -> str:
    """"Aug 25-31" for an alert body. The SMS and the push carried no period
    at all, so a figure from months ago read as this week's."""
    from datetime import datetime as _d
    try:
        a = _d.strptime(str(start)[:10], "%Y-%m-%d")
        b = _d.strptime(str(end)[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        return str(start or "the last synced period")
    if a.month == b.month:
        return f"{a.strftime('%b')} {a.day}-{b.day}"
    return f"{a.strftime('%b %-d')}-{b.strftime('%b %-d')}"


def check_daily_alerts(db_path: str = DB_PATH, local_hour: int = None):
    """
    Daily check for negative trend, rating threshold, and labor over target.
    Called once per day by the scheduler alongside check_no_response_alerts.
    """
    conn = models.get_conn(db_path)
    # Selected on the alert switches, not the SMS/email switches: push has
    # no global switch (deliver_alert gates it per type), so an owner with
    # SMS and email both off and push on was dropped here and never heard
    # about labor, a declining trend or the rating floor (MOD-NOT-8).
    restaurants = conn.execute("""
        SELECT id, name, owner_email, timezone,
               urgent_via_sms, urgent_via_email,
               alert_negative_trend,
               alert_rating_threshold, alert_rating_floor, gbp_rating,
               alert_labor_over, labor_target_pct
        FROM restaurants
        WHERE (COALESCE(alert_negative_trend,0) = 1 OR COALESCE(alert_rating_threshold,0) = 1
               OR COALESCE(alert_labor_over,0) = 1)
          AND """ + models.in_service_sql() + """
    """).fetchall()
    conn.close()

    deadline = _pass_deadline(local_hour)
    for r in _in_window_only(restaurants, "id", local_hour):
        if _past(deadline):
            break
        rid         = r["id"]
        # 10am where the restaurant is (the scheduler attempts this hourly).
        if _gated_out(rid, local_hour, "daily_alerts", db_path):
            continue
        name        = r["name"]
        via_sms     = bool(r["urgent_via_sms"])
        via_email   = bool(r["urgent_via_email"])
        owner_email = r["owner_email"] or ""
        if via_email and not owner_email:
            print(f"[notify] rid={rid} has email alerts on but no owner_email — email suppressed")

        def _fire(sms_text, subject, lines, alert_type):
            # One delivery path for every alert in the product. This used to
            # be a bespoke closure that sent SMS, email, push, log and
            # webhook itself — which is why these alert types were the only
            # ones with no rush holding and no resolved CTA link.
            #
            # `lines` rather than a built html body: when several of these
            # fire in the same 10am pass they are folded into one morning
            # notification, and that needs the sentences, not a finished
            # email (see DAILY_BATCH_TYPES).
            raise_alert(rid, alert_type, sms_text, subject, lines=lines, db_path=db_path)

        def _already_alerted(alert_type):
            # A 7-day window, not 24h — this job runs once a day, so a
            # persisting condition (rating still under the floor, labor
            # still over target) with a 24h dedup would re-fire on every
            # single run, sending the same alert daily until the owner
            # fixes it. A week between repeats for the SAME unresolved
            # issue is still timely without becoming daily noise.
            c2 = models.get_conn(db_path)
            row = c2.execute("""
                SELECT id FROM alert_log
                WHERE restaurant_id=? AND alert_type=?
                AND fired_at >= datetime('now', '-7 days')
            """, (rid, alert_type)).fetchone()
            c2.close()
            return row is not None

        # ── Negative trend ────────────────────────────────────
        if r["alert_negative_trend"] and not _already_alerted("negative_trend"):
            c2 = models.get_conn(db_path)
            # COALESCE(NULLIF(review_date,''), fetched_at), not review_date
            # alone: a CSV/manual import with no date was excluded from its
            # own restaurant's trend entirely. Soft-deleted rows excluded.
            # `n` comes back so a "week" of one review cannot be a trend.
            weeks = c2.execute("""
                SELECT strftime('%Y-%W', COALESCE(NULLIF(review_date,''), fetched_at)) as week,
                       AVG(rating) as avg_rating,
                       COUNT(*)    as n
                FROM reviews
                WHERE restaurant_id=?
                  AND deleted_at IS NULL
                  AND COALESCE(NULLIF(review_date,''), fetched_at) >= date('now', '-28 days')
                  AND rating IS NOT NULL
                GROUP BY week
                ORDER BY week ASC
            """, (rid,)).fetchall()
            c2.close()
            # Three consecutive weekly averages, each over at least
            # MIN_TREND_REVIEWS_PER_WEEK reviews. Without the volume gate a
            # single 5-star week followed by a single 3-star week read as a
            # "rating declining 3 weeks in a row" SMS.
            recent = weeks[-3:]
            if len(recent) >= 3 and all((w["n"] or 0) >= MIN_TREND_REVIEWS_PER_WEEK for w in recent):
                avgs = [w["avg_rating"] for w in recent]
                if avgs[0] > avgs[1] > avgs[2]:
                    sms  = (
                        f"📉 {name}: Average rating has declined 3 weeks in a row "
                        f"({avgs[0]:.1f} → {avgs[1]:.1f} → {avgs[2]:.1f}★).\n"
                        f"dashboard.cavnar.ai"
                    )
                    _fire(sms, f"Rating declining — {name}", [
                        f"Weekly average ratings have dropped 3 weeks in a row: "
                        f"<strong>{avgs[0]:.1f} → {avgs[1]:.1f} → {avgs[2]:.1f}★</strong>",
                        "This trend warrants a closer look at what guests are saying.",
                    ], "negative_trend")

        # ── Rating drops below threshold ───────────────────────
        if r["alert_rating_threshold"] and not _already_alerted("rating_threshold"):
            gbp_rating = r["gbp_rating"]
            floor      = r["alert_rating_floor"] or baseline_rating_floor(rid, db_path) or 4.0
            if gbp_rating is not None and gbp_rating < floor:
                sms  = (
                    f"⚠️ {name}: Google rating dropped to {gbp_rating:.1f}★ "
                    f"(below your {floor:.1f}★ threshold).\n"
                    f"dashboard.cavnar.ai"
                )
                _fire(sms, f"Rating below your threshold — {name}", [
                    f"Current Google rating: <strong>{gbp_rating:.1f}★</strong> — "
                    f"below your alert threshold of {floor:.1f}★.",
                    "Responding to recent negative reviews can help recover your score.",
                ], "rating_threshold")

        # ── Labor over target ──────────────────────────────────
        if r["alert_labor_over"] and not _already_alerted("labor_over"):
            c2 = models.get_conn(db_path)
            # Bounded on the age of the PERIOD, not just on when the snapshot
            # happened to be written. Snapshots are saved every time an
            # insight is generated — i.e. on every Labor tab open — so a
            # restaurant that last uploaded in June kept getting a fresh
            # "labor over target" text every seven days, forever, quoting
            # June. An alert about a period nobody is working any more is
            # not an alert, it is noise the owner learns to ignore.
            recent = c2.execute("""
                SELECT labor_pct, period_start, period_end
                FROM labor_history
                WHERE restaurant_id=?
                  AND period_end >= date('now', ?)
                ORDER BY period_end DESC, saved_at DESC LIMIT 1
            """, (rid, f"-{LABOR_ALERT_MAX_PERIOD_AGE_DAYS} days")).fetchone()
            c2.close()
            if recent and recent["labor_pct"] is not None:
                actual = recent["labor_pct"]
                target = r["labor_target_pct"] or baseline_labor_target(rid, db_path) or 30.0
                if actual > target:
                    over_by = round(actual - target, 1)
                    _period_label = _short_period(recent["period_start"], recent["period_end"])
                    sms  = (
                        f"💸 {name}: Labor at {actual:.1f}% for {_period_label} — "
                        f"{over_by}pts over your {target:.0f}% target.\n"
                        f"dashboard.cavnar.ai"
                    )
                    _fire(sms, f"Labor over target — {name}", [
                        f"Most recent labor period: <strong>{actual:.1f}%</strong> — "
                        f"<strong>{over_by} points over</strong> your {target:.0f}% target.",
                        f"Period: {recent['period_start']} – {recent['period_end']}",
                    ], "labor_over")


def health_bypasses_quiet_hours(restaurant_id: int, db_path: str = DB_PATH) -> bool:
    try:
        conn = models.get_conn(db_path)
        row = conn.execute("SELECT alert_health_bypass_quiet FROM restaurants WHERE id=?", (restaurant_id,)).fetchone()
        conn.close()
        return bool(row and row["alert_health_bypass_quiet"])
    except Exception:
        return False


def _lost_query_lines(restaurant_id: int, db_path: str = DB_PATH) -> list:
    """The actual questions that stopped mentioning this restaurant.

    The alert has always said "open Intel to see which questions changed",
    and nothing could produce that — ai_visibility_runs stored a score and
    no questions, so the previous run's were never written down. They are
    now, so the alert can say it in the alert.
    """
    try:
        from models import ai_visibility_query_diff
        diff = ai_visibility_query_diff(restaurant_id, db_path)
    except Exception:
        return []
    if not diff.get("ok") or not diff.get("lost"):
        return []
    import html as _h
    qs = [_h.escape(q) for q in diff["lost"][:3]]
    more = len(diff["lost"]) - len(qs)
    line = "These stopped mentioning you: " + "; ".join(f"<em>{q}</em>" for q in qs)
    if more > 0:
        line += f" (and {more} more)"
    return [line + "."]


# A visibility score is appearances over a handful of questions answered by
# a non-deterministic model. The alert threshold used to be 15 points on a
# three-question run, where the score could only be 0, 33, 67 or 100 — so
# every single question flipping cleared it, and the owner got a text about
# a decline that was model variance rather than anything about their
# business. Two bars now: the run has to be big enough to say anything, and
# the move has to be more than one question changing its mind.
AI_VISIBILITY_MIN_SAMPLE = 5
AI_VISIBILITY_MIN_QUERIES_MOVED = 2


def _ai_visibility_drop(runs: list):
    """(current, previous, questions_moved) when a drop is worth telling
    the owner about, else None."""
    if len(runs) != 2:
        return None
    now, prev = runs[0], runs[1]
    n_now, n_prev = now.get("answered") or 0, prev.get("answered") or 0
    if n_now < AI_VISIBILITY_MIN_SAMPLE or n_prev < AI_VISIBILITY_MIN_SAMPLE:
        return None
    # Compare like with like: two runs over different numbers of questions
    # are not comparable as counts, so scale the previous appearance rate
    # onto this run's sample.
    a_now = now.get("appeared")
    a_prev = prev.get("appeared")
    if a_now is None or a_prev is None:
        return None
    expected_now = (a_prev / n_prev) * n_now
    moved = expected_now - a_now
    if moved < AI_VISIBILITY_MIN_QUERIES_MOVED:
        return None
    s_now, s_prev = now.get("ai_score"), prev.get("ai_score")
    if s_now is None or s_prev is None or s_now >= s_prev:
        return None
    return s_now, s_prev, int(round(moved))


def check_extra_daily_alerts(db_path: str = DB_PATH, local_hour: int = None):
    """The two daily triggers added by the settings audit — food waste and
    an AI-visibility drop — run right after check_daily_alerts(). Same
    7-day repeat window, same three channels (email to owner + extra
    recipients, SMS to consented contacts, push)."""
    conn = models.get_conn(db_path)
    restaurants = conn.execute("""
        SELECT id, name, owner_email, timezone, urgent_via_sms, urgent_via_email,
               alert_food_waste, alert_ai_visibility_drop
        FROM restaurants
        WHERE (COALESCE(alert_food_waste,0)=1 OR COALESCE(alert_ai_visibility_drop,0)=1)
          AND """ + models.in_service_sql() + """
    """).fetchall()
    conn.close()

    deadline = _pass_deadline(local_hour)
    for r in _in_window_only(restaurants, "id", local_hour):
        if _past(deadline):
            break
        rid, name = r["id"], r["name"]
        if _gated_out(rid, local_hour, "extra_alerts", db_path):
            continue
        owner_email = r["owner_email"] or ""

        def _recent(alert_type):
            c2 = models.get_conn(db_path)
            row = c2.execute("""SELECT id FROM alert_log WHERE restaurant_id=? AND alert_type=?
                                AND fired_at >= datetime('now', '-7 days')""", (rid, alert_type)).fetchone()
            c2.close()
            return row is not None

        def _fire(alert_type, sms_text, subject, lines, value=None):
            raise_alert(rid, alert_type, sms_text, subject, lines=lines,
                        db_path=db_path, value=value)

        # ── Food waste ────────────────────────────────────────
        if r["alert_food_waste"] and not _recent("food_waste"):
            try:
                # load_inventory_for_restaurant returns (items, is_live). The
                # tuple used to be passed straight into analyse_inventory,
                # which subscripts each element by string key — so this alert
                # raised TypeError on every restaurant, every day, and the
                # bare except below printed it and moved on. It had never
                # fired. is_live now gates it too: without that, fixing the
                # unpack would start emailing sample-pantry dollars to
                # restaurants that have no inventory connected.
                from inventory import analysis_for
                items, is_live, analysis = analysis_for(rid)
                waste_items = (analysis or {}).get("waste_items") or [] if (items and is_live) else []
                total = sum(float(x.get("waste_cost") or 0) for x in waste_items)
                flagged = [x for x in waste_items if float(x.get("waste_cost") or 0) > 0]
                # Fire on deterioration, not on a level: a restaurant that is
                # chronically over tolerance used to get the identical alert
                # every 7 days forever. _waste_alert_worsened compares against
                # what was last alerted on.
                if (len(flagged) >= 3 or total >= 150) and _waste_alert_worsened(rid, total, db_path=db_path):
                    top = ", ".join(x.get("item", "?") for x in flagged[:3])
                    _fire("food_waste",
                          f"Cavnar AI: ${total:,.0f} of waste flagged this week at {name} ({top}).",
                          f"Food waste flagged — {name}",
                          [f"${total:,.0f} of waste across {len(flagged)} items this week.",
                           f"Biggest: {top}.", "Open Food Cost to see the breakdown."],
                          value=total)
            except Exception as e:
                # Was a bare print, which is how a TypeError on every run for
                # every restaurant stayed invisible for the life of the alert.
                import ops
                ops.capture(e, job="notify.food_waste", context=f"rid={rid}", db_path=db_path)
                print(f"[notify] food waste check error rid={rid}: {e}")

        # ── Running out before the next delivery ──────────────
        # The two conditions an owner most wants pushed at them had no alert
        # at all: an item that runs out before the truck comes, and a Big-8
        # ingredient whose price is climbing. Both ride the same food-cost
        # preference as waste rather than adding two more toggles.
        if r["alert_food_waste"] and not _recent("critical_low"):
            try:
                from inventory import analysis_for
                items, is_live, analysis = analysis_for(rid)
                crit = (analysis or {}).get("critical_low") or [] if (items and is_live) else []
                if crit:
                    names = ", ".join(x.get("item", "?") for x in crit[:3])
                    _fire("critical_low",
                          f"Cavnar AI: {len(crit)} item(s) run out before your next delivery at {name} ({names}).",
                          f"Running out before delivery — {name}",
                          [f"{len(crit)} item(s) won't last until the next delivery.",
                           f"Soonest: {names}.", "Open Food Cost to send the order."],
                          value=float(len(crit)))
            except Exception as e:
                import ops
                ops.capture(e, job="notify.critical_low", context=f"rid={rid}", db_path=db_path)
                print(f"[notify] critical low check error rid={rid}: {e}")

        # ── Ingredient price climbing ────────────────────────
        if r["alert_food_waste"] and not _recent("price_spike"):
            try:
                from inventory import load_inventory_for_restaurant, compute_item_trends, build_price_watch
                _pw_items, _pw_live = load_inventory_for_restaurant(rid)
                watch = build_price_watch(compute_item_trends(rid, _pw_items)) if (_pw_items and _pw_live) else []
                big = [w for w in watch if w.get("is_big_8") and (w.get("change_pct") or 0) >= 5]
                if big:
                    top = big[0]
                    _fire("price_spike",
                          f"Cavnar AI: {top['item']} is up {abs(top['change_pct']):.0f}% at {name}.",
                          f"Ingredient price climbing — {name}",
                          [f"{top['item']} moved from ${top['old_price']:.2f} to ${top['new_price']:.2f}"
                           f" ({abs(top['change_pct']):.0f}%).",
                           top.get("action_hint") or "", "Open Food Cost to see Price Watch."],
                          value=float(top.get("change_pct") or 0))
            except Exception as e:
                import ops
                ops.capture(e, job="notify.price_spike", context=f"rid={rid}", db_path=db_path)
                print(f"[notify] price spike check error rid={rid}: {e}")

        # ── AI visibility drop ───────────────────────────────
        if r["alert_ai_visibility_drop"] and not _recent("ai_visibility_drop"):
            try:
                from models import last_two_ai_visibility_runs
                runs = last_two_ai_visibility_runs(rid, db_path)
                drop = _ai_visibility_drop(runs)
                if drop:
                    now_s, prev_s, moved = drop
                    _fire("ai_visibility_drop",
                          f"Cavnar AI: {name}'s AI visibility fell from {prev_s}% to {now_s}% "
                          f"({moved} fewer of the questions we ask mentioned you).",
                          f"AI visibility dropped — {name}",
                          [f"Your visibility in Perplexity went from <strong>{prev_s}%</strong> to "
                           f"<strong>{now_s}%</strong> since the last check.",
                           f"That is {moved} fewer of the questions we ask that mentioned you.",
                           *_lost_query_lines(rid, db_path),
                           "Perplexity varies run to run, so a small move is normal. This one was "
                           "large enough to be worth a look.",
                           "Open Intel → AI Visibility for the full picture."])
            except Exception as e:
                print(f"[notify] ai visibility check error rid={rid}: {e}")
