"""
confidence_engine.py — the Recommendation Confidence arithmetic. Pure: no
I/O, no database, no clock. Every number here is computed from inputs the
caller measured; nothing is a stand-in.

WHAT THE PERCENTAGE MEANS (the owner's decision, 9/24/26 — "support
score"): the overall "72% confidence" is how well SUPPORTED the advice is —
evidence × track record × freshness, every part measured. It is NOT the
chance the recommendation works, and nothing may present it as one
(MEANING travels in every K1 object). The admin view checks that a higher
figure really does go with better results (admin_ops.confidence_calibration
→ `ordering`).

A recommendation's confidence is three measured dimensions and one overall
percentage (contract K1, INTELLIGENCE_ENGINE.md → Recommendation Confidence):

  Evidence Strength  = 100 × sample_factor × coverage × quality, then capped.
      sample_factor = min(1, n / N_FULL[kind] × corroboration) — how much of
      a full sample stands behind the card (N_FULL below, each with the
      variance reason it is that number); corroboration = 1 +
      CORROBORATION_STEP per independent agreeing module, for at most
      CORROBORATION_MAX of them;
      coverage      = the share of the window's trading days measured
      (metrics.coverage), or 1 when the figure is not a trading-day metric;
      quality       = the product of QUALITY multipliers for what is known to
      be weaker (hours estimated from the schedule, gross sales missing …).
      Caps: coverage under MIN_COVERAGE → at most LOW_COVERAGE_CAP; any
      partial-data flag (incl. "inferred": causal wording) → at most
      PARTIAL_CAP; any unverified figure → at most UNVERIFIED_CAP; a model's
      own band only LOWERS it (MODEL_CAPS), never raises it. Sample or demo
      data scores 0 and says so (`sample` on the K1 object).
  Historical Accuracy = how likely this kind of advice beats DOING NOTHING
      here: P(its improved-rate when taken > the do-nothing rate), a Beta-
      Binomial read of this restaurant's measured record (rec_learning.
      kind_record — learned_verdict, one result per window, confounded and
      baseline-overlap results excluded). The taken rate's prior is centred
      on the do-nothing rate with SHRINK_K pseudo-results (the anonymous
      cohort may only pull that centre DOWN, never up); the do-nothing rate
      is the kind's untaken results when there are MIN_MEASURED of them,
      else the chance rate held as loosely as STATED_RATE_K results. Shown
      only at MIN_MEASURED own results, held to ACCURACY_BOUNDS; the lift
      itself rides in `lift` and the basis says it: "improved 4 of 6 times
      vs 1 of 6 when not acted on".
  Data Freshness     = 100 × min over the card's sources of recency × completeness
      (data_freshness.source_state; the recency curve is recency() below).
  Overall            = the geometric mean of the dimensions that could be
      measured — freshness folded in AFTER the record's ceiling, so aging
      data pulls a capped figure down too — with the caps IN THIS ORDER,
      each named in `caps_applied` and in the line's `reason` when it binds:
        a. no track record here (accuracy null) → at most NO_TRACK_RECORD_CAP;
        b. a record that is not BEATS_AT likely to beat doing nothing → at
           most UNPROVEN_RECORD_CAP, and one more likely than not NOT to
           (under AGAINST_BELOW) → at most RECORD_AGAINST_CAP;
        c. Data Freshness under STALE_BELOW → at most STALE_CAP, with the
           out-of-date caution; a failing source always carries a caution;
        d. freshness nothing could date → at most FRESHNESS_UNMEASURED_CAP:
           an unmeasured dimension never raises the figure.
      No evidence → not measurable. Never above MAX_OVERALL.

The band words kept for older clients are derived from the percentage
(HIGH_AT / MEDIUM_AT, sent as `thresholds`); `score` is pct / 100 and 0.0
when not measurable, because shipped iOS builds decode it as a non-optional
number.
"""
import math
from functools import lru_cache

# 2: the support-score meaning (9/24/26). A snapshot's trust_version says
# which meaning the % it recorded had, so admin compares like with like.
VERSION = 2

MEANING = "How well supported this is — not the chance it works"

# ── bands (derived, for older clients and the colour map) ──────────────────
HIGH_AT = 75
MEDIUM_AT = 50

# ── Evidence Strength ──────────────────────────────────────────────────────
#
# How many observations make a full sample, per kind of evidence — each the
# point where one more observation stops moving the figure by more than the
# decision it supports can tolerate (confidence round 2, B1 M4 / B4 M3:
# "one row is the whole sample" is gone from every recommendation).
N_FULL = {
    # A share of reviews on one theme: one review moves it by 1/n, so at 8
    # one guest moves it by 12.5 points — below that a single guest can make
    # or unmake a "most-mentioned complaint".
    "reviews": 8,
    # One weekday's labor %: its night-to-night spread is about 3 points
    # (B2 p2's measured day_sd); the mean of 4 has a standard error of 1.5,
    # half the 3-point over-target line (thresholds.LABOR_OVER_TARGET_PTS).
    # Fewer, and one bad Tuesday decides it.
    "weekdays": 4,
    # A weekly series' slope: 8 points is the fewest where a least-squares
    # slope's t-test has 6 degrees of freedom — the window every weekly
    # series here reads (waste history, the rating trend, marketing).
    "weeks": 8,
    # An item over its tolerance in 4 of 8 weeks: by chance one bad week in
    # 8 is expected; 4 is a standing pattern (food_cost_intelligence).
    "waste_weeks": 4,
    # A price that has held for 4 weekly readings is a trend, not a single
    # invoice line (one reading's error is the whole change).
    "price_weeks": 4,
    # Labor % and food cost % over 28 trading days: with a 3-point daily
    # spread the 28-day mean's standard error is 0.6 points, inside the
    # smallest move the outcome band reads (metrics' own window).
    "trading_days": 28,
    # Four posts / campaigns before one is compared against the others: the
    # comparison's baseline is then the mean of at least three.
    "posts": 4,
    "campaigns": 4,
    # Three nearby competitors before a comparison says anything about a
    # market rather than one neighbour.
    "competitors": 3,
    # Three quotes for the same unit before a spread is a market price and
    # not one supplier's list (a sourcing driver).
    "supplier_quotes": 3,
    # A gap between recipes and counts seen at 4 recounts: one recount's gap
    # can be a miscount; four that agree are usage (a portion driver).
    "recounts": 4,
    # Distinct live reads backing a stated figure (Ask, R3) or distinct
    # corroborating modules' verified figures (R1): three independent reads
    # is as supported as the prompts ask a claim to be.
    "evidence_items": 3,
    # Nights of observation behind a daily-report action's figures: one
    # night is an anecdote. 8 is the demand forecast's own window (8 of the
    # same weekday, demand.LOOKBACK_WEEKS), so an action whose figure is
    # tonight against that forecast stands on a full sample.
    "nights": 8,
    # Older names kept so a stored or older caller's input reads as it did:
    # night_facts (cites of one night — the daily report counts nights now)
    # and count (a fact on file — facts carry no confidence now, B4 H5).
    "night_facts": 3,
    "count": 1,
}

# Independent agreement (B4 M2): each other module whose verified figure
# agrees raises the sample factor by this much, for at most this many
# modules. Bounded because one restaurant's modules share its weeks (one bad
# fortnight touches sales, labor and reviews at once), so agreement is less
# than independent evidence. The "inferred" PARTIAL cap still binds causal
# wording: corroboration never makes a cause "high".
CORROBORATION_STEP = 0.25
CORROBORATION_MAX = 2

MIN_COVERAGE = 0.7          # outcomes.MIN_COVERAGE (a test holds them in step)
LOW_COVERAGE_CAP = 49       # under 70% of the window measured: never better than low
PARTIAL_CAP = 74            # any partial-data flag: never high
UNVERIFIED_CAP = 35         # a figure that did not check out against the data
MODEL_CAPS = {"high": 100, "medium": 65, "low": 35}   # a model's self-rating only lowers
# What is known to be weaker, as a multiplier on the evidence.
QUALITY = {
    "hours_are_estimated": 0.8,    # hours from the schedule, no clock-ins on file
    "gross_missing": 0.8,          # the POS did not give gross sales
    "days_with_conflicting_sales": 0.9,
    "stale_read": 0.8,             # a stored model read past its refresh (its TTL)
    "sampled": 0.8,                # Google Places returns five reviews at a time
    "list_prices": 0.8,            # supplier list prices, not what was paid
    "no_sales_mix": 0.8,           # a per-plate figure with no units sold
}
# Flags that mean the figure covers only part of what it claims: each caps
# evidence at PARTIAL_CAP (on top of any QUALITY multiplier). `stale_read`
# (a stored read past its refresh — its own age also weighs in Data
# Freshness through rec_trust's diagnosis pseudo-source, DH3-1) and
# `changed_since` (the owner changed something after the data window —
# a reprice, a published schedule, a target, a recommendation marked done:
# the figure describes the business before it, DH3-14) are partial too.
PARTIAL_FLAGS = ("days_missing_sales", "hours_are_estimated", "gross_missing", "days_with_conflicting_sales",
                 "partial", "inferred", "sampled", "provisional", "period_too_short", "stale_read",
                 "changed_since")
# The note each of those partial flags carries when it is the only one.
_PARTIAL_NOTES = {"stale_read": "a stored read not refreshed since it was written",
                  "changed_since": "a change since the data window isn't reflected yet"}

# ── Historical Accuracy ────────────────────────────────────────────────────
MIN_MEASURED = 5            # rec_learning.MIN_MEASURED_FOR_RATE (held in step by a test)
PRIOR_MIN_MEASURED = 10     # rec_learning.PRIOR_MIN_MEASURED
SHRINK_K = 5                # pseudo-results in the taken rate's prior (rec_learning.SHRINK_K)
SHRINK_PRIOR = 0.5          # the old toward-even centre, read only by shrink()
# The chance rate when nothing here measured doing nothing: half the noise
# band's stated 10% false-alarm rate (rec_learning.BASE_RATE_STATED, held
# in step by a test) — held as loosely as STATED_RATE_K results, because the
# do-nothing improved rates measured end to end ran 3-10% against it (B2 p2
# S1/S2, group Q's S1/S4); Beta(1, 19)'s 90% range, 0.3-14%, covers them.
BASE_RATE_STATED = 0.05
STATED_RATE_K = 20
# A posterior probability is never certainty either way.
ACCURACY_BOUNDS = (1, 99)
_INTEGRATION_STEPS = 200

# ── Overall ────────────────────────────────────────────────────────────────
NO_TRACK_RECORD_CAP = 70
# A record that is not 95% likely to beat doing nothing — the lower end of
# its 90% range does not clear it — is no better support than no record
# (B2 #4: 2 of 5 lifted a card from 70 to 77). Applied at ANY length, not
# only to a short record: a long record of advice that does nothing would
# otherwise read ~79% (the do-nothing simulation, test_confidence_round2_p).
BEATS_AT = 0.95
UNPROVEN_RECORD_CAP = 70
# A record more likely than not NOT to beat doing nothing leans against the
# advice: it never reads medium (0 of 10 improved read 55% "medium", B2 p1).
AGAINST_BELOW = 0.5
RECORD_AGAINST_CAP = 49
STALE_BELOW = 50
STALE_CAP = 49
# Freshness nothing could date is a penalty, not an absence (B1 H1b: a card
# with no dated source read 87% where the same card 60% fresh read 77%).
FRESHNESS_UNMEASURED_CAP = 49
# A support score never reads as certain.
MAX_OVERALL = 99
CAP_ORDER = ("no_track_record", "record_unproven", "record_against", "stale", "freshness_unmeasured")

# ── Data Freshness: states from the source's percentage ────────────────────
CURRENT_AT = 80
AGING_AT = 50

_Z90 = 1.645


def thresholds() -> dict:
    """The band cut-offs every client reads from the payload (B6 low)."""
    return {"high": HIGH_AT, "medium": MEDIUM_AT}


def caps() -> dict:
    """The ceilings overall() applies, sent with every K1 object so the
    Why? panel's plain words carry the engine's figures."""
    return {"no_track_record": NO_TRACK_RECORD_CAP, "record_unproven": UNPROVEN_RECORD_CAP,
            "record_against": RECORD_AGAINST_CAP, "stale": STALE_CAP, "stale_below": STALE_BELOW,
            "freshness_unmeasured": FRESHNESS_UNMEASURED_CAP, "beats_at": int(round(BEATS_AT * 100)),
            "against_below": int(round(AGAINST_BELOW * 100)), "max": MAX_OVERALL}


def _mdy(iso):
    """M/D/YY from an ISO date; '' when there is none (time_utils.mdy's rule,
    inlined so this module stays import-free)."""
    s = str(iso or "")[:10]
    try:
        y, m, d = int(s[0:4]), int(s[5:7]), int(s[8:10])
    except (TypeError, ValueError):
        return ""
    return f"{m}/{d}/{y % 100:02d}"


def wilson(k, n, z=_Z90):
    """90% Wilson interval for k of n as (low, high) shares; (None, None) at n=0."""
    if not n:
        return None, None
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * ((p * (1 - p) + z * z / (4 * n)) / n) ** 0.5
    return max(0.0, (c - m) / d), min(1.0, (c + m) / d)


def shrink(rate, n, prior=SHRINK_PRIOR, k=SHRINK_K):
    """A rate pulled toward `prior` by k pseudo-results. Historical Accuracy
    no longer reads it (it shrank toward even, B2 #2); kept for any caller
    that passes its own prior — candidate for future cleanup after
    additional verification."""
    if rate is None or not n:
        return prior
    return (float(rate) * n + prior * k) / (n + k)


def band(pct):
    """high | medium | low from a percentage; low when not measurable."""
    if pct is None:
        return "low"
    return "high" if pct >= HIGH_AT else ("medium" if pct >= MEDIUM_AT else "low")


def state(pct):
    """current | aging | stale for a source's freshness percentage."""
    if pct is None:
        return "unknown"
    return "current" if pct >= CURRENT_AT else ("aging" if pct >= AGING_AT else "stale")


# ── the Beta arithmetic behind Historical Accuracy ─────────────────────────

def _betacf(a, b, x):
    """The continued fraction of the regularized incomplete beta function
    (Numerical Recipes 6.4, modified Lentz)."""
    tiny, eps = 1e-300, 3e-12
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 301):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        de = d * c
        h *= de
        if abs(de - 1.0) < eps:
            break
    return h


def beta_cdf(a, b, x):
    """P(X ≤ x) for X ~ Beta(a, b), a, b > 0."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    lbt = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x)
    bt = math.exp(lbt)
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


@lru_cache(maxsize=4096)
def _p_greater(at, bt, ad, bd):
    s = max(1, int(math.ceil(1.0 / min(at, 1.0))))
    ln_b = math.lgamma(at) + math.lgamma(bt) - math.lgamma(at + bt)
    e = s * at - 1.0

    def g(t):
        # X = t^s, so the density's x^(at-1) singularity at 0 becomes the
        # bounded t^(s*at-1) and Simpson's rule converges.
        if t <= 0.0:
            return s * math.exp(-ln_b) * beta_cdf(ad, bd, 0.0) if abs(e) < 1e-12 else 0.0
        x = t ** s
        if x >= 1.0:
            return 0.0
        return s * math.exp((bt - 1.0) * math.log1p(-x) - ln_b + e * math.log(t)) * beta_cdf(ad, bd, x)
    n = _INTEGRATION_STEPS
    h = 1.0 / n
    tot = g(0.0) + g(1.0)
    for i in range(1, n):
        tot += (4.0 if i % 2 else 2.0) * g(i * h)
    return max(0.0, min(1.0, tot * h / 3.0))


def p_greater(at, bt, ad, bd) -> float:
    """P(X > Y) for independent X ~ Beta(at, bt), Y ~ Beta(ad, bd):
    E_X[F_Y(X)], integrated numerically (within 1e-4 of a 200,000-draw
    Monte Carlo on the cases the tests pin). Pure; memoised."""
    return _p_greater(round(float(at), 6), round(float(bt), 6), round(float(ad), 6), round(float(bd), 6))


# ── the three dimensions ───────────────────────────────────────────────────

def evidence(n=None, kind="count", coverage=None, flags=(), model_band=None, unverified=0, sample=False,
             basis=None, n_full=None, cap=None, cap_reason=None, corroborating=0, read_age_days=None,
             read_as_of_iso=None) -> dict:
    """{pct, basis, n, n_full, kind, corroborating} — Evidence Strength.
    `n` None means the card has no sample to weigh (a setup step): pct
    None, never a guess. `cap` is a caller's documented ceiling (a campaign
    read with no holdout) and `cap_reason` says why. `corroborating` is how
    many OTHER modules' verified figures agree (bounded, B4 M2).
    `read_age_days` / `read_as_of_iso` ride on a stored read's input
    (rec_trust.diagnosis_evidence) for Data Freshness, never Evidence
    Strength: accepted here so the input stays one dict, and ignored."""
    basis = (str(basis).strip().rstrip(".") if basis else "")
    full = float(n_full or N_FULL.get(kind) or 1)
    full_out = int(full) if float(full).is_integer() else full
    if sample:
        return {"pct": 0, "basis": "Sample data — not scored", "n": 0, "n_full": full_out, "kind": kind,
                "sample": True}
    if n is None:
        return {"pct": None, "basis": basis or "Nothing to measure yet", "n": None, "n_full": full_out,
                "kind": kind}
    try:
        n = max(0.0, float(n))
    except (TypeError, ValueError):
        return {"pct": None, "basis": basis or "Nothing to measure yet", "n": None, "n_full": full_out,
                "kind": kind}
    try:
        corr = max(0, min(CORROBORATION_MAX, int(corroborating or 0)))
    except (TypeError, ValueError):
        corr = 0
    factor = min(1.0, (n / full) * (1.0 + CORROBORATION_STEP * corr)) if full > 0 else 1.0
    cov = 1.0 if coverage is None else max(0.0, min(1.0, float(coverage)))
    flags = tuple(f for f in (flags or ()) if f)
    quality = 1.0
    for f in flags:
        quality *= QUALITY.get(f, 1.0)
    pct = 100.0 * factor * cov * quality
    notes = []
    if corr:
        notes.append(f"{corr} other module{'s' if corr != 1 else ''} agree{'s' if corr == 1 else ''}")
    if coverage is not None and cov < MIN_COVERAGE and pct > LOW_COVERAGE_CAP:
        pct = LOW_COVERAGE_CAP
        notes.append(f"only {int(round(cov * 100))}% of the window measured")
    if any(f in PARTIAL_FLAGS for f in flags) and pct > PARTIAL_CAP:
        pct = PARTIAL_CAP
        partial = [f for f in dict.fromkeys(flags) if f in PARTIAL_FLAGS]
        notes.append("an inference, not a measured cause" if "inferred" in flags else
                     _PARTIAL_NOTES[partial[0]] if len(partial) == 1 and partial[0] in _PARTIAL_NOTES else
                     "partial data")
    if unverified and pct > UNVERIFIED_CAP:
        pct = UNVERIFIED_CAP
        notes.append("a figure could not be verified")
    if model_band in MODEL_CAPS and pct > MODEL_CAPS[model_band]:
        pct = MODEL_CAPS[model_band]
        notes.append(f"the AI read rated itself {model_band}")
    if cap is not None and pct > cap:
        pct = float(cap)
        if cap_reason:
            notes.append(str(cap_reason))
    if notes:
        basis = (basis + " — " if basis else "") + "; ".join(notes)
    return {"pct": int(round(pct)), "basis": basis or "measured", "n": (int(n) if float(n).is_integer() else n),
            "n_full": full_out, "kind": kind, "corroborating": corr}


def do_nothing(record) -> dict:
    """The do-nothing rate a kind's record is compared against, as a Beta:
    {rate, alpha, beta, source, untaken_n, untaken_improved}. The kind's
    untaken results when kind_record's base rate came from MIN_MEASURED of
    them (base_rate = the untaken share shrunk by SHRINK_K toward chance —
    exactly this Beta's mean); else the chance rate (this restaurant's own
    noise bands', or the stated 5%) held as loosely as STATED_RATE_K
    results."""
    r = record or {}
    try:
        rate = float(r.get("base_rate"))
    except (TypeError, ValueError):
        rate = None
    src = str(r.get("base_rate_source") or "stated")
    if rate is None or not 0.0 < rate < 1.0:
        rate, src = BASE_RATE_STATED, "stated"
    unt = r.get("untaken") if isinstance(r.get("untaken"), dict) else {}
    un, uk = int(unt.get("measured") or 0), int(unt.get("improved") or 0)
    bn = int(r.get("base_rate_n") or 0)
    weight = (bn + SHRINK_K) if (src == "untaken" and bn >= MIN_MEASURED) else STATED_RATE_K
    if src == "untaken" and bn >= MIN_MEASURED and un < MIN_MEASURED:
        un = bn
    return {"rate": rate, "alpha": rate * weight, "beta": (1.0 - rate) * weight, "source": src,
            "untaken_n": un, "untaken_improved": uk}


def _cohort_centre(record, rate):
    """The anonymous cohort's improved share, shrunk toward the do-nothing
    rate, when kind_record found one at PRIOR_MIN_MEASURED (capped counts,
    cohort_ok, assert_anonymous upstream) — else None."""
    r = record or {}
    pm, pi = int(r.get("prior_measured") or 0), int(r.get("prior_improved") or 0)
    if pm < PRIOR_MIN_MEASURED:
        return None, pm, pi
    return (pi + rate * SHRINK_K) / float(pm + SHRINK_K), pm, pi


def accuracy(record) -> dict:
    """{pct, basis, n, improved, source, low, high, lift, prior} — Historical
    Accuracy from rec_learning.kind_record's output: how likely this kind of
    advice beats doing nothing here (module docstring). Never a figure below
    the own floor; the cohort alone never produces one (B1 H9, B2 #3)."""
    r = record or {}
    measured, improved = int(r.get("measured") or 0), int(r.get("improved") or 0)
    improved = max(0, min(improved, measured))
    dn = do_nothing(r)
    centre = dn["rate"]
    c_centre, pm, pi = _cohort_centre(r, dn["rate"])
    if measured < MIN_MEASURED or r.get("source") not in ("own", "cohort") or \
            (r.get("source") == "cohort" and measured < MIN_MEASURED):
        basis = f"Not enough history yet — {measured} measured, needs {MIN_MEASURED}"
        if c_centre is not None:
            basis += f" ({r.get('prior_label') or 'other restaurants on Cavnar'}: {pi} of {pm} improved — not counted until your own are in)"
        return {"pct": None, "basis": basis, "n": measured, "improved": improved,
                "source": "cohort" if c_centre is not None else "none", "low": None, "high": None,
                "lift": None, "prior": None}
    prior_source = "do_nothing"
    if c_centre is not None and c_centre < centre:
        # Peers saw this kind do WORSE than chance: the prior may sit lower.
        # Never higher — a peer's success is not this restaurant's.
        centre, prior_source = c_centre, "cohort"
    at = centre * SHRINK_K + improved
    bt = (1.0 - centre) * SHRINK_K + (measured - improved)
    p = p_greater(at, bt, dn["alpha"], dn["beta"])
    pct = int(max(ACCURACY_BOUNDS[0], min(ACCURACY_BOUNDS[1], round(100.0 * p))))
    lo, hi = wilson(improved, measured)
    if dn["source"] == "untaken" and dn["untaken_n"] >= MIN_MEASURED:
        vs = f"vs {dn['untaken_improved']} of {dn['untaken_n']} when not acted on"
    else:
        vs = f"vs about {int(round(100 * dn['rate']))}% by chance"
    basis = f"improved {improved} of {measured} times {vs}"
    return {"pct": pct, "basis": basis, "n": measured, "improved": improved, "source": "own",
            "low": int(round(lo * 100)), "high": int(round(hi * 100)),
            "p_beats": round(p, 4), "beats_label": f"{pct}% likely to beat doing nothing",
            "lift": {"improved": improved, "n": measured, "rate": round(improved / float(measured), 3),
                     "untaken_improved": dn["untaken_improved"], "untaken_n": dn["untaken_n"],
                     "do_nothing_rate": round(dn["rate"], 3), "source": dn["source"]},
            "prior": {"source": prior_source, "centre": round(centre, 3), "weight": SHRINK_K,
                      "cohort_measured": pm or None, "cohort_improved": pi if pm else None}}


def recency(lag_days, grace_days, horizon_days):
    """1 inside the grace period, then linear to 0 over the horizon. An
    unknown lag is 0 — never fresh."""
    if lag_days is None:
        return 0.0
    lag = float(lag_days)
    if lag <= grace_days:
        return 1.0
    if horizon_days <= 0:
        return 0.0
    return max(0.0, 1.0 - (lag - grace_days) / float(horizon_days))


def _unmeasured_why(sources, rests_on=None) -> str:
    """Why nothing confirms a card's data is current, in the owner's words:
    what it rests on when the caller knows (\"link taps only\"), else the
    sources it would read that aren't connected, else that none is."""
    if rests_on:
        return f"This is based on {rests_on}; no connected source confirms it's current"
    labels = []
    for s in (sources or []):
        lab = str((s or {}).get("label") or (s or {}).get("key") or "").strip()
        if lab and (s or {}).get("state") == "not_connected" and lab not in labels:
            labels.append(lab)
    if labels:
        names = labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + " and " + labels[-1]
        verb = "isn't" if len(labels) == 1 else "aren't"
        return f"{names} {verb} connected to confirm this is current"
    return "No connected source confirms this is current"


def freshness(sources, rests_on=None) -> dict:
    """{pct, basis, as_of, as_of_iso, stalest, stalest_basis, errors} — Data
    Freshness over the sources a card rests on (data_freshness.source_state
    dicts). The minimum, not an average: one stale source cannot hide behind
    a fresh one. A source that does not apply (not connected) carries pct
    None and is left out; `errors` names every source whose sync failed.
    With no connected source, `basis` says what the card rests on instead
    (`rests_on`, from the caller) or which sources aren't connected."""
    live = [s for s in (sources or []) if s and s.get("pct") is not None]
    errors = [str(s.get("label") or s.get("key")) for s in (sources or []) if s and s.get("error")]
    if not live:
        return {"pct": None, "basis": _unmeasured_why(sources, rests_on), "as_of": None, "as_of_iso": None,
                "stalest": None, "stalest_basis": None, "errors": errors, "unmeasured": True}
    worst = min(live, key=lambda s: (s["pct"], s.get("as_of_iso") or ""))
    basis = " · ".join(str(s.get("basis") or "").strip() for s in live if s.get("basis"))
    return {"pct": int(round(worst["pct"])), "basis": basis or "dated",
            "as_of": _mdy(worst.get("as_of_iso")) or None, "as_of_iso": worst.get("as_of_iso"),
            "stalest": worst.get("key"), "stalest_basis": str(worst.get("basis") or "").strip() or None,
            "errors": errors}


# ── the overall figure and the K1 object ───────────────────────────────────

def _combine(ev, acc, fr) -> dict:
    """{pct, caution, caps_applied, cap_reason} — the geometric mean of the
    measured dimensions, then the caps in CAP_ORDER (module docstring)."""
    e, a, f = (ev or {}).get("pct"), (acc or {}).get("pct"), (fr or {}).get("pct")
    out = {"pct": None, "caution": None, "caps_applied": [], "cap_reason": None}
    if e is None:
        return out
    cautions = []

    def geo(vals):
        if any(v <= 0 for v in vals):
            return 0.0
        return 100.0 * math.exp(sum(math.log(v / 100.0) for v in vals) / len(vals))

    # a / b — the track record's ceiling, if its condition holds.
    rec_cap = rec_name = rec_reason = None
    if a is None:
        rec_cap, rec_name = NO_TRACK_RECORD_CAP, "no_track_record"
        rec_reason = (f"no track record here yet — it can't read above {NO_TRACK_RECORD_CAP}% until "
                      f"{MIN_MEASURED} results of this kind are measured here")
        cautions.append(f"No track record here yet — it can't read high until {MIN_MEASURED} results of this "
                        "kind are measured here.")
    else:
        p = (acc or {}).get("p_beats")
        p = (a / 100.0) if p is None else float(p)
        if p < AGAINST_BELOW:
            rec_cap, rec_name = RECORD_AGAINST_CAP, "record_against"
            rec_reason = f"its record here leans against it — {a}% likely to beat doing nothing"
            cautions.append(f"Its record here leans against it: only {a}% likely to beat doing nothing.")
        elif p < BEATS_AT:
            rec_cap, rec_name = UNPROVEN_RECORD_CAP, "record_unproven"
            rec_reason = (f"its record here doesn't yet show it beats doing nothing ({a}% likely; "
                          f"{int(round(BEATS_AT * 100))}% needed to read higher)")
    # The geometric mean of what was measured, with freshness folded in
    # AFTER the record's ceiling (B3 #2): the evidence × record part is held
    # to its cap first, then freshness weighs on the held figure — so data
    # 2-5 days old reads below a fresh card even with no record here (100,
    # 93, 79, 60 and 50 fresh all read 70 before). Uncapped, this is exactly
    # the geometric mean of all three.
    core_vals = [v for v in (e, a) if v is not None]
    uncapped = geo(core_vals + ([f] if f is not None else []))
    core = geo(core_vals)
    if rec_cap is not None:
        core = min(core, float(rec_cap))
    if f is None:
        pct = core
    elif f <= 0 or core <= 0:
        pct = 0.0
    else:
        pct = 100.0 * math.exp((len(core_vals) * math.log(core / 100.0) + math.log(f / 100.0))
                               / (len(core_vals) + 1))
    if rec_cap is not None:
        pct = min(pct, float(rec_cap))
    if rec_cap is not None and pct < uncapped - 1e-9:
        out["caps_applied"].append(rec_name)
        out["cap_reason"] = rec_reason
    pct = min(pct, float(MAX_OVERALL))

    def apply(name, cap, reason):
        nonlocal pct
        if pct > cap:
            pct = float(cap)
            out["caps_applied"].append(name)
            out["cap_reason"] = reason

    # c / d — freshness, applied after the record so a broken source never
    # reads the same as a healthy one (B3 #2).
    errs = (fr or {}).get("errors") or []
    if f is not None and f < STALE_BELOW:
        what = (fr or {}).get("stalest_basis") or (fr or {}).get("basis") or "stale"
        apply("stale", STALE_CAP, f"the data under it is out of date ({what})")
        cautions.append(f"The data under this is out of date ({(fr or {}).get('basis') or 'stale'}).")
    elif f is None:
        apply("freshness_unmeasured", FRESHNESS_UNMEASURED_CAP,
              "no connected source confirms the data under it is current")
        why = str((fr or {}).get("basis") or "").strip() if (fr or {}).get("unmeasured") else ""
        cautions.append(f"{why or 'No connected source confirms this is current'}, "
                        f"so it can't read above {FRESHNESS_UNMEASURED_CAP}%.")
    if errs and not any("out of date" in c for c in cautions):
        cautions.append(f"A data source is failing ({', '.join(errs)}) — figures may stop updating.")
    # The data caution leads: it is the one an owner acts on first.
    cautions.sort(key=lambda c: 0 if ("out of date" in c or "failing" in c or "confirms" in c) else 1)
    out["pct"] = int(round(pct))
    out["caution"] = cautions[0] if cautions else None
    return out


def overall(ev, acc, fr) -> tuple:
    """(pct or None, caution or None)."""
    c = _combine(ev, acc, fr)
    return c["pct"], c["caution"]


def _common(pct, caps_applied=(), sample=False) -> dict:
    return {"meaning": MEANING, "thresholds": thresholds(), "caps": caps(),
            "caps_applied": list(caps_applied or ()), "sample": bool(sample)}


def assemble(ev, acc, fr) -> dict:
    """The K1 confidence object from the three dimensions."""
    c = _combine(ev, acc, fr)
    pct = c["pct"]
    dims = {"evidence": ev, "accuracy": acc, "freshness": fr}
    if pct is None:
        reason = (ev or {}).get("basis") or "Nothing to measure yet"
    elif c["cap_reason"]:
        # The cap that set the figure, said on the line (B1 M5).
        reason = c["cap_reason"]
    else:
        present = [(d["pct"], name) for name, d in dims.items() if d and d.get("pct") is not None]
        weakest = min(present)[1] if present else "evidence"
        reason = (dims[weakest] or {}).get("basis")
    out = {"pct": pct, "band": band(pct),
           "label": f"{pct}% confidence" if pct is not None else "Confidence not yet measurable",
           "reason": reason, "score": round(pct / 100.0, 2) if pct is not None else 0.0,
           "caution": c["caution"], "dimensions": dims, "version": VERSION}
    out.update(_common(pct, c["caps_applied"], sample=bool((ev or {}).get("sample"))))
    # The Recommendation Confidence Impact rides on every K1 object, so the
    # Why? drawer can say "94% once inventory counts are current" (#22):
    # None unless fresher data would move it IMPACT_MIN_DELTA points.
    imp = freshness_impact(out)
    out["confidence_impact"] = imp if imp and (imp.get("delta") or 0) >= IMPACT_MIN_DELTA else None
    return out


CURRENT_FRESHNESS = {"pct": 100, "errors": [], "basis": "every source current"}
# A confidence impact under this many points is not worth saying.
IMPACT_MIN_DELTA = 3


def impact(ev, acc, fr) -> dict:
    """{now, when_current, delta} — the same three dimensions scored as they
    are and again with Data Freshness replaced by a current source. The
    evidence and track-record caps still bind, so a card held at 70 by "no
    track record" shows no impact from fresher data. Pure; it re-reads
    _combine and never writes, so freshness is never counted twice
    (the Recommendation Confidence Impact, DH5 §2.5)."""
    now = _combine(ev, acc, fr)["pct"]
    best = _combine(ev, acc, CURRENT_FRESHNESS)["pct"]
    if now is None or best is None:
        return {"now": now, "when_current": best, "delta": None}
    return {"now": now, "when_current": best, "delta": max(0, best - now)}


def freshness_impact(conf) -> dict:
    """The Recommendation Confidence Impact of one K1 object: {now,
    when_current, delta, blocked_by, basis} — what the figure would read with
    every source current, and the stalest source holding it down. None for
    an unmeasurable or sample conf."""
    if not isinstance(conf, dict) or conf.get("pct") is None or conf.get("sample"):
        return None
    d = conf.get("dimensions") or {}
    out = impact(d.get("evidence"), d.get("accuracy"), d.get("freshness"))
    fr = d.get("freshness") or {}
    out["blocked_by"] = fr.get("stalest") if (out.get("delta") or 0) >= IMPACT_MIN_DELTA else None
    out["basis"] = fr.get("stalest_basis") or fr.get("basis")
    return out


def unknown(reason="Confidence could not be measured") -> dict:
    """Every dimension unmeasured: low band, never medium (CA6 duplicate #2)."""
    out = {"pct": None, "band": "low", "label": "Confidence not yet measurable", "reason": reason,
           "score": 0.0, "caution": None,
           "dimensions": {"evidence": {"pct": None, "basis": reason, "n": None, "n_full": None},
                          "accuracy": {"pct": None, "basis": reason, "n": 0, "improved": 0, "source": "none",
                                       "low": None, "high": None, "lift": None, "prior": None},
                          "freshness": {"pct": None, "basis": reason, "as_of": None, "as_of_iso": None,
                                        "stalest": None, "stalest_basis": None, "errors": []}},
           "version": VERSION}
    out.update(_common(None))
    return out


def snapshot(conf) -> dict:
    """The rec_instances / shown-meta snapshot fields (contract K3) from a
    K1 object; every field None when there is none."""
    c = conf if isinstance(conf, dict) else {}
    d = c.get("dimensions") or {}
    acc = d.get("accuracy") or {}
    fr = d.get("freshness") or {}
    return {"confidence_pct": c.get("pct"), "evidence_pct": (d.get("evidence") or {}).get("pct"),
            "accuracy_pct": acc.get("pct"), "accuracy_n": acc.get("n") if acc else None,
            "freshness_pct": fr.get("pct"), "freshness_as_of": fr.get("as_of_iso"),
            "trust_version": c.get("version") if c else None}


# ── calibration (admin only) ───────────────────────────────────────────────
#
# The support score is not a probability, so it is not checked as one: the
# check is ORDER — a higher band must not do worse than a lower one
# (ordering() below). reliability() and brier() stay for the record,
# labelled in the admin view as not the meaning of the %.

def decile(pct) -> str:
    """"70-79" for a percentage; "90-100" at the top."""
    p = max(0, min(100, int(pct)))
    lo = min(90, (p // 10) * 10)
    return f"{lo}-{100 if lo == 90 else lo + 9}"


def reliability(pairs, floor_n) -> list:
    """[(pct, improved 0|1)] → one row per decile with anything in it:
    {range, n, predicted_mean, observed_rate, low, high, enough}. The observed
    rate and its interval are withheld below `floor_n`."""
    groups = {}
    for pct, y in pairs or []:
        if pct is None:
            continue
        groups.setdefault(decile(pct), []).append((float(pct), 1 if y else 0))
    rows = []
    for rng in sorted(groups, key=lambda r: int(r.split("-")[0])):
        g = groups[rng]
        n = len(g)
        k = sum(y for _, y in g)
        enough = n >= floor_n
        lo, hi = wilson(k, n) if enough else (None, None)
        rows.append({"range": rng, "n": n, "predicted_mean": round(sum(p for p, _ in g) / n, 1),
                     "observed_rate": round(100.0 * k / n, 1) if enough else None,
                     "low": round(lo * 100, 1) if lo is not None else None,
                     "high": round(hi * 100, 1) if hi is not None else None, "enough": enough})
    return rows


def brier(pairs, floor_n):
    """Mean squared error of pct/100 against the 0/1 result; None below the
    floor. NOT the meaning of the support score (it is not a probability):
    kept for the record only."""
    ps = [(float(p) / 100.0, 1 if y else 0) for p, y in (pairs or []) if p is not None]
    if len(ps) < floor_n:
        return None
    return round(sum((p - y) ** 2 for p, y in ps) / len(ps), 4)


ORDER_BANDS = (("0-49", 0, 49), ("50-74", 50, 74), ("75-100", 75, 100))


def _rank(xs):
    """Average ranks (1-based), ties shared."""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2.0 + 1.0
        i = j + 1
    return ranks


def spearman(pairs):
    """Spearman's rho between the % and the 0/1 result, or None when either
    side does not vary."""
    ps = [(float(p), 1.0 if y else 0.0) for p, y in (pairs or []) if p is not None]
    if len(ps) < 3:
        return None
    rx, ry = _rank([p for p, _ in ps]), _rank([y for _, y in ps])
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    sxy = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    sxx = sum((a - mx) ** 2 for a in rx)
    syy = sum((b - my) ** 2 for b in ry)
    if sxx <= 0 or syy <= 0:
        return None
    return round(sxy / math.sqrt(sxx * syy), 3)


def ordering(pairs, floor_n, bands=ORDER_BANDS) -> dict:
    """Whether a higher support score really goes with better results
    (the owner's check, 9/24/26): the observed improved rate by band with
    its 90% Wilson range, which must not DECREASE from a lower band to a
    higher one. A violation is a higher band whose range sits wholly below
    a lower band's (beyond its interval, not noise); a band under `floor_n`
    is withheld and never judged. Plus Spearman's rho between the % and the
    result at the floor.
      {bands: [{range, n, observed_rate, low, high, enough}], ordered: bool
       | None, violations: [{higher, lower, higher_high, lower_low}],
       spearman, n}"""
    ps = [(p, 1 if y else 0) for p, y in (pairs or []) if p is not None]
    rows = []
    for name, lo_b, hi_b in bands:
        g = [y for p, y in ps if lo_b <= int(p) <= hi_b]
        n, k = len(g), sum(g)
        enough = n >= floor_n
        lo, hi = wilson(k, n) if enough else (None, None)
        rows.append({"range": name, "n": n, "observed_rate": round(100.0 * k / n, 1) if enough else None,
                     "low": round(lo * 100, 1) if lo is not None else None,
                     "high": round(hi * 100, 1) if hi is not None else None, "enough": enough})
    judged = [r for r in rows if r["enough"]]
    violations = []
    for i, lower in enumerate(judged):
        for higher in judged[i + 1:]:
            if higher["high"] < lower["low"]:
                violations.append({"higher": higher["range"], "lower": lower["range"],
                                   "higher_high": higher["high"], "lower_low": lower["low"]})
    return {"bands": rows, "ordered": (None if len(judged) < 2 else not violations), "violations": violations,
            "spearman": spearman(ps) if len(ps) >= floor_n else None, "n": len(ps)}
