"""onboarding.py — taking restaurants on at scale (scale audit 10/19/26, §9).

The single-store mechanics were careful; the process was built for one
restaurant at a time with Will doing the work. This module is the fleet
side of it:

  * Backfill pacing (ONB-1). The 90-day POS archive backfill is ~55 MB a
    store; twenty stores joining in a week broke the nightly backup's
    free-space gate and ~47 filled the volume. A store backfills only while
    it holds one of POS_BACKFILL_MAX_STORES slots (pos_backfill_slots) and
    only while the volume has room for tonight's days AND the backup that
    must follow them (storage_room). A store waiting for a slot or for room
    still archives last night — only its history waits.
  * The onboarding tracker's setup steps (ONB-9): the POS bound, first data,
    the archive's history, the DSR calendar and its history, checklists, and
    the judgment calls (ONB-11, ONB-12) as explicit confirmations
    (onboarding_confirmations) — and the fleet board that counts stores by
    stage and names the stuck ones.
  * The partner / reseller entity (ONB-10), off unless PARTNER_BILLING=1.

Tables (init_onboarding, at boot from models.init_db): pos_backfill_slots,
onboarding_confirmations, provision_batches / provision_rows (bulk
provisioning, bulk_provisioning.py), partners, partner_stores.
"""
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import models as _models_mod

log = logging.getLogger("onboarding")


def get_conn(db_path=None):
    """models.get_conn resolved at call time (CLAUDE.md's bound-import hazard)."""
    return _models_mod.get_conn(db_path) if db_path is not None else _models_mod.get_conn()


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def init_onboarding(db_path=None):
    """Every table here, at boot (models.init_db) — never on a request."""
    conn = get_conn(db_path)
    try:
        # One row per store that has held a backfill slot (ONB-1). Active
        # while finished_at is NULL; a store whose archive gaps open again
        # (a layout bump) takes a slot again.
        conn.execute("""CREATE TABLE IF NOT EXISTS pos_backfill_slots (
            restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
            provider        TEXT NOT NULL,
            admitted_at     TEXT NOT NULL,
            finished_at     TEXT,
            held_reason     TEXT,
            held_at         TEXT,
            PRIMARY KEY (restaurant_id, provider)
        )""")
        # The setup judgments a person makes once per store (ONB-11/12):
        # pending with what the data suggests, or confirmed with the value
        # and who confirmed it.
        conn.execute("""CREATE TABLE IF NOT EXISTS onboarding_confirmations (
            restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
            step            TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'pending',
            suggested_json  TEXT,
            value_json      TEXT,
            source          TEXT,
            confirmed_by    TEXT,
            confirmed_at    TEXT,
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (restaurant_id, step)
        )""")
        # Bulk provisioning (ONB-2): one batch per applied import, one row
        # per line of it — what was created, skipped as already there, or
        # refused, and why.
        conn.execute("""CREATE TABLE IF NOT EXISTS provision_batches (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            created_by      TEXT,
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            finished_at     TEXT,
            status          TEXT NOT NULL DEFAULT 'running',
            partner_id      INTEGER,
            rows_total      INTEGER NOT NULL DEFAULT 0,
            rows_created    INTEGER NOT NULL DEFAULT 0,
            rows_existing   INTEGER NOT NULL DEFAULT 0,
            rows_failed     INTEGER NOT NULL DEFAULT 0
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS provision_rows (
            batch_id        INTEGER NOT NULL REFERENCES provision_batches(id),
            line            INTEGER NOT NULL,
            store_key       TEXT,
            restaurant_id   INTEGER,
            status          TEXT NOT NULL,
            error           TEXT,
            PRIMARY KEY (batch_id, line)
        )""")
        # A partner / reseller selling Cavnar AI to its stores under one
        # master agreement (ONB-10). Nothing reads billing_mode unless
        # PARTNER_BILLING=1.
        conn.execute("""CREATE TABLE IF NOT EXISTS partners (
            id                          INTEGER PRIMARY KEY AUTOINCREMENT,
            name                        TEXT NOT NULL UNIQUE,
            billing_mode                TEXT NOT NULL DEFAULT 'direct',
            master_agreement_signed_at  TEXT,
            master_agreement_ref        TEXT,
            contact_email               TEXT,
            created_at                  TEXT NOT NULL DEFAULT (datetime('now'))
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS partner_stores (
            restaurant_id   INTEGER NOT NULL PRIMARY KEY REFERENCES restaurants(id),
            partner_id      INTEGER NOT NULL REFERENCES partners(id),
            added_at        TEXT NOT NULL DEFAULT (datetime('now'))
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_partner_stores_partner ON partner_stores(partner_id)")
        conn.commit()
    finally:
        conn.close()


# ── storage: the database's size and the room left (ONB-1) ──────────────────

# What one archived store-day costs, lines and tickets and their indexes
# (scale audit A8: ~613 KB measured from the archive's own tables).
BACKFILL_BYTES_PER_STORE_DAY = int(os.getenv("POS_BACKFILL_BYTES_PER_STORE_DAY", str(650 * 1000)))
# How many stores may backfill history at once (ONB-1). A wave of new stores
# queues behind it, newest-admitted last. An operator's POS_BACKFILL_MAX_STORES
# is kept as given; unset, the cap is sized from the RPOWER fleet budget
# (backfill_cap — scale re-audit 10/20/26 R8-08: a fixed 8 finished 1.6
# stores a night, so a 1,500-store rollout took years to show "History
# loaded"). The storage check below bounds it either way.
BACKFILL_MAX_STORES = (max(1, int(os.getenv("POS_BACKFILL_MAX_STORES")))
                       if (os.getenv("POS_BACKFILL_MAX_STORES") or "").strip() else None)
# The budget's inputs: the share of RPOWER_FLEET_RPS history may take over
# the night's archive window, and what one archived store-day costs in
# requests (tickets, lines, punches and payments pages — ~835 for the
# 60-day onboarding read, pos_archive; ASSUMPTION until pos_request_ledger
# measures it per store-night). The floor is the old fixed 8; the ceiling
# keeps one night's history writes inside what the volume check can judge.
BACKFILL_WINDOW_HOURS = float(os.getenv("POS_BACKFILL_WINDOW_HOURS", "3") or 3)
BACKFILL_FLEET_SHARE = 0.5
BACKFILL_REQUESTS_PER_STORE_DAY = max(1, int(os.getenv("POS_BACKFILL_REQUESTS_PER_STORE_DAY", "15") or 15))
BACKFILL_CAP_FLOOR = 8
BACKFILL_CAP_CEILING = 200
# A slot whose store archived no day for this many nights (a revoked token,
# a POS that fails every night) is released, so it can never hold a slot a
# working store needs (R8-09); it queues for a slot again on its next night.
BACKFILL_SLOT_STALL_NIGHTS = 3
# The console counts slots held longer than this (a slot should finish in
# about two weeks at 7 days a night).
BACKFILL_SLOT_OLD_DAYS = 30


def backfill_cap() -> int:
    """The backfill slot cap: POS_BACKFILL_MAX_STORES when set, else the
    stores the RPOWER fleet budget can backfill a night — RPOWER_FLEET_RPS x
    BACKFILL_FLEET_SHARE over BACKFILL_WINDOW_HOURS, over
    BACKFILL_REQUESTS_PER_STORE_DAY x the days a store takes a night —
    between BACKFILL_CAP_FLOOR and BACKFILL_CAP_CEILING (the ceiling when
    the fleet budget is off)."""
    if BACKFILL_MAX_STORES:
        return int(BACKFILL_MAX_STORES)
    try:
        import pos_archive
        import rpower
        rps, per_night = float(rpower.FLEET_RPS or 0), int(pos_archive.BACKFILL_DAYS_PER_NIGHT)
    except Exception:
        return BACKFILL_CAP_FLOOR
    if rps <= 0:
        return BACKFILL_CAP_CEILING
    stores = rps * BACKFILL_FLEET_SHARE * BACKFILL_WINDOW_HOURS * 3600 / (BACKFILL_REQUESTS_PER_STORE_DAY * per_night)
    return int(max(BACKFILL_CAP_FLOOR, min(BACKFILL_CAP_CEILING, stores)))
# The Postgres volume's size, when it is known (Railway shows it; the app
# cannot see another service's disk): past PG_VOLUME_FILL_LIMIT of it a
# backfill waits. 0 = not set: only the app volume is checked.
PG_VOLUME_MAX_BYTES = int(float(os.getenv("PG_VOLUME_MAX_BYTES", "0") or 0))
PG_VOLUME_FILL_LIMIT = 0.8


def _pg_bytes(db_path=None):
    """pg_database_size when the connection get_conn gives is Postgres, else
    None (the connection decides, not the environment: a test or a script
    may route one path to Postgres)."""
    conn = get_conn(db_path)
    try:
        import db_dialect
        if not db_dialect.is_postgres(conn):
            return None
        return int(conn.execute_native("SELECT pg_database_size(current_database())").fetchone()[0] or 0)
    finally:
        conn.close()


def live_database_bytes(db_path=None) -> int:
    """The live database's size in bytes on either backend: the SQLite file
    and its WAL, or pg_database_size on Postgres (DB_PATH there is the file
    left behind by the cutover, which never grows)."""
    pg = _pg_bytes(db_path)
    if pg is not None:
        return pg
    path = db_path or _models_mod.DB_PATH
    total = 0
    for p in (path, path + "-wal"):
        try:
            total += os.path.getsize(p)
        except OSError:
            pass
    return total


def storage_room(projected_bytes, db_path=None) -> dict:
    """Whether the volumes have room for `projected_bytes` more data AND the
    nightly backup that must follow it: {ok, reason, free_bytes, db_bytes,
    need_bytes}. The backup refuses to start below BACKUP_FREE_SPACE_FACTOR
    times the database free (scheduler.backup_db) — on Postgres the export
    it takes lands on the app volume too — so a backfill that left less
    than that would break tomorrow's backup."""
    import shutil
    import scheduler
    # The whole run's peak (scale re-audit 10/20/26 R8-03), not the local
    # stage's 1.5x: a backfill that left room for the snapshot but not the
    # encrypted off-site copy failed the next night's backup.
    factor = float(getattr(scheduler, "BACKUP_PEAK_SPACE_FACTOR", 2.75))
    try:
        on_pg = _pg_bytes(db_path) is not None
        db_bytes = live_database_bytes(db_path)
    except Exception as e:
        return {"ok": False, "reason": f"database size unreadable: {e}"[:200]}
    path = db_path or _models_mod.DB_PATH
    try:
        free = shutil.disk_usage(os.path.dirname(os.path.abspath(path)) or ".").free
    except OSError as e:
        return {"ok": False, "reason": f"free space unreadable: {e}"[:200]}
    projected = max(0, int(projected_bytes or 0))
    need = int(factor * (db_bytes + projected)) + (0 if on_pg else projected)
    out = {"ok": free >= need, "free_bytes": int(free), "db_bytes": int(db_bytes), "need_bytes": need,
           "reason": None}
    if not out["ok"]:
        out["reason"] = (f"{free // 1048576} MB free on the volume; the backfill and the backup after it need "
                         f"{need // 1048576} MB")
    elif on_pg and PG_VOLUME_MAX_BYTES:
        ceiling = int(PG_VOLUME_MAX_BYTES * PG_VOLUME_FILL_LIMIT)
        if db_bytes + projected > ceiling:
            out.update(ok=False, reason=f"the Postgres database ({db_bytes // 1048576} MB) would pass "
                                        f"{int(PG_VOLUME_FILL_LIMIT * 100)}% of its {PG_VOLUME_MAX_BYTES // 1048576} "
                                        "MB volume")
    return out


# ── backfill slots (ONB-1) ───────────────────────────────────────────────────

def _finish_slot(conn, restaurant_id, provider):
    conn.execute("UPDATE pos_backfill_slots SET finished_at=?, held_reason=NULL, held_at=NULL "
                 "WHERE restaurant_id=? AND provider=? AND finished_at IS NULL",
                 (_now(), int(restaurant_id), provider))


def _slot_stale(conn, restaurant_id, provider, admitted_at, held_reason):
    """Why an active slot should be released, or None (R8-09): its store no
    longer has `provider` connected (disconnected, churned, token removed),
    or it has archived no day in BACKFILL_SLOT_STALL_NIGHTS nights since it
    was admitted (a revoked token, a POS failing every night). A slot held
    for room is never stale — room is the fleet's problem, not the store's."""
    if held_reason:
        return None
    try:
        import pos_archive
        name, _mod = pos_archive.provider_for(restaurant_id)
    except Exception:
        name = provider
    if name != provider:
        return "released: the store no longer has its POS connected"
    since = (datetime.now(timezone.utc) - timedelta(days=BACKFILL_SLOT_STALL_NIGHTS)).strftime("%Y-%m-%d %H:%M:%S")
    if str(admitted_at or "") > since:
        return None
    hit = conn.execute("SELECT 1 FROM pos_archive_days WHERE restaurant_id=? AND provider=? AND archived_at >= ? "
                       "LIMIT 1", (int(restaurant_id), provider, since)).fetchone()
    if hit:
        return None
    return f"released: no day archived in {BACKFILL_SLOT_STALL_NIGHTS} nights"


def release_stale_slots(conn) -> list:
    """Release every stale active slot (_slot_stale) in the caller's write
    transaction: finished, with why in held_reason (the store's next night
    queues for a slot again, as a new store would). Returns the released
    [(restaurant_id, provider, why)]."""
    out = []
    for r in conn.execute("SELECT restaurant_id, provider, admitted_at, held_reason FROM pos_backfill_slots "
                          "WHERE finished_at IS NULL").fetchall():
        why = _slot_stale(conn, r["restaurant_id"], r["provider"], r["admitted_at"], r["held_reason"])
        if why:
            conn.execute("UPDATE pos_backfill_slots SET finished_at=?, held_reason=?, held_at=? "
                         "WHERE restaurant_id=? AND provider=? AND finished_at IS NULL",
                         (_now(), why, _now(), int(r["restaurant_id"]), r["provider"]))
            out.append((int(r["restaurant_id"]), r["provider"], why))
    if out:
        log.warning("backfill: released %d stale slot(s): %s", len(out), out)
    return out


def backfill_admission(restaurant_id, provider, missing_days, days_tonight, db_path=None) -> dict:
    """May this store backfill tonight? {admit, reason, slots_used, cap}.

    A store with nothing missing finishes its slot. One already holding a
    slot keeps it; another takes one only while fewer than backfill_cap()
    are active — counted and taken in ONE write transaction, so concurrent
    archive rows cannot pass the cap (scale re-audit 10/20/26 R8-21). With
    every slot taken, the stale ones are released first (release_stale_slots,
    R8-09): a disconnected or failing store never holds a slot for good.
    Either way the volumes must have room for tonight's days and the backup
    after them (storage_room) — a store held for room keeps its slot, so the
    wave resumes in order. Never raises: an unreadable ledger admits (the
    archive's own bound stands)."""
    rid, cap = int(restaurant_id), backfill_cap()
    try:
        conn = get_conn(db_path)
    except Exception as e:
        log.warning("backfill admission unreadable (%s); admitting %s", e, rid)
        return {"admit": True, "reason": "ledger unreadable", "cap": cap}
    try:
        if missing_days <= 0:
            _finish_slot(conn, rid, provider)
            conn.commit()
            return {"admit": False, "reason": "complete", "cap": cap}
        import db_dialect
        db_dialect.begin_write(conn, "pos_backfill_slots", "admit")
        row = conn.execute("SELECT finished_at FROM pos_backfill_slots WHERE restaurant_id=? AND provider=?",
                           (rid, provider)).fetchone()
        holding = row is not None and row["finished_at"] is None
        used = int(conn.execute("SELECT COUNT(*) FROM pos_backfill_slots WHERE finished_at IS NULL")
                   .fetchone()[0] or 0)
        if not holding and used >= cap:
            if release_stale_slots(conn):
                used = int(conn.execute("SELECT COUNT(*) FROM pos_backfill_slots WHERE finished_at IS NULL")
                           .fetchone()[0] or 0)
        if not holding:
            if used >= cap:
                conn.commit()                 # any release above stands
                waiting = {"admit": False, "reason": "waiting_for_slot", "slots_used": used, "cap": cap}
                return waiting
            if row is None:
                conn.execute("INSERT INTO pos_backfill_slots (restaurant_id, provider, admitted_at) VALUES (?,?,?) "
                             "ON CONFLICT(restaurant_id, provider) DO NOTHING", (rid, provider, _now()))
            else:
                conn.execute("UPDATE pos_backfill_slots SET admitted_at=?, finished_at=NULL, held_reason=NULL, "
                             "held_at=NULL WHERE restaurant_id=? AND provider=?", (_now(), rid, provider))
            used += 1
        conn.commit()
        room = storage_room(max(0, int(days_tonight)) * BACKFILL_BYTES_PER_STORE_DAY, db_path=db_path)
        if not room.get("ok"):
            conn.execute("UPDATE pos_backfill_slots SET held_reason=?, held_at=? WHERE restaurant_id=? AND provider=?",
                         (str(room.get("reason") or "no room")[:300], _now(), rid, provider))
            conn.commit()
            _page_no_room(room)
            return {"admit": False, "reason": "no_room", "detail": room.get("reason"), "slots_used": used,
                    "cap": cap}
        conn.execute("UPDATE pos_backfill_slots SET held_reason=NULL, held_at=NULL WHERE restaurant_id=? "
                     "AND provider=? AND held_reason IS NOT NULL", (rid, provider))
        conn.commit()
        return {"admit": True, "reason": None, "slots_used": used, "cap": cap}
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        log.warning("backfill admission failed for %s (%s); admitting", rid, e)
        return {"admit": True, "reason": f"admission failed: {e}"[:200], "cap": cap}
    finally:
        conn.close()


def _page_no_room(room):
    """Tell the on-call once in 12 hours that history backfill is held for
    room — on Railway only (the scheduler's rule for every send)."""
    try:
        import scheduler
        if not scheduler.scheduling_allowed():
            return
        import ops
        ops.page_operator("backfill_no_room", "Cavnar AI: POS history backfill is held for disk space",
                          [str(room.get("reason") or ""),
                           "New stores' history waits; last night still archives. Grow the volume "
                           "(Railway Pro: self-serve to 1 TB), then the next night resumes the wave."],
                          cooldown_minutes=12 * 60)
    except Exception as e:
        log.warning("backfill no-room page failed: %s", e)


def backfill_state(db_path=None) -> dict:
    """The slots for the console: {cap, active: [{restaurant_id, provider,
    admitted_at, held_reason}], used}."""
    conn = get_conn(db_path)
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT restaurant_id, provider, admitted_at, held_reason, held_at FROM pos_backfill_slots "
            "WHERE finished_at IS NULL ORDER BY admitted_at")]
    finally:
        conn.close()
    old = (datetime.now(timezone.utc) - timedelta(days=BACKFILL_SLOT_OLD_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    return {"cap": backfill_cap(), "used": len(rows), "active": rows,
            "bytes_per_store_day": BACKFILL_BYTES_PER_STORE_DAY,
            # Slots held past BACKFILL_SLOT_OLD_DAYS (R8-09): a history that
            # should have finished in two weeks — the console counts them.
            "old": sum(1 for r in rows if str(r.get("admitted_at") or "") < old),
            "old_after_days": BACKFILL_SLOT_OLD_DAYS}


# ── setup confirmations (ONB-11, ONB-12) ─────────────────────────────────────

# The judgment calls a person makes once per store, in the order a setup
# call takes them. Each is a confirmation: the data's suggestion (where there
# is one) shown, a person's yes recorded. `labor` steps apply to a Labor
# restaurant, `dsr` to one with the daily report on.
SETUP_STEPS = (
    ("week_start", "Payroll week start confirmed", "labor"),
    ("roles_rates", "Roles and pay rates confirmed ($0 in the POS is not entered)", "labor"),
    ("salaried", "Salaried managers entered", "labor"),
    ("station_logins", "Station logins excluded", "labor"),
    ("floors_closers", "Floors, closers and leader rules confirmed", "labor"),
    ("availability", "Staff availability entered", "labor"),
    ("dsr_calendar", "DSR fiscal calendar confirmed", "dsr"),
)
SETUP_STEP_KEYS = tuple(k for k, _l, _s in SETUP_STEPS)
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def start_setup(restaurant_id, db_path=None) -> None:
    """Open the setup checklist for a new store: every SETUP_STEPS row
    pending (the ones that do not apply to it are never shown). A store
    with no rows was set up by hand before the checklist existed and is not
    asked (setup_started) — deterministic, never a date. Called by the New
    client form, bulk provisioning and an RPOWER bind."""
    conn = get_conn(db_path)
    try:
        conn.executemany("INSERT INTO onboarding_confirmations (restaurant_id, step, status) VALUES (?, ?, 'pending') "
                         "ON CONFLICT(restaurant_id, step) DO NOTHING",
                         [(int(restaurant_id), k) for k in SETUP_STEP_KEYS])
        conn.commit()
    finally:
        conn.close()


def setup_started(facts) -> bool:
    """Whether this store's setup checklist was opened (start_setup)."""
    return bool((facts or {}).get("confirmations"))


def suggest(restaurant_id, step, suggested, source, db_path=None) -> None:
    """Record what the data suggests for `step` as PENDING — never over a
    confirmation a person already gave."""
    conn = get_conn(db_path)
    try:
        conn.execute("INSERT INTO onboarding_confirmations (restaurant_id, step, status, suggested_json, source) "
                     "VALUES (?, ?, 'pending', ?, ?) ON CONFLICT(restaurant_id, step) DO UPDATE SET "
                     "suggested_json=excluded.suggested_json, source=excluded.source "
                     "WHERE onboarding_confirmations.status='pending'",
                     (int(restaurant_id), step, json.dumps(suggested), source))
        conn.commit()
    finally:
        conn.close()


def confirmations(restaurant_id, db_path=None) -> dict:
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT step, status, suggested_json, value_json, source, confirmed_by, confirmed_at "
                            "FROM onboarding_confirmations WHERE restaurant_id=?", (int(restaurant_id),)).fetchall()
    finally:
        conn.close()
    out = {}
    for r in rows:
        d = dict(r)
        for k in ("suggested_json", "value_json"):
            try:
                d[k[:-5]] = json.loads(d.pop(k)) if d.get(k) else None
            except (TypeError, ValueError):
                d[k[:-5]] = None
        out[d["step"]] = d
    return out


def confirm(restaurant_id, step, value=None, who=None, db_path=None) -> dict:
    """A person's yes to `step` (with `value` where the step carries one).
    week_start applies its day to the restaurant (update_restaurant) — the
    one confirmation that changes a setting; the others record that the
    judgment was made where the setting itself lives (Team, Schedule
    Studio, the DSR settings). Returns {ok, error?, applied?}."""
    if step not in SETUP_STEP_KEYS:
        return {"ok": False, "error": f"unknown setup step {step!r}"}
    applied = {}
    if step == "week_start":
        if value is None:
            pending = confirmations(restaurant_id, db_path).get("week_start") or {}
            value = (pending.get("suggested") or {}).get("week_start_day")
        try:
            day = int(value["week_start_day"] if isinstance(value, dict) else value)
        except (TypeError, ValueError, KeyError):
            day = -1
        if not 0 <= day <= 6:
            return {"ok": False, "error": "Pick the day the payroll week starts."}
        _models_mod.update_restaurant(int(restaurant_id), {"week_start_day": day},
                                      **({"db_path": db_path} if db_path else {}))
        value, applied = {"week_start_day": day, "label": _WEEKDAYS[day]}, {"week_start_day": day}
    conn = get_conn(db_path)
    try:
        conn.execute("INSERT INTO onboarding_confirmations (restaurant_id, step, status, value_json, confirmed_by, "
                     "confirmed_at) VALUES (?, ?, 'confirmed', ?, ?, ?) ON CONFLICT(restaurant_id, step) DO UPDATE "
                     "SET status='confirmed', value_json=excluded.value_json, confirmed_by=excluded.confirmed_by, "
                     "confirmed_at=excluded.confirmed_at",
                     (int(restaurant_id), step, json.dumps(value) if value is not None else None,
                      (who or "admin")[:120], _now()))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "applied": applied, "value": value}


def setup_items(r, facts=None) -> list:
    """The store's setup confirmations as [{key, label, done, suggested,
    confirmed_at, confirmed_by}] — the steps that apply to it."""
    facts = facts or {}
    conf = facts.get("confirmations") or {}
    out = []
    for key, label, scope in SETUP_STEPS:
        if scope == "labor" and not r.get("module_labor"):
            continue
        if scope == "dsr" and not r.get("dsr_enabled"):
            continue
        c = conf.get(key) or {}
        out.append({"key": key, "label": label, "done": c.get("status") == "confirmed",
                    "suggested": c.get("suggested"), "confirmed_at": c.get("confirmed_at"),
                    "confirmed_by": c.get("confirmed_by")})
    return out


# The steps an OWNER may confirm from the app (scale re-audit 10/20/26
# R8-11): the labor judgments that hold the auto-draft. Each was an admin's
# confirmation only, so every labor store's first draft waited on a Cavnar AI
# operator — ~1.5-3 stores a day per person. The DSR calendar has the
# owner's own settings already; the admin's path stays for assisted setups.
OWNER_STEPS = tuple(k for k, _l, scope in SETUP_STEPS if scope == "labor")


def owner_setup(r, db_path=None) -> dict:
    """What the owner's Account shows (R8-11): {items: [{key, label, done,
    suggested, confirmed_at, confirmed_by}], pending: [keys], holds_auto_draft}
    for a Labor restaurant whose checklist was opened; {items: []}
    otherwise. `r` is a Restaurant or a dict with id and module_labor."""
    row = r if isinstance(r, dict) else {"id": getattr(r, "id", None),
                                         "module_labor": getattr(r, "module_labor", 0)}
    if not row.get("module_labor") or row.get("id") is None:
        return {"items": [], "pending": [], "holds_auto_draft": False}
    conf = confirmations(row["id"], db_path)
    if not conf:
        return {"items": [], "pending": [], "holds_auto_draft": False}
    items = [i for i in setup_items({"module_labor": 1}, {"confirmations": conf}) if i["key"] in OWNER_STEPS]
    pending = [i["key"] for i in items if not i["done"]]
    return {"items": items, "pending": pending, "holds_auto_draft": bool(pending)}


def labor_setup_pending(r, db_path=None) -> list:
    """The labor setup confirmations still open for a restaurant (an object
    with the Restaurant fields, or a dict) — [] for one whose setup
    checklist was never opened (set up by hand before it existed). The
    auto-draft waits while any is open (ONB-12)."""
    row = r if isinstance(r, dict) else {"module_labor": getattr(r, "module_labor", 0),
                                         "id": getattr(r, "id", None)}
    if not row.get("module_labor") or row.get("id") is None:
        return []
    conf = confirmations(row["id"], db_path)
    if not conf:
        return []
    return [k for k, _l, scope in SETUP_STEPS if scope == "labor"
            and (conf.get(k) or {}).get("status") != "confirmed"]


# ── the tracker's steps and the fleet board (ONB-9) ─────────────────────────

ARCHIVE_HISTORY_DAYS = 90
# The board's history step is DONE at this many archived days (scale
# re-audit 10/20/26 R8-08): what the onboarding read archives in one go
# (pos_archive.ONBOARD_DAYS) and enough for every reader; the rest of the 90
# fills in the background and is shown as "N days to go", never as a store
# stuck on "History loading".
ARCHIVE_HISTORY_DONE_DAYS = 60
DSR_HISTORY_DAYS = 60
# A store not live this many days after signing up is stuck on the board.
ONBOARDING_STUCK_DAYS = int(os.getenv("ONBOARDING_STUCK_DAYS", "14"))


def fleet_facts(conn, rests) -> dict:
    """{restaurant_id: facts} for the tracker's onboarding steps, in a few
    grouped reads over the whole fleet (admin_ops builds every location's
    record from one pass): archived days in the last 90, DSR nights filled
    in the last 60, checklists, confirmations, backfill slots, partner.
    A table this database lacks reads as nothing."""
    out = {}
    today = datetime.now(timezone.utc).date()

    def _rows(sql, params=()):
        try:
            return conn.execute(sql, params).fetchall()
        except Exception as e:
            log.warning("onboarding facts read failed (%s): %s", sql.split()[3] if len(sql.split()) > 3 else sql, e)
            return []
    for r in _rows("SELECT restaurant_id, COUNT(*) AS n FROM pos_archive_days WHERE business_date >= ? "
                   "GROUP BY restaurant_id", ((today - timedelta(days=ARCHIVE_HISTORY_DAYS)).isoformat(),)):
        out.setdefault(r["restaurant_id"], {})["archive_days"] = int(r["n"] or 0)
    dsr_ids = [int(x["id"]) for x in (rests or []) if x.get("dsr_enabled")]
    if dsr_ids:
        marks = ",".join("?" * len(dsr_ids))
        for r in _rows(f"SELECT restaurant_id, COUNT(DISTINCT business_date) AS n FROM dsr_metrics "
                       f"WHERE restaurant_id IN ({marks}) AND business_date >= ? AND metric='sales.net' "
                       f"GROUP BY restaurant_id",
                       (*dsr_ids, (today - timedelta(days=DSR_HISTORY_DAYS)).isoformat())):
            out.setdefault(r["restaurant_id"], {})["dsr_nights"] = int(r["n"] or 0)
    for r in _rows("SELECT restaurant_id, COUNT(*) AS n FROM task_sheets GROUP BY restaurant_id"):
        out.setdefault(r["restaurant_id"], {})["checklists"] = int(r["n"] or 0)
    for r in _rows("SELECT restaurant_id, step, status, suggested_json, confirmed_at, confirmed_by "
                   "FROM onboarding_confirmations"):
        try:
            sug = json.loads(r["suggested_json"]) if r["suggested_json"] else None
        except (TypeError, ValueError):
            sug = None
        out.setdefault(r["restaurant_id"], {}).setdefault("confirmations", {})[r["step"]] = {
            "status": r["status"], "suggested": sug, "confirmed_at": r["confirmed_at"],
            "confirmed_by": r["confirmed_by"]}
    for r in _rows("SELECT restaurant_id, admitted_at, held_reason FROM pos_backfill_slots WHERE finished_at IS NULL"):
        out.setdefault(r["restaurant_id"], {})["backfill_slot"] = {"admitted_at": r["admitted_at"],
                                                                   "held_reason": r["held_reason"]}
    for r in _rows("SELECT ps.restaurant_id, p.id AS partner_id, p.name, p.billing_mode, "
                   "p.master_agreement_signed_at FROM partner_stores ps JOIN partners p ON p.id = ps.partner_id"):
        out.setdefault(r["restaurant_id"], {})["partner"] = dict(r)
    return out


def _has_pos(r) -> bool:
    return bool(r.get("rpower_token") or r.get("rpower_store_mid")
                or (r.get("pos_system") or "").strip().upper() == "RPOWER")


def pos_steps(r, facts=None) -> list:
    """The POS steps of the tracker for an RPOWER store (ONB-9): bound to its
    store, first data in, the archive's 90 days of history, and — with the
    daily report on — its 60 nights of history."""
    if not _has_pos(r):
        return []
    facts = facts or {}
    archived = int(facts.get("archive_days") or 0)
    left = max(0, (ARCHIVE_HISTORY_DAYS - 1) - archived)
    usable = archived >= ARCHIVE_HISTORY_DONE_DAYS
    slot = facts.get("backfill_slot") or {}
    hist = "POS history archived" + (f" ({left} days to go" + (
        f"; held: {slot['held_reason']}" if slot.get("held_reason") else
        ("" if slot or usable else "; waiting for a backfill slot")) + ")" if left else "")
    steps = [
        {"key": "pos_bound", "label": "RPOWER store bound", "done": bool(r.get("rpower_store_mid"))},
        {"key": "pos_first_data", "label": "First POS data in", "done": bool(r.get("rpower_last_synced"))},
        {"key": "pos_history", "label": hist, "done": left == 0 or usable, "left": left},
    ]
    if r.get("dsr_enabled"):
        nights = int(facts.get("dsr_nights") or 0)
        dleft = max(0, (DSR_HISTORY_DAYS - 1) - nights)
        steps.append({"key": "dsr_history", "label": "DSR history filled" + (f" ({dleft} nights to go)" if dleft else ""),
                      "done": dleft == 0, "left": dleft})
    return steps


def extra_steps(r, facts=None) -> list:
    """Everything this module adds to admin_ops._onboarding_for's steps."""
    facts = facts or {}
    steps = pos_steps(r, facts)
    # A restaurant set up by hand before the checklist existed has no setup
    # rows and is not asked again: its checklist and confirmation steps are
    # left off its tracker (not counted done — its step count is what it was).
    if not setup_started(facts):
        return steps
    if r.get("module_labor"):
        steps.append({"key": "checklists", "label": "Checklists loaded",
                      "done": int(facts.get("checklists") or 0) > 0})
    steps += [{"key": f"setup:{s['key']}", "label": s["label"], "done": s["done"]} for s in setup_items(r, facts)]
    return steps


def partner_covers(facts, step) -> bool:
    """With PARTNER_BILLING=1, a store under a partner whose master agreement
    is signed needs no contract of its own, and its billing is the
    partner's (ONB-10). Off by default: nothing changes."""
    if not partner_billing_enabled():
        return False
    p = (facts or {}).get("partner") or {}
    if not p or p.get("billing_mode") != "partner" or not p.get("master_agreement_signed_at"):
        return False
    return step in ("contract", "billing")


# Stages in order: where a store is is the first one not done.
STAGES = (("contract", "Contract", ("contract",)),
          ("access", "Owner signed in", ("login",)),
          ("pos", "POS connected", ("pos_bound", "pos_first_data")),
          ("history", "History loading", ("pos_history", "dsr_history")),
          ("setup", "Setup to confirm", None),
          ("live", "Live", ()))


def stage_of(steps) -> str:
    by = {s["key"]: s for s in steps}
    for key, _label, keys in STAGES:
        if key == "live":
            return "live"
        if keys is None:
            pending = [s for s in steps if (s["key"].startswith("setup:") or s["key"] == "checklists") and not s["done"]]
        else:
            pending = [by[k] for k in keys if k in by and not by[k]["done"]]
        if pending:
            return key
    return "live"


def board(rows) -> dict:
    """The fleet onboarding board (ONB-9): how many stores sit at each stage,
    and the stuck ones — not live ONBOARDING_STUCK_DAYS after signing up,
    oldest first. `rows` are the Onboarding tab's rows (steps, days)."""
    counts = {k: 0 for k, _l, _s in STAGES}
    stuck = []
    for r in rows:
        st = stage_of(r.get("steps") or [])
        counts[st] = counts.get(st, 0) + 1
        r["stage"] = st
        if st != "live" and int(r.get("days_since_signup") or 0) > ONBOARDING_STUCK_DAYS:
            stuck.append({"id": r.get("id"), "name": r.get("name"), "stage": st,
                          "days": int(r.get("days_since_signup") or 0)})
    stuck.sort(key=lambda x: -x["days"])
    return {"stages": [{"key": k, "label": l, "count": counts.get(k, 0)} for k, l, _s in STAGES],
            "stuck": stuck, "stuck_after_days": ONBOARDING_STUCK_DAYS}


# ── partners (ONB-10) ────────────────────────────────────────────────────────

def partner_billing_enabled() -> bool:
    """PARTNER_BILLING=1 turns the partner entity's billing on. Off by
    default: a partner can be recorded and stores attached, and nothing
    about a store's contract or billing changes."""
    return (os.getenv("PARTNER_BILLING") or "").strip() == "1"


PARTNER_BILLING_MODES = ("direct", "partner")


def create_partner(name, billing_mode="direct", contact_email=None, master_agreement_ref=None,
                   master_agreement_signed_at=None, db_path=None) -> int:
    name = (name or "").strip()
    if not name:
        raise ValueError("A partner needs a name.")
    if billing_mode not in PARTNER_BILLING_MODES:
        raise ValueError("Billing is direct (each store contracts and pays) or partner (the partner does).")
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT id FROM partners WHERE name=?", (name[:200],)).fetchone()
        if row:
            conn.execute("UPDATE partners SET billing_mode=?, contact_email=COALESCE(?, contact_email), "
                         "master_agreement_ref=COALESCE(?, master_agreement_ref), "
                         "master_agreement_signed_at=COALESCE(?, master_agreement_signed_at) WHERE id=?",
                         (billing_mode, contact_email, master_agreement_ref, master_agreement_signed_at, row["id"]))
            conn.commit()
            return int(row["id"])
        cur = conn.execute("INSERT INTO partners (name, billing_mode, contact_email, master_agreement_ref, "
                           "master_agreement_signed_at) VALUES (?,?,?,?,?)",
                           (name[:200], billing_mode, contact_email, master_agreement_ref, master_agreement_signed_at))
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def partners(db_path=None) -> list:
    conn = get_conn(db_path)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT p.id, p.name, p.billing_mode, p.master_agreement_signed_at, p.master_agreement_ref, "
            "p.contact_email, (SELECT COUNT(*) FROM partner_stores s WHERE s.partner_id = p.id) AS stores "
            "FROM partners p ORDER BY p.name")]
    finally:
        conn.close()


def attach_store(partner_id, restaurant_id, conn=None, db_path=None) -> None:
    own = conn is None
    conn = conn or get_conn(db_path)
    try:
        conn.execute("INSERT INTO partner_stores (restaurant_id, partner_id) VALUES (?, ?) "
                     "ON CONFLICT(restaurant_id) DO UPDATE SET partner_id=excluded.partner_id",
                     (int(restaurant_id), int(partner_id)))
        if own:
            conn.commit()
    finally:
        if own:
            conn.close()


def partner_for(restaurant_id, db_path=None):
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT p.* FROM partner_stores s JOIN partners p ON p.id = s.partner_id "
                           "WHERE s.restaurant_id=?", (int(restaurant_id),)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def skips_direct_contract(restaurant_id, db_path=None) -> bool:
    """True when this store is billed by a partner under a signed master
    agreement and PARTNER_BILLING is on: no DocuSign envelope and no
    checkout of its own (ONB-10)."""
    if not partner_billing_enabled():
        return False
    p = partner_for(restaurant_id, db_path)
    return bool(p and p.get("billing_mode") == "partner" and p.get("master_agreement_signed_at"))
