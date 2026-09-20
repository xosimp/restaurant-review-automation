"""value_delivered.py — what Cavnar AI has actually been worth to this
restaurant, and what it has not.

WHAT THIS USED TO BE, and why it was replaced (ROI audit, Sep 2026).

"Total Value Delivered" on the web Home banner and the mobile Home tab was
the sum of four numbers, and three of them could not survive being read
aloud to the owner paying for them:

  * labour "value" was `potential_savings_monthly` — the gap ABOVE the
    owner's target. That is money still being LOST, counted as money
    delivered. The arithmetic ran backwards: a restaurant that fixed its
    scheduling watched its Total Value Delivered FALL, and the worst-run
    restaurant on the platform showed the biggest number.
  * food cost "value" was `recoverable_monthly` — the same inversion, waste
    still being thrown away.
  * marketing "value" was `months_active * 1500`, triggered by a single
    generated post ever and accruing every month afterwards whether or not
    anything was posted again.

and the sum added lifetime cumulative figures (reviews, marketing) to
monthly run-rates (labour, food cost), which business_intelligence.py
refuses to do three files away: "the money lines are deliberately never
summed — a measured cost, a scheduling gap and an elasticity forecast are
not addends."

WHAT IT IS NOW. Three figures, each measured its own way, each labelled,
and never added together:

  delivered     what MEASURED improvements are worth per month. Sourced
                entirely from outcomes.py: a tracker with a baseline taken
                before the change, a re-measure after, and a move that
                cleared the metric's own noise band. Carries the causation
                caveat everywhere it is rendered.
  avoided       work the product did that the owner would otherwise have
                paid someone for. Cost avoidance, not measurement — every
                rate is a STATED assumption, carried in the payload so the
                UI can show it, and counted only for work that actually
                happened.
  opportunity   the old labour/food-cost figures, under their real name:
                money on the table, not money in hand.

An owner asking "what has this been worth" gets the first. An owner asking
"what is still available" gets the third. Nothing in this file claims the
second is the first.
"""
import models
from models import get_restaurant, get_review_stats, DB_PATH

# get_conn is looked up on the models module at call time (models.get_conn(...)),
# never imported by name here. Every test in this codebase redirects the
# database by monkeypatching models.get_conn — `from models import get_conn`
# would bind a private, unpatchable copy of the original function into this
# module's own namespace, exactly the bug already documented and fixed once
# before in guest_marketing.py. That bug existed here for real: every test
# exercising the Home brief's Total Value Delivered figure was silently
# reading and writing the developer's own real local reviews.db instead of
# the test's isolated fixture database (masked on any machine that happens
# to have one sitting around), and hard-crashed with "no such table:
# value_snapshots" on a clean checkout with no such file — exactly what
# GitHub Actions' CI runner hit on every push (confirmed Sep 7 2026).

# ── Stated assumptions ──────────────────────────────────────────────────────
# Every rate below is an ASSUMPTION, not a measurement. Each travels with the
# figure it produces (see `avoided()`) so the owner reads the rate next to
# the number rather than being asked to trust it. Change one here and every
# surface changes with it — there is no second copy.
REPLY_RATE = 5.00        # per review reply
REPLY_RATE_BASIS = "what a managed review-response service charges per reply"
REPLY_MINUTES = 6        # to read a review and write a considered reply
AGENCY_MONTHLY = 1500.0  # per month content was actually produced
AGENCY_BASIS = "a part-time social media manager, for months content was actually produced"
SCHEDULE_MINUTES = 90    # to build a week's schedule by hand
INVOICE_MINUTES = 12     # to key one supplier invoice in by hand


def _scalar(conn, sql, args):
    row = conn.execute(sql, args).fetchone()
    return (row[0] if row else 0) or 0


# ── 1. Delivered: measured, realised, caveated ──────────────────────────────

def delivered(restaurant_id: int, db_path: str = DB_PATH) -> dict:
    """What measured improvements are worth per month, and the honest
    denominator beside it.

    Never a bare number. `wins` against `evaluated` is the difference
    between "two things worked" and "two of eleven things worked", and an
    owner shown only the first stops trusting the second time.
    """
    import outcomes
    v = outcomes.total_value(restaurant_id, db_path=db_path)
    best = outcomes.best_ever(restaurant_id, db_path=db_path)
    return {
        "monthly": v["monthly"],
        "annual": v["annual"],
        "wins": v["wins"],
        "evaluated": v["evaluated"],
        "in_flight": v["in_flight"],
        "unmeasurable": v["unmeasurable"],
        "no_clear_change": v["no_clear_change"],
        "by_module": v["by_module"],
        "biggest": ({"title": best["title"],
                     "monthly": round(abs(float(best["dollars_monthly"])), 2),
                     "metric": best.get("metric_label") or best["metric"],
                     "summary": outcomes.summarise(best)} if best else None),
        "caveat": v["caveat"],
        "basis": "measured before and after each change, over the metric's own window",
    }


# ── 2. Avoided: cost avoidance, every rate stated ───────────────────────────

def avoided(restaurant_id: int, db_path: str = DB_PATH) -> dict:
    """Work the product did that someone would otherwise have been paid for,
    and the hours behind it.

    Cost avoidance is a weaker claim than measurement and is kept apart from
    it for that reason. Two rules hold every line here honest:

      COUNT ONLY WORK THAT HAPPENED. Replies actually posted, months content
      was actually produced, schedules actually built, invoices actually
      read. The old marketing figure accrued $1,500 every month forever off
      one generated post; this counts the months.

      CARRY THE RATE. Every item ships `rate` and `basis` so the number is
      shown with its assumption attached, and an owner who disagrees with
      the rate can see exactly what to discount.
    """
    restaurant = get_restaurant(restaurant_id, db_path=db_path)
    if not restaurant:
        return {"items": [], "dollars": 0.0, "hours": 0.0}

    items = []
    conn = models.get_conn(db_path)
    try:
        if restaurant.module_reviews:
            replies = int((get_review_stats(restaurant_id) or {}).get("responded", 0) or 0)
            if replies:
                items.append({
                    "key": "replies", "label": f"{replies:,} review replies written",
                    "dollars": round(replies * REPLY_RATE, 2),
                    "hours": round(replies * REPLY_MINUTES / 60.0, 1),
                    "rate": f"${REPLY_RATE:,.2f} each", "basis": REPLY_RATE_BASIS})

        if restaurant.module_marketing:
            # Months in which content was ACTUALLY produced — not months
            # since signup. One post in month one no longer bills the owner's
            # goodwill for every month after it.
            months = _scalar(conn,
                             "SELECT COUNT(DISTINCT substr(created_at,1,7)) "
                             "FROM marketing_content_log WHERE restaurant_id=?",
                             (restaurant_id,))
            posts = _scalar(conn, "SELECT COUNT(*) FROM marketing_content_log "
                                  "WHERE restaurant_id=?", (restaurant_id,))
            if months:
                items.append({
                    "key": "content",
                    "label": f"{posts:,} posts written across {months} "
                             f"{'month' if months == 1 else 'months'}",
                    "dollars": round(months * AGENCY_MONTHLY, 2), "hours": None,
                    "rate": f"${AGENCY_MONTHLY:,.0f}/month", "basis": AGENCY_BASIS})

        if restaurant.module_labor:
            schedules = _scalar(conn, "SELECT COUNT(*) FROM schedule_history "
                                      "WHERE restaurant_id=?", (restaurant_id,))
            if schedules:
                items.append({
                    "key": "schedules", "label": f"{schedules:,} schedules built",
                    "dollars": None,
                    "hours": round(schedules * SCHEDULE_MINUTES / 60.0, 1),
                    "rate": f"{SCHEDULE_MINUTES} min each",
                    "basis": "building a week's schedule by hand"})

        if restaurant.module_inventory:
            invoices = _scalar(conn, "SELECT COUNT(*) FROM invoice_imports "
                                     "WHERE restaurant_id=? AND applied_at IS NOT NULL",
                               (restaurant_id,))
            if invoices:
                items.append({
                    "key": "invoices", "label": f"{invoices:,} invoices read and applied",
                    "dollars": None,
                    "hours": round(invoices * INVOICE_MINUTES / 60.0, 1),
                    "rate": f"{INVOICE_MINUTES} min each",
                    "basis": "keying one supplier invoice in by hand"})
    except Exception:
        # A missing table on an old database must not take the Home page
        # down over a secondary figure.
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return {
        "items": items,
        "dollars": round(sum(i["dollars"] or 0 for i in items), 2),
        "hours": round(sum(i["hours"] or 0 for i in items), 1),
        "basis": "work the product did, at stated rates — an estimate, not a measurement",
    }


# ── 3. Opportunity: money on the table, under its real name ─────────────────

def opportunity(restaurant_id: int, db_path: str = DB_PATH) -> dict:
    """The labour and food-cost figures that used to be called "delivered".

    Unchanged arithmetic, honest label. Both are gaps against a target —
    what the restaurant could still recover, which is the opposite of what
    it has already banked.
    """
    restaurant = get_restaurant(restaurant_id, db_path=db_path)
    if not restaurant:
        return {"items": [], "monthly": 0.0}
    items = []
    if restaurant.module_labor:
        try:
            from labor import analyse_shifts_for_restaurant
            labor = analyse_shifts_for_restaurant(restaurant_id)
            # Sample shifts are not the restaurant's own numbers.
            if labor.get("is_live"):
                v = float(labor.get("potential_savings_monthly", 0) or 0)
                if v > 0:
                    items.append({"key": "labor", "label": "Scheduling against your target",
                                  "monthly": round(v, 2), "module": "labor"})
        except Exception:
            pass
    if restaurant.module_inventory:
        try:
            from inventory import analysis_for
            _items, live, inv = analysis_for(restaurant_id)
            if live:
                v = float(inv.get("recoverable_monthly", 0) or 0)
                if v > 0:
                    items.append({"key": "inventory", "label": "Waste above tolerance",
                                  "monthly": round(v, 2), "module": "inventory"})
        except Exception:
            pass
    return {"items": items,
            "monthly": round(sum(i["monthly"] for i in items), 2),
            "basis": "gaps against your own targets — available, not captured"}


# ── The whole picture ───────────────────────────────────────────────────────

def breakdown(restaurant_id: int, db_path: str = DB_PATH) -> dict:
    """All three figures, never summed. Every Home surface reads this."""
    return {
        "delivered": delivered(restaurant_id, db_path=db_path),
        "avoided": avoided(restaurant_id, db_path=db_path),
        "opportunity": opportunity(restaurant_id, db_path=db_path),
    }


def compute_total_value_delivered(restaurant_id: int, db_path: str = DB_PATH) -> int:
    """The headline figure: measured monthly dollars, and nothing else.

    Kept under its original name because the web banner, the mobile Home
    payload and the value snapshots all call it. What changed is what it
    MEANS — it is now only what was measured, so it is a number that can be
    defended line by line, and for most restaurants it starts at zero and
    grows as trackers close. That is the true state, and the old figure's
    only advantage was that it was never true.
    """
    try:
        return int(round(delivered(restaurant_id, db_path=db_path)["monthly"]))
    except Exception:
        return 0


def record_value_snapshot(restaurant_id: int, total_value: int, db_path: str = DB_PATH):
    """Upserts today's total — called opportunistically from the Home
    endpoints, so the first Home load of each day records that day's figure.
    No separate scheduled job: a restaurant whose owner never opens the app
    that day simply doesn't get a data point, which is fine for a "how's
    this trending" sparkline."""
    conn = models.get_conn(db_path)
    conn.execute("""
        INSERT INTO value_snapshots (restaurant_id, snapshot_date, total_value)
        VALUES (?, date('now'), ?)
        ON CONFLICT(restaurant_id, snapshot_date) DO UPDATE SET total_value = excluded.total_value
    """, (restaurant_id, total_value))
    conn.commit()
    conn.close()


def get_value_history(restaurant_id: int, days: int = 30, db_path: str = DB_PATH) -> list[dict]:
    """Ascending by date — oldest first, matching how a sparkline is drawn
    left to right."""
    conn = models.get_conn(db_path)
    rows = conn.execute(f"""
        SELECT snapshot_date, total_value FROM value_snapshots
        WHERE restaurant_id=? AND snapshot_date >= date('now', '-{int(days)} days')
        ORDER BY snapshot_date ASC
    """, (restaurant_id,)).fetchall()
    conn.close()
    return [{"date": r["snapshot_date"], "value": r["total_value"]} for r in rows]
