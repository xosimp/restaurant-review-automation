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
    # `comparable` False (the NRA median includes benefits; this labor % is
    # wages from shifts) makes the figure context only: no dollar gap, no
    # standing (re-audit #2, R3-5/R4-1).
    return {"pct": float(ind["median"]), "basis": basis, "inferred": bool(ind.get("inferred")),
            "source_kind": ind.get("source_kind"), "comparable": ind.get("comparable") is True,
            "definition_note": ind.get("definition_note")}


def labor_vs_industry_monthly(overall_pct, total_sales, period_days, hours_are_estimated=False,
                              sales_data_missing=False, analysis_failed=False, industry_pct=None,
                              cost_basis=None, comparable=False) -> int:
    """Monthly dollars this restaurant's labor % runs under the industry
    figure for its type (`industry_pct`, from labor_industry_benchmark), or
    0 when that cannot be said honestly: no benchmark for the type, a
    figure that is not measured the same way (`comparable` False — every
    published labor figure today, re-audit #2: the NRA median includes
    benefits), estimated hours understate labor (and so overstate the gap),
    missing sales or a sub-week period leave no monthly rate to compare, or
    the labor cost rests on the $26/hr default (`cost_basis == "default"`,
    Benchmarking audit #14) — an assumed wage is not a gap.

    labor.savings_breakdown no longer calls this (it sends 0): no client
    draws the tile. Candidate for future cleanup after additional
    verification — tests pin its floors."""
    if industry_pct is None or cost_basis == "default" or not comparable:
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
    dict. Pure: reads role_rates_json, hourly_rate and hourly_rate_source.
    A rate the owner entered is theirs even when it is exactly $26
    (`hourly_rate_source == "set"`, re-audit #45)."""
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
    if rate > 0 and (abs(rate - LABOR_DEFAULT_HOURLY_RATE) > 1e-9 or g("hourly_rate_source") == "set"):
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


def target_phrase(restaurant, kind, value) -> str:
    """The target in a sentence: "your 30% target", or "Cavnar's starting
    target of 30%" when the owner has not set one (#13)."""
    try:
        v = f"{float(value):g}%"
    except (TypeError, ValueError):
        v = "—"
    return f"your {v} target" if target_source(restaurant, kind) == "set" else f"{STARTING_TARGET_LABEL} of {v}"


def target_alerts_allowed(restaurant, kind) -> bool:
    """No over-target alert on an unconfirmed default (#13): an SMS saying a
    steakhouse is "over your 30% target" when nobody set 30 is the bug."""
    return target_source(restaurant, kind) != "default"


def target_value(restaurant, kind) -> float:
    """The target % itself: the stored value when it is a positive number,
    else Cavnar's default for the kind. notify.labor_target_for reads it."""
    field = _TARGET_FIELDS[kind][0]
    if restaurant is not None:
        own = restaurant.get(field) if isinstance(restaurant, dict) else getattr(restaurant, field, None)
        try:
            v = float(own)
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    return TARGET_DEFAULTS[kind]


def target_for(restaurant, kind) -> dict:
    """The ONE read of a labor ("labor") or food-cost ("food") target for
    every surface that judges a figure against it (Benchmarking re-audit
    #10, R2-3/R3-16/R4-26): {pct, source, label, alerts_allowed, phrase}.
    `label` is "your target" or "Cavnar's starting target"; on a starting
    target nothing is "over" in red — a tag, a colour or a severity caps at
    a watch, and no alert fires (`alerts_allowed` False)."""
    pct = target_value(restaurant, kind)
    src = target_source(restaurant, kind)
    return {"pct": pct, "source": src, "label": target_label(restaurant, kind),
            "alerts_allowed": src != "default", "phrase": target_phrase(restaurant, kind, pct)}


def seeded_targets(restaurant) -> dict:
    """The update that seeds a not-yet-set target from a PUBLISHED median
    for the owner-CONFIRMED type and service model that is measured the way
    Cavnar measures it ({} when the profile is unconfirmed; nothing for a
    target the owner set). With no such figure the target is Cavnar's
    default — the value reset to it, labelled as the default, alerts off
    (re-audit #3, R1-03/R2-4/R4-6/R3-26): the NRA labor median includes
    benefits and the NRA food median counts non-alcohol beverages, so
    neither seeds a wages-only labor % or a COGS food cost %, and a
    counter-service Italian never gets the full-service figure (#4)."""
    try:
        from intelligence import categories
        from intelligence import metrics_registry as _mr
        import benchmark_registry as _br
    except Exception:
        return {}
    prof = categories.profile_for(restaurant)
    concept = prof.get("concept")
    if not prof.get("confirmed") or not concept:
        return {}
    out = {}
    for kind, metric, engine_metric in (("labor", "labor_pct", "labor_pct_28d"),
                                        ("food", "food_cost_pct", "food_cost_pct_28d")):
        field, src_field = _TARGET_FIELDS[kind]
        if target_source(restaurant, kind) == "set":
            continue
        e = _br.lookup(metric, concept, published_only=True, definition=_mr.definition(engine_metric),
                       service_model=prof.get("service_model"))
        if e and e.get("median") is not None:
            out[field] = float(e["median"])
            out[src_field] = "seeded"
        else:
            out[field] = TARGET_DEFAULTS[kind]
            out[src_field] = "default"
    return out


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
