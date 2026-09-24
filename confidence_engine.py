"""
confidence_engine.py — the Recommendation Confidence arithmetic. Pure: no
I/O, no database, no clock. Every number here is computed from inputs the
caller measured; nothing is a stand-in.

A recommendation's confidence is three measured dimensions and one overall
percentage (contract K1, INTELLIGENCE_ENGINE.md → Recommendation Confidence):

  Evidence Strength  = 100 × sample_factor × coverage × quality, then capped.
      sample_factor = min(1, n / N_FULL[kind]) — how much of a full sample
      stands behind the card (N_FULL below, each with its reason);
      coverage      = the share of the window's trading days measured
      (metrics.coverage), or 1 when the figure is not a trading-day metric;
      quality       = the product of QUALITY multipliers for what is known to
      be weaker (hours estimated from the schedule, gross sales missing …).
      Caps: coverage under MIN_COVERAGE → at most LOW_COVERAGE_CAP; any
      partial-data flag → at most PARTIAL_CAP; any unverified figure → at
      most UNVERIFIED_CAP; a model's own band only LOWERS it (MODEL_CAPS),
      never raises it. Sample or demo data scores 0 and says so.
  Historical Accuracy = this restaurant's measured improved ÷ measured for
      the recommendation kind (rec_learning.kind_record, read only through
      learned_verdict), shrunk toward even by SHRINK_K pseudo-results, shown
      only at MIN_MEASURED own results; else the anonymous cohort's record
      at PRIOR_MIN_MEASURED; else not measurable (pct None).
  Data Freshness     = 100 × min over the card's sources of recency × completeness
      (data_freshness.source_state; the recency curve is recency() below).
  Overall            = the geometric mean of the dimensions that could be
      measured. No evidence → not measurable. No track record here → at most
      NO_TRACK_RECORD_CAP (it cannot read high on a first showing). Data
      Freshness under STALE_BELOW → at most STALE_CAP.

The band words kept for older clients are derived from the percentage
(HIGH_AT / MEDIUM_AT); `score` is pct / 100 and 0.0 when not measurable,
because shipped iOS builds decode it as a non-optional number.
"""
import math

VERSION = 1

# ── bands (derived, for older clients and the colour map) ──────────────────
HIGH_AT = 75
MEDIUM_AT = 50

# ── Evidence Strength ──────────────────────────────────────────────────────
#
# How many observations make a full sample, per kind of evidence. One table,
# each with the reason it is that number (CA6 §B; the old per-card bands).
N_FULL = {
    # Eight reviews on one theme: below it one guest moves the share by more
    # than a tenth (Home's old review cards called 8 "high").
    "reviews": 8,
    # Four of one weekday = a month of that day; one bad Tuesday is an
    # anecdote (the trim-day card's old "high" floor).
    "weekdays": 4,
    # Eight weeks is the window every weekly series here reads (waste
    # history, the rating trend, marketing baselines).
    "weeks": 8,
    # An item over its tolerance in four of the last eight weeks is a
    # standing pattern, not one bad count (food_cost_intelligence).
    "waste_weeks": 4,
    # A price that has held for four weekly readings is a trend, not a
    # single invoice line.
    "price_weeks": 4,
    # 28 trading days is labor % and food cost %'s own window (metrics).
    "trading_days": 28,
    # Four posts / campaigns before one is compared against the others.
    "posts": 4,
    "campaigns": 4,
    # Three nearby competitors before a comparison says anything about a
    # market rather than one neighbour.
    "competitors": 3,
    # A model diagnosis citing three verified figures from the ledger or
    # another system (operational_evidence) is as supported as the
    # diagnosis prompts ask it to be.
    "evidence_items": 3,
    # A DSR action cites measured facts of one night; three is the most a
    # line may cite (dsr.narrative.MAX_CITES).
    "night_facts": 3,
    # A direct count of rows on file (drafts written, reviews flagged, items
    # below par): the figure IS the count, so one row is the whole sample.
    "count": 1,
}

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
    "stale_read": 0.8,             # a stored model read over a week old
    "sampled": 0.8,                # Google Places returns five reviews at a time
    "list_prices": 0.8,            # supplier list prices, not what was paid
    "no_sales_mix": 0.8,           # a per-plate figure with no units sold
}
# Flags that mean the figure covers only part of what it claims: each caps
# evidence at PARTIAL_CAP (on top of any QUALITY multiplier).
PARTIAL_FLAGS = ("days_missing_sales", "hours_are_estimated", "gross_missing", "days_with_conflicting_sales",
                 "partial", "inferred", "sampled", "provisional", "period_too_short")

# ── Historical Accuracy ────────────────────────────────────────────────────
MIN_MEASURED = 5            # rec_learning.MIN_MEASURED_FOR_RATE (held in step by a test)
PRIOR_MIN_MEASURED = 10     # rec_learning.PRIOR_MIN_MEASURED
SHRINK_K = 5                # pseudo-results pulling a rate toward even
SHRINK_PRIOR = 0.5

# ── Overall ────────────────────────────────────────────────────────────────
NO_TRACK_RECORD_CAP = 70
STALE_BELOW = 50
STALE_CAP = 49

# ── Data Freshness: states from the source's percentage ────────────────────
CURRENT_AT = 80
AGING_AT = 50

_Z90 = 1.645


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


# ── the three dimensions ───────────────────────────────────────────────────

def evidence(n=None, kind="count", coverage=None, flags=(), model_band=None, unverified=0, sample=False,
             basis=None, n_full=None, cap=None, cap_reason=None) -> dict:
    """{pct, basis, n, kind} — Evidence Strength. `n` None means the card has
    no sample to weigh (a setup step): pct None, never a guess. `cap` is a
    caller's documented ceiling (a campaign read with no holdout) and
    `cap_reason` says why."""
    basis = (str(basis).strip().rstrip(".") if basis else "")
    if sample:
        return {"pct": 0, "basis": "Sample data — not scored", "n": 0, "kind": kind}
    if n is None:
        return {"pct": None, "basis": basis or "Nothing to measure yet", "n": None, "kind": kind}
    try:
        n = max(0.0, float(n))
    except (TypeError, ValueError):
        return {"pct": None, "basis": basis or "Nothing to measure yet", "n": None, "kind": kind}
    full = float(n_full or N_FULL.get(kind) or 1)
    factor = min(1.0, n / full) if full > 0 else 1.0
    cov = 1.0 if coverage is None else max(0.0, min(1.0, float(coverage)))
    flags = tuple(f for f in (flags or ()) if f)
    quality = 1.0
    for f in flags:
        quality *= QUALITY.get(f, 1.0)
    pct = 100.0 * factor * cov * quality
    notes = []
    if coverage is not None and cov < MIN_COVERAGE and pct > LOW_COVERAGE_CAP:
        pct = LOW_COVERAGE_CAP
        notes.append(f"only {int(round(cov * 100))}% of the window measured")
    if any(f in PARTIAL_FLAGS for f in flags) and pct > PARTIAL_CAP:
        pct = PARTIAL_CAP
        notes.append("partial data")
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
            "kind": kind}


def accuracy(record) -> dict:
    """{pct, basis, n, improved, source, low, high} — Historical Accuracy from
    rec_learning.kind_record's output. Never a figure below the floors."""
    r = record or {}
    measured, improved = int(r.get("measured") or 0), int(r.get("improved") or 0)
    src = r.get("source")
    if src == "own" and measured >= MIN_MEASURED:
        lo, hi = wilson(improved, measured)
        return {"pct": int(round(100 * shrink(improved / measured, measured))),
                "basis": f"{improved} of {measured} measured changes like this improved here",
                "n": measured, "improved": improved, "source": "own",
                "low": int(round(lo * 100)), "high": int(round(hi * 100))}
    pm, pi = int(r.get("prior_measured") or 0), int(r.get("prior_improved") or 0)
    if src == "cohort" and pm >= PRIOR_MIN_MEASURED:
        lo, hi = wilson(pi, pm)
        return {"pct": int(round(100 * shrink(pi / pm, pm))),
                "basis": f"At restaurants like yours: {pi} of {pm} improved",
                "n": pm, "improved": pi, "source": "cohort", "own_n": measured,
                "low": int(round(lo * 100)), "high": int(round(hi * 100))}
    return {"pct": None, "basis": f"Not enough history yet — {measured} measured, needs {MIN_MEASURED}",
            "n": measured, "improved": improved, "source": "none", "low": None, "high": None}


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


def freshness(sources) -> dict:
    """{pct, basis, as_of, as_of_iso, stalest} — Data Freshness over the
    sources a card rests on (data_freshness.source_state dicts). The minimum,
    not an average: one stale source cannot hide behind a fresh one. A source
    that does not apply (not connected) carries pct None and is left out."""
    live = [s for s in (sources or []) if s and s.get("pct") is not None]
    if not live:
        return {"pct": None, "basis": "No connected source dates this", "as_of": None, "as_of_iso": None,
                "stalest": None}
    worst = min(live, key=lambda s: (s["pct"], s.get("as_of_iso") or ""))
    basis = " · ".join(str(s.get("basis") or "").strip() for s in live if s.get("basis"))
    return {"pct": int(round(worst["pct"])), "basis": basis or "dated",
            "as_of": _mdy(worst.get("as_of_iso")) or None, "as_of_iso": worst.get("as_of_iso"),
            "stalest": worst.get("key")}


# ── the overall figure and the K1 object ───────────────────────────────────

def overall(ev, acc, fr) -> tuple:
    """(pct or None, caution or None)."""
    e, a, f = (ev or {}).get("pct"), (acc or {}).get("pct"), (fr or {}).get("pct")
    if e is None:
        return None, None
    vals = [v for v in (e, a, f) if v is not None]
    if any(v <= 0 for v in vals):
        pct = 0.0
    else:
        pct = 100.0 * math.exp(sum(math.log(v / 100.0) for v in vals) / len(vals))
    caution = None
    if a is None and pct > NO_TRACK_RECORD_CAP:
        pct = NO_TRACK_RECORD_CAP
    if a is None:
        caution = (f"No track record here yet — it can't read high until {MIN_MEASURED} results of this kind "
                   "are measured here.")
    if f is not None and f < STALE_BELOW:
        pct = min(pct, STALE_CAP)
        caution = f"The data under this is out of date ({(fr or {}).get('basis') or 'stale'})."
    return int(round(pct)), caution


def assemble(ev, acc, fr) -> dict:
    """The K1 confidence object from the three dimensions."""
    pct, caution = overall(ev, acc, fr)
    dims = {"evidence": ev, "accuracy": acc, "freshness": fr}
    if pct is None:
        reason = (ev or {}).get("basis") or "Nothing to measure yet"
    else:
        present = [(d["pct"], name) for name, d in dims.items() if d and d.get("pct") is not None]
        weakest = min(present)[1] if present else "evidence"
        reason = (dims[weakest] or {}).get("basis")
    return {"pct": pct, "band": band(pct),
            "label": f"{pct}% confidence" if pct is not None else "Confidence not yet measurable",
            "reason": reason, "score": round(pct / 100.0, 2) if pct is not None else 0.0,
            "caution": caution, "dimensions": dims, "version": VERSION}


def unknown(reason="Confidence could not be measured") -> dict:
    """Every dimension unmeasured: low band, never medium (CA6 duplicate #2)."""
    return {"pct": None, "band": "low", "label": "Confidence not yet measurable", "reason": reason,
            "score": 0.0, "caution": None,
            "dimensions": {"evidence": {"pct": None, "basis": reason, "n": None},
                           "accuracy": {"pct": None, "basis": reason, "n": 0, "improved": 0, "source": "none",
                                        "low": None, "high": None},
                           "freshness": {"pct": None, "basis": reason, "as_of": None, "as_of_iso": None,
                                         "stalest": None}},
            "version": VERSION}


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
    """Mean squared error of pct/100 against the 0/1 result; None below the floor."""
    ps = [(float(p) / 100.0, 1 if y else 0) for p, y in (pairs or []) if p is not None]
    if len(ps) < floor_n:
        return None
    return round(sum((p - y) ** 2 for p, y in ps) / len(ps), 4)
