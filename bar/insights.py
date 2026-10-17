"""
bar/insights.py — What deserves a look: the page's lead insights, each from
a figure the payload measured and each through the one judgement policy
(bar.judge): a reading that was not judged gives no verdict card.

Copy rules (Bar audit V1, AIX-3): while the basis is "purchases" the
figure is "bought vs sold", never pour cost, and an action about a high or
a low reading leads with timing — a delivery or a stock-up moves it; count
the shelf to know — before pours or comps. Nothing is "improved", and
nothing says "keep doing what changed". A price card names only a change
that has held (W5's rows: kind "increase" and persistent), and hedges an
increase and a decrease the same way. Every card carries `basis` and a
`confidence` of {evidence_pct, freshness_pct} computed from the window's
days, coverage and invoices and how far behind the invoices are (R19).
"""
from bar.common import MAX_INSIGHTS, _money
from bar.judge import MIN_DAYS, confidence

# A price change worth a card: this much a month at the bar's usual buying
# (barfix Decision 7).
PRICE_CARD_MONTHLY_USD = 10.0
# The concentration card needs this many held increases and a top-three
# share at least this multiple of the uniform 3/n (V25).
CONCENTRATION_MIN_N = 7
CONCENTRATION_OVER_UNIFORM = 1.5
# A distributor's quantity ladder is a card (V3) when this product's own
# orders prove it, two or more smaller orders were bought in 90 days, and
# ordering at the best tier would have saved this much over those 90 days
# (the headline floor, $10 a month).
LADDER_CARD_90D_USD = 30.0

_TIMING = "A delivery or a stock-up moves this; count the shelf to know."
_LOW_ACTION = "Earlier bottles may be pouring now; count the shelf before calling it a win."


def _span(days) -> str:
    if days is None:
        return "between the two counts"
    return "in four weeks" if days == MIN_DAYS else f"over {days} days"


def _name(basis) -> str:
    return "pour cost" if basis == "counted" else "bought vs sold"


def _voice(c, basis):
    """(basis, name, tone, suffix, "more bought" | "more used", action lead)
    for one category's card, from its own basis when it says one."""
    cb = c.get("basis") or basis
    if cb == "counted":
        return cb, "pour cost", "bad", "", "more used", ""   # the counted basis only
    return cb, "bought vs sold", "warn", " on purchases", "more bought", _TIMING + " If you didn't stock up, "


def _days_of(c, days):
    """The days a category's reading covers: None (said "between the two
    counts") for a counted one, else the window's."""
    return None if c.get("basis") == "counted" else days


def _usd(v) -> str:
    v = float(v or 0)
    return f"${v:,.2f}" if abs(v) < 100 and round(v) != v else _money(v)


def _pour_conf(pc, cat=None):
    w = pc.get("window") or {}
    fr = pc.get("freshness") or {}
    cov = None
    if cat is not None and w.get("open_days"):
        cov = (cat.get("days_with_sales") or 0) / w["open_days"]
    elif w.get("days"):
        cov = (w.get("open_days") or 0) / w["days"]
    return confidence(window_days=w.get("days"), coverage=cov, invoices=w.get("invoices"),
                      lag_days=fr.get("lag_days"))


def _card(key, tone, headline, metric, action, basis, conf):
    return {"key": key, "tone": tone, "headline": headline, "metric": metric, "action": action,
            "basis": basis, "confidence": conf}


def _price_cards(pw, pc) -> list:
    """Cards for price changes that HELD (W5 row contract: kind, persistent,
    regime {reference, since_mdy}, forward_monthly_usd). A row without
    those fields is not persistent and gives no card."""
    lag = (pc.get("freshness") or {}).get("lag_days") if pc else None
    out = []
    rows = [r for r in (pw.get("increases") or []) + (pw.get("decreases") or [])
            if r.get("persistent") is True and r.get("kind") in ("increase", "decrease")
            and abs(float(r.get("forward_monthly_usd") or 0)) >= PRICE_CARD_MONTHLY_USD
            # Low confidence (the old price seen once — EJ's Ketel One, 10/15/26)
            # stays in the list, never a card.
            and r.get("confidence") != "low" and r.get("headline", True) is not False]
    rows.sort(key=lambda r: -abs(float(r.get("forward_monthly_usd") or 0)))
    for r in rows[:1]:
        up = r["kind"] == "increase"
        reg = r.get("regime") or {}
        # The usual price BEFORE the change. The regime's reference is the
        # new usual once a change has held, so the card read "$330 a case
        # since 9/10/26, from a usual $330" (barfix W9, found on the way).
        ref = r.get("from") if r.get("from") is not None else reg.get("reference")
        since = reg.get("since_mdy") or r.get("since_mdy") or ""
        unit = r.get("unit") or "unit"
        pct = abs(float(r.get("pct") or 0))
        fwd = abs(float(r.get("forward_monthly_usd") or 0))
        metric = (f"{_usd(r.get('to'))} a {unit} since {since}" + (f", from a usual {_usd(ref)}" if ref else "")
                  + f" · about {'+' if up else '−'}{_money(fwd)} a month at your usual buying")
        # The headline already says it held (text round, 10/17/26).
        action = (f"Raise it with {r.get('distributor')} at the next order."
                  if up else
                  f"Confirm with {r.get('distributor')} that it stays.")
        out.append(_card("price:" + str(r.get("product_key")), "warn" if up else "info",
                         f"{r.get('name') or r.get('product')} is {'up' if up else 'down'} {pct:g}% and has held",
                         metric, action, "prices",
                         confidence(invoices=r.get("purchases"), lag_days=lag)))
    held_up = [r for r in (pw.get("increases") or [])
               if r.get("persistent") is True and r.get("kind") == "increase"
               and float(r.get("forward_monthly_usd") or 0) > 0]
    n = len(held_up)
    if n >= CONCENTRATION_MIN_N:
        held_up.sort(key=lambda r: -float(r["forward_monthly_usd"]))
        top = sum(float(r["forward_monthly_usd"]) for r in held_up[:3])
        whole = sum(float(r["forward_monthly_usd"]) for r in held_up)
        share = top / whole if whole else 0
        first = float(held_up[0]["forward_monthly_usd"]) / whole if whole else 1
        if share >= max(0.5, CONCENTRATION_OVER_UNIFORM * 3 / n) and first <= 0.5:
            out.append(_card("concentration", "info",
                             f"Three products carry {round(share * 100)}% of the price increases that held",
                             f"+{_money(top)} of +{_money(whole)} a month at your usual buying, across {n} products",
                             "Start with these three when you next talk to your distributors", "prices",
                             confidence(invoices=n, lag_days=lag)))
    # The ladder as a finding (V3): what the order size costs, from the
    # distributor's own billing — "order 3 kegs, save $15 a keg".
    ladders = [x for x in (pw.get("ladders") or [])
               if x.get("proven") and x.get("confidence") == "high" and (x.get("small_orders_90d") or 0) >= 2
               and float(x.get("save_90d_at_best") or 0) >= LADDER_CARD_90D_USD and x.get("tiers")]
    ladders.sort(key=lambda x: -float(x["save_90d_at_best"]))
    for x in ladders[:1]:
        small, best = x["tiers"][0], x["tiers"][-1]
        unit = x.get("unit") or "unit"
        out.append(_card("ladder:" + str(x.get("product_key")), "info",
                         f"{x.get('name')} costs {_usd(x['save_per_unit'])} a {unit} less ordered "
                         f"{x['best_label']} at a time",
                         f"{x.get('distributor')} bills {x['smallest_label']} at {_usd(small['price'])} and "
                         f"{x['best_label']} at {_usd(best['price'])} · {x['small_orders_90d']} smaller orders in "
                         f"90 days cost {_money(x['save_90d_at_best'])} more",
                         "Where storage and the pace allow, order to the larger size.", "prices",
                         confidence(invoices=sum(t.get("purchases") or 0 for t in x["tiers"]), lag_days=lag)))
    return out


def _insights(pc, pw, to_check) -> list:
    """What deserves a look first: [{key, tone, headline, metric, action,
    basis, confidence}], each from a figure this page measured (no model, no
    estimate dressed as money), most pressing first, at most MAX_INSIGHTS.
    tone is good / warn / bad / info."""
    out = []
    pw = pw or {}
    if pc.get("available"):
        basis = pc.get("basis") or "purchases"
        name = _name(basis)
        bad = "bad" if basis == "counted" else "warn"
        w = pc.get("window") or {}
        days = w.get("days") or 0
        fr = pc.get("freshness") or {}
        cats = pc.get("categories") or []
        tot = pc.get("total") or {}
        if fr.get("stale"):
            out.append(_card("feed_stale", "warn", f"No invoices since {fr.get('invoices_through_mdy')}",
                             (f"Sales through {fr.get('sales_through_mdy')}; " if fr.get("sales_through_mdy")
                              else "") + "nothing is judged until invoices catch up",
                             "Check that Fintech's daily file still reaches Cavnar AI", basis, _pour_conf(pc)))
        for g in (pc.get("gaps") or [])[:1]:
            what = ", ".join(c for c in g.get("categories") or []) or "what they supply"
            out.append(_card("gap", "warn", f"Invoices from {g['distributor']} aren't on file",
                             f"Nothing between {g['from_mdy']} and {g['to_mdy']} — {what} reads low until they arrive",
                             "Forward Fintech's invoice detail for those dates, or mark that the bar stopped "
                             f"buying from {g['distributor']}", basis, _pour_conf(pc)))
        for c in cats:
            s = c.get("stock_up")
            if s and "stock_up" in (c.get("reasons") or []):
                times = (f" — {s['amount'] / s['median_week']:.0f}× its usual week" if s.get("median_week") else "")
                out.append(_card("stock_up:" + c["key"], "info",
                                 f"A large {c['label'].lower()} delivery on {s['date_mdy']}",
                                 f"{s['amount_text']} from {s['distributor']}{times}",
                                 f"{c['label']} isn't judged while it's in the window; count the shelf to know.",
                                 basis, _pour_conf(pc, c)))
                break
        judged = {c["key"]: c for c in cats if c.get("judged")}
        said = set()
        if tot.get("judged") and tot.get("over_target_usd"):
            top = tot.get("top_over")
            more = "more used" if basis == "counted" else "more bought"
            lead = (f"{top['label']} carries the most, {_money(top['usd'])}. " if top else "")
            out.append(_card("over_target:total", bad,
                             f"The bar's {name} is over {_overall_phrase(tot)}" + (" on purchases" if basis == "purchases" else ""),
                             f"{tot['pct']}% {_span(days)} · {_money(tot['over_target_usd'])} {more} than "
                             f"{tot['target']:g}% of sales",
                             lead + (_TIMING + " If you didn't stock up, check pours and comps." if basis == "purchases"
                                     else "Check pours, comps and waste."), basis, _pour_conf(pc)))
            if top:
                said.add(top["key"])
        for c in judged.values():
            jt = c.get("judge_target") or {}
            cb, cn, cbad, on, more, lead = _voice(c, basis)
            if jt.get("scope") == "category" and c.get("over_target_usd"):
                out.append(_card("over_target:" + c["key"], cbad, f"{c['label']} is over {jt['phrase']}{on}",
                                 f"{c['pct']}% {cn} {_span(_days_of(c, days))} · {_money(c['over_target_usd'])} {more} "
                                 f"than {jt['pct']:g}% of its sales",
                                 lead + f"check {c['label'].lower()} pours, comps and the latest prices.", cb,
                                 _pour_conf(pc, c)))
                said.add(c["key"])
        for c in judged.values():
            if c.get("judge_target") or c["key"] in said or c.get("range_tone") != "above" or not c.get("over_range_usd"):
                continue
            lo, hi = c["range"]
            cb, cn, cbad, on, more, lead = _voice(c, basis)
            # Against a purchase ratio the range is context, not a verdict.
            out.append(_card("over:" + c["key"], cbad if cb == "counted" else "info",
                             f"{c['label']} {cn} is above the counted-pour-cost range",
                             f"{c['pct']}% against {lo:g}–{hi:g}% · {_money(c['over_range_usd'])} {more} than the "
                             f"range's top {_span(_days_of(c, days))}",
                             lead + f"check {c['label'].lower()} pours, comps and waste.", cb, _pour_conf(pc, c)))
            said.add(c["key"])
        out.extend(_price_cards(pw, pc))
        trend = pc.get("trend") or []
        noise = pc.get("trend_noise") or {}
        if len(trend) >= 6:
            for c in judged.values():
                k = c["key"]
                band = noise.get(k)
                now, prev, now2, prev2 = trend[-1][k], trend[-5][k], trend[-2][k], trend[-6][k]
                if k in said or band is None or None in (now, prev, now2, prev2):
                    continue
                d1, d2 = now - prev, now2 - prev2
                if abs(d1) <= band or abs(d2) <= band or (d1 > 0) != (d2 > 0):
                    continue
                # The weekly series is always bought vs sold (counts are monthly).
                out.append(_card("trend:" + k, "warn" if d1 > 0 else "info",
                                 f"{c['label']} bought vs sold moved from {prev}% to {now}%",
                                 f"Vs the four weeks to {trend[-5]['end_mdy']}, held a week — beyond its normal "
                                 f"±{band:g}-point swing",
                                 "Delivery timing moves it; count the shelf to know. "
                                 "If deliveries didn't change, check prices, pours and comps.",
                                 "purchases", _pour_conf(pc, c)))
                said.add(k)
        for c in judged.values():
            if c.get("range_tone") != "low" or c["key"] in said:
                continue
            lo, hi = c["range"]
            jt = c.get("judge_target")
            cb, cn = _voice(c, basis)[:2]
            out.append(_card("low:" + c["key"], "info",
                             f"{c['label']} {cn} is below the counted-pour-cost range",
                             f"{c['pct']}% against {lo:g}–{hi:g}%" + (f", under {jt['phrase']}" if jt else ""),
                             _LOW_ACTION if cb == "purchases" else "Check the counts and the pour sizes behind it.",
                             cb, _pour_conf(pc, c)))
        if not tot.get("judged") and tot.get("not_judged") == "short_window" and not out:
            out.append(_card("not_judged:short_window", "info", f"{pc.get('label') or 'Bought vs sold'} isn't judged yet",
                             f"{days} of 28 days of invoices and sales on file ({w.get('from_mdy')} – {w.get('to_mdy')})",
                             "It's judged once four weeks are on file; the figures below are what's in so far.",
                             basis, _pour_conf(pc)))
    else:
        out.extend(_price_cards(pw, pc or {}))
    if to_check:
        out.append(_card("to_check", "info", f"{to_check} product{'' if to_check == 1 else 's'} sorted by a guess",
                         "Counted where Cavnar AI guessed",
                         "Check them under How every product is sorted", "products",
                         confidence()))
    return out[:MAX_INSIGHTS]


def _overall_phrase(tot) -> str:
    label = tot.get("target_label") or "your target"
    pct = tot.get("target")
    if label == "your target":
        return f"your {pct:g}% overall target"
    return label.replace("the target", f"the {pct:g}% overall target", 1)
