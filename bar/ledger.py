"""
bar/ledger.py — the invoice lines and the drink sales, indexed ONCE per
build so every window the page reads (the four weeks, the 12 trend points,
the month rows, the 13-week context, each counted period) is a slice, not a
pass over all history (bar fix round 10/15/26, V17 / SEC-2 / RPW-18).

Before: `_window`, `_gaps` and `bar.judge.houses` each scanned every line
ever, and `_gaps` ran `houses` and `stopped` again for each of ~26 windows —
O(windows x lines): 3 years at 45k lines took ~1 s in the engine alone, and
grew faster than the history.

Now: `Lines` is the list `bar.purchases._lines` returns (a plain list
subclass, so every caller that iterates it is unchanged); its `ledger`
holds, built on first use in O(lines log lines):

- every line in date order (a stable sort: `_lines` already reads them in
  date order, so sums keep the order — and the floating-point result — the
  scans had), with each line's date, by category and by distributor, for
  bisect slices;
- each distributor's distinct invoice days and its per-day pour-category
  spend (and per category), which `houses(end)` reads for any `end`
  without touching a line — memoised per `end`, so the ~26 windows share
  one house read per distinct end.

`Sales` does the same for the {category: {date: value}} sales shape: each
category's positive nights in date order and the union of open days.

A caller that passes a plain list or dict (the tests, older callers) gets a
ledger built for that call: the same answers, at the old cost. Nothing here
reads the database or changes a figure; pure.
"""
from bisect import bisect_left, bisect_right
from collections import defaultdict

from bar.common import POUR_CATEGORIES


def _day(l):
    return str(l["invoice_date"])[:10]


class Ledger:
    """The lines indexed by date, category and distributor (module note)."""

    def __init__(self, lines):
        rows = sorted(lines, key=_day)                   # stable: keeps _lines' order within a day
        self.rows = rows
        self.dates = [_day(l) for l in rows]
        self.days = sorted(set(self.dates))
        # The Fintech feed's own invoice days (a Back Office entry left out):
        # what the overlap with sales is bounded by (BAR-CALC-15).
        self.feed_days = sorted({d for d, l in zip(self.dates, rows) if l.get("kind") != "backoffice"})
        by_cat, by_dist = defaultdict(list), defaultdict(list)
        for l in rows:
            by_cat[l["category"]].append(l)
            # A Back Office entry (kind 'backoffice': a check-paid house, a
            # store run) buys, but it is no delivery rhythm to watch.
            if l.get("kind") != "backoffice":
                by_dist[l["distributor"]].append(l)
        self._cat = {c: (ls, [_day(l) for l in ls]) for c, ls in by_cat.items()}
        self._dist = {d: (ls, [_day(l) for l in ls]) for d, ls in by_dist.items()}
        # Per distributor: its days, its pour spend per day and its spend
        # per pour category per day — each day's sum taken line by line in
        # line order, exactly as bar.judge.houses summed them.
        self.dist_order = list(by_dist)                  # first appearance (date order)
        self.dist_days, self.dist_spend, self.dist_cat, self.dist_cat_first = {}, {}, {}, {}
        for dist, (ls, ds) in self._dist.items():
            self.dist_days[dist] = sorted(set(ds))
            spend = {}
            per = {}
            first = {}
            for i, l in enumerate(ls):
                c = l["category"]
                if c not in POUR_CATEGORIES:
                    continue
                d = ds[i]
                spend[d] = spend.get(d, 0.0) + l["net"]
                pc = per.setdefault(c, {})
                pc[d] = pc.get(d, 0.0) + l["net"]
                first.setdefault(c, d)
            self.dist_spend[dist] = (list(spend), list(spend.values()))
            self.dist_cat[dist] = {c: (list(v), list(v.values())) for c, v in per.items()}
            self.dist_cat_first[dist] = first
        self._houses = {}

    # ── slices ────────────────────────────────────────────────────────────
    @staticmethod
    def _slice(pair, lo, hi, lo_open=False):
        ls, ds = pair
        i = bisect_right(ds, lo) if lo_open else (bisect_left(ds, lo) if lo is not None else 0)
        j = bisect_right(ds, hi) if hi is not None else len(ds)
        return ls[i:j]

    def between(self, lo, hi):
        """Every line dated in [lo, hi] (either bound None: open)."""
        return self._slice((self.rows, self.dates), lo, hi)

    def in_cat(self, cat, lo, hi, lo_open=False):
        """`cat`'s lines dated in [lo, hi] — or (lo, hi] with lo_open."""
        pair = self._cat.get(cat)
        return self._slice(pair, lo, hi, lo_open) if pair else []

    def in_dist(self, dist, lo, hi):
        pair = self._dist.get(dist)
        return self._slice(pair, lo, hi) if pair else []

    def cat_before(self, cat, lo, hi):
        """`cat`'s lines dated in [lo, hi) — hi excluded."""
        pair = self._cat.get(cat)
        if not pair:
            return []
        ls, ds = pair
        return ls[bisect_left(ds, lo):bisect_left(ds, hi)]

    def days_through(self, end):
        """The distinct invoice days on or before `end`."""
        return self.days[:bisect_right(self.days, end)]

    def feed_days_through(self, end):
        """The Fintech feed's distinct invoice days on or before `end` — every
        invoice day when no line is the feed's (Back Office alone)."""
        days = self.feed_days or self.days
        return days[:bisect_right(days, end)]

    # ── houses (bar.judge.houses, read per end from the per-day sums) ─────
    def houses(self, end, history_days, to_date, minus_days):
        """bar.judge.houses(lines, end), memoised per `end` (the same dict
        is handed to every caller: read it, never change it)."""
        key = (end, history_days)
        hit = self._houses.get(key)
        if hit is not None:
            return hit
        out = {}
        for dist in self.dist_order:
            alld = self.dist_days[dist]
            ds = alld[:bisect_right(alld, end)]
            if not ds:
                continue
            steps = sorted((to_date(b) - to_date(a)).days for a, b in zip(ds, ds[1:]))
            typical = (steps[len(steps) // 2] or 1) if steps else None
            last = ds[-1]
            since = minus_days(last, history_days)
            firsts = self.dist_cat_first[dist]
            cand = [c for c in sorted(firsts, key=lambda c: firsts[c]) if firsts[c] <= end]
            per_sum, recent = {}, {}
            for c in cand:
                cds, vals = self.dist_cat[dist][c]
                j = bisect_right(cds, end)
                per_sum[c] = sum(vals[:j])
                recent[c] = sum(vals[bisect_left(cds, since):j])
            cats = sorted((c for c in cand if recent[c] > 0), key=lambda c: -per_sum[c])
            sds, svals = self.dist_spend[dist]
            amounts = sorted(v for v in svals[:bisect_right(sds, end)] if v > 0)
            out[dist] = {"dates": ds, "first": ds[0], "last": last, "typical": typical, "cats": cats,
                         "amount": amounts[len(amounts) // 2] if amounts else 0.0}
        self._houses[key] = out
        return out


class Lines(list):
    """The invoice lines (a list) carrying their Ledger, built on first use.
    Build it once the lines are final; it never sees a later change."""

    @property
    def ledger(self):
        lg = getattr(self, "_ledger", None)
        if lg is None:
            lg = Ledger(self)
            self._ledger = lg
        return lg


def as_lines(lines):
    """`lines` as a Lines (itself when it already is one)."""
    return lines if isinstance(lines, Lines) else Lines(lines or ())


def ledger_of(lines) -> Ledger:
    return as_lines(lines).ledger


# ── sales ───────────────────────────────────────────────────────────────────

class SalesIndex:
    """{category: {date: value}} indexed: each category's positive nights in
    date order (with their values, in the dict's order within a date), the
    union of open days (any pour category > 0) and every date on file."""

    def __init__(self, sales):
        self.pos = {}
        open_set, every = set(), set()
        for c, per in (sales or {}).items():
            items = sorted(((d, v) for d, v in (per or {}).items() if (v or 0) > 0), key=lambda kv: kv[0])
            self.pos[c] = ([d for d, _v in items], [v for _d, v in items])
            every.update(per or {})
            if c in POUR_CATEGORIES:
                open_set.update(d for d, _v in items)
        self.open_days = sorted(open_set)
        self.all_days = sorted(every)

    def positive(self, cat, lo, hi):
        """[(date, value)] of `cat`'s nights in [lo, hi] with sales > 0."""
        pair = self.pos.get(cat)
        if not pair:
            return []
        ds, vs = pair
        i, j = bisect_left(ds, lo), bisect_right(ds, hi)
        return list(zip(ds[i:j], vs[i:j]))

    def open_between(self, lo, hi):
        return self.open_days[bisect_left(self.open_days, lo):bisect_right(self.open_days, hi)]

    def open_before(self, hi):
        return self.open_days[:bisect_left(self.open_days, hi)]


class Sales(dict):
    """The sales dict carrying its SalesIndex, built on first use."""

    @property
    def index(self):
        ix = getattr(self, "_index", None)
        if ix is None:
            ix = SalesIndex(self)
            self._index = ix
        return ix


def as_sales(sales):
    return sales if isinstance(sales, Sales) else Sales(sales or {})


def sales_index(sales) -> SalesIndex:
    return as_sales(sales).index
