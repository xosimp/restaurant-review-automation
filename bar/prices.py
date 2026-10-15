"""
bar/prices.py — the price watch (rebuilt 10/15/26: V2, V3, V15, V23, V24,
V34, F3, F8): what each product's usual price is, and when it really changed.

The shipped version compared each product's latest price with the last
different one. At Simple EJ's that read deal endings, case-quantity tiers and
credit memos as price changes: 9 of its 17 increases were false, and its
first insight told the owner to call Southern Glazer's about a St Germain
price that had not moved since May. Now, per product (one canonical product
across its codes, bar.products) and purchase unit:

1. Credits first (V15). A negative line paired with an opposite line of the
   same product — on one invoice, or on the invoice its Ref Inv Number
   names — is a credit event
   {original_price, rebilled_price, units, credit_usd}; both lines leave the
   price series and the volume. A negative line with no partner is a return
   (it comes off the volume; its price is never a price).
2. Tiers (V3). Each purchase is bucketed by its quantity — a mix-and-match
   family's total on the invoice when its lines share one price (15 cases of
   High Noon at $42) — and a distributor's ladder is learned per product
   from orders of different sizes priced differently (Superior's Ultra:
   1 keg $125, 2 kegs $115, 3+ $110). Prices are compared within a tier
   only; a product's same-day lines in a tier are one weighted price.
3. Regimes (V2). Within a tier the reference is the usual price: a new
   price is a change only when it persists (two purchases, or a distributor's
   new monthly posting); a price that drops and returns is a deal ("$198 is
   its usual price; $179.85 on 8/4/26 was a deal"); a single purchase off the
   usual price is a deal or unconfirmed until the next order says which.
4. Money (V23). A change's forward monthly figure is the change × the
   average monthly units bought at that tier; already paid is the change ×
   the units bought at the new price since it started. A deal, a tier or an
   unconfirmed price is never in the increase or decrease totals.
5. Floors (V24). A change is listed at 2% and $0.50 a unit or more; it is a
   headline at $10 a month or more. Smaller ones are kept under `minor`.
6. Expected prices (F8). The owner's deal sheet, else the learned ladder,
   gives the price an order's quantity earns; a line billed at a smaller
   tier's price is flagged and kept out of the price series.
"""
import functools
import re
from collections import Counter, defaultdict
from datetime import date, timedelta

from bar.common import LABELS, PRICE_LOOKBACK_DAYS, _UOM_WORD, _mdy, display_name
from bar.products import canonical_keys, parse_pack, unit_costs
from bar.spend import rebates_and_free_goods

# A change is listed at both of these (V24)...
LIST_PCT = 2.0
LIST_USD = 0.50
# ...and is a headline (the insight, an alert) at this much a month forward.
HEADLINE_MONTHLY_USD = 10.0
# Two orders of different sizes count toward a ladder within these many days.
TIER_EVIDENCE_DAYS = 60
# A smaller order billed at a larger one's price this close to it disproves
# a ladder outright (Euclid's promos; Red Bull's flavours at one price).
NEAR_DAYS = 21
VOLUME_DAYS = 90
_PLURAL = {"case": "cases", "keg": "kegs", "bottle": "bottles", "each": "each", "unit": "units",
           "deposit": "deposits"}


def _usd(p) -> str:
    s = f"${abs(float(p)):,.2f}"
    return s[:-3] if s.endswith(".00") else s


def _units(n, unit) -> str:
    n = float(n)
    return f"{n:g} {unit if n == 1 else _PLURAL.get(unit, unit + 's')}"


def _same(a, b) -> bool:
    return abs(float(a) - float(b)) < 0.01 + 1e-9


def _material(old, new) -> bool:
    d = abs(float(new) - float(old))
    return bool(old) and d >= LIST_USD and d / abs(float(old)) * 100 >= LIST_PCT


def _family(description) -> str:
    """A brand family for mix-and-match: the description's first word, or
    its first two when the first is short (SURFSIDE; HIGH NOON; CH ESCLANS).
    Kept per description: a 3-year history asks ~90k times for ~400 names."""
    return _family_of(str(description or ""))


@functools.lru_cache(maxsize=8192)
def _family_of(description) -> str:
    w = re.sub(r"[^A-Z0-9 ]", " ", description.upper()).split()
    return " ".join(w[:1] if w and len(w[0]) >= 4 else w[:2])


def _d(s) -> date:
    return _day(str(s)[:10])


@functools.lru_cache(maxsize=8192)
def _day(iso) -> date:
    return date.fromisoformat(iso)


# ── 1. credits ─────────────────────────────────────────────────────────────

def credit_events(lines, canon) -> tuple:
    """(events, used): credit pairs and lone returns from negative product
    lines, and the indices of `lines` they consume (never priced, never
    volume). A rebate or a deposit is not a credit here (bar.spend's
    ledger holds those)."""
    def eligible(l):
        return (l.get("kind") == "item" and l.get("category") not in ("fee", "deposit")
                and not l.get("rebate") and float(l.get("qty") or 0) != 0)

    # One pass over the lines: the positive ones indexed by product, the
    # negative ones kept in line order (V17 — the second pass was over all).
    pos, negs = defaultdict(list), []
    for i, l in enumerate(lines):
        if eligible(l):
            if float(l["qty"]) > 0:
                pos[(l["distributor"], canon.get(l["product_key"], l["product_key"]),
                     str(l.get("uom") or ""))].append(i)
            else:
                negs.append(i)
    used, events = set(), []
    for i in negs:
        l = lines[i]
        key = (l["distributor"], canon.get(l["product_key"], l["product_key"]), str(l.get("uom") or ""))
        q = -float(l["qty"])
        ref = str(l.get("ref_invoice_number") or "").strip()
        best, rank = None, None
        for j in pos.get(key, []):
            m = lines[j]
            if j in used or not _same(float(m["qty"]), q):
                continue
            mref = str(m.get("ref_invoice_number") or "").strip()
            if m["invoice_number"] == l["invoice_number"]:
                r = 0
            elif ref and ref == m["invoice_number"] or mref and mref == l["invoice_number"]:
                r = 1
            else:
                continue
            if rank is None or r < rank:
                best, rank = j, r
        orig = float(l.get("unit_price") or 0)
        if best is not None:
            m = lines[best]
            used.update((i, best))
            reb = float(m.get("unit_price") or 0)
            events.append({"kind": "credit" if not _same(orig, reb) else "reversal",
                           "distributor": l["distributor"], "product_key": l["product_key"],
                           "name": display_name(l["description"]), "uom": l.get("uom"),
                           "unit": _UOM_WORD.get(str(l.get("uom") or "").upper(), "unit"),
                           "date": str(l["invoice_date"])[:10], "date_mdy": _mdy(l["invoice_date"]),
                           "invoice_number": l["invoice_number"], "rebill_invoice_number": m["invoice_number"],
                           "ref_invoice_number": ref or None, "units": round(q, 2),
                           "original_price": round(orig, 2), "rebilled_price": round(reb, 2),
                           "credit_usd": round((orig - reb) * q, 2)})
        else:
            used.add(i)
            events.append({"kind": "return", "distributor": l["distributor"], "product_key": l["product_key"],
                           "name": display_name(l["description"]), "uom": l.get("uom"),
                           "unit": _UOM_WORD.get(str(l.get("uom") or "").upper(), "unit"),
                           "date": str(l["invoice_date"])[:10], "date_mdy": _mdy(l["invoice_date"]),
                           "invoice_number": l["invoice_number"], "ref_invoice_number": ref or None,
                           "units": round(q, 2), "original_price": round(orig, 2), "rebilled_price": None,
                           "credit_usd": round(-float(l.get("cost") or 0) or orig * q, 2)})
    events.sort(key=lambda e: e["date"], reverse=True)
    return events, used


# ── 2. tiers ───────────────────────────────────────────────────────────────

def _tier_evidence(obs) -> tuple:
    """From orders in date order, consecutive orders of different sizes:
    'for' a quantity break when the larger is cheaper by the listing floor,
    'against' one otherwise."""
    fors, against = [], []
    o = sorted(obs, key=lambda x: (x["date"], x["tq"]))
    for a, b in zip(o, o[1:]):
        if _same(a["tq"], b["tq"]) or (_d(b["date"]) - _d(a["date"])).days > TIER_EVIDENCE_DAYS:
            continue
        lo, hi = (a, b) if a["tq"] < b["tq"] else (b, a)
        if _material(hi["price"], lo["price"]) and lo["price"] > hi["price"]:
            fors.append({"lo": lo["tq"], "hi": hi["tq"], "same_day": a["date"] == b["date"],
                         "up": a["tq"] < b["tq"], "hi_date": hi["date"]})
        else:
            against.append((lo["tq"], hi["tq"]))
    return fors, against


def _violations(k, obs) -> tuple:
    """(for, against) over every pair of a smaller (< k) and a larger (>= k)
    order within TIER_EVIDENCE_DAYS: a ladder never bills the smaller order
    at the larger one's price or less (Euclid's weekly promos fail this; a
    real ladder never does)."""
    small = [o for o in obs if o["tq"] < k]
    large = [o for o in obs if o["tq"] >= k]
    f = a = 0
    near_small, near_large = set(), set()
    for x in small:
        for y in large:
            gap = abs((_d(x["date"]) - _d(y["date"])).days)
            if gap > TIER_EVIDENCE_DAYS:
                continue
            if _material(x["price"], y["price"]) and x["price"] > y["price"]:
                f += 1
            elif x["price"] <= y["price"] + max(0.01, y["price"] * 0.01):
                a += 1
                if gap <= NEAR_DAYS:
                    near_small.add(id(x))
                    near_large.add(id(y))
    # One larger order billed at the smaller price is a mis-bill to flag (F8);
    # two, or one smaller order at two larger orders' price, is no ladder.
    return f, a, max(len(near_small), len(near_large)) >= 2


def _learn_breaks(obs, house_has_ladders, partners=()) -> tuple:
    """(breaks, proven): the quantities a new price tier starts at. A break
    needs more consecutive orders saying so than against it; under a third of
    every nearby smaller/larger pair (this product's and its mix-and-match
    partners') contradicting it, and no two orders contradicting it within
    three weeks of each other; and either proof in this
    product's own orders (two sizes the same day, or a larger order cheaper
    both before and after a smaller one) or a distributor already proven to
    price by quantity on another product."""
    fors, against = _tier_evidence(obs)
    qs = sorted({o["tq"] for o in obs})
    pool = list(obs) + list(partners)
    breaks, proven, left = [], False, list(fors)
    while True:
        best = None
        for k in qs[1:]:
            if k in breaks:
                continue
            f = [x for x in left if x["lo"] < k <= x["hi"]]
            a = [x for x in against if x[0] < k <= x[1]]
            score = len(f) - len(a)
            if not f or score <= 0:
                continue
            vf, va, near = _violations(k, pool)
            if near or va * 3 > vf + va:
                continue
            own = any(x["same_day"] for x in f) or (any(x["up"] for x in f) and any(not x["up"] for x in f)
                                                     and len({x["hi_date"] for x in f}) >= 2)
            if not (own or house_has_ladders):
                continue
            if best is None or score > best[1] or (score == best[1] and k < best[0]):
                best = (k, score, own)
        if not best:
            break
        breaks.append(best[0])
        proven = proven or best[2]
        left = [x for x in left if not (x["lo"] < best[0] <= x["hi"])]
    return sorted(breaks), proven


def _tier_of(q, breaks) -> int:
    return sum(1 for b in breaks if q >= b)


def _tier_label(i, breaks, unit, lo_q) -> str:
    if not breaks:
        return None
    lo = lo_q if i == 0 else breaks[i - 1]
    hi = breaks[i] - 1 if i < len(breaks) else None
    plural = _PLURAL.get(unit, unit + "s")
    if hi is None:
        return f"{lo:g}+ {plural}"
    if _same(hi, lo):
        return _units(lo, unit)
    return f"{lo:g}–{hi:g} {plural}"


# ── 3. regimes ─────────────────────────────────────────────────────────────

def _monthly(pts) -> bool:
    """A distributor posting monthly: every month with two or more purchases
    has one price, at least two such months, and those months at two or
    more prices (Lite 1/2 BBL at Euclid: $122, $124, $122, $124, $120)."""
    by, n = defaultdict(set), Counter()
    for p in pts:
        m = p["date"][:7]
        by[m].add(round(p["price"], 2))
        n[m] += 1
    multi = [m for m in by if n[m] >= 2]
    if len(multi) < 2 or any(len(by[m]) > 1 for m in multi):
        return False
    # The price held within months at two or more different levels.
    return len({next(iter(by[m])) for m in multi}) >= 2


def _run(p):
    return {"price": p["price"], "start": p["date"], "end": p["date"], "n": 1, "dates": [p["date"]]}


def _regimes(pts) -> dict:
    """One tier's purchases (one weighted price a date, oldest first) read
    as regimes: {runs (the usual prices in order), deals [{price, from, to,
    ref}], highs, monthly, pending (the last purchase alone off the usual
    price) }."""
    monthly = _monthly(pts)
    runs = []
    for p in pts:
        if runs and _same(runs[-1]["price"], p["price"]):
            r = runs[-1]
            r["end"], r["n"] = p["date"], r["n"] + 1
            r["dates"].append(p["date"])
        else:
            runs.append(_run(p))
    deals, highs = [], []
    if monthly:
        # Each new posting is a regime the day it is first billed.
        return {"runs": runs, "deals": deals, "highs": highs, "monthly": True}
    seen_levels = set()

    def merge(a, c):
        return {"price": a["price"], "start": a["start"], "end": c["end"], "n": a["n"] + c["n"],
                "dates": a["dates"] + c["dates"]}

    changed = True
    while changed:
        changed = False
        # A price between two runs of one price: a deal when lower, a one-off
        # when higher — except a single lower purchase at the end after an
        # established higher run, which the pending rule reads.
        for j in range(1, len(runs) - 1):
            a, b, c = runs[j - 1], runs[j], runs[j + 1]
            if not _same(a["price"], c["price"]) or _same(a["price"], b["price"]):
                continue
            if b["price"] > a["price"] and b["n"] >= 2 and j + 1 == len(runs) - 1 and c["n"] == 1:
                continue
            (deals if b["price"] < a["price"] else highs).append(
                {"price": b["price"], "from": b["start"], "to": b["end"], "ref": a["price"], "n": b["n"]})
            seen_levels.add(round(b["price"], 2))
            runs = runs[:j - 1] + [merge(a, c)] + runs[j + 2:]
            changed = True
            break
        if changed:
            continue
        # A single purchase between two different prices never persisted.
        for j in range(1, len(runs) - 1):
            b = runs[j]
            if b["n"] != 1:
                continue
            a, c = runs[j - 1], runs[j + 1]
            (deals if b["price"] < a["price"] else highs).append(
                {"price": b["price"], "from": b["start"], "to": b["end"], "ref": a["price"], "n": 1})
            seen_levels.add(round(b["price"], 2))
            runs = runs[:j] + runs[j + 1:]
            if _same(a["price"], c["price"]):
                runs = runs[:j - 1] + [merge(a, c)] + runs[j + 1:]
            changed = True
            break
    return {"runs": runs, "deals": deals, "highs": highs, "monthly": False, "levels": seen_levels}


def _tier_state(pts, cutoff, posting_house=False) -> dict:
    """What one tier says now: its reference and, at most one each, a
    persistent change, an unconfirmed price, a deal now, a deal ended. At a
    distributor that posts prices monthly (another of its products holds a
    price a month at a time), a new price on the month's first order is
    that month's posting: a change on one purchase, at low confidence."""
    g = _regimes(pts)
    runs, deals = g["runs"], g["deals"]
    last = runs[-1]
    out = {"reference": last["price"], "reference_since": last["start"], "change": None, "pending": None,
           "deal_now": None, "deal_ended": None, "monthly": g["monthly"]}
    if len(runs) >= 2:
        prev = runs[-2]
        posted = g["monthly"] or (posting_house and last["n"] == 1 and last["start"][:7] != prev["end"][:7]
                                  and not g.get("levels", set()) & {round(last["price"], 2)})
        if last["n"] >= 2 or posted:
            if last["start"] >= cutoff:
                out["change"] = {"from": prev["price"], "to": last["price"], "since": last["start"],
                                 "was_since": prev["start"], "was_until": prev["end"],
                                 "basis": "monthly posting" if posted and last["n"] < 2 else "repeat",
                                 "confidence": "high" if (prev["n"] >= 2 and (last["n"] >= 2 or g["monthly"]))
                                 else "low"}
        else:
            earlier = {round(r["price"], 2) for r in runs[:-2]} | {round(d["price"], 2) for d in deals} \
                | g.get("levels", set())
            known = round(last["price"], 2) in earlier
            if known and last["price"] > prev["price"]:
                # Back to a price it held before, above the one between:
                # that one was a deal and this is the usual price.
                deals.append({"price": prev["price"], "from": prev["start"], "to": prev["end"],
                              "ref": last["price"], "n": prev["n"]})
                first = min(d for r in runs if _same(r["price"], last["price"]) for d in r["dates"])
                out.update(reference=last["price"], reference_since=first)
            elif known:
                out["deal_now"] = {"price": last["price"], "date": last["start"], "ref": prev["price"]}
                out.update(reference=prev["price"], reference_since=prev["start"])
            elif last["start"] >= cutoff:
                out["pending"] = {"from": prev["price"], "to": last["price"], "since": last["start"],
                                  "was_since": prev["start"], "was_until": prev["end"]}
                out.update(reference=prev["price"], reference_since=prev["start"])
            else:
                out.update(reference=prev["price"], reference_since=prev["start"])
    # A partner's purchases can say when a price moved; only this product's
    # own purchase at the new price makes it this product's event.
    def own_since(day):
        return any(p["units"] > 0 and p["date"] >= day for p in pts)
    for k in ("change", "pending"):
        if out[k] and not own_since(out[k]["since"]):
            out[k] = None
    if out["deal_now"] and not own_since(out["deal_now"]["date"]):
        out["deal_now"] = None
    if deals and not out["deal_now"]:
        d = max(deals, key=lambda x: x["to"])
        after = [p["date"] for p in pts if p["date"] > d["to"] and p["units"] > 0]
        if after and after[0] >= cutoff and (not out["change"] or out["change"]["since"] <= d["from"]) \
                and d["price"] < out["reference"] and not out["pending"]:
            out["deal_ended"] = {"price": d["price"], "from": d["from"], "to": d["to"], "ended": after[0],
                                 "ref": out["reference"]}
    return out


# ── 4. one product ─────────────────────────────────────────────────────────

def _monthly_units(units, first_day, today) -> float:
    """Units a month: the last 90 days' units over 90 days, or over the days
    since the tier was first bought when that is shorter (never under 28)."""
    span = max(28, min(VOLUME_DAYS, (today - _d(first_day)).days + 1)) if first_day else VOLUME_DAYS
    return units / span * 30.44


def _series_rows(obs, breaks, today, cutoff, unit, mates=(), posting_house=False):
    """Per tier: the purchases (one weighted price a date) and their state.
    A mix-and-match partner that is always billed at this product's price
    (Red Bull's three flavours) fills the dates this product wasn't bought:
    its prices say when the family's price moved; its units are never this
    product's."""
    lo_q = min(o["tq"] for o in obs)
    tiers = defaultdict(list)
    for o in obs:
        tiers[_tier_of(o["tq"], breaks)].append(o)
    own_days = {o["date"] for o in obs}
    fill = defaultdict(list)
    for o in mates:
        if o["date"] not in own_days:
            fill[_tier_of(o["tq"], breaks)].append(o)
    out = {}
    since90 = (today - timedelta(days=VOLUME_DAYS)).isoformat()
    for t, items in tiers.items():
        by_day = defaultdict(lambda: [0.0, 0.0, 0.0])
        for o in items:
            by_day[o["date"]][0] += o["price"] * o["q"]
            by_day[o["date"]][1] += o["q"]
            by_day[o["date"]][2] += o["q"]
        for o in fill.get(t, []):
            by_day[o["date"]][0] += o["price"] * o["q"]
            by_day[o["date"]][1] += o["q"]
        pts = [{"date": d, "price": round(v[0] / v[1], 2) if v[1] else 0.0, "units": v[2]}
               for d, v in sorted(by_day.items())]
        st = _tier_state(pts, cutoff, posting_house)
        u90 = sum(p["units"] for p in pts if p["date"] >= since90)
        st.update(tier=t, label=_tier_label(t, breaks, unit, lo_q), pts=pts, units_90d=u90,
                  monthly_units=_monthly_units(u90, min(o["date"] for o in items), today) if u90 else 0.0,
                  mixed=any(o["pooled"] for o in items))
        out[t] = st
    return out


def _explain(kind, r, unit) -> str:
    u = unit
    if kind in ("increase", "decrease"):
        tier = f" ({r['tier']['label']})" if r.get("tier") and r["tier"].get("label") else ""
        if r["persistent"]:
            was = (f"on {r['was_since_mdy']}" if r["was_since_mdy"] == r["was_until_mdy"]
                   else f"from {r['was_since_mdy']} to {r['was_until_mdy']}")
            return f"{_usd(r['to'])} a {u} since {r['since_mdy']}, after {_usd(r['from'])} {was}{tier}"
        return (f"{_usd(r['to'])} a {u} on {r['since_mdy']}, after {_usd(r['from'])}{tier} — one purchase at "
                f"the new price so far; the next order says whether it holds")
    if kind == "deal_ended":
        return f"{_usd(r['to'])} is its usual price; {_usd(r['deal']['price'])} on {r['deal']['from_mdy']}" \
               f"{'' if r['deal']['from'] == r['deal']['to'] else ' to ' + r['deal']['to_mdy']} was a deal"
    if kind == "deal":
        return f"{_usd(r['to'])} on {r['since_mdy']} is a deal price it has had before; its usual price is " \
               f"{_usd(r['from'])}"
    if kind == "tier":
        return f"{r['latest']['qty_label']} at {_usd(r['to'])}; {r['latest']['other_label']} at " \
               f"{_usd(r['from'])} — the order size, not a price change"
    return ""


def analyse(lines, today, deal_sheet=None, packs=None, pour=None, links=None) -> dict:
    """Everything the price watch reads, per product: {payload, series,
    canon, credits}. `series` maps (canonical key, uom) to its tiers, for
    the drink margin check."""
    canon = canonical_keys(lines, links)
    credits, used = credit_events(lines, canon)
    cutoff = (today - timedelta(days=PRICE_LOOKBACK_DAYS)).isoformat()
    since90 = (today - timedelta(days=VOLUME_DAYS)).isoformat()
    # Eligible purchases, with their tier quantity (a mix-and-match family's
    # total on the invoice when its lines share one price). Two passes over
    # the eligible lines, each line's keys read once (V17).
    elig = []
    fam = defaultdict(lambda: {"qty": 0.0, "keys": set()})
    for i, l in enumerate(lines):
        if i in used or l.get("kind") != "item" or l.get("category") in ("fee", "deposit") or l.get("rebate"):
            continue
        q, p = float(l.get("qty") or 0), float(l.get("unit_price") or 0)
        if q <= 0 or p < 1:
            continue
        ck = canon.get(l["product_key"], l["product_key"])
        u = str(l.get("uom") or "")
        f = fam[(l["distributor"], l["invoice_number"], u, _family(l["description"]), round(p, 2))]
        f["qty"] += q
        f["keys"].add(ck)
        elig.append((i, l, q, p, ck, u, f))
    series = defaultdict(list)
    meta = {}
    partners = defaultdict(set)
    # Each product's prices per invoice, and its invoices in order: the
    # partners below compare on the invoices they share.
    inv_price = {}
    invs_of = defaultdict(list)
    for i, l, q, p, ck, u, f in elig:
        pooled = len(f["keys"]) > 1
        sk = (ck, u)
        if pooled:
            partners[sk].update((k, sk[1]) for k in f["keys"] if k != ck)
        inv = l["invoice_number"]
        series[sk].append({"i": i, "date": str(l["invoice_date"])[:10], "q": q,
                           "tq": f["qty"] if pooled else q, "price": p,
                           "pooled": pooled, "invoice": inv, "key": l["product_key"]})
        meta[sk] = l
        ip = inv_price.get((sk, inv))
        if ip is None:
            ip = inv_price[(sk, inv)] = set()
            invs_of[sk].append(inv)
        ip.add(round(p, 2))
    returns = defaultdict(float)
    for e in credits:
        if e["kind"] == "return" and e["date"] >= since90:
            returns[(canon.get(e["product_key"], e["product_key"]), str(e.get("uom") or ""))] += e["units"]
    # A distributor proven to price by quantity on one product is believed on
    # another with fewer orders to show it.
    houses, posting = set(), set()
    first_read = {}
    for sk, obs in series.items():
        if len(obs) >= 2:
            first_read[sk] = _learn_breaks(obs, False, [o for p in partners[sk] for o in series.get(p, [])])
            if first_read[sk][1]:
                houses.add(meta[sk]["distributor"])
        by_day = {}
        for o in obs:
            by_day.setdefault(o["date"], o["price"])
        if _monthly([{"date": d, "price": p} for d, p in sorted(by_day.items())]):
            posting.add(meta[sk]["distributor"])
    # Partners that share this product's price on every invoice they share:
    # their purchases say when the family's price moved.
    same_price_mates = {}
    for sk in series:
        ok = []
        for p in partners[sk]:
            # This product's own invoices, not every (product, invoice) pair
            # on file per partner (V17: that was quadratic in products).
            shared = [inv for inv in invs_of[sk] if (p, inv) in inv_price]
            if shared and all(inv_price[(sk, inv)] == inv_price[(p, inv)] for inv in shared):
                ok.append(p)
        same_price_mates[sk] = ok
    sheet = defaultdict(list)
    for s in deal_sheet or []:
        sheet[canon.get(s["product_key"], s["product_key"])].append(s)

    rows, ladders, flags, refs = [], [], [], {}
    for sk, obs in series.items():
        l = meta[sk]
        unit = _UOM_WORD.get(str(l.get("uom") or "").upper(), "unit")
        mates = [o for p in partners[sk] for o in series.get(p, [])]
        fills = [o for p in same_price_mates[sk] for o in series.get(p, [])]
        # A mix-and-match family shares one ladder: its partners' orders
        # count toward this product's tiers.
        own_dates = {x["date"] for x in obs}
        extra = [o for o in fills if o["date"] not in own_dates]
        learn = obs + extra
        if len(learn) < 2:
            breaks, proven = [], False
        elif not extra and l["distributor"] not in houses:
            # The same reading the house pass made (the same orders and
            # partners, no proven house): kept, not repeated.
            breaks, proven = first_read[sk]
        else:
            breaks, proven = _learn_breaks(learn, l["distributor"] in houses, mates)
        posts = l["distributor"] in posting
        tiers = _series_rows(obs, breaks, today, cutoff, unit, fills, posts)
        # F8: a line billed at a smaller tier's price than its quantity
        # earns, or above the owner's deal sheet, is flagged and re-read
        # without it.
        flagged = set()
        for o in obs:
            if o["date"] < cutoff:
                continue
            t = _tier_of(o["tq"], breaks)
            own = tiers[t]
            exp, src = None, None
            rules = [s for s in sheet.get(sk[0], []) if s["min_qty"] <= o["tq"]
                     and (not s.get("valid_from") or s["valid_from"] <= o["date"])
                     and (not s.get("valid_to") or o["date"] <= s["valid_to"])]
            if rules:
                exp, src = max(rules, key=lambda s: s["min_qty"])["price"], "owner"
            elif t > 0 and o["date"] >= own["reference_since"]:
                smaller = [tiers[s]["reference"] for s in range(t) if s in tiers]
                if any(abs(o["price"] - sp) <= max(0.01, sp * 0.01) for sp in smaller):
                    exp, src = own["reference"], "ladder"
            if exp is not None and o["price"] - exp >= max(LIST_USD, exp * 0.01):
                flagged.add(o["i"])
                flags.append({"product_key": o["key"], "name": display_name(lines[o["i"]]["description"]),
                              "distributor": l["distributor"], "invoice_number": o["invoice"],
                              "date": o["date"], "date_mdy": _mdy(o["date"]), "qty": o["tq"], "unit": unit,
                              "tier_label": tiers[t]["label"], "billed": round(o["price"], 2),
                              "expected": round(float(exp), 2), "expected_source": src,
                              "over_usd": round((o["price"] - float(exp)) * o["q"], 2)})
        if flagged:
            obs = [o for o in obs if o["i"] not in flagged]
            if not obs:
                continue
            tiers = _series_rows(obs, breaks, today, cutoff, unit, fills, posts)
        refs[sk] = {"tiers": tiers, "breaks": breaks, "line": l, "unit": unit}
        # The ladder, when there is one (V3).
        if breaks:
            lo_q = min(o["tq"] for o in obs)
            steps = [{"tier": t, "label": tiers[t]["label"], "min_qty": lo_q if t == 0 else breaks[t - 1],
                      "price": round(tiers[t]["reference"], 2), "purchases": len(tiers[t]["pts"]),
                      "mixed": tiers[t]["mixed"]} for t in sorted(tiers)]
            if len(steps) >= 2 and all(a["price"] > b["price"] for a, b in zip(steps, steps[1:])):
                small, best = steps[0], steps[-1]
                small_orders = [o for o in obs if o["date"] >= since90 and _tier_of(o["tq"], breaks) == 0]
                ladders.append({"product_key": l["product_key"], "name": display_name(l["description"]),
                                "distributor": l["distributor"], "unit": unit, "tiers": steps,
                                "proven": proven,
                                # Proven in this product's own orders with two or
                                # more orders at each end: worth an insight.
                                "confidence": ("high" if proven and small["purchases"] >= 2
                                               and best["purchases"] >= 2 else "low"),
                                "save_per_unit": round(small["price"] - best["price"], 2),
                                "smallest_label": small["label"], "best_label": best["label"],
                                "small_orders_90d": len({o["date"] for o in small_orders}),
                                "small_units_90d": round(sum(o["q"] for o in small_orders), 2),
                                "save_90d_at_best": round(sum(o["q"] for o in small_orders)
                                                          * (small["price"] - best["price"]), 2)})
        row = _product_row(sk, obs, tiers, breaks, l, unit, today, cutoff, since90, returns, lines,
                           (packs or {}), pour)
        if row:
            rows.append(row)
    ladders.sort(key=lambda r: -(r["save_90d_at_best"] or 0))
    flags.sort(key=lambda f: f["date"], reverse=True)
    payload = _payload(rows, ladders, flags, credits, series, lines, canon, today, packs, pour)
    return {"payload": payload, "series": refs, "canon": canon, "credits": credits}


def _product_row(sk, obs, tiers, breaks, l, unit, today, cutoff, since90, returns, lines, packs, pour):
    name = display_name(l["description"])
    keys = sorted({o["key"] for o in obs})
    latest_o = max(obs, key=lambda o: (o["date"], o["i"]))
    # The main tier: where most units were bought in 90 days (smaller on a tie).
    main = max(tiers.values(), key=lambda s: (s["units_90d"], -s["tier"]))
    changes = [s for s in tiers.values() if s["change"]]
    kind, st, persistent = None, None, False
    if changes:
        st = max(changes, key=lambda s: (s["units_90d"], -s["tier"]))
        ev, kind, persistent = st["change"], ("increase" if st["change"]["to"] > st["change"]["from"] else "decrease"), True
    else:
        lt = tiers[_tier_of(latest_o["tq"], breaks)]
        for s in [lt] + [x for x in tiers.values() if x is not lt]:
            if s["pending"]:
                st, ev = s, s["pending"]
                kind = "increase" if ev["to"] > ev["from"] else "decrease"
                break
            if s["deal_now"]:
                st, ev, kind = s, s["deal_now"], "deal"
                break
            if s["deal_ended"]:
                st, ev, kind = s, s["deal_ended"], "deal_ended"
                break
    # The latest purchase against the one before it, whatever the tier.
    by_date = sorted(obs, key=lambda o: (o["date"], o["i"]))
    prev_o = next((o for o in reversed(by_date) if o["date"] < latest_o["date"]), None)
    lt_tier = _tier_of(latest_o["tq"], breaks)
    latest = {"date": latest_o["date"], "date_mdy": _mdy(latest_o["date"]), "price": round(latest_o["price"], 2),
              "qty": latest_o["tq"], "tier_label": tiers[lt_tier]["label"], "kind": "regular"}
    if prev_o and _material(prev_o["price"], latest_o["price"]):
        pt = _tier_of(prev_o["tq"], breaks)
        if pt != lt_tier:
            latest.update(kind="tier", other_price=round(prev_o["price"], 2), other_qty=prev_o["tq"],
                          qty_label=_units(latest_o["tq"], unit), other_label=_units(prev_o["tq"], unit))
        elif kind == "deal" or (st and st.get("deal_now")):
            latest["kind"] = "deal"
        elif kind == "deal_ended":
            latest["kind"] = "deal_ended"
        else:
            latest["kind"] = "new_price"
    if kind is None and latest["kind"] == "tier" and latest_o["date"] >= cutoff:
        kind, st = "tier", tiers[lt_tier]
        ev = {"from": prev_o["price"], "to": latest_o["price"], "since": latest_o["date"]}
    if kind is None:
        return None
    u90_all = sum(o["q"] for o in obs if o["date"] >= since90) - returns.get(sk, 0.0)
    row = {"product_key": l["product_key"], "canonical_key": sk[0], "aliases": [k for k in keys if k != l["product_key"]],
           "product": l["description"], "name": name, "distributor": l["distributor"],
           "uom": l.get("uom"), "unit": unit, "category": l["category"],
           "category_label": LABELS.get(l["category"], ""), "kind": kind, "persistent": persistent,
           "tier": ({"label": st["label"], "index": st["tier"], "mixed": st["mixed"]} if st["label"] else None),
           "latest": latest, "monthly_posting": bool(st["monthly"])}
    if kind in ("increase", "decrease"):
        frm, to = ev["from"], ev["to"]
        row.update(since=ev["since"], was_since=ev["was_since"], was_until=ev["was_until"],
                   was_since_mdy=_mdy(ev["was_since"]), was_until_mdy=_mdy(ev["was_until"]),
                   confidence=ev.get("confidence", "low"), basis=ev.get("basis"))
    elif kind == "deal_ended":
        frm, to = ev["price"], ev["ref"]
        row.update(since=ev["ended"], deal={"price": round(ev["price"], 2), "from": ev["from"], "to": ev["to"],
                                            "from_mdy": _mdy(ev["from"]), "to_mdy": _mdy(ev["to"])})
    elif kind == "deal":
        frm, to = ev["ref"], ev["price"]
        row.update(since=ev["date"], deal={"price": round(ev["price"], 2), "from": ev["date"], "to": ev["date"],
                                           "from_mdy": _mdy(ev["date"]), "to_mdy": _mdy(ev["date"])})
    else:  # tier
        frm, to = ev["from"], ev["to"]
        row.update(since=ev["since"])
    change = round(to - frm, 2)
    row.update({"from": round(frm, 2), "to": round(to, 2), "change": change,
                "pct": round(change / frm * 100, 1) if frm else None, "since_mdy": _mdy(row["since"])})
    if kind in ("increase", "decrease"):
        row.setdefault("was_since_mdy", "")
    # Money (V23): forward a month at the tier's usual volume; paid since.
    fwd, paid = 0.0, 0.0
    if kind in ("increase", "decrease"):
        tiers_moved = [s for s in tiers.values() if s["change"]] if persistent else [st]
        for s in tiers_moved:
            e = s["change"] if persistent else s["pending"]
            d = e["to"] - e["from"]
            fwd += d * s["monthly_units"]
            paid += d * sum(p["units"] for p in s["pts"] if p["date"] >= e["since"])
    ref = st["reference"]
    row.update(regime={"reference": round(ref, 2), "since": st["reference_since"],
                       "since_mdy": _mdy(st["reference_since"])},
               forward_monthly_usd=round(fwd, 2) if kind in ("increase", "decrease") else None,
               already_paid_usd=round(paid, 2) if kind in ("increase", "decrease") else None,
               bought_90d=round(max(0.0, u90_all), 2), tier_bought_90d=round(st["units_90d"], 2),
               main_tier_label=main["label"])
    row["impact_90d"] = round((row["forward_monthly_usd"] or 0) * 3, 2)
    row["not_bought_lately"] = row["bought_90d"] <= 0
    row["listed"] = _material(frm, to)
    # A headline (an insight, an alert) needs the old price seen twice too.
    row["headline"] = bool(persistent and row["listed"] and row.get("confidence") == "high"
                           and abs(row["forward_monthly_usd"] or 0) >= HEADLINE_MONTHLY_USD)
    row["per_unit"] = unit_costs(ref, l.get("uom"), l["description"], l["category"],
                                 packs.get(l["product_key"]), pour)
    # A code change under one product (a gift box, a new vintage): the
    # history continues, marked.
    codes = []
    for o in by_date:
        if not codes or codes[-1]["key"] != o["key"]:
            codes.append({"key": o["key"], "date": o["date"]})
    row["code_changes"] = [{"from_key": a["key"], "to_key": b["key"], "date_mdy": _mdy(b["date"])}
                           for a, b in zip(codes, codes[1:])]
    recent = [o for o in by_date if o["date"] >= cutoff]
    row["history"] = [{"date": o["date"], "date_mdy": _mdy(o["date"]), "price": round(o["price"], 2),
                       "qty": o["q"], "tier_label": tiers[_tier_of(o["tq"], breaks)]["label"]} for o in recent]
    row["purchases"] = len(recent)
    row["last_bought_mdy"] = _mdy(latest_o["date"])
    row["explain"] = _explain(kind, row, unit)
    return row


# ── 5. split case (F3) ─────────────────────────────────────────────────────

def _split_case(lines, canon, today, packs) -> list:
    """A product bought by the case and by the bottle: what the bottle
    costs out of its case, and the premium paid for buying it loose
    (Woodford +35% at EJ's)."""
    cutoff = (today - timedelta(days=PRICE_LOOKBACK_DAYS)).isoformat()
    by = defaultdict(lambda: {"CA": [], "BO": []})
    for l in lines:
        if l.get("kind") != "item" or float(l.get("qty") or 0) <= 0 or float(l.get("unit_price") or 0) < 1:
            continue
        if l.get("category") not in ("liquor", "wine"):
            continue
        u = str(l.get("uom") or "").upper()
        u = "CA" if u in ("CA", "CS") else ("BO" if u in ("BO", "BT", "BL") else None)
        if u:
            by[canon.get(l["product_key"], l["product_key"])][u].append(l)
    out = []
    for ck, s in by.items():
        if not s["CA"] or not s["BO"]:
            continue
        ca, bo = s["CA"][-1], s["BO"][-1]
        pk = parse_pack(ca["description"], "CA", ca["category"])
        ov = packs.get(ca["product_key"]) or {}
        units = int(ov.get("units_per_case") or pk["units_per_case"] or 12)
        source = "owner" if ov.get("units_per_case") else pk["units_source"]
        # The usual case and bottle prices: the most common recent ones.
        def usual(ls):
            prices = [round(float(x["unit_price"]), 2) for x in ls[-4:]]
            return max(set(prices), key=lambda p: (prices.count(p), -prices[::-1].index(p)))
        case_p, bottle_p = usual(s["CA"]), usual(s["BO"])
        if source == "assumed":
            # An assumed pack is checked against the prices: a 6-bottle case
            # read as 12 would show a bottle at twice its case price.
            fits = [n for n in (units, 6, 12, 24) if -5 <= (bottle_p - case_p / n) / (case_p / n) * 100 <= 60]
            if not fits:
                continue
            units = fits[0]
        per = case_p / units
        if per <= 0:
            continue
        loose = [x for x in s["BO"] if str(x["invoice_date"])[:10] >= cutoff]
        bottles = sum(float(x["qty"]) for x in loose)
        out.append({"product_key": bo["product_key"], "name": display_name(bo["description"]),
                    "distributor": bo["distributor"], "case_price": case_p, "units_per_case": units,
                    "units_source": source, "per_bottle_in_case": round(per, 2), "bottle_price": bottle_p,
                    "premium_pct": round((bottle_p - per) / per * 100, 1),
                    "premium_per_bottle": round(bottle_p - per, 2), "bottles_bought": round(bottles, 2),
                    "paid_extra_usd": round((bottle_p - per) * bottles, 2),
                    "last_bottle_mdy": _mdy(bo["invoice_date"]),
                    "assumes": (f"assumes {units} a case" if source == "assumed" else None)})
    out.sort(key=lambda r: (-(r["paid_extra_usd"] or 0), -(r["premium_pct"] or 0)))
    return out


# ── 6. the payload ─────────────────────────────────────────────────────────

def _payload(rows, ladders, flags, credits, series, lines, canon, today, packs, pour) -> dict:
    ups = [r for r in rows if r["kind"] == "increase" and r["persistent"] and r["listed"]]
    downs = [r for r in rows if r["kind"] == "decrease" and r["persistent"] and r["listed"]]
    unconfirmed = [r for r in rows if r["kind"] in ("increase", "decrease") and not r["persistent"] and r["listed"]]
    deals = [r for r in rows if r["kind"] in ("deal", "deal_ended") and r["listed"]]
    tiers = [r for r in rows if r["kind"] == "tier" and r["listed"]]
    minor = [r for r in rows if not r["listed"]]
    ups.sort(key=lambda r: (-(r["forward_monthly_usd"] or 0), -(r["pct"] or 0)))
    downs.sort(key=lambda r: ((r["forward_monthly_usd"] or 0), (r["pct"] or 0)))
    for lst in (unconfirmed, deals, tiers, minor):
        lst.sort(key=lambda r: r["since"], reverse=True)
    counted_up = [r for r in ups if not r["not_bought_lately"]]
    counted_down = [r for r in downs if not r["not_bought_lately"]]
    up_fwd = round(sum(r["forward_monthly_usd"] for r in counted_up), 2)
    down_fwd = round(sum(r["forward_monthly_usd"] for r in counted_down), 2)
    by_pct_up = max(counted_up, key=lambda r: r["pct"] or 0) if counted_up else None
    by_pct_down = min(counted_down, key=lambda r: r["pct"] or 0) if counted_down else None

    def brief(r):
        return ({"product_key": r["product_key"], "name": r["name"], "pct": r["pct"], "change": r["change"],
                 "unit": r["unit"], "forward_monthly_usd": r["forward_monthly_usd"]} if r else None)

    # The credit ledger (F7): credit pairs and returns, a product's own
    # rebates, free goods beside a deal — newest first.
    credit_rows = sorted([c for c in credits if c["kind"] != "reversal"] + rebates_and_free_goods(lines, canon),
                         key=lambda c: c["date"], reverse=True)
    summary = {
        # Persistent same-tier changes over the floor, bought in 90 days.
        "up_n": len(counted_up), "down_n": len(counted_down),
        "up_forward_monthly_usd": up_fwd, "down_forward_monthly_usd": down_fwd,
        "net_forward_monthly_usd": round(up_fwd + down_fwd, 2),
        "up_already_paid_usd": round(sum(r["already_paid_usd"] for r in counted_up), 2),
        "down_already_paid_usd": round(sum(r["already_paid_usd"] for r in counted_down), 2),
        # The same forward figures over 90 days (the keys the page read before).
        "up_impact_90d": round(up_fwd * 3, 2), "down_impact_90d": round(down_fwd * 3, 2),
        "net_impact_90d": round((up_fwd + down_fwd) * 3, 2),
        "headline_n": sum(1 for r in counted_up + counted_down if r["headline"]),
        "not_bought_lately_n": sum(1 for r in ups + downs if r["not_bought_lately"]),
        "unconfirmed_n": len(unconfirmed), "deals_n": len(deals), "tiers_n": len(tiers), "minor_n": len(minor),
        "credits_usd": round(sum(c["credit_usd"] or 0 for c in credit_rows), 2),
        "tier_flags_n": len(flags), "tier_flags_usd": round(sum(f["over_usd"] for f in flags), 2),
        "largest_up": brief(by_pct_up), "largest_down": brief(by_pct_down),
        "floor": {"pct": LIST_PCT, "usd_per_unit": LIST_USD, "headline_monthly_usd": HEADLINE_MONTHLY_USD}}
    return {"increases": ups, "decreases": downs, "unconfirmed": unconfirmed, "deals": deals, "tiers": tiers,
            "minor": minor, "events": sorted(rows, key=lambda r: r["since"], reverse=True),
            "credits": credit_rows, "ladders": ladders, "tier_flags": flags,
            "split_case": _split_case(lines, canon, today, packs or {}),
            "products_repeat": sum(1 for obs in series.values() if len(obs) >= 2),
            "lookback_days": PRICE_LOOKBACK_DAYS, "summary": summary}


def _price_watch(lines, today, deal_sheet=None, packs=None, pour=None, links=None) -> dict:
    """The price watch payload (bar.build reads analyse() once instead)."""
    return analyse(lines, today, deal_sheet=deal_sheet, packs=packs, pour=pour, links=links)["payload"]
