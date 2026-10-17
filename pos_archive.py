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

  1. the last complete business date (pos.complete_through — at 4am local
     that is still the night before last, so the queue row waits for the
     night to end: strategy_jobs.run_pos_archive), once the POS has closed
     it when it is that recent;
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
# from, so they read a closed day from here — both 9/29/26; 4: each line's
# department and sales category, RPOWER's cost / discounted / aqu_count /
# fraction, and the modifier links from ticketitem — the Bar, 10/15/26 F5).
ARCHIVE_VERSION = 4
# The oldest layout the READERS can use (rpower._raw_from_archive needs the
# layout-3 raw fields; a layout-4 field is read where present and the live
# catalog used where not). Separate from ARCHIVE_VERSION on purpose (food &
# bar audit 10/16/26 POS-09): one number gated both "store this day again"
# and "a reader may use this day", so the bump to 4 made every stored day
# unreadable at once and each reader downloaded it again — the 10/16
# incident. A bump now only re-stores days through the bounded backfill;
# readers keep reading. Raise this only when a reader needs a new field
# that an older day does not have.
READ_MIN_VERSION = 3
# A night's own line total against RPOWER's per-ticket type totals (POS-11):
# more apart than this, or any dollars on a sales type we could not read,
# is raised to the console.
RECONCILE_TOLERANCE = 1.00

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
              "void_mgr_id", "reason_id", "shift",
              # layout 4 (F5): where RPOWER filed the line, its bar-relevant
              # fields, and which line a modifier modifies (never summed).
              "department", "sales_category", "category_id", "cost", "discounted", "aqu_count", "fraction",
              "atom", "parent_atom", "mod_level", "parent_line_id")
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


def init_pos_catalog(db_path=None):
    """The POS's own sales catalog, kept nightly beside the archive (bar fix
    round 10/15/26, F5) — boot only (models.init_db):

      pos_sales_categories  every sales category with its department and
                            what RPOWER says about it for a bar: is_bar,
                            alt_qty_unit / alt_rpt_unit / aqu_in_aru (the
                            alternate quantity, e.g. ounces poured). Seeds
                            the bar mapping's suggestions (bar.mapping).
      pos_menu_catalog      every menu item: its sales category, is_mod and
                            RPOWER's last_cost, plu, upc and aqu_count (POS
                            cost per item where a store keeps one; a
                            theoretical pour cost needs no recipe then).
    Both are replaced per store on each read; 0 / "" are kept as RPOWER
    sends them (no value), never read as a measurement here."""
    conn = get_conn(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS pos_sales_categories (
            restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
            provider        TEXT NOT NULL,
            category_id     TEXT NOT NULL,
            name            TEXT,
            department_id   TEXT,
            department      TEXT,
            is_bar          INTEGER NOT NULL DEFAULT 0,
            alt_qty_unit    TEXT,
            alt_rpt_unit    TEXT,
            aqu_in_aru      REAL,
            seen_at         TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (restaurant_id, provider, category_id)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS pos_menu_catalog (
            restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
            provider        TEXT NOT NULL,
            item_id         TEXT NOT NULL,
            name            TEXT,
            category_id     TEXT,
            is_mod          INTEGER NOT NULL DEFAULT 0,
            last_cost       REAL,
            plu             TEXT,
            upc             TEXT,
            aqu_count       REAL,
            seen_at         TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (restaurant_id, provider, item_id)
        )""")
        conn.commit()
    finally:
        conn.close()


def sync_catalog(restaurant_id, db_path=DB_PATH) -> dict:
    """Keep the POS's sales categories and menu items (init_pos_catalog) as
    the provider lists them now — a provider that defines
    `fetch_sales_catalog` (rpower). A category or item the POS no longer
    lists is removed: the archived lines keep their own department and
    category, so nothing a night says is lost with it."""
    name, mod = provider_for(restaurant_id)
    fn = getattr(mod, "fetch_sales_catalog", None) if mod else None
    if not callable(fn):
        return {"ok": False, "reason": "the POS does not share its sales catalog"}
    cat = fn(restaurant_id) or {}
    cats, items = cat.get("categories") or [], cat.get("items") or []
    if not cats and not items:
        return {"ok": False, "reason": "the POS listed no sales categories"}
    conn = get_conn(db_path)
    try:
        conn.execute("DELETE FROM pos_sales_categories WHERE restaurant_id=? AND provider=?", (restaurant_id, name))
        conn.executemany(
            "INSERT INTO pos_sales_categories (restaurant_id, provider, category_id, name, department_id, department, "
            "is_bar, alt_qty_unit, alt_rpt_unit, aqu_in_aru, seen_at) VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now'))",
            [(restaurant_id, name, c["category_id"], c.get("name"), c.get("department_id"), c.get("department"),
              int(c.get("is_bar") or 0), c.get("alt_qty_unit"), c.get("alt_rpt_unit"), c.get("aqu_in_aru"))
             for c in cats if c.get("category_id")])
        conn.execute("DELETE FROM pos_menu_catalog WHERE restaurant_id=? AND provider=?", (restaurant_id, name))
        conn.executemany(
            "INSERT INTO pos_menu_catalog (restaurant_id, provider, item_id, name, category_id, is_mod, last_cost, "
            "plu, upc, aqu_count, seen_at) VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now'))",
            [(restaurant_id, name, i["item_id"], i.get("name"), i.get("category_id"), int(i.get("is_mod") or 0),
              i.get("last_cost"), i.get("plu"), i.get("upc"), i.get("aqu_count")) for i in items if i.get("item_id")])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"ok": True, "categories": len(cats), "items": len(items),
            "bar_categories": sum(1 for c in cats if c.get("is_bar")),
            "costed_items": sum(1 for i in items if (i.get("last_cost") or 0) > 0)}


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


class ArchiveCountMismatch(RuntimeError):
    """Fewer rows landed in a table than the POS returned for the day: two
    records shared a key (POS-10). The day's write is rolled back, never
    stored short."""


def _reconcile(restaurant_id, iso, checks):
    """(gap, unknown_sales) from the provider's `checks` (rpower.archive_rows):
    our revenue lines minus the POS's own per-ticket totals of the same sales
    types, and the dollars on lines whose sales type the POS's list did not
    name. Either past RECONCILE_TOLERANCE is an ops event the console shows
    (food & bar audit 10/16/26 POS-11) — a sales type added at the store, or
    a line our rules drop, would otherwise vanish from net, food and drink
    sales with nobody told. (None, None) when the provider has no checks."""
    if not isinstance(checks, dict) or "gap" not in checks:
        return None, None
    gap = round(float(checks.get("gap") or 0), 2)
    unknown = round(float(checks.get("unknown_type_sales") or 0), 2)
    if abs(gap) > RECONCILE_TOLERANCE or abs(unknown) > 0.005 or int(checks.get("unknown_type_lines") or 0):
        try:
            import ops
            ops.capture(RuntimeError(
                f"POS reconcile {iso}: our revenue lines ${sum((checks.get('lines_by_type') or {}).values()):,.2f} "
                f"vs the POS's ticket totals ${sum((checks.get('tickets_by_type') or {}).values()):,.2f} "
                f"(gap ${gap:,.2f}); {int(checks.get('unknown_type_lines') or 0)} line(s) of an unknown sales type, "
                f"${unknown:,.2f}"), job="pos_reconcile", context=f"restaurant_id={restaurant_id} date={iso}",
                restaurant_id=restaurant_id)
        except Exception:
            pass
    return gap, unknown


def archive_day(restaurant_id, day, db_path=DB_PATH, provider=None) -> dict:
    """Read one business date from the POS and store it whole.
    {"date", "tickets", "lines", "punches", "net_sales", "restated",
    "links_ok", "recon_gap", "unknown_sales"}."""
    name, mod = provider or provider_for(restaurant_id)
    if mod is None:
        raise LookupError("no connected POS can archive tickets")
    rows = mod.archive_rows(restaurant_id, day)
    iso = day.isoformat() if hasattr(day, "isoformat") else str(day)[:10]
    net = round(sum(float(t.get("net_sales") or 0) for t in rows["tickets"]), 2)
    # A provider with no modifier links to read has none to lose; one that
    # reports links_ok False read none tonight (POS-12).
    links_ok = rows.get("links_ok")
    links_ok = None if links_ok is None else (1 if links_ok else 0)
    gap, unknown = _reconcile(restaurant_id, iso, rows.get("checks"))
    conn = get_conn(db_path)
    try:
        prev = conn.execute("SELECT max_stamp, restated FROM pos_archive_days WHERE restaurant_id=? AND provider=? "
                            "AND business_date=?", (restaurant_id, name, iso)).fetchone()
        for table, cols, key in _TABLES:
            conn.execute(f"DELETE FROM {table} WHERE restaurant_id=? AND provider=? AND business_date=?",
                         (restaurant_id, name, iso))
            want = rows.get(key) or []
            marks = ",".join("?" for _ in range(len(cols) + 2))
            conn.executemany(
                f"INSERT OR REPLACE INTO {table} (restaurant_id, provider, {','.join(cols)}) VALUES ({marks})",
                [(restaurant_id, name, *[r.get(c) for c in cols]) for r in want])
            # Every record the POS returned is a row (POS-10): a key two
            # records shared replaced one with the other, silently.
            got = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE restaurant_id=? AND provider=? "
                               f"AND business_date=?", (restaurant_id, name, iso)).fetchone()[0]
            if int(got) != len(want):
                raise ArchiveCountMismatch(f"{table} {iso}: {len(want)} records from the POS, {got} stored")
        restated = bool(prev and prev["max_stamp"] and rows.get("max_stamp")
                        and rows["max_stamp"] != prev["max_stamp"])
        conn.execute(
            "INSERT INTO pos_archive_days (restaurant_id, provider, business_date, tickets, lines, punches, "
            "net_sales, max_stamp, restated, version, links_ok, recon_gap, unknown_sales, archived_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now')) "
            "ON CONFLICT(restaurant_id, provider, business_date) DO UPDATE SET tickets=excluded.tickets, "
            "lines=excluded.lines, punches=excluded.punches, net_sales=excluded.net_sales, "
            "max_stamp=excluded.max_stamp, restated=pos_archive_days.restated + ?, version=excluded.version, "
            "links_ok=excluded.links_ok, recon_gap=excluded.recon_gap, unknown_sales=excluded.unknown_sales, "
            "archived_at=datetime('now')",
            (restaurant_id, name, iso, len(rows["tickets"]), len(rows["lines"]), len(rows["punches"]), net,
             rows.get("max_stamp"), 1 if restated else 0, ARCHIVE_VERSION, links_ok, gap, unknown,
             1 if restated else 0))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    try:
        # The night's drink sales by POS item category, net of the check
        # discounts allocated per ticket (bar.sales.derive_nights) — every
        # path that stores a day (the nightly run, a restatement, a reader's
        # on-demand archive) derives it again from what was just stored.
        from bar.sales import derive_nights
        derive_nights(restaurant_id, [iso], db_path=db_path, provider=name)
    except Exception as e:                           # the archive stands; the Bar estimates the night
        log.warning("pos_archive: bar night %s not derived for %s: %s", iso, restaurant_id, e)
    return {"date": iso, "tickets": len(rows["tickets"]), "lines": len(rows["lines"]),
            "punches": len(rows["punches"]), "net_sales": net, "restated": restated,
            "links_ok": links_ok, "recon_gap": gap, "unknown_sales": unknown}


def archived_dates(restaurant_id, provider, db_path=DB_PATH) -> set:
    """Dates stored whole under the current layout (ARCHIVE_VERSION): an
    older day, or one whose modifier links failed to read (links_ok 0,
    POS-12), reads as missing, so the backfill stores it again."""
    conn = get_conn(db_path)
    try:
        return {r[0] for r in conn.execute("SELECT business_date FROM pos_archive_days WHERE restaurant_id=? "
                                           "AND provider=? AND COALESCE(version, 1) >= ? "
                                           "AND COALESCE(links_ok, 1) = 1",
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


def _closed(mod, restaurant_id, day) -> bool:
    """Whether the POS has run its close for `day` (a provider without the
    read is taken as closed: it has no such record to wait for)."""
    fn = getattr(mod, "day_closed_cached", None)
    if not callable(fn):
        return True
    try:
        return bool(fn(restaurant_id, day))
    except Exception as e:
        log.warning("pos_archive: closed-day read failed for %s %s: %s", restaurant_id, day, e)
        return False


def run_for(restaurant_id, today=None, db_path=DB_PATH, now_utc=None) -> dict:
    """One restaurant's night: the last complete night, restated days, then
    backfill. `today` is the restaurant's current business date; without it
    the newest night is pos.complete_through — the last business date that
    has ENDED (food & bar audit 10/16/26 POS-08: at 4:30am Chicago the
    business date is still last night, so "today - 1" archived the night
    before last and last night waited a whole day). A night that recent is
    archived only once the POS has closed it (POS-16), so a partial night is
    never stored as whole; a store that never runs a close has it stored the
    next night, as backfill."""
    name, mod = provider_for(restaurant_id)
    if mod is None:
        return {"ok": False, "reason": "no connected POS can archive tickets"}
    if today is None:
        from models import get_restaurant
        from time_utils import restaurant_now, business_date
        import pos
        r = get_restaurant(restaurant_id)
        local = restaurant_now(r, naive=True)
        today = business_date(r, local)
        yesterday = pos.complete_through(r, local)
    else:
        yesterday = today - timedelta(days=1)
    now_utc = now_utc or datetime.now(timezone.utc)
    have = archived_dates(restaurant_id, name, db_path)
    waiting = []
    todo = []
    if yesterday >= today - timedelta(days=1) and not _closed(mod, restaurant_id, yesterday):
        waiting.append(yesterday.isoformat())
    else:
        todo.append(yesterday)
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
    catalog = backfill_restated = None
    try:
        catalog = sync_catalog(restaurant_id, db_path=db_path)
    except Exception as e:                           # the archive stands; the catalog tries again tomorrow
        log.warning("pos_archive: catalog sync failed for %s: %s", restaurant_id, e)
    if restated_dates:
        try:
            # A night the DSR backfill filled is restated with the archive
            # (bar audit 10/14/26 RPW-13 / R11): its stored figures were the
            # POS's before the re-post.
            from dsr.backfill import restate
            backfill_restated = restate(restaurant_id, [d.isoformat() for d in restated_dates
                                                        if d.isoformat() in done], db_path=db_path)
        except Exception as e:
            log.warning("pos_archive: backfilled nights not restated for %s: %s", restaurant_id, e)
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
            "not_closed": waiting,
            "backfill_left": max(0, len(missing) - BACKFILL_DAYS_PER_NIGHT), "provider": name, "roles": roles,
            "prices": prices, "catalog": catalog, "backfill_restated": backfill_restated}


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
        # A job the owner removed stays removed, whatever the POS still lists
        # (people.remove_role makes a person's removal the owner's row).
        refused = {(r[0], str(r[1]).lower()) for r in conn.execute(
            "SELECT employee_key, role FROM person_roles WHERE restaurant_id=? AND removed_at IS NOT NULL "
            "AND COALESCE(source, '') != ?", (restaurant_id, ROLE_SOURCE)).fetchall()}
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
            if (key, role.lower()) in everyone or (key, role.lower()) in refused:
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
    if today is None:                    # the restaurant's own date (POS-18)
        import pos
        today = pos.local_complete_through(restaurant_id) + timedelta(days=1)
    since = (today - timedelta(days=PRICE_LEVEL_LOOKBACK_DAYS)).isoformat()
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
# started, the day is closed (checked for yesterday as well as today: a
# reader at 5:30am asking for a night the store has not closed yet, POS-16),
# and it is stored under a layout the readers can use (READ_MIN_VERSION) —
# archived on the spot if not (ON_DEMAND_MAX a call). The last FRESH_DAYS are
# re-checked against the POS's change feed once RECHECK_MINUTES after they
# were stored, so a re-post after close (a tab closed at 2am) is picked up
# before the next reader, not the next night; when that change read fails,
# those days are not served from the archive as if they were fresh.
#
# `live=False` is a web request (POS-13): the archive as it stands, no
# closed-day read, no change read, nothing archived on the spot — a page
# load makes no POS request. Every path that stores a recent night checks
# its close first (run_for, the on-demand archive here), so a stored night
# is a closed one.

ON_DEMAND_MAX = 7
FRESH_DAYS = 3
RECHECK_MINUTES = 30
CHANGE_MEMO_SECONDS = 300
CHANGE_MEMO_MAX = 256             # restaurants remembered at once (bounded, R2)
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
    if len(_change_memo) >= CHANGE_MEMO_MAX:
        now = _t.monotonic()
        for k in [k for k, v in list(_change_memo.items()) if now - v[0] >= CHANGE_MEMO_SECONDS]:
            _change_memo.pop(k, None)
        while len(_change_memo) >= CHANGE_MEMO_MAX:
            _change_memo.pop(next(iter(_change_memo)), None)
    _change_memo[restaurant_id] = (_t.monotonic(), since_utc, out)
    return out


def ready_dates(restaurant_id, provider, dates, db_path=DB_PATH, now_utc=None, live=True) -> set:
    """ISO dates among `dates` the archive can answer for, archiving closed
    days it lacks (ON_DEMAND_MAX per call) and re-storing a recent day the POS
    changed. An empty set means "read the POS", never an error. `live=False`
    (a web request): the stored days only, and no POS request of any kind."""
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
            "AND COALESCE(version, 1) >= ?", (restaurant_id, provider, READ_MIN_VERSION)).fetchall()}
    finally:
        conn.close()
    if not live:
        return {d.isoformat() for d in dates if d.isoformat() in stored}
    ready, todo = set(), []
    for d in dates:
        iso = d.isoformat()
        if d >= today or (d == today - timedelta(days=1) and iso not in stored):
            # Yesterday too, before it is archived on the spot (POS-16): a
            # closed day is remembered forever, an open one for a few
            # minutes (rpower.day_closed_cached). A stored yesterday was
            # closed when it was stored — every path that stores one checks.
            if not _closed(mod, restaurant_id, d):
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
            # Unchecked is not unchanged (POS-16): the recent days the check
            # was for go to the POS this time, never served as fresh.
            log.warning("pos_archive: change read failed for %s: %s", restaurant_id, e)
            for iso in stale:
                ready.discard(iso)
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
