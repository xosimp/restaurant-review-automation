"""thresholds.py — the few numbers that decide when something is a problem.

One definition per question, read by every surface that asks it. Home, the
alerts and the issues each used to carry their own "labor over target" (any
overage, more than 3 points, 3 or more) and an owner saw a problem on one
screen that another screen said did not exist. L0: constants, and the one
pure comparison a constant implies (demand_level, labor_vs_industry_monthly)
so no caller re-derives it with a copy of the numbers.
"""

# Labor % over the restaurant's own target before anything calls it "over":
# the period alert, the labor issue, Home's attention item and a single
# day in the overstaffed list.
LABOR_OVER_TARGET_PTS = 3.0

# A day under target counts as "strong sales on a lean crew" only when its
# sales are at least this multiple of the restaurant's own median day — a
# fixed dollar floor meant nothing to a $1,500/day cafe or a $20,000/day room.
STRONG_DAY_SALES_MULTIPLE = 1.15

# Reviews older than this are history, not replies owed: "N reviews waiting"
# and the no-response alert count only the recent ones.
REPLY_OWED_MAX_AGE_DAYS = 30

# ── Demand levels (CA1 L25/L27/L28, fix I13) ─────────────────────────────
# One table for "how busy is this weekday against an average day", read by
# the Shift Quality scorer (shift_quality.demand_from_pct), the staff
# pre-shift line (preshift) and the scheduler's demand read. The pre-shift
# line used its own ±15% while the scorer used +25/+8/-15, so a day the
# scorer staffed as "high" was "a typical Friday" at lineup. The cut-offs are
# the scorer's (they choose staffing profiles, and moving them would move
# every schedule); the lineup speaks in the same levels.
#   peak   >= +25%   the weekday's median sales a quarter above an average day
#   high   >= +8%    busier than normal by more than ordinary day-to-day swing
#   low    <= -15%   the SLOW_DAY_PCT line demand.py already used
#   normal otherwise
DEMAND_PEAK_PCT = 25
DEMAND_HIGH_PCT = 8
DEMAND_LOW_PCT = -15
# A weekday's median is a level only on at least this many readings — a
# median of two nights is one of them (CA1 L25).
DEMAND_LEVEL_MIN_READINGS = 3


def demand_level(vs_average_pct):
    """'peak' | 'high' | 'low' | 'normal' from a weekday's % against an
    average day, or None when there is no figure."""
    if vs_average_pct is None:
        return None
    pct = float(vs_average_pct)
    if pct >= DEMAND_PEAK_PCT:
        return "peak"
    if pct >= DEMAND_HIGH_PCT:
        return "high"
    if pct <= DEMAND_LOW_PCT:
        return "low"
    return "normal"


# ── Labor against the industry (CA1 L33, fix I10) ────────────────────────
# One benchmark for both clients: the web tile used 32% (a pre-pandemic
# figure) and iOS 34.5%, so the same restaurant saw two dollar figures.
# 34.5% is the midpoint of the 33-36% full-service range (NRA 2024
# Restaurant Operations Data Abstract), the band both clients print.
LABOR_INDUSTRY_PCT = 34.5
LABOR_INDUSTRY_BASIS = "midpoint of the 33-36% full-service range (NRA 2024 Restaurant Operations Data Abstract)"
LABOR_INDUSTRY_MIN_DAYS = 7


def labor_vs_industry_monthly(overall_pct, total_sales, period_days, hours_are_estimated=False,
                              sales_data_missing=False, analysis_failed=False) -> int:
    """Monthly dollars this restaurant's labor % runs under the industry
    midpoint, or 0 when that cannot be said honestly: estimated hours
    understate labor (and so overstate the gap), missing sales or a
    sub-week period leave no monthly rate to compare."""
    try:
        pct, sales, days = float(overall_pct or 0), float(total_sales or 0), int(period_days or 0)
    except (TypeError, ValueError):
        return 0
    if analysis_failed or hours_are_estimated or sales_data_missing or days < LABOR_INDUSTRY_MIN_DAYS or not pct:
        return 0
    from metrics import DAYS_PER_MONTH   # one month definition (NS3 L5)
    monthly_sales = sales / days * DAYS_PER_MONTH
    return max(0, int(round((LABOR_INDUSTRY_PCT - pct) / 100 * monthly_sales)))


# ── Group strongest / weakest (CA1 H13, fix I12) ─────────────────────────
# A location is ranked "strongest" or "weakest" by rating only when at least
# this many reviews stand behind its rating in the window — the group Home
# brief, the single-location brief's portfolio line and the group digest all
# read this one floor (they had 3 / none / none).
GROUP_RANK_MIN_REVIEWS = 3

# ── A night's rating (CA1 D6, fix I11) ───────────────────────────────────
# The same floor metrics.measure("avg_rating") applies: under five reviews a
# rating is a handful of guests, not a reading.
RATING_MIN_REVIEWS = 5
