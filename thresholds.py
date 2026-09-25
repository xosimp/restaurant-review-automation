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


# ── Labor against the industry (CA1 L33, fix I10; NS4 H3) ────────────────
# One benchmark for both clients, and now by restaurant type. The 34.5%
# full-service midpoint used to be applied to every restaurant, so a coffee
# shop at 22% labor was told it "saved" $7,500 a month against a figure for
# a different kind of business. The comparison is made only where
# benchmark_registry holds a PUBLISHED entry for the restaurant's type with a
# stated median (today: the NRA full-service figure, for full-service types).
# Every other restaurant gets no industry figure at all — the server sends
# `labor_industry_pct` null and both clients hide the tile.
LABOR_INDUSTRY_MIN_DAYS = 7


def labor_industry_benchmark(restaurant) -> dict | None:
    """{pct, basis, inferred, source_kind}: the published labor median for
    this restaurant's type, or None when there is no PUBLISHED entry with a
    stated median for it (no entry, no benchmark). The basis names the
    source and year and says when the type was inferred rather than set.

    Read from the Benchmark Engine's `industry` comparison (intelligence.
    engine; Benchmarking #48), the one place a published figure is chosen
    for a restaurant, so the labor tile, Ask and the How you compare card
    quote the same entry. A rule of thumb never feeds a figure here. (The
    `entry` key it used to carry had no reader.)"""
    try:
        from intelligence import engine as _engine
        cm = _engine.compare(getattr(restaurant, "id", None), "labor_pct_28d", kinds=("industry",),
                             restaurant=restaurant, rows=[])
    except Exception as e:
        print(f"[thresholds] labor industry benchmark unavailable: {e}")
        return None
    ind = next((c for c in cm.get("comparisons") or () if c.get("kind") == "industry"), None)
    if not ind or not ind.get("available") or ind.get("source_kind") != "published" or ind.get("median") is None:
        return None
    basis = f"{ind.get('median_basis') or 'published median'}, {float(ind['median']):g}% ({ind.get('source')})"
    if ind.get("inferred"):
        import benchmark_registry as _br
        basis += f"; {_br.INFERRED_NOTE}"
    return {"pct": float(ind["median"]), "basis": basis, "inferred": bool(ind.get("inferred")),
            "source_kind": ind.get("source_kind")}


def labor_vs_industry_monthly(overall_pct, total_sales, period_days, hours_are_estimated=False,
                              sales_data_missing=False, analysis_failed=False, industry_pct=None) -> int:
    """Monthly dollars this restaurant's labor % runs under the industry
    figure for its type (`industry_pct`, from labor_industry_benchmark), or
    0 when that cannot be said honestly: no benchmark for the type,
    estimated hours understate labor (and so overstate the gap), missing
    sales or a sub-week period leave no monthly rate to compare."""
    if industry_pct is None:
        return 0
    try:
        pct, sales, days = float(overall_pct or 0), float(total_sales or 0), int(period_days or 0)
        bench = float(industry_pct)
    except (TypeError, ValueError):
        return 0
    if analysis_failed or hours_are_estimated or sales_data_missing or days < LABOR_INDUSTRY_MIN_DAYS or not pct:
        return 0
    from metrics import DAYS_PER_MONTH   # one month definition (NS3 L5)
    monthly_sales = sales / days * DAYS_PER_MONTH
    return max(0, int(round((bench - pct) / 100 * monthly_sales)))


# ── A night's rating (CA1 D6, fix I11) ───────────────────────────────────
# The same floor metrics.measure("avg_rating") applies: under five reviews a
# rating is a handful of guests, not a reading.
RATING_MIN_REVIEWS = 5

# ── Group strongest / weakest (CA1 H13, fix I12; Benchmarking #19) ───────
# A location is ranked "strongest" or "weakest" by rating only when at least
# this many reviews stand behind its rating in the window — the group Home
# brief, the single-location brief's portfolio line and the group digest all
# read this one floor. It was 3, below the platform's own rating floor, so a
# location was named "weakest" on three reviews (BM1-19, BM3-18): it is now
# that floor.
GROUP_RANK_MIN_REVIEWS = RATING_MIN_REVIEWS

# Spread of star ratings on the 1-5 scale, skewed hard to 4 and 5: a stated
# assumption (the same 1.1 models.py uses for competitor rating moves), not
# something estimated from data Cavnar does not hold. The standard error of
# a mean rating on n reviews is about RATING_SIGMA / sqrt(n).
RATING_SIGMA = 1.1


def rating_se(n):
    """The standard error of a mean rating on `n` reviews, or None with none."""
    try:
        n = int(n or 0)
    except (TypeError, ValueError):
        return None
    return RATING_SIGMA / (n ** 0.5) if n > 0 else None


def rating_gap_beyond_noise(a, n_a, b, n_b, z=1.0) -> bool:
    """Whether two mean ratings differ by more than `z` standard errors of
    their difference (review counts n_a, n_b). An unknown count is never
    "beyond noise": a gap is called only when the volume behind it is known."""
    try:
        a, b = float(a), float(b)
    except (TypeError, ValueError):
        return False
    se_a, se_b = rating_se(n_a), rating_se(n_b)
    if se_a is None or se_b is None:
        return False
    return abs(a - b) > z * (se_a ** 2 + se_b ** 2) ** 0.5
