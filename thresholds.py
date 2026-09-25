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
    """{pct, basis, inferred, entry}: the published labor median for this
    restaurant's type, or None when the registry has no published entry
    for it (no entry, no benchmark). The basis names the source and year
    and says when the type was inferred rather than set."""
    import benchmark_registry as _br
    e = _br.for_restaurant("labor_pct", restaurant, published_only=True)
    if not e or e.get("median") is None:
        return None
    # A published DOLLAR figure is never computed on a type Cavnar guessed
    # (Benchmarking audit #8, BM2-2): the owner confirms the type first.
    if e.get("inferred"):
        return None
    basis = f"{e.get('median_basis') or 'published median'}, {e['median']:g}% ({_br.cite(e)})"
    if e.get("inferred"):
        basis += f"; {_br.INFERRED_NOTE}"
    return {"pct": float(e["median"]), "basis": basis, "inferred": bool(e.get("inferred")), "entry": e}


def labor_vs_industry_monthly(overall_pct, total_sales, period_days, hours_are_estimated=False,
                              sales_data_missing=False, analysis_failed=False, industry_pct=None,
                              cost_basis=None) -> int:
    """Monthly dollars this restaurant's labor % runs under the industry
    figure for its type (`industry_pct`, from labor_industry_benchmark), or
    0 when that cannot be said honestly: no benchmark for the type,
    estimated hours understate labor (and so overstate the gap), missing
    sales or a sub-week period leave no monthly rate to compare, or the
    labor cost rests on the $26/hr default (`cost_basis == "default"`,
    Benchmarking audit #14) — an assumed wage is not a gap."""
    if industry_pct is None or cost_basis == "default":
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


# ── Where the labor cost comes from (Benchmarking audit #14, BM1-9) ──────
# Labor % is labor cost ÷ sales, and labor cost is hours × a rate. When the
# rate is the $26/hr default — which benchmark_registry.ABSENT says has no
# source — every dollar built on it is an assumption: the "under industry"
# figure, the target-gap dollars and a place in a peer band are withheld.
#   pos_wages      the POS supplied each shift's wage (reserved: no
#                  integration supplies wages today)
#   role_rates     the owner set a rate per role
#   owner_blended  the owner set one blended rate (anything but the default)
#   default        the unsourced $26/hr — mirrors labor.DEFAULT_HOURLY_RATE
LABOR_DEFAULT_HOURLY_RATE = 26.0
LABOR_COST_BASES = ("pos_wages", "role_rates", "owner_blended", "default")
LABOR_COST_BASIS_LABELS = {
    "pos_wages": "wages from your POS", "role_rates": "your per-role pay rates",
    "owner_blended": "your blended hourly rate", "default": "Cavnar's assumed $26/hr (not your payroll)",
}


def labor_cost_basis(restaurant) -> str:
    """'role_rates' | 'owner_blended' | 'default' for a Restaurant or row
    dict. Pure: reads role_rates_json and hourly_rate only."""
    def g(k):
        return restaurant.get(k) if isinstance(restaurant, dict) else getattr(restaurant, k, None)
    if restaurant is None:
        return "default"
    raw = g("role_rates_json")
    if raw:
        try:
            import json as _json
            rates = _json.loads(raw) if isinstance(raw, str) else raw
            if any(k != "_default" and float(v or 0) > 0 for k, v in (rates or {}).items()):
                return "role_rates"
        except Exception:
            pass
    try:
        rate = float(g("hourly_rate") or 0)
    except (TypeError, ValueError):
        rate = 0.0
    if rate > 0 and abs(rate - LABOR_DEFAULT_HOURLY_RATE) > 1e-9:
        return "owner_blended"
    return "default"


# ── Where a target comes from (Benchmarking audit #13, BM1-10) ───────────
# A 30% labor and a 30% food target for every type of restaurant were shown
# as "your target", and a steakhouse at the published median was texted
# "over target" every night. A target now says where it came from:
#   set      the owner (or admin) chose it — behaviour unchanged
#   seeded   the published median for the owner-CONFIRMED type
#   default  Cavnar's starting target — labelled so, and no over-target
#            alert fires on it
TARGET_DEFAULTS = {"labor": 30.0, "food": 30.0}
_TARGET_FIELDS = {"labor": ("labor_target_pct", "labor_target_source"),
                  "food": ("food_cost_target", "food_cost_target_source")}
STARTING_TARGET_LABEL = "Cavnar's starting target"


def target_source(restaurant, kind) -> str:
    """'set' | 'seeded' | 'default' for kind 'labor' or 'food'. A row from
    before the source was recorded counts as the owner's own when it holds
    anything but the default."""
    field, src_field = _TARGET_FIELDS[kind]

    def g(k):
        return restaurant.get(k) if isinstance(restaurant, dict) else getattr(restaurant, k, None)
    if restaurant is None:
        return "default"
    src = g(src_field)
    if src in ("set", "seeded", "default"):
        return src
    try:
        v = float(g(field))
    except (TypeError, ValueError):
        return "default"
    return "default" if abs(v - TARGET_DEFAULTS[kind]) < 1e-9 else "set"


def target_label(restaurant, kind) -> str:
    """How a surface names the target: "your target", or "Cavnar's
    starting target" for a seeded or default one."""
    return "your target" if target_source(restaurant, kind) == "set" else STARTING_TARGET_LABEL


def target_alerts_allowed(restaurant, kind) -> bool:
    """No over-target alert on an unconfirmed default (#13): an SMS saying a
    steakhouse is "over your 30% target" when nobody set 30 is the bug."""
    return target_source(restaurant, kind) != "default"


def seeded_targets(restaurant) -> dict:
    """The update that seeds a not-yet-set target from the PUBLISHED median
    for the owner-confirmed type ({} when the type is unconfirmed or the
    owner already set one). With no published median the default stays,
    labelled as the default."""
    try:
        from intelligence import categories
        import benchmark_registry as _br
    except Exception:
        return {}
    prof = categories.profile_for(restaurant)
    concept = prof.get("concept")
    if not prof.get("confirmed") or not concept:
        return {}
    out = {}
    for kind, metric in (("labor", "labor_pct"), ("food", "food_cost_pct")):
        field, src_field = _TARGET_FIELDS[kind]
        if target_source(restaurant, kind) == "set":
            continue
        e = _br.lookup(metric, concept, published_only=True)
        if e and e.get("median") is not None:
            out[field] = float(e["median"])
            out[src_field] = "seeded"
        else:
            out[src_field] = "default"
    return out
