"""value_delivered.py — what Cavnar AI has actually been worth to this
restaurant, and what it has not.

WHAT THIS USED TO BE, and why it was replaced (ROI audit, Sep 2026).

"Total Value Delivered" on the web Home banner and the mobile Home tab was
the sum of four numbers, and three of them could not survive being read
aloud to the owner paying for them:

  * labor "value" was `potential_savings_monthly` — the gap ABOVE the
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
monthly run-rates (labor, food cost), which business_intelligence.py
refuses to do three files away: "the money lines are deliberately never
summed — a measured cost, a scheduling gap and an elasticity forecast are
not addends."

WHAT IT IS NOW. Four figures, each measured its own way, each labelled,
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
  opportunity   the old labor/food-cost figures, under their real name:
                money on the table, not money in hand.
  surfaced      what the alerts raised carried in dollars. Putting a problem
                in front of someone is not the same as their having fixed
                it, so it is its own figure too.

An owner asking "what has this been worth" gets the first. An owner asking
"what is still available" gets the third. Nothing in this file claims any
of them is any other.
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
AGENCY_MONTHLY = 1500.0  # a full month of a part-time social media manager's output
# The rate counts PIECES of work (NS3 M1): $1,500 a month buys about this
# many posts, so each real piece is credited AGENCY_PER_PIECE, capped at a
# full month's fee in any one month. It paid $1,500 for any month with ONE
# post — three captions, one per month, read "$4,500 you'd otherwise have
# paid for".
AGENCY_PIECES_PER_MONTH = 12
AGENCY_PER_PIECE = round(AGENCY_MONTHLY / AGENCY_PIECES_PER_MONTH, 2)
AGENCY_BASIS = (f"a part-time social media manager at ${AGENCY_MONTHLY:,.0f} a month for about "
                f"{AGENCY_PIECES_PER_MONTH} posts — ${AGENCY_MONTHLY / AGENCY_PIECES_PER_MONTH:,.0f} a piece, "
                f"counted only for pieces actually produced, never more than a month's fee in one month")
SCHEDULE_MINUTES = 90    # to build a week's schedule by hand
INVOICE_MINUTES = 12     # to key one supplier invoice in by hand


def rates() -> dict:
    """Every stated rate behind every value figure, in one place, so a
    surface shows the server's number instead of keeping its own copy (the
    web's review-savings bar multiplied replies by a hard-coded 5 — rec-ROI
    audit #12). Assumptions (the avoided figure's rates) and conversions
    (the calendar, the overtime premium, the re-check rules the delivered
    figure is read under) are both here; nothing in it is a measurement."""
    import labor
    import metrics
    import outcomes
    return {
        "reply_rate": REPLY_RATE, "reply_rate_basis": REPLY_RATE_BASIS, "reply_minutes": REPLY_MINUTES,
        "agency_monthly": AGENCY_MONTHLY, "agency_basis": AGENCY_BASIS,
        "agency_per_piece": AGENCY_PER_PIECE, "agency_pieces_per_month": AGENCY_PIECES_PER_MONTH,
        "schedule_minutes": SCHEDULE_MINUTES, "invoice_minutes": INVOICE_MINUTES,
        "overtime_multiplier": labor.OVERTIME_MULTIPLIER,
        "overtime_threshold_hours": labor.OVERTIME_THRESHOLD_HOURS,
        "overtime_basis": metrics.OVERTIME_BASIS,
        "days_per_month": round(metrics.DAYS_PER_MONTH, 4),
        "weeks_per_month": round(metrics.WEEKS_PER_MONTH, 4),
        "recheck_days": outcomes.RECHECK_DAYS,
        "accrual_horizon_days": outcomes.ACCRUAL_HORIZON_DAYS,
        "consistent_multiple": outcomes.CONSISTENT_MULTIPLE,
    }


def _scalar(conn, sql, args):
    row = conn.execute(sql, args).fetchone()
    return (row[0] if row else 0) or 0


# ── 1. Delivered: measured, realised, caveated ──────────────────────────────

def _scope_args(scope, denied_modules):
    """outcomes' viewer filters from a viewer_scope() (or just the denied
    modules a caller passed)."""
    if scope is None:
        return {"denied_modules": set(denied_modules or ())}
    return {"denied_modules": set(scope.get("denied_modules") or ()),
            "exclude_metrics": tuple(scope.get("exclude_metrics") or ()),
            "exclude_ids": set(scope.get("exclude_ids") or ())}


def viewer_scope(restaurant_id, user, db_path: str = DB_PATH) -> dict:
    """What one login may see of the measured value, applied BEFORE anything
    is summed (re-audit A26) — the same line owner_report draws:

      denied_modules   modules it may not open (viewer_denied)
      exclude_metrics  comps and voids without LOSS_VIEW (a comp result can
                       name the manager approving them)
      exclude_ids      trackers whose recommendation it may not see
                       (rec_learning.viewer_sees: owner-only, a loss, a
                       module it lacks)

    A manager's /value used to hand back the owner-only and comp results as
    the biggest win and inside every total. None or an admin: nothing."""
    if user is None or user.get("is_admin"):
        return {"denied_modules": set(), "exclude_metrics": (), "exclude_ids": set()}
    import outcomes
    return {"denied_modules": viewer_denied(user),
            "exclude_metrics": tuple(m for m in outcomes.LOSS_METRICS if not outcomes.metric_visible_to(user, m)),
            "exclude_ids": outcomes.hidden_tracker_ids(restaurant_id, user, db_path=db_path)}


def _restaurant_wide(scope) -> bool:
    return not (scope.get("denied_modules") or scope.get("exclude_metrics") or scope.get("exclude_ids"))


def delivered(restaurant_id: int, db_path: str = DB_PATH, denied_modules=None, scope=None) -> dict:
    """What measured improvements are worth per month, and the honest
    denominator beside it.

    Never a bare number. `wins` against `evaluated` is the difference
    between "two things worked" and "two of eleven things worked", and an
    owner shown only the first stops trusting the second time.

    `denied_modules` — or a whole viewer_scope() — is applied inside
    outcomes.total_value, BEFORE the sum. Filtering the breakdown and
    leaving the total alone would hand a manager without FOOD_COST_VIEW the
    margin dollars back by subtraction.
    """
    import outcomes
    f = _scope_args(scope, denied_modules)
    v = outcomes.total_value(restaurant_id, db_path=db_path, **f)
    best = outcomes.best_ever(restaurant_id, db_path=db_path, **f)
    return {
        # `monthly` stays the improvements alone (what every surface already
        # renders); the changes that got worse sit BESIDE it and `net_monthly`
        # is the one less the other (rec-ROI #1). Never summed into avoided,
        # surfaced or opportunity.
        "monthly": v["monthly"],
        "annual": v["annual"],
        "annual_basis": v.get("annual_basis"),
        "wins": v["wins"],
        "wins_measured": v.get("wins_measured", v["wins"]),
        "wins_by_module": v.get("wins_by_module", {}),
        "unpriced_wins": v.get("unpriced_wins", []),
        "worsened": v.get("worsened", {"count": 0, "monthly": 0.0, "priced_count": 0}),
        "net_monthly": v.get("net_monthly", v["monthly"]),
        "net_note": v.get("net_note"),
        "net_by_module": v.get("net_by_module", v["by_module"]),
        "validated_monthly": v.get("validated_monthly", 0.0),
        "validated": v.get("validated", 0),
        # The improvements by attribution grade, side by side (CA2 #7): a
        # clear or held move apart from one tied to other changes (or past
        # the band only once). Parts of `monthly`, never added to it again.
        "consistent_monthly": v.get("consistent_monthly", 0.0),
        "consistent": v.get("consistent", 0),
        "associated_monthly": v.get("associated_monthly", 0.0),
        "associated": v.get("associated", 0),
        "faded": v.get("faded", 0),
        "evaluated": v["evaluated"],
        "in_flight": v["in_flight"],
        "unmeasurable": v["unmeasurable"],
        "no_clear_change": v["no_clear_change"],
        "by_module": v["by_module"],
        # Gross revenue measured before and after — never added to the
        # savings above (re-audit A6); `sales_pricing` says so in the payload.
        "sales_lift": v.get("sales_lift"),
        "sales_pricing": v.get("sales_pricing", "separate"),
        "cumulative": outcomes.cumulative(restaurant_id, db_path=db_path, **f),
        "rates": rates(),
        "biggest": ({"title": best["title"],
                     "monthly": round(abs(float(best["dollars_monthly"])), 2),
                     "metric": best.get("metric_label") or best["metric"],
                     "module": best.get("module"),
                     "attribution": best.get("attribution"),
                     "summary": outcomes.summarise(best)} if best else None),
        "caveat": v["caveat"],
        "basis": "measured before and after each change, over the metric's own window",
    }


# ── 2. Avoided: cost avoidance, every rate stated ───────────────────────────

def avoided(restaurant_id: int, db_path: str = DB_PATH, denied_modules=None) -> dict:
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

    denied = set(denied_modules or ())
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
            #
            # And only REAL pieces (marketing.REAL_PIECE_SQL): published, or
            # made by a person — not the quiet-night job's own draft, not a
            # calendar marker, and a Regenerate is not a second piece (M-15).
            # A month nobody opened Marketing but a scheduled job drafted a
            # post was credited $1,500 of work done.
            from marketing import REAL_PIECE_SQL, PIECE_ID_SQL
            per_month = conn.execute(
                f"SELECT substr(created_at,1,7) AS m, COUNT(DISTINCT {PIECE_ID_SQL}) AS n "
                f"FROM marketing_content_log WHERE restaurant_id=? AND {REAL_PIECE_SQL} "
                f"GROUP BY substr(created_at,1,7)", (restaurant_id,)).fetchall()
            per_month = [(r[0], int(r[1] or 0)) for r in per_month if r and r[1]]
            months = len(per_month)
            posts = sum(n for _, n in per_month)
            if months:
                # Each piece at the per-piece rate, never more than a full
                # month's fee in any one month (NS3 M1).
                dollars = sum(min(n, AGENCY_PIECES_PER_MONTH) * AGENCY_PER_PIECE for _, n in per_month)
                items.append({
                    "key": "content",
                    "label": f"{posts:,} {'post' if posts == 1 else 'posts'} written across {months} "
                             f"{'month' if months == 1 else 'months'}",
                    "dollars": round(dollars, 2), "hours": None,
                    "pieces": posts,
                    "rate": f"${AGENCY_PER_PIECE:,.0f} a piece, up to ${AGENCY_MONTHLY:,.0f} a month",
                    "basis": AGENCY_BASIS})

        if restaurant.module_labor:
            # DISTINCT WEEKS, not rows. schedule_history keeps a row per
            # generated draft — the weekly auto-draft job writes one every
            # week, and every regeneration writes another. A week scheduled
            # five times saved one schedule's worth of work, not five.
            # Counting rows produced 5,672 "schedules" and 8,508 hours for a
            # single restaurant, which is the same unbounded accrual the old
            # marketing figure was guilty of.
            weeks = _scalar(conn, "SELECT COUNT(DISTINCT week_start) FROM schedule_history "
                                  "WHERE restaurant_id=? AND week_start IS NOT NULL",
                            (restaurant_id,))
            if weeks:
                items.append({
                    "key": "schedules",
                    "label": f"{weeks:,} week{'' if weeks == 1 else 's'} of schedule built",
                    "dollars": None,
                    "hours": round(weeks * SCHEDULE_MINUTES / 60.0, 1),
                    "rate": f"{SCHEDULE_MINUTES} min a week",
                    "basis": "building a week's schedule by hand"})

        if restaurant.module_inventory and "inventory" not in denied:
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

def opportunity(restaurant_id: int, db_path: str = DB_PATH, denied_modules=None) -> dict:
    """The labor and food-cost figures that used to be called "delivered".

    Unchanged arithmetic, honest label. Both are gaps against a target —
    what the restaurant could still recover, which is the opposite of what
    it has already banked.
    """
    restaurant = get_restaurant(restaurant_id, db_path=db_path)
    if not restaurant:
        return {"items": [], "monthly": 0.0, "withheld": []}
    items, withheld = [], []
    if restaurant.module_labor:
        try:
            from labor import analyse_shifts_for_restaurant
            labor = analyse_shifts_for_restaurant(restaurant_id)
            # Sample shifts are not the restaurant's own numbers.
            if labor.get("is_live"):
                v = float(labor.get("potential_savings_monthly", 0) or 0)
                import thresholds as _thr
                if v > 0 and _thr.labor_cost_basis(restaurant) == "default":
                    # Hours × Cavnar's assumed $26/hr is not a dollar gap the
                    # restaurant has (Benchmarking audit #14).
                    withheld.append({"key": "labor", "label": "Scheduling against your target", "module": "labor",
                                     "source": "labor", "state": "default_rate",
                                     "reason": "labor cost rests on the assumed $26/hr, not your pay rates"})
                elif v > 0:
                    _dated(items, withheld, restaurant, {"key": "labor", "label": "Scheduling against your target",
                                                          "monthly": round(v, 2), "module": "labor"},
                           ("labor",), {"labor": labor}, db_path)
        except Exception:
            pass
    if restaurant.module_inventory and "inventory" not in set(denied_modules or ()):
        try:
            from inventory import analysis_for
            _items, live, inv = analysis_for(restaurant_id)
            if live:
                v = float(inv.get("recoverable_monthly", 0) or 0)
                if v > 0:
                    _dated(items, withheld, restaurant, {"key": "inventory", "label": "Waste above tolerance",
                                                          "monthly": round(v, 2), "module": "inventory"},
                           ("inventory", "waste"), None, db_path)
        except Exception:
            pass
    return {"items": items,
            "monthly": round(sum(i["monthly"] for i in items), 2),
            # Items held back because a source they rest on is stale: named,
            # dated, and carrying no dollars (DH1-4).
            "withheld": withheld,
            "basis": "gaps against your own targets — available, not captured"}


def _dated(items, withheld, restaurant, item, sources, context, db_path):
    """Date one opportunity item from the freshness registry and file it
    (DH1-4). The item's `as_of` is its stalest source's data date and
    `state` that source's state. A source that is stale, unknown or failing
    withholds the item — its gap was measured on data that no longer
    describes the restaurant (a June labor period, a waste log nobody has
    written to in weeks), so its dollars are neither shown nor in `monthly`.
    An aging source keeps the item, marked `aged` with its as-of date, so
    it is visibly old rather than silently current. Unreadable freshness
    keeps the item undated: the arithmetic is unchanged and the gap is
    real on the data it was read from."""
    import data_freshness as _df
    try:
        states = [_df.source_state(restaurant, k, db_path=db_path, context=context) for k in sources]
    except Exception:
        items.append(item)
        return
    use = [s for s in states if s and s.get("state") != "not_connected"]
    rank = {"unknown": 0, "stale": 1, "aging": 2, "current": 3}
    worst = min(use, key=lambda s: (0 if s.get("error") else 1, rank.get(s.get("state"), 0),
                                    s.get("pct") or 0)) if use else None
    if worst is None:
        items.append(item)
        return
    item["as_of"] = worst.get("as_of")
    item["as_of_iso"] = worst.get("as_of_iso")
    item["source"] = worst.get("key")
    item["state"] = worst.get("state")
    if worst.get("error") or worst.get("state") in ("stale", "unknown"):
        withheld.append({"key": item["key"], "label": item["label"], "module": item["module"],
                         "source": worst.get("key"), "state": worst.get("state"),
                         "as_of": worst.get("as_of"), "as_of_iso": worst.get("as_of_iso"),
                         "reason": (f"{worst.get('label') or worst.get('key')} is out of date"
                                    + (f" — {worst.get('basis')}" if worst.get("basis") else ""))})
        return
    item["aged"] = worst.get("state") == "aging"
    items.append(item)


# ── The whole picture ───────────────────────────────────────────────────────

def surfaced(restaurant_id: int, days: int = 30, db_path: str = DB_PATH, denied_modules=None) -> dict:
    """What the alerts raised this month were worth, from the dollars they
    already carried. A fourth figure, and a fourth thing not summed into the
    others: putting a problem in front of someone is not the same as their
    having fixed it. Only dollar-valued alert types count, and a module the
    viewer may not see is dropped before the sum (models.money_surfaced)."""
    try:
        from models import money_surfaced
        return money_surfaced(restaurant_id, days=days, db_path=db_path, denied_modules=denied_modules)
    except Exception:
        return {"days": days, "items": [], "dollars": 0.0, "alerts": 0}


def breakdown(restaurant_id: int, db_path: str = DB_PATH, denied_modules=None, scope=None) -> dict:
    """All four figures, never summed. Every Home surface reads this.

    `denied_modules` is threaded into each one rather than applied to the
    result, so a login without FOOD_COST_VIEW never receives a margin dollar
    in any figure — including inside a total it could otherwise subtract
    its way back through. `scope` (viewer_scope) also drops the results this
    login may not see from the delivered figure, before it is summed.
    """
    if scope is not None:
        denied_modules = set(scope.get("denied_modules") or ())
    d = delivered(restaurant_id, db_path=db_path, denied_modules=denied_modules, scope=scope)
    # Which "measured" figure this is (fix I7, CA4 F16): Home's is a MONTHLY
    # RATE; the owner report's is a sum over its window; a milestone's is an
    # all-time sum. Each carries its scope so no surface prints one under
    # another's words.
    if isinstance(d, dict):
        d = dict(d, scope="monthly_rate")
    return {
        "delivered": d,
        "avoided": avoided(restaurant_id, db_path=db_path, denied_modules=denied_modules),
        "opportunity": opportunity(restaurant_id, db_path=db_path, denied_modules=denied_modules),
        "surfaced": surfaced(restaurant_id, db_path=db_path, denied_modules=denied_modules),
    }


# The value section's two headings (fix I7, CA4 F6), served beside the four
# figures by /api/value as `sections`: the web put "Measured — What Cavnar AI
# has been worth" over all four tiles, two of which grow as the restaurant
# does worse. Only `delivered` is measured; the rest are what Cavnar surfaced
# or what is still available. Clients head each group with its own words.
VALUE_SECTIONS = (
    {"key": "measured", "heading": "What was measured", "figures": ["delivered"]},
    {"key": "surfaced", "heading": "What Cavnar surfaced / still available",
     "figures": ["avoided", "surfaced", "opportunity"]},
)


# What each module's measured dollars are called on the Home headline. Built
# from delivered()["by_module"] only — what was actually measured — never
# from the modules a restaurant has switched on (H-8): the phone credited
# "reviews answered, posts drafted", which cannot produce measured value.
MODULE_VALUE_LABELS = {"labor": "labor", "inventory": "food cost", "reviews": "rating",
                       "marketing": "promoted-day sales", "other": "other measured changes"}


def viewer_denied(user) -> set:
    """The modules this login may not see (permissions.MODULE_VIEW_PERMISSIONS).
    None or an admin: nothing denied. Fails closed: a lookup that errors
    denies every module."""
    if user is None or user.get("is_admin"):
        return set()
    try:
        from permissions import MODULE_VIEW_PERMISSIONS, has_permission
        return {k for k, perm in MODULE_VIEW_PERMISSIONS.items() if not has_permission(user, perm)}
    except Exception as e:
        print(f"[value] permission lookup failed, denying all modules: {e}")
        return {"reviews", "labor", "inventory", "marketing", "intel"}


def headline(restaurant_id: int, user=None, db_path: str = DB_PATH) -> dict:
    """The Home value headline as one viewer may see it (H-8).

    `monthly` is delivered()["monthly"] — a MONTHLY run-rate of measured
    improvements, labelled as such (it read "since you started" on the web
    and "since you joined" on the phone, so one $420/month win read as $420
    in total). `by_module` is where it came from, largest first. The
    viewer's scope (viewer_scope: denied modules, comps and voids without
    LOSS_VIEW, results whose recommendation it may not see) is applied
    before the sum, as /api/value does, so a manager never sees margin or
    owner-only dollars here either. `restaurant_wide` is False when anything
    was filtered: that figure is not the restaurant's, so it is never
    written as the day's snapshot.

    Contract K4 (the web Home `value` block and the mobile Home `value`
    object): `net_monthly`, `worsened` {count, monthly, priced_count},
    `cumulative` (outcomes.cumulative — dollars summed over days actually
    measured), `unpriced_wins` and `sales_lift` ride beside `monthly`;
    a surface shows the net when worsened.count > 0.

    Light on purpose (re-audit A35): total_value and the SQL-summed
    cumulative only — no best-ever, no rates, no second cumulative."""
    import outcomes
    scope = viewer_scope(restaurant_id, user, db_path=db_path)
    f = _scope_args(scope, None)
    v = outcomes.total_value(restaurant_id, db_path=db_path, **f)
    cum = outcomes.cumulative(restaurant_id, db_path=db_path, **f)
    parts = sorted(((m, float(x)) for m, x in (v.get("by_module") or {}).items() if x),
                   key=lambda x: -x[1])
    lift = v.get("sales_lift") or {}
    return {
        "monthly": int(round(v["monthly"] or 0)),
        # Beside the improvements, never folded into them (rec-ROI #1): what
        # got worse and the net, so a surface can show both.
        "net_monthly": int(round(v.get("net_monthly", v["monthly"]) or 0)),
        "worsened": v.get("worsened") or {"count": 0, "monthly": 0.0, "priced_count": 0},
        "by_module": [{"module": m, "label": MODULE_VALUE_LABELS.get(m, m), "monthly": round(x, 2)}
                      for m, x in parts],
        "wins": v.get("wins"),
        # Parts of `monthly` by attribution grade (CA2 #7), for a surface to
        # say apart — never summed with anything.
        "consistent_monthly": int(round(v.get("consistent_monthly") or 0)),
        "associated_monthly": int(round(v.get("associated_monthly") or 0)),
        "unpriced_wins": v.get("unpriced_wins") or [],
        "cumulative": cum,
        "sales_lift": {"monthly": lift.get("monthly", 0.0), "net_monthly": lift.get("net_monthly", 0.0),
                       "wins": lift.get("wins", 0), "basis": lift.get("basis")},
        "label": "measured, per month",
        # `monthly` is a monthly RATE; `cumulative` an all-time sum over
        # measured days (fix I7).
        "scope": "monthly_rate",
        "caveat": v.get("caveat"),
        "restaurant_wide": _restaurant_wide(scope),
    }


def home_block(vh, history=None) -> dict:
    """The `value` object both Homes carry (web /api/home/brief, mobile
    /mobile/api/home), from one headline() — contract K4:

      total          the improvements, per month (what `monthly` always was)
      net_monthly    total less what got worse — shown when worsened.count > 0
      worsened       {count, monthly, priced_count}
      cumulative     outcomes.cumulative: dollars summed over measured days
      unpriced_wins  wins with no honest dollar figure (a rating that rose)
      sales_lift     gross revenue measured, never added to the savings
    plus the history (net, per day snapshotted), the label and by_module."""
    vh = vh or {}
    return {"total": vh.get("monthly", 0), "net_monthly": vh.get("net_monthly", vh.get("monthly", 0)),
            "worsened": vh.get("worsened") or {"count": 0, "monthly": 0.0, "priced_count": 0},
            "cumulative": vh.get("cumulative"), "unpriced_wins": vh.get("unpriced_wins") or [],
            "sales_lift": vh.get("sales_lift"), "history": history or [], "per": "month",
            "scope": "monthly_rate", "cumulative_scope": "measured_days_sum",
            "label": vh.get("label"), "by_module": vh.get("by_module") or [], "caveat": vh.get("caveat")}


def value_lines(d) -> list:
    """The delivered figure as plain sentences, for the emails (monthly
    review, lifecycle): net of what got worse, the ×12 figure called a
    projection, the sum over measured days, and the sales lift apart from
    the savings (re-audit A29, A6). `d` is delivered()'s dict. [] when
    nothing was measured."""
    out = []
    wins = int(d.get("wins") or 0)
    worse = d.get("worsened") or {}
    net = float(d.get("net_monthly", d.get("monthly")) or 0)
    if wins or worse.get("priced_count"):
        s = (f"Measured results: ${float(d.get('monthly') or 0):,.0f}/month from {wins} "
             f"change{'' if wins == 1 else 's'} that improved")
        if worse.get("priced_count"):
            n = int(worse["priced_count"])
            tail = f"${net:,.0f}/month net" if net >= 0 else f"${abs(net):,.0f}/month below zero, net"
            s += f", less ${float(worse.get('monthly') or 0):,.0f}/month from {n} that got worse — {tail}"
        s += "."
        if net > 0:
            s += f" If that holds for a year, about ${net * 12:,.0f} — a projection, not a measurement."
        assoc, clear = float(d.get("associated_monthly") or 0), float(d.get("consistent_monthly") or 0)
        if assoc and clear:
            s += (f" Of the improvements, ${clear:,.0f}/month were clear moves or held at their re-check; "
                  f"${assoc:,.0f}/month came alongside other changes or crossed normal variation only once.")
        elif assoc:
            s += " Every improvement came alongside other changes or crossed normal variation only once."
        out.append(s)
    cum = d.get("cumulative") or {}
    if cum.get("total") is not None and cum.get("measured_days"):
        total = float(cum["total"])
        out.append(f"Summed over the {int(cum['measured_days'])} days actually measured so far: "
                   f"{'$' if total >= 0 else 'minus $'}{abs(total):,.0f}, net of anything that got worse.")
    lift = d.get("sales_lift") or {}
    if lift.get("wins"):
        n = int(lift["wins"])
        out.append(f"Sales rose about ${float(lift.get('monthly') or 0):,.0f}/month across {n} "
                   f"change{'' if n == 1 else 's'} — gross revenue, not profit, so it is not added to the "
                   f"savings.")
    return out


def compute_total_value_delivered(restaurant_id: int, db_path: str = DB_PATH) -> int:
    """The headline figure: measured monthly dollars, NET of what got worse
    (re-audit A29), and nothing else.

    Kept under its original name because the web banner, the mobile Home
    payload and the value snapshots all call it. What changed is what it
    MEANS — it is now only what was measured, so it is a number that can be
    defended line by line, and for most restaurants it starts at zero and
    grows as trackers close. That is the true state, and the old figure's
    only advantage was that it was never true. total_value alone: no
    cumulative, no best-ever (re-audit A35).
    """
    try:
        import outcomes
        v = outcomes.total_value(restaurant_id, db_path=db_path)
        return int(round(v.get("net_monthly", v["monthly"]) or 0))
    except Exception as e:
        # Fails to 0 rather than 500ing the Home page, but never silently:
        # this swallow hid a TypeError for a whole test run once already.
        try:
            import ops
            ops.capture(e, job="value_delivered", context=f"restaurant_id={restaurant_id}")
        except Exception:
            pass
        return 0


def record_value_snapshot(restaurant_id: int, total_value: int, db_path: str = DB_PATH):
    """Upserts today's total — the NET monthly figure (headline's
    net_monthly, re-audit A29) — called opportunistically from the Home
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


# ── the ledger: what has been done, since the account began ─────────────────

def ledger(restaurant_id, db_path=None):
    """Distinct work counted since sign-up — not dollars, not estimates.

    The retention audit found the product compounds (replies learn from
    approved examples, the assistant remembers, shift profiles accumulate)
    and never says so: the only "since you started" surface was the value
    hero, which reads "Nothing measured yet" for any account that has not
    pressed Track. This is the other half — the plain count of things done,
    which needs no button and cannot be argued with.

    Every figure counts DISTINCT work (the ROI audit's rule): replies are
    reviews with a draft, not draft attempts; schedules are distinct weeks,
    not rows in schedule_history; months are calendar months with activity.
    """
    from models import get_conn, DB_PATH
    db_path = db_path or DB_PATH
    conn = get_conn(db_path)
    try:
        def one(sql, *a):
            try:
                r = conn.execute(sql, a).fetchone()
                return int((r[0] if r else 0) or 0)
            except Exception:
                return 0
        started = None
        try:
            row = conn.execute("SELECT created_at FROM restaurants WHERE id=?",
                               (restaurant_id,)).fetchone()
            started = (row[0] or "")[:10] if row else None
        except Exception:
            pass
        out = {
            "started": started,
            "replies_drafted": one("SELECT COUNT(*) FROM reviews WHERE restaurant_id=? AND deleted_at IS NULL "
                                   "AND draft_response IS NOT NULL", restaurant_id),
            "replies_posted": one("SELECT COUNT(*) FROM reviews WHERE restaurant_id=? AND deleted_at IS NULL "
                                  "AND response_status='posted'", restaurant_id),
            "reviews_watched": one("SELECT COUNT(*) FROM reviews WHERE restaurant_id=? AND deleted_at IS NULL",
                                   restaurant_id),
            "schedules_built": one("SELECT COUNT(DISTINCT week_start) FROM schedule_history "
                                   "WHERE restaurant_id=? AND week_start IS NOT NULL", restaurant_id),
            "alerts_sent": one("SELECT COUNT(*) FROM alert_log WHERE restaurant_id=?", restaurant_id),
            "issues_resolved": one("SELECT COUNT(*) FROM ops_issues WHERE restaurant_id=? AND status='resolved'",
                                   restaurant_id),
            "outcomes_measured": one("SELECT COUNT(*) FROM recommendation_outcomes WHERE restaurant_id=? "
                                     "AND status='evaluated'", restaurant_id),
            # Distinct measured improvements, priced or not: a rating that
            # rose is a win even though nothing here can price it (#49).
            "outcomes_improved": _improved_count(restaurant_id, db_path),
            "milestones": one("SELECT COUNT(*) FROM milestones WHERE restaurant_id=?", restaurant_id),
            "months_active": one("SELECT COUNT(DISTINCT substr(fired_at,1,7)) FROM alert_log "
                                 "WHERE restaurant_id=?", restaurant_id),
        }
    finally:
        conn.close()
    return out


def _improved_count(restaurant_id, db_path):
    """Distinct measured improvements that still count, priced or not."""
    try:
        import outcomes
        return int(outcomes.total_value(restaurant_id, db_path=db_path).get("wins_measured") or 0)
    except Exception as e:
        print(f"[value] improvements count unreadable for {restaurant_id}: {e}")
        return 0


def ledger_lines(led):
    """The ledger as sentences, largest first, zeros omitted."""
    items = [
        (led.get("replies_drafted", 0), "review {n} drafted in your voice", "review replies drafted in your voice"),
        (led.get("replies_posted", 0), "reply posted to Google", "replies posted to Google"),
        (led.get("schedules_built", 0), "week's schedule built", "weeks' schedules built"),
        # alerts_sent counts every alert_log row (briefs, 5-star alerts
        # included), so it says what it counts: nothing here knows any of
        # them came "before it became a problem" (NS1 H5).
        (led.get("alerts_sent", 0), "alert sent to you", "alerts sent to you"),
        (led.get("issues_resolved", 0), "issue resolved with a name on it", "issues resolved with a name on them"),
        (led.get("outcomes_measured", 0), "change measured before and after", "changes measured before and after"),
        (led.get("outcomes_improved", 0), "change measured as an improvement", "changes measured as improvements"),
    ]
    out = []
    for n, one_form, many_form in sorted(items, key=lambda x: -x[0]):
        if n <= 0:
            continue
        label = one_form.replace("{n}", "reply") if n == 1 else many_form
        out.append(f"{n:,} {label}")
    return out
