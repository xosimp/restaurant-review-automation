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
RPOWER and each row's store must be one it sees; the store is bound and the
token kept (encrypted, credentials.py). Without a token the store is
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
           "modules", "location_group", "location_name", "rpower_cg", "rpower_store_mid")
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


def plan(rows, db_path=None) -> dict:
    """The dry run: [{line, store_key, action, restaurant_id, error,
    restaurant_name, owner_email, rpower_store_mid, joins_owner}] and counts,
    and the billing status the new stores start at. Nothing is written.
    `joins_owner` ({restaurant_id, name, group}) on a create row whose owner
    email already signs in as an owner: the new store joins that owner's
    group and login (R7-12) — shown, so it is never a surprise."""
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
        out.append(item)
    counts = {a: sum(1 for i in out if i["action"] == a) for a in ("create", "exists", "error")}
    return {"ok": True, "rows": out, "counts": counts, "billing": billing_note(),
            "joins_owner": sum(1 for i in out if i.get("joins_owner"))}


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


def _create_one(row, partner_id, stores, token, send_welcome, actor, db_path=None):
    """One row's restaurant, owner, store binding and modules. Returns the
    new restaurant id; raises (after taking the half-made restaurant back
    out) on any failure."""
    from auth import create_user
    import auth as _auth
    dbp = {"db_path": db_path} if db_path else {}
    email = row["owner_email"].strip().lower()
    keys, _bad = _modules(row.get("modules"))
    u, is_owner = _owner_login(email, db_path)
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
                if store is None:
                    raise ValueError("the RPOWER token cannot see that store")
            updates.update({"rpower_cg": int(row["rpower_cg"]), "rpower_store_mid": row["rpower_store_mid"],
                            "pos_system": "RPOWER"})
            if store is not None:
                from datetime import datetime as _dt
                updates.update({"rpower_token": token, "rpower_store_name": store.get("name"),
                                "rpower_verified_at": _dt.utcnow().isoformat(timespec="seconds"),
                                "rpower_sync_error": None})
                if store.get("timezone") and not (row.get("timezone") or "").strip():
                    updates["timezone"] = store["timezone"]
        _models_mod.update_restaurant(rid, updates, **dbp)
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


def apply(rows, actor, partner_id=None, rpower_token=None, send_welcome=True, db_path=None) -> dict:
    """Create every `create` row of plan(rows); `exists` and `error` rows are
    recorded and left alone. One provision_batches row; returns {ok,
    batch_id, counts, rows, error?}."""
    stores = None
    if not billing_note()["ok"]:
        return {"ok": False, "error": billing_note()["note"]}
    token = (rpower_token or "").strip() or None
    if token and any(r.get("rpower_store_mid") for r in rows):
        import rpower
        probe = rpower.test_token(token)
        if not probe.get("ok"):
            return {"ok": False, "error": f"RPOWER refused the token: {probe.get('error')}"}
        stores = probe.get("stores") or []
    batch_id, busy = _start_batch(actor, partner_id, len(rows), db_path)
    if batch_id is None:
        return {"ok": False, "error": busy}
    try:
        planned = plan(rows, db_path)
        by_line = {r["line"]: r for r in rows}
        results = _apply_rows(planned, by_line, partner_id, stores, token, send_welcome, actor, db_path)
    except BaseException:
        _finish_batch(batch_id, [], {"created": 0, "exists": 0, "error": 0}, "failed", db_path)
        raise
    counts = {a: sum(1 for i in results if i["action"] == a) for a in ("created", "exists", "error")}
    _finish_batch(batch_id, results, counts, "done", db_path)
    return {"ok": True, "batch_id": batch_id, "counts": counts, "rows": results}


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


def _apply_rows(planned, by_line, partner_id, stores, token, send_welcome, actor, db_path=None):
    results = []
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
            try:
                item["restaurant_id"] = _create_one(row, partner_id, stores, token, send_welcome,
                                                    actor, db_path=db_path)
                item["action"] = "created"
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
