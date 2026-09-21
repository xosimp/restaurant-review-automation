"""Pure-Python statistics for the intelligence engine.

No NumPy or SciPy on the platform, and none needed: cohorts are tens to
thousands of restaurants, and a seeded permutation test is exact enough,
explainable in one sentence, and free of distributional assumptions that
restaurant metrics do not meet.
"""
import math
import random


def mean(xs):
    xs = [float(x) for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def sd(xs):
    xs = [float(x) for x in xs if x is not None]
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def median(xs):
    return percentile(xs, 50)


def percentile(xs, p):
    """Linear-interpolated percentile, p in 0..100. None on empty."""
    xs = sorted(float(x) for x in xs if x is not None)
    if not xs:
        return None
    if len(xs) == 1:
        return xs[0]
    k = (len(xs) - 1) * (p / 100.0)
    f, c = math.floor(k), math.ceil(k)
    if f == c:
        return xs[int(k)]
    return xs[f] + (xs[c] - xs[f]) * (k - f)


def cohen_d(a, b):
    """Standardised difference of means; None when either side lacks spread."""
    a = [float(x) for x in a if x is not None]
    b = [float(x) for x in b if x is not None]
    if len(a) < 2 or len(b) < 2:
        return None
    sa, sb = sd(a), sd(b)
    pooled = math.sqrt(((len(a) - 1) * sa ** 2 + (len(b) - 1) * sb ** 2) / (len(a) + len(b) - 2))
    if not pooled:
        return None
    return (mean(a) - mean(b)) / pooled


def permutation_test(a, b, shuffles=2000, seed=7):
    """Two-sided permutation test on the difference of means.

    Returns (observed_difference, p_value). Seeded, so the same inputs give
    the same p every night — a pattern must not flicker on and off because
    the shuffle changed."""
    a = [float(x) for x in a if x is not None]
    b = [float(x) for x in b if x is not None]
    if not a or not b:
        return None, None
    observed = mean(a) - mean(b)
    pool = a + b
    na = len(a)
    rng = random.Random(seed)
    hits = 0
    for _ in range(shuffles):
        rng.shuffle(pool)
        diff = mean(pool[:na]) - mean(pool[na:])
        if abs(diff) >= abs(observed) - 1e-12:
            hits += 1
    # +1 smoothing: the observed split is one of the permutations.
    return observed, (hits + 1) / (shuffles + 1)


def benjamini_hochberg(p_values):
    """False-discovery-rate adjusted q-values, same order as the input.
    None entries stay None."""
    idx = [i for i, p in enumerate(p_values) if p is not None]
    m = len(idx)
    q = [None] * len(p_values)
    if not m:
        return q
    order = sorted(idx, key=lambda i: p_values[i])
    prev = 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        val = min(prev, p_values[i] * m / rank)
        q[i] = val
        prev = val
    return q


def slope(ys):
    """Least-squares slope per step over evenly spaced points; None under 2."""
    ys = [float(y) if y is not None else None for y in ys]
    pts = [(i, y) for i, y in enumerate(ys) if y is not None]
    if len(pts) < 2:
        return None
    n = len(pts)
    mx = sum(i for i, _ in pts) / n
    my = sum(y for _, y in pts) / n
    den = sum((i - mx) ** 2 for i, _ in pts)
    if not den:
        return None
    return sum((i - mx) * (y - my) for i, y in pts) / den


def shrink(rate, n, prior=0.5, k=5):
    """A rate pulled toward `prior` by `k` pseudo-observations, so two of
    two does not read as certainty."""
    if rate is None or n is None:
        return None
    n = float(n)
    return (float(rate) * n + prior * k) / (n + k)
