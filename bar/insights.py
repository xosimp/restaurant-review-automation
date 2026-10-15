"""
bar/insights.py — What deserves a look: the page's lead insights, each from
a figure the payload measured.
"""
from bar.common import MAX_INSIGHTS, TREND_SAY_POINTS, _money


def _insights(pc, pw, to_check) -> list:
    """What deserves a look first: [{key, tone, headline, metric, action}],
    each from a figure this page measured (no model, no estimate dressed as
    money earned), most pressing first, at most MAX_INSIGHTS. tone is
    good / warn / bad / info."""
    out = []
    if pc.get("available"):
        for g in (pc.get("gaps") or [])[:1]:
            out.append({"key": "gap", "tone": "warn",
                        "headline": f"Invoices from {g['distributor']} aren't on file",
                        "metric": f"Nothing between {g['from_mdy']} and {g['to_mdy']}",
                        "action": "Forward Fintech's invoice detail for those dates — what they supply reads low until then"})
        cats = pc.get("categories") or []
        tot = pc.get("total") or {}
        if tot.get("over_target_usd"):
            out.append({"key": "over_target:total", "tone": "bad",
                        "headline": f"The bar is over your {tot['target']:g}% alcohol cost target",
                        "metric": f"{tot['pct']}% · {_money(tot['over_target_usd'])} over your target in four weeks",
                        "action": "Start with the category furthest over its target below"})
        for c in cats:
            if c.get("target") is not None and c.get("over_target_usd"):
                out.append({"key": "over_target:" + c["key"], "tone": "bad",
                            "headline": f"{c['label']} is over your {c['target']:g}% target",
                            "metric": f"{c['pct']}% · {_money(c['over_target_usd'])} over your target in four weeks",
                            "action": f"Check {c['label'].lower()} pours, comps and the latest prices"})
        for c in cats:
            if c.get("target") is not None:
                continue
            if c.get("tone") == "warn" and c.get("over_range_usd"):
                lo, hi = c["range"]
                out.append({"key": "over:" + c["key"], "tone": "bad",
                            "headline": f"{c['label']} is above its usual range",
                            "metric": f"{c['pct']}% against {lo:g}–{hi:g}% · {_money(c['over_range_usd'])} over the top "
                                      f"of the range in four weeks",
                            "action": f"Check {c['label'].lower()} pours, comps and waste"})
    ups = pw.get("increases") or []
    if ups and (ups[0].get("impact_90d") or 0) > 0:
        r = ups[0]
        out.append({"key": "price:" + r["product_key"], "tone": "warn",
                    "headline": f"{r['name']} went up {r['pct']:g}%",
                    "metric": f"+{_money(r['impact_90d'])} at the {r['bought_90d']:g} {r['unit']}"
                              f"{'' if r['bought_90d'] == 1 else 's'} bought in 90 days",
                    "action": f"Ask {r['distributor']} about it before the next order"})
    positive = [r for r in ups if (r.get("impact_90d") or 0) > 0]
    if len(positive) >= 4:
        top = sum(r["impact_90d"] for r in positive[:3])
        whole = sum(r["impact_90d"] for r in positive)
        share = round(top / whole * 100) if whole else 0
        if share >= 50:
            out.append({"key": "concentration", "tone": "info",
                        "headline": f"Three products carry {share}% of the price increases",
                        "metric": f"+{_money(top)} of +{_money(whole)} at the amounts bought in 90 days",
                        "action": "Start with these three when you talk to your distributors"})
    if pc.get("available"):
        for c in pc.get("categories") or []:
            prev, now = c.get("prev_pct"), c.get("pct")
            if prev is None or now is None or c.get("incomplete") or abs(now - prev) < TREND_SAY_POINTS:
                continue
            better = now < prev
            out.append({"key": "trend:" + c["key"], "tone": "good" if better else "warn",
                        "headline": f"{c['label']} pour cost {'improved' if better else 'rose'}",
                        "metric": f"{now}% from {prev}% four weeks earlier ({c['prev_end_mdy']})",
                        "action": ("Keep doing what changed" if better
                                   else "Look at what moved: prices, pours or comps")})
        for c in pc.get("categories") or []:
            if c.get("tone") == "low" and c.get("target") is None:
                lo, hi = c["range"]
                out.append({"key": "low:" + c["key"], "tone": "info",
                            "headline": f"{c['label']} reads below its usual range",
                            "metric": f"{c['pct']}% against {lo:g}–{hi:g}%",
                            "action": "Purchase-based: bottles bought earlier may be pouring now — count the shelf "
                                      "before reading it as a win"})
    if to_check:
        out.append({"key": "to_check", "tone": "info",
                    "headline": f"{to_check} product{'' if to_check == 1 else 's'} sorted by a guess",
                    "metric": "Pour cost counts them where Cavnar AI guessed",
                    "action": "Check their category under How every product is sorted"})
    return out[:MAX_INSIGHTS]
