"""
bar/judge.py — the ONE judgement policy for the Bar page (barfix PLAN
Decision 10). Every tone, every dollar figure over a range or a target and
every verdict insight passes `judge`; a reading that fails it is still shown,
with `judged: False` and the reason it was not judged.

A reading is judged only when:

- the window holds four weeks (MIN_DAYS) of invoices and sales together — a
  1-day window read 300% and "$1,128 over your target in four weeks" (V8);
- the category's sales are on file for at least 85% of the days the bar was
  open (V29), and the bar was open about as often as it usually is (a
  closure after a stock-up read "above range");
- the invoice feed is fresh: invoices no more than FEED_GRACE_DAYS behind
  today, or the slowest active house's own rhythm (V22);
- no stock-up sits in the window: one delivery over STOCK_UP_WEEK_MULTIPLE x
  that house's median week, or a reading over STOCK_UP_RANGE_MULTIPLE x the
  range's top — the delivery is named, never judged as over-pouring (V11);
- the category is complete: no distributor's invoices missing (the gap
  detector, bar.pour_cost._gaps), no unbalanced invoice (W1 exposes
  `balanced` per line), and credits not exceeding purchases (V37).

It also keeps the owner's "we stopped buying from X" (bar_stopped_distributors,
created at boot by init_bar_judge), so a supplier change is reported once and
never read as invoices missing (V9).

Pure apart from that store: no model, no estimate.
"""
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from bar.common import POUR_CATEGORIES, _mdy, _money, get_conn
from bar.ledger import ledger_of, sales_index

# Four weeks of invoices and sales together before anything is judged.
MIN_DAYS = 28
# A category's sales on file for at least this share of the bar's open days.
MIN_SALES_COVERAGE = 0.85
# The bar open at least this share of its usual open days in the window.
MIN_OPEN_SHARE = 0.85
# Invoices this many days behind today (or the slowest active house's
# rhythm, if longer, capped at FEED_GRACE_MAX_DAYS) and the feed is stale.
FEED_GRACE_DAYS = 7
FEED_GRACE_MAX_DAYS = 31
# A delivery over this multiple of its house's median week is a stock-up;
# the median needs this many weeks with a delivery behind it.
STOCK_UP_WEEK_MULTIPLE = 3.0
STOCK_UP_MIN_WEEKS = 3
# A reading over this multiple of the range's top is a stock-up whatever
# the house's history.
STOCK_UP_RANGE_MULTIPLE = 2.0
# A delivery explains a high reading only when it is a real part of what the
# category bought in the window (EJ's, 10/15/26: a $630 High Noon order from
# Breakthru, 3x that house's small beer weeks, held back a 21% beer reading
# inside its range, though beer buys ~$13,000 in four weeks).
STOCK_UP_MIN_SHARE = 0.25
# How far back a house's rhythm and median week are read.
HISTORY_DAYS = 91

# Why a reading is not judged, most telling first; `not_judged` is the
# first that applies, `reasons` all of them.
REASONS = ("feed_stale", "credits_exceed", "no_reading", "short_window", "closed_days", "sales_coverage",
           "sales_gap", "incomplete", "unbalanced", "stock_up")

# The sentence each reason is said with (owner copy; never "pour cost" for a
# purchase ratio). Filled by `reason_text`.
_REASON_TEXT = {
    "feed_stale": "No invoices since {last_mdy} — check the Fintech forward. Nothing is judged until they arrive.",
    "credits_exceed": "Credits exceed purchases in this window, so there is no reading to judge.",
    "no_reading": "Not measured: purchases and sales aren't both on file for this window.",
    "short_window": "Judged once four weeks of invoices and sales are on file — {days} of 28 days so far.",
    "closed_days": "The bar was open {open_days} of {days} days — what was bought before a closure reads high, "
                   "so this window isn't judged.",
    "sales_coverage": "Sales for this category are on file for {have} of {open_days} open days — judged once the "
                      "nightly reports fill in.",
    "sales_gap": "The nightly report for {gap_mdy} didn't sort its drink sales into categories, so this window "
                 "isn't judged until that night is repaired.",
    "incomplete": "Some invoices for this category aren't on file, so it reads low until they are.",
    "unbalanced": "An invoice for this category didn't add up to its total, so it isn't judged until it's re-read.",
    "stock_up": "A large delivery on {date_mdy} — {amount_text} from {distributor} — makes this window read high; it "
                "evens out as it pours. Count the shelf to know.",
}


def reason_text(reason, **facts) -> str:
    """The owner's sentence for one not-judged reason."""
    t = _REASON_TEXT.get(reason)
    if not t:
        return ""
    try:
        return t.format(**facts)
    except (KeyError, IndexError, ValueError):
        return t.split(" — ")[0].split("{")[0].strip()


def _d(iso):
    return date.fromisoformat(str(iso)[:10])


# ── open days and sales coverage ──────────────────────────────────────────

def open_days(sales, start, end) -> set:
    """The days in [start, end] with any bar sale on file: the bar was open."""
    return set(sales_index(sales).open_between(start, end))


def usual_open_share(sales, start):
    """The share of days the bar was open over the HISTORY_DAYS before
    `start` (from its first sales day), or None with under four weeks of
    history: what "open as usual" is measured against."""
    days = sales_index(sales).open_before(start)
    if not days:
        return None
    lo = max(_d(days[0]), _d(start) - timedelta(days=HISTORY_DAYS))
    span = (_d(start) - lo).days
    if span < MIN_DAYS:
        return None
    n = sum(1 for d in days if d >= lo.isoformat())
    return n / span


# ── houses: rhythm, active, stopped ───────────────────────────────────────

def houses(lines, end) -> dict:
    """{distributor: {dates, first, last, typical, cats, amount}} from the
    invoices on file through `end`: its invoice dates, its typical days
    between invoices (median step; None with one date), the pour categories
    it supplied in the HISTORY_DAYS before its own last invoice, and its
    median pour-category spend per delivery."""
    return ledger_of(lines).houses(end, HISTORY_DAYS, _d,
                                   lambda iso, n: (_d(iso) - timedelta(days=n)).isoformat())


def stopped(lines, end, owner_stopped=()) -> list:
    """Distributors no longer expected through `end`, each reported once:
    [{distributor, last, last_mdy, categories, why, taken_over_by}].
    why is "owner" (the owner said "we stopped buying from X"), "switched"
    (silent over 4x its rhythm and at least four weeks, with another house
    supplying each of its categories since) or "dormant" (no invoice in
    HISTORY_DAYS). A stopped house is never a gap."""
    lg = ledger_of(lines)
    hs = houses(lines, end)
    owner = {" ".join(str(x).split()).lower() for x in (owner_stopped or ())}
    out = []
    for dist, h in hs.items():
        quiet = (_d(end) - _d(h["last"])).days
        row = {"distributor": dist, "last": h["last"], "last_mdy": _mdy(h["last"]), "categories": h["cats"],
               "why": None, "taken_over_by": []}
        if " ".join(str(dist).split()).lower() in owner:
            row["why"] = "owner"
        elif quiet > HISTORY_DAYS:
            row["why"] = "dormant"
        elif h["typical"] and quiet > max(MIN_DAYS, 4 * h["typical"]) and h["cats"]:
            takers = set()
            covered = True
            for c in h["cats"]:
                who = {l["distributor"] for l in lg.in_cat(c, h["last"], end, lo_open=True)
                       if l["distributor"] != dist and l["net"] > 0}
                if not who:
                    covered = False
                    break
                takers |= who
            if covered:
                row["why"] = "switched"
                row["taken_over_by"] = sorted(takers)
        if row["why"]:
            out.append(row)
    return sorted(out, key=lambda r: r["last"])


# ── feed freshness ────────────────────────────────────────────────────────

def freshness(lines, sales, today, feed=None) -> dict:
    """{invoices_through, invoices_through_mdy, sales_through, sales_through_mdy,
    lag_days, grace_days, stale, stale_days}. `feed` is the invoice feed's own
    status (bar.feed.feed_status, W2) when it is known — None means unknown,
    and the invoices on file say how far they reach. Stale when invoices are
    more than FEED_GRACE_DAYS behind today, or more than the slowest active
    house's rhythm if that is longer (capped at FEED_GRACE_MAX_DAYS)."""
    today_iso = today.isoformat()
    inv = ledger_of(lines).days_through(today_iso)
    sal = [d for d in sales_index(sales).all_days if d <= today_iso]
    through = (feed or {}).get("invoices_through") or (inv[-1] if inv else None)
    hs = houses(lines, today_iso)
    active = [h["typical"] for h in hs.values()
              if h["typical"] and (today - _d(h["last"])).days <= HISTORY_DAYS]
    grace = max(FEED_GRACE_DAYS, min(max(active) if active else 0, FEED_GRACE_MAX_DAYS))
    lag = (today - _d(through)).days if through else None
    # The feed says how far invoices reach; staleness is judged here, against
    # the slowest active house's rhythm: the feed's own flag is a fixed week,
    # which would call a biweekly house stale on day 8.
    stale = lag is not None and lag > grace
    stale_days = lag if stale else None
    return {"invoices_through": through, "invoices_through_mdy": _mdy(through) if through else "",
            "sales_through": sal[-1] if sal else None, "sales_through_mdy": _mdy(sal[-1]) if sal else "",
            "last_file_mdy": (feed or {}).get("last_file_mdy"),
            "lag_days": lag, "grace_days": grace, "stale": bool(stale), "stale_days": stale_days}


# ── stock-ups ─────────────────────────────────────────────────────────────

def _week(iso):
    d = _d(iso)
    return (d - timedelta(days=d.weekday())).isoformat()


def stock_up(lines, cat, start, end, pct=None, range_hi=None):
    """The delivery that makes [start, end] read high for `cat`, or None —
    only ever for a reading above its range's top (a stock-up makes a window
    read high; it never explains a reading inside or below the range):
    {date, date_mdy, distributor, amount, amount_text, why}. A delivery is a
    stock-up when it is over STOCK_UP_WEEK_MULTIPLE x its house's median week
    of `cat` (weeks with a delivery, in the HISTORY_DAYS to `end`, the
    delivery itself left out; at least STOCK_UP_MIN_WEEKS of them), or —
    whatever the history — when the reading is over STOCK_UP_RANGE_MULTIPLE x
    the range's top (the window's largest delivery is named)."""
    since = (_d(end) - timedelta(days=HISTORY_DAYS)).isoformat()
    deliveries = defaultdict(float)
    weeks = defaultdict(lambda: defaultdict(float))
    for l in ledger_of(lines).in_cat(cat, min(start, since), end):
        d = str(l["invoice_date"])[:10]
        if start <= d <= end:
            deliveries[(l["distributor"], d)] += l["net"]
        if since <= d <= end:
            weeks[l["distributor"]][(_week(d), d)] += l["net"]
    if not deliveries:
        return None
    if pct is None or not range_hi or pct <= range_hi:
        return None
    bought = sum(v for v in deliveries.values() if v > 0)
    best = None
    usual = set()
    for (dist, d), amt in deliveries.items():
        if amt <= 0:
            continue
        per_week = defaultdict(float)
        for (wk, dd), v in weeks[dist].items():
            if dd != d:
                per_week[wk] += v
        base = sorted(v for v in per_week.values() if v > 0)
        if len(base) < STOCK_UP_MIN_WEEKS:
            continue
        median = base[len(base) // 2]
        if amt > STOCK_UP_WEEK_MULTIPLE * median and amt >= STOCK_UP_MIN_SHARE * bought:
            if best is None or amt > best[2]:
                best = (dist, d, amt, "house_week", median)
        else:
            usual.add((dist, d))
    if best is None and pct > STOCK_UP_RANGE_MULTIPLE * range_hi:
        # The window's largest delivery, unless its own house's history
        # shows it is an ordinary week: a bar that buys this much every
        # week is read as it is, not excused as a stock-up.
        (dist, d), amt = max(deliveries.items(), key=lambda kv: kv[1])
        if amt > 0 and (dist, d) not in usual:
            best = (dist, d, amt, "range", None)
    if best is None:
        return None
    dist, d, amt, why, median = best
    return {"date": d, "date_mdy": _mdy(d), "distributor": dist, "amount": round(amt, 2),
            "amount_text": _money(amt), "why": why,
            "median_week": round(median, 2) if median is not None else None}


# ── the judgement ─────────────────────────────────────────────────────────

def judge(pct, purchases, *, window_days, open_n=None, have_n=None, usual_share=None, fresh=None,
          stock=None, incomplete=False, unbalanced=False, sales_gap=()) -> dict:
    """{judged, not_judged, reasons, not_judged_text} for one reading.

    pct / purchases     the reading and the window's net purchases
    window_days         calendar days in the window
    open_n / have_n     days the bar was open / days this category's sales
                        are on file (None: not counted — a total)
    usual_share         the bar's usual open-day share (usual_open_share)
    fresh               freshness(); stale -> not judged
    stock               stock_up() or None
    incomplete          a distributor's invoices missing (_gaps)
    unbalanced          an invoice in the window that didn't add up
    sales_gap           nights in the window whose drink sales weren't
                        sorted (bar.sales, V5): ISO dates"""
    reasons = []
    if fresh and fresh.get("stale"):
        reasons.append("feed_stale")
    if purchases is not None and purchases < 0:
        reasons.append("credits_exceed")
    elif pct is None:
        reasons.append("no_reading")
    if window_days < MIN_DAYS:
        reasons.append("short_window")
    if open_n is not None and usual_share and window_days and open_n < MIN_OPEN_SHARE * usual_share * window_days:
        reasons.append("closed_days")
    if have_n is not None and open_n and have_n < MIN_SALES_COVERAGE * open_n:
        reasons.append("sales_coverage")
    if sales_gap:
        reasons.append("sales_gap")
    if incomplete:
        reasons.append("incomplete")
    if unbalanced:
        reasons.append("unbalanced")
    if stock:
        reasons.append("stock_up")
    reasons = sorted(set(reasons), key=REASONS.index)
    first = reasons[0] if reasons else None
    facts = {"days": window_days, "open_days": open_n, "have": have_n,
             "last_mdy": (fresh or {}).get("invoices_through_mdy") or "—",
             "gap_mdy": ", ".join(_mdy(d) for d in sales_gap) if sales_gap else ""}
    facts.update(stock or {})
    return {"judged": not reasons, "not_judged": first, "reasons": reasons,
            "not_judged_text": reason_text(first, **facts) if first else None}


# ── evidence and freshness as percentages (R19) ───────────────────────────

def confidence(window_days=None, coverage=None, invoices=None, lag_days=None) -> dict:
    """{evidence_pct, freshness_pct}, each 0–100, computed from what the
    reading rests on: evidence = the window's share of four weeks x the
    sales coverage x the invoices in it (four or more is whole); freshness =
    100 while invoices are two days behind or less, falling to 0 at
    fourteen. A part that is unknown counts as whole (None -> not held
    against it); an unknown lag gives no freshness (None)."""
    ev = 1.0
    if window_days is not None:
        ev *= min(1.0, max(0.0, window_days / MIN_DAYS))
    if coverage is not None:
        ev *= min(1.0, max(0.0, coverage))
    if invoices is not None:
        ev *= min(1.0, max(0.0, invoices / 4.0))
    fr = None
    if lag_days is not None:
        fr = round(max(0.0, min(100.0, 100.0 - 100.0 * max(0, lag_days - 2) / 12.0)))
    return {"evidence_pct": round(ev * 100), "freshness_pct": fr}


# ── the owner's "we stopped buying from X" ────────────────────────────────

def init_bar_judge(db_path=None):
    """bar_stopped_distributors: a distributor the owner said the bar stopped
    buying from — never a gap again. Boot only (models.init_db)."""
    conn = get_conn(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS bar_stopped_distributors (
            restaurant_id INTEGER NOT NULL,
            distributor TEXT NOT NULL,
            set_by INTEGER,
            set_at TEXT NOT NULL,
            PRIMARY KEY (restaurant_id, distributor))""")
        conn.commit()
    finally:
        conn.close()


def owner_stopped(restaurant_id, db_path=None) -> list:
    """The distributors the owner said the bar stopped buying from."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT distributor FROM bar_stopped_distributors WHERE restaurant_id=? "
                            "ORDER BY distributor", (int(restaurant_id),)).fetchall()
    except Exception as e:
        print(f"[bar.judge] stopped distributors unreadable for {restaurant_id}: {e}")
        rows = []
    finally:
        conn.close()
    return [r["distributor"] for r in rows]


def set_stopped(restaurant_id, distributor, stopped_buying=True, user_id=None, db_path=None) -> dict:
    """The owner's word that the bar stopped (or resumed) buying from a
    distributor. Keyed on the name lowercased and trimmed."""
    key = " ".join(str(distributor or "").split()).lower()
    if not key:
        raise ValueError("Name the distributor.")
    conn = get_conn(db_path)
    try:
        if stopped_buying:
            conn.execute("INSERT INTO bar_stopped_distributors (restaurant_id, distributor, set_by, set_at) "
                         "VALUES (?,?,?,?) ON CONFLICT(restaurant_id, distributor) DO UPDATE SET "
                         "set_by=excluded.set_by, set_at=excluded.set_at",
                         (int(restaurant_id), key, user_id,
                          datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")))
        else:
            conn.execute("DELETE FROM bar_stopped_distributors WHERE restaurant_id=? AND distributor=?",
                         (int(restaurant_id), key))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "distributor": key, "stopped": bool(stopped_buying)}
