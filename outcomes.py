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
import sqlite3
import time
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
# A window counted in trading days (metrics.COVERAGE_METRICS) is read only
# when at least this share of its trading days was measured: one day before
# and one day after was a verdict, a grade and a monthly figure (re-audit
# A2). Below it the reading is unknown — baseline, after-window, re-check
# and each day's accrual window alike.
MIN_COVERAGE = 0.7
# Starts no owner pressed a button for — a schedule published, an order
# sent, a month's reprices, a guest-text campaign, a schedule review
# accepted, Home's Done — are refused while ANYTHING in the metric's family
# is being measured, not only the same metric (re-audit A18).
AUTOMATIC_SOURCES = ("observed", "reprice", "slow_day_campaign", "schedule")

BASELINE_KINDS = ("prior window", "matched weekdays", "same weeks last year", "before the trigger")
# ── regression to the mean (CA2 #1) ─────────────────────────────────────────
# A recommendation fires because a number was BAD: labor over target, the
# worst weekday of seven, a spike in waste. The window that fired it is the
# window a number is most likely to come back from on its own. Measured
# against that window, a change that did nothing read "improved" 54% of the
# time and "worsened" 11% (CA2 probe R). So a tracker on a TRIGGERED
# recommendation — one a surface showed before the owner took it, or an
# alert — is never measured against the window that triggered it:
#
#   trigger window   the tracker's window length ending the day before the
#                    recommendation was first shown (its chain's start) —
#                    what the card was reading when it fired. Stored as
#                    trigger_start / trigger_end with its reading,
#                    trigger_value.
#   baseline         the MIRROR of the after-window about the trigger: the
#                    same length, as far before the trigger window as the
#                    after-window starts after it. A number's pull back to
#                    its usual level after a bad stretch is the same looking
#                    back as looking forward, so a change that does nothing
#                    reads improved as often as worsened (the probe-R
#                    regression test). A longer trailing baseline (8–12
#                    weeks) was tried and read "worsened" twice as often as
#                    "improved" for a do-nothing change — the after-window
#                    sits nearer the trigger than most of those weeks.
#                    Adjusted by last year's same weeks where a year covers
#                    both, as any seasonal baseline is.
#   fallback         when that window cannot be read (too little history,
#                    under MIN_COVERAGE), the old baseline is used and, if
#                    it overlaps the trigger window, the result is flagged
#                    `baseline_overlaps_trigger`: shown, never counted —
#                    not as a win, not as a loss, not in learning, not in
#                    delivered value.
TRIGGER_BASELINE_KIND = "before the trigger"
# A recommendation taken long after it fired is no longer measured from its
# trigger: past this many days between the trigger window's end and the
# start, the mirror would reach back across seasons, and the plain baseline
# (which then cannot overlap the trigger window) is used.
TRIGGER_MAX_GAP_DAYS = 56
# Trackers whose key names a recommendation shown before it was taken are
# triggered (the episode's first showing); an alert read is triggered by
# the alert. A routine action (a schedule published) is not.
_ALERT_TRIGGERED_PREFIX = "observed:alert_"
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


def local_today(restaurant_id, db_path=DB_PATH) -> date:
    """The restaurant's own calendar date (re-audit A8). The server runs on
    UTC: a Pacific owner pressing Track at 5pm was on "tomorrow", so the
    day the change started sat inside its own baseline window."""
    from time_utils import restaurant_now
    try:
        rest = _models_mod.get_restaurant(restaurant_id, db_path)
    except Exception as e:
        print(f"[outcomes] restaurant {restaurant_id} unreadable for its local date: {e}")
        rest = None
    return restaurant_now(rest, naive=True).date()


def metric_visible_to(viewer, metric) -> bool:
    """Whether a login may see results on `metric`: food cost and waste need
    FOOD_COST_VIEW, comps and voids LOSS_VIEW (a comp result can name the
    manager approving them — re-audit A26/A27). No viewer is an internal
    caller; an admin sees everything. Fails closed."""
    if viewer is None or (isinstance(viewer, dict) and viewer.get("is_admin")):
        return True
    base = metrics.parse(metric)[0]
    try:
        from permissions import has_permission, FOOD_COST_VIEW, LOSS_VIEW
        if base in ("food_cost_pct", "weekly_waste"):
            return has_permission(viewer, FOOD_COST_VIEW)
        if base in LOSS_METRICS:
            return has_permission(viewer, LOSS_VIEW)
        return True
    except Exception as e:
        print(f"[outcomes] metric visibility check failed closed: {e}")
        return False


LOSS_METRICS = ("comp_rate", "void_rate")


def _measure(restaurant_id, metric, start, end, db_path=DB_PATH):
    """metrics.measure with the coverage floor (MIN_COVERAGE): a window
    counted in trading days is unknown unless enough of it was measured."""
    value, detail = metrics.measure(restaurant_id, metric, _iso(start), _iso(end), db_path)
    if value is None:
        return value, detail
    cov = metrics.coverage(restaurant_id, metric, _iso(start), _iso(end), db_path)
    if cov and cov["expected"] and cov["share"] < MIN_COVERAGE:
        return None, (f"only {cov['measured']} of {cov['expected']} trading days in this window were "
                      f"measured")
    return value, detail


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
            d["attribution"] = grade(d.get("verdict"), None, conc, checked=conc is not None,
                                     checkin=_checkin_of(d), overlaps=overlaps_trigger(d))
    else:
        d["attribution"] = None
    d["owner_checkin"] = _checkin_of(d)
    d["baseline_overlaps_trigger"] = overlaps_trigger(d)
    d["attribution_label"] = attribution_label(d)
    d["validated"] = is_validated(d)
    d["counts"] = counts_in_delivered(d)
    d["grade_phrase"] = grade_phrase(d) if d.get("status") == "evaluated" else None
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


# Recommendation kinds whose module and metric are theirs whatever surface
# presented them (re-audit A11): "Fill Tuesdays" is a guest-text
# recommendation aimed at Tuesdays' sales. The brief and the weekly email
# filed it under Labor, so its Track started a labor tracker (or none) and
# it read as a change on labor cost for every labor tracker in its weeks.
KIND_MODULE = {"slow_day": "marketing"}


# Kinds that are about one number by definition, whatever surface showed
# them (re-audit A30): Home's "N people over 40h this week" card and the
# schedule review's overtime moves are overtime recommendations. They were
# presented with no metric, and "schedule" is not a module Track has a
# fallback number for, so taking one measured nothing.
KIND_METRIC = {"overtime": "overtime_hours", "overtime_move": "overtime_hours"}


def _kind_metric(key):
    """The metric a recommendation kind carries by definition, or None."""
    kind, _, param = str(key or "").partition(":")
    if kind == "slow_day":
        day = param.strip().capitalize()
        m = f"weekday_sales:{day}"
        return m if metrics.known(m) else None
    return KIND_METRIC.get(kind)


def resolve_module(restaurant_id, source, source_key, metric, module=None, db_path=DB_PATH) -> str:
    """The module a new tracker is credited to, most specific first: what
    the recommendation's kind says (KIND_MODULE), the recommendation's own
    (rec_instances, by key), a DSR action's block, the caller's, the
    source's, then the metric's.

    The caller's module used to come first — and iOS sends the SCREEN a
    Track was pressed on, so a guest-text recommendation answered on the
    Labor tab was credited to Labor (re-audit A25). A caller's module now
    names the module only for a tracker no recommendation stands behind."""
    kind = str(source_key or "").split(":", 1)[0]
    if kind in KIND_MODULE:
        return KIND_MODULE[kind]
    rec = _latest_rec(restaurant_id, source_key, db_path)
    if rec is not None:
        vm = value_module(rec["module"])
        if vm:
            return vm
    if str(source_key or "").startswith("dsr_action:"):
        vm = _dsr_block_module(source_key)
        if vm:
            return vm
    vm = value_module(module)
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


def metric_for_rec(restaurant_id, key, body_metric=None, db_path=DB_PATH, viewer=None):
    """(metric or None, authoritative) — the metric this recommendation
    CARRIES. A DSR action's kind is authoritative: "reorder" carries none
    and nothing may be substituted for it; so is a kind that names its own
    number (a slow day is that weekday's sales). Otherwise the metric the
    recommendation was presented with (rec_instances.expected_metric), else
    one the client sent, else None.

    `viewer` (a route's login): a metric that login may not see is never
    started from its answer (re-audit A27) — a client-sent one is ignored,
    and a carried one refuses (None, authoritative) rather than measuring
    food cost or comps for a login that cannot read them."""
    key = str(key or "")

    def _seen(m):
        return m if (m and metric_visible_to(viewer, m)) else None
    if key.startswith("dsr_action:"):
        parts = key.split(":")
        m = DSR_ACTION_METRICS.get(parts[1] if len(parts) > 1 else "")
        return _seen(m), True
    m = _kind_metric(key)
    if m:
        return _seen(m), True
    rec = _latest_rec(restaurant_id, key, db_path)
    if rec is not None and rec["expected_metric"] and metrics.known(rec["expected_metric"]):
        m = metrics.normalize(rec["expected_metric"])
        return (m, False) if _seen(m) else (None, True)
    if body_metric and metrics.known(body_metric) and _seen(body_metric):
        return metrics.normalize(body_metric), False
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
    # Informational only (CA2 #7, INFORMATIONAL_PREFIXES): an order sent is
    # routine buying, not a change aimed at food cost %, and its tracker
    # accrued food-cost "savings" no recommendation stood behind. What the
    # number did next is still shown; it never counts as value or learning.
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
# Every key that measures what followed something other than a change the
# owner made on Cavnar's advice (rec_learning holds the same tuple):
#   observed:alert_…               an alert READ
#   observed:supplier_order_sent:… a routine order (CA2 #7)
#   observed:untaken:…             a recommendation shown and NOT taken — the
#                                  comparison group for the ones that were
#                                  (CA2 #11, observe_untaken)
UNTAKEN_PREFIX = "observed:untaken:"
INFORMATIONAL_PREFIXES = (INFORMATIONAL_PREFIX, "observed:supplier_order_sent:", UNTAKEN_PREFIX)


def is_informational(r) -> bool:
    """A tracker that measures what followed a READ (or a routine order, or
    advice not taken), not a change — shown, never counted."""
    return str((r or {}).get("source_key") or "").startswith(INFORMATIONAL_PREFIXES)


def _informational_key(key) -> bool:
    return str(key or "").startswith(INFORMATIONAL_PREFIXES)


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
    # A detail can carry a date ("within 7 days of 2026-09-01"): M/D/YY for
    # the owner (re-audit A34).
    why = f" ({owner_title(detail)})" if detail else ""
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
    want = metrics.normalize(metric)
    for r in rows:
        if not include_informational and _informational_key(r["source_key"]):
            continue
        # Normalised both sides: a row written before keys were normalised
        # still blocks its own number (re-audit A17).
        if (metrics.family(r["metric"]) == fam) if family else (metrics.normalize(r["metric"]) == want):
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
    today = today or local_today(restaurant_id, db_path)
    # An informational tracker (an alert opened) never counts, so it must
    # not block a real owner action on the same metric either; any tracker
    # blocks a second informational one. Automatic, so the FAMILY gate
    # (re-audit A18): a schedule published while overtime is measured
    # would read the same labor dollars twice.
    key = f"observed:{action}:{today.strftime('%Y-%m')}"
    live = in_flight_on(restaurant_id, metric, db_path=db_path, family=True,
                        include_informational=_informational_key(key))
    if live:
        return None
    # Once per metric per calendar month, whatever became of the first:
    # the month's key was unique only while tracking, so a schedule
    # published on the 30th started a second August tracker the day after
    # the first was evaluated (re-audit A19).
    conn = get_conn(db_path)
    try:
        seen = conn.execute("SELECT 1 FROM recommendation_outcomes WHERE restaurant_id=? AND source_key=? LIMIT 1",
                            (restaurant_id, key)).fetchone()
    finally:
        conn.close()
    if seen:
        return None
    if detail:
        title = f"{title} — {str(detail)[:80]}"
    try:
        return record(restaurant_id, "observed", key, title, metric, user_id=user_id,
                      db_path=db_path, today=today, gate="family")
    except TrackerRefused:
        return None


# ── advice not taken: the comparison group (CA2 #11) ────────────────────────
# Only recommendations the owner TOOK were ever measured, so a success rate
# had nothing to be compared with: a number that improves after most bad
# stretches improves after the taken ones too. A recommendation that was
# shown and then dismissed or left to expire now gets an informational
# tracker on the number it carried, measured the same way (trigger, mirror
# baseline, the restaurant's band) from the day it was settled. It never
# counts as value or as learning (INFORMATIONAL_PREFIXES); rec_learning.
# untaken_comparison sets taken beside not-taken, worded "compared with when
# you didn't", never as cause.
UNTAKEN_LOOKBACK_DAYS = 14        # episodes settled this recently get one
# An owner who said "already doing it" did take it, just not through us.
UNTAKEN_SKIP_REASONS = ("already_doing",)


def observe_untaken(restaurant_id, db_path=DB_PATH, today=None) -> int:
    """Start an informational tracker for each recommendation of this
    restaurant shown and then dismissed or expired in the last
    UNTAKEN_LOOKBACK_DAYS, that carried a measurable metric and has none
    yet. Key: observed:untaken:<rec_id>. Returns how many started. Never
    raises; refused starts (the number already being measured) are
    skipped."""
    today = today or local_today(restaurant_id, db_path)
    since = (today - timedelta(days=UNTAKEN_LOOKBACK_DAYS)).isoformat()
    try:
        conn = get_conn(db_path)
        try:
            eps = [dict(r) for r in conn.execute(
                "SELECT * FROM rec_instances i WHERE i.restaurant_id=? AND i.status IN ('dismissed','expired') "
                "AND i.expected_metric IS NOT NULL AND i.tracker_id IS NULL "
                "AND COALESCE(i.closed_at, i.created_at) >= ? "
                "AND EXISTS (SELECT 1 FROM rec_events s WHERE s.rec_id=i.rec_id AND s.event='shown') "
                "ORDER BY i.created_at", (restaurant_id, since)).fetchall()]
            started = {r["source_key"] for r in conn.execute(
                "SELECT source_key FROM recommendation_outcomes WHERE restaurant_id=? AND source_key LIKE ?",
                (restaurant_id, UNTAKEN_PREFIX + "%")).fetchall()}
            reasons = {}
            for e in eps:
                row = conn.execute("SELECT meta FROM rec_events WHERE rec_id=? AND event='dismissed' "
                                   "ORDER BY at DESC, id DESC LIMIT 1", (e["rec_id"],)).fetchone()
                try:
                    reasons[e["rec_id"]] = (json.loads(row["meta"] or "{}") or {}).get("reason_code") if row else None
                except (TypeError, ValueError):
                    reasons[e["rec_id"]] = None
        finally:
            conn.close()
    except Exception as ex:
        print(f"[outcomes] untaken episodes unreadable for {restaurant_id}: {ex}")
        return 0
    n = 0
    for e in eps:
        key = f"{UNTAKEN_PREFIX}{e['rec_id']}"
        metric = e.get("expected_metric")
        if key in started or not metrics.known(metric) or reasons.get(e["rec_id"]) in UNTAKEN_SKIP_REASONS:
            continue
        metric = metrics.normalize(metric)
        window = int(metrics.describe(metric)["default_window_days"])
        if metrics.parse(metric)[0] in metrics.WEEKDAY_MIX_METRICS:
            window = max(7, int(round(window / 7.0)) * 7)
        stamps = [str(e.get(c) or "")[:10] for c in ("chain_started_at", "created_at")]
        try:
            first = min(date.fromisoformat(s) for s in stamps if len(s) == 10)
        except ValueError:
            continue
        first = min(first, today)
        trig = (first - timedelta(days=window), first - timedelta(days=1))
        try:
            record(restaurant_id, "observed", key, f"Not taken: {e.get('title') or e.get('key')}", metric,
                   window_days=window, db_path=db_path, today=today, module=e.get("module"), gate="family",
                   trigger=trig)
            n += 1
        except TrackerRefused:
            continue
        except Exception as ex:
            print(f"[outcomes] untaken tracker not started for {restaurant_id} {key}: {ex}")
    return n


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
        # Coverage in TRADING days: a restaurant closed two days a week
        # measured 20 of 28 calendar days and was never adjusted at all.
        cov = metrics.coverage(restaurant_id, metric, ls.isoformat(), le.isoformat(), db_path)
        if cov is not None and (not cov["expected"] or cov["share"] < LY_MIN_COVERAGE):
            return None
        v, _ = metrics.measure(restaurant_id, metric, ls.isoformat(), le.isoformat(), db_path)
        if v is None:
            return None
        out.append(v)
    return out[0], out[1]


def trigger_window_for(restaurant_id, source_key, window, today, db_path=DB_PATH):
    """(start, end) dates of the window that triggered this tracker's
    recommendation, or None for an untriggered one (see the regression-to-
    the-mean note at TRIGGER_BASELINE_KIND): the `window` days ending the day
    before the recommendation was first shown — its episode chain's start —
    or before today for an alert read. Never raises."""
    key = str(source_key or "")
    first = None
    if key.startswith(_ALERT_TRIGGERED_PREFIX):
        first = today
    elif key.startswith("observed:"):
        return None
    else:
        conn = get_conn(db_path)
        try:
            row = conn.execute("SELECT * FROM rec_instances WHERE restaurant_id=? AND key=? "
                               "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                               (restaurant_id, key)).fetchone()
        except Exception as e:
            print(f"[outcomes] trigger episode unreadable for {restaurant_id}: {e}")
            row = None
        finally:
            conn.close()
        if row is None:
            return None
        row = dict(row)
        stamps = [str(row.get(c) or "")[:10] for c in ("chain_started_at", "created_at")]
        stamps = [s for s in stamps if len(s) == 10]
        try:
            first = min(date.fromisoformat(s) for s in stamps) if stamps else None
        except ValueError:
            first = None
    if first is None:
        return None
    first = min(first, today)
    end = first - timedelta(days=1)
    return end - timedelta(days=int(window) - 1), end


def mirror_window(trigger, today, window):
    """(start, end) dates of a triggered tracker's baseline: the mirror of
    the after-window [today, today + window − 1] about the trigger window —
    `window` days, ending as many days before the trigger window starts as
    the after-window starts after it ends (TRIGGER_BASELINE_KIND). Pure."""
    ts, te = _day(trigger[0]), _day(trigger[1])
    today = today if isinstance(today, date) else _day(today)
    gap = max(0, (today - te).days - 1)
    end = ts - timedelta(days=gap + 1)
    return end - timedelta(days=int(window) - 1), end


def _windows_overlap(a, b) -> bool:
    return bool(a and b) and _iso(a[0]) <= _iso(b[1]) and _iso(b[0]) <= _iso(a[1])


def _seasonal_adjust(restaurant_id, metric, raw, detail, b_start, b_end, today, window, db_path):
    """(value, kind or None, detail): `raw` moved by what the same weeks did
    last year, when a year of history covers the baseline and the after-
    window; kind None when it could not be adjusted."""
    a_end = today + timedelta(days=window - 1)
    shift = _seasonal_shift(restaurant_id, metric, (b_start, b_end), (today, a_end), db_path)
    adjusted = _apply_shift(metric, raw, *shift) if shift else None
    if adjusted is None:
        return raw, None, detail
    return adjusted, "same weeks last year", (f"{detail}; {_fmt(metric, raw)} before, moved as the same weeks "
                                              f"moved last year ({_fmt(metric, shift[0])} to "
                                              f"{_fmt(metric, shift[1])})")


def _baseline(restaurant_id, metric, today, window, db_path, trigger=None):
    """The reading the after-window is compared against (audit #30).

    prior window         the `window` days ending yesterday — every metric
                         a season does not move (ratings, waste, replies).
    matched weekdays     labor %, food cost %, sales: a window a whole
                         number of weeks before, so it holds the same
                         weekdays as the after-window (for a 28-day window
                         that IS the prior window).
    same weeks last year the matched window, moved by what the same weeks
                         did last year — when a year of history covers both.
    before the trigger   a TRIGGERED recommendation (`trigger` is its
                         trigger window, trigger_window_for): the mirror of
                         the after-window about the trigger window (CA2 #1,
                         TRIGGER_BASELINE_KIND), adjusted by last year where
                         it can be. When it cannot be read the plain
                         baseline above is used and `overlaps_trigger` says
                         whether it overlaps the trigger window.
    """
    base, _ = metrics.parse(metric)
    seasonal = base in metrics.SEASONAL_METRICS
    out = {"trigger": trigger, "overlaps_trigger": False, "trigger_value": None}
    if trigger:
        ts, te = _day(trigger[0]), _day(trigger[1])
        tv, _ = _measure(restaurant_id, metric, ts.isoformat(), te.isoformat(), db_path)
        out["trigger_value"] = tv
        m_start, m_end = mirror_window(trigger, today, window)
        if (today - te).days - 1 > TRIGGER_MAX_GAP_DAYS:
            raw, detail = None, None        # taken long after it fired: the plain baseline below
        else:
            raw, detail = _measure(restaurant_id, metric, m_start.isoformat(), m_end.isoformat(), db_path)
        if raw is not None:
            from time_utils import mdy
            detail = (f"{detail}; read before what prompted the recommendation ({mdy(ts.isoformat())}–"
                      f"{mdy(te.isoformat())}), so a number coming back from a bad stretch on its own is not "
                      f"counted as the change")
            value, kind = raw, TRIGGER_BASELINE_KIND
            if seasonal:
                value, lk, detail = _seasonal_adjust(restaurant_id, metric, raw, detail, m_start, m_end, today,
                                                     window, db_path)
                kind = lk or kind
            out.update(value=value, raw=raw, detail=detail, start=m_start.isoformat(), end=m_end.isoformat(),
                       kind=kind)
            return out
    if seasonal:
        weeks = -(-int(window) // 7)
        b_start = today - timedelta(days=weeks * 7)
        b_end = b_start + timedelta(days=window - 1)
        kind = "matched weekdays"
    else:
        b_end = today - timedelta(days=1)
        b_start = b_end - timedelta(days=window - 1)
        kind = "prior window"
    raw, detail = _measure(restaurant_id, metric, b_start.isoformat(), b_end.isoformat(), db_path)
    value = raw
    if raw is not None and seasonal:
        value, lk, detail = _seasonal_adjust(restaurant_id, metric, raw, detail, b_start, b_end, today, window,
                                             db_path)
        kind = lk or kind
    out.update(value=value, raw=raw, detail=detail, start=b_start.isoformat(), end=b_end.isoformat(), kind=kind,
               overlaps_trigger=_windows_overlap((b_start, b_end), trigger) if trigger else False)
    return out


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
           window_days=None, db_path=DB_PATH, today=None, module=None, gate="metric", trigger="auto"):
    """Start tracking one recommendation the owner has committed to.

    Idempotent on (restaurant, source_key) while tracking — committing to the
    same fix twice returns the existing tracker rather than resetting its
    baseline, which would quietly erase the improvement already made.

    `gate` is the one-tracker-per-number rule (audit #3): "metric" refuses
    while another tracker measures the same metric, "family" while one
    measures anything in its family (the automatic starts, #18), None
    skips it. A refusal raises TrackerRefused; start() turns it into a
    reply. `module` is who the recommendation was (resolve_module when None).

    Two starts at the same moment (web and phone, a double tap) both passed
    the gate before either inserted, and a second start on the same key
    500'd on the unique index (re-audit A16): the gate is checked again
    inside BEGIN IMMEDIATE, so the second waits for the first and is
    answered by it.

    `trigger` is the window that fired the recommendation, (start, end):
    "auto" resolves it from the recommendation's episode or alert
    (trigger_window_for), None says the tracker was not triggered. A
    triggered tracker's baseline is never its trigger window (CA2 #1). The
    restaurant's own noise band for this comparison is stored with it
    (noise_band, false_alarm_rate, band_basis — CA2 #3).
    """
    if not metrics.known(metric):
        raise ValueError(f"unknown metric {metric}")
    metric = metrics.normalize(metric)
    today = today or local_today(restaurant_id, db_path)
    info = metrics.describe(metric)
    window = int(window_days or info["default_window_days"])
    if metrics.parse(metric)[0] in metrics.WEEKDAY_MIX_METRICS:
        # Whole weeks (re-audit A21): a 30-day window holds two extra
        # weekdays, so its re-check and each day's accrual window held a
        # different weekday mix from its baseline — a Friday-heavy restaurant
        # read "worse" with nothing changed.
        window = max(7, int(round(window / 7.0)) * 7)
    if gate == "metric" and source in AUTOMATIC_SOURCES:
        gate = "family"
    informational = _informational_key(source_key)

    def _gate(conn):
        existing = conn.execute(
            "SELECT * FROM recommendation_outcomes WHERE restaurant_id=? AND source_key=? "
            "AND status='tracking'", (restaurant_id, source_key)).fetchone()
        if existing:
            return existing
        if gate:
            live = _live_on(conn, restaurant_id, metric, family=(gate == "family"),
                            include_informational=informational)
            if live is not None:
                raise TrackerRefused(_row(live), metric)
        return None

    conn = get_conn(db_path)
    try:
        existing = _gate(conn)
    finally:
        conn.close()
    if existing:
        return _row(existing)

    # The baseline ends before today: today is part of the "after", and a
    # baseline that includes the day the change started is contaminated by it.
    if trigger == "auto":
        trigger = trigger_window_for(restaurant_id, source_key, window, today, db_path=db_path)
    b = _baseline(restaurant_id, metric, today, window, db_path, trigger=trigger)
    nb = {"band": None, "sigma": None, "false_alarm_rate": None, "basis": None}
    if b["value"] is not None:
        nb = metrics.noise_band(restaurant_id, metric, window_days=window, end=b["end"], before=b["raw"],
                                db_path=db_path)
    evaluate_on = today + timedelta(days=window)
    mod = resolve_module(restaurant_id, source, source_key, metric, module=module, db_path=db_path)
    trig = b.get("trigger")

    conn = get_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = _gate(conn)
        except TrackerRefused:
            conn.rollback()
            raise
        if existing:
            conn.rollback()
            return _row(existing)
        try:
            cur = conn.execute(
                "INSERT INTO recommendation_outcomes (restaurant_id, source, source_key, title, metric, "
                "baseline_value, baseline_raw, baseline_kind, baseline_start, baseline_end, baseline_detail, "
                "started_on, evaluate_on, status, created_by, module, trigger_value, trigger_start, trigger_end, "
                "baseline_overlaps_trigger, noise_band, noise_sigma, false_alarm_rate, band_basis) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, 'tracking', ?, ?, ?,?,?,?,?,?,?,?)",
                (restaurant_id, source, source_key, owner_title(title)[:200], metric, b["value"], b["raw"],
                 b["kind"], b["start"], b["end"], b["detail"], today.isoformat(), evaluate_on.isoformat(),
                 user_id, mod, b.get("trigger_value"),
                 _iso(trig[0]) if trig else None, _iso(trig[1]) if trig else None,
                 1 if b.get("overlaps_trigger") else 0, nb.get("band"), nb.get("sigma"),
                 nb.get("false_alarm_rate"), nb.get("basis")))
        except sqlite3.IntegrityError:
            # The same key started by another connection that got there
            # first (a database without the lock's guarantee): answered by it.
            conn.rollback()
            existing = conn.execute(
                "SELECT * FROM recommendation_outcomes WHERE restaurant_id=? AND source_key=? "
                "AND status='tracking'", (restaurant_id, source_key)).fetchone()
            if existing:
                return _row(existing)
            raise
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
# Numbers that are a share of sales: a sales move alone moves them (re-audit
# A5). Labor % fell four points on a 15% sales lift with labor dollars flat,
# and was read — and paid — as a labor saving.
_COST_FAMILIES = {"labor_cost", "food_cost", "comps", "voids"}
_REC_MODULE_FAMILY = {"labor": "labor_cost", "schedule": "labor_cost", "food": "food_cost",
                      "reviews": "guest_rating", "marketing": "sales", "guests": "sales"}
_DISOWNED_LIKE = '%"did_it": "no"%'
_CHANGED_LIKE = '%"conditions_changed": true%'
# The SQL twin of result_counts for a joined tracker `o` (cumulative and
# _release_days): not disowned, no "something else changed" check-in, not
# measured against its trigger window, nothing else found moving the number
# (confounded: the stored concurrent list is empty — evaluate writes '[]').
# Two ? — _DISOWNED_LIKE, _CHANGED_LIKE.
_COUNTS_SQL = ("(o.owner_checkin IS NULL OR (o.owner_checkin NOT LIKE ? AND o.owner_checkin NOT LIKE ?)) "
               "AND COALESCE(o.baseline_overlaps_trigger, 0) = 0 "
               "AND (o.concurrent IS NULL OR TRIM(o.concurrent) IN ('', '[]'))")


def _rec_family(key, module, expected_metric):
    key = str(key or "")
    if key.startswith("dsr_action:"):
        m = DSR_ACTION_METRICS.get(key.split(":")[1] if ":" in key else "")
        return metrics.family(m) if m else None
    m = _kind_metric(key)
    if m:
        return metrics.family(m)
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


def _next_day(iso):
    return (_day(iso) + timedelta(days=1)).isoformat()


def _sales_move(r, a, b, db_path):
    """A concurrent-change entry when sales per day moved past their own
    band between the tracker's baseline window and [a, b], else None. For a
    "same weeks last year" tracker the sales expectation is moved the way
    the same weeks moved last year first, as its own baseline was."""
    if not (r.get("baseline_start") and r.get("baseline_end")):
        return None
    rid = r["restaurant_id"]
    before, _ = _measure(rid, "sales", r["baseline_start"], r["baseline_end"], db_path)
    after, _ = _measure(rid, "sales", a, b, db_path)
    if before is None or after is None:
        return None
    expected = before
    if (r.get("baseline_kind") or "") == "same weeks last year":
        shift = _seasonal_shift(rid, "sales", (r["baseline_start"], r["baseline_end"]), (a, b), db_path)
        if shift:
            expected = _apply_shift("sales", before, *shift) or before
    cmp = metrics.compare("sales", expected, after)
    if cmp["verdict"] not in _MOVED:
        return None
    pct = abs(cmp.get("delta_pct") or 0)
    word = "rose" if cmp["delta"] > 0 else "fell"
    return {"kind": "sales_move", "label": f"Sales per day {word} {pct:.0f}% in the same weeks", "date": _iso(a)}


def _ly_holiday_gaps(a, b):
    """Holidays in [a, b] with no holiday of the same NAME in the same weeks
    last year, and the reverse: a "same weeks last year" baseline holds last
    year's holidays, and Easter moves (re-audit A12)."""
    now = _holidays_between(a, b)
    ly = _holidays_between((_day(a) - timedelta(days=LY_OFFSET_DAYS)).isoformat(),
                           (_day(b) - timedelta(days=LY_OFFSET_DAYS)).isoformat())
    out = [{"kind": "holiday", "label": n, "date": d} for d, n in sorted(now.items())
           if n not in set(ly.values())]
    out += [{"kind": "holiday", "label": f"{n} (in last year's comparison weeks, not this year's)", "date": d}
            for d, n in sorted(ly.items()) if n not in set(now.values())]
    return out


def find_concurrent(r, start, end, db_path=DB_PATH, read_windows=None):
    """Every other change that could move this tracker's number:
    [{kind, label, date}] with kind one of tracker | accepted_rec |
    price_change | event | holiday | closure | sales_move. Any one of them
    caps the attribution at "associated" (grade).

    Lasting changes (another tracker, an accepted recommendation, a price)
    count anywhere in [start, end]. One-day ones (an event, a holiday, a
    closure) count only inside the windows actually read — `read_windows`,
    default [(start, end)] — since a party between the evaluation and the
    re-check moved neither reading. A number that is a share of sales
    (labor %, food cost %, comps, voids) also lists a sales move past its
    band in a read window, and any sales tracker over the same weeks
    (re-audit A5). A change the owner said they never made (a disowned
    tracker) is not a change (re-audit A23)."""
    fam = metrics.family(r["metric"])
    rid = r["restaurant_id"]
    s, e = _iso(start), _iso(end)
    windows = [(_iso(a), _iso(b)) for a, b in (read_windows or [(s, e)])]
    cost = fam in _COST_FAMILIES

    def _read(d):
        return any(a <= d <= b for a, b in windows)
    out = []
    conn = get_conn(db_path)
    try:
        tracker_keys = set()
        for o in conn.execute(
                "SELECT id, source_key, title, metric, started_on, evaluate_on, after_end, owner_checkin FROM "
                "recommendation_outcomes WHERE restaurant_id=? AND id!=? AND status IN ('tracking','evaluated') "
                "AND started_on<=?", (rid, r.get("id") or 0, e)).fetchall():
            if _informational_key(o["source_key"]):
                continue        # reading an alert (or advice not taken) is not a change
            ofam = metrics.family(o["metric"])
            if ofam != fam and not (cost and ofam == "sales"):
                continue
            o_end = _iso(o["after_end"]) or (_day(o["evaluate_on"]) - timedelta(days=1)).isoformat()
            if o_end < s:
                continue
            tracker_keys.add(o["source_key"])
            if disowned(dict(o)):
                continue        # the owner said this change was never made
            out.append({"kind": "tracker", "label": owner_title(o["title"]), "date": _iso(o["started_on"])})
        try:
            # A range on the stored timestamp, not date(e.at): the index on
            # (restaurant_id, event, at) can serve it (re-audit A37).
            events = conn.execute(
                "SELECT e.key, e.at, i.module, i.expected_metric, i.title FROM rec_events e "
                "LEFT JOIN rec_instances i ON i.rec_id = e.rec_id WHERE e.restaurant_id=? "
                "AND e.event IN ('accepted','completed') AND e.at>=? AND e.at<? "
                "ORDER BY e.at", (rid, s, _next_day(e))).fetchall()
        except Exception as ex:
            print(f"[outcomes] rec events unreadable for {rid}: {ex}")
            events = []
        seen = set()
        reprice = r.get("source") == "reprice"
        for ev in events:
            key = ev["key"]
            if key == r.get("source_key") or key in tracker_keys or key in seen:
                continue
            if reprice and str(key or "").split(":", 1)[0] == "reprice":
                # The month's reprice tracker IS its repricing: its own
                # accepted reprices are not another change (re-audit A10).
                continue
            if _rec_family(key, ev["module"], ev["expected_metric"]) != fam:
                continue
            seen.add(key)
            out.append({"kind": "accepted_rec", "label": owner_title(ev["title"] or key),
                        "date": _iso(ev["at"])})
        # The reprice tracker IS the price change; every other tracker on a
        # price-moved number sees the prices that moved under it.
        if fam in _PRICE_FAMILIES and not reprice:
            try:
                for p in conn.execute("SELECT dish, created_at FROM reprice_decisions WHERE restaurant_id=? "
                                      "AND created_at>=? AND created_at<? ORDER BY created_at",
                                      (rid, s, _next_day(e))).fetchall():
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
        # hold one. A "same weeks last year" baseline holds last year's —
        # compared by NAME, since Easter moves; any other baseline is
        # balanced when a read window holds no more holidays than it does.
        if (r.get("baseline_kind") or "prior window") == "same weeks last year":
            for a, b in windows:
                out.extend(_ly_holiday_gaps(a, b))
        else:
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
    if cost:
        for a, b in windows:
            try:
                mv = _sales_move(r, a, b, db_path)
            except Exception as ex:
                print(f"[outcomes] sales move unreadable for {rid}: {ex}")
                mv = None
            if mv:
                out.append(mv)
    seen, unique = set(), []
    for c in sorted(out, key=lambda c: (c["date"], c["kind"], c["label"] or "")):
        k = (c["kind"], c["date"], c["label"])
        if k not in seen:
            seen.add(k)
            unique.append(c)
    return unique


# ── a trend already under way (re-audit A13) ────────────────────────────────

TREND_LABEL = "Already moving this way before the change"


def pre_trend(r, verdict, after_value, db_path=DB_PATH):
    """A concurrent-change entry (kind "trend") when the number was already
    moving the way it moved, before the change started, by enough to
    explain the move — else None.

    Sales drifting up 0.4% a day into summer, with nothing changed, read as
    "improved clearly" and then "held" at 90 days (re-audit A13). The
    baseline window is read in two halves (whole weeks where it can be);
    the half-to-half move is carried forward to the after-window's middle,
    and when the after reading is not past the noise band beyond that
    projection, the move is the trend's as much as the change's. A "same
    weeks last year" baseline is already moved by last year's same weeks
    and is not tested again."""
    if verdict not in _MOVED or after_value is None:
        return None
    if (r.get("baseline_kind") or "prior window") == "same weeks last year":
        return None
    if not (r.get("baseline_start") and r.get("baseline_end") and r.get("started_on")):
        return None
    base = r.get("baseline_raw") if r.get("baseline_raw") is not None else r.get("baseline_value")
    if base is None:
        return None
    bs, be = _day(r["baseline_start"]), _day(r["baseline_end"])
    n = (be - bs).days + 1
    if n < 14:
        return None
    half = (n // 14) * 7 if n % 7 == 0 else n // 2
    first = (bs, bs + timedelta(days=half - 1))
    second = (be - timedelta(days=half - 1), be)
    rid = r["restaurant_id"]
    v1, _ = _measure(rid, r["metric"], first[0].isoformat(), first[1].isoformat(), db_path)
    v2, _ = _measure(rid, r["metric"], second[0].isoformat(), second[1].isoformat(), db_path)
    if v1 is None or v2 is None or v2 == v1:
        return None
    moved_up = float(after_value) > float(base)
    if (v2 > v1) != moved_up:
        return None             # it was moving the other way, if at all
    step = (second[0] - first[0]).days
    window = _window_days(r)
    base_mid = bs + timedelta(days=(n - 1) / 2.0)
    after_mid = _day(r["started_on"]) + timedelta(days=(window - 1) / 2.0)
    lead = (after_mid - base_mid).total_seconds() / 86400.0
    projected = float(base) + (float(v2) - float(v1)) * (lead / step if step else 0.0)
    _exp, scale, _k = expected_for(r, r["started_on"], _after_end(r), db_path)
    if _cmp(r, projected, after_value, scale)["verdict"] == verdict:
        return None             # past the band beyond the trend: not explained by it
    return {"kind": "trend", "label": TREND_LABEL, "date": _iso(r["baseline_start"])}


# ── a level shift inside the trigger window (confidence re-audit B2 #10) ────

LEVEL_SHIFT_LABEL = "Already at this level before the change started"
# The share of the after-window's move the trigger window must already hold
# before the move is read as the trigger's level carried on.
LEVEL_SHIFT_MIN_SHARE = 0.5


def level_shift(r, verdict, after_value, db_path=DB_PATH):
    """A concurrent-change entry (kind "level_shift") when the number had
    already moved to where it was read, inside the window that PROMPTED the
    recommendation — before the change started — else None.

    A triggered tracker is read against the mirror of the after-window
    about its trigger window (TRIGGER_BASELINE_KIND), which assumes the
    number returns to where it was. A lasting step at the trigger (a wage
    rise, a new lease on a cost) never returns: with nothing changed, 35.5%
    of such trackers read "worsened" (probe B2 p2 S7). The step test: the
    trigger window's reading already sits on the move's side of the
    baseline, holding at least LEVEL_SHIFT_MIN_SHARE of the move, and the
    after-window is not past the noise band beyond the trigger reading —
    the number did not move after the change, it stayed where the trigger
    put it. Such a result is shown and never counted (result_counts)."""
    if verdict not in _MOVED or after_value is None:
        return None
    tv = r.get("trigger_value")
    if tv is None or not r.get("trigger_start"):
        return None
    expected, scale, _k = expected_for(r, r["started_on"], _after_end(r), db_path)
    if expected is None:
        return None
    move = float(after_value) - float(expected)
    held = float(tv) - float(expected)
    if move == 0 or (held > 0) != (move > 0) or abs(held) < LEVEL_SHIFT_MIN_SHARE * abs(move):
        return None
    if _cmp(r, tv, after_value, scale)["verdict"] == verdict:
        return None             # moved on past the trigger's level: the after-window moved by itself
    return {"kind": "level_shift", "label": LEVEL_SHIFT_LABEL, "date": _iso(r["trigger_start"])}


def _own_confounders(r, verdict, after_value, db_path=DB_PATH):
    """The confounders read from the tracker's own series — a trend already
    under way (pre_trend) and a level shift at the trigger (level_shift) —
    as concurrent-change entries. evaluate and recheck both append them."""
    out = []
    for fn in (pre_trend, level_shift):
        try:
            c = fn(r, verdict, after_value, db_path)
        except Exception as ex:
            print(f"[outcomes] {fn.__name__} unreadable for tracker {r.get('id')}: {ex}")
            c = None
        if c:
            out.append(c)
    return out


def _cmp(r, expected, value, scale=1.0):
    """metrics.compare for one tracker, with the restaurant's own noise band
    stored on it at the start (CA2 #3); a row from before bands were stored
    reads against the stated band alone."""
    return metrics.compare(r["metric"], expected, value, band_scale=scale, band=r.get("noise_band"))


# ── attribution: how strongly a move can be tied to the change (#23) ────────

def grade(verdict, multiple, concurrent, recheck_verdict=None, checked=True, checkin=None, overlaps=False) -> str:
    """none | associated | consistent | held. Never a claim of cause.

    none        no clear change, or nothing could be measured.
    associated  past the noise band once — or any size of move with another
                change on the same number in the window (a concurrent change
                caps it here), or a window never checked for one, or a result
                the owner's check-in says can't be separated (they said
                something else changed, or that they never made the change).
    consistent  at least CONSISTENT_MULTIPLE bands, nothing else changing on
                the same number in the window.
    held        still past the band when re-checked, nothing else changing.

    THE one grading rule (re-audit A4): evaluate, recheck and apply_checkin
    all grade through here with the check-in in hand. A check-in saying
    conditions changed used to be undone by the 90-day re-check ("held"),
    a "no" still read validated, and a check-in given while the tracker was
    running was ignored by its evaluation.
    """
    if verdict not in _MOVED:
        return "none"
    ck = checkin if isinstance(checkin, dict) else {}
    if (concurrent or not checked or overlaps or ck.get("conditions_changed")
            or ck.get("did_it") == "no"):
        return "associated"
    if recheck_verdict == "held":
        return "held"
    if multiple is not None and multiple >= CONSISTENT_MULTIPLE:
        return "consistent"
    return "associated"


def regrade(r, db_path=DB_PATH):
    """A stored result's grade, recomputed from what is stored: its first
    reading's size in noise bands, the other changes found, its re-check and
    the owner's check-in. A tracking row keeps what it has (None)."""
    if r.get("status") != "evaluated":
        return r.get("attribution")
    if r.get("verdict") not in _MOVED:
        return "none"
    first, scale, _k = expected_for(r, r["started_on"], _after_end(r), db_path)
    cmp = _cmp(r, first, r.get("after_value"), scale)
    conc = _concurrent_list(r)
    return grade(r["verdict"], cmp["multiple"], conc, recheck_verdict=r.get("recheck_verdict"),
                 checked=conc is not None, checkin=_checkin_of(r), overlaps=overlaps_trigger(r))


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
    if overlaps_trigger(r):
        return (f"{moved} over these weeks, but it could only be measured against the same weeks that prompted "
                f"the recommendation. A number read right after an unusually bad stretch tends to come back on "
                f"its own, so this result isn't counted either way.")
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
        trend = [c for c in conc if c.get("kind") == "trend"]
        shift = [c for c in conc if c.get("kind") == "level_shift"]
        others = [c for c in conc if c.get("kind") not in ("trend", "level_shift")]
        parts = []
        if others:
            names = ", ".join(c.get("label") or c.get("kind") for c in others[:2])
            more = f" and {len(others) - 2} more" if len(others) > 2 else ""
            parts.append(f"other changes on this number fell in the same weeks ({names}{more})")
        if trend:
            parts.append("it was already moving this way in the weeks before the change started")
        if shift:
            parts.append("it was already at this level in the weeks that prompted the recommendation, "
                         "before the change started")
        s = (f"{moved} alongside the change, but {' and '.join(parts)}, so it can't be separated from "
             f"{'them' if others else 'that'} and isn't counted either way. {CAUSATION_CAVEAT}")
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
            and r.get("attribution") == "held" and result_counts(r))


def overlaps_trigger(r) -> bool:
    """The result was read against a baseline overlapping the window that
    triggered its recommendation (CA2 #1): shown, never counted."""
    try:
        return bool(int((r or {}).get("baseline_overlaps_trigger") or 0))
    except (TypeError, ValueError):
        return bool((r or {}).get("baseline_overlaps_trigger"))


def conditions_changed(r) -> bool:
    """The owner's check-in said something else changed in those weeks."""
    c = _checkin_of(r or {})
    return bool(c and c.get("conditions_changed"))


def confounded(r) -> bool:
    """Something besides the recommendation could have moved this number in
    the weeks it was read (confidence re-audit B2 #5, #10): another change
    on the same number (find_concurrent — a tracker, an accepted
    recommendation, a price, an event, a holiday, a closure, a sales move),
    a trend already under way (pre_trend) or a level shift at the trigger
    (level_shift). Read from the stored `concurrent` list, as evaluate and
    recheck wrote it; a row never checked (NULL) is not confounded by this
    rule (its grade is capped at "associated" instead)."""
    conc = _concurrent_list(r or {})
    return bool(conc and any(isinstance(c, dict) for c in conc))


def result_counts(r) -> bool:
    """Whether an evaluated result is admitted as a measurement of the
    recommendation at all — THE rule learning (rec_learning.learned_verdict)
    and value (counts_in_delivered) share (CA2 #7): not an alert read, a
    routine order or advice not taken (informational), not a change the
    owner said they never made or that something else changed alongside
    (their check-in), not one measured against the window that triggered it
    (baseline_overlaps_trigger), and not one read alongside another change
    on the same number, a trend already under way or a level shift at the
    trigger (confounded — re-audit B2 #5: 46 of 89 "wins" on a do-nothing
    drifting restaurant carried the trend flag and still counted)."""
    return ((r or {}).get("status") == "evaluated" and not is_informational(r) and not disowned(r)
            and not conditions_changed(r) and not overlaps_trigger(r) and not confounded(r))


def counts_in_delivered(r) -> bool:
    """An evaluated move that still counts: admitted as a measurement
    (result_counts) and not faded or reversed at its re-check (#33). A result
    counts here exactly when rec_learning counts it as improved or worsened
    (the learning == value test)."""
    return (result_counts(r) and r.get("verdict") in _MOVED
            and r.get("recheck_verdict") not in _FAILED_RECHECK)


# How a result's attribution grade reads in one clause, wherever a result is
# quoted in words (outcomes.summarise, decisions, the emails — CA1 red flag
# 19). "held" is the only grade that may say it held.
GRADE_PHRASES = {
    "held": "it held when re-checked, with nothing else changing on this number",
    "consistent": "a clear move (more than twice normal variation), with nothing else changing on this number",
    "associated_other": "other changes fell in the same weeks, so it can't be separated from them",
    "associated_once": "past normal variation once — not yet a clear result",
}


def grade_phrase(r) -> str:
    """The attribution grade of a moved result as one clause, or None."""
    if r.get("status") != "evaluated" or r.get("verdict") not in _MOVED:
        return None
    a = r.get("attribution")
    if a in ("held", "consistent"):
        return GRADE_PHRASES[a]
    if a == "associated":
        other = [c for c in (r.get("concurrent") if isinstance(r.get("concurrent"), list)
                             else (_concurrent_list(r) or [])) if isinstance(c, dict)]
        if other or conditions_changed(r):
            return GRADE_PHRASES["associated_other"]
        return GRADE_PHRASES["associated_once"]
    return None


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


def _aligned_window(r, end_limit):
    """(start, end) dates of the latest window of the tracker's own length
    ending on or before `end_limit`. A whole-weeks window holds the same
    weekday mix wherever it falls; a window of another length (a manual
    30-day tracker from before whole weeks, re-audit A21) is placed a whole
    number of weeks from the after-window's start, so it holds the weekdays
    the after-window — and its matched baseline — held."""
    window = _window_days(r)
    end_limit = end_limit if isinstance(end_limit, date) else _day(end_limit)
    if window % 7 == 0 or metrics.parse(r["metric"])[0] not in metrics.WEEKDAY_MIX_METRICS:
        return end_limit - timedelta(days=window - 1), end_limit
    started = _day(r["started_on"])
    k = max(0, ((end_limit - timedelta(days=window - 1)) - started).days // 7)
    start = started + timedelta(days=7 * k)
    return start, start + timedelta(days=window - 1)


def evaluate(outcome_id, db_path=DB_PATH, today=None):
    """Re-measure one tracker whose window has closed and store the verdict,
    the after-value and % change, the other changes in the window, the
    attribution grade and the re-check date; then accrue the window's
    measured dollars (outcome_value_days)."""
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT * FROM recommendation_outcomes WHERE id=?", (outcome_id,)).fetchone()
    finally:
        conn.close()
    if not r or r["status"] != "tracking":
        return None
    r = dict(r)
    today = today or local_today(r["restaurant_id"], db_path)
    started = _day(r["started_on"])
    ev = _day(r["evaluate_on"])
    if today < ev:
        return None
    after_start, after_end = started, ev - timedelta(days=1)
    after, after_detail = _measure(r["restaurant_id"], r["metric"], after_start.isoformat(),
                                   after_end.isoformat(), db_path)
    expected, scale, _kind = expected_for(r, after_start.isoformat(), after_end.isoformat(), db_path)
    cmp = _cmp(r, expected, after, scale)
    # Priced on the after-window's own sales and trading days (re-audit A1).
    dollars = (metrics.monthly_dollars(r["restaurant_id"], r["metric"], cmp["delta"], db_path,
                                       window=(after_start.isoformat(), after_end.isoformat()))
               if cmp["verdict"] in _MOVED else None)
    concurrent = find_concurrent(r, after_start.isoformat(), after_end.isoformat(), db_path)
    concurrent.extend(_own_confounders(r, cmp["verdict"], after, db_path))
    attribution = grade(cmp["verdict"], cmp["multiple"], concurrent, checkin=_checkin_of(r),
                        overlaps=overlaps_trigger(r))
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


def evaluate_due(restaurant_id=None, db_path=DB_PATH, today=None, max_seconds=None):
    """Evaluate every tracker whose window has closed. Scheduler entry point
    (strategy_jobs.run_outcome_evaluations calls it once per restaurant,
    bounded and resumable, with that restaurant's own date).

    Without a restaurant, `today` defaults to the latest local date anywhere
    (a day ahead of UTC at most) and each tracker is still only evaluated
    once ITS restaurant's date has reached evaluate_on. `max_seconds` bounds
    one pass by wall clock; oldest first, and an evaluated tracker leaves
    the set, so the next pass carries on from where this one stopped.

    For one restaurant it first starts the informational trackers of
    recently settled advice not taken (observe_untaken, CA2 #11) — the same
    daily per-restaurant pass, so they need no job of their own."""
    local = today or (local_today(restaurant_id, db_path) if restaurant_id is not None
                      else date.today() + timedelta(days=1))
    if restaurant_id is not None:
        try:
            observe_untaken(restaurant_id, db_path=db_path, today=local)
        except Exception as e:
            print(f"[outcomes] untaken trackers skipped for {restaurant_id}: {e}")
    conn = get_conn(db_path)
    try:
        sql = ("SELECT id, restaurant_id FROM recommendation_outcomes WHERE status='tracking' AND evaluate_on<=?")
        args = [local.isoformat()]
        if restaurant_id is not None:
            sql += " AND restaurant_id=?"
            args.append(restaurant_id)
        rows = [(r["id"], r["restaurant_id"]) for r in conn.execute(sql + " ORDER BY id", args).fetchall()]
    finally:
        conn.close()
    done = []
    began = time.monotonic()
    for oid, rid in rows:
        if max_seconds is not None and time.monotonic() - began > max_seconds:
            break
        try:
            if today is None and restaurant_id is None:
                # Each tracker on its own restaurant's calendar (re-audit A8).
                out = evaluate(oid, db_path=db_path, today=local_today(rid, db_path))
                if out:
                    done.append(out)
                continue
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
    again over the whole span, for rows evaluated before they were recorded.
    The grade goes through grade() with the owner's check-in, so a "held"
    re-check never lifts a result the owner said can't be separated."""
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT * FROM recommendation_outcomes WHERE id=?", (outcome_id,)).fetchone()
    finally:
        conn.close()
    if not r:
        return None
    r = dict(r)
    today = today or local_today(r["restaurant_id"], db_path)
    if (r["status"] != "evaluated" or r["verdict"] not in _MOVED or r.get("recheck_verdict")
            or not r.get("recheck_on") or today < _day(r["recheck_on"]) or is_informational(r)):
        return None
    start, end = _aligned_window(r, _day(r["recheck_on"]) - timedelta(days=1))
    value, _detail = _measure(r["restaurant_id"], r["metric"], start.isoformat(), end.isoformat(), db_path)
    expected, scale, _kind = expected_for(r, start.isoformat(), end.isoformat(), db_path)
    cmp = _cmp(r, expected, value, scale)
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
    concurrent.extend(_own_confounders(r, r["verdict"], r.get("after_value"), db_path))
    first, _s, _k = expected_for(r, r["started_on"], _after_end(r), db_path)
    eval_cmp = _cmp(r, first, r.get("after_value"), _s)
    attribution = grade(r["verdict"], eval_cmp["multiple"], concurrent, recheck_verdict=rv,
                        checkin=_checkin_of(r), overlaps=overlaps_trigger(r))
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
    today = today or (local_today(restaurant_id, db_path) if restaurant_id is not None else date.today())
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
# on a measured day the move did not hold. ONE row per restaurant, family
# and day is `counted` — the broadest reading of the family (metrics.BREADTH)
# that held, then the largest: waste and food cost improving over the same
# days are one saving, not two (#4), and a labor % win with an overtime loss
# over the same days is the labor % reading, not the win less the overtime
# premium a second time (re-audit A7). cumulative() elects the same way at
# read time, so a row's `counted` and the sum can never disagree.

def _unit_amount(r, monthly, per=None):
    """A monthly figure as one measured day's share. `per` is how many of the
    metric's days make a month (metrics.days_per_month): trading days for a
    sales-priced metric (re-audit A1), one weekday's recurrences for a
    weekday metric, calendar days otherwise."""
    if per is None:
        base, _ = metrics.parse(r["metric"])
        per = metrics.WEEKS_PER_MONTH if base == "weekday_sales" else metrics.DAYS_PER_MONTH
    return round(float(monthly) / float(per), 4)


def _accrues(r) -> bool:
    """A result accrues measured days only while it is admitted as a
    measurement (result_counts): a check-in saying something else changed
    stops it as a "no" does (CA2 #7). A failed re-check stops it at the
    re-check (accrue_daily)."""
    return (r.get("verdict") in _MOVED and r.get("dollars_monthly") not in (None, 0) and result_counts(r))


def _breadth(metric) -> int:
    return metrics.BREADTH.get(metrics.parse(metric)[0], 0)


def _put_day(conn, r, day, amount, held, basis):
    """One measured day for one tracker, counted only if it is the broadest,
    then the largest, held reading of its family that day."""
    if conn.execute("SELECT 1 FROM outcome_value_days WHERE outcome_id=? AND day=?",
                    (r["id"], day)).fetchone():
        return False
    fam = metrics.family(r["metric"])
    sign = 1 if r["verdict"] == "improved" else -1
    counted = 0
    if held and amount:
        cur = conn.execute("SELECT outcome_id, metric, dollars FROM outcome_value_days WHERE restaurant_id=? AND "
                           "family=? AND day=? AND counted=1",
                           (r["restaurant_id"], fam, day)).fetchall()
        if not cur:
            counted = 1
        else:
            mine = (_breadth(r["metric"]), -abs(float(amount)))
            if all(mine < (_breadth(c["metric"]), -abs(float(c["dollars"] or 0))) for c in cur):
                conn.execute("UPDATE outcome_value_days SET counted=0 WHERE restaurant_id=? AND family=? "
                             "AND day=? AND counted=1", (r["restaurant_id"], fam, day))
                counted = 1
    conn.execute("INSERT INTO outcome_value_days (outcome_id, restaurant_id, day, module, metric, family, "
                 "sign, dollars, held, counted, basis) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (r["id"], r["restaurant_id"], day, module_of_row(r), r["metric"], fam, sign,
                  round(float(amount or 0), 4) if held else 0.0, 1 if held else 0, counted, basis))
    return True


def _measured_days(r, start, end, db_path):
    """The days in [start, end] a move on r's metric was measured on
    (metrics.accrual_days) — every calendar day for a metric read over
    calendar days."""
    days = metrics.accrual_days(r["restaurant_id"], r["metric"], _iso(start), _iso(end), db_path)
    if days is None:
        s = _day(start)
        days = [(s + timedelta(days=i)).isoformat() for i in range((_day(end) - s).days + 1)]
    return days


def accrue_window(r, db_path=DB_PATH):
    """Accrue an evaluated move's after-window: each measured day of it at
    the evaluated monthly figure's daily share. A day without data — a
    closed Monday, a day comps were never synced — was not measured and
    accrues nothing, and a sales-priced figure's share is per TRADING day,
    so the window sums to what the days actually moved (re-audit A1)."""
    if not r or r.get("status") != "evaluated":
        return 0
    after_end = _after_end(r)
    written = 0
    conn = get_conn(db_path)
    try:
        if _accrues(r):
            days = _measured_days(r, r["started_on"], after_end, db_path)
            per = metrics.days_per_month(r["restaurant_id"], r["metric"], r["started_on"], after_end, db_path)
            if days and per:
                amount = _unit_amount(r, r["dollars_monthly"], per)
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
    ending that day (whole weeks, _aligned_window), or None when the day was
    not measured or its window was too thinly measured (MIN_COVERAGE)."""
    day = d.isoformat()
    measured = metrics.accrual_days(r["restaurant_id"], r["metric"], day, day, db_path)
    if measured is not None and not measured:
        return None
    s, e = _aligned_window(r, d)
    start, end = s.isoformat(), e.isoformat()
    expected, scale, _k = expected_for(r, start, end, db_path)
    value, _ = _measure(r["restaurant_id"], r["metric"], start, end, db_path)
    cmp = _cmp(r, expected, value, scale)
    if cmp["verdict"] == "unknown":
        return None
    if cmp["verdict"] != r["verdict"]:
        return (False, 0.0)
    monthly = metrics.monthly_dollars(r["restaurant_id"], r["metric"], cmp["delta"], db_path, window=(start, end))
    per = metrics.days_per_month(r["restaurant_id"], r["metric"], start, end, db_path)
    if monthly is None or not per:
        return None
    # Never more than the evaluated figure: a number that kept moving after
    # the window is more likely something else than more of the change.
    capped = min(abs(float(monthly)), abs(float(r["dollars_monthly"])))
    return (True, _unit_amount(r, capped if r["verdict"] == "improved" else -capped, per))


def accrue_daily(r, db_path=DB_PATH, today=None):
    """Accrue the days after the window, one measured day at a time, while
    the move still holds that day — up to ACCRUAL_HORIZON_DAYS from the
    start, and never past a failed re-check. Returns rows written."""
    if not _accrues(r) or not r.get("accrued_through"):
        return 0
    today = today or local_today(r["restaurant_id"], db_path)
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
    today = today or local_today(restaurant_id, db_path)
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


def _hidden_row(r, denied=None, exclude_metrics=None, exclude_ids=None) -> bool:
    """A result a viewer may not see: a denied module, a metric it may not
    read (comps and voids without LOSS_VIEW), or a tracker whose
    recommendation it may not see (value_delivered.viewer_scope). Applied
    BEFORE anything is summed (re-audit A26)."""
    if _denied_row(r, denied):
        return True
    if exclude_metrics and metrics.parse(r.get("metric"))[0] in {metrics.parse(m)[0] for m in exclude_metrics}:
        return True
    return bool(exclude_ids) and r.get("id") in set(exclude_ids)


_BASE_SQL = "CASE WHEN instr(v.metric, ':') > 0 THEN substr(v.metric, 1, instr(v.metric, ':') - 1) ELSE v.metric END"
SALES_LIFT_BASIS = ("gross revenue — sales through the till, not profit — measured before and after; kept apart "
                    "from the savings and never added to them")


def cumulative(restaurant_id, db_path=DB_PATH, denied_modules=None, since=None, exclude_metrics=None,
               exclude_ids=None) -> dict:
    """Measured dollars accrued so far, net of changes that got worse: a SUM
    of measured days, never a monthly figure times months. `total` is None
    when no day has been measured yet (nothing measured is not $0).
    `days` counts days on which a counted move held; `measured_days` every
    day read. `since` (ISO date) keeps only the days on or after it — the
    owner report's "over the past 6 months" (owner_report.what_worked);
    `exclude_metrics` drops metrics a viewer may not read (comps and voids
    without LOSS_VIEW) and `exclude_ids` trackers whose recommendation it
    may not see, before anything is summed.

    Summed in SQL (re-audit A35), electing at read time one reading per
    family and day — the broadest that held, then the largest (_put_day's
    rule) — from trackers still admitted as measurements (_COUNTS_SQL, the
    SQL twin of result_counts: not disowned, no "something else changed"
    check-in, not measured against its trigger window). `by_grade` splits
    the same counted days into consistent_or_held and associated (CA2 #7),
    side by side. Two more rules:
      * SALES ARE NOT SAVINGS (re-audit A6). A sales lift is gross revenue;
        it is summed apart, in `sales_lift`, and never into `total`.
      * A SALES MOVE IS NOT A COST SAVING (re-audit A5). A labor, food-cost,
        comp or void day is not counted on a day a sales reading of the same
        direction is: the share of sales fell because sales rose.
    """
    denied = set(denied_modules or ())
    excluded = {metrics.parse(m)[0] for m in (exclude_metrics or ())}
    denied_bases = {b for b, m in METRIC_MODULE.items() if m in denied}
    where = ["v.restaurant_id=?", _COUNTS_SQL]
    args = [restaurant_id, _DISOWNED_LIKE, _CHANGED_LIKE]
    if since:
        where.append("v.day >= ?")
        args.append(str(since)[:10])
    if denied:
        where.append(f"COALESCE(v.module, 'other') NOT IN ({','.join('?' for _ in denied)})")
        args += sorted(denied)
    bases = sorted(excluded | denied_bases)
    if bases:
        where.append(f"{_BASE_SQL} NOT IN ({','.join('?' for _ in bases)})")
        args += bases
    ids = sorted({int(i) for i in (exclude_ids or ()) if i is not None})
    if ids:
        where.append(f"v.outcome_id NOT IN ({','.join('?' for _ in ids)})")
        args += ids
    narrow = sorted(metrics.BREADTH)
    cost = sorted(_COST_FAMILIES)
    cte = (
        "WITH v AS (SELECT v.day, COALESCE(v.module, 'other') AS module, v.family, v.dollars, v.held, "
        "CASE WHEN o.attribution IN ('consistent','held') THEN 'consistent_or_held' ELSE 'associated' END AS grade, "
        f"v.outcome_id, CASE WHEN {_BASE_SQL} IN ({','.join('?' for _ in narrow)}) THEN 1 ELSE 0 END AS rk "
        "FROM outcome_value_days v JOIN recommendation_outcomes o ON o.id = v.outcome_id "
        f"WHERE {' AND '.join(where)}), "
        "pick AS (SELECT *, ROW_NUMBER() OVER (PARTITION BY family, day ORDER BY rk, ABS(dollars) DESC, "
        "outcome_id) AS rn FROM v WHERE held = 1 AND dollars <> 0), "
        "chosen AS (SELECT * FROM pick WHERE rn = 1), "
        # The days a sales reading moved each way: a cost share that moved
        # the same way that day is not counted (a join, not a correlated
        # subquery per row).
        "sales_days AS (SELECT day, MAX(dollars > 0) AS up, MAX(dollars < 0) AS down FROM chosen "
        "WHERE family = 'sales' GROUP BY day), "
        "final AS (SELECT c.* FROM chosen c LEFT JOIN sales_days s ON s.day = c.day "
        f"WHERE NOT (c.family IN ({','.join('?' for _ in cost)}) AND "
        "((c.dollars > 0 AND COALESCE(s.up, 0) = 1) OR (c.dollars < 0 AND COALESCE(s.down, 0) = 1)))) ")
    cargs = narrow + args + cost
    conn = get_conn(db_path)
    try:
        measured = {bool(r["sl"]): dict(r) for r in conn.execute(
            cte + "SELECT family = 'sales' AS sl, COUNT(DISTINCT day) AS n, MIN(day) AS a, MAX(day) AS b "
                  "FROM v GROUP BY sl", cargs).fetchall()}
        # One pass over the counted days: summed by module in SQL, the
        # distinct counted days taken from the same rows.
        sums, counted_days = [], {}
        for r in conn.execute(
                cte + "SELECT family = 'sales' AS sl, module, "
                      "ROUND(SUM(CASE WHEN dollars > 0 THEN dollars ELSE 0 END), 4) AS g, "
                      "ROUND(SUM(CASE WHEN dollars < 0 THEN -dollars ELSE 0 END), 4) AS l, MIN(day) AS a, "
                      "MAX(day) AS b, group_concat(DISTINCT day) AS ds FROM final GROUP BY sl, module",
                cargs).fetchall():
            r = dict(r)
            counted_days.setdefault(bool(r["sl"]), set()).update((r.pop("ds") or "").split(","))
            sums.append(r)
        counted_days = {k: len(v - {""}) for k, v in counted_days.items()}
        # The same counted days by attribution grade (CA2 #7): a result tied
        # to other changes ("associated") is reported apart from one that
        # was clear or held — never summed into it.
        grades = {}
        for r in conn.execute(cte + "SELECT family = 'sales' AS sl, grade, ROUND(SUM(dollars), 4) AS net "
                                    "FROM final GROUP BY sl, grade", cargs).fetchall():
            grades.setdefault(bool(r["sl"]), {})[r["grade"]] = round(float(r["net"] or 0), 2)
    finally:
        conn.close()

    def _part(sl):
        m = measured.get(sl)
        rows = [s for s in sums if bool(s["sl"]) == sl]
        gained = sum(float(s["g"] or 0) for s in rows)
        lost = sum(float(s["l"] or 0) for s in rows)
        by_module = {s["module"]: round(float(s["g"] or 0) - float(s["l"] or 0), 2) for s in rows}
        firsts = [s["a"] for s in rows if s["a"]]
        lasts = [s["b"] for s in rows if s["b"]]
        return {
            "total": round(gained - lost, 2) if m else None,
            "gained": round(gained, 2), "lost": round(lost, 2),
            "since": min(firsts) if firsts else (m["a"] if m else None),
            "until": max(lasts) if lasts else (m["b"] if m else None),
            "days": int(counted_days.get(sl) or 0),
            "measured_days": int(m["n"]) if m else 0,
            "by_module": by_module,
            "by_grade": {"consistent_or_held": (grades.get(sl) or {}).get("consistent_or_held", 0.0),
                         "associated": (grades.get(sl) or {}).get("associated", 0.0)},
        }
    out = _part(False)
    lift = _part(True)
    out["sales_lift"] = dict(lift, basis=SALES_LIFT_BASIS) if lift["measured_days"] else None
    out["basis"] = ("summed over days actually measured, only while each change held, net of changes that got "
                    f"worse; related numbers over the same days count once; each change for up to "
                    f"{ACCRUAL_HORIZON_DAYS} days from when it started; sales lifts are kept apart, and a cost "
                    f"share that fell only because sales rose is not counted as a saving")
    return out


def apply_checkin(restaurant_id, tracker_id, did_it, conditions_changed, db_path=DB_PATH, at=None):
    """The owner's check-in on a tracked change (rec-ROI #21), applied to its
    result. The latest answer wins and can be changed back.

    - "no" (the change was not made): the result is neither a win nor a loss
      of this recommendation's — it stops counting in Delivered and its
      measured days are released, so another change on the same family that
      day is counted instead (the one-per-family rule, #4).
    - conditions_changed: the grade is capped at "associated", like any other
      change in the same weeks (#31), and — as learning already read it
      (rec_learning.learned_verdict) — the result stops counting in Delivered
      and its days are released, as for a "no" (CA2 #7: learning and value
      count the same results).
    - "yes"/"partly" with nothing else changed, after either: the days are
      re-accrued by the next pass.
    The grade is recomputed from the stored result by the one grading rule
    (regrade -> grade, re-audit A4/A22): a "yes" after a "held" re-check is
    held again, and a check-in given while the tracker was still running is
    read by its evaluation. Returns the updated row, or None when the
    tracker is not this restaurant's."""
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM recommendation_outcomes WHERE id=? AND restaurant_id=?",
                           (tracker_id, restaurant_id)).fetchone()
        if not row:
            return None
        r = dict(row)
        before = _checkin_of(r) or {}
        # "No" and "something else changed" both take the result out of
        # delivered value, as they take it out of learning (CA2 #7).
        was_discounted = before.get("did_it") == "no" or bool(before.get("conditions_changed"))
        ck = {"did_it": did_it, "conditions_changed": bool(conditions_changed),
              "at": at or datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")}
        discounted = did_it == "no" or bool(conditions_changed)
        r["owner_checkin"] = ck
        grade_now = regrade(r, db_path) if r.get("status") == "evaluated" else r.get("attribution")
        conn.execute("UPDATE recommendation_outcomes SET owner_checkin=?, attribution=? WHERE id=?",
                     (json.dumps(ck), grade_now, r["id"]))
        if discounted and not was_discounted:
            _release_days(conn, r)
        elif not discounted and was_discounted:
            # Re-accrued from the start by the next accrual pass.
            conn.execute("DELETE FROM outcome_value_days WHERE outcome_id=?", (r["id"],))
            conn.execute("UPDATE recommendation_outcomes SET accrued_through=NULL WHERE id=?", (r["id"],))
        conn.commit()
    finally:
        conn.close()
    return get_outcome(tracker_id, db_path=db_path)


def _release_days(conn, r):
    """Stop counting a disowned change's measured days, and on each day it was
    the counted row of its family, count the next one instead — the
    broadest, then the largest, held reading of a change still owned."""
    days = conn.execute("SELECT day, family FROM outcome_value_days WHERE outcome_id=? AND counted=1",
                        (r["id"],)).fetchall()
    conn.execute("UPDATE outcome_value_days SET counted=0 WHERE outcome_id=?", (r["id"],))
    for d in days:
        rows = conn.execute(
            "SELECT v.outcome_id, v.metric, v.dollars FROM outcome_value_days v "
            "JOIN recommendation_outcomes o ON o.id=v.outcome_id "
            "WHERE v.restaurant_id=? AND v.family=? AND v.day=? AND v.held=1 AND v.dollars<>0 AND v.outcome_id<>? "
            f"AND {_COUNTS_SQL}",
            (r["restaurant_id"], d["family"], d["day"], r["id"], _DISOWNED_LIKE, _CHANGED_LIKE)).fetchall()
        if rows:
            nxt = min(rows, key=lambda x: (_breadth(x["metric"]), -abs(float(x["dollars"] or 0)), x["outcome_id"]))
            conn.execute("UPDATE outcome_value_days SET counted=1 WHERE outcome_id=? AND day=?",
                         (nxt["outcome_id"], d["day"]))


# ── reading ─────────────────────────────────────────────────────────────────

def progress(restaurant_id, db_path=DB_PATH, today=None):
    """Interim reading for trackers still running: the metric since the start
    date, against the baseline. Labelled as partial by callers — a window that
    is a week old is a hint, not a result."""
    today = today or local_today(restaurant_id, db_path)
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
                            **_cmp(r, expected, value, scale)}
        out.append(r)
    return out


def get_outcome(outcome_id, db_path=DB_PATH):
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM recommendation_outcomes WHERE id=?", (outcome_id,)).fetchone()
    finally:
        conn.close()
    return _row(row) if row else None


def list_outcomes(restaurant_id, status=None, limit=50, db_path=DB_PATH, ids=None, include_untaken=False):
    """Newest first. `ids` narrows to those trackers (the timeline asks for
    the ones its page links to, however old — the newest 50 alone left an
    older result unshown). Advice NOT taken (observe_untaken's comparison
    trackers) is left out unless `include_untaken`: it is a comparison the
    learning reads (rec_learning.untaken_comparison), not a result of the
    owner's to list, brief or email."""
    conn = get_conn(db_path)
    try:
        sql = "SELECT * FROM recommendation_outcomes WHERE restaurant_id=?"
        args = [restaurant_id]
        if not include_untaken:
            sql += " AND source_key NOT LIKE ?"
            args.append(UNTAKEN_PREFIX + "%")
        if status:
            sql += " AND status=?"
            args.append(status)
        if ids:
            ids = [int(i) for i in ids][:200]
            sql += f" AND id IN ({','.join('?' for _ in ids)})"
            args.extend(ids)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        return [_row(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def checkin_keys(restaurant_id, tracker_ids, db_path=DB_PATH) -> dict:
    """{tracker id: the recommendation key it measures} — what a check-in
    for that result is sent against. A tracker with no recommendation behind
    it (a manual tracker, a monthly reprice figure) has no entry, so the
    surfaces never offer a check-in the server would refuse."""
    ids = [int(i) for i in (tracker_ids or []) if i is not None]
    if not ids:
        return {}
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            f"SELECT tracker_id, key FROM rec_instances WHERE restaurant_id=? AND tracker_id IN "
            f"({','.join('?' for _ in ids)}) ORDER BY rec_id DESC", (restaurant_id, *ids)).fetchall()
    except Exception as e:
        print(f"[outcomes] checkin keys unavailable: {e}")
        return {}
    finally:
        conn.close()
    out = {}
    for r in rows:
        out.setdefault(r["tracker_id"], r["key"])
    return out


def tracking_for_key(restaurant_id, source_key, db_path=DB_PATH):
    """The tracker running on this key, or None."""
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM recommendation_outcomes WHERE restaurant_id=? AND source_key=? "
                           "AND status='tracking'", (restaurant_id, source_key)).fetchone()
    finally:
        conn.close()
    return _row(row) if row else None


def linked_episodes(restaurant_id, db_path=DB_PATH) -> dict:
    """{tracker id: the rec_instances episode that names it}, newest first."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT * FROM rec_instances WHERE restaurant_id=? AND tracker_id IS NOT NULL "
                            "ORDER BY created_at DESC, rowid DESC", (restaurant_id,)).fetchall()
    except Exception as e:
        print(f"[outcomes] linked episodes unreadable for {restaurant_id}: {e}")
        return {}
    finally:
        conn.close()
    out = {}
    for r in rows:
        out.setdefault(r["tracker_id"], dict(r))
    return out


def visible_to(viewer, r, linked=None, db_path=DB_PATH) -> bool:
    """Whether a login may see one tracker (re-audit A26/A28): its metric
    (metric_visible_to), the module it is credited to (the line /value
    draws, permissions.MODULE_VIEW_PERMISSIONS), and the recommendation
    behind it — the linked episode, else the source key read as one — by
    rec_learning.viewer_sees (owner-only, a loss, a module it lacks). No
    viewer: an internal caller. Fails closed."""
    if viewer is None or (isinstance(viewer, dict) and viewer.get("is_admin")):
        return True
    if not metric_visible_to(viewer, r.get("metric")):
        return False
    try:
        from permissions import MODULE_VIEW_PERMISSIONS, has_permission
        need = MODULE_VIEW_PERMISSIONS.get(module_of_row(r))
        if need and not has_permission(viewer, need):
            return False
    except Exception as e:
        print(f"[outcomes] module visibility check failed closed: {e}")
        return False
    if linked is None:
        linked = linked_episodes(r.get("restaurant_id"), db_path=db_path)
    ep = linked.get(r.get("id")) or {"key": r.get("source_key"), "module": None}
    try:
        import rec_learning
        return rec_learning.viewer_sees(viewer, ep)
    except Exception as e:
        print(f"[outcomes] viewer check failed closed: {e}")
        return False


def hidden_tracker_ids(restaurant_id, viewer, db_path=DB_PATH) -> set:
    """The trackers of this restaurant a login may not see (visible_to) —
    dropped from every value figure before it is summed."""
    if viewer is None or (isinstance(viewer, dict) and viewer.get("is_admin")):
        return set()
    linked = linked_episodes(restaurant_id, db_path=db_path)
    conn = get_conn(db_path)
    try:
        rows = [dict(r, restaurant_id=restaurant_id) for r in conn.execute(
            "SELECT id, source_key, metric FROM recommendation_outcomes WHERE restaurant_id=?",
            (restaurant_id,)).fetchall()]
    finally:
        conn.close()
    return {r["id"] for r in rows if not visible_to(viewer, r, linked=linked, db_path=db_path)}


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
    in today's brief and again in tomorrow's. The restaurant's own date,
    as evaluate_on is (re-audit A8)."""
    today = today or local_today(restaurant_id, db_path)
    since = (today - timedelta(days=days)).isoformat()
    until = today.isoformat()
    return [r for r in list_outcomes(restaurant_id, status="evaluated", db_path=db_path)
            if since < str(r.get("evaluate_on") or "")[:10] <= until]


def realised(restaurant_id, db_path=DB_PATH, since=None, denied_modules=None, exclude_metrics=None,
             exclude_ids=None):
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
    or its metric's module is denied — and, per viewer, a metric it may not
    read or a tracker whose recommendation it may not see (_hidden_row).
    """
    rows = []
    for r in list_outcomes(restaurant_id, status="evaluated", limit=500, db_path=db_path):
        if r.get("verdict") != "improved" or not r.get("dollars_monthly"):
            continue
        if not r.get("counts"):
            continue          # an alert READ, or a win that no longer holds
        if since and (r.get("evaluate_on") or "") < str(since)[:10]:
            continue
        if _hidden_row(r, set(denied_modules or ()), exclude_metrics, exclude_ids):
            continue
        rows.append(r)
    return rows


def _after_window(r):
    """(start, end) ISO dates of the window a tracker's "after" was read over."""
    start = r.get("after_start") or r.get("started_on") or ""
    end = r.get("after_end") or r.get("evaluate_on") or start
    return str(start)[:10], str(end)[:10]


def _overlaps(a, b) -> bool:
    sa, ea = _after_window(a)
    sb, eb = _after_window(b)
    return sa <= eb and sb <= ea


def _weight(r) -> float:
    # A tiny weight for an unpriced result, so a rating win that overlaps
    # nothing is still kept.
    return abs(float(r.get("dollars_monthly") or 0)) + 1e-6


def _max_weight_set(rows):
    """The non-overlapping rows of largest total dollars (weighted interval
    scheduling). Grouping by chains of overlap kept one row where two did
    not overlap each other at all (A overlaps B, B overlaps C, A and C are
    separate weeks — re-audit A20)."""
    rows = sorted(rows, key=lambda r: (_after_window(r)[1], _after_window(r)[0], r.get("id") or 0))
    n = len(rows)
    best = [0.0] * (n + 1)
    took = [False] * (n + 1)
    prev = [0] * (n + 1)
    for i in range(1, n + 1):
        start = _after_window(rows[i - 1])[0]
        j = i - 1
        while j > 0 and _after_window(rows[j - 1])[1] >= start:
            j -= 1
        prev[i] = j
        take = best[j] + _weight(rows[i - 1])
        if take > best[i - 1]:
            best[i], took[i] = take, True
        else:
            best[i] = best[i - 1]
    out, i = [], n
    while i > 0:
        if took[i]:
            out.append(rows[i - 1])
            i = prev[i]
        else:
            i -= 1
    return out[::-1]


def distinct(rows):
    """One result per piece of work: moves in the same metric FAMILY whose
    after-windows overlap measured the same before/after change, so they are
    one result, not two (AI-18, rec-ROI #4) — waste and food cost over the
    same weeks, labor % and overtime, one weekday's sales and sales.

    Within a family, a narrower reading that overlaps a broader one
    (metrics.BREADTH) is the broader one's: labor % already holds the
    overtime premium, so a labor % win and an overtime loss over the same
    weeks are the labor % result, not the win less the premium a second
    time (re-audit A7) — wins and losses are netted this way per family
    before anything is summed. Of what remains, the non-overlapping set of
    largest dollars is kept (_max_weight_set, re-audit A20). Separate
    windows in one family, and overlapping windows in different families,
    stay separate."""
    out = []
    by_family = {}
    for r in rows:
        by_family.setdefault(metrics.family(r.get("metric")), []).append(r)
    for group in by_family.values():
        broad = [r for r in group
                 if not any(_breadth(o.get("metric")) < _breadth(r.get("metric")) and _overlaps(o, r)
                            for o in group)]
        out.extend(_max_weight_set(broad))
    return out


def distinct_wins(wins):
    """distinct() over wins — kept under the name value callers know."""
    return distinct(wins)


def _drop_sales_artifacts(rows):
    """A labor, food-cost, comp or void move is not counted beside an
    overlapping sales move the same way (re-audit A5): a cost share of sales
    falls when sales rise, with not a dollar of cost saved."""
    sales = [r for r in rows if metrics.family(r.get("metric")) == "sales"]
    return [r for r in rows
            if not (metrics.family(r.get("metric")) in _COST_FAMILIES
                    and any(s.get("verdict") == r.get("verdict") and _overlaps(s, r) for s in sales))]


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


PROJECTION_NOTE = "a projection — this month's measured figure × 12, if it holds — not a measurement"


def _selected(restaurant_id, db_path, since, denied, exclude_metrics, exclude_ids):
    """(evaluated rows this viewer may see, tracking rows, the counted moves
    kept after the family and sales rules)."""
    evaluated = [r for r in list_outcomes(restaurant_id, status="evaluated", limit=500, db_path=db_path)
                 if (not since or (r.get("evaluate_on") or "") >= str(since)[:10])
                 and not _hidden_row(r, denied, exclude_metrics, exclude_ids) and not r.get("informational")]
    tracking = [r for r in list_outcomes(restaurant_id, status="tracking", limit=500, db_path=db_path)
                if not _hidden_row(r, denied, exclude_metrics, exclude_ids) and not r.get("informational")]
    kept = _drop_sales_artifacts(distinct([r for r in evaluated if r.get("verdict") in _MOVED and r.get("counts")]))
    return evaluated, tracking, kept


def counted_ids(restaurant_id, db_path=DB_PATH) -> set:
    """The evaluated trackers total_value counts (savings and sales lift
    alike) after the family and sales rules — what a win announcement may
    quote, so it never quotes a result the value figures set aside."""
    _e, _t, kept = _selected(restaurant_id, db_path, None, set(), None, None)
    return {r["id"] for r in kept}


def total_value(restaurant_id, db_path=DB_PATH, since=None, denied_modules=None, exclude_metrics=None,
                exclude_ids=None):
    """What measured moves are worth, per month, with the honest denominator
    alongside.

    Deliberately NOT one bare number. `monthly` is the SAVINGS that improved
    (one per family per overlapping window, only those still holding);
    `worsened` is the savings that got worse, counted the same way
    (`priced_count` of them carry dollars); `net_monthly` is the one less
    the other and may be below zero — it says so rather than stopping at
    zero. `validated_monthly` is the part that held at its re-check with
    nothing else changing on its number. `tracked` and `unmeasurable` say
    how much of what the owner committed to could be read at all, because
    $400 from two results means something different when eight other
    trackers came back unknown.

    `sales_lift` is kept apart and never added in (re-audit A6): a sales
    rise is gross revenue — money through the till, not profit — and
    adding it to a labor saving summed two different kinds of dollar.
    `annual` is `monthly` × 12, a projection (PROJECTION_NOTE).

    Summing here is legitimate where business_intelligence.money_at_stake
    refuses to: these are all the same claim kind (measured, before/after,
    per month, cost saved) over the same restaurant, not a measured cost
    added to an elasticity forecast.
    """
    denied = set(denied_modules or ())
    evaluated, tracking, kept = _selected(restaurant_id, db_path, since, denied, exclude_metrics, exclude_ids)

    def _is_sales(r):
        return metrics.family(r.get("metric")) == "sales"
    savings = [r for r in kept if not _is_sales(r)]
    sales = [r for r in kept if _is_sales(r)]
    # Distinct work only (CLAUDE.md: "counts distinct work, never rows").
    all_wins = [r for r in kept if r.get("verdict") == "improved"]
    wins = [r for r in savings if r.get("verdict") == "improved" and r.get("dollars_monthly")]
    all_worse = [r for r in savings if r.get("verdict") == "worsened"]
    worse = [r for r in all_worse if r.get("dollars_monthly")]
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
        net_note = (f"More dollars were measured getting worse than improving: ${worse_monthly:,.0f}/month "
                    f"worse against ${monthly:,.0f}/month better. The figure is below zero because that is "
                    f"what was measured.")
    lift_wins = [r for r in sales if r.get("verdict") == "improved" and r.get("dollars_monthly")]
    lift_worse = [r for r in sales if r.get("verdict") == "worsened" and r.get("dollars_monthly")]
    lift = round(sum(abs(float(r["dollars_monthly"])) for r in lift_wins), 2)
    lift_down = round(sum(abs(float(r["dollars_monthly"])) for r in lift_worse), 2)
    lift_by_module = {}
    for r in lift_wins:
        m = module_of_row(r)
        lift_by_module[m] = round(lift_by_module.get(m, 0.0) + abs(float(r["dollars_monthly"])), 2)
    for r in lift_worse:
        m = module_of_row(r)
        lift_by_module[m] = round(lift_by_module.get(m, 0.0) - abs(float(r["dollars_monthly"])), 2)
    return {
        "monthly": monthly,
        "annual": round(monthly * 12, 2),
        "annual_basis": PROJECTION_NOTE,
        "wins": len(wins),
        "wins_measured": len(all_wins),
        "wins_by_module": wins_by_module,
        "unpriced_wins": [{"title": r.get("title"), "module": module_of_row(r), "line": r.get("result_line")}
                          for r in all_wins if not r.get("dollars_monthly")],
        "worsened": {"count": len(all_worse), "monthly": worse_monthly, "priced_count": len(worse)},
        "net_monthly": net,
        "net_note": net_note,
        "net_by_module": net_by_module,
        "validated_monthly": round(sum(abs(float(r["dollars_monthly"])) for r in validated), 2),
        "validated": len(validated),
        # The improvements by attribution grade (CA2 #7), side by side and
        # never summed into each other: a clear or held move against one
        # other changes fell beside (or that crossed the band only once).
        # Together they are `monthly`; a surface quotes them apart.
        "consistent_monthly": round(sum(abs(float(r["dollars_monthly"])) for r in wins
                                        if r.get("attribution") in ("consistent", "held")), 2),
        "consistent": len([r for r in wins if r.get("attribution") in ("consistent", "held")]),
        "associated_monthly": round(sum(abs(float(r["dollars_monthly"])) for r in wins
                                        if r.get("attribution") not in ("consistent", "held")), 2),
        "associated": len([r for r in wins if r.get("attribution") not in ("consistent", "held")]),
        "faded": len([r for r in evaluated if r.get("verdict") == "improved"
                      and r.get("recheck_verdict") in _FAILED_RECHECK]),
        "evaluated": len(evaluated),
        "in_flight": len(tracking),
        "unmeasurable": len([r for r in evaluated if r.get("verdict") in (None, "unknown")]),
        "no_clear_change": len([r for r in evaluated if r.get("verdict") == "no_clear_change"]),
        "by_module": by_module,
        # Sales per month measured before and after — gross revenue, not
        # profit, never added to the savings (re-audit A6).
        "sales_lift": {"monthly": lift, "wins": len(lift_wins),
                       "worsened": {"count": len([r for r in sales if r.get("verdict") == "worsened"]),
                                    "monthly": lift_down},
                       "net_monthly": round(lift - lift_down, 2), "by_module": lift_by_module,
                       "basis": SALES_LIFT_BASIS},
        "sales_pricing": "separate",
        "caveat": CAUSATION_CAVEAT,
    }


def best_ever(restaurant_id, db_path=DB_PATH, denied_modules=None, exclude_metrics=None, exclude_ids=None):
    """The single biggest measured SAVING that still counts, all time — the
    answer to "which recommendation created the biggest impact". Chosen
    from what total_value counts (the family and sales rules applied), so
    the biggest is never a reading total_value set aside, and never a sales
    lift (gross revenue, kept apart).

    strategy_jobs picks the biggest of ONE daily pass to notify on; that is
    a different question and deliberately stays where it is. This one has
    no time window and no dollar floor.
    """
    _e, _t, kept = _selected(restaurant_id, db_path, None, set(denied_modules or ()), exclude_metrics,
                             exclude_ids)
    wins = [r for r in kept if r.get("verdict") == "improved" and r.get("dollars_monthly")
            and metrics.family(r.get("metric")) != "sales"]
    if not wins:
        return None
    return max(wins, key=lambda r: abs(float(r["dollars_monthly"])))


_BASELINE_PHRASE = {
    "prior window": "the same length of time before",
    "matched weekdays": "the same weekdays in the weeks before",
    "same weeks last year": "the weeks before, adjusted by how the same weeks moved last year",
    TRIGGER_BASELINE_KIND: "the same number of weeks before what prompted the recommendation",
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
    """One honest sentence for a finished tracker. A result the owner said
    they never made carries no dollars (re-audit A15, contract K7): it is
    said, and said not to count — the money was quoted beside "doesn't
    count" on Home."""
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
    word = "improved" if r["verdict"] == "improved" else "got worse"
    if disowned(r):
        return f"{r['title']}: {moved} — {word}, but it isn't counted: you said the change wasn't made."
    if conditions_changed(r):
        return (f"{r['title']}: {moved} — {word}, but it isn't counted: you said something else changed in "
                f"the same weeks.")
    if overlaps_trigger(r):
        return (f"{r['title']}: {moved} — {word}, but it isn't counted: it could only be measured against the "
                f"weeks that prompted the recommendation, and a number often comes back from a bad stretch on "
                f"its own.")
    money = ""
    if r.get("dollars_monthly"):
        money = f", roughly ${abs(r['dollars_monthly']):,.0f}/month"
        if metrics.family(r.get("metric")) == "sales":
            money += " in sales (revenue, not profit)"
    out = f"{r['title']}: {moved} — {word}{money}."
    if r.get("recheck_verdict") in _FAILED_RECHECK:
        when = mdy(r.get("rechecked_at") or r.get("recheck_on"))
        out += (f" It no longer held when re-checked on {when}, so it "
                f"{'no longer counts' if r['verdict'] == 'improved' else 'is no longer subtracted'}.")
        return out
    # The attribution grade, in words (CA1 red flag 19): "held" only for a
    # result whose re-check held, and every grade before-and-after only.
    phrase = r.get("grade_phrase") or grade_phrase(r)
    if phrase:
        out += f" {phrase[:1].upper()}{phrase[1:]}. Measured, not proven cause."
    return out


# ── boot ────────────────────────────────────────────────────────────────────

# Columns added at boot by init_outcomes (DATABASE_SCHEMA.md,
# recommendation_outcomes): the trigger a result is measured clear of (CA2
# #1) and the restaurant's own noise band it was read against (CA2 #3).
_ADDED_COLUMNS = (
    ("trigger_value", "REAL"),                        # the metric over the trigger window
    ("trigger_start", "TEXT"),                        # the window that fired the recommendation
    ("trigger_end", "TEXT"),
    ("baseline_overlaps_trigger", "INTEGER DEFAULT 0"),  # 1 = shown, never counted
    ("noise_band", "REAL"),                           # this restaurant's band, metric units (NULL = stated band)
    ("noise_sigma", "REAL"),                          # spread of one window, metric units
    ("false_alarm_rate", "REAL"),                     # two-sided, with no real change
    ("band_basis", "TEXT"),                           # how the band was estimated, in words
)


def init_outcomes(db_path=DB_PATH):
    """Boot (models.init_db, after the columns and rec_instances exist): fill
    the rec-ROI columns on rows written before them, once. What can be
    recomputed is — the % change, the module (from the recommendation's own
    record where there is one), the baseline kind (every row before this
    used the prior window), the re-check date — and what cannot (the other
    changes in an old window, until its re-check reads them) stays NULL.

    First, the calibration columns (_ADDED_COLUMNS) on a database from
    before them — boot only, never on a request path (CLAUDE.md)."""
    conn = get_conn(db_path)
    try:
        try:
            have = {r[1] for r in conn.execute("PRAGMA table_info(recommendation_outcomes)").fetchall()}
            for col, decl in _ADDED_COLUMNS:
                if have and col not in have:
                    conn.execute(f"ALTER TABLE recommendation_outcomes ADD COLUMN {col} {decl}")
            conn.commit()
        except Exception as e:
            print(f"[outcomes] calibration columns not added: {e}")
        not_info = " AND ".join("source_key NOT LIKE ?" for _ in INFORMATIONAL_PREFIXES)
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM recommendation_outcomes WHERE module IS NULL OR baseline_kind IS NULL "
            "OR (delta_pct IS NULL AND delta IS NOT NULL AND baseline_value IS NOT NULL AND baseline_value != 0) "
            "OR (status='evaluated' AND verdict IN ('improved','worsened') AND recheck_on IS NULL "
            f"AND {not_info})", tuple(p + "%" for p in INFORMATIONAL_PREFIXES)).fetchall()]
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
