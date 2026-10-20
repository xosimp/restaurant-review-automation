"""
guest_hub.py — the guest list from the host stand to the next-day text.

Owner, 10/19/26: at Simple EJ's the hosts take every party's details at the
stand, retype them into a spreadsheet, and Danny texts each new guest the
next day to thank them and ask for consent. This is that job inside
Cavnar AI, with consent moved to the stand:

  capture     Tock's webhooks (reservations, walk-ins, guest profiles —
              the URLs registered 10/13/26, docs/integrations/
              TOCK_WEBHOOKS.md), the host stand's own add, a spreadsheet
              import, and (phase 3) RPOWER checks with a customer attached.
              Every one lands in guest_contacts UNCONSENTED: a number given
              at the stand is a recognition key, never texting consent
              (project rule 10/7/26; the host-stand script promises "we
              don't blow up your phone").
  consent     only the guest's own yes: the host taps Invite to texts with
              the guest there and the guest replies Y (the join flow's
              double opt-in, guest_marketing.request_public_optin), or the
              guest joins from the QR card. An import may carry consent only
              where the owner attests the guest already replied yes, and
              that attestation is the consent event's own record.
  follow-up   the next-day thank-you (run_followups) to guests who visited
              yesterday AND hold marketing consent, under the same audience
              rule, sending hours and spacing as every campaign.
  replies     a text to the restaurant's OWN guest number that is not a
              keyword lands in guest_inbox for the owner to answer
              (handle_inbound_sms passes the number it was sent to).
  sender      guest_sms_senders: the restaurant's own registered Twilio
              messaging service and number (each client needs its own brand
              and campaign under Twilio's ISV rules); notify uses it for
              every guest text when set.

Tock had not confirmed its payload format when this was written, so every
event is kept raw (tock_events, 90 days) and read tolerantly; reprocess_tock
re-applies the stored events once the format is known.
"""
import csv
import hashlib
import hmac
import io
import json
import logging
import os
import re
import time
from datetime import date, datetime, timedelta

from models import DB_PATH

log = logging.getLogger(__name__)


def get_conn(db_path=None):
    """Resolved through models at call time (CLAUDE.md, bound imports): a
    patch of models.get_conn reaches every read here."""
    import models
    return models.get_conn(db_path) if db_path else models.get_conn()

TOCK_HOOKS = ("reservations", "walkins", "guests")
# "rid:token,rid:token" — one shared secret per restaurant Tock posts for.
TOCK_TOKENS_ENV = "TOCK_WEBHOOK_TOKENS"
TOCK_MAX_BODY = 512 * 1024
FOLLOWUP_HOUR = 11                  # local; the next-day thank-you goes out from here
FOLLOWUP_MAX_PER_PASS = 300
FOLLOWUP_PASS_SECONDS = 240
DEFAULT_FOLLOWUP = "Thanks for coming in yesterday! We'd love to see you again soon."
INBOX_REPLY_WINDOW_DAYS = 30        # a guest who texted in may be answered for this long
_PLACEHOLDER_CUSTOMERS = {"", "0", "1"}


# ── tables (created at boot: guest_marketing.init_guest_marketing) ──────────

def init_guest_hub(db_path=DB_PATH):
    conn = get_conn(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS tock_events (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id INTEGER NOT NULL REFERENCES restaurants(id),
            hook          TEXT    NOT NULL,
            event_key     TEXT    NOT NULL,
            event_type    TEXT,
            payload       TEXT    NOT NULL,
            received_at   TEXT    NOT NULL DEFAULT (datetime('now')),
            processed_at  TEXT,
            error         TEXT,
            UNIQUE(restaurant_id, hook, event_key)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tock_events_received ON tock_events(received_at)")
        conn.execute("""CREATE TABLE IF NOT EXISTS tock_bookings (
            restaurant_id  INTEGER NOT NULL REFERENCES restaurants(id),
            kind           TEXT    NOT NULL,
            tock_id        TEXT    NOT NULL,
            status         TEXT,
            party_size     INTEGER,
            booked_for     TEXT,
            business_date  TEXT,
            guest_ref      TEXT,
            guest_name     TEXT,
            phone          TEXT,
            email          TEXT,
            marketing_optin INTEGER,
            contact_id     INTEGER,
            visit_counted  INTEGER NOT NULL DEFAULT 0,
            created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at     TEXT    NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (restaurant_id, kind, tock_id)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tock_bookings_day ON tock_bookings(restaurant_id, business_date)")
        conn.execute("""CREATE TABLE IF NOT EXISTS guest_followup_settings (
            restaurant_id  INTEGER PRIMARY KEY REFERENCES restaurants(id),
            enabled        INTEGER NOT NULL DEFAULT 0,
            message        TEXT,
            review_link    INTEGER NOT NULL DEFAULT 0,
            updated_by     INTEGER,
            updated_at     TEXT    NOT NULL DEFAULT (datetime('now'))
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS guest_followups (
            restaurant_id  INTEGER NOT NULL REFERENCES restaurants(id),
            contact_id     INTEGER NOT NULL,
            visit_date     TEXT    NOT NULL,
            status         TEXT    NOT NULL,
            error          TEXT,
            sent_at        TEXT,
            created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (restaurant_id, contact_id, visit_date)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS guest_inbox (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id  INTEGER NOT NULL REFERENCES restaurants(id),
            phone          TEXT    NOT NULL,
            contact_id     INTEGER,
            direction      TEXT    NOT NULL,
            body           TEXT    NOT NULL,
            kind           TEXT,
            message_sid    TEXT,
            user_id        INTEGER,
            status         TEXT,
            at             TEXT    NOT NULL DEFAULT (datetime('now')),
            read_at        TEXT
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_guest_inbox_thread ON guest_inbox(restaurant_id, phone, at)")
        conn.execute("""CREATE TABLE IF NOT EXISTS guest_sms_senders (
            restaurant_id  INTEGER PRIMARY KEY REFERENCES restaurants(id),
            service_sid    TEXT    NOT NULL,
            phone_number   TEXT    NOT NULL,
            updated_at     TEXT    NOT NULL DEFAULT (datetime('now'))
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS guest_import_rows (
            restaurant_id  INTEGER NOT NULL REFERENCES restaurants(id),
            row_key        TEXT    NOT NULL,
            imported_at    TEXT    NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (restaurant_id, row_key)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS guest_pos_visits (
            restaurant_id  INTEGER NOT NULL REFERENCES restaurants(id),
            ticket_id      TEXT    NOT NULL,
            contact_id     INTEGER NOT NULL,
            business_date  TEXT    NOT NULL,
            net_sales      REAL,
            created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (restaurant_id, ticket_id)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_guest_pos_visits_contact ON guest_pos_visits(restaurant_id, contact_id)")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(guest_contacts)").fetchall()}
        for col, decl in (("tock_guest_id", "TEXT"), ("last_party_size", "INTEGER"),
                          ("tock_marketing_optin", "INTEGER"), ("first_seen_at", "TEXT")):
            if col not in cols:
                conn.execute(f"ALTER TABLE guest_contacts ADD COLUMN {col} {decl}")
        conn.commit()
    finally:
        conn.close()


# ── small helpers ───────────────────────────────────────────────────────────

def _phone(raw):
    """A US number as +1XXXXXXXXXX, or None when it isn't one."""
    digits = re.sub(r"\D", "", str(raw or ""))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10 or digits[0] in "01":
        return None
    return "+1" + digits


def phone_display(p):
    d = re.sub(r"\D", "", str(p or ""))[-10:]
    return f"({d[:3]}) {d[3:6]}-{d[6:]}" if len(d) == 10 else (p or "")


def _local_now(rid):
    from time_utils import restaurant_now_by_id
    return restaurant_now_by_id(rid, naive=True)


def _restaurant(rid, db_path=DB_PATH):
    import models
    return models.get_restaurant(rid) if db_path == DB_PATH else models.get_restaurant(rid, db_path)


def _record_visit(conn, rid, contact_id, when_iso):
    """One visit: the count goes up once, last_visit moves forward only."""
    conn.execute("UPDATE guest_contacts SET visit_count=COALESCE(visit_count,0)+1, "
                 "last_visit=CASE WHEN last_visit IS NULL OR last_visit < ? THEN ? ELSE last_visit END "
                 "WHERE id=? AND restaurant_id=?", (when_iso, when_iso, contact_id, rid))


def _touch_contact(rid, phone, name=None, email=None, source=None, party=None, tock_guest=None,
                   tock_optin=None, db_path=DB_PATH):
    """The guest's row (created unconsented if new) with what this source
    knows filled in where the row has nothing. Never consent."""
    import guest_marketing as gm
    cid = gm._upsert_contact(rid, phone, name=name, consent=False, db_path=db_path, source=source)
    conn = get_conn(db_path)
    try:
        conn.execute(
            "UPDATE guest_contacts SET email=COALESCE(email, ?), last_party_size=COALESCE(?, last_party_size), "
            "tock_guest_id=COALESCE(?, tock_guest_id), tock_marketing_optin=COALESCE(?, tock_marketing_optin), "
            "first_seen_at=COALESCE(first_seen_at, ?) WHERE id=? AND restaurant_id=?",
            ((email or "").strip().lower() or None, party, tock_guest,
             None if tock_optin is None else (1 if tock_optin else 0),
             _local_now(rid).isoformat(timespec="seconds"), cid, rid))
        conn.commit()
    finally:
        conn.close()
    return cid


# ── Tock ────────────────────────────────────────────────────────────────────

def _tock_tokens():
    out = []
    for part in str(os.getenv(TOCK_TOKENS_ENV, "") or "").split(","):
        rid, _, tok = part.strip().partition(":")
        if rid.strip().isdigit() and tok.strip():
            out.append((int(rid), tok.strip()))
    return out


def tock_restaurant_for(token):
    """The restaurant whose Tock secret this is (constant-time), or None."""
    token = str(token or "").strip()
    hit = None
    for rid, tok in _tock_tokens():
        if token and hmac.compare_digest(token.encode(), tok.encode()):
            hit = rid
    return hit


def _nk(k):
    return re.sub(r"[^a-z0-9]", "", str(k).lower())


def _get(d, *names):
    if not isinstance(d, dict):
        return None
    want = {_nk(n) for n in names}
    for k, v in d.items():
        if _nk(k) in want and v not in (None, "", [], {}):
            return v
    return None


def _find(obj, *names, depth=4):
    """The first value under any of `names`, breadth first, at most `depth`
    levels down."""
    want = {_nk(n) for n in names}
    level = [obj]
    for _ in range(depth):
        nxt = []
        for o in level:
            if isinstance(o, dict):
                for k, v in o.items():
                    if _nk(k) in want and v not in (None, "", [], {}) and not isinstance(v, (dict, list)):
                        return v
                nxt.extend(v for v in o.values() if isinstance(v, (dict, list)))
            elif isinstance(o, list):
                nxt.extend(x for x in o if isinstance(x, (dict, list)))
        level = nxt
    return None


def _events(payload):
    if isinstance(payload, list):
        return [e for e in payload if isinstance(e, dict)]
    if isinstance(payload, dict):
        for k in ("events", "items", "records"):
            v = payload.get(k)
            if isinstance(v, list):
                return [e for e in v if isinstance(e, dict)]
        return [payload]
    return []


def _body(event, hook):
    names = {"reservations": ("reservation", "booking"), "walkins": ("walkin", "walkIn", "walk_in"),
             "guests": ("guest", "patron", "customer", "profile")}[hook] + ("data", "payload", "object")
    for n in names:
        v = _get(event, n)
        if isinstance(v, dict):
            inner = _get(v, *names)
            return inner if isinstance(inner, dict) else v
    return event


def _guest_part(body):
    for n in ("guest", "patron", "customer", "diner", "profile", "contact"):
        v = _get(body, n)
        if isinstance(v, dict):
            return v
    return body


def _status(event, body):
    words = " ".join(str(x or "") for x in (_get(event, "type", "eventType", "event", "action", "topic"),
                                            _get(body, "status", "state", "reservationStatus"))).lower()
    if "cancel" in words:
        return "cancelled"
    if "no_show" in words or "noshow" in words or "no-show" in words or "no show" in words:
        return "no_show"
    if any(w in words for w in ("complete", "finish", "closed", "departed", "paid")):
        return "completed"
    if "seat" in words or "arriv" in words:
        return "seated"
    return "booked"


def _when(rid, body):
    """The booking's local wall time (naive), else now."""
    raw = _find(body, "dateTime", "reservationDateTime", "reservationTime", "startTime", "startDateTime",
                "bookedFor", "seatedAt", "arrivalTime", "time", "date")
    tz = None
    try:
        from time_utils import restaurant_tz
        tz = restaurant_tz(_restaurant(rid))
    except Exception:
        pass
    if raw:
        s = str(raw).strip().replace("Z", "+00:00")
        try:
            if re.fullmatch(r"\d{10,13}", s):
                n = int(s)
                dt = datetime.fromtimestamp(n / 1000 if n > 10 ** 11 else n, tz) if tz else None
            else:
                dt = datetime.fromisoformat(s)
            if dt is not None:
                if dt.tzinfo is not None and tz is not None:
                    dt = dt.astimezone(tz)
                return dt.replace(tzinfo=None)
        except (ValueError, OverflowError, OSError):
            pass
    return _local_now(rid)


def _int(v, lo=1, hi=200):
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return None
    return n if lo <= n <= hi else None


def _bool(v):
    if isinstance(v, bool):
        return v
    s = str(v or "").strip().lower()
    if s in ("true", "1", "yes", "y", "opted_in", "optedin", "subscribed"):
        return True
    if s in ("false", "0", "no", "n", "opted_out", "unsubscribed"):
        return False
    return None


def _event_key(event, hook, body):
    k = _get(event, "eventId", "event_id", "webhookId", "messageId", "deliveryId", "notificationId")
    if k:
        return str(k)[:200]
    return hashlib.sha256(json.dumps(event, sort_keys=True, default=str).encode()).hexdigest()


def receive_tock(hook, token, raw_body) -> tuple:
    """One Tock webhook delivery: ({ok, received, new}, status). 401 for an
    unknown secret, 400 for a body that isn't JSON. Each event is stored
    once (idempotent on Tock's event id, else on the event's own bytes) and
    applied; a failure to apply is kept on the event, never a 500 that makes
    Tock retry a payload this build can't read."""
    if hook not in TOCK_HOOKS:
        return {"ok": False, "error": "unknown webhook"}, 404
    rid = tock_restaurant_for(token)
    if rid is None:
        return {"ok": False, "error": "unauthorized"}, 401
    if raw_body is None or len(raw_body) > TOCK_MAX_BODY:
        return {"ok": False, "error": "body too large"}, 413
    try:
        payload = json.loads(raw_body.decode("utf-8") if isinstance(raw_body, bytes) else raw_body)
    except (ValueError, UnicodeDecodeError):
        return {"ok": False, "error": "not JSON"}, 400
    received = new = 0
    for event in _events(payload):
        received += 1
        body = _body(event, hook)
        key = _event_key(event, hook, body)
        etype = str(_get(event, "type", "eventType", "event", "action", "topic") or "")[:80] or None
        conn = get_conn()
        try:
            cur = conn.execute("INSERT OR IGNORE INTO tock_events (restaurant_id, hook, event_key, event_type, payload) "
                               "VALUES (?,?,?,?,?)", (rid, hook, key, etype, json.dumps(event, default=str)))
            conn.commit()
            fresh = bool(cur.rowcount)
        finally:
            conn.close()
        if not fresh:
            continue
        new += 1
        _apply_logged(rid, hook, key, event)
    return {"ok": True, "received": received, "new": new}, 200


def _apply_logged(rid, hook, key, event, db_path=DB_PATH):
    err = None
    try:
        apply_tock_event(rid, hook, event, db_path=db_path)
    except Exception as e:                    # kept on the event; reprocess_tock retries it
        err = f"{type(e).__name__}: {e}"[:400]
        log.warning("tock event not applied rid=%s hook=%s: %s", rid, hook, err)
    conn = get_conn(db_path)
    try:
        conn.execute("UPDATE tock_events SET processed_at=datetime('now'), error=? WHERE restaurant_id=? AND hook=? "
                     "AND event_key=?", (err, rid, hook, key))
        conn.commit()
    finally:
        conn.close()


def apply_tock_event(rid, hook, event, db_path=DB_PATH) -> dict:
    """What one Tock event says, applied: the guest (unconsented), the
    booking, a visit when the party was seated or finished, and the night's
    booked covers for the forecast."""
    body = _body(event, hook)
    g = _guest_part(body)
    first, last = _get(g, "firstName", "first_name", "givenName"), _get(g, "lastName", "last_name", "familyName")
    name = " ".join(str(x).strip() for x in (first, last) if x) or _get(g, "name", "fullName", "full_name",
                                                                         "guestName", "displayName")
    name = " ".join(str(name or "").split())[:80] or None
    phone = _phone(_find(g, "phone", "phoneNumber", "mobile", "mobilePhone", "cell", "cellPhone") or
                   _find(body, "phone", "phoneNumber", "mobile"))
    email = _find(g, "email", "emailAddress")
    optin = _bool(_find(g, "marketingOptIn", "optInMarketing", "acceptsMarketing", "marketingConsent",
                        "smsOptIn", "emailOptIn", "optIn"))
    guest_ref = _get(g, "guestId", "patronId", "customerId") or (_get(g, "id") if g is not body else None)
    guest_ref = str(guest_ref)[:80] if guest_ref else None
    if hook == "guests":
        if not phone:
            return {"applied": False, "why": "no phone"}
        cid = _touch_contact(rid, phone, name=name, email=email, source="tock", tock_guest=guest_ref,
                             tock_optin=optin, db_path=db_path)
        return {"applied": True, "contact_id": cid}
    kind = "reservation" if hook == "reservations" else "walkin"
    tock_id = _get(body, "reservationId", "walkInId", "walkinId", "bookingId", "confirmationNumber", "id") \
        or _find(body, "reservationId", "walkInId", "bookingId", "confirmationNumber")
    tock_id = str(tock_id or _event_key(event, hook, body))[:120]
    status = _status(event, body)
    when = _when(rid, body)
    from time_utils import BUSINESS_DAY_START_HOUR
    bday = (when.date() - timedelta(days=1)) if when.hour < BUSINESS_DAY_START_HOUR else when.date()
    party = _int(_find(body, "partySize", "party_size", "size", "guestCount", "numberOfGuests", "covers",
                       "seats", "guests"))
    cid = _touch_contact(rid, phone, name=name, email=email, source="tock", party=party, tock_guest=guest_ref,
                         tock_optin=optin, db_path=db_path) if phone else None
    conn = get_conn(db_path)
    try:
        prev = conn.execute("SELECT visit_counted, business_date FROM tock_bookings WHERE restaurant_id=? AND kind=? "
                            "AND tock_id=?", (rid, kind, tock_id)).fetchone()
        conn.execute(
            "INSERT INTO tock_bookings (restaurant_id, kind, tock_id, status, party_size, booked_for, business_date, "
            "guest_ref, guest_name, phone, email, marketing_optin, contact_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(restaurant_id, kind, tock_id) DO UPDATE SET status=excluded.status, "
            "party_size=COALESCE(excluded.party_size, tock_bookings.party_size), booked_for=excluded.booked_for, "
            "business_date=excluded.business_date, guest_ref=COALESCE(excluded.guest_ref, tock_bookings.guest_ref), "
            "guest_name=COALESCE(excluded.guest_name, tock_bookings.guest_name), "
            "phone=COALESCE(excluded.phone, tock_bookings.phone), email=COALESCE(excluded.email, tock_bookings.email), "
            "marketing_optin=COALESCE(excluded.marketing_optin, tock_bookings.marketing_optin), "
            "contact_id=COALESCE(excluded.contact_id, tock_bookings.contact_id), updated_at=datetime('now')",
            (rid, kind, tock_id, status, party, when.isoformat(timespec="seconds"), bday.isoformat(), guest_ref, name,
             phone, (str(email).strip().lower() if email else None), None if optin is None else int(optin), cid))
        visited = False
        if status in ("seated", "completed") and cid and not (prev and prev["visit_counted"]):
            _record_visit(conn, rid, cid, when.isoformat(timespec="seconds"))
            conn.execute("UPDATE tock_bookings SET visit_counted=1 WHERE restaurant_id=? AND kind=? AND tock_id=?",
                         (rid, kind, tock_id))
            visited = True
        conn.commit()
    finally:
        conn.close()
    days = {bday.isoformat()} | ({prev["business_date"]} if prev and prev["business_date"] else set())
    if kind == "reservation":
        for d in days:
            _sync_covers(rid, d, db_path=db_path)
    return {"applied": True, "kind": kind, "status": status, "contact_id": cid, "visit": visited}


def _sync_covers(rid, day, db_path=DB_PATH):
    """The night's booked covers from Tock, as the forecast's reservation
    signal (demand_signals, label "Reservations" — the same row an imported
    reservation report writes, so the two never add up)."""
    conn = get_conn(db_path)
    try:
        covers = conn.execute("SELECT COALESCE(SUM(party_size), 0) FROM tock_bookings WHERE restaurant_id=? "
                              "AND kind='reservation' AND business_date=? AND status NOT IN ('cancelled','no_show')",
                              (rid, day)).fetchone()[0]
    finally:
        conn.close()
    import demand_signals
    demand_signals.save(rid, [{"date": day, "kind": "reservations", "covers": int(covers or 0)}], source="tock",
                        created_by="Tock", db_path=db_path)


def reprocess_tock(rid, db_path=DB_PATH) -> dict:
    """Re-apply every stored event in arrival order — the step once Tock's
    payload format is confirmed and the readers above are adjusted."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT hook, event_key, payload FROM tock_events WHERE restaurant_id=? ORDER BY id",
                            (rid,)).fetchall()
    finally:
        conn.close()
    ok = failed = 0
    for r in rows:
        try:
            _apply_logged(rid, r["hook"], r["event_key"], json.loads(r["payload"]), db_path=db_path)
            ok += 1
        except Exception:
            failed += 1
    return {"events": len(rows), "ok": ok, "failed": failed}


def tock_status(rid, db_path=DB_PATH) -> dict:
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT COUNT(*) n, MAX(received_at) last, SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) "
                         "bad FROM tock_events WHERE restaurant_id=?", (rid,)).fetchone()
    finally:
        conn.close()
    from time_utils import mdy
    return {"connected": any(x == rid for x, _ in _tock_tokens()), "events": int(r["n"] or 0),
            "last_event": mdy(str(r["last"] or "")[:10]) if r["last"] else None, "unread": int(r["bad"] or 0)}


# ── the guest list ──────────────────────────────────────────────────────────

SOURCE_LABELS = {"tock": "Tock", "host_stand": "Host stand", "import": "Spreadsheet", "pos": "RPOWER check",
                 "join": "Joined online", "manual": "Added by hand", "toast_invite": "Toast", "explicit_optin": "Joined",
                 "sms_reply": "Text reply"}


def _texts_state(row, pending):
    if row["unsubscribed"] or row["stopped"]:
        return "stopped"
    if row["consent"]:
        return "joined"
    if row["phone"] in pending:
        return "waiting"
    return "not_invited"


def guest_list(rid, view="all", q="", limit=300, db_path=DB_PATH) -> dict:
    """Danny's list: each guest, their visits and their texts state
    (joined / waiting for their Y / not invited / stopped), with the counts
    the screen leads with. `view`: all | yesterday | joined | waiting."""
    import guest_marketing as gm
    now = _local_now(rid)
    yday = (now.date() - timedelta(days=1)).isoformat()
    since = (datetime.utcnow() - timedelta(hours=gm.JOIN_CONFIRM_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_conn(db_path)
    try:
        pending = {r[0] for r in conn.execute(
            "SELECT DISTINCT phone FROM guest_optin_requests WHERE restaurant_id=? AND status='pending' "
            "AND requested_at >= ?", (rid, since)).fetchall()}
        rows = [dict(r) for r in conn.execute(
            "SELECT g.id, g.name, g.phone, g.email, g.consent, g.unsubscribed, g.last_visit, g.visit_count, g.source, "
            "g.last_party_size, g.tock_marketing_optin, g.created_at, "
            "CASE WHEN EXISTS (SELECT 1 FROM guest_sms_optouts o WHERE o.phone=g.phone AND o.restaurant_id IN (0, "
            "g.restaurant_id)) THEN 1 ELSE 0 END AS stopped FROM guest_contacts g WHERE g.restaurant_id=? "
            "ORDER BY COALESCE(g.last_visit, g.created_at) DESC", (rid,)).fetchall()]
        unread = conn.execute("SELECT COUNT(*) FROM guest_inbox WHERE restaurant_id=? AND direction='in' "
                              "AND read_at IS NULL", (rid,)).fetchone()[0]
    finally:
        conn.close()
    for r in rows:
        r["texts"] = _texts_state(r, pending)
        r["visited_yesterday"] = str(r.get("last_visit") or "")[:10] == yday
    counts = {"total": len(rows), "yesterday": sum(1 for r in rows if r["visited_yesterday"]),
              "joined": sum(1 for r in rows if r["texts"] == "joined"),
              "waiting": sum(1 for r in rows if r["texts"] == "waiting"), "unread": int(unread or 0)}
    if view == "yesterday":
        rows = [r for r in rows if r["visited_yesterday"]]
    elif view in ("joined", "waiting"):
        rows = [r for r in rows if r["texts"] == view]
    q = (q or "").strip().lower()
    if q:
        digits = re.sub(r"\D", "", q)
        rows = [r for r in rows if q in (r.get("name") or "").lower() or (digits and digits in (r.get("phone") or ""))
                or q in (r.get("email") or "").lower()]
    shown = rows[:max(1, min(int(limit or 300), 1000))]
    usual = usual_items(rid, [r["id"] for r in shown], db_path=db_path)
    from time_utils import mdy
    out = []
    for r in shown:
        out.append({"id": r["id"], "name": r.get("name") or "", "phone": phone_display(r["phone"]),
                    "email": r.get("email") or "", "last_visit": mdy(str(r["last_visit"])[:10]) if r.get("last_visit")
                    else None, "visits": int(r.get("visit_count") or 0), "party": r.get("last_party_size"),
                    "source": SOURCE_LABELS.get(r.get("source") or "", "Added"), "texts": r["texts"],
                    "tock_optin": r.get("tock_marketing_optin"), "usual": usual.get(r["id"]) or []})
    return {"ok": True, "counts": counts, "guests": out, "more": max(0, len(rows) - len(shown)), "view": view}


def host_add(rid, name, phone, party=None, email=None, invite=False, asked=False, user_id=None,
             db_path=DB_PATH) -> dict:
    """The host stand's add: the guest saved (unconsented) with a visit now,
    and — only when the guest asked for texts there and then (`asked`, the
    host's own confirmation) — the reply-Y invite. The invite is the join
    flow's double opt-in: nothing is consent until the guest's own Y."""
    p = _phone(phone)
    if not p:
        return {"ok": False, "error": "Enter a 10-digit phone number."}
    if invite and not asked:
        return {"ok": False, "error": "Only send the invite when the guest asks for texts."}
    party = _int(party) if party not in (None, "") else None
    cid = _touch_contact(rid, p, name=(name or "").strip()[:80] or None, email=email, source="host_stand",
                         party=party, db_path=db_path)
    conn = get_conn(db_path)
    try:
        _record_visit(conn, rid, cid, _local_now(rid).isoformat(timespec="seconds"))
        conn.commit()
    finally:
        conn.close()
    out = {"ok": True, "contact_id": cid, "invited": False}
    if invite:
        import guest_marketing as gm
        res = gm.request_public_optin(rid, p, name=(name or "").strip()[:80] or None, ip=None,
                                      user_agent="host stand", db_path=db_path, source="host_stand",
                                      detail=f"host stand, user {user_id}" if user_id else "host stand")
        out["invited"] = bool(res.get("ok"))
        if not res.get("ok"):
            out["invite_error"] = res.get("error")
    return out


# ── the spreadsheet import ──────────────────────────────────────────────────

_COLS = {
    "name": ("name", "guest", "guestname", "fullname", "customer", "customername"),
    "first": ("first", "firstname", "fname"),
    "last": ("last", "lastname", "lname", "surname"),
    "phone": ("phone", "phonenumber", "cell", "cellphone", "mobile", "number", "tel", "telephone"),
    "email": ("email", "emailaddress", "mail"),
    "date": ("date", "visit", "visitdate", "visited", "lastvisit", "datevisited", "day"),
    "consent": ("consent", "optin", "optedin", "repliedyes", "yes", "texts", "subscribed", "replied", "sms"),
}
_YES = {"yes", "y", "true", "1", "x", "opted in", "optedin", "replied yes", "subscribed", "✓", "✔", "ok", "okay"}
# A header that isn't one of the exact names above is matched by what it
# contains, in this order, so "Last Visit" is a date before it can be a last
# name and "Cell #" is a phone (owner's sheet, 10/20/26: headers are whatever
# the hosts typed).
_CONTAINS = (
    ("phone", ("phone", "cell", "mobile", "tel")),
    ("email", ("email", "mail")),
    ("date", ("date", "visit", "came", "dined")),
    ("consent", ("consent", "optin", "opted", "replied", "subscrib", "textok", "oktotext", "smsok", "texts",
                 "textme", "agree")),
    ("first", ("firstname", "first")),
    ("last", ("lastname", "surname")),
    ("name", ("name", "guest", "customer")),
)


def _parse_date(s):
    s = str(s or "").strip()
    for fmt in ("%m/%d/%y", "%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%m-%d-%y", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def xlsx_to_csv(data: bytes) -> str:
    """The first sheet of an Excel workbook (.xlsx — Excel, Google Sheets,
    Numbers all export it) as CSV text, read with the standard library: a
    sheet is zipped XML. Shared and inline strings, numbers, and a date
    column's serial numbers as M/D/YYYY. ValueError for anything else."""
    import zipfile
    import xml.etree.ElementTree as ET
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    rel_ns = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError("That isn't an Excel file (.xlsx).")
    names = set(z.namelist())
    shared = []
    if "xl/sharedStrings.xml" in names:
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", ns):
            shared.append("".join(t.text or "" for t in si.iter("{%s}t" % ns["m"])))
    sheet = "xl/worksheets/sheet1.xml"
    try:
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        first = wb.find("m:sheets/m:sheet", ns)
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        rid = first.get(rel_ns) if first is not None else None
        for r in rels:
            if r.get("Id") == rid:
                t = r.get("Target").lstrip("/")
                sheet = t if t.startswith("xl/") else "xl/" + t
    except (KeyError, ET.ParseError, AttributeError):
        pass
    if sheet not in names:
        raise ValueError("That workbook has no sheet to read.")
    def col_index(ref):
        n = 0
        for ch in re.match(r"[A-Z]+", ref).group(0):
            n = n * 26 + ord(ch) - 64
        return n - 1
    rows = []
    for row in ET.fromstring(z.read(sheet)).iter("{%s}row" % ns["m"]):
        vals = {}
        for c in row.findall("m:c", ns):
            t, ref = c.get("t"), c.get("r") or ""
            v = c.find("m:v", ns)
            if t == "s" and v is not None:
                val = shared[int(v.text)] if v.text and int(v.text) < len(shared) else ""
            elif t == "inlineStr":
                val = "".join(x.text or "" for x in c.iter("{%s}t" % ns["m"]))
            else:
                val = v.text if v is not None and v.text is not None else ""
            if ref:
                vals[col_index(ref)] = val
        if vals:
            rows.append([vals.get(i, "") for i in range(max(vals) + 1)])
    if not rows:
        raise ValueError("The first sheet is empty.")
    # A date column holds Excel serial numbers (days since 12/30/1899).
    head = [_nk(h) for h in rows[0]]
    date_cols = [i for i, k in enumerate(head) if any(b in k for b in ("date", "visit", "came", "dined"))]
    for r in rows[1:]:
        for i in date_cols:
            if i < len(r) and re.fullmatch(r"\d{5}(\.\d+)?", str(r[i]).strip()):
                d = date(1899, 12, 30) + timedelta(days=int(float(r[i])))
                r[i] = f"{d.month}/{d.day}/{d.year}"
    # A phone stored as a number comes back as 6305550101 or 6.305550101E9.
    for r in rows[1:]:
        for i, v in enumerate(r):
            if re.fullmatch(r"\d(\.\d+)?E\+?\d+", str(v)):
                r[i] = str(int(float(v)))
    out = io.StringIO()
    csv.writer(out).writerows(rows)
    return out.getvalue()


def parse_import(csv_text) -> dict:
    """{columns, rows: [{name, phone, email, date, consent}], skipped}: a
    spreadsheet read by its headers, whatever order or wording."""
    text = (csv_text or "").lstrip("﻿")
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        return {"columns": {}, "rows": [], "skipped": 0, "error": "The file is empty."}
    cols = {}
    for i, h in enumerate(header):
        k = _nk(h)
        for field, names in _COLS.items():
            if field not in cols and k in names:
                cols[field] = i
    taken = set(cols.values())
    for i, h in enumerate(header):
        k = _nk(h)
        if i in taken or not k:
            continue
        for field, bits in _CONTAINS:
            if field not in cols and any(b in k for b in bits):
                cols[field] = i
                taken.add(i)
                break
    if "phone" not in cols:
        return {"columns": cols, "rows": [], "skipped": 0,
                "error": "No phone column found. Name a column Phone (or Cell, Mobile, Number)."}
    rows, skipped = [], 0
    for rec in reader:
        def cell(f):
            i = cols.get(f)
            return rec[i].strip() if i is not None and i < len(rec) else ""
        p = _phone(cell("phone"))
        if not p:
            skipped += 1
            continue
        name = cell("name") or " ".join(x for x in (cell("first"), cell("last")) if x)
        rows.append({"name": name[:80] or None, "phone": p, "email": cell("email") or None,
                     "date": (_parse_date(cell("date")) or None),
                     "consent": cell("consent").lower() in _YES if "consent" in cols else False})
    return {"columns": cols, "rows": rows, "skipped": skipped, "error": None}


def import_guests(rid, csv_text, commit=False, attest_consent=False, user_id=None, db_path=DB_PATH) -> dict:
    """Preview (commit False) or import a spreadsheet of guests. Every row
    is a guest; a dated row is one visit, counted once however often the
    file is imported. A row marked as having said yes is imported with
    marketing consent ONLY with `attest_consent` — the owner's statement
    that this guest already replied yes to the restaurant's texts — and that
    statement, who made it and when, is the consent event's record."""
    parsed = parse_import(csv_text)
    if parsed.get("error"):
        return {"ok": False, "error": parsed["error"]}
    rows = parsed["rows"]
    said_yes = sum(1 for r in rows if r["consent"])
    preview = {"ok": True, "rows": len(rows), "skipped": parsed["skipped"], "said_yes": said_yes,
               "columns": [f for f, _i in sorted(parsed["columns"].items(), key=lambda kv: kv[1])],
               "sample": [{"name": r["name"] or "", "phone": phone_display(r["phone"]),
                           "date": r["date"].isoformat() if r["date"] else None, "consent": r["consent"]}
                          for r in rows[:5]]}
    if not commit:
        return preview
    import guest_marketing as gm
    added = visits = consented = 0
    for r in rows:
        cid = _touch_contact(rid, r["phone"], name=r["name"], email=r["email"], source="import", db_path=db_path)
        added += 1
        if r["date"]:
            key = hashlib.sha256(f"{r['phone']}|{r['date'].isoformat()}".encode()).hexdigest()[:32]
            conn = get_conn(db_path)
            try:
                cur = conn.execute("INSERT OR IGNORE INTO guest_import_rows (restaurant_id, row_key) VALUES (?,?)",
                                   (rid, key))
                if cur.rowcount:
                    _record_visit(conn, rid, cid, r["date"].isoformat() + "T12:00:00")
                    visits += 1
                conn.commit()
            finally:
                conn.close()
        if r["consent"] and attest_consent:
            gm.add_guest_contact_public_optin(rid, r["phone"], name=r["name"], db_path=db_path,
                                              source="import_attested",
                                              detail=(f"owner attested this guest already replied yes to the "
                                                      f"restaurant's texts (user {user_id})"))
            consented += 1
    return dict(preview, imported=added, visits=visits, consented=consented)


# ── the next-day thank-you ──────────────────────────────────────────────────

def followup_settings(rid, db_path=DB_PATH) -> dict:
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT enabled, message, review_link, updated_at FROM guest_followup_settings "
                         "WHERE restaurant_id=?", (rid,)).fetchone()
    finally:
        conn.close()
    restaurant = _restaurant(rid, db_path)
    import guest_marketing as gm
    return {"enabled": bool(r and r["enabled"]), "message": (r["message"] if r and r["message"] else DEFAULT_FOLLOWUP),
            "prefix": gm.sms_prefix(getattr(restaurant, "name", "") or ""),
            "review_link": bool(r and r["review_link"]), "hour": FOLLOWUP_HOUR,
            "can_review_link": bool(getattr(restaurant, "google_place_id", None)),
            "preview": followup_text(restaurant, (r["message"] if r and r["message"] else DEFAULT_FOLLOWUP),
                                     bool(r and r["review_link"]))}


def followup_text(restaurant, message, review_link=False) -> str:
    import guest_marketing as gm
    body = gm.campaign_text(getattr(restaurant, "name", "") or "", message)
    if review_link and getattr(restaurant, "google_place_id", None):
        body += "\nReview us: " + gm._google_review_link(restaurant.google_place_id)
    return body + gm.STOP_LINE


def set_followup(rid, enabled, message, review_link=False, user_id=None, db_path=DB_PATH) -> dict:
    import guest_marketing as gm
    msg = " ".join(str(message or "").split()) or DEFAULT_FOLLOWUP
    bad = gm.check_campaign_text(rid, msg, db_path=db_path)
    if bad:
        return {"ok": False, "error": bad["error"]}
    conn = get_conn(db_path)
    try:
        conn.execute("INSERT INTO guest_followup_settings (restaurant_id, enabled, message, review_link, updated_by, "
                     "updated_at) VALUES (?,?,?,?,?,datetime('now')) ON CONFLICT(restaurant_id) DO UPDATE SET "
                     "enabled=excluded.enabled, message=excluded.message, review_link=excluded.review_link, "
                     "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                     (rid, 1 if enabled else 0, msg, 1 if review_link else 0, user_id))
        conn.commit()
    finally:
        conn.close()
    return dict(followup_settings(rid, db_path=db_path), ok=True)


def _followup_candidates(conn, rid, yday, limit):
    import guest_marketing as gm
    spacing = (_local_now(rid) - timedelta(days=gm.GUEST_SMS_MIN_DAYS_BETWEEN)).isoformat(timespec="seconds")
    return conn.execute(
        "SELECT g.id, g.phone, g.name FROM guest_contacts g WHERE g.restaurant_id=? AND substr(g.last_visit,1,10)=? "
        "AND " + gm.marketing_text_sql("g") + " AND (g.last_campaign_at IS NULL OR g.last_campaign_at < ?) "
        "AND NOT EXISTS (SELECT 1 FROM guest_followups f WHERE f.restaurant_id=g.restaurant_id "
        "AND f.contact_id=g.id AND f.visit_date=?) ORDER BY g.id LIMIT ?",
        (rid, yday, spacing, yday, limit)).fetchall()


def followup_one(rid, now=None, db_path=DB_PATH, deadline=None) -> dict:
    """Thank yesterday's opted-in guests at one restaurant, once each (the
    guest_followups row is claimed before the send). Nothing before
    FOLLOWUP_HOUR or outside the guest sending hours."""
    import guest_marketing as gm
    import notify
    settings = followup_settings(rid, db_path=db_path)
    if not settings["enabled"]:
        return {"attempted": 0, "ok": 0, "failed": 0, "skipped": 1}
    local = now or _local_now(rid)
    if local.hour < FOLLOWUP_HOUR or not gm.guest_sms_allowed_now(rid):
        return {"attempted": 0, "ok": 0, "failed": 0, "skipped": 1}
    yday = (local.date() - timedelta(days=1)).isoformat()
    restaurant = _restaurant(rid, db_path)
    body = followup_text(restaurant, settings["message"], settings["review_link"])
    conn = get_conn(db_path)
    try:
        rows = _followup_candidates(conn, rid, yday, FOLLOWUP_MAX_PER_PASS)
    finally:
        conn.close()
    sent = failed = 0
    for g in rows:
        if deadline and time.monotonic() > deadline:
            break
        conn = get_conn(db_path)
        try:
            cur = conn.execute("INSERT OR IGNORE INTO guest_followups (restaurant_id, contact_id, visit_date, status) "
                               "VALUES (?,?,?,'sending')", (rid, g["id"], yday))
            conn.commit()
            claimed = bool(cur.rowcount)
        finally:
            conn.close()
        if not claimed:
            continue
        with notify.sms_context(rid):
            ok = bool(gm.send_sms(g["phone"], body, use_case="guest",
                                  validity_seconds=gm.guest_sms_validity_seconds(rid)))
        stamp = _local_now(rid).isoformat(timespec="seconds")
        conn = get_conn(db_path)
        try:
            conn.execute("UPDATE guest_followups SET status=?, sent_at=?, error=? WHERE restaurant_id=? "
                         "AND contact_id=? AND visit_date=?", ("sent" if ok else "failed", stamp if ok else None,
                                                              None if ok else "not sent", rid, g["id"], yday))
            if ok:
                conn.execute("UPDATE guest_contacts SET last_campaign_at=? WHERE id=? AND restaurant_id=?", (stamp, g["id"], rid))
                conn.execute("INSERT INTO guest_inbox (restaurant_id, phone, contact_id, direction, body, kind, status) "
                             "VALUES (?,?,?,'out',?,'followup','sent')", (rid, g["phone"], g["id"], body))
            conn.commit()
        finally:
            conn.close()
        sent += 1 if ok else 0
        failed += 0 if ok else 1
    return {"attempted": sent + failed, "ok": sent, "failed": failed, "skipped": 0}


def run_followups(db_path=DB_PATH, now=None) -> dict:
    """The scheduled pass: every restaurant with the thank-you on. Bounded
    by FOLLOWUP_PASS_SECONDS; a restaurant cut short is finished by the next
    pass (its claimed rows are skipped, its unclaimed ones remain)."""
    conn = get_conn(db_path)
    try:
        rids = [r[0] for r in conn.execute("SELECT restaurant_id FROM guest_followup_settings WHERE enabled=1 "
                                           "ORDER BY restaurant_id").fetchall()]
    finally:
        conn.close()
    deadline = time.monotonic() + FOLLOWUP_PASS_SECONDS
    tot = {"attempted": 0, "ok": 0, "failed": 0, "skipped": 0, "hit_bound": False}
    for rid in rids:
        if time.monotonic() > deadline:
            tot["hit_bound"] = True
            break
        try:
            res = followup_one(rid, now=now, db_path=db_path, deadline=deadline)
        except Exception as e:
            import ops
            ops.capture(e, job="guest_followups", context=f"restaurant_id={rid}")
            tot["failed"] += 1
            continue
        for k in ("attempted", "ok", "failed", "skipped"):
            tot[k] += res.get(k, 0)
    return tot


# ── the restaurant's own number and its replies ────────────────────────────

_SENDER_CACHE = {"at": 0.0, "rows": {}}


def _senders(db_path=DB_PATH):
    if db_path == DB_PATH and time.monotonic() - _SENDER_CACHE["at"] < 60:
        return _SENDER_CACHE["rows"]
    conn = get_conn(db_path)
    try:
        rows = {r["restaurant_id"]: {"service_sid": r["service_sid"], "number": r["phone_number"]}
                for r in conn.execute("SELECT restaurant_id, service_sid, phone_number FROM guest_sms_senders").fetchall()}
    except Exception:
        rows = {}
    finally:
        conn.close()
    if db_path == DB_PATH:
        _SENDER_CACHE.update(at=time.monotonic(), rows=rows)
    return rows


def sender_for(rid, db_path=DB_PATH):
    """{service_sid, number} of this restaurant's own guest texting, or None."""
    return _senders(db_path).get(rid) if rid else None


def restaurant_for_number(to_phone, db_path=DB_PATH):
    p = _phone(to_phone)
    for rid, s in _senders(db_path).items():
        if p and _phone(s["number"]) == p:
            return rid
    return None


def set_sender(rid, service_sid, phone_number, db_path=DB_PATH) -> dict:
    sid = str(service_sid or "").strip()
    p = _phone(phone_number)
    if not re.fullmatch(r"MG[0-9a-fA-F]{32}", sid) or not p:
        return {"ok": False, "error": "A Messaging Service SID (MG…) and a 10-digit number are both needed."}
    conn = get_conn(db_path)
    try:
        conn.execute("INSERT INTO guest_sms_senders (restaurant_id, service_sid, phone_number, updated_at) VALUES "
                     "(?,?,?,datetime('now')) ON CONFLICT(restaurant_id) DO UPDATE SET service_sid=excluded.service_sid, "
                     "phone_number=excluded.phone_number, updated_at=excluded.updated_at", (rid, sid, p))
        conn.commit()
    finally:
        conn.close()
    _SENDER_CACHE["at"] = 0.0
    return {"ok": True, "number": phone_display(p)}


def log_inbound(rid, phone, body, message_sid=None, db_path=DB_PATH):
    """A guest's own text to the restaurant's number, kept for the owner."""
    p = _phone(phone) or phone
    conn = get_conn(db_path)
    try:
        c = conn.execute("SELECT id FROM guest_contacts WHERE restaurant_id=? AND phone=?", (rid, p)).fetchone()
        conn.execute("INSERT INTO guest_inbox (restaurant_id, phone, contact_id, direction, body, kind, message_sid) "
                     "VALUES (?,?,?,'in',?,'reply',?)", (rid, p, c["id"] if c else None, str(body or "")[:1600],
                                                          message_sid))
        conn.commit()
    finally:
        conn.close()


def inbox_threads(rid, db_path=DB_PATH) -> list:
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT i.phone, MAX(i.at) last_at, SUM(CASE WHEN i.direction='in' AND i.read_at IS NULL THEN 1 ELSE 0 END) "
            "unread, MAX(g.name) name FROM guest_inbox i LEFT JOIN guest_contacts g ON g.restaurant_id=i.restaurant_id "
            "AND g.phone=i.phone WHERE i.restaurant_id=? AND EXISTS (SELECT 1 FROM guest_inbox x WHERE "
            "x.restaurant_id=i.restaurant_id AND x.phone=i.phone AND x.direction='in') GROUP BY i.phone "
            "ORDER BY last_at DESC LIMIT 100", (rid,)).fetchall()
        out = []
        for r in rows:
            last = conn.execute("SELECT body, direction FROM guest_inbox WHERE restaurant_id=? AND phone=? "
                                "ORDER BY at DESC, id DESC LIMIT 1", (rid, r["phone"])).fetchone()
            out.append({"phone": r["phone"], "display": phone_display(r["phone"]), "name": r["name"] or "",
                        "unread": int(r["unread"] or 0), "last_at": r["last_at"],
                        "last": (("You: " if last["direction"] == "out" else "") + last["body"])[:140] if last else ""})
        return out
    finally:
        conn.close()


def thread(rid, phone, db_path=DB_PATH) -> list:
    p = _phone(phone) or phone
    conn = get_conn(db_path)
    try:
        rows = [dict(r) for r in conn.execute("SELECT direction, body, kind, at, status FROM guest_inbox WHERE "
                                               "restaurant_id=? AND phone=? ORDER BY at, id", (rid, p)).fetchall()]
        conn.execute("UPDATE guest_inbox SET read_at=datetime('now') WHERE restaurant_id=? AND phone=? AND "
                     "direction='in' AND read_at IS NULL", (rid, p))
        conn.commit()
    finally:
        conn.close()
    return rows


def reply(rid, phone, body, user_id=None, db_path=DB_PATH) -> dict:
    """The owner's answer to a guest who texted the restaurant's number —
    only inside INBOX_REPLY_WINDOW_DAYS of the guest's own last text, and
    never to a number that said STOP."""
    import guest_marketing as gm
    import notify
    p = _phone(phone)
    text = " ".join(str(body or "").split())
    if not p or not text:
        return {"ok": False, "error": "Write a reply first."}
    if len(text) > 320:
        return {"ok": False, "error": "Keep a reply under 320 characters."}
    if gm.phone_opted_out(p, db_path=db_path):
        return {"ok": False, "error": "This guest replied STOP, so they can't be texted."}
    cutoff = (datetime.utcnow() - timedelta(days=INBOX_REPLY_WINDOW_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_conn(db_path)
    try:
        asked = conn.execute("SELECT 1 FROM guest_inbox WHERE restaurant_id=? AND phone=? AND direction='in' "
                             "AND at >= ? LIMIT 1", (rid, p, cutoff)).fetchone()
    finally:
        conn.close()
    if not asked:
        return {"ok": False, "error": "You can reply to a guest within 30 days of their own text."}
    restaurant = _restaurant(rid, db_path)
    out_text = gm.sms_prefix(getattr(restaurant, "name", "")) + text
    with notify.sms_context(rid):
        ok = bool(gm.send_sms(p, out_text, use_case="guest"))
    conn = get_conn(db_path)
    try:
        conn.execute("INSERT INTO guest_inbox (restaurant_id, phone, direction, body, kind, user_id, status) VALUES "
                     "(?,?,'out',?,'reply',?,?)", (rid, p, out_text, user_id, "sent" if ok else "failed"))
        conn.commit()
    finally:
        conn.close()
    return {"ok": ok, "error": None if ok else "That reply didn't send. Try again in a minute."}


def texting_status(rid, db_path=DB_PATH) -> dict:
    """What the screen says about the number guest texts come from."""
    s = sender_for(rid, db_path=db_path)
    if s:
        return {"own_number": True, "number": phone_display(s["number"])}
    return {"own_number": False, "number": None}


# ── RPOWER checks with a customer attached (phase 3) ───────────────────────

def usual_items(rid, contact_ids, db_path=DB_PATH) -> dict:
    """{contact_id: [up to 3 item names]} — what each guest orders most,
    from the RPOWER checks matched to them (guest_pos_visits)."""
    ids = [int(c) for c in contact_ids or [] if c]
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            f"SELECT v.contact_id, l.item_name, SUM(l.qty) q FROM guest_pos_visits v JOIN pos_ticket_lines l ON "
            f"l.restaurant_id=v.restaurant_id AND l.ticket_id=v.ticket_id WHERE v.restaurant_id=? AND "
            f"v.contact_id IN ({marks}) AND l.item_name IS NOT NULL AND l.parent_line_id IS NULL AND "
            f"COALESCE(l.kind,'') NOT IN ('comp','void','refund') GROUP BY v.contact_id, l.item_name",
            (rid, *ids)).fetchall()
    except Exception:
        return {}
    finally:
        conn.close()
    by = {}
    for r in rows:
        by.setdefault(r["contact_id"], []).append((float(r["q"] or 0), r["item_name"]))
    return {cid: [n for _q, n in sorted(v, key=lambda x: (-x[0], x[1]))[:3]] for cid, v in by.items()}


def match_pos_visits(rid, day, customers=None, db_path=DB_PATH) -> dict:
    """A night's RPOWER checks that carry a real customer (not the
    placeholder every untagged check uses): the customer's phone finds or
    adds the guest (unconsented), and each check is one visit, once."""
    conn = get_conn(db_path)
    try:
        tickets = conn.execute("SELECT ticket_id, customer_id, net_sales FROM pos_tickets WHERE restaurant_id=? AND "
                               "business_date=? AND customer_id IS NOT NULL AND cancelled=0",
                               (rid, str(day)[:10])).fetchall()
    finally:
        conn.close()
    tickets = [t for t in tickets if str(t["customer_id"] or "").strip() not in _PLACEHOLDER_CUSTOMERS]
    if not tickets:
        return {"tickets": 0, "matched": 0}
    if customers is None:
        import rpower
        customers = rpower.fetch_customers(rid)
    matched = 0
    for t in tickets:
        c = customers.get(str(t["customer_id"])) or {}
        p = _phone(c.get("phone"))
        if not p or c.get("company"):
            continue                       # a house account or a delivery service, not a guest
        cid = _touch_contact(rid, p, name=c.get("name"), email=c.get("email"), source="pos", db_path=db_path)
        conn = get_conn(db_path)
        try:
            cur = conn.execute("INSERT OR IGNORE INTO guest_pos_visits (restaurant_id, ticket_id, contact_id, "
                               "business_date, net_sales) VALUES (?,?,?,?,?)",
                               (rid, str(t["ticket_id"]), cid, str(day)[:10], t["net_sales"]))
            if cur.rowcount:
                _record_visit(conn, rid, cid, str(day)[:10] + "T19:00:00")
                matched += 1
            conn.commit()
        finally:
            conn.close()
    return {"tickets": len(tickets), "matched": matched}


def run_pos_guest_match(db_path=DB_PATH, day=None) -> dict:
    """Nightly, after the POS archive: yesterday's checks at every RPOWER
    restaurant. A restaurant whose checks carry no customer costs one read."""
    out = {"attempted": 0, "ok": 0, "failed": 0, "skipped": 0, "hit_bound": False}
    conn = get_conn(db_path)
    try:
        rids = [r[0] for r in conn.execute("SELECT id FROM restaurants WHERE COALESCE(rpower_token,'') <> '' "
                                           "AND COALESCE(billing_status,'') NOT IN ('cancelled','internal')").fetchall()]
    finally:
        conn.close()
    for rid in rids:
        target = day or (_local_now(rid).date() - timedelta(days=1))
        try:
            res = match_pos_visits(rid, target, db_path=db_path)
        except Exception as e:
            import ops
            ops.capture(e, job="guest_pos_match", context=f"restaurant_id={rid}")
            out["failed"] += 1
            continue
        if res["tickets"]:
            out["attempted"] += 1
            out["ok"] += 1
        else:
            out["skipped"] += 1
    return out
