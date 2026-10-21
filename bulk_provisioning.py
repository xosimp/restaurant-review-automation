"""bulk_provisioning.py — many restaurants from one list (scale audit 10/19/26 ONB-2).

Provisioning was one admin form per restaurant, one token paste and store
pick per RPOWER store, one envelope per store: a hundred stores were a few
hundred console sessions. This takes a CSV — one row per store — and does
what the New client form and the RPOWER connect do, for every row:

  restaurant_name, owner_email          required
  owner_name, owner_phone, timezone (one of time_utils.COMMON_TIMEZONES),
  google_place_id, modules ("reviews,labor,inventory,marketing"; reviews
  when blank), location_group, location_name,
  rpower_cg, rpower_store_mid           the RPOWER store this restaurant is

DRY RUN FIRST (plan): every row checked and classed create / exists /
error, nothing written. APPLY (apply, on the admin job pool — it may read
RPOWER once): idempotent per store — a row whose RPOWER store (cg +
store_mid), or else whose owner email and restaurant name, already has a
restaurant is `exists` and left alone, so a list can be applied again after
fixing its errors. Each row is its own unit of work: a row that fails part
way is taken back out (models.delete_restaurant, as the New client form
does) and the rest go on. Every row is a provision_rows line under one
provision_batches row.

The owner gets THE welcome (a set-password link — emails.
send_welcome_with_set_password_link through billing_jobs' outbox, which only
sends on Railway); no password is typed, shown or emailed. An owner email
that already signs in as an owner adds the restaurant to that owner's group,
as a paid checkout does (provisioning.py); any other existing login is
refused for a person to reconcile.

RPOWER: with one partner token for the batch (RPOWER's store listing — the
token a reseller or a group holds), the token is checked once against
RPOWER and each row's store must be one it sees; the store is bound to the
partner credential that holds the token once (credentials.pos_credentials,
sealed — Audit #23 A7-01), never a copy per row. Without a token the store is
recorded (it reserves the store against a second restaurant) and the token
is pasted per store later (Data & integrations → RPOWER). The payroll week
RPOWER suggests is a setup confirmation, never applied (ONB-11).

No DocuSign envelope and no checkout are sent from here: a direct store's
contract is sent from its billing tab, and a store under a partner billed
by the partner needs neither (onboarding.skips_direct_contract, ONB-10).

Scale re-audit 10/20/26:
  * BILLING (R8-07): every store a list creates starts at ONE billing status,
    BULK_PROVISION_BILLING_STATUS (default `trial`, as before — how RPOWER-
    onboarded stores are billed is not decided yet). The dry run says which,
    and what it means for the AI pools, before anything is created.
  * HISTORY (R8-10): a store bound with the batch's token gets its onboarding
    archive queued (strategy_jobs.enqueue_pos_onboard), as a store connected
    by hand does — its first nightly sync no longer reads 60 days live.
  * ONE APPLY AT A TIME (R8-19): an apply takes the batch ledger's write lock
    to start, and refuses while another batch started in the last
    APPLY_STALE_MINUTES is still running; each row is checked again just
    before it is created, so a double submit or two admins can never bind
    one RPOWER store to two restaurants.
  * OWNER MATCH (R7-12, CONFIRMED-DIFFERENT): a row whose owner email already
    signs in as an owner joins that owner's group — intended for a
    multi-location owner, and now SHOWN in the dry run (joins_owner) instead
    of reading as a plain create. Admin logins never match.
"""
import csv
import io
import os
import re
import secrets

import models as _models_mod

MAX_ROWS = 200
MODULE_KEYS = ("reviews", "labor", "inventory", "marketing")
COLUMNS = ("restaurant_name", "owner_email", "owner_name", "owner_phone", "timezone", "google_place_id",
           "modules", "location_group", "location_name", "rpower_cg", "rpower_store_mid", "merge_owner")
_YES = ("1", "yes", "y", "true", "on")

# Audit #23 F7 (10/21/26):
#   * OWNER CHECK (A7-02): an owner email in a list proves nothing — one
#     stale or mistyped address in 1,500 rows gave a stranger a real store's
#     sales, wages and staff. Every store a list binds to RPOWER is HELD
#     (restaurants.pos_owner_hold): its archive runs, but every login of it
#     sees only the store check until the owner confirms the store — its
#     name as RPOWER has it and its serial number, from their own RPOWER
#     server (verify_owner) — or an admin who has spoken to them releases it
#     (release_hold, audited). A list signed by RPOWER (manifest_verified:
#     HMAC-SHA256 of the CSV text under RPOWER_MANIFEST_HMAC_KEY) holds
#     nothing: RPOWER attests the owners. The dry run says which (`owner_check`).
#   * MERGE (A7-02): a row whose owner email already signs in as an owner
#     joins that owner's group only with merge_owner=yes on the row; without
#     it the row is an error the dry run shows.
#   * ONE CREDENTIAL (A7-01): the batch's token is held once
#     (credentials.pos_credentials) and every row binds its id — never a copy
#     of the token per row.
#   * CAP (A5-01): at most RPOWER_MAX_STORES_PER_CREDENTIAL (500) live stores
#     on one partner credential until RPOWER states its limit — one token is
#     one request budget.
OWNER_CHECK_STEP = "pos_owner"
OWNER_CHECK_MAX_FAILS = 5
OWNER_CHECK_WINDOW_MINUTES = 60
OWNER_CHECK_SECRET = "pos_owner_check"


def max_stores_per_credential() -> int:
    try:
        return max(1, int(os.getenv("RPOWER_MAX_STORES_PER_CREDENTIAL", "500") or 500))
    except ValueError:
        return 500


def manifest_verified(csv_text, signature) -> bool:
    """Whether `signature` is RPOWER's HMAC-SHA256 (hex) of this exact CSV
    text under RPOWER_MANIFEST_HMAC_KEY. False without a key or signature —
    the format is provisional until RPOWER confirms one (Audit #23 §5 Q5)."""
    import hashlib
    import hmac
    key = (os.getenv("RPOWER_MANIFEST_HMAC_KEY") or "").strip()
    sig = (signature or "").strip().lower()
    if not key or not sig or not isinstance(csv_text, str):
        return False
    want = hmac.new(key.encode(), csv_text.strip().encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, sig)


def _norm_name(s) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).split())


def _serial_key(serial, db_path=None):
    """A keyed digest of a store's serial number (never kept as typed)."""
    import hashlib
    import hmac
    norm = re.sub(r"[^A-Za-z0-9]+", "", str(serial or "")).upper()
    if not norm:
        return None
    key = _models_mod.kept_secret(OWNER_CHECK_SECRET, 1, **({"db_path": db_path} if db_path else {}))
    return hmac.new(key, norm.encode(), hashlib.sha256).hexdigest()[:32] if key else None


def _check_row(rid, db_path=None):
    import onboarding
    return (onboarding.confirmations(rid, db_path) or {}).get(OWNER_CHECK_STEP) or {}


def _write_check(rid, suggested, db_path=None):
    import json
    conn = get_conn(db_path)
    try:
        conn.execute("INSERT INTO onboarding_confirmations (restaurant_id, step, status, suggested_json, source) "
                     "VALUES (?, ?, 'pending', ?, 'bulk owner check') ON CONFLICT(restaurant_id, step) DO UPDATE "
                     "SET status='pending', suggested_json=excluded.suggested_json, value_json=NULL, "
                     "confirmed_by=NULL, confirmed_at=NULL",
                     (int(rid), OWNER_CHECK_STEP, json.dumps(suggested)))
        conn.commit()
    finally:
        conn.close()


def hold_for_owner(rid, store=None, via="bulk", db_path=None) -> None:
    """Hold a store's POS data for its owner's check (see the note above)."""
    from time_utils import utc_stamp
    dbp = {"db_path": db_path} if db_path else {}
    _models_mod.update_restaurant(rid, {"pos_owner_hold": utc_stamp()}, **dbp)
    sug = {"via": via, "fails": 0, "fail_at": None, "store_name": None, "serial_key": None}
    if store:
        sug.update(store_name=store.get("name"), serial_key=_serial_key(store.get("serial_number"), db_path))
    _write_check(rid, sug, db_path)


def record_store_facts(rid, store, db_path=None) -> None:
    """A held store bound later (its token pasted after the list): the name
    and serial the owner's check compares with."""
    sug = dict(_check_row(rid, db_path).get("suggested") or {"via": "bind", "fails": 0})
    sug.update(store_name=store.get("name"), serial_key=_serial_key(store.get("serial_number"), db_path))
    _write_check(rid, sug, db_path)


def _locked(sug) -> bool:
    from datetime import datetime, timedelta
    if int(sug.get("fails") or 0) < OWNER_CHECK_MAX_FAILS or not sug.get("fail_at"):
        return False
    try:
        at = datetime.strptime(str(sug["fail_at"])[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False
    return datetime.utcnow() - at < timedelta(minutes=OWNER_CHECK_WINDOW_MINUTES)


def owner_check_state(rid, db_path=None) -> dict:
    """{held, store_known, locked, can_check} for the store check page."""
    r = _models_mod.get_restaurant(rid, **({"db_path": db_path} if db_path else {}))
    held = bool(r is not None and getattr(r, "pos_owner_hold", None))
    sug = _check_row(rid, db_path).get("suggested") or {}
    locked = _locked(sug)
    return {"held": held, "store_known": bool(sug.get("serial_key")), "locked": locked,
            "can_check": held and bool(sug.get("serial_key")) and not locked}


def verify_owner(rid, store_name, serial, who, db_path=None) -> dict:
    """The owner's store check (A7-02): the store's name as RPOWER has it
    and its serial number. Five misses in an hour lock it for the hour.
    {ok, error?, released?, locked?}."""
    import hmac as _hmac
    from time_utils import utc_stamp
    state = owner_check_state(rid, db_path)
    if not state["held"]:
        return {"ok": True, "released": False}
    sug = dict(_check_row(rid, db_path).get("suggested") or {})
    if not sug.get("serial_key"):
        return {"ok": False, "error": "Cavnar AI hasn't read this store from RPOWER yet, so it can't be checked "
                                      "here. Contact support and we'll confirm it with you."}
    if _locked(sug):
        return {"ok": False, "locked": True,
                "error": "Too many tries. Wait an hour, or contact support and we'll confirm it with you."}
    name_ok = bool(_norm_name(store_name)) and _norm_name(store_name) == _norm_name(sug.get("store_name"))
    serial_ok = _hmac.compare_digest(_serial_key(serial, db_path) or "", str(sug.get("serial_key") or ""))
    if not (name_ok and serial_ok):
        fails = int(sug.get("fails") or 0)
        if fails >= OWNER_CHECK_MAX_FAILS:
            fails = 0                                   # the last lock lapsed
        sug.update(fails=fails + 1, fail_at=utc_stamp())
        _write_check(rid, sug, db_path)
        return {"ok": False, "error": "That doesn't match RPOWER's record of this store. Check the store name and "
                                      "the serial number on your RPOWER server."}
    release_hold(rid, who, "owner_check", db_path=db_path)
    return {"ok": True, "released": True}


def release_hold(rid, who, how, db_path=None) -> None:
    """End the hold: the owner checked the store, or an admin confirmed it."""
    import json
    from time_utils import utc_stamp
    _models_mod.update_restaurant(rid, {"pos_owner_hold": None}, **({"db_path": db_path} if db_path else {}))
    conn = get_conn(db_path)
    try:
        conn.execute("INSERT INTO onboarding_confirmations (restaurant_id, step, status, value_json, confirmed_by, "
                     "confirmed_at) VALUES (?, ?, 'confirmed', ?, ?, ?) ON CONFLICT(restaurant_id, step) DO UPDATE "
                     "SET status='confirmed', value_json=excluded.value_json, confirmed_by=excluded.confirmed_by, "
                     "confirmed_at=excluded.confirmed_at",
                     (int(rid), OWNER_CHECK_STEP, json.dumps({"how": how}), (who or "")[:120] or None, utc_stamp()))
        conn.commit()
    finally:
        conn.close()
_EMAIL = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")

# The billing status every store a list creates starts at (R8-07). `trial`
# (the default, the column's own default) draws on the trial AI pool;
# `active` is a paying store — the paying pool, and the nightly Stripe
# reconcile then reports each one with no Stripe customer unless a group or
# partner bills it. The choice is the owner's, per rollout (docs/ops/
# ENVIRONMENT.md); anything else is refused at the dry run.
BILLING_STATUSES = ("trial", "active")
# An apply still `running` after this long is a crashed one, not a live one.
APPLY_STALE_MINUTES = 120


def billing_status() -> str:
    return (os.getenv("BULK_PROVISION_BILLING_STATUS") or "trial").strip().lower()


def billing_note() -> dict:
    """What the dry run says about billing: {status, ok, note}."""
    st = billing_status()
    if st not in BILLING_STATUSES:
        return {"status": st, "ok": False,
                "note": f"BULK_PROVISION_BILLING_STATUS is {st!r}; it must be one of {', '.join(BILLING_STATUSES)}."}
    if st == "trial":
        note = ("Each new store starts as a trial (BULK_PROVISION_BILLING_STATUS=trial): its AI draws on the "
                "shared trial pool until it is moved to paying.")
    else:
        note = ("Each new store starts as paying (BULK_PROVISION_BILLING_STATUS=active): its AI draws on the "
                "paying pool, and the nightly Stripe check lists any store with no Stripe customer.")
    return {"status": st, "ok": True, "note": note}


def get_conn(db_path=None):
    return _models_mod.get_conn(db_path) if db_path is not None else _models_mod.get_conn()


def parse_csv(text):
    """(rows, error): each row a dict of COLUMNS (blank → ""), with `line`
    (1 = the first data row). Headers are matched case-insensitively,
    spaces as underscores."""
    if not isinstance(text, str) or not text.strip():
        return None, "Paste the list as CSV with a header row."
    reader = csv.DictReader(io.StringIO(text.strip()))
    if not reader.fieldnames:
        return None, "The first line must name the columns."
    names = {f: re.sub(r"[\s-]+", "_", (f or "").strip().lower()) for f in reader.fieldnames}
    unknown = sorted(n for n in names.values() if n and n not in COLUMNS)
    if unknown:
        return None, f"Unknown column(s): {', '.join(unknown)}. Columns are: {', '.join(COLUMNS)}."
    if "restaurant_name" not in names.values() or "owner_email" not in names.values():
        return None, "The list needs restaurant_name and owner_email columns."
    rows = []
    for i, raw in enumerate(reader, start=1):
        if i > MAX_ROWS:
            return None, f"One list takes up to {MAX_ROWS} restaurants; split it."
        row = {c: "" for c in COLUMNS}
        for k, v in raw.items():
            if k in names and names[k]:
                row[names[k]] = (v or "").strip()
        if not any(row.values()):
            continue
        row["line"] = i
        rows.append(row)
    if not rows:
        return None, "The list has no rows."
    return rows, None


def _modules(text):
    keys = {k.strip().lower() for k in (text or "").replace(";", ",").split(",") if k.strip()}
    bad = sorted(k for k in keys if k not in MODULE_KEYS)
    return keys or {"reviews"}, bad


def store_key(row) -> str:
    if row.get("rpower_store_mid"):
        return f"rpower:{(row.get('rpower_cg') or '').strip()}:{row['rpower_store_mid'].strip()}"
    return f"owner:{row['owner_email'].strip().lower()}:{row['restaurant_name'].strip().lower()}"


def _existing(row, db_path=None):
    """The id of the restaurant this row already is, or None. An RPOWER row
    is matched on its consolidation group AND store (R7-12: a store id alone
    can repeat across groups)."""
    conn = get_conn(db_path)
    try:
        if row.get("rpower_store_mid"):
            hit = conn.execute("SELECT id FROM restaurants WHERE TRIM(COALESCE(rpower_store_mid,''))=? "
                               "AND (rpower_cg IS NULL OR CAST(rpower_cg AS TEXT)=?) "
                               "AND COALESCE(is_demo,0)=0 ORDER BY id LIMIT 1",
                               (row["rpower_store_mid"].strip(), str(row.get("rpower_cg") or "").strip())).fetchone()
            if hit:
                return int(hit["id"])
        hit = conn.execute("SELECT id FROM restaurants WHERE LOWER(TRIM(owner_email))=? AND LOWER(TRIM(name))=? "
                           "AND COALESCE(is_demo,0)=0 ORDER BY id LIMIT 1",
                           (row["owner_email"].strip().lower(), row["restaurant_name"].strip().lower())).fetchone()
        return int(hit["id"]) if hit else None
    finally:
        conn.close()


def _owner_login(email, db_path=None):
    """(user row, is_that_owner) for an email that already signs in."""
    conn = get_conn(db_path)
    try:
        u = conn.execute("SELECT u.id, u.restaurant_id, COALESCE(u.role,'client') AS role, r.owner_email, "
                         "r.location_group, r.name FROM users u JOIN restaurants r ON r.id = u.restaurant_id "
                         "WHERE LOWER(u.email)=? AND u.is_active=1 AND COALESCE(u.is_admin,0)=0 "
                         "ORDER BY u.id LIMIT 1", (email,)).fetchone()
    finally:
        conn.close()
    if not u:
        return None, False
    return dict(u), (u["role"] in ("client", "owner") and (u["owner_email"] or "").strip().lower() == email)


def check_row(row, db_path=None):
    """The first thing wrong with one row, or None (no database writes)."""
    import time_utils
    name, email = row.get("restaurant_name", ""), row.get("owner_email", "").lower()
    if not name:
        return "restaurant_name is blank."
    if len(name) > 200:
        return "restaurant_name is too long."
    if not email or len(email) > 254 or not _EMAIL.match(email):
        return "owner_email must be one email address."
    tz = row.get("timezone") or ""
    if tz and tz not in time_utils.COMMON_TIMEZONES:
        return f"timezone {tz!r} is not one the product offers."
    place = row.get("google_place_id") or ""
    if place and (len(place) > 300 or any(c.isspace() for c in place)):
        return "google_place_id has spaces in it."
    _keys, bad = _modules(row.get("modules"))
    if bad:
        return f"Unknown module(s): {', '.join(bad)} (reviews, labor, inventory, marketing)."
    if bool(row.get("rpower_cg")) != bool(row.get("rpower_store_mid")):
        return "An RPOWER store needs both rpower_cg and rpower_store_mid."
    if row.get("rpower_cg") and not row["rpower_cg"].isdigit():
        return "rpower_cg is a number."
    return None


def _rpower_page_size():
    import rpower
    return rpower.PAGE_SIZE


def _rpower_store(token, cg, store_mid):
    """One store by its mid (rpower.store_by_mid), in test_token's shape, when
    it is in group `cg`; None otherwise or when RPOWER can't say."""
    import rpower
    try:
        one = rpower.store_by_mid(token, store_mid)
    except rpower.RPowerError:
        return None
    if not one or str(one.get("cg")) != str(cg):
        return None
    tz, problem = rpower.store_timezone(one.get("timezone"))
    return {"cg": one.get("cg"), "store_mid": one.get("mid"), "name": one.get("name"), "timezone": tz,
            "timezone_raw": one.get("timezone"), "timezone_problem": problem, "ot_dow": one.get("ot_dow"),
            "week_dow": one.get("week_dow"), "open_days": one.get("open_days")}


def plan(rows, db_path=None, manifest=False) -> dict:
    """The dry run: [{line, store_key, action, restaurant_id, error,
    restaurant_name, owner_email, rpower_store_mid, joins_owner,
    owner_check}] and counts, and the billing status the new stores start
    at. Nothing is written. `joins_owner` ({restaurant_id, name, group}) on
    a create row whose owner email already signs in as an owner: the new
    store joins that owner's group and login (R7-12) — only with
    merge_owner=yes on the row (A7-02), else the row is an error.
    `owner_check` on an RPOWER create row: "held" (its POS data waits for
    the owner's store check) or "manifest" (`manifest`: RPOWER signed the
    list, nothing is held)."""
    out, seen = [], {}
    for row in rows:
        item = {"line": row["line"], "restaurant_name": row.get("restaurant_name"),
                "owner_email": row.get("owner_email"), "rpower_store_mid": row.get("rpower_store_mid") or None,
                "action": "create", "restaurant_id": None, "error": None}
        err = check_row(row, db_path)
        key = store_key(row) if not err else None
        item["store_key"] = key
        if not err and key in seen:
            err = f"the same store as line {seen[key]}."
        if err:
            item.update(action="error", error=err)
            out.append(item)
            continue
        seen[key] = row["line"]
        rid = _existing(row, db_path)
        if rid:
            item.update(action="exists", restaurant_id=rid)
            out.append(item)
            continue
        err = _conflicts(row, db_path)
        if err:
            item.update(action="error", error=err)
        else:
            u, is_owner = _owner_login(row["owner_email"].strip().lower(), db_path)
            if u and is_owner:
                item["joins_owner"] = {"restaurant_id": u.get("restaurant_id"), "name": u.get("name"),
                                       "group": (u.get("location_group") or "").strip() or u.get("name")}
                if str(row.get("merge_owner") or "").strip().lower() not in _YES:
                    item.update(action="error", error=(
                        f"{row['owner_email'].strip().lower()} already signs in as the owner of "
                        f"{u.get('name')}. Set merge_owner=yes on this row to add the store to that owner's "
                        f"group, or use a different owner email."))
            if item["action"] == "create" and row.get("rpower_store_mid"):
                item["owner_check"] = "manifest" if manifest else "held"
        out.append(item)
    counts = {a: sum(1 for i in out if i["action"] == a) for a in ("create", "exists", "error")}
    return {"ok": True, "rows": out, "counts": counts, "billing": billing_note(),
            "joins_owner": sum(1 for i in out if i.get("joins_owner")),
            "owner_check": {"held": sum(1 for i in out if i.get("owner_check") == "held"),
                            "manifest": sum(1 for i in out if i.get("owner_check") == "manifest"),
                            "note": ("RPOWER signed this list: its owners are attested and nothing is held."
                                     if manifest else
                                     "Each RPOWER store's POS data is held until its owner confirms the store "
                                     "(its name and serial number) or an admin confirms it; the archive "
                                     "still runs.")}}


def _conflicts(row, db_path=None):
    """Why a new restaurant for this row would collide with another, or None."""
    email = row["owner_email"].strip().lower()
    u, is_owner = _owner_login(email, db_path)
    if u and not is_owner:
        return f"{email} already signs in to {u.get('name')} and is not its owner — reconcile it by hand."
    place = (row.get("google_place_id") or "").strip()
    if place:
        clash = _models_mod.place_id_conflict(place, **({"db_path": db_path} if db_path else {}))
        if clash:
            return f"that Google listing is already connected to {clash}."
    if row.get("rpower_store_mid"):
        clash = _models_mod.pos_binding_conflict("rpower", (int(row["rpower_cg"]), row["rpower_store_mid"]),
                                                 **({"db_path": db_path} if db_path else {}))
        if clash:
            return f"that RPOWER store is already connected to {clash}."
    group = (row.get("location_group") or "").strip()
    if group:
        clash = _models_mod.location_group_conflict(group, email, **({"db_path": db_path} if db_path else {}))
        if clash:
            return f"location group {group!r} already belongs to {clash}."
    return None


def _username_for(email, db_path=None):
    import provisioning
    return provisioning._username_for(email, db_path or _models_mod.DB_PATH)


def _create_one(row, partner_id, stores, cred_id, send_welcome, actor, db_path=None, manifest=False, token=None):
    """One row's restaurant, owner, store binding and modules. Returns the
    new restaurant id; raises (after taking the half-made restaurant back
    out) on any failure. `cred_id` is the batch's partner credential (A7-01):
    the row binds it, never a copy of the token. An RPOWER store is held for
    its owner's check unless `manifest` (A7-02)."""
    from auth import create_user
    import auth as _auth
    dbp = {"db_path": db_path} if db_path else {}
    email = row["owner_email"].strip().lower()
    keys, _bad = _modules(row.get("modules"))
    u, is_owner = _owner_login(email, db_path)
    if u and is_owner and str(row.get("merge_owner") or "").strip().lower() not in _YES:
        raise ValueError(f"{email} already signs in as an owner; set merge_owner=yes to add the store to that "
                         f"owner's group")
    rid = _models_mod.create_restaurant(_models_mod.Restaurant(
        name=row["restaurant_name"].strip()[:200], owner_email=email,
        owner_name=(row.get("owner_name") or "").strip()[:120] or None,
        owner_phone=(row.get("owner_phone") or "").strip()[:40] or None,
        google_place_id=(row.get("google_place_id") or "").strip() or None,
        location_group=(row.get("location_group") or "").strip() or None,
        location_name=(row.get("location_name") or "").strip() or None,
        timezone=(row.get("timezone") or "").strip() or "America/Chicago"), **dbp)
    try:
        uid = None
        updates = {f"module_{k}": (1 if k in keys else 0) for k in MODULE_KEYS}
        updates["billing_status"] = billing_status()
        updates["reviews_live"] = 1 if ((row.get("google_place_id") or "").strip() and "reviews" in keys) else 0
        if u and is_owner:
            group = (u.get("location_group") or "").strip() or (u.get("name") or "").strip() or email
            if not (u.get("location_group") or "").strip():
                _models_mod.update_restaurant(u["restaurant_id"], {"location_group": group}, **dbp)
            updates["location_group"] = group
            _auth.upsert_membership(u["id"], rid, "owner", **dbp)
        else:
            # A password nobody is ever shown: the owner chooses one through
            # the welcome's set-password link.
            uid = create_user(restaurant_id=rid, username=_username_for(email, db_path), email=email,
                              password=secrets.token_urlsafe(24), generated=True, **dbp)
        if row.get("rpower_store_mid"):
            store = None
            if stores is not None:
                store = next((s for s in stores if str(s.get("store_mid")) == row["rpower_store_mid"]
                              and str(s.get("cg")) == str(row["rpower_cg"])), None)
                if store is None and len(stores) >= _rpower_page_size():
                    # A partner token's list may not name every store it can
                    # read (audit #23 A1-05): the store is asked for itself.
                    store = _rpower_store(token, row["rpower_cg"], row["rpower_store_mid"])
                if store is None:
                    raise ValueError("the RPOWER token cannot see that store")
            updates.update({"rpower_cg": int(row["rpower_cg"]), "rpower_store_mid": row["rpower_store_mid"],
                            "pos_system": "RPOWER"})
            if store is not None:
                from datetime import datetime as _dt
                updates.update({"rpower_credential_id": cred_id, "rpower_store_name": store.get("name"),
                                "rpower_verified_at": _dt.utcnow().isoformat(timespec="seconds"),
                                "rpower_sync_error": None})
                # RPOWER's timezone, checked against the tz database (A1-18):
                # a name it does not know is never stored, and is said.
                if store.get("timezone") and not (row.get("timezone") or "").strip():
                    updates["timezone"] = store["timezone"]
        _models_mod.update_restaurant(rid, updates, **dbp)
        if (row.get("rpower_store_mid") and (store or {}).get("timezone_problem")
                and not (row.get("timezone") or "").strip()):
            import rpower
            rpower.flag_timezone(rid, store.get("timezone_raw"), store.get("name"))
        if row.get("rpower_store_mid") and not manifest:
            hold_for_owner(rid, store, via="bulk", db_path=db_path)
        import onboarding
        onboarding.start_setup(rid, db_path=db_path)
        if partner_id:
            import onboarding
            onboarding.attach_store(partner_id, rid, db_path=db_path)
        if row.get("rpower_store_mid") and stores is not None:
            ot = (store or {}).get("ot_dow")
            if ot is not None:
                try:
                    import onboarding
                    day = (int(ot) - 1) % 7
                    onboarding.suggest(rid, "week_start", {"week_start_day": day, "ot_dow": ot,
                                                           "label": onboarding._WEEKDAYS[day]},
                                       source="rpower ot_dow (unconfirmed reading)", db_path=db_path)
                except (TypeError, ValueError):
                    pass
    except BaseException:
        try:
            _models_mod.delete_restaurant(rid, **dbp)
        except Exception as e:
            import ops
            ops.capture(e, job="bulk_provision_rollback", context=f"restaurant_id={rid}")
        raise
    if row.get("rpower_store_mid") and stores is not None:
        # Bound with the batch's token: its onboarding archive and first sync
        # now, on the pos_io lane, as an RPOWER connect by hand does (R8-10).
        try:
            import strategy_jobs
            strategy_jobs.enqueue_pos_onboard(rid, f"{row['rpower_cg']}:{row['rpower_store_mid']}")
        except Exception as e:
            import ops
            ops.capture(e, job="bulk_provision_onboard", context=f"restaurant_id={rid}")
    if send_welcome and uid:
        try:
            import billing_jobs
            billing_jobs.enqueue("welcome", rid, f"welcome:{uid}", to_email=email, payload={"user_id": uid},
                                 source="bulk_provision", db_path=db_path)
        except Exception as e:
            import ops
            ops.capture(e, job="bulk_provision_welcome", context=f"restaurant_id={rid}")
    return rid


def apply(rows, actor, partner_id=None, rpower_token=None, send_welcome=True, db_path=None,
          credential_id=None, csv_text=None, manifest_signature=None) -> dict:
    """Create every `create` row of plan(rows); `exists` and `error` rows are
    recorded and left alone. One provision_batches row; returns {ok,
    batch_id, counts, rows, credential_id?, error?}. The RPOWER token is the
    pasted `rpower_token` or a partner credential already held
    (`credential_id`); either way the rows bind the credential (A7-01).
    `manifest_signature` over `csv_text` (manifest_verified) means RPOWER
    attests the owners and no store is held (A7-02)."""
    stores, cred_id = None, None
    if not billing_note()["ok"]:
        return {"ok": False, "error": billing_note()["note"]}
    import credentials
    token = (rpower_token or "").strip() or None
    if not token and credential_id:
        token = credentials.credential_token(credential_id, **({"db_path": db_path} if db_path else {}))
        if not token:
            return {"ok": False, "error": "That partner credential can't be read (revoked, or its key is missing)."}
    if token and any(r.get("rpower_store_mid") for r in rows):
        refused = credentials.saves_refused()
        if refused:
            return {"ok": False, "error": refused}
        import rpower
        probe = rpower.test_token(token)
        if not probe.get("ok"):
            return {"ok": False, "error": f"RPOWER refused the token: {probe.get('error')}"}
        stores = probe.get("stores") or []
        try:
            cred_id = credentials.credential_for_token("rpower", token, actor=actor, label="RPOWER partner token",
                                                       **({"db_path": db_path} if db_path else {}))
        except credentials.CredentialKeyError as e:
            return {"ok": False, "error": str(e)}
    manifest = manifest_verified(csv_text, manifest_signature)
    batch_id, busy = _start_batch(actor, partner_id, len(rows), db_path)
    if batch_id is None:
        return {"ok": False, "error": busy}
    try:
        planned = plan(rows, db_path, manifest=manifest)
        by_line = {r["line"]: r for r in rows}
        results = _apply_rows(planned, by_line, partner_id, stores, cred_id, send_welcome, actor, db_path,
                              manifest=manifest, token=token)
    except BaseException:
        _finish_batch(batch_id, [], {"created": 0, "exists": 0, "error": 0}, "failed", db_path)
        raise
    counts = {a: sum(1 for i in results if i["action"] == a) for a in ("created", "exists", "error")}
    _finish_batch(batch_id, results, counts, "done", db_path)
    return {"ok": True, "batch_id": batch_id, "counts": counts, "rows": results, "credential_id": cred_id,
            "manifest": manifest}


def _start_batch(actor, partner_id, n_rows, db_path=None):
    """(batch id, None), or (None, why) while another apply is running — the
    check and the new batch row in ONE write transaction (R8-19)."""
    import db_dialect
    conn = get_conn(db_path)
    try:
        db_dialect.begin_write(conn, "provision_batches", "apply")
        busy = conn.execute("SELECT id, created_by FROM provision_batches WHERE status='running' "
                            "AND created_at >= datetime('now', ?) ORDER BY id DESC LIMIT 1",
                            (f"-{APPLY_STALE_MINUTES} minutes",)).fetchone()
        if busy:
            conn.rollback()
            return None, (f"Another list (batch {busy['id']}, by {busy['created_by']}) is being applied now; "
                          "apply this one when it finishes.")
        cur = conn.execute("INSERT INTO provision_batches (created_by, partner_id, rows_total) VALUES (?,?,?)",
                           ((actor or "admin")[:120], partner_id, int(n_rows)))
        batch_id = int(cur.lastrowid)
        conn.commit()
        return batch_id, None
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _apply_rows(planned, by_line, partner_id, stores, cred_id, send_welcome, actor, db_path=None, manifest=False,
                token=None):
    results = []
    cap = max_stores_per_credential()
    on_cred = None
    if cred_id:
        import credentials
        on_cred = credentials.stores_on(cred_id, **({"db_path": db_path} if db_path else {}))
    for item in planned["rows"]:
        if item["action"] == "create":
            row = by_line[item["line"]]
            # Checked again just before it is made (R8-19): a store created
            # since the plan (an earlier batch) is left alone.
            rid = _existing(row, db_path)
            err = None if rid else _conflicts(row, db_path)
            if rid:
                item.update(action="exists", restaurant_id=rid)
                results.append(item)
                continue
            if err:
                item.update(action="error", error=err)
                results.append(item)
                continue
            binds = bool(row.get("rpower_store_mid") and stores is not None and cred_id)
            if binds and on_cred is not None and on_cred >= cap:
                item.update(action="error", error=(
                    f"the partner token already serves {on_cred} stores, the most one token may serve until "
                    f"RPOWER states its limit (RPOWER_MAX_STORES_PER_CREDENTIAL={cap})."))
                results.append(item)
                continue
            try:
                item["restaurant_id"] = _create_one(row, partner_id, stores, cred_id, send_welcome,
                                                    actor, db_path=db_path, manifest=manifest, token=token)
                item["action"] = "created"
                if binds and on_cred is not None:
                    on_cred += 1
            except Exception as e:
                item.update(action="error", error=str(e)[:300])
        results.append(item)
    return results


def _finish_batch(batch_id, results, counts, status, db_path=None):
    conn = get_conn(db_path)
    try:
        if results:
            conn.executemany("INSERT INTO provision_rows (batch_id, line, store_key, restaurant_id, status, error) "
                             "VALUES (?,?,?,?,?,?)",
                             [(batch_id, i["line"], i.get("store_key"), i.get("restaurant_id"), i["action"],
                               i.get("error")) for i in results])
        conn.execute("UPDATE provision_batches SET finished_at=datetime('now'), status=?, rows_created=?, "
                     "rows_existing=?, rows_failed=? WHERE id=?",
                     (status, counts["created"], counts["exists"], counts["error"], batch_id))
        conn.commit()
    finally:
        conn.close()


def batches(limit=20, db_path=None) -> list:
    conn = get_conn(db_path)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT id, created_by, created_at, finished_at, status, partner_id, rows_total, rows_created, "
            "rows_existing, rows_failed FROM provision_batches ORDER BY id DESC LIMIT ?", (int(limit),))]
    finally:
        conn.close()
