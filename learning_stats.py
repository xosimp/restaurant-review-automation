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
                                    restaurant-cluster 90% interval (RM-4)
  ols(...)                          a small ridge-penalised least squares
                                    (calibration v2, LEARNING-8)
"""
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


def fe_slope_ci(rows, z=Z90) -> dict:
    """The within-restaurant slope of y over x with a restaurant-cluster
    (CR1) 90% interval: rows = [(restaurant, x, y)]. Each restaurant's own
    mean is taken out first (restaurant fixed effects), so a restaurant that
    started with a better draft is not read as improvement. One restaurant:
    the plain OLS slope and its own interval. {slope, lo, hi, n,
    restaurants}; slope None with too little spread."""
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
        return {"slope": None, "lo": None, "hi": None, "n": n, "restaurants": g}
    b = sum(x * y for _r, x, y in dm) / sxx
    if g >= 2:
        u = {}
        for r, x, y in dm:
            u[r] = u.get(r, 0.0) + x * (y - b * x)
        se = math.sqrt(g / (g - 1.0) * sum(v * v for v in u.values())) / sxx
    else:
        resid = sum((y - b * x) ** 2 for _r, x, y in dm)
        se = math.sqrt(resid / max(1, n - 2) / sxx)
    return {"slope": round(b, 4), "lo": round(b - z * se, 4), "hi": round(b + z * se, 4), "n": n,
            "restaurants": g}


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
