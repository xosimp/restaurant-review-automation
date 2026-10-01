"""pos_archive — the ticket-level POS archive.

RPower endpoint audit (9/29/26), Critical #2. Every night Cavnar AI read each
ticket, sale line and time punch, summed them into daily totals (the DSR,
labor_daily_history, pos_loss_daily) and threw the detail away: who served
which table, each comp's reason, each punch's pay and tips. Every learner
that should measure people, tables, prices or minutes needs that history,
and a competitor starting later cannot have it. RPOWER itself asks
integrators to archive, a week per request.

Tables (models.init_db): pos_tickets, pos_ticket_lines, pos_punches, one
row per POS record, ids as the POS keys them; pos_archive_days, what each
archived day holds; pos_archive_state, how far the change read has looked.

A provider takes part by defining `archive_rows(restaurant_id, day)`
(rpower.archive_rows) and `changed_business_dates(restaurant_id, since_utc,
until_utc)`. Nightly (run_nightly, the `pos_archive` job):

  1. yesterday's business date;
  2. any archived date the POS rewrote since the last look (a re-post after
     close) — archived again and counted as a restatement;
  3. a bounded backfill toward BACKFILL_DAYS, BACKFILL_DAYS_PER_NIGHT at a
     time, newest first.

A day is replaced whole, in one transaction: a re-read never leaves a line
the POS has since removed.
"""
import logging
from datetime import date, datetime, timedelta, timezone

import models as _models_mod
from models import DB_PATH


def get_conn(db_path=None):
    """models.get_conn, resolved at call time — CLAUDE.md's bound-import
    hazard. A bound `from models import get_conn` kept whichever function
    was there when this module was first imported, so the nightly report's
    at-report-time read (dsr.common.night_archive) wrote a test's checks to
    another test's database."""
    return _models_mod.get_conn(db_path) if db_path is not None else _models_mod.get_conn()

log = logging.getLogger("pos_archive")

# The archive's layout. Raise it when a table joins the archive: every day
# stored under an older layout counts as not archived and the backfill stores
# it again (2: payments and payouts; 3: the raw fields the POS reads derive
# from, so they read a closed day from here — both 9/29/26).
ARCHIVE_VERSION = 3

BACKFILL_DAYS = 90
BACKFILL_DAYS_PER_NIGHT = 7
# The change read looks back this far the first time (no state yet).
FIRST_CHANGE_LOOKBACK_HOURS = 48

_TICKET_COLS = ("ticket_id", "business_date", "ticket_no", "opened_at", "closed_at", "fired_at", "bumped_at",
                "need_at", "server_id", "server_name", "table_id", "table_name", "room_name", "is_bar",
                "guest_count", "entree_count", "bev_count", "net_sales", "discount", "tip", "grat", "tax",
                "mealtime", "profit_center", "cancelled", "source_stamp", "type_sale", "type_discnt", "type_promo",
                "type_comp")
_LINE_COLS = ("line_id", "ticket_id", "business_date", "item_id", "item_name", "item_kind", "kind", "qty",
              "sales", "price", "regular_price", "loss_amount", "reason", "approver_id", "item_at", "mealtime",
              "profit_center", "price_level_id", "source_stamp", "sales_type_id", "voided", "mgr_id",
              "void_mgr_id", "reason_id", "shift")
_PUNCH_COLS = ("punch_id", "business_date", "employee_id", "employee_name", "job_id", "role", "clock_in",
               "clock_out", "reg_hours", "ot_hours", "dt_hours", "reg_rate", "ot_rate", "pay", "ot_pay", "tips",
               "tips_net", "grats", "break_minutes", "meal_minutes", "rest_minutes", "edited_by", "edited_at",
               "edit_what", "is_station", "source_stamp")
_PAYMENT_COLS = ("payment_id", "ticket_id", "business_date", "method", "is_cash", "is_card", "amount", "tip",
                 "tip_fee")
_PAYOUT_COLS = ("payout_id", "business_date", "category", "is_payin", "amount", "manager_id", "manager_name",
                "paid_at", "reference")
_TABLES = (("pos_tickets", _TICKET_COLS, "tickets"), ("pos_ticket_lines", _LINE_COLS, "lines"),
           ("pos_punches", _PUNCH_COLS, "punches"), ("pos_payments", _PAYMENT_COLS, "payments"),
           ("pos_payouts", _PAYOUT_COLS, "payouts"))


def provider_for(restaurant_id):
    """(name, module) of a connected POS that can archive, else (None, None)."""
    import pos
    try:
        name, mod = pos.connected_provider(restaurant_id)
    except Exception:
        return None, None
    if mod is None or not callable(getattr(mod, "archive_rows", None)):
        return None, None
    return name, mod


def archive_day(restaurant_id, day, db_path=DB_PATH, provider=None) -> dict:
    """Read one business date from the POS and store it whole.
    {"date", "tickets", "lines", "punches", "net_sales", "restated"}."""
    name, mod = provider or provider_for(restaurant_id)
    if mod is None:
        raise LookupError("no connected POS can archive tickets")
    rows = mod.archive_rows(restaurant_id, day)
    iso = day.isoformat() if hasattr(day, "isoformat") else str(day)[:10]
    net = round(sum(float(t.get("net_sales") or 0) for t in rows["tickets"]), 2)
    conn = get_conn(db_path)
    try:
        prev = conn.execute("SELECT max_stamp, restated FROM pos_archive_days WHERE restaurant_id=? AND provider=? "
                            "AND business_date=?", (restaurant_id, name, iso)).fetchone()
        for table, cols, key in _TABLES:
            conn.execute(f"DELETE FROM {table} WHERE restaurant_id=? AND provider=? AND business_date=?",
                         (restaurant_id, name, iso))
            marks = ",".join("?" for _ in range(len(cols) + 2))
            conn.executemany(
                f"INSERT OR REPLACE INTO {table} (restaurant_id, provider, {','.join(cols)}) VALUES ({marks})",
                [(restaurant_id, name, *[r.get(c) for c in cols]) for r in rows.get(key) or []])
        restated = bool(prev and prev["max_stamp"] and rows.get("max_stamp")
                        and rows["max_stamp"] != prev["max_stamp"])
        conn.execute(
            "INSERT INTO pos_archive_days (restaurant_id, provider, business_date, tickets, lines, punches, "
            "net_sales, max_stamp, restated, version, archived_at) VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now')) "
            "ON CONFLICT(restaurant_id, provider, business_date) DO UPDATE SET tickets=excluded.tickets, "
            "lines=excluded.lines, punches=excluded.punches, net_sales=excluded.net_sales, "
            "max_stamp=excluded.max_stamp, restated=pos_archive_days.restated + ?, version=excluded.version, "
            "archived_at=datetime('now')",
            (restaurant_id, name, iso, len(rows["tickets"]), len(rows["lines"]), len(rows["punches"]), net,
             rows.get("max_stamp"), 1 if restated else 0, ARCHIVE_VERSION, 1 if restated else 0))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"date": iso, "tickets": len(rows["tickets"]), "lines": len(rows["lines"]),
            "punches": len(rows["punches"]), "net_sales": net, "restated": restated}


def archived_dates(restaurant_id, provider, db_path=DB_PATH) -> set:
    """Dates stored under the current layout (ARCHIVE_VERSION); an older
    day reads as missing, so the backfill stores it again."""
    conn = get_conn(db_path)
    try:
        return {r[0] for r in conn.execute("SELECT business_date FROM pos_archive_days WHERE restaurant_id=? "
                                           "AND provider=? AND COALESCE(version, 1) >= ?",
                                           (restaurant_id, provider, ARCHIVE_VERSION)).fetchall()}
    finally:
        conn.close()


def _changes_checked_to(restaurant_id, provider, db_path):
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT changes_checked_to FROM pos_archive_state WHERE restaurant_id=? AND provider=?",
                           (restaurant_id, provider)).fetchone()
    finally:
        conn.close()
    try:
        return datetime.fromisoformat(row[0]).replace(tzinfo=timezone.utc) if row and row[0] else None
    except ValueError:
        return None


def _set_changes_checked_to(restaurant_id, provider, when, db_path):
    conn = get_conn(db_path)
    try:
        conn.execute("INSERT INTO pos_archive_state (restaurant_id, provider, changes_checked_to) VALUES (?,?,?) "
                     "ON CONFLICT(restaurant_id, provider) DO UPDATE SET changes_checked_to=excluded.changes_checked_to",
                     (restaurant_id, provider, when.strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
    finally:
        conn.close()


def run_for(restaurant_id, today=None, db_path=DB_PATH, now_utc=None) -> dict:
    """One restaurant's night: yesterday, restated days, then backfill.
    `today` is the restaurant's current business date."""
    name, mod = provider_for(restaurant_id)
    if mod is None:
        return {"ok": False, "reason": "no connected POS can archive tickets"}
    if today is None:
        from models import get_restaurant
        from time_utils import restaurant_now, business_date
        r = get_restaurant(restaurant_id)
        today = business_date(r, restaurant_now(r, naive=True))
    now_utc = now_utc or datetime.now(timezone.utc)
    yesterday = today - timedelta(days=1)
    have = archived_dates(restaurant_id, name, db_path)
    todo = [yesterday]
    # Restated days: rewritten in the POS since the last look.
    since = _changes_checked_to(restaurant_id, name, db_path) or (now_utc - timedelta(hours=FIRST_CHANGE_LOOKBACK_HOURS))
    restated_dates = []
    changed = getattr(mod, "changed_business_dates", None)
    if callable(changed):
        try:
            for d in sorted(changed(restaurant_id, since, now_utc)):
                if d in have and d < yesterday.isoformat():
                    restated_dates.append(date.fromisoformat(d))
        except Exception as e:                      # the night's archive still runs
            log.warning("pos_archive: change read failed for %s: %s", restaurant_id, e)
            changed = None
    todo += restated_dates
    # Backfill, newest first, toward BACKFILL_DAYS.
    floor = yesterday - timedelta(days=BACKFILL_DAYS - 1)
    missing = [yesterday - timedelta(days=i) for i in range(1, BACKFILL_DAYS)
               if (yesterday - timedelta(days=i)) >= floor and (yesterday - timedelta(days=i)).isoformat() not in have]
    todo += missing[:BACKFILL_DAYS_PER_NIGHT]
    done, restated, failed = [], 0, []
    for d in dict.fromkeys(todo):
        try:
            out = archive_day(restaurant_id, d, db_path=db_path, provider=(name, mod))
            done.append(out["date"])
            restated += 1 if out["restated"] else 0
        except Exception as e:
            log.warning("pos_archive: %s %s failed: %s", restaurant_id, d, e)
            failed.append(d.isoformat())
    if callable(changed) and not failed:
        _set_changes_checked_to(restaurant_id, name, now_utc, db_path)
    roles = prices = None
    try:
        roles = sync_roles(restaurant_id, db_path=db_path)
    except Exception as e:                           # the archive stands; roles try again tomorrow
        log.warning("pos_archive: role sync failed for %s: %s", restaurant_id, e)
    try:
        prices = sync_prices(restaurant_id, db_path=db_path, today=today)
    except Exception as e:
        log.warning("pos_archive: price sync failed for %s: %s", restaurant_id, e)
    return {"ok": not failed, "archived": done, "restated": restated, "failed": failed,
            "backfill_left": max(0, len(missing) - BACKFILL_DAYS_PER_NIGHT), "provider": name, "roles": roles,
            "prices": prices}


# ── who can work which job (RPower endpoint audit, High ROI #5) ─────────────

ROLE_SOURCE = "sync"


def sync_roles(restaurant_id, db_path=DB_PATH) -> dict:
    """Mirror the POS's job list into people.person_roles, the table every
    who-can-work-what reader uses (the replacement picker, the roster, the
    schedule engine). Only for people Cavnar AI already knows — by their POS
    id (person_aliases), else by one exact name match; roles an owner added are never touched; a POS role
    never overrides a primary role the owner set. A job the POS no longer
    lists is removed only if the POS put it there. No change_log rows."""
    import people
    name, mod = provider_for(restaurant_id)
    fn = getattr(mod, "fetch_employee_jobs", None) if mod else None
    if not callable(fn):
        return {"ok": False, "reason": "the POS does not share job assignments"}
    rows = fn(restaurant_id)
    conn = get_conn(db_path)
    try:
        known = {str(r[0]): r[1] for r in conn.execute(
            "SELECT a.external_id, p.display_name FROM person_aliases a JOIN people p ON p.id = a.person_id "
            "WHERE a.restaurant_id=? AND a.source=? AND a.external_id IS NOT NULL AND p.merged_into IS NULL",
            (restaurant_id, name)).fetchall()}
        # A store whose people carry no POS id yet (Simple EJ's, 9/29/26: no
        # alias rows) is matched on the exact name instead, one live person
        # per name — never a guess between two.
        by_key = {}
        for r in conn.execute("SELECT display_name, name_key FROM people WHERE restaurant_id=? AND merged_into IS NULL",
                              (restaurant_id,)).fetchall():
            by_key.setdefault(r["name_key"], []).append(r["display_name"])
    finally:
        conn.close()
    want = {}
    for r in rows:
        person = known.get(str(r["external_id"]))
        if not person and r.get("employee_name"):
            hits = by_key.get(people._nk(r["employee_name"])) or []
            person = hits[0] if len(hits) == 1 else None
        if person:
            want.setdefault(person, {})[r["role"]] = bool(r.get("primary"))
    held = people.held_roles(restaurant_id, db_path=db_path)
    has_primary = {h["key"] for h in held if h["primary"]}
    mine = {(h["name"], h["role"]) for h in held if h["source"] == ROLE_SOURCE}
    everyone = {(h["key"], h["role"].lower()) for h in held}
    added = removed = 0
    for person, roles in want.items():
        key = people._nk(person)
        for role, primary in roles.items():
            if (key, role.lower()) in everyone:
                continue
            people.add_role(restaurant_id, person, role, primary=primary and key not in has_primary,
                            source=ROLE_SOURCE, db_path=db_path, record=False)
            if primary and key not in has_primary:
                has_primary.add(key)
            added += 1
    for person, role in mine:
        if role not in (want.get(person) or {}):
            removed += 1 if people.remove_role(restaurant_id, person, role, db_path=db_path, record=False) else 0
    return {"ok": True, "people": len(want), "added": added, "removed": removed}


# ── menu prices (RPower endpoint audit, High ROI #7) ────────────────────────

PRICE_LEVEL_LOOKBACK_DAYS = 60
# More price changes than this in one night is a remap or a glitch, not a
# repricing: stored, not logged, and flagged for an operator.
MAX_PRICE_CHANGES_LOGGED = 50


def _levels_in_use(restaurant_id, provider, db_path, today=None):
    since = ((today or date.today()) - timedelta(days=PRICE_LEVEL_LOOKBACK_DAYS)).isoformat()
    conn = get_conn(db_path)
    try:
        return {r[0] for r in conn.execute(
            "SELECT DISTINCT price_level_id FROM pos_ticket_lines WHERE restaurant_id=? AND provider=? "
            "AND business_date>=? AND kind='sale' AND price_level_id IS NOT NULL", (restaurant_id, provider, since))}
    finally:
        conn.close()


def sync_prices(restaurant_id, db_path=DB_PATH, today=None) -> dict:
    """Read the POS's menu prices; each price that moved since last night
    becomes a change_log "price" row (source "sync"), the same record a
    typed or Back Office price makes — so outcomes knows a price moved inside
    a tracked window, and every repricing is on file to learn from. Only
    levels rung in the last PRICE_LEVEL_LOOKBACK_DAYS of archived sales are
    tracked. The first read is the baseline and logs nothing."""
    name, mod = provider_for(restaurant_id)
    fn = getattr(mod, "fetch_menu_prices", None) if mod else None
    if not callable(fn):
        return {"ok": False, "reason": "the POS does not share menu prices"}
    used = _levels_in_use(restaurant_id, name, db_path, today)
    if not used:
        return {"ok": False, "reason": "no archived sales yet to say which price levels are rung"}
    rows = [r for r in fn(restaurant_id) if r["level_id"] in used]
    main = None
    conn = get_conn(db_path)
    try:
        main = conn.execute(
            "SELECT price_level_id FROM pos_ticket_lines WHERE restaurant_id=? AND provider=? AND kind='sale' "
            "AND price_level_id IS NOT NULL GROUP BY price_level_id ORDER BY COUNT(*) DESC LIMIT 1",
            (restaurant_id, name)).fetchone()
        main = main[0] if main else None
        have = {(r["item_id"], r["level_id"]): r["price"] for r in conn.execute(
            "SELECT item_id, level_id, price FROM pos_menu_prices WHERE restaurant_id=? AND provider=?",
            (restaurant_id, name)).fetchall()}
        baseline = not have
        changes = [(r, have[(r["item_id"], r["level_id"])]) for r in rows
                   if (r["item_id"], r["level_id"]) in have and abs(have[(r["item_id"], r["level_id"])] - r["price"]) >= 0.005]
        conn.executemany(
            "INSERT INTO pos_menu_prices (restaurant_id, provider, item_id, level_id, item_name, level, price, seen_at) "
            "VALUES (?,?,?,?,?,?,?,datetime('now')) ON CONFLICT(restaurant_id, provider, item_id, level_id) DO UPDATE "
            "SET item_name=excluded.item_name, level=excluded.level, price=excluded.price, seen_at=excluded.seen_at",
            [(restaurant_id, name, r["item_id"], r["level_id"], r["item_name"], r["level"], r["price"]) for r in rows])
        conn.commit()
    finally:
        conn.close()
    if baseline:
        return {"ok": True, "baseline": True, "tracked": len(rows), "changes": 0}
    if len(changes) > MAX_PRICE_CHANGES_LOGGED:
        try:
            import ops
            ops.capture(RuntimeError(f"pos_archive: {len(changes)} POS prices changed in one night; stored, "
                                     f"not logged (over {MAX_PRICE_CHANGES_LOGGED})"),
                        job="pos_archive", context=f"restaurant_id={restaurant_id}")
        except Exception:
            pass
        return {"ok": True, "tracked": len(rows), "changes": len(changes), "logged": 0}
    import change_log
    logged = 0
    for r, before in changes:
        subject = r["item_name"] or f"POS item {r['item_id']}"
        if r["level_id"] != main and r["level"]:
            subject = f"{subject} ({r['level']})"
        try:
            change_log.record(restaurant_id, "price", "sell_price", before, r["price"], subject=subject,
                              source="sync", db_path=db_path, via="pos_archive.sync_prices")
            logged += 1
        except Exception as e:
            log.warning("pos_archive: price change not logged rid=%s: %s", restaurant_id, e)
    return {"ok": True, "tracked": len(rows), "changes": len(changes), "logged": logged}


# ── the archive as the readers' source (9/29/26) ────────────────────────────
#
# rpower._day_rows asks here which days it may read from the archive instead
# of downloading them again. A day qualifies when the store's archive has
# started, the day is closed, and it is stored under the current layout —
# archived on the spot if not (ON_DEMAND_MAX a call). The last FRESH_DAYS are
# re-checked against the POS's change feed once RECHECK_MINUTES after they
# were stored, so a re-post after close (a tab closed at 2am) is picked up
# before the next reader, not the next night.

ON_DEMAND_MAX = 7
FRESH_DAYS = 3
RECHECK_MINUTES = 30
CHANGE_MEMO_SECONDS = 300
_change_memo = {}


def archive_started(restaurant_id, provider, db_path=DB_PATH) -> bool:
    conn = get_conn(db_path)
    try:
        return conn.execute("SELECT 1 FROM pos_archive_days WHERE restaurant_id=? AND provider=? LIMIT 1",
                            (restaurant_id, provider)).fetchone() is not None
    finally:
        conn.close()


def _business_today(restaurant_id):
    from models import get_restaurant
    from time_utils import restaurant_now, business_date
    r = get_restaurant(restaurant_id)
    return business_date(r, restaurant_now(r, naive=True))


def _changed_since(restaurant_id, mod, since_utc, now_utc):
    """The provider's changed business dates since `since_utc`, remembered
    for CHANGE_MEMO_SECONDS so four readers in a row ask once."""
    import time as _t
    hit = _change_memo.get(restaurant_id)
    if hit and _t.monotonic() - hit[0] < CHANGE_MEMO_SECONDS and hit[1] <= since_utc:
        return hit[2]
    out = set(mod.changed_business_dates(restaurant_id, since_utc, now_utc))
    _change_memo[restaurant_id] = (_t.monotonic(), since_utc, out)
    return out


def ready_dates(restaurant_id, provider, dates, db_path=DB_PATH, now_utc=None) -> set:
    """ISO dates among `dates` the archive can answer for, archiving closed
    days it lacks (ON_DEMAND_MAX per call) and re-storing a recent day the POS
    changed. An empty set means "read the POS", never an error."""
    import os
    if os.getenv("POS_ARCHIVE_READS", "1").strip() in ("0", "false", "off", "no"):
        return set()                 # the switch: every reader goes to the POS, as before 9/29/26
    name, mod = provider_for(restaurant_id)
    if mod is None or name != provider or not archive_started(restaurant_id, provider, db_path):
        return set()
    now_utc = now_utc or datetime.now(timezone.utc)
    today = _business_today(restaurant_id)
    conn = get_conn(db_path)
    try:
        stored = {r[0]: r[1] for r in conn.execute(
            "SELECT business_date, archived_at FROM pos_archive_days WHERE restaurant_id=? AND provider=? "
            "AND COALESCE(version, 1) >= ?", (restaurant_id, provider, ARCHIVE_VERSION)).fetchall()}
    finally:
        conn.close()
    closed_fn = getattr(mod, "day_closed_cached", None)
    ready, todo = set(), []
    for d in dates:
        iso = d.isoformat()
        if d >= today:
            try:
                if not (callable(closed_fn) and closed_fn(restaurant_id, d)):
                    continue
            except Exception:
                continue
        if iso in stored:
            ready.add(iso)
        else:
            todo.append(d)
    recent_floor = (today - timedelta(days=FRESH_DAYS)).isoformat()
    stale = {}
    for iso in ready:
        if iso < recent_floor:
            continue
        try:
            at = datetime.fromisoformat(stored[iso]).replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        if now_utc - at > timedelta(minutes=RECHECK_MINUTES):
            stale[iso] = at
    if stale and callable(getattr(mod, "changed_business_dates", None)):
        try:
            changed = _changed_since(restaurant_id, mod, min(stale.values()), now_utc)
        except Exception as e:
            log.warning("pos_archive: change read failed for %s: %s", restaurant_id, e)
            changed = set()
        for iso, at in stale.items():
            if iso in changed:
                ready.discard(iso)
                todo.append(date.fromisoformat(iso))
    for d in sorted(todo, reverse=True)[:ON_DEMAND_MAX]:
        try:
            archive_day(restaurant_id, d, db_path=db_path, provider=(name, mod))
            ready.add(d.isoformat())
        except Exception as e:
            log.warning("pos_archive: on-demand archive of %s %s failed: %s", restaurant_id, d, e)
    return ready
