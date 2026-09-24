"""
outcomes.py — did the recommendation work?

Every module in this product recommends things. Nothing ever checked whether
the owner did one and whether the number it was aimed at moved. That left the
product unable to say the one sentence that justifies it — "you trimmed Monday
lunch, labor moved 1.8 points, that's about $410 a month" — and left the
assistant unable to learn which of its suggestions work for this restaurant.

The shape is deliberately simple:

  record()    the owner commits to a recommendation. The metric it targets is
              measured over a window BEFORE today (matched by weekday, and by
              the same weeks last year where there is a year of history) and
              stored with the kind of baseline it is.
  evaluate()  once the same length of window has passed AFTER, re-measure,
              compare with metrics.compare (which respects each metric's
              noise band), list the other changes inside the window, grade
              how strongly the move can be tied to the change, and store it.
  recheck()   a win (or a loss) is read again at RECHECK_DAYS: one that no
              longer holds stops counting and stops accruing.
  accrue_*()  measured dollars accrue day by day into outcome_value_days,
              only for days actually measured and only while the move holds,
              so "over six months" is a sum of measured days, never
              monthly x 12.

Two honesty rules, both enforced here rather than left to wording:

  BEFORE-AND-AFTER IS NOT CAUSE. A verdict says the number moved while the
  change was in place. It never says the change moved it — a busy month, a
  menu change or a new hire all move the same numbers. Every verdict carries
  a graded attribution label (ATTRIBUTION) and none of them claims cause.

  UNKNOWN STAYS UNKNOWN. If either window cannot be measured the verdict is
  "unknown", not a zero delta and not a failure; a day that cannot be
  measured accrues nothing rather than zero-or-something.

One change per number (the rec-ROI audit, #3/#4): a second tracker on a
metric already being measured is refused on every entry point (record's
`gate`), an automatic tracker is refused when anything in the metric's
FAMILY is being measured, and related metrics measured over the same weeks
count as one result.
"""
import json
from datetime import date, datetime, timedelta

import metrics
import models as _models_mod
from models import DB_PATH


def get_conn(db_path=None):
    """models.get_conn resolved at call time (CLAUDE.md, bound imports): the
    bound copy kept writing trackers to whatever database models.get_conn
    pointed at when this module was first imported."""
    if db_path is None or db_path == DB_PATH:
        return _models_mod.get_conn()
    return _models_mod.get_conn(db_path)


CAUSATION_CAVEAT = ("Measured before and after, not proven cause: other changes in "
                    "the same weeks move the same number.")

# ── the rules, each stated once ─────────────────────────────────────────────
# A measured move is read again this long after the change started (audit
# #33), and never inside its own after-window: the re-check window is the
# metric's window ending the day before recheck_on.
RECHECK_DAYS = 90
# How long a change keeps accruing measured dollars after it started. A win
# still holding a year on has become how the restaurant runs; crediting it
# forever would be the months-since-signup accrual CLAUDE.md forbids.
ACCRUAL_HORIZON_DAYS = 365
# Days a single accrual pass reads per tracker (a long-evaluated win catches
# up over several passes rather than in one), and how long a day with no
# data is waited for before it is passed over as not measured.
ACCRUAL_MAX_DAYS_PER_PASS = 31
ACCRUAL_GRACE_DAYS = 3
# A baseline adjusted by last year's same weeks carries last year's wobble
# as well as this year's; the noise band is widened by about sqrt(2) for it.
SEASONAL_BAND_SCALE = 1.4
# Last year's windows must each have this share of their days measured
# before they adjust anything (per-day metrics).
LY_MIN_COVERAGE = 0.8
LY_OFFSET_DAYS = 364              # 52 weeks: the same weekday last year
# A move of at least this many noise bands, with nothing else changing on
# the same number, is "consistent" rather than "associated".
CONSISTENT_MULTIPLE = 2.0

BASELINE_KINDS = ("prior window", "matched weekdays", "same weeks last year")
ATTRIBUTION = ("none", "associated", "consistent", "held")
RECHECK_VERDICTS = ("held", "faded", "reversed", "unknown")
_MOVED = ("improved", "worsened")
_FAILED_RECHECK = ("faded", "reversed")

_ISO_DATE = None


def owner_title(title) -> str:
    """A tracker title as an owner reads it: every ISO date in it as M/D/YY.
    Callers write details like "week of 2026-09-07" (client_api's schedule
    publish); the owner reads "week of 9/7/26" (CLAUDE.md, dates)."""
    global _ISO_DATE
    import re
    from time_utils import mdy
    if _ISO_DATE is None:
        _ISO_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
    return _ISO_DATE.sub(lambda m: mdy(m.group(1)), str(title or ""))


def _iso(v):
    return str(v or "")[:10]


def _day(v):
    return date.fromisoformat(_iso(v))


def _lower_first(label) -> str:
    """"Labor %" -> "labor %", "Sales on Tuesday" -> "sales on Tuesday"."""
    label = str(label or "")
    return label[:1].lower() + label[1:] if label else label


def _window_days(r) -> int:
    try:
        return max(1, (_day(r["evaluate_on"]) - _day(r["started_on"])).days)
    except (KeyError, TypeError, ValueError):
        return 28


def _after_end(r) -> str:
    return _iso(r.get("after_end")) or (_day(r["evaluate_on"]) - timedelta(days=1)).isoformat()


def _concurrent_list(r):
    raw = r.get("concurrent")
    if raw in (None, ""):
        return None
    if isinstance(raw, list):
        return raw
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else None
    except (TypeError, ValueError):
        return None


def _row(r):
    d = dict(r)
    if d.get("title"):
        d["title"] = owner_title(d["title"])
    d["informational"] = is_informational(d)
    known = metrics.known(d["metric"])
    info = metrics.describe(d["metric"]) if known else {}
    d["metric_label"] = info.get("label", d["metric"])
    d["unit"] = info.get("unit")
    d["family"] = metrics.family(d["metric"])
    d["module"] = d.get("module") or module_of(d["metric"])
    d["window_days"] = _window_days(d)
    # A row written before baseline kinds existed was always a prior window.
    d["baseline_kind"] = d.get("baseline_kind") or "prior window"
    if d.get("delta_pct") is None and d.get("delta") is not None and d.get("baseline_value"):
        d["delta_pct"] = round(float(d["delta"]) / float(d["baseline_value"]) * 100, 1)
    conc = _concurrent_list(d)
    d["concurrent_checked"] = conc is not None
    d["concurrent"] = conc or []
    if d.get("status") == "evaluated":
        # Stored at evaluation; a row evaluated before grading existed never
        # had its window checked for other changes, so it can be at most
        # "associated" — never "consistent" on a check that did not happen.
        if not d.get("attribution"):
            d["attribution"] = grade(d.get("verdict"), None, conc, checked=conc is not None)
    else:
        d["attribution"] = None
    d["owner_checkin"] = _checkin_of(d)
    d["attribution_label"] = attribution_label(d)
    d["validated"] = is_validated(d)
    d["counts"] = counts_in_delivered(d)
    d["result_line"] = result_line(d) if d.get("status") == "evaluated" else None
    return d


def known_metric(metric):
    return metrics.known(metric)


# ── modules: whose recommendation was it ────────────────────────────────────
# A win is credited to the module of the RECOMMENDATION that started it
# (audit #5), stored on the row. The metric's module is only the fallback for
# a tracker nothing recommended (a manual one, or Ask on the owner's own
# idea): "sales" used to map to labor, so every marketing Track landed in
# Labor's value.
VALUE_MODULES = ("labor", "inventory", "reviews", "marketing", "intel", "other")
# rec_ledger.MODULES -> the module value is reported under (permissions.
# MODULE_VIEW_PERMISSIONS' keys, value_delivered.MODULE_VALUE_LABELS).
REC_MODULE_TO_VALUE = {"reviews": "reviews", "labor": "labor", "schedule": "labor", "food": "inventory",
                       "marketing": "marketing", "guests": "marketing", "intel": "intel", "ops": "other"}
SOURCE_MODULE = {"reprice": "inventory", "slow_day_campaign": "marketing", "schedule": "labor"}
OBSERVED_MODULE = {"schedule_published": "labor", "supplier_order_sent": "inventory"}

# The fallback: a module for every metric, for a tracker no recommendation
# started. "sales" is nobody's module — a sales move credited to Labor was
# the mislabel the audit found (outcomes.py:333).
METRIC_MODULE = {
    "labor_pct": "labor", "overtime_hours": "labor", "sales": "other", "weekday_sales": "marketing",
    "food_cost_pct": "inventory", "weekly_waste": "inventory",
    "avg_rating": "reviews", "complaints": "reviews", "response_hours": "reviews",
    "comp_rate": "labor", "void_rate": "labor",
}


def module_of(metric) -> str:
    """Which module a metric belongs to — the fallback when no recommendation
    names one (module_of_row)."""
    base, _ = metrics.parse(metric)
    return METRIC_MODULE.get(base, "other")


def module_of_row(r) -> str:
    """The module a tracker is credited to: the one stored on it, else its
    metric's."""
    return (r or {}).get("module") or module_of((r or {}).get("metric"))


def value_module(m):
    """A module name in the value vocabulary, from either vocabulary."""
    if not m:
        return None
    m = str(m)
    return m if m in VALUE_MODULES else REC_MODULE_TO_VALUE.get(m)


def _latest_rec(restaurant_id, key, db_path=DB_PATH):
    """The latest rec_instances episode for this key, or None."""
    if not key:
        return None
    conn = get_conn(db_path)
    try:
        return conn.execute("SELECT module, expected_metric, title FROM rec_instances WHERE restaurant_id=? "
                            "AND key=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                            (restaurant_id, str(key))).fetchone()
    except Exception as e:
        # rec_instances is created at boot (rec_ledger.init_rec_ledger); a
        # database without it simply has no recommendation to read.
        print(f"[outcomes] rec_instances unreadable for {restaurant_id}: {e}")
        return None
    finally:
        conn.close()


def presented_title(restaurant_id, key, db_path=DB_PATH):
    """The words a recommendation was last shown with, or None."""
    rec = _latest_rec(restaurant_id, key, db_path)
    return rec["title"] if rec is not None and rec["title"] else None


def _dsr_block_module(key):
    """dsr_action:<kind>:<block>[/<entity>] -> the value module of <block>."""
    parts = str(key or "").split(":")
    if len(parts) < 3:
        return None
    block = parts[2].split("/", 1)[0]
    try:
        from dsr.narrative import _MODULE as _DSR_MODULE
        return value_module(_DSR_MODULE.get(block))
    except Exception as e:
        print(f"[outcomes] DSR block module unreadable: {e}")
        return None


def resolve_module(restaurant_id, source, source_key, metric, module=None, db_path=DB_PATH) -> str:
    """The module a new tracker is credited to, most specific first: the
    caller's, the recommendation's own (rec_instances, by key), a DSR
    action's block, the source's, then the metric's."""
    vm = value_module(module)
    if vm:
        return vm
    rec = _latest_rec(restaurant_id, source_key, db_path)
    if rec is not None:
        vm = value_module(rec["module"])
        if vm:
            return vm
    if str(source_key or "").startswith("dsr_action:"):
        vm = _dsr_block_module(source_key)
        if vm:
            return vm
    if source in SOURCE_MODULE:
        return SOURCE_MODULE[source]
    key = str(source_key or "")
    if key.startswith("observed:"):
        action = key.split(":")[1] if ":" in key else ""
        if action in OBSERVED_MODULE:
            return OBSERVED_MODULE[action]
        if action.startswith("alert_"):
            spec = ALERT_METRICS.get(action[len("alert_"):])
            if spec:
                return module_of(spec[0])
    return module_of(metric)


# ── what a recommendation measures ──────────────────────────────────────────
# A DSR action is measured by its KIND (dsr/narrative.py ACTION_KINDS), and
# only where the kind has an honest number (audit #39). A reorder, a talk
# with the team, a price or menu-mix change, a post: nothing here can say
# what they moved, so they carry no metric and nothing starts.
DSR_ACTION_METRICS = {
    "adjust_staffing": "labor_pct",
    "control_hours": "labor_pct",
    "coach_team": None,
    "reorder": None,
    "reduce_waste": "weekly_waste",
    "adjust_pricing": None,          # prices, comps OR discounts: no one number
    "push_sales": None,              # sales move for every reason (the post rule)
    "respond_reviews": "response_hours",
    "promote": None,                 # a post has no honest metric (see below)
    "investigate": None,
}


def metric_for_rec(restaurant_id, key, body_metric=None, db_path=DB_PATH):
    """(metric or None, authoritative) — the metric this recommendation
    CARRIES. A DSR action's kind is authoritative: "reorder" carries none
    and nothing may be substituted for it. Otherwise the metric the
    recommendation was presented with (rec_instances.expected_metric), else
    one the client sent, else None."""
    key = str(key or "")
    if key.startswith("dsr_action:"):
        parts = key.split(":")
        return DSR_ACTION_METRICS.get(parts[1] if len(parts) > 1 else ""), True
    rec = _latest_rec(restaurant_id, key, db_path)
    if rec is not None and rec["expected_metric"] and metrics.known(rec["expected_metric"]):
        return rec["expected_metric"], False
    if body_metric and metrics.known(body_metric):
        return body_metric, False
    return None, False


# An owner action the product can see — a schedule published, a supplier
# order sent — is an owner acting on what the product told them, whether or
# not they pressed Track. The retention audit put the empty value ledger on
# exactly that gap: the honest number was honest and small because it
# needed a button. observe() records the action as an outcome with source
# "observed", at most once per metric per calendar month, and never while
# another tracker on the same metric is already in flight (two trackers on
# labor % would double-count the same move).
# An alert the owner OPENED is an action too: they read it and went to
# look. What the metric did over the next window is the alert's own
# receipt — the moat audit's "outcome matching for alerts".
ALERT_METRICS = {
    "labor_over": ("labor_pct", "Read the labor-over-target alert"),
    "food_waste": ("weekly_waste", "Read the food-waste alert"),
    "rating_threshold": ("avg_rating", "Read the rating-below-threshold alert"),
    "negative_trend": ("avg_rating", "Read the rating-declining alert"),
    "price_spike": ("food_cost_pct", "Read the price-spike alert"),
}

OBSERVED_ACTIONS = {
    "schedule_published": ("labor_pct", "Published a schedule"),
    "supplier_order_sent": ("food_cost_pct", "Sent a supplier order from the draft"),
    # "post_published" used to be here, measured against SALES. Home says
    # in as many words that a post has no honest metric (home_brief's
    # add_rec: pointing "post more" at sales reads every unrelated thing
    # that moved sales as proof the post worked) — and a tracker observed
    # on every post published the same claim as delivered value. Removed;
    # observe() returns None for it, so its callers need no change.
}

# An alert being OPENED is reading, not acting. Its tracker still records
# what the metric did next — useful to see — but it is informational and
# never counts as value delivered (realised() and total_value skip it).
INFORMATIONAL_PREFIX = "observed:alert_"


def is_informational(r) -> bool:
    """A tracker that measures what followed a READ, not a change."""
    return str((r or {}).get("source_key") or "").startswith(INFORMATIONAL_PREFIX)


# ── one tracker per number ──────────────────────────────────────────────────

class TrackerRefused(Exception):
    """A tracker was not started because another is already measuring the
    same number (gate "metric") or a number in the same family (gate
    "family"). Carries the one in flight so the reply can name it."""

    def __init__(self, in_flight, metric):
        self.in_flight = in_flight
        self.metric = metric
        super().__init__(refusal_reason(in_flight, metric))

    def reply(self) -> dict:
        live = self.in_flight or {}
        return {"code": "in_flight", "reason": str(self),
                "in_flight_until": live.get("evaluate_on"),
                "in_flight": {"id": live.get("id"), "metric": live.get("metric"),
                              "label": live.get("metric_label"), "title": live.get("title")}}


def refusal_reason(live, metric) -> str:
    from time_utils import mdy
    live = live or {}
    until = mdy(live.get("evaluate_on")) if live.get("evaluate_on") else "its window closes"
    live_label = _lower_first(live.get("metric_label") or live.get("metric") or "that number")
    if live.get("metric") == metric or not metrics.known(metric):
        return (f"Already measuring {live_label} until {until}. One change per number at a time, "
                "so their before-and-after readings don't overlap.")
    new_label = _lower_first(metrics.describe(metric)["label"])
    return (f"Already measuring {live_label} until {until}. {new_label[:1].upper() + new_label[1:]} "
            f"moves with it, so a second reading over the same weeks would count the same change twice.")


def no_metric_reply() -> dict:
    return {"code": "no_metric", "reason": "There is nothing here Cavnar can measure it against yet",
            "in_flight_until": None}


def not_measurable_reply(metric, detail=None) -> dict:
    label = metrics.describe(metric)["label"] if metrics.known(metric) else str(metric)
    why = f" ({detail})" if detail else ""
    return {"code": "not_measurable", "in_flight_until": None,
            "reason": f"{label} can't be read right now{why}, so there is nothing to measure it against yet"}


def tracker_reply(row) -> dict:
    """What a client shows when a tracker starts: `label_text` is the owner's
    sentence, dates M/D/YY."""
    from time_utils import mdy
    label = row.get("metric_label") or row.get("metric")
    return {"id": row.get("id"), "metric": row.get("metric"), "label": label,
            "evaluate_on": row.get("evaluate_on"), "window_days": row.get("window_days"),
            "baseline_kind": row.get("baseline_kind"), "module": row.get("module"),
            "label_text": f"measuring {_lower_first(label)} until {mdy(row.get('evaluate_on'))}"}


def _live_on(conn, restaurant_id, metric, family=False, include_informational=False):
    rows = conn.execute("SELECT * FROM recommendation_outcomes WHERE restaurant_id=? AND status='tracking' "
                        "ORDER BY id", (restaurant_id,)).fetchall()
    fam = metrics.family(metric)
    for r in rows:
        if not include_informational and str(r["source_key"] or "").startswith(INFORMATIONAL_PREFIX):
            continue
        if (metrics.family(r["metric"]) == fam) if family else (r["metric"] == metric):
            return r
    return None


def in_flight_on(restaurant_id, metric, db_path=DB_PATH, family=False, include_informational=False):
    """The tracker currently measuring `metric` (or, with family=True, any
    metric in its family), or None. One at a time: two trackers in flight on
    one number read the same move twice. An alert-opened tracker is
    informational and never blocks a real change unless asked to."""
    conn = get_conn(db_path)
    try:
        row = _live_on(conn, restaurant_id, metric, family=family,
                       include_informational=include_informational)
    finally:
        conn.close()
    return _row(row) if row else None


def observe(restaurant_id, action, detail=None, user_id=None, db_path=DB_PATH, today=None):
    """Record an observed owner action as an outcome. Returns the tracker,
    or None when nothing was recorded (unknown action, a tracker already in
    flight on that metric, or this month's already observed)."""
    spec = OBSERVED_ACTIONS.get(action)
    if spec is not None and user_id is None:
        # "Owner acted" needs an owner. A supplier order the trusted-supplier
        # automation sent, or a schedule the scheduler auto-published, is
        # Cavnar acting — crediting it as the owner's change would put the
        # product's own work in the owner's value ledger.
        return None
    if spec is None and action.startswith("alert_"):
        spec = ALERT_METRICS.get(action[len("alert_"):])
    if not spec:
        return None
    metric, title = spec
    today = today or date.today()
    # An informational tracker (an alert opened) never counts, so it must
    # not block a real owner action on the same metric either; any tracker
    # blocks a second informational one.
    live = in_flight_on(restaurant_id, metric, db_path=db_path,
                        include_informational=action.startswith("alert_"))
    if live:
        return None
    key = f"observed:{action}:{today.strftime('%Y-%m')}"
    if detail:
        title = f"{title} — {str(detail)[:80]}"
    try:
        return record(restaurant_id, "observed", key, title, metric, user_id=user_id,
                      db_path=db_path, today=today)
    except TrackerRefused:
        return None


# ── baselines ───────────────────────────────────────────────────────────────

def _fmt(metric, v):
    if v is None:
        return "unknown"
    unit = metrics.describe(metric)["unit"] if metrics.known(metric) else ""
    return f"${v:,.0f}" if unit == "$" else f"{v:g}{unit}"


def _apply_shift(metric, raw, base_ly, win_ly):
    """This year's before-window reading moved the way the same weeks moved
    last year: a ratio for sales, points for a percentage."""
    base, _ = metrics.parse(metric)
    if base == "sales":
        if not base_ly:
            return None
        return round(raw * (win_ly / base_ly), 2)
    return round(raw + (win_ly - base_ly), 2)


def _seasonal_shift(restaurant_id, metric, base_window, win_window, db_path):
    """(last year's reading over the baseline's weeks, last year's reading
    over the comparison's weeks), or None when last year cannot honestly
    adjust anything (not enough of it measured)."""
    out = []
    for s, e in (base_window, win_window):
        ls, le = _day(s) - timedelta(days=LY_OFFSET_DAYS), _day(e) - timedelta(days=LY_OFFSET_DAYS)
        n = metrics.days_with_data(restaurant_id, metric, ls.isoformat(), le.isoformat(), db_path)
        span = (le - ls).days + 1
        if n is not None and n < LY_MIN_COVERAGE * span:
            return None
        v, _ = metrics.measure(restaurant_id, metric, ls.isoformat(), le.isoformat(), db_path)
        if v is None:
            return None
        out.append(v)
    return out[0], out[1]


def _baseline(restaurant_id, metric, today, window, db_path):
    """The reading the after-window is compared against (audit #30).

    prior window         the `window` days ending yesterday — every metric
                         a season does not move (ratings, waste, replies).
    matched weekdays     labor %, food cost %, sales: a window a whole
                         number of weeks before, so it holds the same
                         weekdays as the after-window (for a 28-day window
                         that IS the prior window).
    same weeks last year the matched window, moved by what the same weeks
                         did last year — when a year of history covers both.
    """
    base, _ = metrics.parse(metric)
    seasonal = base in metrics.SEASONAL_METRICS
    if seasonal:
        weeks = -(-int(window) // 7)
        b_start = today - timedelta(days=weeks * 7)
        b_end = b_start + timedelta(days=window - 1)
        kind = "matched weekdays"
    else:
        b_end = today - timedelta(days=1)
        b_start = b_end - timedelta(days=window - 1)
        kind = "prior window"
    raw, detail = metrics.measure(restaurant_id, metric, b_start.isoformat(), b_end.isoformat(), db_path)
    value = raw
    if raw is not None and seasonal:
        a_end = today + timedelta(days=window - 1)
        shift = _seasonal_shift(restaurant_id, metric, (b_start, b_end), (today, a_end), db_path)
        adjusted = _apply_shift(metric, raw, *shift) if shift else None
        if adjusted is not None:
            value, kind = adjusted, "same weeks last year"
            detail = (f"{detail}; {_fmt(metric, raw)} before, moved as the same weeks moved last year "
                      f"({_fmt(metric, shift[0])} to {_fmt(metric, shift[1])})")
    return {"value": value, "raw": raw, "detail": detail, "start": b_start.isoformat(),
            "end": b_end.isoformat(), "kind": kind}


def expected_for(r, start, end, db_path=DB_PATH):
    """(expected value, band scale, baseline kind) to compare a reading over
    [start, end] against. The evaluation window uses the stored baseline;
    any other window of a "same weeks last year" tracker (the re-check, a
    daily accrual read) is re-adjusted for ITS weeks, and falls back to the
    unadjusted matched-weekday reading when last year cannot cover them."""
    kind = r.get("baseline_kind") or "prior window"
    if kind != "same weeks last year":
        return r.get("baseline_value"), 1.0, kind
    if _iso(start) == _iso(r.get("started_on")) and _iso(end) == _after_end(r):
        return r.get("baseline_value"), SEASONAL_BAND_SCALE, kind
    raw = r.get("baseline_raw")
    if raw is None:
        return r.get("baseline_value"), SEASONAL_BAND_SCALE, kind
    shift = _seasonal_shift(r["restaurant_id"], r["metric"], (r["baseline_start"], r["baseline_end"]),
                            (start, end), db_path)
    adjusted = _apply_shift(r["metric"], raw, *shift) if shift else None
    if adjusted is None:
        return raw, 1.0, "matched weekdays"
    return adjusted, SEASONAL_BAND_SCALE, kind


# ── starting a tracker ──────────────────────────────────────────────────────

def record(restaurant_id, source, source_key, title, metric, user_id=None,
           window_days=None, db_path=DB_PATH, today=None, module=None, gate="metric"):
    """Start tracking one recommendation the owner has committed to.

    Idempotent on (restaurant, source_key) while tracking — committing to the
    same fix twice returns the existing tracker rather than resetting its
    baseline, which would quietly erase the improvement already made.

    `gate` is the one-tracker-per-number rule (audit #3): "metric" refuses
    while another tracker measures the same metric, "family" while one
    measures anything in its family (the automatic starts, #18), None
    skips it. A refusal raises TrackerRefused; start() turns it into a
    reply. `module` is who the recommendation was (resolve_module when None).
    """
    if not metrics.known(metric):
        raise ValueError(f"unknown metric {metric}")
    today = today or date.today()
    info = metrics.describe(metric)
    window = int(window_days or info["default_window_days"])

    conn = get_conn(db_path)
    try:
        existing = conn.execute(
            "SELECT * FROM recommendation_outcomes WHERE restaurant_id=? AND source_key=? "
            "AND status='tracking'", (restaurant_id, source_key)).fetchone()
        if existing:
            return _row(existing)
        if gate:
            live = _live_on(conn, restaurant_id, metric, family=(gate == "family"),
                            include_informational=str(source_key or "").startswith(INFORMATIONAL_PREFIX))
            if live is not None:
                raise TrackerRefused(_row(live), metric)
    finally:
        conn.close()

    # The baseline ends before today: today is part of the "after", and a
    # baseline that includes the day the change started is contaminated by it.
    b = _baseline(restaurant_id, metric, today, window, db_path)
    evaluate_on = today + timedelta(days=window)
    mod = resolve_module(restaurant_id, source, source_key, metric, module=module, db_path=db_path)

    conn = get_conn(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO recommendation_outcomes (restaurant_id, source, source_key, title, metric, "
            "baseline_value, baseline_raw, baseline_kind, baseline_start, baseline_end, baseline_detail, "
            "started_on, evaluate_on, status, created_by, module) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, 'tracking', ?, ?)",
            (restaurant_id, source, source_key, owner_title(title)[:200], metric, b["value"], b["raw"],
             b["kind"], b["start"], b["end"], b["detail"], today.isoformat(), evaluate_on.isoformat(),
             user_id, mod))
        conn.commit()
        row = conn.execute("SELECT * FROM recommendation_outcomes WHERE id=?",
                           (cur.lastrowid,)).fetchone()
    finally:
        conn.close()
    # The recommendation and the tracker measuring it, linked for good at the
    # moment it starts, whichever door it came through (rec-ROI #36). A
    # source_key that is not a recommendation's key links nothing.
    try:
        import rec_ledger
        rec_ledger.link_tracker(restaurant_id, source_key, row["id"], db_path=db_path)
    except Exception as e:
        print(f"[outcomes] tracker {row['id']} not linked to its recommendation: {e}")
    return _row(row)


def start(restaurant_id, source, source_key, title, metric, user_id=None, window_days=None,
          module=None, gate="metric", db_path=DB_PATH, today=None) -> dict:
    """record() for a caller that answers an owner: {"ok": True, "outcome",
    "tracker"} or {"ok": False, "tracker_refused"}. An unknown metric still
    raises ValueError — that is a bad request, not a refusal."""
    try:
        row = record(restaurant_id, source, source_key, title, metric, user_id=user_id,
                     window_days=window_days, db_path=db_path, today=today, module=module, gate=gate)
    except TrackerRefused as e:
        return {"ok": False, "tracker_refused": e.reply()}
    return {"ok": True, "outcome": row, "tracker": tracker_reply(row)}


# ── other changes in the same weeks (audit #31) ─────────────────────────────
# Which kinds of other change can move which family. Owner events, holidays
# and closures move trading volume; a price change moves food cost, sales
# and (through the sales denominator) labor %. Other trackers and accepted
# recommendations count when they are on the same family.
_VOLUME_FAMILIES = {"labor_cost", "food_cost", "sales", "comps", "voids"}
_PRICE_FAMILIES = {"labor_cost", "food_cost", "sales"}
_REC_MODULE_FAMILY = {"labor": "labor_cost", "schedule": "labor_cost", "food": "food_cost",
                      "reviews": "guest_rating", "marketing": "sales", "guests": "sales"}


def _rec_family(key, module, expected_metric):
    key = str(key or "")
    if key.startswith("dsr_action:"):
        m = DSR_ACTION_METRICS.get(key.split(":")[1] if ":" in key else "")
        return metrics.family(m) if m else None
    if expected_metric and metrics.known(expected_metric):
        return metrics.family(expected_metric)
    return _REC_MODULE_FAMILY.get(module or "")


def _holidays_between(s, e):
    try:
        from schedule_economics import _holiday_dates
    except Exception as ex:
        print(f"[outcomes] holiday list unavailable: {ex}")
        return {}
    names = {}
    for y in range(_day(s).year, _day(e).year + 1):
        names.update(_holiday_dates(y))
    return {d: n for d, n in names.items() if _iso(s) <= d <= _iso(e)}


def find_concurrent(r, start, end, db_path=DB_PATH, read_windows=None):
    """Every other change that could move this tracker's number:
    [{kind, label, date}] with kind one of tracker | accepted_rec |
    price_change | event | holiday | closure. Any one of them caps the
    attribution at "associated" (grade).

    Lasting changes (another tracker, an accepted recommendation, a price)
    count anywhere in [start, end]. One-day ones (an event, a holiday, a
    closure) count only inside the windows actually read — `read_windows`,
    default [(start, end)] — since a party between the evaluation and the
    re-check moved neither reading."""
    fam = metrics.family(r["metric"])
    rid = r["restaurant_id"]
    s, e = _iso(start), _iso(end)
    windows = [(_iso(a), _iso(b)) for a, b in (read_windows or [(s, e)])]

    def _read(d):
        return any(a <= d <= b for a, b in windows)
    out = []
    conn = get_conn(db_path)
    try:
        tracker_keys = set()
        for o in conn.execute(
                "SELECT id, source_key, title, metric, started_on, evaluate_on, after_end FROM "
                "recommendation_outcomes WHERE restaurant_id=? AND id!=? AND status IN ('tracking','evaluated') "
                "AND started_on<=?", (rid, r.get("id") or 0, e)).fetchall():
            if str(o["source_key"] or "").startswith(INFORMATIONAL_PREFIX):
                continue        # reading an alert is not a change
            if metrics.family(o["metric"]) != fam:
                continue
            o_end = _iso(o["after_end"]) or (_day(o["evaluate_on"]) - timedelta(days=1)).isoformat()
            if o_end < s:
                continue
            tracker_keys.add(o["source_key"])
            out.append({"kind": "tracker", "label": owner_title(o["title"]), "date": _iso(o["started_on"])})
        try:
            events = conn.execute(
                "SELECT e.key, e.at, i.module, i.expected_metric, i.title FROM rec_events e "
                "LEFT JOIN rec_instances i ON i.rec_id = e.rec_id WHERE e.restaurant_id=? "
                "AND e.event IN ('accepted','completed') AND date(e.at)>=? AND date(e.at)<=? "
                "ORDER BY e.at", (rid, s, e)).fetchall()
        except Exception as ex:
            print(f"[outcomes] rec events unreadable for {rid}: {ex}")
            events = []
        seen = set()
        for ev in events:
            key = ev["key"]
            if key == r.get("source_key") or key in tracker_keys or key in seen:
                continue
            if _rec_family(key, ev["module"], ev["expected_metric"]) != fam:
                continue
            seen.add(key)
            out.append({"kind": "accepted_rec", "label": owner_title(ev["title"] or key),
                        "date": _iso(ev["at"])})
        # The reprice tracker IS the price change; every other tracker on a
        # price-moved number sees the prices that moved under it.
        if fam in _PRICE_FAMILIES and r.get("source") != "reprice":
            try:
                for p in conn.execute("SELECT dish, created_at FROM reprice_decisions WHERE restaurant_id=? "
                                      "AND date(created_at)>=? AND date(created_at)<=? ORDER BY created_at",
                                      (rid, s, e)).fetchall():
                    out.append({"kind": "price_change", "label": f"{p['dish'] or 'A dish'} repriced",
                                "date": _iso(p["created_at"])})
            except Exception as ex:
                print(f"[outcomes] price changes unreadable for {rid}: {ex}")
        if fam in _VOLUME_FAMILIES:
            try:
                for ev in conn.execute("SELECT date, label FROM demand_signals WHERE restaurant_id=? "
                                       "AND kind='event' AND date>=? AND date<=? ORDER BY date",
                                       (rid, s, e)).fetchall():
                    if _read(_iso(ev["date"])):
                        out.append({"kind": "event", "label": ev["label"], "date": _iso(ev["date"])})
            except Exception as ex:
                print(f"[outcomes] events unreadable for {rid}: {ex}")
    finally:
        conn.close()
    if fam in _VOLUME_FAMILIES:
        # A holiday is a confounder only when the comparison did not already
        # hold one: a "same weeks last year" baseline has the same holidays
        # in it, and a read window with no more holidays than the baseline
        # window is balanced.
        if (r.get("baseline_kind") or "prior window") != "same weeks last year":
            before = (_holidays_between(r["baseline_start"], r["baseline_end"])
                      if r.get("baseline_start") and r.get("baseline_end") else {})
            for a, b in windows:
                inside = _holidays_between(a, b)
                if len(inside) > len(before):
                    out.extend({"kind": "holiday", "label": n, "date": d} for d, n in sorted(inside.items()))
        try:
            import schedule_rules
            rest = _models_mod.get_restaurant(rid, db_path)
            if rest is not None:
                for d in schedule_rules.closures(rest)["closed_dates"]:
                    if _read(d):
                        out.append({"kind": "closure", "label": "Closed", "date": d})
        except Exception as ex:
            print(f"[outcomes] closures unreadable for {rid}: {ex}")
    seen, unique = set(), []
    for c in sorted(out, key=lambda c: (c["date"], c["kind"], c["label"] or "")):
        k = (c["kind"], c["date"], c["label"])
        if k not in seen:
            seen.add(k)
            unique.append(c)
    return unique


# ── attribution: how strongly a move can be tied to the change (#23) ────────

def grade(verdict, multiple, concurrent, recheck_verdict=None, checked=True) -> str:
    """none | associated | consistent | held. Never a claim of cause.

    none        no clear change, or nothing could be measured.
    associated  past the noise band once — or any size of move with another
                change on the same number in the window (a concurrent change
                caps it here), or a window never checked for one.
    consistent  at least CONSISTENT_MULTIPLE bands, nothing else changing on
                the same number in the window.
    held        still past the band when re-checked, nothing else changing.
    """
    if verdict not in _MOVED:
        return "none"
    if concurrent or not checked:
        return "associated"
    if recheck_verdict == "held":
        return "held"
    if multiple is not None and multiple >= CONSISTENT_MULTIPLE:
        return "consistent"
    return "associated"


def attribution_label(r) -> str:
    """The owner's sentence for a result's attribution. Every level says
    what was measured and never that the change caused it."""
    from time_utils import mdy
    if r.get("status") != "evaluated":
        return None
    v = r.get("verdict")
    if v in (None, "unknown"):
        return "Couldn't be measured before and after."
    if v == "no_clear_change":
        return "No clear change: within this number's normal week-to-week movement."
    moved = "Improved" if v == "improved" else "Got worse"
    ck = _checkin_of(r) or {}
    if ck.get("did_it") == "no":
        return (f"{moved} over these weeks, but you said this change wasn't made, so the result isn't "
                f"credited to it.")
    a = r.get("attribution")
    conc = [c for c in (r.get("concurrent") or []) if isinstance(c, dict)]
    rechecked = mdy(r.get("rechecked_at") or r.get("recheck_on")) if r.get("recheck_verdict") else None
    if a == "held":
        s = (f"{moved} and held: still past normal variation when re-checked on {rechecked}, with no "
             f"other change on this number in the same weeks. Measured, not proven cause.")
    elif a == "consistent":
        s = (f"{moved} clearly: more than twice this number's normal variation, with no other change "
             f"on it in the same weeks. Measured, not proven cause.")
    elif ck.get("conditions_changed"):
        s = (f"{moved} alongside the change, but you said something else changed in the same weeks, so it "
             f"can't be separated from that. {CAUSATION_CAVEAT}")
    elif conc:
        names = ", ".join(c.get("label") or c.get("kind") for c in conc[:2])
        more = f" and {len(conc) - 2} more" if len(conc) > 2 else ""
        s = (f"{moved} alongside the change, but other changes on this number fell in the same weeks "
             f"({names}{more}), so it can't be separated from them. {CAUSATION_CAVEAT}")
    else:
        s = f"{moved} alongside the change, past normal variation. {CAUSATION_CAVEAT}"
    rv = r.get("recheck_verdict")
    gone = "no longer counts" if v == "improved" else "is no longer subtracted"
    if rv == "faded":
        s += f" It faded: no longer past normal variation when re-checked on {rechecked}, so it {gone}."
    elif rv == "reversed":
        s += f" It reversed by the re-check on {rechecked}, so it {gone}."
    elif rv == "unknown":
        s += f" The re-check on {rechecked} couldn't be measured."
    return s


def is_validated(r) -> bool:
    """A measured win whose re-check held with nothing else changing on its
    family (audit #34)."""
    return (r.get("status") == "evaluated" and r.get("verdict") == "improved"
            and r.get("attribution") == "held" and not is_informational(r))


def counts_in_delivered(r) -> bool:
    """An evaluated move that still counts: a change the owner made (not an
    alert read) that has not faded or reversed at its re-check (#33), and
    that the owner has not said they never made (#21)."""
    return (r.get("status") == "evaluated" and r.get("verdict") in _MOVED
            and not is_informational(r) and r.get("recheck_verdict") not in _FAILED_RECHECK
            and not disowned(r))


def _checkin_of(r):
    raw = r.get("owner_checkin")
    if isinstance(raw, dict):
        return raw
    try:
        v = json.loads(raw) if raw else None
        return v if isinstance(v, dict) else None
    except (TypeError, ValueError):
        return None


def disowned(r) -> bool:
    """The owner answered the check-in "No, I didn't make this change": the
    number may have moved, but not because of this recommendation, so it is
    neither a win nor a loss of Cavnar's."""
    c = _checkin_of(r)
    return bool(c and c.get("did_it") == "no")


# ── evaluating ──────────────────────────────────────────────────────────────

def recheck_on_for(r):
    """started_on + RECHECK_DAYS, never inside the after-window's own span:
    the re-check reads the window ending the day before it."""
    started, ev = _day(r["started_on"]), _day(r["evaluate_on"])
    return max(started + timedelta(days=RECHECK_DAYS), ev + timedelta(days=_window_days(r))).isoformat()


def evaluate(outcome_id, db_path=DB_PATH, today=None):
    """Re-measure one tracker whose window has closed and store the verdict,
    the after-value and % change, the other changes in the window, the
    attribution grade and the re-check date; then accrue the window's
    measured dollars (outcome_value_days)."""
    today = today or date.today()
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT * FROM recommendation_outcomes WHERE id=?", (outcome_id,)).fetchone()
    finally:
        conn.close()
    if not r or r["status"] != "tracking":
        return None
    r = dict(r)
    started = _day(r["started_on"])
    ev = _day(r["evaluate_on"])
    if today < ev:
        return None
    after_start, after_end = started, ev - timedelta(days=1)
    after, after_detail = metrics.measure(r["restaurant_id"], r["metric"], after_start.isoformat(),
                                          after_end.isoformat(), db_path)
    expected, scale, _kind = expected_for(r, after_start.isoformat(), after_end.isoformat(), db_path)
    cmp = metrics.compare(r["metric"], expected, after, band_scale=scale)
    dollars = (metrics.monthly_dollars(r["restaurant_id"], r["metric"], cmp["delta"], db_path)
               if cmp["verdict"] in _MOVED else None)
    concurrent = find_concurrent(r, after_start.isoformat(), after_end.isoformat(), db_path)
    attribution = grade(cmp["verdict"], cmp["multiple"], concurrent)
    recheck_on = recheck_on_for(r) if (cmp["verdict"] in _MOVED and not is_informational(r)) else None
    conn = get_conn(db_path)
    try:
        conn.execute(
            "UPDATE recommendation_outcomes SET after_value=?, after_start=?, after_end=?, after_detail=?, "
            "verdict=?, delta=?, delta_pct=?, dollars_monthly=?, attribution=?, concurrent=?, recheck_on=?, "
            "status='evaluated' WHERE id=? AND status='tracking'",
            (after, after_start.isoformat(), after_end.isoformat(), after_detail, cmp["verdict"],
             cmp["delta"], cmp["delta_pct"], dollars, attribution, json.dumps(concurrent), recheck_on,
             outcome_id))
        conn.commit()
        row = conn.execute("SELECT * FROM recommendation_outcomes WHERE id=?", (outcome_id,)).fetchone()
    finally:
        conn.close()
    accrue_window(_row(row), db_path=db_path)
    return get_outcome(outcome_id, db_path=db_path)


def evaluate_due(restaurant_id=None, db_path=DB_PATH, today=None):
    """Evaluate every tracker whose window has closed. Scheduler entry point."""
    today = today or date.today()
    conn = get_conn(db_path)
    try:
        sql = ("SELECT id FROM recommendation_outcomes WHERE status='tracking' AND evaluate_on<=?")
        args = [today.isoformat()]
        if restaurant_id is not None:
            sql += " AND restaurant_id=?"
            args.append(restaurant_id)
        ids = [r["id"] for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()
    done = []
    for oid in ids:
        try:
            out = evaluate(oid, db_path=db_path, today=today)
            if out:
                done.append(out)
        except Exception as e:
            try:
                import ops
                ops.capture(e, job="evaluate_outcomes", context=f"outcome_id={oid}")
            except Exception:
                pass
    return done


# ── the re-check (#33) ──────────────────────────────────────────────────────

def recheck(outcome_id, db_path=DB_PATH, today=None):
    """Read a measured move again over the window ending the day before
    recheck_on, against the same baseline. held: still past the band the
    same way; faded: back inside it; reversed: past it the other way;
    unknown: not measurable. A win that faded or reversed stops counting in
    Delivered and stops accruing. The window's other changes are checked
    again over the whole span, for rows evaluated before they were recorded."""
    today = today or date.today()
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT * FROM recommendation_outcomes WHERE id=?", (outcome_id,)).fetchone()
    finally:
        conn.close()
    if not r:
        return None
    r = dict(r)
    if (r["status"] != "evaluated" or r["verdict"] not in _MOVED or r.get("recheck_verdict")
            or not r.get("recheck_on") or today < _day(r["recheck_on"]) or is_informational(r)):
        return None
    window = _window_days(r)
    end = _day(r["recheck_on"]) - timedelta(days=1)
    start = end - timedelta(days=window - 1)
    value, _detail = metrics.measure(r["restaurant_id"], r["metric"], start.isoformat(), end.isoformat(),
                                     db_path)
    expected, scale, _kind = expected_for(r, start.isoformat(), end.isoformat(), db_path)
    cmp = metrics.compare(r["metric"], expected, value, band_scale=scale)
    if cmp["verdict"] == "unknown":
        rv = "unknown"
    elif cmp["verdict"] == r["verdict"]:
        rv = "held"
    elif cmp["verdict"] == "no_clear_change":
        rv = "faded"
    else:
        rv = "reversed"
    # Everything that changed on this number from the start to the re-check:
    # lasting changes anywhere in the span, one-day ones inside the two
    # windows that were read.
    concurrent = find_concurrent(r, r["started_on"], end.isoformat(), db_path,
                                 read_windows=[(r["started_on"], _after_end(r)),
                                               (start.isoformat(), end.isoformat())])
    first, _s, _k = expected_for(r, r["started_on"], _after_end(r), db_path)
    eval_cmp = metrics.compare(r["metric"], first, r.get("after_value"), band_scale=_s)
    attribution = grade(r["verdict"], eval_cmp["multiple"], concurrent, recheck_verdict=rv)
    conn = get_conn(db_path)
    try:
        conn.execute("UPDATE recommendation_outcomes SET recheck_value=?, recheck_verdict=?, rechecked_at=?, "
                     "attribution=?, concurrent=? WHERE id=? AND recheck_verdict IS NULL",
                     (value, rv, today.isoformat(), attribution, json.dumps(concurrent), outcome_id))
        conn.commit()
        row = conn.execute("SELECT * FROM recommendation_outcomes WHERE id=?", (outcome_id,)).fetchone()
    finally:
        conn.close()
    return _row(row)


def recheck_due(restaurant_id=None, db_path=DB_PATH, today=None):
    """Re-check every measured move whose recheck_on has come. Daily job."""
    today = today or date.today()
    conn = get_conn(db_path)
    try:
        sql = ("SELECT id FROM recommendation_outcomes WHERE status='evaluated' AND verdict IN "
               "('improved','worsened') AND recheck_verdict IS NULL AND recheck_on IS NOT NULL "
               "AND recheck_on<=?")
        args = [today.isoformat()]
        if restaurant_id is not None:
            sql += " AND restaurant_id=?"
            args.append(restaurant_id)
        ids = [r["id"] for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()
    done = []
    for oid in ids:
        try:
            out = recheck(oid, db_path=db_path, today=today)
            if out:
                done.append(out)
        except Exception as e:
            import ops
            ops.capture(e, job="outcome_recheck", context=f"outcome_id={oid}")
    return done


# ── cumulative measured value (#14) ─────────────────────────────────────────
# outcome_value_days holds one row per tracker per measured day. `dollars`
# is signed (a win's day positive, a worsened change's day negative) and 0
# on a measured day the move did not hold. Only ONE row per restaurant,
# family, direction and day is `counted`: waste and food cost improving over
# the same days are one saving, not two (#4), so the larger stands.

def _unit_amount(r, monthly):
    """A monthly figure as one measured day's share. A weekday metric's day
    is one of that weekday — its monthly figure recurs WEEKS_PER_MONTH times."""
    base, _ = metrics.parse(r["metric"])
    per = metrics.WEEKS_PER_MONTH if base == "weekday_sales" else metrics.DAYS_PER_MONTH
    return round(float(monthly) / per, 4)


def _accrues(r) -> bool:
    return (r.get("status") == "evaluated" and r.get("verdict") in _MOVED
            and r.get("dollars_monthly") not in (None, 0) and not is_informational(r)
            and not disowned(r))


def _put_day(conn, r, day, amount, held, basis):
    """One measured day for one tracker, counted only if it is the largest
    held reading of its family and direction that day."""
    if conn.execute("SELECT 1 FROM outcome_value_days WHERE outcome_id=? AND day=?",
                    (r["id"], day)).fetchone():
        return False
    fam = metrics.family(r["metric"])
    sign = 1 if r["verdict"] == "improved" else -1
    counted = 0
    if held and amount:
        cur = conn.execute("SELECT outcome_id, dollars FROM outcome_value_days WHERE restaurant_id=? AND "
                           "family=? AND sign=? AND day=? AND counted=1",
                           (r["restaurant_id"], fam, sign, day)).fetchone()
        if cur is None:
            counted = 1
        elif abs(float(amount)) > abs(float(cur["dollars"] or 0)):
            conn.execute("UPDATE outcome_value_days SET counted=0 WHERE outcome_id=? AND day=?",
                         (cur["outcome_id"], day))
            counted = 1
    conn.execute("INSERT INTO outcome_value_days (outcome_id, restaurant_id, day, module, metric, family, "
                 "sign, dollars, held, counted, basis) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (r["id"], r["restaurant_id"], day, module_of_row(r), r["metric"], fam, sign,
                  round(float(amount or 0), 4) if held else 0.0, 1 if held else 0, counted, basis))
    return True


def accrue_window(r, db_path=DB_PATH):
    """Accrue an evaluated move's after-window: each measured day of it at
    the evaluated monthly figure's daily share. A per-day metric's days
    without data were not measured and accrue nothing."""
    if not r or r.get("status") != "evaluated":
        return 0
    after_end = _after_end(r)
    written = 0
    conn = get_conn(db_path)
    try:
        if _accrues(r):
            days = metrics.data_days(r["restaurant_id"], r["metric"], r["started_on"], after_end, db_path)
            if days is None:
                s = _day(r["started_on"])
                days = [(s + timedelta(days=i)).isoformat() for i in range((_day(after_end) - s).days + 1)]
            amount = _unit_amount(r, r["dollars_monthly"])
            for d in days:
                written += 1 if _put_day(conn, r, d, amount, True, "window") else 0
        conn.execute("UPDATE recommendation_outcomes SET accrued_through=? WHERE id=? "
                     "AND (accrued_through IS NULL OR accrued_through<?)", (after_end, r["id"], after_end))
        conn.commit()
    finally:
        conn.close()
    return written


def _read_day(r, d, db_path):
    """(held, amount) for one day after the window, from the trailing window
    ending that day, or None when the day was not measured."""
    base, param = metrics.parse(r["metric"])
    day = d.isoformat()
    if base in metrics.PER_DAY_METRICS and not metrics.data_days(r["restaurant_id"], r["metric"], day, day,
                                                                 db_path):
        return None
    start = (d - timedelta(days=_window_days(r) - 1)).isoformat()
    expected, scale, _k = expected_for(r, start, day, db_path)
    value, _ = metrics.measure(r["restaurant_id"], r["metric"], start, day, db_path)
    cmp = metrics.compare(r["metric"], expected, value, band_scale=scale)
    if cmp["verdict"] == "unknown":
        return None
    if cmp["verdict"] != r["verdict"]:
        return (False, 0.0)
    monthly = metrics.monthly_dollars(r["restaurant_id"], r["metric"], cmp["delta"], db_path)
    if monthly is None:
        return None
    # Never more than the evaluated figure: a number that kept moving after
    # the window is more likely something else than more of the change.
    capped = min(abs(float(monthly)), abs(float(r["dollars_monthly"])))
    return (True, _unit_amount(r, capped if r["verdict"] == "improved" else -capped))


def accrue_daily(r, db_path=DB_PATH, today=None):
    """Accrue the days after the window, one measured day at a time, while
    the move still holds that day — up to ACCRUAL_HORIZON_DAYS from the
    start, and never past a failed re-check. Returns rows written."""
    today = today or date.today()
    if not _accrues(r) or not r.get("accrued_through"):
        return 0
    base, param = metrics.parse(r["metric"])
    stop = min(today - timedelta(days=1),
               _day(r["started_on"]) + timedelta(days=ACCRUAL_HORIZON_DAYS - 1))
    if r.get("recheck_verdict") in _FAILED_RECHECK and r.get("recheck_on"):
        stop = min(stop, _day(r["recheck_on"]) - timedelta(days=1))
    d = _day(r["accrued_through"]) + timedelta(days=1)
    through, written, n = None, 0, 0
    conn = get_conn(db_path)
    try:
        while d <= stop and n < ACCRUAL_MAX_DAYS_PER_PASS:
            n += 1
            if base == "weekday_sales" and d.strftime("%A") != (param or "").strip().capitalize():
                through = d
                d += timedelta(days=1)
                continue
            got = _read_day(r, d, db_path)
            if got is None:
                if d >= today - timedelta(days=ACCRUAL_GRACE_DAYS):
                    break           # the day's data may still land; read it tomorrow
            else:
                held, amount = got
                written += 1 if _put_day(conn, r, d.isoformat(), amount, held, "daily") else 0
            through = d
            d += timedelta(days=1)
        if through is not None:
            conn.execute("UPDATE recommendation_outcomes SET accrued_through=? WHERE id=?",
                         (through.isoformat(), r["id"]))
        conn.commit()
    finally:
        conn.close()
    return written


def accrue_due(restaurant_id, db_path=DB_PATH, today=None):
    """One accrual pass for one restaurant: any evaluated move whose window
    was never accrued (evaluated before this existed) is accrued first, then
    every move is read forward day by day. Daily job."""
    today = today or date.today()
    written = 0
    for r in list_outcomes(restaurant_id, status="evaluated", limit=500, db_path=db_path):
        if not _accrues(r):
            continue
        try:
            if not r.get("accrued_through"):
                written += accrue_window(r, db_path=db_path)
                r = get_outcome(r["id"], db_path=db_path)
            written += accrue_daily(r, db_path=db_path, today=today)
        except Exception as e:
            import ops
            ops.capture(e, job="outcome_accrual", context=f"outcome_id={r.get('id')}")
    return written


def _denied_row(r, denied) -> bool:
    return bool(denied) and (module_of_row(r) in denied or module_of(r.get("metric")) in denied)


def cumulative(restaurant_id, db_path=DB_PATH, denied_modules=None, since=None, exclude_metrics=None) -> dict:
    """Measured dollars accrued so far, net of changes that got worse: a SUM
    of measured days, never a monthly figure times months. `total` is None
    when no day has been measured yet (nothing measured is not $0).
    `days` counts days on which a counted move held; `measured_days` every
    day read. `since` (ISO date) keeps only the days on or after it — the
    owner report's "over the past 6 months" (owner_report.what_worked);
    `exclude_metrics` drops metrics a viewer may not read (comps and voids
    without LOSS_VIEW) before anything is summed."""
    denied = set(denied_modules or ())
    excluded = {str(m).split(":", 1)[0] for m in (exclude_metrics or ())}
    sql = "SELECT day, module, metric, dollars, held, counted FROM outcome_value_days WHERE restaurant_id=?"
    args = [restaurant_id]
    if since:
        sql += " AND day >= ?"
        args.append(str(since)[:10])
    conn = get_conn(db_path)
    try:
        rows = conn.execute(sql + " ORDER BY day", args).fetchall()
    finally:
        conn.close()
    rows = [dict(r) for r in rows if not _denied_row(dict(r), denied)
            and str(r["metric"] or "").split(":", 1)[0] not in excluded]
    counted = [r for r in rows if r["counted"]]
    by_module = {}
    for r in counted:
        by_module[r["module"] or "other"] = round(by_module.get(r["module"] or "other", 0.0) + r["dollars"], 2)
    gained = sum(r["dollars"] for r in counted if r["dollars"] > 0)
    lost = sum(-r["dollars"] for r in counted if r["dollars"] < 0)
    return {
        "total": round(gained - lost, 2) if rows else None,
        "gained": round(gained, 2), "lost": round(lost, 2),
        "since": counted[0]["day"] if counted else (rows[0]["day"] if rows else None),
        "until": counted[-1]["day"] if counted else (rows[-1]["day"] if rows else None),
        "days": len({r["day"] for r in counted}),
        "measured_days": len({r["day"] for r in rows}),
        "by_module": by_module,
        "basis": ("summed over days actually measured, only while each change held, net of changes that got "
                  f"worse; related numbers over the same days count once; each change for up to "
                  f"{ACCRUAL_HORIZON_DAYS} days from when it started"),
    }


def apply_checkin(restaurant_id, tracker_id, did_it, conditions_changed, db_path=DB_PATH, at=None):
    """The owner's check-in on a tracked change (rec-ROI #21), applied to its
    result. The latest answer wins and can be changed back.

    - "no" (the change was not made): the result is neither a win nor a loss
      of this recommendation's — it stops counting in Delivered and its
      measured days are released, so another change on the same family that
      day is counted instead (the one-per-family rule, #4).
    - conditions_changed: the grade is capped at "associated", like any other
      change in the same weeks (#31).
    - "yes"/"partly" after a "no": the days are re-accrued by the next pass
      and the grade before the check-in is restored (still capped when
      conditions changed).
    Returns the updated row, or None when the tracker is not this
    restaurant's."""
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM recommendation_outcomes WHERE id=? AND restaurant_id=?",
                           (tracker_id, restaurant_id)).fetchone()
        if not row:
            return None
        r = dict(row)
        before = _checkin_of(r) or {}
        was_disowned = before.get("did_it") == "no"
        base_grade = before.get("attribution_before", r.get("attribution"))
        ck = {"did_it": did_it, "conditions_changed": bool(conditions_changed),
              "at": at or datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), "attribution_before": base_grade}
        grade_now = r.get("attribution")
        if r.get("status") == "evaluated" and r.get("verdict") in _MOVED:
            grade_now = "associated" if (conditions_changed and base_grade in ("consistent", "held")) else base_grade
        conn.execute("UPDATE recommendation_outcomes SET owner_checkin=?, attribution=? WHERE id=?",
                     (json.dumps(ck), grade_now, r["id"]))
        if did_it == "no" and not was_disowned:
            _release_days(conn, r)
        elif did_it != "no" and was_disowned:
            # Re-accrued from the start by the next accrual pass.
            conn.execute("DELETE FROM outcome_value_days WHERE outcome_id=?", (r["id"],))
            conn.execute("UPDATE recommendation_outcomes SET accrued_through=NULL WHERE id=?", (r["id"],))
        conn.commit()
    finally:
        conn.close()
    return get_outcome(tracker_id, db_path=db_path)


def _release_days(conn, r):
    """Stop counting a disowned change's measured days, and on each day it was
    the counted row of its family and direction, count the next-largest
    held reading instead."""
    days = conn.execute("SELECT day, family, sign FROM outcome_value_days WHERE outcome_id=? AND counted=1",
                        (r["id"],)).fetchall()
    conn.execute("UPDATE outcome_value_days SET counted=0 WHERE outcome_id=?", (r["id"],))
    for d in days:
        nxt = conn.execute(
            "SELECT outcome_id FROM outcome_value_days v JOIN recommendation_outcomes o ON o.id=v.outcome_id "
            "WHERE v.restaurant_id=? AND v.family=? AND v.sign=? AND v.day=? AND v.held=1 AND v.outcome_id<>? "
            "AND (o.owner_checkin IS NULL OR o.owner_checkin NOT LIKE '%\"did_it\": \"no\"%') "
            "ORDER BY ABS(v.dollars) DESC LIMIT 1",
            (r["restaurant_id"], d["family"], d["sign"], d["day"], r["id"])).fetchone()
        if nxt:
            conn.execute("UPDATE outcome_value_days SET counted=1 WHERE outcome_id=? AND day=?",
                         (nxt["outcome_id"], d["day"]))


# ── reading ─────────────────────────────────────────────────────────────────

def progress(restaurant_id, db_path=DB_PATH, today=None):
    """Interim reading for trackers still running: the metric since the start
    date, against the baseline. Labelled as partial by callers — a window that
    is a week old is a hint, not a result."""
    today = today or date.today()
    out = []
    for r in list_outcomes(restaurant_id, status="tracking", db_path=db_path):
        started = _day(r["started_on"])
        end = today - timedelta(days=1)
        if end < started:
            r["interim"] = None
        else:
            value, _ = metrics.measure(restaurant_id, r["metric"], started.isoformat(),
                                       end.isoformat(), db_path)
            expected, scale, _k = expected_for(r, started.isoformat(), end.isoformat(), db_path)
            days_in = (end - started).days + 1
            r["interim"] = {"value": value, "days": days_in, "days_in": days_in, "as_of": end.isoformat(),
                            "baseline": expected,
                            **metrics.compare(r["metric"], expected, value, band_scale=scale)}
        out.append(r)
    return out


def get_outcome(outcome_id, db_path=DB_PATH):
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM recommendation_outcomes WHERE id=?", (outcome_id,)).fetchone()
    finally:
        conn.close()
    return _row(row) if row else None


def list_outcomes(restaurant_id, status=None, limit=50, db_path=DB_PATH):
    conn = get_conn(db_path)
    try:
        sql = "SELECT * FROM recommendation_outcomes WHERE restaurant_id=?"
        args = [restaurant_id]
        if status:
            sql += " AND status=?"
            args.append(status)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        return [_row(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def abandon(restaurant_id, outcome_id, db_path=DB_PATH):
    """Stop tracking — the owner reversed the change or it no longer applies."""
    conn = get_conn(db_path)
    try:
        cur = conn.execute("UPDATE recommendation_outcomes SET status='abandoned' "
                           "WHERE id=? AND restaurant_id=? AND status='tracking'",
                           (outcome_id, restaurant_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def recent_results(restaurant_id, days=7, db_path=DB_PATH, today=None):
    """Verdicts that landed in the last `days` — what the morning brief and
    the weekly review lead with.

    The window is (today - days, today]: `days` whole days ending today, so
    consecutive sends never overlap. It used to include both ends — with
    days=1 a verdict due today, evaluated at 6am before the 7am brief, was
    in today's brief and again in tomorrow's."""
    today = today or date.today()
    since = (today - timedelta(days=days)).isoformat()
    until = today.isoformat()
    return [r for r in list_outcomes(restaurant_id, status="evaluated", db_path=db_path)
            if since < str(r.get("evaluate_on") or "")[:10] <= until]


def realised(restaurant_id, db_path=DB_PATH, since=None, denied_modules=None):
    """Every evaluated tracker that actually IMPROVED, carries dollars and
    still counts (not an alert read, not faded or reversed at its re-check).

    This is the one honest basis for "what has Cavnar AI been worth": each
    row was measured over a named window before the change and the same
    window after, and cleared its metric's own noise band to be called an
    improvement at all. `since` is an ISO date filtering on evaluate_on.

    `denied_modules` drops whole modules before anything is summed. It has
    to happen HERE rather than on the way out: filtering a breakdown while
    leaving the total intact hands a manager the food-cost dollars back by
    subtraction. A row is dropped when either the module it is credited to
    or its metric's module is denied.
    """
    denied = set(denied_modules or ())
    rows = []
    for r in list_outcomes(restaurant_id, status="evaluated", limit=500, db_path=db_path):
        if r.get("verdict") != "improved" or not r.get("dollars_monthly"):
            continue
        if not r.get("counts"):
            continue          # an alert READ, or a win that no longer holds
        if since and (r.get("evaluate_on") or "") < str(since)[:10]:
            continue
        if _denied_row(r, denied):
            continue
        rows.append(r)
    return rows


def _after_window(r):
    """(start, end) ISO dates of the window a tracker's "after" was read over."""
    start = r.get("after_start") or r.get("started_on") or ""
    end = r.get("after_end") or r.get("evaluate_on") or start
    return str(start)[:10], str(end)[:10]


def distinct(rows):
    """One result per piece of work: moves in the same metric FAMILY whose
    after-windows overlap measured the same before/after change, so they are
    one result, not two (AI-18, rec-ROI #4) — waste and food cost over the
    same weeks, labor % and overtime, one weekday's sales and sales. Each
    overlapping group keeps its largest dollar reading. Separate windows in
    one family, and overlapping windows in different families, stay
    separate. Callers pass one direction at a time (wins, or losses)."""
    out = []
    by_family = {}
    for r in rows:
        by_family.setdefault(metrics.family(r.get("metric")), []).append(r)

    def _size(g):
        return abs(float(g.get("dollars_monthly") or 0))
    for group_rows in by_family.values():
        group_rows = sorted(group_rows, key=lambda r: _after_window(r)[0])
        group, group_end = [], ""
        for r in group_rows:
            start, end = _after_window(r)
            if group and start <= group_end:
                group.append(r)
                group_end = max(group_end, end)
                continue
            if group:
                out.append(max(group, key=_size))
            group, group_end = [r], end
        if group:
            out.append(max(group, key=_size))
    return out


def distinct_wins(wins):
    """distinct() over wins — kept under the name value callers know."""
    return distinct(wins)


def result_line(r) -> str:
    """"Average rating 4.2★ → 4.4★, improved" — the result as numbers, with
    no dollars invented for a metric that has none (#49)."""
    if r.get("status") != "evaluated":
        return None
    label = r.get("metric_label") or r.get("metric")
    v = r.get("verdict")
    if v in (None, "unknown"):
        return f"{label}: couldn't be measured"
    word = {"improved": "improved", "worsened": "got worse", "no_clear_change": "no clear change"}.get(v, v)
    return f"{label} {_fmt(r['metric'], r.get('baseline_value'))} → {_fmt(r['metric'], r.get('after_value'))}, {word}"


def total_value(restaurant_id, db_path=DB_PATH, since=None, denied_modules=None):
    """What measured moves are worth, per month, with the honest denominator
    alongside.

    Deliberately NOT one bare number. `monthly` is the improvements (one per
    family per overlapping window, only those still holding); `worsened` is
    the changes that got worse, counted the same way; `net_monthly` is the
    one less the other and may be below zero — it says so rather than
    stopping at zero. `validated_monthly` is the part that held at its
    re-check with nothing else changing on its number. `tracked` and
    `unmeasurable` say how much of what the owner committed to could be read
    at all, because $400 from two results means something different when
    eight other trackers came back unknown.

    Summing here is legitimate where business_intelligence.money_at_stake
    refuses to: these are all the same claim kind (measured, before/after,
    per month) over the same restaurant, not a measured cost added to an
    elasticity forecast.
    """
    denied = set(denied_modules or ())

    def _visible(r):
        return not _denied_row(r, denied)

    evaluated = [r for r in list_outcomes(restaurant_id, status="evaluated", limit=500, db_path=db_path)
                 if (not since or (r.get("evaluate_on") or "") >= str(since)[:10]) and _visible(r)
                 and not r.get("informational")]
    tracking = [r for r in list_outcomes(restaurant_id, status="tracking", limit=500,
                                         db_path=db_path) if _visible(r) and not r.get("informational")]
    # Distinct work only (CLAUDE.md: "counts distinct work, never rows").
    all_wins = distinct([r for r in evaluated if r.get("verdict") == "improved" and r.get("counts")])
    wins = distinct([r for r in evaluated if r.get("verdict") == "improved" and r.get("counts")
                     and r.get("dollars_monthly")])
    all_worse = distinct([r for r in evaluated if r.get("verdict") == "worsened" and r.get("counts")])
    worse = distinct([r for r in evaluated if r.get("verdict") == "worsened" and r.get("counts")
                      and r.get("dollars_monthly")])
    validated = [r for r in wins if r.get("validated")]
    monthly = round(sum(abs(float(r["dollars_monthly"])) for r in wins), 2)
    worse_monthly = round(sum(abs(float(r["dollars_monthly"])) for r in worse), 2)
    net = round(monthly - worse_monthly, 2)
    by_module, net_by_module, wins_by_module = {}, {}, {}
    for r in wins:
        m = module_of_row(r)
        by_module[m] = round(by_module.get(m, 0.0) + abs(float(r["dollars_monthly"])), 2)
        net_by_module[m] = round(net_by_module.get(m, 0.0) + abs(float(r["dollars_monthly"])), 2)
    for r in worse:
        m = module_of_row(r)
        net_by_module[m] = round(net_by_module.get(m, 0.0) - abs(float(r["dollars_monthly"])), 2)
    for r in all_wins:
        m = module_of_row(r)
        wins_by_module[m] = wins_by_module.get(m, 0) + 1
    if not worse_monthly:
        net_note = "Nothing measured got worse, so the net is the improvements."
    elif net >= 0:
        net_note = (f"${monthly:,.0f}/month of improvements less ${worse_monthly:,.0f}/month from changes "
                    f"that got worse.")
    else:
        net_note = (f"More was measured getting worse than improving: ${worse_monthly:,.0f}/month worse "
                    f"against ${monthly:,.0f}/month better. The figure is below zero because that is what "
                    f"was measured.")
    return {
        "monthly": monthly,
        "annual": round(monthly * 12, 2),
        "wins": len(wins),
        "wins_measured": len(all_wins),
        "wins_by_module": wins_by_module,
        "unpriced_wins": [{"title": r.get("title"), "module": module_of_row(r), "line": r.get("result_line")}
                          for r in all_wins if not r.get("dollars_monthly")],
        "worsened": {"count": len(all_worse), "monthly": worse_monthly},
        "net_monthly": net,
        "net_note": net_note,
        "net_by_module": net_by_module,
        "validated_monthly": round(sum(abs(float(r["dollars_monthly"])) for r in validated), 2),
        "validated": len(validated),
        "faded": len([r for r in evaluated if r.get("verdict") == "improved"
                      and r.get("recheck_verdict") in _FAILED_RECHECK]),
        "evaluated": len(evaluated),
        "in_flight": len(tracking),
        "unmeasurable": len([r for r in evaluated if r.get("verdict") in (None, "unknown")]),
        "no_clear_change": len([r for r in evaluated if r.get("verdict") == "no_clear_change"]),
        "by_module": by_module,
        "caveat": CAUSATION_CAVEAT,
    }


def best_ever(restaurant_id, db_path=DB_PATH, denied_modules=None):
    """The single biggest measured win that still counts, all time — the
    answer to "which recommendation created the biggest impact".

    strategy_jobs picks the biggest of ONE daily pass to notify on; that is
    a different question and deliberately stays where it is. This one has
    no time window and no dollar floor.
    """
    wins = realised(restaurant_id, db_path=db_path, denied_modules=denied_modules)
    if not wins:
        return None
    return max(wins, key=lambda r: abs(float(r["dollars_monthly"])))


_BASELINE_PHRASE = {
    "prior window": "the same length of time before",
    "matched weekdays": "the same weekdays in the weeks before",
    "same weeks last year": "the weeks before, adjusted by how the same weeks moved last year",
}


def win_message(r) -> str:
    """The body of "That one worked" for an improved tracker. It used to say
    the metric improved "over the window you set" — but most trackers are
    observed or started with a default window, and no owner set one. This
    names the actual dates and what the change was measured against."""
    from time_utils import mdy
    label = r.get("metric_label") or r.get("metric") or "the number"
    start, end = _after_window(r)
    when = f" between {mdy(start)} and {mdy(end)}" if start and end else ""
    against = _BASELINE_PHRASE.get(r.get("baseline_kind") or "prior window", _BASELINE_PHRASE["prior window"])
    return (f"{owner_title(r.get('title')) or 'The change you made'}: {label} improved{when}, "
            f"measured against {against}. {CAUSATION_CAVEAT}")


def summarise(r) -> str:
    """One honest sentence for a finished tracker."""
    from time_utils import mdy
    label = r.get("metric_label") or r["metric"]
    unit = r.get("unit") or ""
    if r.get("verdict") in (None, "unknown"):
        return f"{r['title']}: couldn't measure {label.lower()} before and after."
    fmt = (lambda v: f"${v:,.0f}") if unit == "$" else (lambda v: f"{v:g}{unit}")
    moved = f"{label} went from {fmt(r['baseline_value'])} to {fmt(r['after_value'])}"
    if r["verdict"] == "no_clear_change":
        return f"{r['title']}: {moved} — within normal week-to-week noise, so no clear change."
    if is_informational(r):
        # Reading an alert is not a change: what followed is shown, never priced.
        return f"{r['title']}: {moved} in the weeks after — for information, not counted as value."
    money = ""
    if r.get("dollars_monthly"):
        money = f", roughly ${abs(r['dollars_monthly']):,.0f}/month"
    word = "improved" if r["verdict"] == "improved" else "got worse"
    out = f"{r['title']}: {moved} — {word}{money}."
    if r.get("recheck_verdict") in _FAILED_RECHECK:
        when = mdy(r.get("rechecked_at") or r.get("recheck_on"))
        out += (f" It no longer held when re-checked on {when}, so it "
                f"{'no longer counts' if r['verdict'] == 'improved' else 'is no longer subtracted'}.")
    return out


# ── boot ────────────────────────────────────────────────────────────────────

def init_outcomes(db_path=DB_PATH):
    """Boot (models.init_db, after the columns and rec_instances exist): fill
    the rec-ROI columns on rows written before them, once. What can be
    recomputed is — the % change, the module (from the recommendation's own
    record where there is one), the baseline kind (every row before this
    used the prior window), the re-check date — and what cannot (the other
    changes in an old window, until its re-check reads them) stays NULL."""
    conn = get_conn(db_path)
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM recommendation_outcomes WHERE module IS NULL OR baseline_kind IS NULL "
            "OR (delta_pct IS NULL AND delta IS NOT NULL AND baseline_value IS NOT NULL AND baseline_value != 0) "
            "OR (status='evaluated' AND verdict IN ('improved','worsened') AND recheck_on IS NULL "
            "AND source_key NOT LIKE ?)", (INFORMATIONAL_PREFIX + "%",)).fetchall()]
    finally:
        conn.close()
    if not rows:
        return 0
    for r in rows:
        mod = r.get("module") or resolve_module(r["restaurant_id"], r["source"], r["source_key"], r["metric"],
                                                db_path=db_path)
        pct = r.get("delta_pct")
        if pct is None and r.get("delta") is not None and r.get("baseline_value"):
            pct = round(float(r["delta"]) / float(r["baseline_value"]) * 100, 1)
        recheck_on = r.get("recheck_on")
        if (recheck_on is None and r["status"] == "evaluated" and r.get("verdict") in _MOVED
                and not is_informational(r)):
            recheck_on = recheck_on_for(r)
        conn = get_conn(db_path)
        try:
            conn.execute("UPDATE recommendation_outcomes SET module=?, delta_pct=?, recheck_on=?, "
                         "baseline_kind=COALESCE(baseline_kind, 'prior window'), "
                         "baseline_raw=COALESCE(baseline_raw, baseline_value) WHERE id=?",
                         (mod, pct, recheck_on, r["id"]))
            conn.commit()
        finally:
            conn.close()
    return len(rows)
