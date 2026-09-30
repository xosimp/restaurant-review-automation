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

from models import get_conn, DB_PATH

log = logging.getLogger("pos_archive")

BACKFILL_DAYS = 90
BACKFILL_DAYS_PER_NIGHT = 7
# The change read looks back this far the first time (no state yet).
FIRST_CHANGE_LOOKBACK_HOURS = 48

_TICKET_COLS = ("ticket_id", "business_date", "ticket_no", "opened_at", "closed_at", "fired_at", "bumped_at",
                "need_at", "server_id", "server_name", "table_id", "table_name", "room_name", "is_bar",
                "guest_count", "entree_count", "bev_count", "net_sales", "discount", "tip", "grat", "tax",
                "mealtime", "profit_center", "cancelled", "source_stamp")
_LINE_COLS = ("line_id", "ticket_id", "business_date", "item_id", "item_name", "item_kind", "kind", "qty",
              "sales", "price", "regular_price", "loss_amount", "reason", "approver_id", "item_at", "mealtime",
              "profit_center", "price_level_id", "source_stamp")
_PUNCH_COLS = ("punch_id", "business_date", "employee_id", "employee_name", "job_id", "role", "clock_in",
               "clock_out", "reg_hours", "ot_hours", "dt_hours", "reg_rate", "ot_rate", "pay", "ot_pay", "tips",
               "tips_net", "grats", "break_minutes", "meal_minutes", "rest_minutes", "edited_by", "edited_at",
               "edit_what", "is_station", "source_stamp")
_TABLES = (("pos_tickets", _TICKET_COLS, "tickets"), ("pos_ticket_lines", _LINE_COLS, "lines"),
           ("pos_punches", _PUNCH_COLS, "punches"))


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
                [(restaurant_id, name, *[r.get(c) for c in cols]) for r in rows[key]])
        restated = bool(prev and prev["max_stamp"] and rows.get("max_stamp")
                        and rows["max_stamp"] != prev["max_stamp"])
        conn.execute(
            "INSERT INTO pos_archive_days (restaurant_id, provider, business_date, tickets, lines, punches, "
            "net_sales, max_stamp, restated, archived_at) VALUES (?,?,?,?,?,?,?,?,?,datetime('now')) "
            "ON CONFLICT(restaurant_id, provider, business_date) DO UPDATE SET tickets=excluded.tickets, "
            "lines=excluded.lines, punches=excluded.punches, net_sales=excluded.net_sales, "
            "max_stamp=excluded.max_stamp, restated=pos_archive_days.restated + ?, archived_at=datetime('now')",
            (restaurant_id, name, iso, len(rows["tickets"]), len(rows["lines"]), len(rows["punches"]), net,
             rows.get("max_stamp"), 1 if restated else 0, 1 if restated else 0))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"date": iso, "tickets": len(rows["tickets"]), "lines": len(rows["lines"]),
            "punches": len(rows["punches"]), "net_sales": net, "restated": restated}


def archived_dates(restaurant_id, provider, db_path=DB_PATH) -> set:
    conn = get_conn(db_path)
    try:
        return {r[0] for r in conn.execute("SELECT business_date FROM pos_archive_days WHERE restaurant_id=? "
                                           "AND provider=?", (restaurant_id, provider)).fetchall()}
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
    return {"ok": not failed, "archived": done, "restated": restated, "failed": failed,
            "backfill_left": max(0, len(missing) - BACKFILL_DAYS_PER_NIGHT), "provider": name}
