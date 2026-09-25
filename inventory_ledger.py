"""
inventory_ledger.py — persistent per-ingredient stock ledger

Replaces the old inventory_csv blob (re-parsed fresh on every read, no
stable per-ingredient identity) with real ingredients rows plus an
append-only ingredient_stock_events log (recount / receiving / depletion /
waste). current_stock, avg_daily_usage, and waste_last_week are cached on
the ingredients row and recomputed from the ledger any time it changes —
never incrementally mutated in isolation, so a re-run can never silently
double-count.

A 'recount' event stores the absolute counted quantity and is the ledger's
anchor: current_stock = latest recount.qty + receiving since - depletion
since - waste since. "Latest" and "since" are by event_date, then row id
within one date: a count entered after a delivery but dated before it must
not erase the delivery (F2-7), and a depletion posted late for a night
before the count must not be subtracted from it. Every ingredient always has at least one recount because CSV
migration inserts one, so there's no "no recount yet" special case.

db_conn/get_conn are imported lazily inside each function (not at module
top level) so tests that monkeypatch models.get_conn to redirect at a
throwaway DB actually take effect here — a top-level `from models import
get_conn` would bind an import-time reference immune to that patch (see
tests/test_ask_cavnar.py's _redirect_db fixture for the exact gotcha).
"""
import logging
from datetime import date, timedelta

log = logging.getLogger("inventory_ledger")

_TREND_WINDOW_DAYS = 7
# How far back menu popularity is measured. Long enough to even out a quiet
# week, short enough to still describe the current menu.
_POPULARITY_WINDOW_DAYS = 28


def _days_per_month():
    """metrics.DAYS_PER_MONTH, the one month definition (NS3 L5)."""
    from metrics import DAYS_PER_MONTH
    return DAYS_PER_MONTH



def local_today(restaurant_id=None):
    """The restaurant's own calendar date (time_utils.restaurant_now_by_id),
    the one day boundary every ledger window takes. The server's
    date.today() is UTC on the host, so after 7pm Central "today" was
    tomorrow and every seven-day window slid a day early (DH1-14)."""
    try:
        from time_utils import restaurant_now_by_id
        return restaurant_now_by_id(restaurant_id).date()
    except Exception:
        return date.today()


def _as_date_str(d, restaurant_id=None) -> str:
    """An event date as YYYY-MM-DD. A string that is not an ISO date raises
    ValueError: the ledger orders and windows events by this text, and
    "9/21/26" sorts after every ISO date, so it sat inside every future
    7-day window (MOD-FC-15). No date is the restaurant's local today."""
    if d is None:
        return local_today(restaurant_id).isoformat()
    if hasattr(d, "isoformat"):
        return d.isoformat()[:10]
    s = str(d).strip()
    if len(s) != 10:
        raise ValueError(f"not an ISO date: {s!r}")
    return date.fromisoformat(s).isoformat()


def _today_on(conn, restaurant_id):
    """local_today, with the timezone read on an open connection."""
    try:
        row = conn.execute("SELECT timezone FROM restaurants WHERE id=?", (restaurant_id,)).fetchone()
        from time_utils import restaurant_now
        return restaurant_now((row["timezone"] if row else None) or None).date()
    except Exception:
        return local_today(restaurant_id)


# The window "waste this week" covers: the restaurant's today and the six
# days before it.
WASTE_WINDOW_DAYS = _TREND_WINDOW_DAYS


def waste_in_window(restaurant_id, end=None, days=WASTE_WINDOW_DAYS, conn=None) -> dict:
    """Waste per ingredient over the `days` ending `end` (default: the
    restaurant's local today), summed from the dated waste events at read
    time — one grouped query for the whole restaurant.

    The cached ingredients.waste_last_week was written only when that
    ingredient's ledger moved, so $400 of produce logged on 9/1 and never
    touched again still read "waste this week" on 9/24, in the food read,
    Ask, the waste alert, the daily snapshot and the opportunity figure
    (DH1-1). metrics._weekly_waste already read the events; this is the
    same rule per ingredient.

    Returns {"start", "end", "by_ingredient": {id: qty in window},
    "logged": {ids with any waste event ever}, "last_event": ISO date of
    the newest waste event or None}. An ingredient never logged is absent
    from `logged`: its figure is a manual entry, not a ledger reading."""
    own = conn is None
    if own:
        from models import get_conn
        conn = get_conn()
    try:
        if end is None:
            end_d = _today_on(conn, restaurant_id)
        elif hasattr(end, "isoformat"):
            end_d = end if not hasattr(end, "date") else end.date()
        else:
            end_d = date.fromisoformat(str(end)[:10])
        start_d = end_d - timedelta(days=days - 1)
        rows = conn.execute(
            "SELECT ingredient_id, "
            "COALESCE(SUM(CASE WHEN event_date >= ? AND event_date <= ? THEN qty END), 0) AS qty, "
            "MAX(CASE WHEN event_date <= ? THEN event_date END) AS last "
            "FROM ingredient_stock_events WHERE restaurant_id=? AND event_type='waste' "
            "GROUP BY ingredient_id",
            (start_d.isoformat(), end_d.isoformat(), end_d.isoformat(), restaurant_id)).fetchall()
    finally:
        if own:
            conn.close()
    by, lasts, logged, last = {}, {}, set(), None
    for r in rows:
        iid = r["ingredient_id"]
        logged.add(iid)
        by[iid] = round(float(r["qty"] or 0), 3)
        lasts[iid] = str(r["last"])[:10] if r["last"] else None
        if r["last"] and (last is None or str(r["last"])[:10] > last):
            last = str(r["last"])[:10]
    return {"start": start_d.isoformat(), "end": end_d.isoformat(), "by_ingredient": by,
            "last_by_ingredient": lasts, "logged": logged, "last_event": last}


def _compute_current_stock(conn, ingredient_id: int, restaurant_id: int = None, as_of: str = None) -> float:
    """Stock on hand for one ingredient, from the ledger — at the end of
    `as_of` (an ISO date) when given, else now.

    `restaurant_id` is optional only because every existing caller already
    checks ownership before reaching here. Pass it: this function reads and
    returns a financial quantity keyed on nothing but an integer id, and it
    is one unguarded future caller away from computing one restaurant's stock
    from another's events. When given, it is enforced on every read.
    """
    own = " AND restaurant_id=?" if restaurant_id is not None else ""
    own_args = (restaurant_id,) if restaurant_id is not None else ()
    scope, extra = own, own_args
    if as_of:
        scope += " AND event_date<=?"
        extra = extra + (str(as_of)[:10],)
    # The anchor is the count TAKEN last — newest event_date, then newest
    # row on that date — and what counts after it is anything dated later,
    # or the same day and entered after it. By row id alone, a count typed
    # in after a delivery but dated before it erased the delivery: stock 45
    # after a 40 lb delivery read 6 once Monday's count of 6 was entered on
    # Tuesday (F2-7). The same rule keeps a late-posted depletion for a
    # night before the count from being subtracted twice.
    recount = conn.execute(
        "SELECT id, qty, event_date FROM ingredient_stock_events "
        f"WHERE ingredient_id=?{scope} AND event_type='recount' ORDER BY event_date DESC, id DESC LIMIT 1",
        (ingredient_id, *extra)
    ).fetchone()
    if not recount:
        row = conn.execute(
            f"SELECT current_stock FROM ingredients WHERE id=?{own}",
            (ingredient_id, *own_args)).fetchone()
        return row["current_stock"] if row else 0.0

    stock = recount["qty"]
    deltas = conn.execute(
        "SELECT event_type, COALESCE(SUM(qty),0) AS total FROM ingredient_stock_events "
        f"WHERE ingredient_id=?{scope} AND (event_date>? OR (event_date=? AND id>?)) "
        "AND event_type IN ('receiving','depletion','waste') "
        "GROUP BY event_type",
        (ingredient_id, *extra, recount["event_date"], recount["event_date"], recount["id"])
    ).fetchall()
    for d in deltas:
        if d["event_type"] == "receiving":
            stock += d["total"]
        else:
            stock -= d["total"]
    return stock


def recompute_rollups(restaurant_id: int, ingredient_id: int, conn=None) -> None:
    """Refresh current_stock (always), and avg_daily_usage/waste_last_week
    (only when this ingredient actually has ledger depletion/waste history
    — otherwise a manually-entered value would get silently zeroed out for
    an ingredient that's never been recipe-mapped)."""
    _own_conn = conn is None
    if _own_conn:
        from models import get_conn
        conn = get_conn()
    try:
        current_stock = _compute_current_stock(conn, ingredient_id, restaurant_id)

        # The restaurant's own today, read on this connection (one row, no
        # second connection per ingredient in the nightly loop), DH1-14.
        window_start = (_today_on(conn, restaurant_id) - timedelta(days=_TREND_WINDOW_DAYS - 1)).isoformat()

        # One aggregate instead of six separate reads. This runs once per
        # ingredient touched by a business date inside compute_daily_depletion's
        # loop, so on a wide menu it was the dominant cost of the nightly sync:
        # six statements x every ingredient x every restaurant, every night.
        # Same numbers, one pass over the same index.
        agg = conn.execute(
            """SELECT
                 SUM(event_type='depletion')                                        AS n_depletion,
                 SUM(event_type='waste')                                            AS n_waste,
                 COALESCE(SUM(CASE WHEN event_type='depletion' AND event_date>=? THEN qty END),0) AS dep_qty,
                 COUNT(DISTINCT CASE WHEN event_type='depletion' AND event_date>=? THEN event_date END) AS dep_days,
                 COALESCE(SUM(CASE WHEN event_type='waste'     AND event_date>=? THEN qty END),0) AS waste_qty,
                 COALESCE(SUM(CASE WHEN event_type='receiving' AND event_date>=? THEN qty END),0) AS recv_qty,
                 MAX(CASE WHEN event_type='recount'   THEN id END)                   AS recount_id,
                 MAX(CASE WHEN event_type='receiving' THEN id END)                   AS last_recv_id
               FROM ingredient_stock_events
               WHERE ingredient_id=? AND restaurant_id=?""",
            (window_start, window_start, window_start, window_start,
             ingredient_id, restaurant_id)
        ).fetchone()

        # Scoped explicitly even though the id came from the restaurant-scoped
        # aggregate above. "Safe because of where the id came from" is exactly
        # the reasoning that left _compute_current_stock reading a financial
        # quantity off a bare integer; the guarantee belongs in the query.
        recount_date = None
        if agg and agg["recount_id"]:
            # The count taken last, by date — the same anchor as the stock
            # figure (F2-7), not the row typed in last.
            r = conn.execute(
                "SELECT MAX(event_date) AS d FROM ingredient_stock_events WHERE ingredient_id=? AND "
                "restaurant_id=? AND event_type='recount'", (ingredient_id, restaurant_id)).fetchone()
            recount_date = r["d"] if r else None

        # The cached figure every order, valuation and COGS read is never
        # below zero. Negative stock is not a kitchen: it is a recipe typed in
        # the wrong unit or a delivery nobody logged, and read as-is it grew
        # the suggested order and turned the stock value negative (MOD-FC-12).
        # The ledger keeps the true sum; the warning names the ingredient.
        #
        # Clamped, but not silently (CA3 F14): the ledger's own negative sum
        # is kept in count_discrepancy_qty, so the item is flagged as a count
        # discrepancy — "count this" — and left out of "critically low"
        # (inventory.analysis_for), where a 0 it never really had would
        # otherwise read as running out. Cleared (NULL) once the ledger is
        # back at or above zero, which a recount does.
        discrepancy = None
        if (current_stock or 0) < 0:
            log.warning(f"[inventory_ledger] restaurant {restaurant_id} ingredient {ingredient_id}: "
                        f"ledger reads {current_stock} — a recipe unit or a missing delivery; "
                        f"clamped to 0 and flagged as a count discrepancy until the next count")
            discrepancy = round(float(current_stock), 3)
            current_stock = 0.0
        # rollup_at stamps THIS recompute (UTC), apart from updated_at, which
        # any edit moves. The cached waste_last_week is only as current as
        # this stamp: nothing recomputes it as the calendar moves, so every
        # "waste this week" figure is summed from dated waste events at read
        # time instead (waste_in_window, DH1-1) and the stamp is the note on
        # the `waste` freshness source.
        sets, params = (["current_stock=?", "updated_at=datetime('now')", "count_discrepancy_qty=?",
                         "rollup_at=datetime('now')"],
                        [current_stock, discrepancy])
        if recount_date:
            sets.append("last_recount_at=?")
            params.append(recount_date)
        if agg and (agg["n_depletion"] or 0) > 0:
            # Divided by the days that actually carry depletion, not a flat 7.
            # A restaurant two days into a Toast connection had its usage
            # understated 3.5x, and one closed Mondays was understated ~14%
            # permanently — which overstates days_remaining, keeps items out
            # of critical_low, and runs the kitchen out of product.
            days_with_data = max(1, int(agg["dep_days"] or 0))
            sets.append("avg_daily_usage=?")
            params.append(round(float(agg["dep_qty"] or 0) / days_with_data, 3))
        if agg and (agg["n_waste"] or 0) > 0:
            sets.append("waste_last_week=?")
            params.append(round(float(agg["waste_qty"] or 0), 3))
        # Purchases over the SAME window as the waste figure above, not the
        # single most recent delivery. waste_last_week is a 7-day total;
        # dividing it by one delivery's quantity inflated waste_pct roughly in
        # proportion to how often the restaurant takes deliveries, which drove
        # the headline benchmark and cut suggested order quantities by up to
        # 40% through waste_adj.
        received = float(agg["recv_qty"] or 0) if agg else 0.0
        if received > 0:
            sets.append("last_order_qty=?")
            params.append(round(received, 3))
        elif agg and agg["last_recv_id"]:
            # Nothing received in the window — fall back to the last delivery
            # so an item ordered less often than weekly still has a denominator.
            lr = conn.execute(
                "SELECT qty FROM ingredient_stock_events WHERE id=? AND restaurant_id=?",
                (agg["last_recv_id"], restaurant_id)).fetchone()
            if lr:
                sets.append("last_order_qty=?")
                params.append(lr["qty"])
        params.append(ingredient_id)
        params.append(restaurant_id)
        # Scoped by restaurant_id as well as id. The tenant guarantee used to
        # rest entirely on callers checking ownership first; one caller that
        # forgot would rewrite another restaurant's cached stock and usage.
        conn.execute(f"UPDATE ingredients SET {', '.join(sets)} WHERE id=? AND restaurant_id=?", params)
        if _own_conn:
            conn.commit()
    finally:
        if _own_conn:
            conn.close()


def record_recount(restaurant_id: int, ingredient_id: int, counted_qty: float,
                    event_date=None, source: str = "manual", note: str = None,
                    infer_waste: bool = True) -> dict:
    """Insert an absolute recount event. If the ledger expected more stock
    than was actually counted, auto-insert an inferred 'waste' event for
    the gap — a recount finding MORE than expected (a prior undercount,
    typically) never fabricates negative waste, it's just logged.

    `infer_waste=False` anchors the count without calling the gap waste:
    an item 86'd at close RAN OUT IN SERVICE (closeout.py), so the gap
    between what the ledger expected and zero is usage the recipes did not
    capture, not stock thrown away — recorded as waste it inflated inferred
    waste and the "unexplained" share on every 86 (CA1 F20, fix I8)."""
    import math
    from models import db_conn
    event_date_str = _as_date_str(event_date, restaurant_id)
    counted_qty = float(counted_qty)
    if not math.isfinite(counted_qty) or counted_qty < 0:
        raise ValueError("a count must be a number of 0 or more")
    with db_conn() as conn:
        # The expectation is read and the waste written under one write lock.
        # Read outside it, two counts of the same item at once (two phones,
        # a double-submit) each inferred the full gap as waste (MOD-FC-14).
        conn.execute("BEGIN IMMEDIATE")
        # The pair used to be taken on trust: an admin URL carrying
        # /recount/<restaurant_id>/<ingredient_id> could write an event
        # tagged with one location against another location's ingredient,
        # leaving both ledgers wrong in opposite directions, permanently.
        if not ingredient_belongs_to(conn, restaurant_id, ingredient_id):
            conn.rollback()
            return {"ok": False, "error": "That ingredient isn't this restaurant's."}
        # What the ledger expected on the day the count was taken — not
        # today: a count dated before a delivery is compared with the stock
        # before that delivery, or the delivery read as waste (F2-7).
        expected = _compute_current_stock(conn, ingredient_id, as_of=event_date_str)
        gap = round(expected - counted_qty, 3)

        # The waste event (if any) must be inserted BEFORE the recount, on
        # the same date, so its row id is lower than the recount's — it
        # explains why the count came in below expectation, it isn't stock
        # that vanished AFTER the recount. _compute_current_stock only sums
        # same-day events with id > the anchor recount's id, so inserting
        # waste afterward would double-subtract the gap the recount's own
        # qty already bakes in.
        inferred_waste = 0.0
        if gap > 0 and not infer_waste:
            log.info(f"[inventory_ledger] recount for ingredient {ingredient_id} ({source}): "
                     f"gap of {gap} not inferred as waste")
        elif gap > 0:
            conn.execute(
                "INSERT INTO ingredient_stock_events "
                "(restaurant_id, ingredient_id, event_type, qty, event_date, source, note) "
                "VALUES (?,?,?,?,?,?,?)",
                (restaurant_id, ingredient_id, "waste", gap, event_date_str, "inferred",
                 f"inferred from recount gap ({expected} expected vs {counted_qty} counted)")
            )
            inferred_waste = gap
        else:
            log.info(f"[inventory_ledger] recount for ingredient {ingredient_id}: "
                     f"counted {counted_qty} >= expected {expected}, no waste inferred")

        cur = conn.execute(
            "INSERT INTO ingredient_stock_events "
            "(restaurant_id, ingredient_id, event_type, qty, event_date, source, note) "
            "VALUES (?,?,?,?,?,?,?)",
            (restaurant_id, ingredient_id, "recount", counted_qty, event_date_str, source, note)
        )
        recount_id = cur.lastrowid

        recompute_rollups(restaurant_id, ingredient_id, conn=conn)
        conn.commit()

    return {"recount_id": recount_id, "inferred_waste_qty": inferred_waste}


def record_receiving(restaurant_id: int, ingredient_id: int, qty: float,
                      event_date=None, source: str = "manual", note: str = None) -> int:
    """Stock that arrived. Returns the event id, or 0 when nothing was
    written. A delivery is a positive, finite quantity: -500 was accepted
    and drove stock to -400 (MOD-FC-12). A correction is a recount."""
    import math
    from models import db_conn
    try:
        qty = float(qty)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(qty) or qty <= 0:
        return 0
    event_date_str = _as_date_str(event_date, restaurant_id)
    with db_conn() as conn:
        if not ingredient_belongs_to(conn, restaurant_id, ingredient_id):
            return 0          # see record_recount's note on the untrusted pair
        # The delivery carries the price it arrived at, so a later invoice
        # does not re-price it in COGS (MOD-FC-23).
        price = conn.execute("SELECT unit_cost FROM ingredients WHERE id=? AND restaurant_id=?",
                             (ingredient_id, restaurant_id)).fetchone()
        cur = conn.execute(
            "INSERT INTO ingredient_stock_events "
            "(restaurant_id, ingredient_id, event_type, qty, event_date, source, note, unit_cost) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (restaurant_id, ingredient_id, "receiving", qty, event_date_str, source, note,
             price["unit_cost"] if price else None)
        )
        event_id = cur.lastrowid
        recompute_rollups(restaurant_id, ingredient_id, conn=conn)
        conn.commit()
    return event_id


def record_waste(restaurant_id: int, ingredient_id: int, qty: float, event_date=None,
                 reason: str = None, source: str = "logged") -> dict:
    """Waste someone saw and logged (friction audit U2-32): a positive,
    finite quantity of this restaurant's ingredient. Tagged `source='logged'`
    so waste_sources counts it as counted waste, never as the 'inferred' gap
    a recount leaves. Returns {"ok", "event_id", "name", "unit"}."""
    import math
    from models import db_conn
    try:
        qty = float(qty)
    except (TypeError, ValueError):
        return {"ok": False, "error": "Waste is a quantity above 0."}
    if not math.isfinite(qty) or qty <= 0 or qty > 1e6:
        return {"ok": False, "error": "Waste is a quantity above 0."}
    event_date_str = _as_date_str(event_date, restaurant_id)
    with db_conn() as conn:
        if not ingredient_belongs_to(conn, restaurant_id, ingredient_id):
            return {"ok": False, "error": "That ingredient isn't this restaurant's."}
        ing = conn.execute("SELECT name, unit FROM ingredients WHERE id=? AND restaurant_id=?",
                           (ingredient_id, restaurant_id)).fetchone()
        cur = conn.execute(
            "INSERT INTO ingredient_stock_events "
            "(restaurant_id, ingredient_id, event_type, qty, event_date, source, note) "
            "VALUES (?,?,?,?,?,?,?)",
            (restaurant_id, ingredient_id, "waste", qty, event_date_str, source,
             (reason or "")[:40] or None))
        event_id = cur.lastrowid
        recompute_rollups(restaurant_id, ingredient_id, conn=conn)
        conn.commit()
    return {"ok": True, "event_id": event_id, "name": ing["name"], "unit": ing["unit"] or ""}


def record_depletion_from_sale(restaurant_id: int, ingredient_id: int, qty: float,
                                event_date, source: str = "toast") -> int:
    """Records the event only — does NOT roll up current_stock/avg_daily_usage.
    compute_daily_depletion calls this in a batch per business date and
    rolls up once per ingredient afterward; a standalone caller must call
    recompute_rollups() itself to see the change reflected."""
    from models import db_conn
    event_date_str = _as_date_str(event_date, restaurant_id)
    with db_conn() as conn:
        # Its recount and receiving siblings both check the pair; this one
        # took it on trust, which is the same trap that let an event be
        # written against another location's ingredient.
        if not ingredient_belongs_to(conn, restaurant_id, ingredient_id):
            return 0
        cur = conn.execute(
            "INSERT INTO ingredient_stock_events "
            "(restaurant_id, ingredient_id, event_type, qty, event_date, source) "
            "VALUES (?,?,?,?,?,?)",
            (restaurant_id, ingredient_id, "depletion", qty, event_date_str, source)
        )
        event_id = cur.lastrowid
        conn.commit()
    return event_id


def compute_daily_depletion(restaurant_id: int, business_date) -> dict:
    """Pull one Toast business date's sold items, match against
    recipe_ingredients via menu_items.toast_guid, and record depletion.
    Idempotent: any existing toast-sourced depletion rows for this exact
    business date are deleted before the freshly computed set is inserted,
    inside the same transaction — safe to re-run (nightly retry, manual
    resync) without double-depleting stock."""
    import pos as _pos
    from models import db_conn

    business_date_str = _as_date_str(business_date, restaurant_id)
    real_date = business_date if hasattr(business_date, "isoformat") else date.fromisoformat(business_date_str)
    # Through pos.py, not toast directly. Recipe depletion was Toast-only for
    # no reason other than this import: any provider that can report
    # item-level sales can drive it, and one that cannot now RAISES rather
    # than returning [] — an empty selection list looks identical to "sold
    # nothing", which would zero every ingredient's usage, overstate days
    # remaining and quietly empty the reorder list.
    selections, _provider = _pos.fetch_order_selections(restaurant_id, real_date)
    # The provider that reported the sales labels the events (DH1-13): an
    # RPOWER night was stored as source='toast'. Rows written before this
    # carry 'toast' whatever the provider, so they are matched too.
    src = str(_provider or "toast")

    sold_by_guid = {}
    for sel in selections:
        guid = (sel.get("item") or {}).get("guid")
        if not guid:
            continue
        sold_by_guid[guid] = sold_by_guid.get(guid, 0) + float(sel.get("quantity", 0) or 0)

    ingredients_updated = set()
    unmapped = []

    with db_conn() as conn:
        # One transaction, explicitly. This block deletes the day's depletion
        # events and rebuilds them, which is what makes a re-run idempotent —
        # but between the DELETE and the last INSERT the day is empty. It was
        # safe only because sqlite3's default isolation opens an implicit
        # transaction and rolls back when a connection closes uncommitted, so
        # a crash mid-loop undid the delete too. That is a real guarantee
        # resting on an implicit default: anyone setting isolation_level=None
        # on get_conn would silently turn a crash here into a day of
        # inventory quietly reading as zero depletion. Say it out loud.
        conn.execute("BEGIN IMMEDIATE")
        qty_by_ingredient = {}

        for guid, qty_sold in sold_by_guid.items():
            menu_item = conn.execute(
                "SELECT id, name FROM menu_items WHERE restaurant_id=? AND toast_guid=? AND is_active=1",
                (restaurant_id, guid)
            ).fetchone()
            if not menu_item:
                unmapped.append({"toast_guid": guid, "qty_sold": qty_sold, "reason": "menu item not discovered"})
                continue

            # Units sold, recorded whether or not a recipe exists: this is the
            # popularity half of menu engineering, and it was being read from
            # Toast and thrown away. Same idempotency as the depletion rows —
            # re-running a business date replaces rather than accumulates.
            conn.execute(
                "INSERT INTO menu_item_sales (restaurant_id, menu_item_id, business_date, qty_sold) "
                "VALUES (?,?,?,?) ON CONFLICT(restaurant_id, menu_item_id, business_date) "
                "DO UPDATE SET qty_sold=excluded.qty_sold",
                (restaurant_id, menu_item["id"], business_date_str, qty_sold)
            )

            recipe_rows = conn.execute(
                "SELECT ingredient_id, qty_per_unit FROM recipe_ingredients WHERE menu_item_id=?",
                (menu_item["id"],)
            ).fetchall()
            if not recipe_rows:
                unmapped.append({"toast_guid": guid, "menu_item": menu_item["name"],
                                  "qty_sold": qty_sold, "reason": "no recipe configured"})
                continue

            for r in recipe_rows:
                qty_by_ingredient[r["ingredient_id"]] = (qty_by_ingredient.get(r["ingredient_id"], 0.0)
                                                         + r["qty_per_unit"] * qty_sold)

        # One row per ingredient per business date, UPDATED in place so it
        # keeps its id. Stock counts events with id > the latest recount, and
        # the nightly window re-syncs the last three days: deleting and
        # re-inserting gave the days before a count new, higher ids, so a
        # count of 80 read as 60 the next morning — two days' sales
        # subtracted twice, and the next count's shrink masked (MOD-FC-4).
        existing = conn.execute(
            "SELECT id, ingredient_id FROM ingredient_stock_events "
            "WHERE restaurant_id=? AND event_date=? AND event_type='depletion' AND source IN (?, 'toast') ORDER BY id",
            (restaurant_id, business_date_str, src)).fetchall()
        keep, stale = {}, []
        for row in existing:
            if row["ingredient_id"] in qty_by_ingredient and row["ingredient_id"] not in keep:
                keep[row["ingredient_id"]] = row["id"]
            else:
                stale.append(row)
        for row in stale:
            conn.execute("DELETE FROM ingredient_stock_events WHERE id=? AND restaurant_id=?",
                         (row["id"], restaurant_id))
            ingredients_updated.add(row["ingredient_id"])
        for ingredient_id, qty in qty_by_ingredient.items():
            if ingredient_id in keep:
                conn.execute("UPDATE ingredient_stock_events SET qty=?, source=? WHERE id=? AND restaurant_id=?",
                             (qty, src, keep[ingredient_id], restaurant_id))
            else:
                conn.execute(
                    "INSERT INTO ingredient_stock_events "
                    "(restaurant_id, ingredient_id, event_type, qty, event_date, source) "
                    "VALUES (?,?,?,?,?,?)",
                    (restaurant_id, ingredient_id, "depletion", qty, business_date_str, src))
            ingredients_updated.add(ingredient_id)

        # Every ingredient still carrying a usage figure from sales, not just
        # the ones sold today: usage is a trailing window, and an ingredient
        # whose only dish came off the menu was never recomputed again, so
        # its last usage froze and it kept being reordered (MOD-FC-13).
        recompute = set(ingredients_updated)
        for r in conn.execute(
                "SELECT i.id FROM ingredients i WHERE i.restaurant_id=? AND i.is_active=1 "
                "AND COALESCE(i.avg_daily_usage,0) > 0 AND EXISTS (SELECT 1 FROM ingredient_stock_events e "
                "WHERE e.ingredient_id=i.id AND e.restaurant_id=i.restaurant_id AND e.event_type='depletion')",
                (restaurant_id,)).fetchall():
            recompute.add(r["id"])

        for ingredient_id in recompute:
            recompute_rollups(restaurant_id, ingredient_id, conn=conn)
        conn.commit()

    if unmapped:
        log.warning(f"[inventory_ledger] restaurant {restaurant_id} on {business_date_str}: "
                    f"{len(unmapped)} unmapped selection(s) — {unmapped}")

    sold_total = sum(sold_by_guid.values())
    covered = sold_total - sum(float(u.get("qty_sold") or 0) for u in unmapped)
    return {
        "ingredients_updated": len(ingredients_updated),
        "unmapped_selections": unmapped,
        # Coverage was only ever logged. Everything downstream — usage, days
        # remaining, reorder urgency — is only as good as the share of sales
        # a recipe actually accounts for, and the owner had no way to see it.
        "units_sold": round(sold_total, 2),
        "units_covered": round(covered, 2),
        "coverage_pct": round(covered / sold_total * 100, 1) if sold_total > 0 else None,
    }


def _window(days, as_of, restaurant_id=None):
    """(first day, last day or None) of a `days`-long window. Anchored on
    the restaurant's local today with no upper bound by default; on `as_of`
    (a past business date — the nightly report) it ends there, so later
    events stay out."""
    if as_of:
        end = _as_date_str(as_of)
        return (date.fromisoformat(end) - timedelta(days=days - 1)).isoformat(), end
    return (local_today(restaurant_id) - timedelta(days=days - 1)).isoformat(), None


def waste_sources(restaurant_id: int, days: int = _TREND_WINDOW_DAYS, as_of=None) -> dict:
    """How much of this week's recorded waste was actually counted, and how
    much was inferred from a recount coming in under expectation.

    record_recount writes the gap as a waste event tagged source='inferred',
    which is the right call for the ledger — but nothing downstream
    distinguished it, so a miscount was reported to the owner as money
    wasted. An owner can act on "you wasted $200 of produce"; they can only
    act on "$200 of the gap is unexplained" by counting more carefully.

    `as_of`: the window ends on that date instead of today (see _window).
    """
    from models import get_conn
    conn = get_conn()
    try:
        window_start, window_end = _window(days, as_of, restaurant_id)
        rows = conn.execute(
            "SELECT COALESCE(e.source,'manual') AS src, "
            "       COALESCE(SUM(e.qty * COALESCE(i.unit_cost,0)), 0) AS cost "
            "FROM ingredient_stock_events e "
            "JOIN ingredients i ON i.id = e.ingredient_id AND i.restaurant_id = e.restaurant_id "
            "WHERE e.restaurant_id=? AND e.event_type='waste' AND e.event_date>=? "
            + ("AND e.event_date<=? " if window_end else "")
            + "GROUP BY COALESCE(e.source,'manual')",
            (restaurant_id, window_start) + ((window_end,) if window_end else ()),
        ).fetchall()
    finally:
        conn.close()
    by_source = {r["src"]: round(float(r["cost"] or 0), 2) for r in rows}
    inferred = by_source.get("inferred", 0.0)
    counted = round(sum(v for k, v in by_source.items() if k != "inferred"), 2)
    total = round(counted + inferred, 2)
    return {
        "window_days": days,
        "counted": counted,
        "inferred": inferred,
        "total": total,
        "inferred_pct": round(inferred / total * 100, 1) if total > 0 else None,
        "has_data": bool(rows),
        # Which ingredients the unexplained gap is actually in. This used to
        # be aggregated away to a single dollar figure, which is the one
        # number an owner cannot act on — see inferred_variance below.
        "top_inferred": inferred_variance(restaurant_id, days=days, as_of=as_of)["ingredients"][:5],
    }


# A gap has to be a real share of what the recipes said should have been used
# before it means anything. Below this it is ordinary count noise — a scale
# read, a partial case, a rounding — not a portioning problem.
MIN_VARIANCE_PCT = 8.0
# And it has to be worth money. A 30% variance on $4 of parsley is not a
# finding; it is a distraction from the 9% variance on the ribeye.
MIN_VARIANCE_DOLLARS = 15.0

# Well above any real menu — a ceiling against an unbounded Toast catalogue,
# not a paging window the UI is expected to walk.
MENU_PAGE_SIZE = 500


def inferred_variance(restaurant_id: int, days: int = _TREND_WINDOW_DAYS, as_of=None) -> dict:
    """Where theoretical usage and actual usage disagree, by ingredient and
    by the dishes that ingredient goes into.

    record_recount already writes the gap between what the ledger expected
    and what was physically counted as a waste event tagged source='inferred'
    — per ingredient, with the expected and counted quantities in its note.
    That gap IS over-portioning, prep loss or theft. waste_sources aggregated
    it into one restaurant-wide dollar figure and discarded which ingredient
    produced it, so the module held the measurement and could not name it.

    The variance is expressed against theoretical depletion over the same
    window (recipe quantity x units sold), because a 6-unit gap means very
    different things on an ingredient that depleted 40 units and one that
    depleted 600.

    `dishes` attributes each ingredient's variance to the menu items that use
    it, weighted by how much of that ingredient each dish actually consumed in
    the window. A dish is named only when a recipe binds it to the ingredient
    — this never guesses which dish is over-portioned, it reports which
    dishes are the candidates and how much of the consumption each accounts
    for.

    `as_of`: the window ends on that date instead of today (see _window).
    """
    from models import get_conn
    conn = get_conn()
    try:
        window_start, window_end = _window(days, as_of, restaurant_id)
        upto = " AND e.event_date<=?" if window_end else ""
        rows = conn.execute(
            """SELECT i.id, i.name, i.unit, COALESCE(i.unit_cost,0) AS unit_cost,
                      COALESCE(SUM(CASE WHEN e.event_type='waste' AND e.source='inferred'
                                        THEN e.qty END),0) AS gap_qty,
                      COALESCE(SUM(CASE WHEN e.event_type='depletion' THEN e.qty END),0) AS theoretical_qty,
                      COUNT(DISTINCT CASE WHEN e.event_type='waste' AND e.source='inferred'
                                          THEN e.event_date END) AS recounts
                 FROM ingredients i
                 JOIN ingredient_stock_events e
                   ON e.ingredient_id=i.id AND e.restaurant_id=i.restaurant_id
                WHERE i.restaurant_id=? AND e.event_date>=?""" + upto + """
                GROUP BY i.id
               HAVING gap_qty > 0""",
            (restaurant_id, window_start) + ((window_end,) if window_end else ()),
        ).fetchall()

        out = []
        for r in rows:
            gap, theo = float(r["gap_qty"] or 0), float(r["theoretical_qty"] or 0)
            cost = round(gap * float(r["unit_cost"] or 0), 2)
            # No theoretical usage means no recipe depleted this ingredient,
            # so there is no baseline to call the gap large or small against.
            # Reported with pct=None rather than dropped or divided by zero.
            pct = round(gap / theo * 100, 1) if theo > 0 else None
            out.append({
                "ingredient_id": r["id"], "ingredient": r["name"],
                "unit": r["unit"] or "", "gap_qty": round(gap, 3),
                "theoretical_qty": round(theo, 3),
                "variance_pct": pct, "cost": cost,
                "monthly_cost": round(cost * (_days_per_month() / days), 2),
                # The recounts (days) that showed a gap — the portion
                # driver's Evidence Strength (confidence_engine N_FULL
                # "recounts"): one recount's gap can be a miscount.
                "recounts": int(r["recounts"] or 0),
                "material": bool(pct is not None and pct >= MIN_VARIANCE_PCT
                                 and cost >= MIN_VARIANCE_DOLLARS),
            })

        # Which dishes consume each flagged ingredient, and in what share.
        material_ids = [e["ingredient_id"] for e in out if e["material"]]
        dishes = []
        if material_ids:
            marks = ",".join("?" for _ in material_ids)
            for d in conn.execute(
                f"""SELECT ri.ingredient_id AS iid, m.id AS mid, m.name AS dish,
                           ri.qty_per_unit,
                           COALESCE(SUM(s.qty_sold),0) AS units_sold
                      FROM recipe_ingredients ri
                      JOIN menu_items m ON m.id = ri.menu_item_id AND m.restaurant_id=?
                      LEFT JOIN menu_item_sales s
                        ON s.menu_item_id = m.id AND s.restaurant_id = m.restaurant_id
                       AND s.business_date >= ?{" AND s.business_date <= ?" if window_end else ""}
                     WHERE ri.ingredient_id IN ({marks})
                     GROUP BY ri.ingredient_id, m.id""",
                (restaurant_id, window_start, *((window_end,) if window_end else ()), *material_ids),
            ).fetchall():
                consumed = float(d["qty_per_unit"] or 0) * float(d["units_sold"] or 0)
                if consumed > 0:
                    dishes.append({"ingredient_id": d["iid"], "menu_item_id": d["mid"],
                                   "dish": d["dish"], "consumed_qty": round(consumed, 3),
                                   "units_sold": round(float(d["units_sold"] or 0), 2)})
    finally:
        conn.close()

    # Share of each ingredient's consumption per dish, so the caller can say
    # "the ribeye accounts for 78% of what used that cut" rather than merely
    # listing every dish the ingredient appears in.
    by_ing = {}
    for d in dishes:
        by_ing.setdefault(d["ingredient_id"], []).append(d)
    for iid, ds in by_ing.items():
        tot = sum(x["consumed_qty"] for x in ds)
        for x in ds:
            x["share"] = round(x["consumed_qty"] / tot, 3) if tot > 0 else None
        ds.sort(key=lambda x: x["consumed_qty"], reverse=True)
    for e in out:
        e["dishes"] = by_ing.get(e["ingredient_id"], [])[:3]

    out.sort(key=lambda e: e["cost"], reverse=True)
    material = [e for e in out if e["material"]]
    return {
        "window_days": days,
        "ingredients": out,
        "material": material,
        "material_monthly_cost": round(sum(e["monthly_cost"] for e in material), 2),
        "min_variance_pct": MIN_VARIANCE_PCT,
        "min_variance_dollars": MIN_VARIANCE_DOLLARS,
        "basis": (f"Gap between recipe-theoretical usage and physical counts over {days} days. "
                  f"Reported when the gap is at least {MIN_VARIANCE_PCT:g}% of theoretical usage "
                  f"AND at least ${MIN_VARIANCE_DOLLARS:g}. A gap is over-portioning, prep loss "
                  f"or shrink — it is not counted waste."),
    }


def recipe_coverage(restaurant_id: int, days: int = _POPULARITY_WINDOW_DAYS) -> dict:
    """What share of what this restaurant sold is accounted for by a recipe.

    The single most important honesty indicator in the module: a dish with no
    recipe depletes nothing, so its ingredients look like they are never used.
    That understates usage, overstates days remaining, and keeps items out of
    the reorder list — silently, and worst for the dishes that sell most.
    """
    from models import get_conn
    conn = get_conn()
    try:
        window_start = (local_today(restaurant_id) - timedelta(days=days - 1)).isoformat()
        rows = conn.execute(
            "SELECT s.menu_item_id AS mid, m.name AS name, SUM(s.qty_sold) AS qty, "
            "       (SELECT COUNT(*) FROM recipe_ingredients ri WHERE ri.menu_item_id=s.menu_item_id) AS n "
            "FROM menu_item_sales s JOIN menu_items m ON m.id = s.menu_item_id "
            "WHERE s.restaurant_id=? AND s.business_date>=? "
            "GROUP BY s.menu_item_id ORDER BY qty DESC",
            (restaurant_id, window_start),
        ).fetchall()
    finally:
        conn.close()
    total = sum(float(r["qty"] or 0) for r in rows)
    covered = sum(float(r["qty"] or 0) for r in rows if (r["n"] or 0) > 0)
    gaps = [{"name": r["name"], "units_sold": round(float(r["qty"] or 0), 2)}
            for r in rows if not (r["n"] or 0)]
    return {
        "window_days": days,
        "units_sold": round(total, 2),
        "units_covered": round(covered, 2),
        "coverage_pct": round(covered / total * 100, 1) if total > 0 else None,
        "uncovered_top": gaps[:8],
        "uncovered_count": len(gaps),
        "has_data": bool(rows),
    }


def discover_menu_items(restaurant_id: int, days: int = 7) -> dict:
    """Scan a recent window of real Toast business dates (via
    fetch_business_days, so we only hit days Toast actually reports rather
    than guessing) and upsert every distinct item.guid seen in their order
    selections into menu_items — the one-time-ish admin action that seeds
    the recipe editor without a separate Toast Menus API integration.
    Safe to re-run: upserts by (restaurant_id, toast_guid), never duplicates."""
    import pos as _pos
    from models import db_conn

    end = local_today(restaurant_id)
    # Toast's demo mode short-circuits fetch_order_selections but not
    # fetch_business_days, so a showcase restaurant would otherwise make a
    # real HTTP call with a fake token. Asked of the toast module directly
    # because it is a Toast-specific fixture, and only when Toast is the
    # provider — other providers have no such mode.
    _name, _mod = _pos.connected_provider(restaurant_id)
    _is_toast_demo = bool(
        _name == "toast" and getattr(_mod, "_is_demo", None) and _mod._is_demo(restaurant_id))
    if _is_toast_demo:
        business_dates = [end.isoformat()]
    else:
        _days, _ = _pos.fetch_business_days(restaurant_id, end - timedelta(days=days), end)
        business_dates = sorted(_days.keys())

    seen = {}  # guid -> display name
    for bd_str in business_dates:
        bd = date.fromisoformat(bd_str)
        _sels, _ = _pos.fetch_order_selections(restaurant_id, bd)
        for sel in _sels:
            guid = (sel.get("item") or {}).get("guid")
            if not guid:
                continue
            seen[guid] = sel.get("displayName") or seen.get(guid) or guid

    discovered, updated = 0, 0
    with db_conn() as conn:
        for guid, name in seen.items():
            existing = conn.execute(
                "SELECT id, name FROM menu_items WHERE restaurant_id=? AND toast_guid=?",
                (restaurant_id, guid)
            ).fetchone()
            if existing:
                if existing["name"] != name:
                    conn.execute("UPDATE menu_items SET name=? WHERE id=?", (name, existing["id"]))
                    updated += 1
                continue
            conn.execute(
                "INSERT INTO menu_items (restaurant_id, toast_guid, name) VALUES (?,?,?)",
                (restaurant_id, guid, name)
            )
            discovered += 1
        conn.commit()

    return {"discovered": discovered, "updated": updated, "total_seen": len(seen)}


def import_csv_to_ingredients(restaurant_id: int) -> dict:
    """One-time admin migration: parses client_data.inventory_csv via the
    existing load_inventory(), upserts one ingredients row per item keyed
    on (restaurant_id, name) — safe to re-run, already-imported names are
    skipped rather than duplicated. inventory_csv itself is left untouched
    as an archival fallback."""
    from inventory import load_inventory
    from models import get_client_data, db_conn

    data = get_client_data(restaurant_id)
    if not data or not data.get("inventory_csv"):
        return {"imported": 0, "skipped_existing": 0, "error": "No inventory_csv found for this restaurant"}

    items = load_inventory(csv_string=data["inventory_csv"])
    today_str = local_today(restaurant_id).isoformat()
    imported, skipped = 0, 0

    with db_conn() as conn:
        for item in items:
            existing = conn.execute(
                "SELECT id FROM ingredients WHERE restaurant_id=? AND LOWER(TRIM(name))=LOWER(TRIM(?)) AND is_active=1",
                (restaurant_id, item["item"])
            ).fetchone()
            if existing:
                skipped += 1
                continue

            cur = conn.execute(
                "INSERT INTO ingredients "
                "(restaurant_id, name, category, unit, par_level, unit_cost, case_size, "
                " current_stock, avg_daily_usage, last_order_qty, waste_last_week, last_recount_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (restaurant_id, item["item"], item.get("category", ""), item.get("unit", ""),
                 item["par_level"], item["unit_cost"], item["case_size"],
                 item["current_stock"], item["avg_daily_usage"], item["last_order_qty"],
                 item["waste_last_week"], today_str)
            )
            ingredient_id = cur.lastrowid
            conn.execute(
                "INSERT INTO ingredient_stock_events "
                "(restaurant_id, ingredient_id, event_type, qty, event_date, source, note) "
                "VALUES (?,?,?,?,?,?,?)",
                (restaurant_id, ingredient_id, "recount", item["current_stock"], today_str,
                 "migration", "initial anchor from CSV import")
            )
            imported += 1
        conn.commit()

    return {"imported": imported, "skipped_existing": skipped}


# ── Admin CRUD helpers (thin routes in admin_routes.py delegate here so DB
#    access always goes through the lazily-imported get_conn/db_conn above) ──

def list_ingredients(restaurant_id: int) -> list:
    from models import get_conn
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM ingredients WHERE restaurant_id=? AND is_active=1 ORDER BY name",
        (restaurant_id,)
    ).fetchall()
    try:
        ww = waste_in_window(restaurant_id, conn=conn)
    except Exception:
        ww = None
    conn.close()
    out = [dict(r) for r in rows]
    # The same week's waste every other surface reads: summed from the dated
    # events, not the cached rollup (DH1-1).
    if ww:
        for d in out:
            if d.get("id") in ww["logged"]:
                d["waste_last_week"] = ww["by_ingredient"].get(d["id"], 0.0)
    return out


def create_ingredient(restaurant_id: int, name: str, category: str = "", unit: str = "",
                       par_level: float = 0, unit_cost: float = 0, case_size: float = 1.0,
                       current_stock: float = 0) -> int:
    """Creates the ingredient and its initial recount anchor in one call —
    every ingredient must have at least one recount (see module docstring)."""
    from models import db_conn
    today_str = local_today(restaurant_id).isoformat()
    with db_conn() as conn:
        cur = conn.execute(
            "INSERT INTO ingredients (restaurant_id, name, category, unit, par_level, "
            "unit_cost, case_size, current_stock, last_recount_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (restaurant_id, name, category, unit, par_level, unit_cost, case_size or 1.0,
             current_stock, today_str)
        )
        ingredient_id = cur.lastrowid
        conn.execute(
            "INSERT INTO ingredient_stock_events "
            "(restaurant_id, ingredient_id, event_type, qty, event_date, source, note) "
            "VALUES (?,?,?,?,?,?,?)",
            (restaurant_id, ingredient_id, "recount", current_stock, today_str,
             "admin", "initial count at creation")
        )
        conn.commit()
    return ingredient_id


def ingredient_belongs_to(conn, restaurant_id: int, ingredient_id: int) -> bool:
    row = conn.execute("SELECT restaurant_id FROM ingredients WHERE id=?", (ingredient_id,)).fetchone()
    return bool(row) and int(row["restaurant_id"]) == int(restaurant_id)


def menu_item_belongs_to(conn, restaurant_id: int, menu_item_id: int) -> bool:
    row = conn.execute("SELECT restaurant_id FROM menu_items WHERE id=?", (menu_item_id,)).fetchone()
    return bool(row) and int(row["restaurant_id"]) == int(restaurant_id)


def update_ingredient(restaurant_id: int, ingredient_id: int, **fields) -> bool:
    """Updates static attributes only (name/category/unit/par_level/unit_cost/
    case_size) plus avg_daily_usage/waste_last_week as a manual override —
    current_stock is deliberately not editable here, it can only change via
    record_recount/record_receiving so the ledger stays the source of truth.

    restaurant_id is required and enforced. The admin route has always had it
    in the URL and used to pass only the ingredient_id, so a stale row id
    from a second tab wrote to another location's ingredient while the URL,
    the screen and the audit trail all named the first. Returns False when
    the ingredient isn't this restaurant's.
    """
    from models import db_conn
    allowed = {"name", "category", "unit", "par_level", "unit_cost", "case_size",
               "avg_daily_usage", "waste_last_week"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    # A NaN passes every `<`/`>` guard, is stored as NULL, and then broke
    # the restaurant's whole Food Cost on every read (MOD-FC-6). A figure
    # that is not a finite, non-negative number writes nothing.
    import math
    for k in ("par_level", "unit_cost", "case_size", "avg_daily_usage", "waste_last_week"):
        if k in updates and updates[k] is not None:
            try:
                v = float(updates[k])
            except (TypeError, ValueError):
                return False
            if not math.isfinite(v) or v < 0:
                return False
            updates[k] = v
    sets = ", ".join(f"{k}=?" for k in updates) + ", updated_at=datetime('now')"
    with db_conn() as conn:
        cur = conn.execute(f"UPDATE ingredients SET {sets} WHERE id=? AND restaurant_id=?",
                           [*updates.values(), ingredient_id, restaurant_id])
        conn.commit()
        return cur.rowcount > 0


def deactivate_ingredient(restaurant_id: int, ingredient_id: int) -> bool:
    """Soft-delete only — recipe_ingredients/ingredient_stock_events may
    still reference this ingredient, never hard-delete it. Scoped: see
    update_ingredient."""
    from models import db_conn
    with db_conn() as conn:
        cur = conn.execute(
            "UPDATE ingredients SET is_active=0, updated_at=datetime('now') "
            "WHERE id=? AND restaurant_id=?", (ingredient_id, restaurant_id))
        conn.commit()
        return cur.rowcount > 0


def create_menu_item(restaurant_id: int, name: str) -> int:
    """Manually add a menu item with no toast_guid — the only way to map a
    recipe for a restaurant that isn't Toast-connected (or for a dish that
    exists but hasn't sold enough yet to show up in discover_menu_items).
    toast_guid stays NULL; SQLite's UNIQUE(restaurant_id, toast_guid)
    treats multiple NULLs as distinct, so this never collides with itself
    or with Toast-discovered rows."""
    from models import db_conn
    with db_conn() as conn:
        cur = conn.execute(
            "INSERT INTO menu_items (restaurant_id, toast_guid, name) VALUES (?,NULL,?)",
            (restaurant_id, name)
        )
        conn.commit()
        return cur.lastrowid


def list_menu_items_with_recipes(restaurant_id: int) -> list:
    from models import get_conn
    conn = get_conn()
    try:
        menu_items = conn.execute(
            "SELECT * FROM menu_items WHERE restaurant_id=? AND is_active=1 ORDER BY name",
            (restaurant_id,)
        ).fetchall()
        # Two queries, not one per dish. This ran a recipe lookup for every
        # menu item — 201 queries for a 200-item menu — while
        # menu_profitability next door already showed the grouped shape.
        #
        # The ingredient is filtered by tenant too: a legacy recipe row
        # written before add_recipe_ingredient validated the pair can point at
        # another restaurant's ingredient, and this renders its name and unit.
        by_item = {}
        for r in conn.execute(
            "SELECT ri.id, ri.menu_item_id, ri.ingredient_id, ri.qty_per_unit, "
            "       i.name AS ingredient_name, i.unit "
            "FROM recipe_ingredients ri "
            "JOIN menu_items m ON m.id = ri.menu_item_id AND m.restaurant_id=? "
            "JOIN ingredients i ON i.id = ri.ingredient_id AND i.restaurant_id=? "
            "ORDER BY ri.id",
            (restaurant_id, restaurant_id)
        ).fetchall():
            by_item.setdefault(r["menu_item_id"], []).append(dict(r))
        return [{**dict(mi), "recipe": by_item.get(mi["id"], [])} for mi in menu_items]
    finally:
        # Was outside any try, so a raised exception leaked the connection.
        conn.close()


def priority_ingredients(restaurant_id: int) -> list:
    """The 8 highest weekly-spend-impact ingredients (unit_cost x
    avg_daily_usage — see inventory.compute_item_trends' identical "Big 8"
    logic, reused here rather than duplicated), each flagged with whether
    it already has a recipe mapped. This is the realistic scope for recipe
    setup — the handful of ingredients that actually move the food-cost
    number, not every ingredient on the menu. Requires no sales/waste
    history to compute, only the current par/cost/usage snapshot, so it
    works the moment a restaurant has any ingredients at all."""
    from inventory import load_inventory_for_restaurant, compute_item_trends
    from models import get_conn

    items, is_live = load_inventory_for_restaurant(restaurant_id)
    if not items or not is_live:
        return []  # don't surface "priorities" computed from generic sample data
    big_8 = compute_item_trends(restaurant_id, items).get("big_8", [])

    conn = get_conn()
    mapped_names = {
        row["name"] for row in conn.execute(
            "SELECT DISTINCT i.name FROM recipe_ingredients ri "
            "JOIN ingredients i ON i.id = ri.ingredient_id WHERE i.restaurant_id=?",
            (restaurant_id,)
        ).fetchall()
    }
    conn.close()

    return [{
        "name": i["item"],
        "weekly_impact": round((i.get("unit_cost") or 0) * (i.get("avg_daily_usage") or 0) * 7, 2),
        "has_recipe": i["item"] in mapped_names,
    } for i in big_8]


def add_recipe_ingredient(restaurant_id: int, menu_item_id: int, ingredient_id: int,
                          qty_per_unit: float, source: str = None) -> int:
    """Bind an ingredient to a dish. Both sides must belong to
    `restaurant_id`.

    recipe_ingredients is a join table with no tenant column of its own, and
    nothing used to check the pair. A recipe could therefore link Chicago's
    Margherita to Dallas's tomatoes — and menu_profitability would then cost
    Chicago's plate from Dallas's unit_cost and show the wrong food-cost
    percentage to the client and to Ask Cavnar. Returns 0 if either side
    isn't this restaurant's.
    """
    import math
    from models import db_conn
    try:
        qty_per_unit = float(qty_per_unit)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(qty_per_unit) or qty_per_unit <= 0:
        return 0
    with db_conn() as conn:
        if not menu_item_belongs_to(conn, restaurant_id, menu_item_id):
            return 0
        if not ingredient_belongs_to(conn, restaurant_id, ingredient_id):
            return 0
        # (menu_item_id, ingredient_id) is unique, so binding the same
        # ingredient to a dish twice — which used to double-count it in the
        # plate cost — is refused the same way an unowned pair is, rather
        # than surfacing as a 500.
        import sqlite3 as _sqlite3
        # `source` is the line's provenance (audit #35): 'owner' when a
        # person typed or imported it, 'draft_accepted' / 'draft_edited' when
        # it came from a Cavnar recipe draft. A plate cost resting on an
        # unedited draft is not a confirmed plate cost.
        try:
            cur = conn.execute(
                "INSERT INTO recipe_ingredients (menu_item_id, ingredient_id, qty_per_unit, source) "
                "VALUES (?,?,?,?)",
                (menu_item_id, ingredient_id, qty_per_unit, source or "owner")
            )
        except _sqlite3.IntegrityError:
            return 0
        conn.commit()
        return cur.lastrowid


def set_recipe_ingredient_qty(restaurant_id: int, menu_item_id: int, ingredient_id: int,
                              qty_per_unit: float) -> bool:
    """Correct the quantity on a (dish, ingredient) pair already bound.
    True when a row changed. Same tenant checks as add_recipe_ingredient —
    the dish scopes the row."""
    import math
    from models import db_conn
    try:
        qty_per_unit = float(qty_per_unit)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(qty_per_unit) or qty_per_unit <= 0:
        return False
    with db_conn() as conn:
        if not menu_item_belongs_to(conn, restaurant_id, menu_item_id):
            return False
        if not ingredient_belongs_to(conn, restaurant_id, ingredient_id):
            return False
        # A person correcting the quantity has reviewed the line.
        cur = conn.execute(
            "UPDATE recipe_ingredients SET qty_per_unit=?, source='owner' WHERE menu_item_id=? AND ingredient_id=?",
            (qty_per_unit, menu_item_id, ingredient_id))
        conn.commit()
        return cur.rowcount > 0


def delete_recipe_ingredient(restaurant_id: int, recipe_ingredient_id: int) -> bool:
    """Scoped through the row's own menu item — the route's menu_item_id used
    to be accepted and then ignored entirely."""
    from models import db_conn
    with db_conn() as conn:
        cur = conn.execute(
            "DELETE FROM recipe_ingredients WHERE id=? AND menu_item_id IN "
            "(SELECT id FROM menu_items WHERE restaurant_id=?)",
            (recipe_ingredient_id, restaurant_id))
        conn.commit()
        return cur.rowcount > 0


# ── Menu profitability ─────────────────────────────────────────────────────────

def menu_profitability(restaurant_id: int) -> dict:
    """Plate cost and margin per menu item, from the recipes already mapped.

    Recipes have costed ingredients all along (qty_per_unit x unit_cost);
    what was missing was the other half of the equation — what the dish
    sells for. With menu_items.sell_price set, each item gets a real food
    cost percentage, which is the number that actually tells an owner which
    dishes are worth pushing.

    Items are returned in three groups so the UI never has to imply a
    margin it can't compute:
      priced   — has a recipe AND a price: real cost, margin, food cost %
      unpriced — has a recipe but no price yet: cost only
      unmapped — no recipe: nothing costable

    `food_cost_pct` is cost/price. The industry rule of thumb is ~28-35%;
    lower is a better margin.
    """
    from models import get_conn
    conn = get_conn()
    try:
        # Bounded. A restaurant that ran discover_menu_items against a large
        # Toast catalogue can carry several thousand active items; this
        # selected all of them, joined every recipe row and every sale in the
        # window, and serialised the lot to a phone. MENU_PAGE_SIZE is a
        # ceiling on the response, not on the maths — the weighted average
        # below is computed from what is returned, so the cap is set well
        # above any real menu and `truncated` says when it bit.
        items = conn.execute(
            "SELECT id, name, sell_price FROM menu_items WHERE restaurant_id=? AND is_active=1 "
            "ORDER BY name LIMIT ?",
            (restaurant_id, MENU_PAGE_SIZE + 1)
        ).fetchall()
        truncated = len(items) > MENU_PAGE_SIZE
        items = items[:MENU_PAGE_SIZE]
        costs = {}
        for row in conn.execute("""
            SELECT ri.menu_item_id AS mid,
                   SUM(ri.qty_per_unit * COALESCE(i.unit_cost, 0)) AS plate_cost,
                   COUNT(*) AS n,
                   -- An ingredient with no unit_cost contributes nothing to
                   -- the sum above, so a dish whose main protein has never
                   -- been priced showed an excellent margin. Counting them
                   -- lets the caller refuse to report a figure it can't
                   -- actually compute.
                   SUM(CASE WHEN i.unit_cost IS NULL OR i.unit_cost <= 0 THEN 1 ELSE 0 END) AS uncosted
            FROM recipe_ingredients ri
            JOIN menu_items  m ON m.id = ri.menu_item_id
            -- The ingredient is filtered too, not just the dish. A recipe row
            -- written before add_recipe_ingredient validated the pair can
            -- point at another restaurant's ingredient; costing a plate from
            -- that row's unit_cost is how one location's food-cost % ends up
            -- computed from another's prices.
            JOIN ingredients i ON i.id = ri.ingredient_id AND i.restaurant_id = m.restaurant_id
            WHERE m.restaurant_id=?
            GROUP BY ri.menu_item_id
        """, (restaurant_id,)).fetchall():
            costs[row["mid"]] = {"cost": float(row["plate_cost"] or 0),
                                 "ingredients": int(row["n"] or 0),
                                 "uncosted": int(row["uncosted"] or 0)}
        window_start = (local_today(restaurant_id) - timedelta(days=_POPULARITY_WINDOW_DAYS - 1)).isoformat()
        sold = {r["menu_item_id"]: float(r["qty"] or 0) for r in conn.execute(
            "SELECT menu_item_id, SUM(qty_sold) AS qty FROM menu_item_sales "
            "WHERE restaurant_id=? AND business_date>=? GROUP BY menu_item_id",
            (restaurant_id, window_start)
        ).fetchall()}
    finally:
        conn.close()

    priced, unpriced, unmapped, uncosted_items = [], [], [], []
    for it in items:
        entry = {"id": it["id"], "name": it["name"],
                 "sell_price": round(float(it["sell_price"]), 2) if it["sell_price"] else None,
                 "units_sold": round(sold.get(it["id"], 0.0), 2) if sold else None}
        costed = costs.get(it["id"])
        if not costed or costed["ingredients"] == 0:
            unmapped.append(entry)
            continue
        entry["ingredient_count"] = costed["ingredients"]
        if costed["uncosted"]:
            # Missing measurement, not a zero. Reporting this plate's cost
            # would understate it by exactly the ingredients nobody priced.
            entry["uncosted_ingredients"] = costed["uncosted"]
            uncosted_items.append(entry)
            continue
        entry["plate_cost"] = round(costed["cost"], 2)
        price = entry["sell_price"]
        if not price or price <= 0:
            unpriced.append(entry)
            continue
        entry["margin"] = round(price - entry["plate_cost"], 2)
        entry["food_cost_pct"] = round(entry["plate_cost"] / price * 100, 1)
        entry["margin_pct"] = round(entry["margin"] / price * 100, 1)
        # ingredients.unit is a free-text display label and qty_per_unit is
        # typed by hand, so nothing stops a cost-per-CASE being multiplied by
        # a quantity in OUNCES. There is no conversion layer to catch it, and
        # the result is wrong by orders of magnitude while looking like an
        # ordinary number. A plate cost this far outside any real food cost
        # is far more likely a unit mismatch than a genuine margin, and
        # saying so is more useful than a silently wrong percentage.
        band = _unit_sanity(entry["food_cost_pct"])
        if band:
            entry["unit_warning"] = band
        if entry["units_sold"]:
            entry["total_contribution"] = round(entry["margin"] * entry["units_sold"], 2)
            entry["total_revenue"] = round(price * entry["units_sold"], 2)
        priced.append(entry)

    have_sales = any(e.get("units_sold") for e in priced)

    # Worst food cost first — the dish quietly eating the margin is the one
    # worth looking at, not the best performer.
    priced.sort(key=lambda e: e["food_cost_pct"], reverse=True)

    # The menu's food cost % is total cost over total revenue, not the mean
    # of each dish's percentage: an unweighted mean gives a $3 side the same
    # weight as the entree that is most of the night's revenue, and it was
    # being shown against the 28-35% rule of thumb, which is revenue-weighted.
    if have_sales:
        rev = sum(e.get("total_revenue") or 0 for e in priced)
        cost = sum((e["plate_cost"] * (e.get("units_sold") or 0)) for e in priced)
        avg_fc = round(cost / rev * 100, 1) if rev > 0 else None
        avg_basis = f"weighted by units sold over the last {_POPULARITY_WINDOW_DAYS} days"
    elif priced:
        avg_fc = round(sum(e["food_cost_pct"] for e in priced) / len(priced), 1)
        avg_basis = ("unweighted — no sales data yet, so every dish counts equally "
                     "regardless of how often it sells")
    else:
        avg_fc, avg_basis = None, None

    # "Best" means the dish contributing the most gross margin, not the one
    # with the lowest food cost percentage. Ranking by percentage promotes
    # cheap low-margin items over the dishes actually paying the rent.
    if have_sales:
        by_contribution = sorted(priced, key=lambda e: e.get("total_contribution") or 0, reverse=True)
    else:
        by_contribution = sorted(priced, key=lambda e: e.get("margin") or 0, reverse=True)

    return {
        "priced": priced,
        "unpriced": unpriced,
        "unmapped": unmapped,
        "uncosted": uncosted_items,
        "average_food_cost_pct": avg_fc,
        "average_basis": avg_basis,
        "has_sales_data": have_sales,
        "popularity_window_days": _POPULARITY_WINDOW_DAYS,
        # Highest and lowest gross-margin contributors.
        "best": by_contribution[0] if by_contribution else None,
        "worst": by_contribution[-1] if by_contribution else None,
        # Highest food cost % — still worth surfacing, just not as "worst dish".
        "highest_food_cost": priced[0] if priced else None,
        "menu_engineering": _menu_engineering(priced) if have_sales else None,
        "truncated": truncated,
        "page_size": MENU_PAGE_SIZE,
    }



_IMPLAUSIBLE_LOW_FC_PCT = 2.0
_IMPLAUSIBLE_HIGH_FC_PCT = 150.0


def _unit_sanity(food_cost_pct):
    """Whether a plate cost is so far outside any real food cost that the
    likeliest explanation is a unit mismatch rather than a margin."""
    if food_cost_pct is None:
        return None
    if food_cost_pct >= _IMPLAUSIBLE_HIGH_FC_PCT:
        return ("This plate costs more than it sells for by a wide margin — "
                "check that the recipe quantities use the same unit as the "
                "ingredient's cost.")
    if 0 < food_cost_pct <= _IMPLAUSIBLE_LOW_FC_PCT:
        return ("This plate costs almost nothing to make — check that the "
                "recipe quantities use the same unit as the ingredient's cost.")
    return None


def _menu_engineering(priced: list) -> dict:
    """The standard four-quadrant classification: each dish against the menu's
    median popularity and median contribution margin.

    stars       — sells well, earns well: protect and feature
    plowhorses  — sells well, earns little: reprice or re-cost
    puzzles     — earns well, sells little: promote or reposition
    dogs        — neither: candidates for removal
    """
    usable = [e for e in priced if e.get("units_sold") and e.get("margin") is not None]
    if len(usable) < 4:
        return None
    sold = sorted(e["units_sold"] for e in usable)
    margins = sorted(e["margin"] for e in usable)
    mid = len(usable) // 2
    med_sold = sold[mid] if len(usable) % 2 else (sold[mid - 1] + sold[mid]) / 2
    med_margin = margins[mid] if len(usable) % 2 else (margins[mid - 1] + margins[mid]) / 2
    buckets = {"stars": [], "plowhorses": [], "puzzles": [], "dogs": []}
    for e in usable:
        popular = e["units_sold"] >= med_sold
        profitable = e["margin"] >= med_margin
        key = ("stars" if popular and profitable else
               "plowhorses" if popular else
               "puzzles" if profitable else "dogs")
        buckets[key].append(e["name"])
    return {"median_units_sold": round(med_sold, 2),
            "median_margin": round(med_margin, 2), **buckets}


def set_menu_item_price(restaurant_id: int, menu_item_id: int, sell_price) -> bool:
    """Scoped by restaurant_id so one restaurant can never price another's
    menu. `sell_price` of None or 0 clears the price. Returns False if the
    item isn't theirs."""
    import math
    from models import get_conn
    try:
        price = None if sell_price in (None, "", 0) else round(float(sell_price), 2)
    except (TypeError, ValueError):
        return False
    # NaN passes every comparison guard (nan < 0 is False) and Infinity passes
    # too. Either one persists, then propagates through food_cost_pct and
    # margin_pct, and jsonify emits bare NaN — invalid JSON that breaks the
    # whole menu-margins payload for that restaurant.
    if price is not None and (not math.isfinite(price) or price < 0):
        return False
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE menu_items SET sell_price=? WHERE id=? AND restaurant_id=? AND is_active=1",
            (price, menu_item_id, restaurant_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()
