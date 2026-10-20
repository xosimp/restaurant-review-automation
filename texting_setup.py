"""texting_setup.py — a restaurant's own guest texting, set up from inside
Cavnar AI (owner, 10/20/26: "think in terms of scale").

Nobody signs into Twilio. Cavnar AI is the ISV: every restaurant's texting
is registered under Cavnar AI's one Twilio account, as that restaurant's own
business, so a carrier sees "Simple EJ's" sending Simple EJ's texts and one
restaurant's complaints never touch another's number. The owner or a
marketing manager fills one form (legal name, EIN, address, a contact) and
the rest runs here, step by step, each step's Twilio id kept so a failure or
a deploy resumes where it stopped and never pays a fee twice:

  1. Secondary Customer Profile (Trust Hub): the business, its authorized
     contact and its address, tied to Cavnar AI's Primary (ISV) profile,
     checked against Twilio's policy, then sent for review.
  2. A2P profile (Trust Hub TrustProduct): the messaging profile on top.
  3. Brand registration (Low-Volume Standard: an EIN, under 6,000 segments
     a day) — $4.50 once.
  4. A Messaging Service for the restaurant, with a local number in it.
  5. Once the brand is APPROVED: the campaign (LOW_VOLUME mixed: thank-yous,
     offers, replies) — $15 vetting once, $1.50 a month.
  6. Once the campaign is VERIFIED: guest_hub.set_sender, and every guest
     text from this restaurant goes out on its own number and campaign.

Steps 1-4 run when the form is sent; 5 and 6 on the hourly
`texting_setup_advance` job, which polls the brand and the campaign.

Billing (owner, 10/20/26): everything bills to Cavnar AI's Twilio account —
the ISV model requires it, and it keeps one bill. A restaurant pays nothing
extra up to TEXTS_INCLUDED_MONTHLY texts a month (pricing.py); usage() is
what the screen and the admin read, from sms_log.

It creates real, billed Twilio resources, so it runs only where the
scheduler may run (scheduler.scheduling_allowed: Railway) — a local backend
holds production's Twilio keys.
"""
import json
import os
import re
import time

import logging

import requests

from models import DB_PATH

log = logging.getLogger(__name__)

# ── Twilio's fixed ids and values ────────────────────────────────────────────
SECONDARY_PROFILE_POLICY = "RNdfbf3fae0e1107f8aded0e7cead80bf5"
A2P_PROFILE_POLICY = "RNb0d4771c2c98518d916a3d4cd70a8f8b"
TRUSTHUB = "https://trusthub.twilio.com/v1"
MESSAGING = "https://messaging.twilio.com/v1"
API = "https://api.twilio.com/2010-04-01"
TIMEOUT = 15
CAMPAIGN_USECASE = "LOW_VOLUME"

BUSINESS_TYPES = ("Sole Proprietorship", "Partnership", "Limited Liability Corporation", "Corporation",
                  "Co-operative", "Non-profit Corporation")
JOB_POSITIONS = ("Director", "GM", "VP", "CEO", "CFO", "General Counsel", "Other")

# Statuses the screen reads, in order.
STATUSES = ("draft", "needs_fix", "brand_review", "campaign_review", "live", "failed")
ADVANCE_MAX = 25            # setups the hourly job looks at per pass
ADVANCE_SECONDS = 120       # its wall-clock bound

_FIELDS = ("legal_name", "ein", "business_type", "website", "street", "street2", "city", "state", "zip",
           "first_name", "last_name", "email", "phone", "title", "job_position", "area_code")


def get_conn(db_path=None):
    import models
    return models.get_conn(db_path) if db_path and db_path != DB_PATH else models.get_conn()


def init_texting_setup(db_path=DB_PATH):
    conn = get_conn(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS texting_setups (
            restaurant_id  INTEGER PRIMARY KEY,
            status         TEXT NOT NULL DEFAULT 'draft',
            form           TEXT,
            sids           TEXT,
            brand_status   TEXT,
            campaign_status TEXT,
            phone_number   TEXT,
            last_error     TEXT,
            submitted_by   INTEGER,
            submitted_at   TEXT,
            live_at        TEXT,
            checked_at     TEXT,
            created_at     TEXT DEFAULT (datetime('now')),
            updated_at     TEXT DEFAULT (datetime('now'))
        )""")
        conn.commit()
    finally:
        conn.close()


# ── The row ──────────────────────────────────────────────────────────────────

def _row(rid, db_path=DB_PATH) -> dict | None:
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT * FROM texting_setups WHERE restaurant_id=?", (rid,)).fetchone()
    finally:
        conn.close()
    if not r:
        return None
    d = dict(r)
    for k in ("form", "sids"):
        try:
            d[k] = json.loads(d.get(k) or "{}")
        except (TypeError, ValueError):
            d[k] = {}
    return d


def _save(rid, db_path=DB_PATH, **cols):
    if "form" in cols:
        cols["form"] = json.dumps(cols["form"])
    if "sids" in cols:
        cols["sids"] = json.dumps(cols["sids"])
    conn = get_conn(db_path)
    try:
        conn.execute("INSERT INTO texting_setups (restaurant_id) VALUES (?) ON CONFLICT(restaurant_id) DO NOTHING",
                     (rid,))
        if cols:
            sets = ", ".join(f"{k}=?" for k in cols)
            conn.execute(f"UPDATE texting_setups SET {sets}, updated_at=datetime('now') WHERE restaurant_id=?",
                         (*cols.values(), rid))
        conn.commit()
    finally:
        conn.close()


# ── The form ─────────────────────────────────────────────────────────────────

def _digits(v) -> str:
    return re.sub(r"\D", "", str(v or ""))


def clean_form(raw) -> tuple[dict, list]:
    """(form, problems) — the owner's answers trimmed and checked the way
    Twilio's policy will check them, so a missing field is said here, in
    plain words, before a fee is paid."""
    raw = raw if isinstance(raw, dict) else {}
    f = {k: str(raw.get(k) or "").strip()[:200] for k in _FIELDS}
    probs = []
    if len(f["legal_name"]) < 2:
        probs.append("The business's legal name, exactly as the IRS has it.")
    ein = _digits(f["ein"])
    if len(ein) != 9:
        probs.append("The EIN: 9 digits, like 12-3456789.")
    f["ein"] = f"{ein[:2]}-{ein[2:]}" if len(ein) == 9 else f["ein"]
    if f["business_type"] not in BUSINESS_TYPES:
        probs.append("The kind of business (an LLC, a corporation…).")
    site = f["website"]
    if site and not re.match(r"^https?://", site, re.I):
        site = "https://" + site
    f["website"] = site
    if not re.match(r"^https?://[^\s/]+\.[^\s/]+", site):
        probs.append("The restaurant's website.")
    if not f["street"] or not f["city"]:
        probs.append("The business address.")
    f["state"] = f["state"].upper()[:2]
    if not re.fullmatch(r"[A-Z]{2}", f["state"]):
        probs.append("The state, as two letters (IL).")
    f["zip"] = _digits(f["zip"])[:5]
    if len(f["zip"]) != 5:
        probs.append("The 5-digit ZIP code.")
    if not f["first_name"] or not f["last_name"]:
        probs.append("Who carriers may contact: a first and last name.")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", f["email"]):
        probs.append("That person's email.")
    ph = _digits(f["phone"])
    ph = ph[1:] if len(ph) == 11 and ph.startswith("1") else ph
    if len(ph) != 10:
        probs.append("That person's 10-digit phone number.")
    f["phone"] = ph
    if not f["title"]:
        probs.append("That person's title (Owner, General Manager…).")
    if f["job_position"] not in JOB_POSITIONS:
        f["job_position"] = "Other"
    ac = _digits(f["area_code"])[:3]
    f["area_code"] = ac if len(ac) == 3 else ""
    return f, probs


# ── What the screen reads ────────────────────────────────────────────────────

_STATUS_WORDS = {
    "draft": "Not set up yet",
    "needs_fix": "Needs a fix before it can go on",
    "brand_review": "Your business is with the carriers for review",
    "campaign_review": "Your business is approved — your texting program is with the carriers",
    "live": "Texting is live",
    "failed": "The carriers turned it down",
}


def status(rid, db_path=DB_PATH) -> dict:
    """What the Guests screen shows about setting up texting."""
    row = _row(rid, db_path) or {}
    st = row.get("status") or "draft"
    form = dict(row.get("form") or {})
    # The EIN is shown masked once it is sent; the full form stays editable while a draft.
    if st != "draft" and st != "needs_fix" and form.get("ein"):
        form["ein"] = "••-•••" + form["ein"][-4:]
    from time_utils import mdy
    return {
        "status": st,
        "headline": _STATUS_WORDS.get(st, st),
        "steps": _steps(st),
        "form": form,
        "number": _display(row.get("phone_number")),
        "error": row.get("last_error") if st in ("needs_fix", "failed") else None,
        "submitted": mdy(row["submitted_at"]) if row.get("submitted_at") else None,
        "live_since": mdy(row["live_at"]) if row.get("live_at") else None,
        "business_types": list(BUSINESS_TYPES),
        "job_positions": list(JOB_POSITIONS),
        "usage": usage(rid, db_path=db_path),
        "editable": st in ("draft", "needs_fix", "failed"),
    }


def _steps(st) -> list:
    order = ["Your details", "Business review", "Texting program review", "Live"]
    reached = {"draft": 0, "needs_fix": 0, "failed": 0, "brand_review": 1, "campaign_review": 2, "live": 4}.get(st, 0)
    return [{"label": l, "done": i < reached, "current": i == reached} for i, l in enumerate(order)]


def _display(p):
    d = _digits(p)
    d = d[1:] if len(d) == 11 else d
    return f"({d[:3]}) {d[3:6]}-{d[6:]}" if len(d) == 10 else None


def usage(rid, month=None, db_path=DB_PATH) -> dict:
    """{"texts", "included", "over", "cost_usd"} — this month's guest texts
    from this restaurant (sms_log, accepted or better), against the
    allowance the plan includes. `cost_usd` is Twilio's own price where its
    status callback reported one."""
    import pricing
    from datetime import date
    month = month or date.today().strftime("%Y-%m")
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT COUNT(*) AS n, SUM(COALESCE(cost_usd, 0)) AS c FROM sms_log WHERE restaurant_id=? "
                         "AND use_case='guest' AND COALESCE(status,'') NOT IN ('failed','undelivered','refused','held') "
                         "AND substr(created_at, 1, 7)=?", (rid, month)).fetchone()
    except Exception:
        r = None
    finally:
        conn.close()
    n = int((r["n"] if r else 0) or 0)
    inc = pricing.TEXTS_INCLUDED_MONTHLY
    return {"month": month, "texts": n, "included": inc, "over": max(0, n - inc),
            "cost_usd": round(float((r["c"] if r else 0) or 0), 2)}


# ── Saving and sending the form ──────────────────────────────────────────────

def save(rid, raw, user_id=None, db_path=DB_PATH) -> dict:
    """Keep the owner's answers (a draft); nothing goes to Twilio."""
    row = _row(rid, db_path) or {}
    if row.get("status") not in (None, "draft", "needs_fix", "failed"):
        return {"ok": False, "error": "This is already with the carriers; it can't be changed now."}
    form, probs = clean_form(raw)
    _save(rid, db_path, form=form)
    return {"ok": True, "problems": probs}


def _live_refused():
    """None where real Twilio resources may be created; else the reason."""
    try:
        import scheduler
        if scheduler.scheduling_allowed():
            return None
    except Exception:
        pass
    return "Texting can only be set up from the live Cavnar AI site."


def _env():
    return (os.getenv("TWILIO_ACCOUNT_SID", ""), os.getenv("TWILIO_AUTH_TOKEN", ""),
            os.getenv("TWILIO_ISV_PROFILE_SID", ""), os.getenv("TWILIO_TRUSTHUB_EMAIL", ""))


def submit(rid, raw, user_id=None, db_path=DB_PATH) -> dict:
    """Send the form: check it, keep it, then run every step that does not
    wait on a carrier (advance). The fees start here, so a form with a
    problem never leaves."""
    row = _row(rid, db_path) or {}
    if row.get("status") not in (None, "draft", "needs_fix", "failed"):
        return {"ok": False, "error": "This is already with the carriers."}
    form, probs = clean_form(raw)
    if probs:
        _save(rid, db_path, form=form)
        return {"ok": False, "problems": probs, "error": "A few details are missing."}
    refused = _live_refused()
    if refused:
        return {"ok": False, "error": refused}
    sid, token, primary, email = _env()
    if not (sid and token and primary and email):
        return {"ok": False, "error": "Texting setup isn't switched on yet. Cavnar AI has been told."}
    sids = dict(row.get("sids") or {})
    if row.get("status") == "failed":
        # A turned-down campaign is sent again on the same brand; a
        # turned-down brand starts over from the profile.
        if row.get("brand_status") == "FAILED":
            sids = {}
        elif sids.get("campaign"):
            # A service holds one campaign: the turned-down one goes first.
            sids["delete_campaign"] = sids.pop("campaign")
    _save(rid, db_path, form=form, sids=sids, status="brand_review", last_error=None, submitted_by=user_id,
          submitted_at=time.strftime("%Y-%m-%d %H:%M:%S"))
    out = advance(rid, db_path=db_path)
    return {"ok": out.get("status") not in ("needs_fix", "failed"), **status(rid, db_path)}


# ── Twilio ───────────────────────────────────────────────────────────────────

class TwilioStepError(Exception):
    def __init__(self, message, fixable=False):
        super().__init__(message)
        self.fixable = fixable


def _tw(method, url, data=None, params=None) -> dict:
    sid, token, _p, _e = _env()
    r = requests.request(method, url, auth=(sid, token), data=data, params=params, timeout=TIMEOUT)
    try:
        body = r.json() if r.content else {}
    except ValueError:
        body = {}
    if r.status_code >= 400:
        msg = (body or {}).get("message") or f"Twilio answered {r.status_code}"
        raise TwilioStepError(str(msg)[:300], fixable=400 <= r.status_code < 500)
    return body or {}


def _assign(kind, parent, child, sids, key):
    """One EntityAssignment, once."""
    done = set(sids.get("assigned") or [])
    if key in done:
        return
    _tw("POST", f"{TRUSTHUB}/{kind}/{parent}/EntityAssignments", {"ObjectSid": child})
    done.add(key)
    sids["assigned"] = sorted(done)


def _restaurant_name(rid):
    try:
        import models
        r = models.get_restaurant(rid)
        return (getattr(r, "name", None) or "").strip() or f"Restaurant {rid}"
    except Exception:
        return f"Restaurant {rid}"


def _profile_steps(rid, f, sids, put):
    """Steps 1-2: the Secondary Customer Profile and the A2P profile."""
    _sid, _tok, primary, email = _env()
    name = _restaurant_name(rid)
    if not sids.get("profile"):
        sids["profile"] = _tw("POST", f"{TRUSTHUB}/CustomerProfiles", {
            "PolicySid": SECONDARY_PROFILE_POLICY, "FriendlyName": f"{name} (rid {rid})", "Email": email})["sid"]
        put()
    bu = sids["profile"]
    if not sids.get("business"):
        sids["business"] = _tw("POST", f"{TRUSTHUB}/EndUsers", {
            "Type": "customer_profile_business_information", "FriendlyName": f"{name} business",
            "Attributes": json.dumps({
                "business_name": f["legal_name"], "business_identity": "direct_customer",
                "business_industry": "FOOD_AND_BEVERAGE", "business_regions_of_operation": "USA_AND_CANADA",
                "business_registration_identifier": "EIN", "business_registration_number": f["ein"],
                "business_type": f["business_type"], "website_url": f["website"]})})["sid"]
        put()
    _assign("CustomerProfiles", bu, sids["business"], sids, "business")
    if not sids.get("rep"):
        sids["rep"] = _tw("POST", f"{TRUSTHUB}/EndUsers", {
            "Type": "authorized_representative_1", "FriendlyName": f"{name} contact",
            "Attributes": json.dumps({
                "first_name": f["first_name"], "last_name": f["last_name"], "email": f["email"],
                "phone_number": "+1" + f["phone"], "business_title": f["title"],
                "job_position": f["job_position"]})})["sid"]
        put()
    _assign("CustomerProfiles", bu, sids["rep"], sids, "rep")
    if not sids.get("address"):
        data = {"FriendlyName": f"{name} address", "CustomerName": f["legal_name"], "Street": f["street"],
                "City": f["city"], "Region": f["state"], "PostalCode": f["zip"], "IsoCountry": "US"}
        if f.get("street2"):
            data["StreetSecondary"] = f["street2"]
        sids["address"] = _tw("POST", f"{API}/Accounts/{_sid}/Addresses.json", data)["sid"]
        put()
    if not sids.get("address_doc"):
        sids["address_doc"] = _tw("POST", f"{TRUSTHUB}/SupportingDocuments", {
            "FriendlyName": f"{name} address", "Type": "customer_profile_address",
            "Attributes": json.dumps({"address_sids": sids["address"]})})["sid"]
        put()
    _assign("CustomerProfiles", bu, sids["address_doc"], sids, "address_doc")
    _assign("CustomerProfiles", bu, primary, sids, "primary")
    put()
    if not sids.get("profile_submitted"):
        ev = _tw("POST", f"{TRUSTHUB}/CustomerProfiles/{bu}/Evaluations", {"PolicySid": SECONDARY_PROFILE_POLICY})
        if ev.get("status") != "compliant":
            raise TwilioStepError(_evaluation_problems(ev), fixable=True)
        _tw("POST", f"{TRUSTHUB}/CustomerProfiles/{bu}", {"Status": "pending-review"})
        sids["profile_submitted"] = True
        put()
    if not sids.get("a2p"):
        sids["a2p"] = _tw("POST", f"{TRUSTHUB}/TrustProducts", {
            "PolicySid": A2P_PROFILE_POLICY, "FriendlyName": f"{name} A2P", "Email": email})["sid"]
        put()
    if not sids.get("a2p_info"):
        sids["a2p_info"] = _tw("POST", f"{TRUSTHUB}/EndUsers", {
            "Type": "us_a2p_messaging_profile_information", "FriendlyName": f"{name} A2P info",
            "Attributes": json.dumps({"company_type": "private"})})["sid"]
        put()
    _assign("TrustProducts", sids["a2p"], sids["a2p_info"], sids, "a2p_info")
    _assign("TrustProducts", sids["a2p"], bu, sids, "a2p_profile")
    put()
    if not sids.get("a2p_submitted"):
        ev = _tw("POST", f"{TRUSTHUB}/TrustProducts/{sids['a2p']}/Evaluations", {"PolicySid": A2P_PROFILE_POLICY})
        if ev.get("status") != "compliant":
            raise TwilioStepError(_evaluation_problems(ev), fixable=True)
        _tw("POST", f"{TRUSTHUB}/TrustProducts/{sids['a2p']}", {"Status": "pending-review"})
        sids["a2p_submitted"] = True
        put()


def _evaluation_problems(ev) -> str:
    """Twilio's evaluation results as one sentence of what to fix."""
    out = []
    for res in (ev.get("results") or []):
        for f in (res.get("fields") or []):
            if not f.get("passed", True):
                out.append(str(f.get("friendly_name") or f.get("object_field") or "a field"))
    return ("Twilio's check flagged: " + ", ".join(dict.fromkeys(out))[:250] + ".") if out else \
        "Twilio's check did not pass these details."


def _service_and_number(rid, f, sids, put):
    """Steps 3-4: the brand, the Messaging Service and its number."""
    _sid, _tok, _p, _e = _env()
    import config
    name = _restaurant_name(rid)
    if not sids.get("brand"):
        sids["brand"] = _tw("POST", f"{MESSAGING}/a2p/BrandRegistrations", {
            "CustomerProfileBundleSid": sids["profile"], "A2PProfileBundleSid": sids["a2p"],
            "SkipAutomaticSecVet": "true"})["sid"]
        put()
    if not sids.get("service"):
        base = config.base_url()
        sids["service"] = _tw("POST", f"{MESSAGING}/Services", {
            "FriendlyName": f"{name} guest texts"[:64], "InboundRequestUrl": base + "/webhooks/twilio/sms",
            "StatusCallback": base + "/webhooks/twilio/status", "UseInboundWebhookOnNumber": "false"})["sid"]
        put()
    if not sids.get("number"):
        avail = []
        for params in ({"AreaCode": f.get("area_code")} if f.get("area_code") else None,
                       {"InPostalCode": f["zip"]}, {"InRegion": f["state"]}):
            if not params:
                continue
            avail = _tw("GET", f"{API}/Accounts/{_sid}/AvailablePhoneNumbers/US/Local.json",
                        params={**params, "SmsEnabled": "true", "PageSize": 5}).get("available_phone_numbers") or []
            if avail:
                break
        if not avail:
            raise TwilioStepError("No local number is free near the restaurant right now; it will try again.")
        bought = _tw("POST", f"{API}/Accounts/{_sid}/IncomingPhoneNumbers.json", {
            "PhoneNumber": avail[0]["phone_number"], "FriendlyName": f"{name} guest texts"[:64]})
        sids["number"], sids["phone"] = bought["sid"], bought["phone_number"]
        put(phone_number=bought["phone_number"])
    if not sids.get("number_in_service"):
        _tw("POST", f"{MESSAGING}/Services/{sids['service']}/PhoneNumbers", {"PhoneNumberSid": sids["number"]})
        sids["number_in_service"] = True
        put()


def campaign_fields(rid) -> dict:
    """The campaign Cavnar AI registers for a restaurant: what its guest
    texts are, how a guest opts in (the host-stand invite and the public
    join page, each confirmed by the guest's own Y), and real samples."""
    import config
    import guest_links
    from guest_marketing import confirmation_text
    name = _restaurant_name(rid)
    join = config.base_url() + "/join/" + guest_links.sign_join(rid)
    return {
        "UsAppToPersonUsecase": CAMPAIGN_USECASE,
        "Description": (f"{name} is a restaurant. It texts guests who opted in: a thank-you after a visit, "
                        "occasional offers and event news, and replies to guests who text the restaurant."),
        "MessageFlow": (f"Guests opt in two ways, and both end with the guest's own reply. (1) Online: the "
                        f"restaurant's public join page, {join}, where the guest enters a mobile number next to "
                        "the consent text (no box is pre-checked; joining is never required to dine). (2) At the "
                        "host stand: when a guest asks to join, staff enter the number and the guest is sent one "
                        "text asking them to reply Y. Either way the guest is texted nothing else until they "
                        f"reply Y. The confirmation reads: \"{confirmation_text(name)}\""),
        "MessageSamples": [
            f"{name}: Thanks for coming in last night! We hope to see you again soon. Reply STOP to opt out.",
            f"{name}: This weekend only - half-price appetizers 3-6pm. See you soon! Reply STOP to opt out.",
            f"{name}: Thanks for your message - we'll get back to you shortly. Reply STOP to opt out.",
        ],
        "HasEmbeddedLinks": "true",
        "HasEmbeddedPhone": "false",
        "OptInKeywords": ["Y", "YES", "JOIN", "START"],
        "OptInMessage": confirmation_text(name),
        "OptOutKeywords": ["STOP", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"],
        "OptOutMessage": f"{name}: You're unsubscribed and won't get more texts. Reply START to rejoin.",
        "HelpKeywords": ["HELP", "INFO"],
        "HelpMessage": f"{name}: Texts about visits and offers. Msg frequency varies. Msg & data rates may apply. "
                       "Reply STOP to cancel.",
        "SubscriberOptIn": "true",
        "PrivacyPolicyUrl": config.base_url() + "/privacy",
        "TermsAndConditionsUrl": config.base_url() + "/terms",
    }


def advance(rid, db_path=DB_PATH) -> dict:
    """Run every step this setup can take now; never raises. A Twilio
    refusal that the owner can fix (a field Twilio's check flagged) is
    'needs_fix' with Twilio's words; one the carriers made is 'failed'."""
    row = _row(rid, db_path)
    if not row or row.get("status") not in ("brand_review", "campaign_review"):
        return {"status": (row or {}).get("status")}
    if _live_refused():
        return {"status": row["status"], "skipped": "not the live server"}
    f, sids = row.get("form") or {}, dict(row.get("sids") or {})
    st = row["status"]

    def put(**cols):
        _save(rid, db_path, sids=sids, **cols)

    try:
        _profile_steps(rid, f, sids, put)
        _service_and_number(rid, f, sids, put)
        brand = _tw("GET", f"{MESSAGING}/a2p/BrandRegistrations/{sids['brand']}")
        bstat = str(brand.get("status") or "").upper()
        put(brand_status=bstat, checked_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        if bstat == "FAILED":
            why = brand.get("failure_reason") or "the carriers did not approve the business"
            put(status="failed", last_error=f"Brand not approved: {why}"[:400])
            return {"status": "failed"}
        if bstat != "APPROVED":
            return {"status": st}
        if sids.get("delete_campaign"):
            _tw("DELETE", f"{MESSAGING}/Services/{sids['service']}/Compliance/Usa2p/{sids['delete_campaign']}")
            sids.pop("delete_campaign")
            put()
        if not sids.get("campaign"):
            sids["campaign"] = _tw("POST", f"{MESSAGING}/Services/{sids['service']}/Compliance/Usa2p",
                                   {"BrandRegistrationSid": sids["brand"], **campaign_fields(rid)})["sid"]
            put(status="campaign_review")
            st = "campaign_review"
        camp = _tw("GET", f"{MESSAGING}/Services/{sids['service']}/Compliance/Usa2p/{sids['campaign']}")
        cstat = str(camp.get("campaign_status") or "").upper()
        put(campaign_status=cstat)
        if cstat == "FAILED":
            errs = "; ".join(str((e or {}).get("description") or e) for e in (camp.get("errors") or []))[:300]
            put(status="failed", last_error=("Texting program not approved: " + (errs or "no reason given")))
            return {"status": "failed"}
        if cstat == "VERIFIED":
            import guest_hub
            res = guest_hub.set_sender(rid, sids["service"], sids.get("phone"), db_path=db_path)
            if res.get("ok"):
                put(status="live", live_at=time.strftime("%Y-%m-%d %H:%M:%S"), last_error=None)
                _tell_owner_live(rid, res.get("number"))
                return {"status": "live"}
        return {"status": st}
    except TwilioStepError as e:
        if e.fixable and not sids.get("brand"):
            put(status="needs_fix", last_error=str(e))
            return {"status": "needs_fix", "error": str(e)}
        put(last_error=str(e))
        return {"status": st, "error": str(e)}
    except Exception as e:
        put(last_error=f"Will try again: {type(e).__name__}")
        try:
            import ops
            ops.capture(e, job="texting_setup", context=f"restaurant_id={rid}")
        except Exception:
            pass
        return {"status": st, "error": str(e)}


def _tell_owner_live(rid, number):
    """One email to the account holder: texting is live, from this number."""
    try:
        import emails
        import models
        r = models.get_restaurant(rid)
        to = getattr(r, "owner_email", None)
        if not to:
            return
        res = emails.send_texting_live_email(to, r.name, number, restaurant_id=rid)
        if not getattr(res, "ok", False):
            log.warning("texting_setup: live email not sent rid=%s", rid)
    except Exception as e:
        log.warning("texting_setup: live email failed rid=%s: %s", rid, e)


def run_advance() -> dict:
    """The hourly job: move every setup waiting on a carrier one step on.
    Bounded (ADVANCE_MAX, ADVANCE_SECONDS) and resumable — the oldest
    checked goes first, so a pass that stops early never starves the tail."""
    start = time.monotonic()
    conn = get_conn()
    try:
        rids = [r[0] for r in conn.execute(
            "SELECT restaurant_id FROM texting_setups WHERE status IN ('brand_review','campaign_review') "
            "ORDER BY COALESCE(checked_at, '') LIMIT ?", (ADVANCE_MAX,)).fetchall()]
    finally:
        conn.close()
    out = {"attempted": 0, "ok": 0, "failed": 0, "skipped": 0, "hit_bound": False}
    for rid in rids:
        if time.monotonic() - start > ADVANCE_SECONDS:
            out["hit_bound"] = True
            break
        out["attempted"] += 1
        res = advance(rid)
        if res.get("skipped"):
            out["skipped"] += 1
        elif res.get("error") or res.get("status") == "failed":
            out["failed"] += 1
        else:
            out["ok"] += 1
    return out
