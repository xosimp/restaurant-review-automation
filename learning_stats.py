"""
learning_stats.py — the one set of statistics the schedule learners share
(schedule audit 10/10/26 ARCH-28).

schedule_versions (the manager's edit patterns and the standing patterns),
schedule_learning (retimes, headcount, role changes, leader swaps, the edit
predictor, calibration) and schedule_memory (the compact memory, teams,
openers) each used to define their own half-life, recency weight and
Wilson bound over overlapping evidence, so a fix to one learner's bias
never reached the others. They now read these, and keep their old names as
aliases of them (a deletion of those names is high-risk: tests and other
modules call them).

Pure: no I/O, no app imports (L0).

  recency_weight(age, half_life)    1.0 today, 0.5 one half-life ago
  wilson_lower / wilson_interval    the rate's 90% Wilson bound(s)
  strength(hits_w, opps_w)          (rate, confidence) of weighted counts
  binom_tail / fisher_tail / holm   the team test (schedule_memory._learn_pairs)
  change_point(series)              where the newest regime of a 0/1 series
                                    starts — a habit counted from its onset,
                                    not averaged over the 24-week window
                                    (LEARNING-2)
  since_change(series)              the weighted hits and opportunities of
                                    that newest regime
  weighted_rate_ci(weights, n)      a weighted count per 100 rows with a 90%
                                    interval (the Draft Rework Rate, RM-4)
  mean_ci(values)                   a mean with a 90% normal interval
  fe_slope_ci(rows)                 the within-restaurant slope of a value
                                    over weeks since onboarding, with a
                                    restaurant-cluster 90% interval, t on
                                    G - 1 degrees of freedom (RM-4;
                                    LEARNING-5)
  trend_slope_ci(values)            ONE series' slope over its weeks with a
                                    Newey-West (HAC) interval, t on n - 2
                                    (the owner's own trend - LEARNING-17)
  t_cdf / t_ppf / t_pvalue          Student's t, pure
  always_valid_p(est, se, tau)      the mixture-SPRT p-value a readout may
                                    be peeked at every week (LEARNING-15)
  fleet_sample(ids, k, key)         a stratified sample across the fleet,
                                    rotated by `key` (LEARNING-23)
  ols(...)                          a small ridge-penalised least squares
                                    (calibration v2, LEARNING-8)
"""
import hashlib
import math

WILSON_Z = 1.645                 # 90%: the experiment readout's intervals use it
Z90 = 1.645
PERSON_HALF_LIFE_DAYS = 120      # a person on a slot
SLOT_HALF_LIFE_DAYS = 180        # a role's headcount, start time, an opener, a pair

# A change point is called only when splitting the series there explains it
# this much better than one rate (a log-likelihood ratio; about 99% for one
# split, a margin for having looked at every split), and the newest regime
# has at least CHANGE_MIN_SEGMENT observations.
CHANGE_MIN_LLR = 3.3
CHANGE_MIN_SEGMENT = 2
CHANGE_MIN_DOWN_SEGMENT = 3


def recency_weight(age_days, half_life_days) -> float:
    """1.0 for evidence from today, 0.5 one half-life ago."""
    try:
        return 0.5 ** (max(0.0, float(age_days or 0)) / float(half_life_days))
    except (TypeError, ValueError, ZeroDivisionError):
        return 1.0


def wilson_interval(hits, n, z=WILSON_Z) -> tuple:
    """(lower, upper) Wilson bounds of hits/n — 2 of 2 is far less sure than
    20 of 20. Weighted counts are allowed. (0.0, 1.0) with no opportunities."""
    try:
        n, hits = float(n), float(hits)
    except (TypeError, ValueError):
        return 0.0, 1.0
    if n <= 0:
        return 0.0, 1.0
    p = min(1.0, max(0.0, hits / n))
    z2 = z * z
    centre = p + z2 / (2 * n)
    margin = z * math.sqrt(max(0.0, p * (1 - p) / n + z2 / (4 * n * n)))
    lo = max(0.0, (centre - margin) / (1 + z2 / n))
    hi = min(1.0, (centre + margin) / (1 + z2 / n))
    return lo, hi


def wilson_lower(hits, n, z=WILSON_Z) -> float:
    """The lower bound of the rate hits/n. 0.0 with no opportunities."""
    try:
        if float(n) <= 0:
            return 0.0
    except (TypeError, ValueError):
        return 0.0
    return wilson_interval(hits, n, z)[0]


def strength(hits_w, opps_w) -> tuple:
    """(rate, confidence) of weighted hits over weighted opportunities."""
    if not opps_w or opps_w <= 0:
        return 0.0, 0.0
    return round(min(1.0, hits_w / opps_w), 3), round(wilson_lower(hits_w, opps_w), 3)


def binom_tail(k, n, p, upper=True) -> float:
    """P(X >= k) (or P(X <= k) with upper False) for X ~ Binomial(n, p)."""
    n, k = int(n), int(k)
    p = min(1.0, max(0.0, float(p)))
    rng = range(k, n + 1) if upper else range(0, k + 1)
    return min(1.0, sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in rng))


def fisher_tail(hits, n, other_hits, other_n, upper=True) -> float:
    """One-sided Fisher exact p-value that `hits` of `n` is higher (upper)
    or lower than `other_hits` of `other_n` beyond chance — both rates
    measured, neither taken as known (the hypergeometric tail)."""
    hits, n, oh, on = int(hits), int(n), int(other_hits), int(other_n)
    total, good = n + on, hits + oh
    if n <= 0 or on <= 0:
        return 1.0
    denom = math.comb(total, n)
    lo, hi = max(0, n - (total - good)), min(n, good)
    rng = range(hits, hi + 1) if upper else range(lo, hits + 1)
    return min(1.0, sum(math.comb(good, i) * math.comb(total - good, n - i) for i in rng) / float(denom))


def holm(pvalues: dict, alpha=0.05) -> set:
    """The keys whose p-value survives Holm's step-down over all of them."""
    out = set()
    m = len(pvalues)
    for i, (k, pv) in enumerate(sorted(pvalues.items(), key=lambda kv: (kv[1], str(kv[0])))):
        if pv > alpha / float(m - i):
            break
        out.add(k)
    return out


def _bern_ll(k, n) -> float:
    if n <= 0 or k <= 0 or k >= n:
        return 0.0
    p = k / float(n)
    return k * math.log(p) + (n - k) * math.log(1 - p)


def change_point(series, min_llr=CHANGE_MIN_LLR, min_segment=CHANGE_MIN_SEGMENT,
                 down_min_segment=CHANGE_MIN_DOWN_SEGMENT) -> int:
    """Where the newest regime of a chronological 0/1 series starts (an index
    into it), or 0 when one rate explains it as well — the single most
    likely change of a Bernoulli rate (the maximum log-likelihood ratio over
    every split), called only when the ratio clears `min_llr` and the newest
    regime has `min_segment` observations (and, when the rate FELL,
    `down_min_segment` of them misses: a habit is let go on a little more
    evidence than it is picked up). `series` items are truthy/falsy hits, or (hit, ...) tuples (the
    first element is read)."""
    xs = [1 if (s[0] if isinstance(s, (tuple, list)) else s) else 0 for s in (series or [])]
    n = len(xs)
    if n < min_segment + 1:
        return 0
    total = sum(xs)
    whole = _bern_ll(total, n)
    best, at = 0.0, 0
    before = 0
    for i in range(1, n - min_segment + 1):
        before += xs[i - 1]
        after_n = n - i
        after_hits = total - before
        if after_hits / float(after_n) < before / float(i) and after_n - after_hits < down_min_segment:
            continue                      # a fall needs that many weeks without it
        llr = _bern_ll(before, i) + _bern_ll(total - before, after_n) - whole
        if llr > best + 1e-12:
            best, at = llr, i
    return at if best >= min_llr else 0


def since_change(series, min_llr=CHANGE_MIN_LLR, min_segment=CHANGE_MIN_SEGMENT,
                 down_min_segment=CHANGE_MIN_DOWN_SEGMENT) -> dict:
    """The newest regime of a chronological series of (hit, weight)
    opportunities: {start, hits, opps, hits_w, opps_w, changed}. A habit is
    counted from its onset (LEARNING-2): a manager who starts taking Bob off
    Tuesday dinner after 24 weeks of leaving him on is two of two, not two
    of twenty-six."""
    items = []
    for s in series or []:
        if isinstance(s, (tuple, list)):
            hit = bool(s[0])
            w = float(s[1]) if len(s) > 1 and s[1] is not None else 1.0
        else:
            hit, w = bool(s), 1.0
        items.append((hit, w))
    start = change_point(items, min_llr, min_segment, down_min_segment)
    seg = items[start:]
    return {"start": start, "changed": start > 0, "hits": sum(1 for h, _w in seg if h), "opps": len(seg),
            "hits_w": sum(w for h, w in seg if h), "opps_w": sum(w for _h, w in seg)}


def weighted_rate_ci(weights, n, per=100.0, z=Z90) -> dict:
    """A weighted count per `per` units with a 90% interval: rate =
    Σw / n × per; the interval treats each item as a Poisson event of its
    weight (var = Σw²) — the Draft Rework Rate per 100 drafted rows (RM-4).
    {rate, lo, hi, n, items}; rate None with no rows."""
    ws = [float(w) for w in (weights or []) if w is not None]
    try:
        n = float(n)
    except (TypeError, ValueError):
        n = 0.0
    if n <= 0:
        return {"rate": None, "lo": None, "hi": None, "n": 0, "items": len(ws)}
    s = sum(ws)
    var = sum(w * w for w in ws)
    rate = s / n * per
    half = z * math.sqrt(var) / n * per if ws else z * per / n   # nothing seen: about one item's width
    return {"rate": round(rate, 2), "lo": round(max(0.0, rate - half), 2), "hi": round(rate + half, 2),
            "n": int(n), "items": len(ws)}


def mean_ci(values, z=Z90) -> dict:
    """{n, mean, lo, hi, sd} with a normal 90% interval (None under two)."""
    vals = [float(v) for v in values if v is not None]
    n = len(vals)
    if not n:
        return {"n": 0, "mean": None, "lo": None, "hi": None, "sd": None}
    m = sum(vals) / n
    if n < 2:
        return {"n": n, "mean": round(m, 4), "lo": None, "hi": None, "sd": None}
    sd = math.sqrt(sum((v - m) ** 2 for v in vals) / (n - 1))
    half = z * sd / math.sqrt(n)
    return {"n": n, "mean": round(m, 4), "lo": round(m - half, 4), "hi": round(m + half, 4), "sd": round(sd, 4)}


def _betacf(a, b, x):
    """The incomplete beta's continued fraction (Lentz; Numerical Recipes)."""
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 300):
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
        if abs(de - 1.0) < 1e-12:
            break
    return h


def _beta_inc(x, a, b):
    """The regularized incomplete beta I_x(a, b). Pure (the same arithmetic
    as metrics._betainc, which this L0 module may not import)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    bt = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1.0 - x))
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def t_cdf(t, df) -> float:
    """P(T <= t) for Student's t with `df` degrees of freedom (the normal
    when df is None). Pure."""
    if df is None:
        return 0.5 * (1.0 + math.erf(float(t) / math.sqrt(2.0)))
    df, t = float(df), float(t)
    tail = 0.5 * _beta_inc(df / (df + t * t), df / 2.0, 0.5)
    return 1.0 - tail if t >= 0 else tail


def t_ppf(q, df) -> float:
    """The q-quantile of Student's t (bisection on t_cdf). Pure."""
    lo, hi = -1000.0, 1000.0
    for _ in range(80):
        mid = (lo + hi) / 2.0
        if t_cdf(mid, df) < q:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def t90(df) -> float:
    """The two-sided 90% quantile of t on `df` degrees of freedom (z when
    df is None)."""
    return Z90 if df is None else t_ppf(0.95, max(1, int(df)))


def t_pvalue(est, se, df=None, side=0) -> float:
    """The p-value of est / se: two-sided (side 0), or one-sided that the
    truth is above zero (side +1) or below it (side -1). 1.0 without a
    standard error."""
    try:
        est, se = float(est), float(se)
    except (TypeError, ValueError):
        return 1.0
    if se <= 0:
        return 1.0 if est == 0 else 0.0
    t = est / se
    if side > 0:
        return 1.0 - t_cdf(t, df)
    if side < 0:
        return t_cdf(t, df)
    return min(1.0, 2.0 * (1.0 - t_cdf(abs(t), df)))


def always_valid_p(est, se, tau) -> float:
    """The always-valid p-value of the mixture sequential probability ratio
    test (Johari et al., the normal mixture with variance tau^2): an
    estimate read again every week may be peeked at without the false-call
    rate growing with the number of looks - p = min(1, 1 / L),
    L = sqrt(V / (V + tau^2)) * exp(tau^2 est^2 / (2 V (V + tau^2))),
    V = se^2. `tau` is the size of effect worth finding (where the test is
    most powerful). 1.0 without a standard error. Pure."""
    try:
        est, se, tau = float(est), float(se), float(tau)
    except (TypeError, ValueError):
        return 1.0
    if se <= 0 or tau <= 0:
        return 1.0
    v, t2 = se * se, tau * tau
    log_lam = 0.5 * math.log(v / (v + t2)) + t2 * est * est / (2.0 * v * (v + t2))
    return 1.0 if log_lam <= 0 else min(1.0, math.exp(-log_lam))


def fe_slope_ci(rows, z=None) -> dict:
    """The within-restaurant slope of y over x with a restaurant-cluster
    (CR1) 90% interval: rows = [(restaurant, x, y)]. Each restaurant's own
    mean is taken out first (restaurant fixed effects), so a restaurant that
    started with a better draft is not read as improvement. The quantile is
    Student's t on G - 1 degrees of freedom for G clusters (blind re-audit
    10/11/26 LEARNING-5: a z on 7 weekday clusters made the interval too
    narrow). One restaurant: the plain OLS slope with t on n - 2 - for one
    restaurant's own series over weeks, trend_slope_ci (serial-correlation
    robust) is the right reading. `z` overrides the quantile. {slope, lo,
    hi, se, df, n, restaurants}; slope None with too little spread."""
    pts = [(r, float(x), float(y)) for r, x, y in rows if x is not None and y is not None]
    by = {}
    for r, x, y in pts:
        by.setdefault(r, []).append((x, y))
    dm = []
    for r, xy in by.items():
        mx = sum(x for x, _y in xy) / len(xy)
        my = sum(y for _x, y in xy) / len(xy)
        dm += [(r, x - mx, y - my) for x, y in xy]
    sxx = sum(x * x for _r, x, _y in dm)
    n, g = len(dm), len(by)
    if n < 3 or sxx <= 0:
        return {"slope": None, "lo": None, "hi": None, "se": None, "df": None, "n": n, "restaurants": g}
    b = sum(x * y for _r, x, y in dm) / sxx
    if g >= 2:
        u = {}
        for r, x, y in dm:
            u[r] = u.get(r, 0.0) + x * (y - b * x)
        se = math.sqrt(g / (g - 1.0) * sum(v * v for v in u.values())) / sxx
        df = g - 1
    else:
        resid = sum((y - b * x) ** 2 for _r, x, y in dm)
        se = math.sqrt(resid / max(1, n - 2) / sxx)
        df = max(1, n - 2)
    q = float(z) if z is not None else t90(df)
    return {"slope": round(b, 4), "lo": round(b - q * se, 4), "hi": round(b + q * se, 4), "se": round(se, 6),
            "df": df, "n": n, "restaurants": g}


def trend_slope_ci(values, min_n=8) -> dict:
    """ONE series' trend over its index (weeks, oldest first; None values
    are skipped but keep their place): the OLS slope with a Newey-West
    (HAC) standard error - Bartlett weights to lag floor(4 (n/100)^(2/9)) + 1,
    so a run of good or bad weeks (weekly noise is serially correlated) is
    not read as a trend - and Student's t on n - 2 (blind re-audit 10/11/26
    LEARNING-17: a plain OLS error with z from 4 weeks told 11-17% of owners
    with no trend at all that their drafts were improving). No slope below
    `min_n` values. {slope, lo, hi, se, df, n, lags}."""
    pts = [(float(i), float(v)) for i, v in enumerate(values or []) if v is not None]
    n = len(pts)
    out = {"slope": None, "lo": None, "hi": None, "se": None, "df": None, "n": n, "lags": None}
    if n < max(3, int(min_n)):
        return out
    mx = sum(x for x, _y in pts) / n
    my = sum(y for _x, y in pts) / n
    sxx = sum((x - mx) ** 2 for x, _y in pts)
    if sxx <= 0:
        return out
    b = sum((x - mx) * (y - my) for x, y in pts) / sxx
    a = my - b * mx
    g = [(x - mx) * (y - a - b * x) for x, y in pts]
    lags = int(4 * (n / 100.0) ** (2.0 / 9.0)) + 1
    s = sum(v * v for v in g)
    for L in range(1, min(lags, n - 1) + 1):
        w = 1.0 - L / float(lags + 1)
        s += 2.0 * w * sum(g[t] * g[t - L] for t in range(L, n))
    # The small-sample correction (n / (n - 2)) the plain OLS error has too.
    var = max(0.0, s) * n / max(1.0, n - 2.0) / (sxx * sxx)
    se = math.sqrt(var)
    q = t90(n - 2)
    out.update(slope=round(b, 4), lo=round(b - q * se, 4), hi=round(b + q * se, 4), se=round(se, 6),
               df=n - 2, lags=lags)
    return out


def fleet_sample(ids, k, key="", strata=5) -> list:
    """At most `k` of `ids` for a fleet-wide reading (blind re-audit
    10/11/26 LEARNING-23: the cohort and the admin trend read the FIRST
    300/500 restaurants by id - the oldest customers only, survivorship and
    staleness at 9,000). The ids are cut into `strata` equal bands by id
    (sign-up order, so every era of customer is in), each band gives its
    share of `k`, and inside a band the pick is a hash of (key, id) - pass
    a new key each week to rotate the sample. All of them when k covers
    them. Deterministic. Pure."""
    ids = sorted({int(i) for i in ids or []})
    k = int(k)
    if k <= 0:
        return []
    if len(ids) <= k:
        return ids
    bands = max(1, min(int(strata), len(ids)))
    size = len(ids) / float(bands)
    out = []
    for b in range(bands):
        band = ids[int(round(b * size)):int(round((b + 1) * size))]
        take = int(round((b + 1) * k / float(bands))) - int(round(b * k / float(bands)))
        ranked = sorted(band, key=lambda i: hashlib.sha256(f"{key}|{i}".encode()).hexdigest())
        out += ranked[:take]
    return sorted(out)


def _solve(a, b):
    """Gaussian elimination with partial pivoting; None when singular."""
    n = len(b)
    m = [list(row) + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r_: abs(m[r_][c]))
        if abs(m[piv][c]) < 1e-12:
            return None
        m[c], m[piv] = m[piv], m[c]
        for r_ in range(n):
            if r_ != c:
                f = m[r_][c] / m[c][c]
                for k in range(c, n + 1):
                    m[r_][k] -= f * m[c][k]
    return [m[i][n] / m[i][i] for i in range(n)]


def ols(xs, ys, ridge=0.0, penalise=None) -> list:
    """Least squares coefficients of ys on the rows of xs (each a list of
    regressors; put a 1.0 in for an intercept), with an optional ridge
    penalty on the columns `penalise` names (all when None). None when the
    system is singular or empty."""
    if not xs or not ys or len(xs) != len(ys):
        return None
    k = len(xs[0])
    a = [[0.0] * k for _ in range(k)]
    b = [0.0] * k
    for row, y in zip(xs, ys):
        for i in range(k):
            b[i] += row[i] * y
            for j in range(k):
                a[i][j] += row[i] * row[j]
    for i in range(k):
        if penalise is None or i in penalise:
            a[i][i] += ridge
    return _solve(a, b)
