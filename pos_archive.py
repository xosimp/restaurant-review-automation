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
the POS has since removed — but an empty or short re-read never replaces a
stored night (ArchiveShrunk, blind re-audit 10/20/26 R3-05), and a stored
night is read again only when the POS wrote to it after it was stored
(the change read's newest write per date against the day's `max_stamp`,
R3-02).
"""
import logging
import os
import re
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
# Days of history a backfilling store archives a night (scale audit 10/19/26
# ONB-5: 7 is 13 nights to 90 days; a wave of new stores can take more a
# night once RPOWER agrees the fleet budget). Only stores holding a backfill
# slot backfill at all, and only with room for the days and the backup after
# them (onboarding.backfill_admission, ONB-1).
BACKFILL_DAYS_PER_NIGHT = max(1, int(os.getenv("POS_BACKFILL_DAYS_PER_NIGHT", "7")))
# The change read looks back this far the first time (no state yet).
FIRST_CHANGE_LOOKBACK_HOURS = 48

_TICKET_COLS = ("ticket_id", "business_date", "ticket_no", "opened_at", "closed_at", "fired_at", "bumped_at",
                "need_at", "server_id", "server_name", "table_id", "table_name", "room_name", "is_bar",
                "guest_count", "entree_count", "bev_count", "net_sales", "discount", "tip", "grat", "tax",
                "mealtime", "profit_center", "cancelled", "source_stamp", "type_sale", "type_discnt", "type_promo",
                "type_comp", "customer_id", "tax_adjust")
_LINE_COLS = ("line_id", "ticket_id", "business_date", "item_id", "item_name", "item_kind", "kind", "qty",
              "sales", "price", "regular_price", "loss_amount", "reason", "approver_id", "item_at", "mealtime",
              "profit_center", "price_level_id", "source_stamp", "sales_type_id", "voided", "mgr_id",
              "void_mgr_id", "reason_id", "shift",
              # layout 4 (F5): where RPOWER filed the line, its bar-relevant
              # fields, and which line a modifier modifies (never summed).
              "department", "sales_category", "category_id", "cost", "discounted", "aqu_count", "fraction",
              "atom", "parent_atom", "mod_level", "parent_line_id",
              # Audit #23 A2-01: the sales type's flags the night it was archived.
              "sales_type_flags")
_PUNCH_COLS = ("punch_id", "business_date", "employee_id", "employee_name", "job_id", "role", "clock_in",
               "clock_out", "reg_hours", "ot_hours", "dt_hours", "reg_rate", "ot_rate", "pay", "ot_pay", "tips",
               "tips_net", "grats", "break_minutes", "meal_minutes", "rest_minutes", "edited_by", "edited_at",
               "edit_what", "is_station", "source_stamp")
_PAYMENT_COLS = ("payment_id", "ticket_id", "business_date", "method", "is_cash", "is_card", "amount", "tip",
                 "tip_fee", "cash_change", "target_amt")
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
    Both are kept in step with each read — only rows that changed, appeared
    or went are written (R3-11), `seen_at` when a row last changed; 0 / ""
    are kept as RPOWER sends them (no value), never read as a measurement
    here."""
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
    # Only what changed is written (blind re-audit 10/20/26 R3-11): every
    # category and menu item was deleted and inserted again nightly, ~600
    # rows a store of WAL for a catalog that changes a few times a month.
    # `seen_at` is when a row last CHANGED (bar.cache keys on it).
    cat_rows = {str(c["category_id"]): (c.get("name"), c.get("department_id"), c.get("department"),
                                        int(c.get("is_bar") or 0), c.get("alt_qty_unit"), c.get("alt_rpt_unit"),
                                        c.get("aqu_in_aru")) for c in cats if c.get("category_id")}
    item_rows = {str(i["item_id"]): (i.get("name"), i.get("category_id"), int(i.get("is_mod") or 0),
                                     i.get("last_cost"), i.get("plu"), i.get("upc"), i.get("aqu_count"))
                 for i in items if i.get("item_id")}
    changed = {"categories": 0, "items": 0, "removed": 0}
    conn = get_conn(db_path)
    try:
        for table, key, cols, want, label in (
                ("pos_sales_categories", "category_id",
                 ("name", "department_id", "department", "is_bar", "alt_qty_unit", "alt_rpt_unit", "aqu_in_aru"),
                 cat_rows, "categories"),
                ("pos_menu_catalog", "item_id",
                 ("name", "category_id", "is_mod", "last_cost", "plu", "upc", "aqu_count"), item_rows, "items")):
            have = {str(r[0]): tuple(r[1:]) for r in conn.execute(
                f"SELECT {key}, {', '.join(cols)} FROM {table} WHERE restaurant_id=? AND provider=?",
                (restaurant_id, name)).fetchall()}
            gone = sorted(set(have) - set(want))
            for i in range(0, len(gone), 500):
                chunk = gone[i:i + 500]
                conn.execute(f"DELETE FROM {table} WHERE restaurant_id=? AND provider=? "
                             f"AND {key} IN ({','.join('?' for _ in chunk)})", (restaurant_id, name, *chunk))
            put = [(k, v) for k, v in want.items() if k not in have or not _same_row(have[k], v)]
            if put:
                conn.executemany(
                    f"INSERT INTO {table} (restaurant_id, provider, {key}, {', '.join(cols)}, seen_at) "
                    f"VALUES (?,?,?,{','.join('?' for _ in cols)},datetime('now')) "
                    f"ON CONFLICT(restaurant_id, provider, {key}) DO UPDATE SET "
                    f"{', '.join(f'{c}=excluded.{c}' for c in cols)}, seen_at=excluded.seen_at",
                    [(restaurant_id, name, k, *v) for k, v in put])
            changed[label] = len(put)
            changed["removed"] += len(gone)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"ok": True, "categories": len(cats), "items": len(items), "changed": changed,
            "bar_categories": sum(1 for c in cats if c.get("is_bar")),
            "costed_items": sum(1 for i in items if (i.get("last_cost") or 0) > 0)}


def _same_row(stored, new) -> bool:
    """Whether a stored catalog row holds the same values as a fresh one
    (numbers compared as numbers: the database hands 1.0 back for 1)."""
    if len(stored) != len(new):
        return False
    for a, b in zip(stored, new):
        if a is None or b is None:
            if a is not b and not (a is None and b is None):
                return False
            continue
        if isinstance(a, (int, float)) or isinstance(b, (int, float)):
            try:
                if abs(float(a) - float(b)) > 1e-9:
                    return False
            except (TypeError, ValueError):
                return False
        elif str(a) != str(b):
            return False
    return True


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
            # Once per night and finding: a restated night is archived again, and the same gap was captured
            # on every re-read - four times for 10/17/26 (owner, 10/19/26).
            if not ops.claim_period(f"pos_reconcile:{restaurant_id}:{iso}", f"{gap:.2f}|{unknown:.2f}"):
                return gap, unknown
            ops.capture(RuntimeError(
                f"POS reconcile {iso}: our revenue lines ${sum((checks.get('lines_by_type') or {}).values()):,.2f} "
                f"vs the POS's ticket totals ${sum((checks.get('tickets_by_type') or {}).values()):,.2f} "
                f"(gap ${gap:,.2f}); {int(checks.get('unknown_type_lines') or 0)} line(s) of an unknown sales type, "
                f"${unknown:,.2f}"), job="pos_reconcile", context=f"restaurant_id={restaurant_id} date={iso}",
                restaurant_id=restaurant_id)
        except Exception:
            pass
    return gap, unknown


class ArchiveBusy(RuntimeError):
    """Another thread or process was archiving the same (restaurant, day)
    and did not finish within ARCHIVE_WAIT_SECONDS."""
    retry_after = 120.0


# One read of a (restaurant, day) at a time across every process (scale
# audit 10/19/26 A3-05): the web DSR sweep and the queue worker could both
# archive the same night at once — two downloads, and a spurious
# `restated` when their max stamps differed. The claim is a cooldown row
# (ops.claim_cooldown) released when the day is stored; one left by a
# killed process lapses after ARCHIVE_CLAIM_MINUTES. A second caller waits
# for the first to store the day and returns what it stored.
ARCHIVE_CLAIM_MINUTES = 10
ARCHIVE_WAIT_SECONDS = 120
# The claim is renewed this often while the day is being read (blind
# re-audit 10/20/26 R3-15): a read starved by the request budget (about 12
# requests, each allowed 60 s for its slot) outlasted the 10 minutes, and a
# second caller re-claimed the day and downloaded it again.
ARCHIVE_CLAIM_RENEW_SECONDS = 60


class ArchiveShrunk(RuntimeError):
    """A re-read returned an empty or short night against the one stored
    (blind re-audit 10/20/26 R3-05): the stored night is kept, the short
    read is recorded on it (`suspect`), and the day is read again later.
    No `retry_after`: it stops no other day's read."""
    kept = True


# A re-read is SHORT when it has fewer lines than the stored night by more
# than max(SHRINK_LINES, SHRINK_SHARE x stored) or fewer tickets by more
# than max(SHRINK_TICKETS, SHRINK_SHARE x stored). An empty re-read of a
# stored night is never taken. A short one is taken only when the same
# short read comes back SHRINK_CONFIRM_MINUTES or more after the first and
# it reconciles with the POS's own ticket totals — the POS's own figure
# moved (a ticket re-dated at the store), not a read made mid-repost.
SHRINK_LINES = 3
SHRINK_TICKETS = 1
SHRINK_SHARE = 0.02
SHRINK_CONFIRM_MINUTES = 30
# A night read while its rows were still arriving — its newest POS write
# within this many minutes of when it was read or last checked — is not
# settled (pos_archive.night_settled): the nightly report does not go out
# FINAL on it, and the readers check it again after this long rather than
# RECHECK_MINUTES (R3-05: the cloud copy trails the store's close).
SETTLE_MINUTES = float(os.getenv("POS_SETTLE_MINUTES", "10") or 0)


class _ClaimKeeper:
    """Renew a claim_cooldown claim every ARCHIVE_CLAIM_RENEW_SECONDS while
    the work inside runs (R3-15)."""

    def __init__(self, claim):
        import threading
        self.claim, self.stop = claim, threading.Event()
        self.thread = threading.Thread(target=self._beat, name="pos-archive-claim", daemon=True)

    def _beat(self):
        import ops
        while not self.stop.wait(ARCHIVE_CLAIM_RENEW_SECONDS):
            ops.renew_cooldown(self.claim)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        return False


def _utc_stamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _stored_row(restaurant_id, name, iso, db_path):
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT business_date, tickets, lines, punches, net_sales, links_ok, recon_gap, "
                           "unknown_sales, archived_at FROM pos_archive_days WHERE restaurant_id=? AND provider=? "
                           "AND business_date=?", (restaurant_id, name, iso)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _wait_for_other(restaurant_id, name, iso, since, db_path):
    """The day as another caller stored it after `since`, waited for up to
    ARCHIVE_WAIT_SECONDS; None when it was not stored in time."""
    import time as _t
    deadline = _t.monotonic() + ARCHIVE_WAIT_SECONDS
    while True:
        row = _stored_row(restaurant_id, name, iso, db_path)
        if row and str(row.get("archived_at") or "") >= since:
            return {"date": iso, "tickets": row["tickets"], "lines": row["lines"], "punches": row["punches"],
                    "net_sales": row["net_sales"], "restated": False, "links_ok": row.get("links_ok"),
                    "recon_gap": row.get("recon_gap"), "unknown_sales": row.get("unknown_sales"), "shared": True}
        if _t.monotonic() >= deadline:
            return None
        _t.sleep(2.0)


# The NOT NULL columns of the archive tables and their DEFAULTs, read from
# the database once a process. SQLite's INSERT OR REPLACE writes a column's
# default for a NULL in a NOT NULL column; Postgres refuses the row — after
# the 10/19/26 cutover a provider row without a figure (Toast's wage-less
# reg_rate, Square's tips, tips_net and grats, a ticket with no type_sale)
# would have failed the whole night. A missing value is stored as SQLite
# always stored it: the column's default.
_NOT_NULL_DEFAULTS = {}


def _default_value(expr):
    """A column DEFAULT as written (SQLite) or as the catalog shows it
    (Postgres: `0`, `'x'::text`) — a number or a string; None when it is an
    expression (a function call) this does not evaluate."""
    if expr is None:
        return None
    t = re.sub(r"::[a-z ]+(\[\])?$", "", str(expr).strip(), flags=re.I).strip()
    if t.startswith("(") and t.endswith(")"):
        t = t[1:-1].strip()
    if len(t) >= 2 and t[0] == t[-1] == "'":
        return t[1:-1].replace("''", "'")
    try:
        return int(t)
    except ValueError:
        try:
            return float(t)
        except ValueError:
            return None


def _not_null_defaults(conn, table) -> dict:
    """{column: default} of `table`'s NOT NULL columns that have one."""
    got = _NOT_NULL_DEFAULTS.get(table)
    if got is None:
        got = {}
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall():
            name, notnull, dflt = row[1], row[3], row[4]
            val = _default_value(dflt)
            if notnull and val is not None:
                got[name] = val
        _NOT_NULL_DEFAULTS[table] = got
    return got



def archive_day(restaurant_id, day, db_path=DB_PATH, provider=None) -> dict:
    """Read one business date from the POS and store it whole.
    {"date", "tickets", "lines", "punches", "net_sales", "restated",
    "links_ok", "recon_gap", "unknown_sales"}. Single-flight per
    (restaurant, day) across processes (A3-05): a caller that finds the day
    being archived waits for it and returns that read (`shared` True)."""
    name, mod = provider or provider_for(restaurant_id)
    if mod is None:
        raise LookupError("no connected POS can archive tickets")
    iso = day.isoformat() if hasattr(day, "isoformat") else str(day)[:10]
    import ops
    claim = f"pos_archive_day:{restaurant_id}:{name}:{iso}"
    started = _utc_stamp()
    if not ops.claim_cooldown(claim, ARCHIVE_CLAIM_MINUTES):
        got = _wait_for_other(restaurant_id, name, iso, started, db_path)
        if got is not None:
            return got
        raise ArchiveBusy(f"{iso} is being archived elsewhere for restaurant {restaurant_id}")
    try:
        with _ClaimKeeper(claim):
            return _archive_day(restaurant_id, day, iso, name, mod, db_path)
    finally:
        ops.release_period("cooldown", claim)


_REDATE_CHUNK = 500


def _drop_redated(conn, table, key_col, restaurant_id, provider, iso, want) -> set:
    """Delete this day's records stored under ANOTHER business date (a
    ticket closed after midnight, a restatement); returns those dates.

    On SQLite the key is (restaurant_id, provider, <id>), so INSERT OR
    REPLACE moved a re-dated record. A partitioned table's key on Postgres
    gained business_date (pg_copy), so the old copy stayed on its old day
    beside the new one — counted twice in sales and labor (blind re-audit
    10/20/26 R2-08). Measure with: SELECT restaurant_id, ticket_id,
    COUNT(DISTINCT business_date) FROM pos_tickets GROUP BY 1, 2 HAVING
    COUNT(DISTINCT business_date) > 1 (punch_id in pos_punches likewise)."""
    ids = sorted({str(r.get(key_col)) for r in want if r.get(key_col) is not None})
    days = set()
    for i in range(0, len(ids), _REDATE_CHUNK):
        part = ids[i:i + _REDATE_CHUNK]
        marks = ",".join("?" for _ in part)
        where = (f"restaurant_id=? AND provider=? AND business_date<>? AND {key_col} IN ({marks})")
        args = (restaurant_id, provider, iso, *part)
        found = conn.execute(f"SELECT DISTINCT business_date FROM {table} WHERE {where}", args).fetchall()
        if found:
            days.update(str(r[0]) for r in found)
            conn.execute(f"DELETE FROM {table} WHERE {where}", args)
    return days


def _short_read(prev, tickets, lines) -> bool:
    """Whether a re-read of (tickets, lines) is short against the stored
    night `prev` (see SHRINK_*)."""
    pt, pl = int(prev["tickets"] or 0), int(prev["lines"] or 0)
    if not (pt or pl):
        return False
    if not (tickets or lines):
        return True
    return (pl - lines > max(SHRINK_LINES, SHRINK_SHARE * pl)
            or pt - tickets > max(SHRINK_TICKETS, SHRINK_SHARE * pt))


def _refuse_short(conn, restaurant_id, name, iso, prev, tickets, lines, net, gap):
    """Keep the stored night against a short re-read (R3-05), unless this is
    the same short read seen SHRINK_CONFIRM_MINUTES or more ago and it
    reconciles. Returns True when the read may be stored; otherwise records
    it on the day (`suspect`), tells the operator once per night and read,
    commits, and raises ArchiveShrunk."""
    import json as _json
    sig = f"{tickets}|{lines}|{net:.2f}"
    now = datetime.now(timezone.utc)
    try:
        seen = _json.loads(prev["suspect"] or "{}") if prev["suspect"] else {}
    except (TypeError, ValueError):
        seen = {}
    first = _utc(seen.get("at")) if seen.get("sig") == sig else None
    reconciles = gap is None or abs(float(gap)) <= RECONCILE_TOLERANCE
    if (tickets or lines) and first is not None and reconciles \
            and now - first >= timedelta(minutes=SHRINK_CONFIRM_MINUTES):
        return True
    conn.execute("UPDATE pos_archive_days SET suspect=? WHERE restaurant_id=? AND provider=? AND business_date=?",
                 (_json.dumps({"sig": sig, "tickets": tickets, "lines": lines, "net_sales": net,
                               "at": (first or now).strftime("%Y-%m-%d %H:%M:%S")}), restaurant_id, name, iso))
    conn.commit()
    try:
        import ops
        if ops.claim_period(f"pos_archive_short:{restaurant_id}:{iso}", sig):
            ops.capture(RuntimeError(
                f"POS re-read of {iso} came back short ({tickets} tickets, {lines} lines, ${net:,.2f}) against the "
                f"stored night ({prev['tickets']} tickets, {prev['lines']} lines, ${float(prev['net_sales'] or 0):,.2f})"
                f"; the stored night is kept and read again later"), job="pos_archive",
                context=f"restaurant_id={restaurant_id} date={iso}", restaurant_id=restaurant_id)
    except Exception:
        pass
    raise ArchiveShrunk(f"{iso}: a re-read came back short ({tickets} tickets, {lines} lines against "
                        f"{prev['tickets']} and {prev['lines']} stored); the stored night is kept")


def _archive_day(restaurant_id, day, iso, name, mod, db_path) -> dict:
    rows = mod.archive_rows(restaurant_id, day)
    net = round(sum(float(t.get("net_sales") or 0) for t in rows["tickets"]), 2)
    # A provider with no modifier links to read has none to lose; one that
    # reports links_ok False read none tonight (POS-12).
    links_ok = rows.get("links_ok")
    links_ok = None if links_ok is None else (1 if links_ok else 0)
    gap, unknown = _reconcile(restaurant_id, iso, rows.get("checks"))
    conn = get_conn(db_path)
    try:
        # On Postgres the nightly roll (roll_item_days) of this restaurant's
        # days waits for this day's store, and this for it (SQLite: no-op —
        # the database has one writer).
        import db_dialect
        db_dialect.lock(conn, "pos_archive", restaurant_id)
        prev = conn.execute("SELECT max_stamp, restated, tickets, lines, net_sales, suspect FROM pos_archive_days "
                            "WHERE restaurant_id=? AND provider=? AND business_date=?",
                            (restaurant_id, name, iso)).fetchone()
        # An empty or short re-read never replaces a stored night (R3-05): a
        # read during a store's repost, or a page lost, stored the night
        # short and the nightly report re-pulled it as a lower FINAL.
        if prev is not None and _short_read(prev, len(rows["tickets"]), len(rows["lines"])):
            _refuse_short(conn, restaurant_id, name, iso, prev, len(rows["tickets"]), len(rows["lines"]), net, gap)
        moved = set()                  # other days a record of this one was moved from
        for table, cols, key in _TABLES:
            conn.execute(f"DELETE FROM {table} WHERE restaurant_id=? AND provider=? AND business_date=?",
                         (restaurant_id, name, iso))
            want = rows.get(key) or []
            if table in db_dialect.PARTITIONED:
                moved.update(_drop_redated(conn, table, cols[0], restaurant_id, name, iso, want))
            marks = ",".join("?" for _ in range(len(cols) + 2))
            dflt = _not_null_defaults(conn, table)
            conn.executemany(
                f"INSERT OR REPLACE INTO {table} (restaurant_id, provider, {','.join(cols)}) VALUES ({marks})",
                [(restaurant_id, name, *[(dflt[c] if (r.get(c) is None and c in dflt) else r.get(c)) for c in cols])
                 for r in want])
            # Every record the POS returned is a row (POS-10): a key two
            # records shared replaced one with the other, silently.
            got = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE restaurant_id=? AND provider=? "
                               f"AND business_date=?", (restaurant_id, name, iso)).fetchone()[0]
            if int(got) != len(want):
                raise ArchiveCountMismatch(f"{table} {iso}: {len(want)} records from the POS, {got} stored")
            if table == "pos_punches" and want:
                # A punch is filed on ONE business date. One the neighbouring
                # night holds (stored before the 4am boundary, rpower.
                # punch_business_date) moves here: Postgres's partitioned key
                # carries the date, so it would otherwise stay on both and
                # labor would count it twice.
                ids = sorted({str(r["punch_id"]) for r in want if r.get("punch_id")})
                d0 = date.fromisoformat(iso)
                for i in range(0, len(ids), 500):
                    chunk = ids[i:i + 500]
                    conn.execute(f"DELETE FROM pos_punches WHERE restaurant_id=? AND provider=? "
                                 f"AND business_date IN (?, ?) AND punch_id IN ({','.join('?' for _ in chunk)})",
                                 (restaurant_id, name, (d0 - timedelta(days=1)).isoformat(),
                                  (d0 + timedelta(days=1)).isoformat(), *chunk))
        restated = bool(prev and prev["max_stamp"] and rows.get("max_stamp")
                        and rows["max_stamp"] != prev["max_stamp"])
        conn.execute(
            "INSERT INTO pos_archive_days (restaurant_id, provider, business_date, tickets, lines, punches, "
            "net_sales, max_stamp, restated, version, links_ok, recon_gap, unknown_sales, archived_at, "
            "punches_read_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now')) "
            "ON CONFLICT(restaurant_id, provider, business_date) DO UPDATE SET tickets=excluded.tickets, "
            "lines=excluded.lines, punches=excluded.punches, net_sales=excluded.net_sales, "
            "max_stamp=excluded.max_stamp, restated=pos_archive_days.restated + ?, version=excluded.version, "
            "links_ok=excluded.links_ok, recon_gap=excluded.recon_gap, unknown_sales=excluded.unknown_sales, "
            "archived_at=datetime('now'), item_rollup=NULL, punches_read_at=datetime('now'), "
            "checked_at=NULL, suspect=NULL",
            (restaurant_id, name, iso, len(rows["tickets"]), len(rows["lines"]), len(rows["punches"]), net,
             rows.get("max_stamp"), 1 if restated else 0, ARCHIVE_VERSION, links_ok, gap, unknown,
             1 if restated else 0))
        # A day a record moved away from is rolled again (its item totals
        # counted the record), as a restated day is.
        for other in sorted(moved):
            conn.execute("UPDATE pos_archive_days SET item_rollup=NULL WHERE restaurant_id=? AND provider=? "
                         "AND business_date=?", (restaurant_id, name, other))
        # The day's lines by item (pos_item_daily, DB-11) in the same
        # transaction. A failure leaves the day unrolled (item_rollup NULL,
        # set just above): readers read its lines and the nightly roll
        # tries again - the archive itself stands.
        try:
            _roll_day(conn, restaurant_id, name, iso)
        except Exception as e:
            log.warning("pos_archive: %s's items for %s not rolled: %s", restaurant_id, iso, e)
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


def ensure_day(restaurant_id, day, db_path=DB_PATH, provider=None) -> dict:
    """archive_day's counts for a night, read from the POS only when the
    archive does not already hold it at the current layout with its links
    (blind re-audit 10/20/26 R3-02): the nightly report's service block
    archived the night again on every pass, seconds after the Sales block's
    read had stored it. A night a re-read came back short for keeps its
    stored copy (ArchiveShrunk) and is answered from it. `stored` True when
    nothing was read."""
    name, mod = provider or provider_for(restaurant_id)
    if mod is None:
        raise LookupError("no connected POS can archive tickets")
    iso = day.isoformat() if hasattr(day, "isoformat") else str(day)[:10]

    def _from(row):
        return {"date": iso, "tickets": row["tickets"], "lines": row["lines"], "punches": row["punches"],
                "net_sales": row["net_sales"], "restated": False, "links_ok": row.get("links_ok"),
                "recon_gap": row.get("recon_gap"), "unknown_sales": row.get("unknown_sales"), "stored": True}
    if iso in archived_dates(restaurant_id, name, db_path):
        row = _stored_row(restaurant_id, name, iso, db_path)
        if row:
            return _from(row)
    try:
        return archive_day(restaurant_id, day, db_path=db_path, provider=(name, mod))
    except ArchiveShrunk:
        row = _stored_row(restaurant_id, name, iso, db_path)
        if row:
            return _from(row)
        raise


def night_settled(restaurant_id, provider, day, now_utc=None, db_path=DB_PATH):
    """(settled, why) — whether the archive's copy of a night can be taken as
    the whole night (R3-05): not when its newest POS write landed within
    SETTLE_MINUTES before it was read or last checked (the cloud copy was
    still filling; the readers check it again after SETTLE_MINUTES). A
    night the archive does not hold, or whose stamps read in another clock
    (a newest write after the read), is settled as far as this can say —
    the caller's own rules stand."""
    if SETTLE_MINUTES <= 0:
        return True, None
    iso = day.isoformat() if hasattr(day, "isoformat") else str(day)[:10]
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT max_stamp, archived_at, checked_at FROM pos_archive_days WHERE restaurant_id=? "
                           "AND provider=? AND business_date=?", (restaurant_id, provider, iso)).fetchone()
    finally:
        conn.close()
    if not row or not row["max_stamp"]:
        return True, None
    newest = _utc(str(row["max_stamp"]).replace("T", " ")[:19])
    looked = max([t for t in (_utc(row["archived_at"]), _utc(row["checked_at"])) if t is not None], default=None)
    if newest is None or looked is None:
        return True, None
    lag = looked - newest
    if timedelta(0) <= lag < timedelta(minutes=SETTLE_MINUTES):
        return False, (f"the POS was still writing this night's checks {int(lag.total_seconds() // 60)} min "
                       "before it was read")
    return True, None


# ── the lines by item and day (scale audit 10/19/26 DB-11, DB-02) ─────────
#
# pos_item_daily: each archived day's lines grouped by item, name, filing
# and the per-line facts the Bar's walk needs (bar.variance.resolve_lines),
# with the sums - written with the day (archive_day), and for every older or
# re-versioned day by roll_item_days, the nightly prune's rollup for
# pos_ticket_lines, so no line is pruned before its day is rolled. Its own
# window (RETAIN_POS_ITEM_DAILY_DAYS) outlives the lines', so the line
# detail can be kept shorter (RETAIN_POS_LINES_DAYS) when the volume needs
# it. Raise ITEM_ROLLUP_VERSION when resolve_lines changes what it records:
# every day reads its lines again until it is rolled at the new version.
ITEM_ROLLUP_VERSION = 1
ITEM_ROLL_MAX_SECONDS = 120
_ROLL_LINE_COLS = ("line_id", "ticket_id", "business_date", "provider", "item_id", "item_name", "kind", "qty",
                   "sales", "cost", "aqu_count", "department", "sales_category", "category_id", "parent_line_id")


def _roll_day(conn, restaurant_id, provider, iso) -> int:
    """One archived day's pos_item_daily rows, replaced from its lines, and
    the day marked rolled at ITEM_ROLLUP_VERSION - inside the caller's
    transaction (the caller commits). Returns the groups written."""
    from bar.variance import resolve_lines, ROLLUP_KEY, ROLLUP_SUMS
    got = conn.execute(f"SELECT {', '.join(_ROLL_LINE_COLS)} FROM pos_ticket_lines WHERE restaurant_id=? "
                       "AND provider=? AND business_date=?", (restaurant_id, provider, iso)).fetchall()
    groups = resolve_lines(dict(zip(_ROLL_LINE_COLS, r)) for r in got)
    cols = ("restaurant_id",) + ROLLUP_KEY + ROLLUP_SUMS
    conn.execute("DELETE FROM pos_item_daily WHERE restaurant_id=? AND provider=? AND business_date=?",
                 (restaurant_id, provider, iso))
    if groups:
        conn.executemany(f"INSERT INTO pos_item_daily ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                         [(restaurant_id,) + tuple(g[c] for c in ROLLUP_KEY + ROLLUP_SUMS) for g in groups])
    conn.execute("UPDATE pos_archive_days SET item_rollup=? WHERE restaurant_id=? AND provider=? AND business_date=?",
                 (ITEM_ROLLUP_VERSION, restaurant_id, provider, iso))
    return len(groups)


def rolled_window(conn, restaurant_id, start, end) -> bool:
    """Whether pos_item_daily holds the whole window: at least one archived
    day in [start, end] and every one of them rolled at ITEM_ROLLUP_VERSION.
    Anything else reads the lines."""
    try:
        row = conn.execute("SELECT COUNT(*), SUM(CASE WHEN item_rollup = ? THEN 1 ELSE 0 END) FROM pos_archive_days "
                           "WHERE restaurant_id=? AND business_date>=? AND business_date<=?",
                           (ITEM_ROLLUP_VERSION, int(restaurant_id), str(start)[:10], str(end)[:10])).fetchone()
    except Exception:
        return False
    n, done = int(row[0] or 0), int(row[1] or 0)
    return n > 0 and done == n


def roll_item_days(db_path=None, max_seconds=None) -> dict:
    """Every archived day not rolled at ITEM_ROLLUP_VERSION, rolled - oldest
    first, a day a transaction, bounded by ITEM_ROLL_MAX_SECONDS. The
    nightly prune runs it as pos_ticket_lines' rollup before any line is
    deleted, so it raises when a day the line window has reached is still
    not rolled: the lines are then kept tonight. {rolled, left}."""
    import time as _t
    import db_dialect
    import ops
    stop = _t.monotonic() + float(ITEM_ROLL_MAX_SECONDS if max_seconds is None else max_seconds)
    conn = get_conn(db_path)
    try:
        todo = [tuple(r) for r in conn.execute(
            "SELECT restaurant_id, provider, business_date FROM pos_archive_days WHERE COALESCE(item_rollup, 0) <> ? "
            "ORDER BY business_date, restaurant_id LIMIT 5000", (ITEM_ROLLUP_VERSION,)).fetchall()]
    finally:
        conn.close()
    rolled = 0
    for rid, prov, iso in todo:
        if _t.monotonic() > stop:
            break
        conn = get_conn(db_path)
        try:
            db_dialect.begin_write(conn, "pos_archive", rid)
            still = conn.execute("SELECT 1 FROM pos_archive_days WHERE restaurant_id=? AND provider=? "
                                 "AND business_date=? AND COALESCE(item_rollup, 0) <> ?",
                                 (rid, prov, iso, ITEM_ROLLUP_VERSION)).fetchone()
            if still:               # archive_day may have stored and rolled it since the list was read
                _roll_day(conn, rid, prov, iso)
            conn.commit()
            rolled += 1
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            log.warning("pos_archive: %s's items for %s not rolled: %s", rid, iso, e)
        finally:
            conn.close()
    days = ops.retention_days("pos_ticket_lines")
    if days:
        edge = (date.today() - timedelta(days=int(days) + 2)).isoformat()
        conn = get_conn(db_path)
        try:
            due = conn.execute("SELECT COUNT(*) FROM pos_archive_days WHERE COALESCE(item_rollup, 0) <> ? "
                               "AND business_date < ?", (ITEM_ROLLUP_VERSION, edge)).fetchone()[0]
        finally:
            conn.close()
        if due:
            raise RuntimeError(f"{due} archived day(s) past the line window are not rolled into pos_item_daily "
                               "yet; their lines are kept tonight")
    return {"rolled": rolled, "left": len(todo) - rolled}


def archived_dates(restaurant_id, provider, db_path=DB_PATH) -> set:
    """Dates stored whole under the current layout (ARCHIVE_VERSION): an
    older day, or one whose modifier links failed to read (links_ok 0,
    POS-12), reads as missing, so the backfill stores it again."""
    return set(_archived_at(restaurant_id, provider, db_path))


def _archived_at(restaurant_id, provider, db_path=DB_PATH) -> dict:
    """{date: archived_at (UTC text)} for archived_dates' days."""
    conn = get_conn(db_path)
    try:
        return {r[0]: r[1] for r in conn.execute("SELECT business_date, archived_at FROM pos_archive_days "
                                                 "WHERE restaurant_id=? AND provider=? AND COALESCE(version, 1) >= ? "
                                                 "AND COALESCE(links_ok, 1) = 1",
                                                 (restaurant_id, provider, ARCHIVE_VERSION)).fetchall()}
    finally:
        conn.close()


def _state(restaurant_id, provider, db_path):
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM pos_archive_state WHERE restaurant_id=? AND provider=?",
                           (restaurant_id, provider)).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def _set_state(restaurant_id, provider, db_path, **fields):
    cols = list(fields)
    conn = get_conn(db_path)
    try:
        conn.execute(f"INSERT INTO pos_archive_state (restaurant_id, provider, {', '.join(cols)}) "
                     f"VALUES (?, ?{', ?' * len(cols)}) ON CONFLICT(restaurant_id, provider) DO UPDATE SET "
                     f"{', '.join(f'{c}=excluded.{c}' for c in cols)}",
                     (restaurant_id, provider, *[fields[c] for c in cols]))
        conn.commit()
    finally:
        conn.close()


def _utc(value):
    try:
        return datetime.fromisoformat(str(value)).replace(tzinfo=timezone.utc) if value else None
    except ValueError:
        return None


def _changes_checked_to(restaurant_id, provider, db_path):
    return _utc(_state(restaurant_id, provider, db_path).get("changes_checked_to"))


def _set_changes_checked_to(restaurant_id, provider, when, db_path):
    _set_state(restaurant_id, provider, db_path, changes_checked_to=when.strftime("%Y-%m-%d %H:%M:%S"))


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


# Menu prices are read weekly, or the night a sold line rang at a price the
# kept list does not have (scale audit 10/19/26 A3-07): the price list is
# 11 pages a night at Simple EJ's and changes a few times a year.
PRICE_SYNC_DAYS = int(__import__("os").getenv("POS_PRICE_SYNC_DAYS", "7"))


def _prices_due(restaurant_id, provider, night_iso, db_path) -> bool:
    """Whether tonight's run reads the POS's price list: never read, last
    read PRICE_SYNC_DAYS or more ago, or PRICE_MOVED_MIN_ITEMS or more items
    of `night_iso` rang at a price the kept list has a different figure for
    (item and level) on lines that rang at the item's own price — not a
    fraction (a half pour), not an override (a regular price apart from the
    price charged): one override read the 11-page list again any night
    (blind re-audit 10/20/26 R3-18). The manager on a line is not used: its
    id may be a placeholder on every line, as the punches' edit manager is
    at Simple EJ's."""
    if PRICE_SYNC_DAYS <= 1:
        return True
    conn = get_conn(db_path)
    try:
        last = _prices_read_at(conn, restaurant_id, provider)
        if not last:
            return True
        age = datetime.now(timezone.utc) - (_utc(last) or datetime.now(timezone.utc))
        if age >= timedelta(days=PRICE_SYNC_DAYS):
            return True
        moved = conn.execute(
            "SELECT COUNT(DISTINCT l.item_id) FROM pos_ticket_lines l JOIN pos_menu_prices p "
            "ON p.restaurant_id=l.restaurant_id AND p.provider=l.provider AND p.item_id=l.item_id "
            "AND p.level_id=l.price_level_id "
            "WHERE l.restaurant_id=? AND l.provider=? AND l.business_date=? AND l.kind='sale' AND l.price > 0 "
            "AND (l.fraction IS NULL OR l.fraction = 0 OR l.fraction = 1) "
            "AND (l.regular_price IS NULL OR l.regular_price = 0 OR ABS(l.regular_price - l.price) < 0.01) "
            "AND ABS(l.price - p.price) >= 0.01", (restaurant_id, provider, night_iso)).fetchone()[0]
        return int(moved or 0) >= PRICE_MOVED_MIN_ITEMS
    finally:
        conn.close()


PRICE_MOVED_MIN_ITEMS = 2


def _prices_read_at(conn, restaurant_id, provider):
    """When the price list was last read: pos_archive_state.prices_read_at,
    else (before 10/20/26) the newest price row's seen_at."""
    try:
        row = conn.execute("SELECT prices_read_at FROM pos_archive_state WHERE restaurant_id=? AND provider=?",
                           (restaurant_id, provider)).fetchone()
        if row and row[0]:
            return row[0]
    except Exception:
        pass
    return conn.execute("SELECT MAX(seen_at) FROM pos_menu_prices WHERE restaurant_id=? AND provider=?",
                        (restaurant_id, provider)).fetchone()[0]


def _keep_orders_due(restaurant_id, mod, today) -> None:
    """The next fortnight's orders due (catering, scheduled pick-up), read
    once a night for a store whose owner's questions have asked for them —
    a kept snapshot exists — so an owner's read on a web request answers
    from it and never reads RPOWER (blind re-audit 10/20/26 R3-07). Never
    raises."""
    fn = getattr(mod, "fetch_orders_due", None)
    if not callable(fn):
        return
    try:
        kind = str(getattr(mod, "ORDERS_DUE_KIND", "orders_due"))
        conn = get_conn()
        try:
            asked = conn.execute("SELECT 1 FROM pos_live_snapshot WHERE restaurant_id=? AND (kind=? OR kind LIKE ?) "
                                 "LIMIT 1", (restaurant_id, kind, kind + ":%")).fetchone()
        finally:
            conn.close()
        if asked:
            fn(restaurant_id, today, today + timedelta(days=int(getattr(mod, "ORDERS_DUE_KEEP_DAYS", 14))))
    except Exception as e:
        log.warning("pos_archive: orders due not kept for %s: %s", restaurant_id, e)


def run_for(restaurant_id, today=None, db_path=DB_PATH, now_utc=None) -> dict:
    """One restaurant's night: the last complete night, restated days, then
    backfill. `today` is the restaurant's current business date; without it
    the newest night is pos.complete_through — the last business date that
    has ENDED (food & bar audit 10/16/26 POS-08: at 4:30am Chicago the
    business date is still last night, so "today - 1" archived the night
    before last and last night waited a whole day). A night that recent is
    archived only once the POS has closed it (POS-16), so a partial night is
    never stored as whole; a store that never runs a close has it stored the
    next night, as backfill.

    Scale audit 10/19/26: the night is read ONCE (A3-05) — a night a reader
    already archived on demand after the close, at the current layout and
    with its links, and that the change read does not name, is not read
    again; a restated day already re-stored after the change read is not
    re-stored on a retry. The change cursor advances whenever the change read
    answered, and a restated day that failed is owed in `restate_pending`
    (A3-11) — one failing day no longer holds the window open to grow every
    night. A provider that asks us to wait (RPOWER's breaker, a long
    Retry-After) ends the night's reads at once; `retry_after` says when."""
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
    have_at = _archived_at(restaurant_id, name, db_path)
    have = set(have_at)
    state = _state(restaurant_id, name, db_path)
    # Restated days: rewritten in the POS since the last look.
    since = _utc(state.get("changes_checked_to")) or (now_utc - timedelta(hours=FIRST_CHANGE_LOOKBACK_HOURS))
    changed_dates, read_at = None, now_utc
    if _reads_changes(mod):
        try:
            changed_dates, read_at = _changed_stamps(restaurant_id, mod, since, now_utc, provider=name,
                                                     db_path=db_path)
        except Exception as e:                      # the night's archive still runs
            log.warning("pos_archive: change read failed for %s: %s", restaurant_id, e)
    try:
        pending = [d for d in __import__("json").loads(state.get("restate_pending") or "[]") if isinstance(d, str)]
    except ValueError:
        pending = []
    waiting, todo, skipped = [], [], []
    y_iso = yesterday.isoformat()
    # The newest POS write each stored day was read with: a date the change
    # read names is read again only when it carries a write AFTER that
    # (blind re-audit 10/20/26 R3-02). Every line of last night was written
    # after the previous night's cursor, so "named by the change read" alone
    # re-read last night every night — the third download of one night.
    stamps = _stored_stamps(restaurant_id, name, set(changed_dates or ()) | set(pending) | {y_iso}, db_path)
    if yesterday >= today - timedelta(days=1) and not _closed(mod, restaurant_id, yesterday):
        waiting.append(y_iso)
    elif (y_iso in have and changed_dates is not None and not _moved(y_iso, changed_dates, stamps)):
        skipped.append(y_iso)          # stored after its close, unchanged since: not read again
    else:
        todo.append(yesterday)
    read_stamp = read_at.strftime("%Y-%m-%d %H:%M:%S")

    def _owed(iso, owed_before):
        # Stored before the change read looked: re-store it. Stored after
        # (an earlier try of this row already did): nothing new to read.
        if not (iso in have and iso < y_iso and str(have_at.get(iso) or "") < read_stamp):
            return False
        return owed_before or _moved(iso, changed_dates or {}, stamps)
    restated_dates = [date.fromisoformat(d) for d in sorted(set(changed_dates or ()) | set(pending))
                      if _owed(d, d in pending)]
    todo += restated_dates
    # Backfill, newest first, toward BACKFILL_DAYS.
    floor = yesterday - timedelta(days=BACKFILL_DAYS - 1)
    missing = [yesterday - timedelta(days=i) for i in range(1, BACKFILL_DAYS)
               if (yesterday - timedelta(days=i)) >= floor and (yesterday - timedelta(days=i)).isoformat() not in have]
    # Paced (ONB-1): a store backfills only while it holds one of the fleet's
    # backfill slots and the volumes have room for tonight's days and the
    # backup after them. Last night and restated days are never held.
    tonight = missing[:BACKFILL_DAYS_PER_NIGHT]
    admission = {"admit": True}
    try:
        import onboarding
        admission = onboarding.backfill_admission(restaurant_id, name, len(missing), len(tonight),
                                                  db_path=None if db_path == DB_PATH else db_path)
    except Exception as e:                           # the archive's own bound stands
        log.warning("pos_archive: backfill admission failed for %s: %s", restaurant_id, e)
        admission = {"admit": True}
    if tonight and admission.get("admit"):
        todo += tonight
    else:
        tonight = []
    done, restated, failed, kept, retry_after = [], 0, [], [], None
    import pos_io
    history = {d for d in tonight if d not in restated_dates and d != yesterday}
    for d in dict.fromkeys(todo):
        if retry_after is not None:
            failed.append(d.isoformat())
            continue
        try:
            # A backfilled night is the "bulk" lane of the request budget;
            # last night and restated days stay "nightly" (R3-01).
            with pos_io.lane("bulk" if d in history else None):
                out = archive_day(restaurant_id, d, db_path=db_path, provider=(name, mod))
            done.append(out["date"])
            restated += 1 if out["restated"] else 0
        except ArchiveShrunk as e:
            # The stored night is kept against a short re-read (R3-05); it
            # is owed again tomorrow, and fails nothing else tonight.
            log.warning("pos_archive: %s %s kept: %s", restaurant_id, d, e)
            kept.append(d.isoformat())
        except Exception as e:
            log.warning("pos_archive: %s %s failed: %s", restaurant_id, d, e)
            failed.append(d.isoformat())
            if getattr(e, "retry_after", None):
                retry_after = float(e.retry_after)         # the provider asked us to wait: stop asking
    if changed_dates is not None:
        owed = sorted(d.isoformat() for d in restated_dates if d.isoformat() in failed or d.isoformat() in kept)
        _set_state(restaurant_id, name, db_path, changes_checked_to=read_stamp,
                   restate_pending=__import__("json").dumps(owed))
        # Every recent stored day the read found unchanged is checked as of
        # it, so the readers after this run do not ask the change feed the
        # same question again (R3-03).
        _mark_checked(restaurant_id, name, read_stamp, db_path,
                      floor_iso=(today - timedelta(days=FRESH_DAYS)).isoformat(),
                      exclude={d for d in changed_dates if _moved(d, changed_dates, stamps)}
                      | {d.isoformat() for d in todo} | set(kept))
    catalog = backfill_restated = None
    roles = prices = None
    if retry_after is None:
        try:
            catalog = sync_catalog(restaurant_id, db_path=db_path)
        except Exception as e:                       # the archive stands; the catalog tries again tomorrow
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
    if retry_after is None:
        try:
            roles = sync_roles(restaurant_id, db_path=db_path)
        except Exception as e:                       # the archive stands; roles try again tomorrow
            log.warning("pos_archive: role sync failed for %s: %s", restaurant_id, e)
        try:
            prices = (sync_prices(restaurant_id, db_path=db_path, today=today)
                      if _prices_due(restaurant_id, name, y_iso, db_path)
                      else {"ok": True, "skipped": f"read within {PRICE_SYNC_DAYS} days and no sold price moved"})
        except Exception as e:
            log.warning("pos_archive: price sync failed for %s: %s", restaurant_id, e)
        _keep_orders_due(restaurant_id, mod, today)
    return {"ok": not failed, "archived": done, "restated": restated, "failed": failed, "kept": kept,
            "not_closed": waiting, "unchanged": skipped, "retry_after": retry_after,
            "backfill_left": max(0, len(missing) - len(tonight)),
            "backfill_held": None if admission.get("admit") or not missing else admission.get("reason"),
            "provider": name, "roles": roles,
            "prices": prices, "catalog": catalog, "backfill_restated": backfill_restated}


# ── onboarding: a new store's history archived first (scale audit 10/19/26
# ONB-4 / ONB-6 / A3-12) ─────────────────────────────────────────────────────
#
# Before a store's archive started, every reader went live and nothing was
# kept: the first night read 60 days of ticketsales for labor, 45 days for
# dish sales and 45 for the DSR backfill — the same nights up to three
# times, ~835 requests. The connect now queues `pos_onboard`
# (strategy_jobs.run_queued_pos_onboard): ONBOARD_DAYS archived newest night
# first under ONBOARD_MAX_SECONDS, then the first sync, so every reader
# after it reads each night from the archive and the night is read once.
ONBOARD_DAYS = int(__import__("os").getenv("POS_ONBOARD_ARCHIVE_DAYS", "60"))
ONBOARD_MAX_SECONDS = int(__import__("os").getenv("POS_ONBOARD_MAX_SECONDS", str(20 * 60)))


def onboard(restaurant_id, days=None, max_seconds=None, db_path=DB_PATH) -> dict:
    """Archive the store's last `days` closed nights it lacks, newest first,
    for up to `max_seconds`. {archived, failed, left, retry_after}. A night
    still trading, or recent and not closed, is left for the nightly run."""
    import time as _t
    name, mod = provider_for(restaurant_id)
    if mod is None:
        return {"archived": [], "failed": [], "left": 0, "retry_after": None, "reason": "no POS that can archive"}
    days = int(days or ONBOARD_DAYS)
    limit = float(max_seconds or ONBOARD_MAX_SECONDS)
    from models import get_restaurant
    from time_utils import restaurant_now
    import pos
    r = get_restaurant(restaurant_id)
    yesterday = pos.complete_through(r, restaurant_now(r, naive=True))
    have = archived_dates(restaurant_id, name, db_path)
    todo = [yesterday - timedelta(days=i) for i in range(days)
            if (yesterday - timedelta(days=i)).isoformat() not in have]
    if todo and todo[0] == yesterday and not _closed(mod, restaurant_id, yesterday):
        todo = todo[1:]
    started = _t.monotonic()
    done, failed, retry_after = [], [], None
    import pos_io
    for i, d in enumerate(todo):
        if _t.monotonic() - started > limit:
            break
        try:
            with pos_io.lane("bulk"):              # history: never ahead of a live read (R3-01)
                archive_day(restaurant_id, d, db_path=db_path, provider=(name, mod))
            done.append(d.isoformat())
        except Exception as e:
            log.warning("pos_archive: onboarding %s %s failed: %s", restaurant_id, d, e)
            failed.append(d.isoformat())
            if getattr(e, "retry_after", None):
                retry_after = float(e.retry_after)
                break
    return {"archived": done, "failed": failed, "left": max(0, len(todo) - len(done) - len(failed)),
            "retry_after": retry_after}


# ── the nightly labor read from the archive (scale audit 10/19/26 A3-06) ────
#
# rpower.build_shifts_csv read 60 days of timeclock every night (9 week-sized
# requests) for punches the archive already held. It now reads live only the
# last few weeks — where a manager still edits punches — and takes the older
# days from pos_punches, but only days whose punches were read from the POS
# once they had settled (`punches_read_at` at least the live window after
# the day). The live read refreshes the archive's punches of the days it
# covers, so every day is re-read at the end of the window before labor
# takes it from the archive.

def _archive_reads_on(restaurant_id):
    import os
    if os.getenv("POS_ARCHIVE_READS", "1").strip() in ("0", "false", "off", "no"):
        return False
    return str(restaurant_id) not in {x.strip() for x in os.getenv("POS_ARCHIVE_READS_OFF", "").split(",")
                                      if x.strip()}


def settled_punch_dates(restaurant_id, provider, lo_iso, hi_iso, settle_days, db_path=DB_PATH) -> set:
    """Business dates in [lo, hi] the archive holds whose punches were last
    read from the POS `settle_days` or more after the date."""
    if not _archive_reads_on(restaurant_id):
        return set()
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT business_date, punches_read_at FROM pos_archive_days WHERE restaurant_id=? "
                            "AND provider=? AND business_date BETWEEN ? AND ? AND COALESCE(version, 1) >= ?",
                            (restaurant_id, provider, lo_iso, hi_iso, READ_MIN_VERSION)).fetchall()
    finally:
        conn.close()
    out = set()
    for d, at in rows:
        try:
            if at and str(at)[:10] >= (date.fromisoformat(str(d)[:10]) + timedelta(days=settle_days)).isoformat():
                out.add(str(d)[:10])
        except ValueError:
            continue
    return out


def archived_punches(restaurant_id, provider, dates, db_path=DB_PATH) -> list:
    """pos_punches rows of `dates` (business dates), as dicts."""
    if not dates:
        return []
    conn = get_conn(db_path)
    try:
        return [dict(r) for r in conn.execute(
            f"SELECT {', '.join(_PUNCH_COLS)} FROM pos_punches WHERE restaurant_id=? AND provider=? "
            f"AND business_date IN ({','.join('?' for _ in dates)})", (restaurant_id, provider, *dates)).fetchall()]
    finally:
        conn.close()


def _punch_key(p):
    return tuple((round(float(p.get(c)), 4) if isinstance(p.get(c), float) else p.get(c)) for c in _PUNCH_COLS)


def refresh_punches(restaurant_id, provider, by_day, db_path=DB_PATH) -> dict:
    """Store a fresh read of the punches of archived days: {iso: [punch
    records shaped as archive_rows shapes them]}. A day whose punches are
    unchanged only has `punches_read_at` stamped; a changed one has its
    punches replaced whole (and its count). A day the archive does not hold,
    or one being archived right now, is left alone. {"checked", "changed"}."""
    import ops
    out = {"checked": 0, "changed": 0}
    if not by_day:
        return out
    conn = get_conn(db_path)
    try:
        held = {r[0] for r in conn.execute(
            f"SELECT business_date FROM pos_archive_days WHERE restaurant_id=? AND provider=? "
            f"AND business_date IN ({','.join('?' for _ in by_day)})",
            (restaurant_id, provider, *by_day)).fetchall()}
    finally:
        conn.close()
    for iso in sorted(held):
        claim = f"pos_archive_day:{restaurant_id}:{provider}:{iso}"
        if not ops.claim_cooldown(claim, ARCHIVE_CLAIM_MINUTES):
            continue                                # being archived: that read stores the punches
        try:
            new = by_day[iso]
            conn = get_conn(db_path)
            try:
                old = [dict(r) for r in conn.execute(
                    f"SELECT {', '.join(_PUNCH_COLS)} FROM pos_punches WHERE restaurant_id=? AND provider=? "
                    f"AND business_date=?", (restaurant_id, provider, iso)).fetchall()]
                if old and not new:
                    # An empty answer never wipes a stored day, and does not
                    # settle it either: it stays unsettled and is read again.
                    conn.rollback()
                    continue
                same = sorted(map(_punch_key, old)) == sorted(map(_punch_key, new))
                if not same:
                    conn.execute("DELETE FROM pos_punches WHERE restaurant_id=? AND provider=? AND business_date=?",
                                 (restaurant_id, provider, iso))
                    marks = ",".join("?" for _ in range(len(_PUNCH_COLS) + 2))
                    conn.executemany(f"INSERT OR REPLACE INTO pos_punches (restaurant_id, provider, "
                                     f"{','.join(_PUNCH_COLS)}) VALUES ({marks})",
                                     [(restaurant_id, provider, *[p.get(c) for c in _PUNCH_COLS]) for p in new])
                conn.execute("UPDATE pos_archive_days SET punches_read_at=datetime('now')"
                             + ("" if same else ", punches=?") + " WHERE restaurant_id=? AND provider=? "
                             "AND business_date=?", ((() if same else (len(new),)) + (restaurant_id, provider, iso)))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
            out["checked"] += 1
            out["changed"] += 0 if same else 1
        finally:
            ops.release_period("cooldown", claim)
    return out


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
    tracked. The first read is the baseline and logs nothing.

    Audit #23 A3-21: a change is dated by the first sold line rung at the new
    price since the list was last read (the list is read weekly, so a Monday
    reprice was logged the next Sunday and outcomes windows misattributed
    it), and a price the POS no longer lists is dropped (a discontinued drink
    kept its last price in the bar's margins for good) — never on a read that
    lists under half of what is kept (a short read empties nothing)."""
    name, mod = provider_for(restaurant_id)
    fn = getattr(mod, "fetch_menu_prices", None) if mod else None
    if not callable(fn):
        return {"ok": False, "reason": "the POS does not share menu prices"}
    used = _levels_in_use(restaurant_id, name, db_path, today)
    if not used:
        return {"ok": False, "reason": "no archived sales yet to say which price levels are rung"}
    if today is None:
        import pos
        today = pos.local_complete_through(restaurant_id) + timedelta(days=1)
    # The main level is read over the same window as the levels in use (scale
    # audit 10/19/26 DB-10): it scanned every line the store ever sold, nightly.
    since = (today - timedelta(days=PRICE_LEVEL_LOOKBACK_DAYS)).isoformat()
    listed = fn(restaurant_id)
    rows = [r for r in listed if r["level_id"] in used]
    main = None
    first_sold = {}
    dropped = 0
    conn = get_conn(db_path)
    try:
        last_read = _prices_read_at(conn, restaurant_id, name)
        main = conn.execute(
            "SELECT price_level_id FROM pos_ticket_lines WHERE restaurant_id=? AND provider=? AND kind='sale' "
            "AND business_date>=? AND price_level_id IS NOT NULL GROUP BY price_level_id "
            "ORDER BY COUNT(*) DESC LIMIT 1", (restaurant_id, name, since)).fetchone()
        main = main[0] if main else None
        stored = {(r["item_id"], r["level_id"]): (r["price"], r["item_name"], r["level"]) for r in conn.execute(
            "SELECT item_id, level_id, price, item_name, level FROM pos_menu_prices WHERE restaurant_id=? "
            "AND provider=?", (restaurant_id, name)).fetchall()}
        have = {k: v[0] for k, v in stored.items()}
        baseline = not have
        changes = [(r, have[(r["item_id"], r["level_id"])]) for r in rows
                   if (r["item_id"], r["level_id"]) in have and abs(have[(r["item_id"], r["level_id"])] - r["price"]) >= 0.005]
        # Only a price (or its names) that moved, or is new, is written (blind
        # re-audit 10/20/26 R3-11): every tracked price was upserted on each
        # read; when the list was read is kept on pos_archive_state.
        put = [r for r in rows if not ((r["item_id"], r["level_id"]) in stored and _same_row(
            stored[(r["item_id"], r["level_id"])], (r["price"], r["item_name"], r["level"])))]
        # Prices the POS no longer lists (A3-21), on a read that lists at least
        # half of what is kept.
        keys = {(r["item_id"], r["level_id"]) for r in listed}
        gone = [k for k in stored if k not in keys]
        if gone and keys and len(keys) >= len(stored) / 2:
            conn.executemany("DELETE FROM pos_menu_prices WHERE restaurant_id=? AND provider=? AND item_id=? "
                             "AND level_id=?", [(restaurant_id, name, k[0], k[1]) for k in gone])
            dropped = len(gone)
        # The first night a moved price was rung (A3-21), since the list was
        # last read (a day's slack for the business date).
        since = ((_utc(last_read).date() - timedelta(days=1)).isoformat() if last_read and _utc(last_read)
                 else (today - timedelta(days=PRICE_LEVEL_LOOKBACK_DAYS)).isoformat())
        for r, _before in changes[:MAX_PRICE_CHANGES_LOGGED]:
            hit = conn.execute(
                "SELECT MIN(business_date) FROM pos_ticket_lines WHERE restaurant_id=? AND provider=? AND item_id=? "
                "AND price_level_id=? AND kind='sale' AND business_date>=? AND ABS(price - ?) < 0.01",
                (restaurant_id, name, r["item_id"], r["level_id"], since, float(r["price"]))).fetchone()
            if hit and hit[0]:
                first_sold[(r["item_id"], r["level_id"])] = str(hit[0])[:10]
        if put:
            conn.executemany(
                "INSERT INTO pos_menu_prices (restaurant_id, provider, item_id, level_id, item_name, level, price, "
                "seen_at) VALUES (?,?,?,?,?,?,?,datetime('now')) ON CONFLICT(restaurant_id, provider, item_id, "
                "level_id) DO UPDATE SET item_name=excluded.item_name, level=excluded.level, price=excluded.price, "
                "seen_at=excluded.seen_at",
                [(restaurant_id, name, r["item_id"], r["level_id"], r["item_name"], r["level"], r["price"])
                 for r in put])
        conn.execute("INSERT INTO pos_archive_state (restaurant_id, provider, prices_read_at) "
                     "VALUES (?, ?, datetime('now')) ON CONFLICT(restaurant_id, provider) DO UPDATE SET "
                     "prices_read_at=excluded.prices_read_at", (restaurant_id, name))
        conn.commit()
    finally:
        conn.close()
    if baseline:
        return {"ok": True, "baseline": True, "tracked": len(rows), "changes": 0, "dropped": dropped}
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
            cid = change_log.record(restaurant_id, "price", "sell_price", before, r["price"], subject=subject,
                                    source="sync", db_path=db_path, via="pos_archive.sync_prices")
            logged += 1
            when = first_sold.get((r["item_id"], r["level_id"]))
            if cid and when:
                _date_change(cid, restaurant_id, when, db_path)
        except Exception as e:
            log.warning("pos_archive: price change not logged rid=%s: %s", restaurant_id, e)
    return {"ok": True, "tracked": len(rows), "changes": len(changes), "logged": logged, "dropped": dropped,
            "dated_by_sale": len(first_sold)}


def _date_change(change_id, restaurant_id, business_date, db_path):
    """A logged price change dated by the first night it was rung (A3-21):
    that business date, never later than when it was logged."""
    conn = get_conn(db_path)
    try:
        conn.execute("UPDATE change_log SET changed_at=? WHERE id=? AND restaurant_id=? AND changed_at > ?",
                     (f"{business_date} 00:00:00", int(change_id), restaurant_id, f"{business_date} 00:00:00"))
        conn.commit()
    finally:
        conn.close()


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


def _reads_changes(mod) -> bool:
    """Whether a provider has a change read (dates, or dates with stamps)."""
    return any(callable(getattr(mod, n, None)) for n in ("changed_business_stamps", "changed_business_dates"))


def _norm_stamp(value):
    """A POS write stamp as comparable text ("YYYY-MM-DD HH:MM:SS"), or ''."""
    return str(value or "").replace("T", " ")[:19]


def _stamp_map(found) -> dict:
    """{date: newest write stamp or None} from a provider's change read — a
    dict (rpower: the newest write per date) or a set of dates (a stamp not
    known: the date counts as moved)."""
    if isinstance(found, dict):
        return {str(k)[:10]: (_norm_stamp(v) or None) for k, v in found.items()}
    return {str(k)[:10]: None for k in (found or ())}


def _stored_stamps(restaurant_id, provider, dates, db_path) -> dict:
    """{date: the newest POS write the stored day was read with} for `dates`."""
    dates = sorted({str(d)[:10] for d in dates if d})
    if not dates:
        return {}
    conn = get_conn(db_path)
    try:
        out = {}
        for i in range(0, len(dates), 500):
            chunk = dates[i:i + 500]
            for r in conn.execute(f"SELECT business_date, max_stamp FROM pos_archive_days WHERE restaurant_id=? "
                                  f"AND provider=? AND business_date IN ({','.join('?' for _ in chunk)})",
                                  (restaurant_id, provider, *chunk)).fetchall():
                out[str(r[0])[:10]] = _norm_stamp(r[1]) or None
        return out
    finally:
        conn.close()


def _moved(iso, changed, stored) -> bool:
    """Whether the change read says `iso` was written to after the stored
    copy was read (R3-02): named, and with a write stamp past the stored
    day's newest one — or with no stamp to compare on either side."""
    if iso not in (changed or {}):
        return False
    stamp, have = changed.get(iso), (stored or {}).get(iso)
    return not stamp or not have or stamp > have


def _mark_checked(restaurant_id, provider, at_stamp, db_path, floor_iso=None, exclude=(), only=None) -> None:
    """Stamp `checked_at` = `at_stamp` on stored days a change read made at
    that moment found unchanged — `only` those dates, else every day from
    `floor_iso` on outside `exclude` — so the readers' re-check of a recent
    day is not repeated until RECHECK_MINUTES after it (R3-03). A day stored
    after the read keeps its own time. Never raises."""
    try:
        sql = ("UPDATE pos_archive_days SET checked_at=? WHERE restaurant_id=? AND provider=? "
               "AND archived_at <= ? AND (checked_at IS NULL OR checked_at < ?)")
        args = [at_stamp, restaurant_id, provider, at_stamp, at_stamp]
        if only is not None:
            only = sorted({str(d)[:10] for d in only})
            if not only:
                return
            sql += f" AND business_date IN ({','.join('?' for _ in only)})"
            args += only
        else:
            sql += " AND business_date >= ?"
            args.append(floor_iso)
            ex = sorted({str(d)[:10] for d in exclude or ()})
            if ex:
                sql += f" AND business_date NOT IN ({','.join('?' for _ in ex)})"
                args += ex
        conn = get_conn(db_path)
        try:
            conn.execute(sql, args)
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.warning("pos_archive: checked days not stamped for %s: %s", restaurant_id, e)


def _changed_stamps(restaurant_id, mod, since_utc, now_utc, provider=None, db_path=DB_PATH):
    """({date: newest write stamp or None}, the read's own UTC time) — the
    provider's change read since `since_utc`, remembered for
    CHANGE_MEMO_SECONDS so the readers in a row ask once — in this process,
    and (scale audit 10/19/26 A3-08) in every process: the last read is kept
    on pos_archive_state (`change_read_at`, the moment it looked from, the
    dates with their newest stamps), so the web process, the scheduler
    thread and the queue worker each reading the same night no longer read
    the feeds four or five times. A kept read is used only when it looked
    from no later than `since_utc`."""
    import json as _json
    import time as _t
    hit = _change_memo.get(restaurant_id)
    if hit and _t.monotonic() - hit[0] < CHANGE_MEMO_SECONDS and hit[1] <= since_utc:
        return dict(hit[2]), hit[3]
    if provider:
        try:
            st = _state(restaurant_id, provider, db_path)
            at, lo = _utc(st.get("change_read_at")), _utc(st.get("change_read_since"))
            if (at and lo and lo <= since_utc and timedelta(0) <= now_utc - at < timedelta(seconds=CHANGE_MEMO_SECONDS)
                    and st.get("change_read_dates") is not None):
                out = _stamp_map(_json.loads(st["change_read_dates"]) or [])
                _remember_change(restaurant_id, lo, out, at)
                return dict(out), at
        except Exception as e:                       # the kept read is an optimisation
            log.warning("pos_archive: kept change read unreadable for %s: %s", restaurant_id, e)
    # A provider that says when each date was last written (rpower) is asked
    # for that; one that names dates only, for the dates.
    read = getattr(mod, "changed_business_stamps", None)
    out = _stamp_map((read if callable(read) else mod.changed_business_dates)(restaurant_id, since_utc, now_utc))
    _remember_change(restaurant_id, since_utc, out, now_utc)
    if provider:
        try:
            _set_state(restaurant_id, provider, db_path, change_read_at=now_utc.strftime("%Y-%m-%d %H:%M:%S"),
                       change_read_since=since_utc.strftime("%Y-%m-%d %H:%M:%S"),
                       change_read_dates=_json.dumps(dict(sorted(out.items()))))
        except Exception as e:
            log.warning("pos_archive: change read not kept for %s: %s", restaurant_id, e)
    return dict(out), now_utc


def _changed_since(restaurant_id, mod, since_utc, now_utc, provider=None, db_path=DB_PATH, with_time=False):
    """The dates of _changed_stamps (a set); `with_time`: (dates, the read's
    own UTC time)."""
    out, at = _changed_stamps(restaurant_id, mod, since_utc, now_utc, provider=provider, db_path=db_path)
    return (set(out), at) if with_time else set(out)


def _remember_change(restaurant_id, since_utc, out, at):
    import time as _t
    if len(_change_memo) >= CHANGE_MEMO_MAX:
        now = _t.monotonic()
        for k in [k for k, v in list(_change_memo.items()) if now - v[0] >= CHANGE_MEMO_SECONDS]:
            _change_memo.pop(k, None)
        while len(_change_memo) >= CHANGE_MEMO_MAX:
            _change_memo.pop(next(iter(_change_memo)), None)
    _change_memo[restaurant_id] = (_t.monotonic(), since_utc, dict(out), at)


def ready_dates(restaurant_id, provider, dates, db_path=DB_PATH, now_utc=None, live=True) -> set:
    """ISO dates among `dates` the archive can answer for, archiving closed
    days it lacks (ON_DEMAND_MAX per call) and re-storing a recent day the POS
    changed. An empty set means "read the POS", never an error. `live=False`
    (a web request): the stored days only, and no POS request of any kind.

    A recent day is re-checked RECHECK_MINUTES after it was stored or last
    checked — SETTLE_MINUTES when it was read while its checks were still
    arriving (night_settled) — and re-stored only when the change read names
    a write after the stored copy's newest one (R3-02); a check that finds
    it unchanged stamps `checked_at`, so the next reader does not ask the
    same question again (R3-03). A re-store that comes back short keeps the
    stored day, which is served (R3-05)."""
    import os
    if os.getenv("POS_ARCHIVE_READS", "1").strip() in ("0", "false", "off", "no"):
        return set()                 # the switch: every reader goes to the POS, as before 9/29/26
    # The same switch for named restaurants only (scale audit 10/19/26
    # A3-11): the fleet-wide one sent every store live at once (the 10/16
    # incident) when one store's archive was in doubt.
    if str(restaurant_id) in {x.strip() for x in os.getenv("POS_ARCHIVE_READS_OFF", "").split(",") if x.strip()}:
        return set()
    name, mod = provider_for(restaurant_id)
    if mod is None or name != provider or not archive_started(restaurant_id, provider, db_path):
        return set()
    now_utc = now_utc or datetime.now(timezone.utc)
    today = _business_today(restaurant_id)
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT business_date, archived_at, checked_at, max_stamp FROM pos_archive_days WHERE restaurant_id=? "
            "AND provider=? AND COALESCE(version, 1) >= ?", (restaurant_id, provider, READ_MIN_VERSION)).fetchall()
    finally:
        conn.close()
    stored = {r[0]: r[1] for r in rows}
    checked = {r[0]: r[2] for r in rows}
    newest = {r[0]: _norm_stamp(r[3]) or None for r in rows}
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
        looked = max([t for t in (_utc(stored[iso]), _utc(checked.get(iso))) if t is not None], default=None)
        if looked is None:
            continue
        settled, _why = (True, None) if SETTLE_MINUTES <= 0 else night_settled(restaurant_id, provider, iso,
                                                                               db_path=db_path)
        wait = timedelta(minutes=RECHECK_MINUTES if settled else SETTLE_MINUTES)
        if now_utc - looked > wait:
            stale[iso] = looked
    if stale and _reads_changes(mod):
        try:
            changed, at = _changed_stamps(restaurant_id, mod, min(stale.values()), now_utc, provider=provider,
                                          db_path=db_path)
        except Exception as e:
            # Unchecked is not unchanged (POS-16): the recent days the check
            # was for go to the POS this time, never served as fresh.
            log.warning("pos_archive: change read failed for %s: %s", restaurant_id, e)
            for iso in stale:
                ready.discard(iso)
            changed, at = None, None
        if changed is not None:
            moved = {iso for iso in stale if _moved(iso, changed, newest)}
            for iso in moved:
                ready.discard(iso)
                todo.append(date.fromisoformat(iso))
            _mark_checked(restaurant_id, provider, at.strftime("%Y-%m-%d %H:%M:%S"), db_path,
                          only=set(stale) - moved)
    for d in sorted(todo, reverse=True)[:ON_DEMAND_MAX]:
        try:
            archive_day(restaurant_id, d, db_path=db_path, provider=(name, mod))
            ready.add(d.isoformat())
        except ArchiveShrunk:
            if d.isoformat() in stored:
                ready.add(d.isoformat())             # the stored, larger night is the answer (R3-05)
        except Exception as e:
            log.warning("pos_archive: on-demand archive of %s %s failed: %s", restaurant_id, d, e)
    return ready
