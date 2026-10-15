"""The Bar page's one judgement policy (bar/judge.py) and the engine around
it (bar fix round 10/15/26, workstream W4): no verdict on a short window, a
stale feed, a stock-up, missing sales days or missing invoices; a gap
detector that knows a supplier change, a biweekly house and a closure; a
total over what was measured on both sides; the purchase ratio never called
pour cost; targets read through thresholds with who set them.

Most tests feed the engine invoice lines and sales directly (the rule is
asserted on the engine, not on one rendered fixture); the ones about
targets, credits and dates go through the database and bar_cost.build."""
import ast
import json
import os
import re
from datetime import date, timedelta

import pytest

import bar_cost as bc
import benchmark_registry as br
import change_log
import fintech_invoices as fi
import models
import thresholds
from bar import insights as bi
from bar import judge as bj
from bar import pour_cost as bp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T = date(2026, 10, 14)


# ── synthetic lines and sales ─────────────────────────────────────────────

def L(dist, day, cat, net, inv=None, **kw):
    d = day.isoformat() if isinstance(day, date) else day
    return dict({"distributor": dist, "invoice_number": inv or f"{dist[:3]}-{d}", "invoice_date": d,
                 "category": cat, "net": float(net), "kind": "item"}, **kw)


def weekly(dist, cat, net, start, n, step=7, skip=()):
    return [L(dist, start + timedelta(days=step * k), cat, net) for k in range(n) if k not in skip]


def daily(start, n, skip=None, **cats):
    out = {}
    for k in range(n):
        d = start + timedelta(days=k)
        if skip and skip(d):
            continue
        for cat, v in cats.items():
            out.setdefault(cat, {})[d.isoformat()] = float(v)
    return out


def pc_of(lines, sales, today=None, **kw):
    """The engine's reading; today defaults to the day after the last
    invoice (a fresh feed) unless a test is about the clock."""
    if today is None:
        today = date.fromisoformat(max(l["invoice_date"] for l in lines)) + timedelta(days=1)
    return bp._pour_cost(lines, sales, today, **kw)


def cat(pc, key):
    return next(c for c in pc["categories"] if c["key"] == key)


def keys(ins):
    return [i["key"] for i in ins]


def steady(n_weeks=10, start=date(2026, 8, 3)):
    """A weekly liquor house and a weekly beer house, sales every day."""
    lines = weekly("Breakthru", "liquor", 300, start, n_weeks) + weekly("Chicago", "beer", 229, start, n_weeks)
    sales = daily(start, 7 * n_weeks, beer=150, liquor=200)
    return lines, sales


# ── the database path ─────────────────────────────────────────────────────

HEAD = ("Retailer Name,Vendor Name,Location ID,Invoice Number,Invoice Date,Invoice Total,Product Number,"
        "Product Description,U.P.C,Unit Of Measure,Item Quantity,Item Cost,Item Adjustment\n")


def _row(vendor, inv, day, code, desc, uom, qty, cost):
    d = day.strftime("%m/%d/%Y") if isinstance(day, date) else day
    return f'EJs,"{vendor}",1,{inv},{d},${qty * cost:.2f},{code},{desc},0,{uom},{qty},${cost:.2f},$0.00\n'


def _rid(db, **kw):
    return models.create_restaurant(models.Restaurant(name="EJ's", owner_email="o@x.test", **kw), db_path=db)


def _sales_db(db, rid, start, n, **cats):
    conn = models.get_conn(db)
    rows = []
    for k in range(n):
        d = (start + timedelta(days=k)).isoformat()
        for c, v in cats.items():
            rows.append((rid, d, "sales.cat:" + c, v))
    conn.executemany("INSERT INTO dsr_metrics (restaurant_id, business_date, metric, value, status, source) "
                     "VALUES (?,?,?,?, 'ready', 'rpower')", rows)
    conn.commit()
    conn.close()


def _liquor_db(db, rid, start=date(2026, 9, 1), weeks=6, cost=300.0):
    csv = HEAD + "".join(_row("Breakthru Beverage", f"B{k}", start + timedelta(days=7 * k), "9031", "TITOS VDK 1L",
                              "CA", 1, cost) for k in range(weeks))
    fi.ingest_file(rid, "base.csv", csv.encode(), db_path=db)


# ══ 1. windows and judgement ═════════════════════════════════════════════

@pytest.mark.parametrize("overlap", [1, 2, 6])
def test_a_window_shorter_than_four_weeks_is_shown_but_never_judged(overlap):
    """EDG-1 / V8: one invoice and a few days of sales read 300% and
    '$1,128 over your target in four weeks'."""
    first = date(2026, 10, 1) - timedelta(days=overlap - 1)
    lines = [L("Breakthru", "2026-10-01", "liquor", 1200)] + ([L("Breakthru", first, "liquor", 300)]
                                                               if overlap > 1 else [])
    sales = daily(first, overlap + 3, liquor=400)
    pc = pc_of(lines, sales, today=date(2026, 10, 5), targets={"liquor": 18.0, None: 20.0})
    liq = cat(pc, "liquor")
    assert pc["window"]["days"] == overlap and liq["pct"] is not None
    assert liq["judged"] is False and liq["not_judged"] == "short_window"
    assert liq["tone"] is None and liq["range_tone"] is None
    assert liq["over_target_usd"] is None and liq["over_range_usd"] is None
    assert pc["total"]["judged"] is False and pc["total"]["over_target_usd"] is None
    ins = bi._insights(pc, {}, 0)
    assert not any(k.startswith(("over", "low", "trend")) for k in keys(ins))
    assert keys(ins) == ["not_judged:short_window"]
    assert all("four weeks" not in i["metric"] for i in ins)
    assert f"{overlap} of 28 days" in liq["not_judged_text"]


def test_four_weeks_of_steady_buying_is_judged():
    lines, sales = steady()
    pc = pc_of(lines, sales)
    assert pc["window"]["days"] == 28
    for c in ("beer", "liquor"):
        assert cat(pc, c)["judged"] is True and cat(pc, c)["range_tone"] is not None
        assert cat(pc, c)["tone"] is None, "no target, purchase basis: the range is context"
    assert pc["total"]["judged"] is True and pc["total"]["range_tone"] is not None


def test_the_total_counts_only_categories_measured_on_both_sides():
    """EDG-4 S5: wine bought with no wine POS category, beer sold with no beer
    invoices — the total read 33.3% while liquor alone was 42.9%."""
    start = date(2026, 9, 1)
    lines = weekly("Breakthru", "liquor", 300, start, 6) + weekly("Winebow", "wine", 400, start, 6)
    sales = daily(start, 40, liquor=100, beer=200)
    pc = pc_of(lines, sales)
    liq = cat(pc, "liquor")
    assert pc["total"]["pct"] == liq["pct"]
    assert pc["total"]["partial"] is True
    assert {e["key"]: e["why"] for e in pc["total"]["excluded"]} == {"beer": "no_purchases", "wine": "no_sales"}
    assert pc["total"]["included"] == ["liquor"]


@pytest.mark.parametrize("fixture", ["steady", "gap"])
def test_the_total_matches_the_trends_latest_point(fixture):
    if fixture == "steady":
        lines, sales = steady()
    else:
        lines = weekly("Breakthru", "liquor", 300, date(2026, 8, 3), 10, skip=(6, 7)) \
            + weekly("Chicago", "beer", 229, date(2026, 8, 3), 10)
        sales = daily(date(2026, 8, 3), 70, beer=150, liquor=200)
    pc = pc_of(lines, sales)
    assert pc["trend"] and pc["trend"][-1]["end"] == pc["window"]["to"]
    assert pc["total"]["pct"] == pc["trend"][-1]["total"]
    assert pc["total"]["partial"] == pc["trend"][-1]["total_partial"]


def test_a_stock_up_is_named_not_judged_as_pour_cost():
    """EDG-6 / V11: one 40-case buy read 228.6% and '$11,570 over the top of
    the range' with 'Check liquor pours, comps and waste'."""
    start = date(2026, 9, 1)
    lines = weekly("Breakthru", "liquor", 300, start, 6) + weekly("Chicago", "beer", 229, start, 6)
    lines.append(L("Breakthru", "2026-10-02", "liquor", 11600, inv="BIG"))
    sales = daily(start, 40, beer=150, liquor=200)
    pc = pc_of(lines, sales)
    liq = cat(pc, "liquor")
    assert liq["stock_up"]["date"] == "2026-10-02" and liq["stock_up"]["amount"] == 11600.0
    assert liq["judged"] is False and liq["not_judged"] == "stock_up" and liq["tone"] is None
    assert liq["over_range_usd"] is None
    ins = bi._insights(pc, {}, 0)
    assert "over:liquor" not in keys(ins)
    s = next(i for i in ins if i["key"] == "stock_up:liquor")
    assert "10/2/26" in s["headline"] and "$11,600" in s["metric"]
    assert pc["total"]["judged"] is False


def test_a_reading_over_twice_the_range_top_is_a_stock_up_whatever_the_history():
    lines = [L("Breakthru", d, "liquor", 300) for d in ("2026-09-09", "2026-09-23")]
    lines.append(L("Breakthru", "2026-10-06", "liquor", 6000, inv="BIG"))
    sales = daily(date(2026, 9, 9), 30, liquor=200)
    pc = pc_of(lines, sales)
    liq = cat(pc, "liquor")
    assert liq["stock_up"] and liq["stock_up"]["why"] == "range" and liq["judged"] is False


def test_invoices_older_than_a_week_say_the_feed_has_stopped():
    """EDG-11 / V22: invoices stopped 7/27 with today 10/14 — a months-old
    window read 'good' with no warning."""
    lines, sales = steady(n_weeks=13, start=date(2026, 4, 27))
    sales = daily(date(2026, 4, 27), 170, beer=150, liquor=200)
    pc = pc_of(lines, sales, today=T)
    assert pc["freshness"]["stale"] is True and pc["freshness"]["invoices_through_mdy"] == "7/20/26"
    for c in ("beer", "liquor"):
        assert cat(pc, c)["judged"] is False and cat(pc, c)["not_judged"] == "feed_stale"
        assert cat(pc, c)["tone"] is None
    ins = bi._insights(pc, {}, 0)
    assert ins[0]["key"] == "feed_stale"
    assert ins[0]["headline"] == "No invoices since 7/20/26 — check the Fintech forward"
    # The feed (W2's bar.feed) says how far invoices reach; staleness is judged
    # against today and the houses' own rhythm, not the feed's fixed-week flag.
    fresh = pc_of(*steady(), feed={"invoices_through": "2026-10-05", "stale_days": None})
    assert fresh["freshness"]["stale"] is False
    stale = pc_of(*steady(), today=T, feed={"invoices_through": "2026-10-05", "stale_days": None})
    assert stale["freshness"]["stale"] is True and cat(stale, "liquor")["judged"] is False


def test_a_category_missing_sales_days_is_not_judged():
    """EDG-17 S18: wine sales missing on 5 of 28 days was judged 'good'."""
    start = date(2026, 9, 1)
    lines = weekly("Breakthru", "liquor", 300, start, 6) + weekly("Winebow", "wine", 100, start, 6)
    sales = daily(start, 40, liquor=200)
    sales["wine"] = {d: 50.0 for d in daily(start, 31, wine=1)["wine"]}
    pc = pc_of(lines, sales)
    wine = cat(pc, "wine")
    assert wine["days_with_sales"] < 0.85 * pc["window"]["open_days"]
    assert wine["judged"] is False and wine["not_judged"] == "sales_coverage" and wine["tone"] is None
    assert cat(pc, "liquor")["judged"] is True


def test_a_closure_after_stocking_is_not_judged():
    """EDG-17 S19: closed two weeks after stocking read 'above range'."""
    start = date(2026, 8, 3)
    lines = weekly("Breakthru", "liquor", 300, start, 10, skip=(7,)) + weekly("Chicago", "beer", 229, start, 10, skip=(7,))
    sales = daily(start, 70, skip=lambda d: date(2026, 9, 14) <= d <= date(2026, 9, 27), beer=150, liquor=200)
    pc = pc_of(lines, sales)
    assert pc["gaps"] == []
    for c in ("beer", "liquor"):
        assert cat(pc, c)["judged"] is False and "closed_days" in cat(pc, c)["reasons"]


def test_net_credits_say_so_rather_than_no_invoices():
    """EDG-18 / V37: a credit bigger than the window's purchases read 'No
    liquor invoices in these four weeks'."""
    start = date(2026, 9, 1)
    lines = [L("Breakthru", start + timedelta(days=7 * k), "liquor", 300) for k in range(2)]
    lines += weekly("Chicago", "beer", 229, start, 6)
    lines.append(L("Breakthru", "2026-10-05", "liquor", -1500, inv="CM2"))
    sales = daily(start, 40, beer=150, liquor=200)
    pc = pc_of(lines, sales)
    liq = cat(pc, "liquor")
    assert liq["purchases"] < 0 and liq["pct"] is None
    assert liq["credits_exceed"] is True and liq["not_judged"] == "credits_exceed"
    assert "Credits exceed purchases" in liq["not_judged_text"]
    assert {e["key"]: e["why"] for e in pc["total"]["excluded"]}["liquor"] == "credits_exceed"


def test_targets_never_judge_an_incomplete_category():
    lines = weekly("Breakthru", "liquor", 300, date(2026, 8, 3), 10, skip=(6, 7)) \
        + weekly("Chicago", "beer", 229, date(2026, 8, 3), 10)
    sales = daily(date(2026, 8, 3), 70, beer=150, liquor=200)
    pc = pc_of(lines, sales, targets={"liquor": 10.0, None: 10.0})
    liq = cat(pc, "liquor")
    assert liq["incomplete"] is True and liq["tone"] is None and liq["over_target_usd"] is None
    assert pc["total"]["over_target_usd"] is None and pc["total"]["judged"] is False
    assert not any(k.startswith("over_target") for k in keys(bi._insights(pc, {}, 0)))


def test_a_low_reading_keeps_its_caveat_under_an_owner_target():
    """V18 / AIX-5: liquor drawn down to ~10% under an 18% target turned green
    and the 'count the shelf' caveat vanished."""
    lines, sales = steady()
    lines = [dict(l, net=140.0) if l["category"] == "liquor" else l for l in lines]
    pc = pc_of(lines, sales, targets={"liquor": 18.0})
    liq = cat(pc, "liquor")
    assert liq["judged"] and liq["range_tone"] == "low" and liq["target_tone"] == "under"
    assert liq["tone"] == "low", "under the target but below the range is never plain good"
    low = next(i for i in bi._insights(pc, {}, 0) if i["key"] == "low:liquor")
    assert "count the shelf" in low["action"] and "your 18% target" in low["metric"]


def test_clearing_a_target_returns_to_the_industry_range():
    lines, sales = steady()
    with_t = pc_of(lines, sales, targets={"liquor": 10.0})
    assert cat(with_t, "liquor")["target"] == 10.0 and cat(with_t, "liquor")["tone"] == "warn"
    cleared = pc_of(lines, sales, targets={"liquor": None})
    liq = cat(cleared, "liquor")
    assert liq["target"] is None and liq["target_source"] == "industry" and liq["judge_target"] is None
    lo, hi = liq["range"]
    assert liq["range_tone"] == ("inside" if lo <= liq["pct"] <= hi else ("above" if liq["pct"] > hi else "low"))
    # The range describes COUNTED pour cost: against a purchase ratio it is
    # context (range_tone), never a verdict tone (V1).
    assert liq["tone"] is None


def test_zero_target_is_unset():
    lines, sales = steady()
    for v in (0, -5, 0.0, "abc", 200):
        pc = pc_of(lines, sales, targets={"liquor": v, None: v})
        assert cat(pc, "liquor")["target"] is None and pc["total"]["target"] is None, v
    assert thresholds.bar_target_value(0) is None and thresholds.bar_target_value(-5) is None
    assert thresholds.bar_target_value("20") == 20.0


def test_overall_target_insight_names_a_real_category():
    """V19 / AIX-6: with only an overall target the insight said 'Start with
    the category furthest over its target below' — no category had one."""
    lines, sales = steady()
    pc = pc_of(lines, sales, targets={None: 15.0})
    tot = pc["total"]
    assert tot["judged"] and tot["over_target_usd"] > 0
    assert tot["top_over"]["key"] in ("beer", "liquor")
    for c in ("beer", "liquor"):
        jt = cat(pc, c)["judge_target"]
        assert jt["scope"] == "overall" and jt["phrase"] == "your 15% overall target"
        assert cat(pc, c)["target"] is None, "a category's own target stays its own"
    ins = next(i for i in bi._insights(pc, {}, 0) if i["key"] == "over_target:total")
    assert tot["top_over"]["label"] in ins["action"]
    assert "its target below" not in ins["action"]
    assert ins["headline"] == "The bar's bought vs sold is over your 15% overall target on purchases"


def test_over_range_usd_uses_unrounded_ratio():
    """R27: the dollars over the range came from the percentage rounded to
    0.1 — off by up to $32 at $63k of sales."""
    start = date(2026, 8, 3)
    lines = weekly("Breakthru", "liquor", 3001.37, start, 10)
    sales = daily(start, 70, liquor=1517.13)
    pc = pc_of(lines, sales)
    liq = cat(pc, "liquor")
    hi = liq["range"][1]
    raw_b = sum(l["net"] for l in lines if pc["window"]["from"] <= l["invoice_date"] <= pc["window"]["to"])
    raw_s = 1517.13 * 28
    assert liq["over_range_usd"] == round(raw_b - hi / 100 * raw_s, 2)


# ══ 2. gaps ═══════════════════════════════════════════════════════════════

def test_a_distributor_that_stops_while_another_takes_over_is_not_a_gap():
    """EDG-2 S4c: Breakthru -> Southern Glazer's left liquor incomplete forever
    and every trend point null."""
    lines = weekly("Breakthru", "liquor", 300, date(2026, 6, 1), 12) \
        + weekly("Southern Glazer's", "liquor", 300, date(2026, 8, 24), 8) \
        + weekly("Chicago", "beer", 229, date(2026, 6, 1), 20)
    sales = daily(date(2026, 6, 1), 140, beer=150, liquor=200)
    pc = pc_of(lines, sales)
    assert pc["gaps"] == []
    liq = cat(pc, "liquor")
    assert not liq.get("incomplete") and liq["judged"] is True
    assert all(p["liquor"] is not None for p in pc["trend"])
    gone = {s["distributor"]: s for s in pc["stopped"]}
    assert gone["Breakthru"]["why"] == "switched" and gone["Breakthru"]["taken_over_by"] == ["Southern Glazer's"]


def test_a_new_distributor_has_no_gap_before_its_first_invoice():
    lines = weekly("Chicago", "beer", 229, date(2026, 6, 1), 20) \
        + weekly("Winebow", "wine", 400, date(2026, 9, 21), 4)
    sales = daily(date(2026, 6, 1), 140, beer=150, wine=100)
    pc = pc_of(lines, sales)
    assert pc["gaps"] == []
    assert all(not m["incomplete"] for m in pc["months"])
    assert not cat(pc, "wine").get("incomplete")


def test_a_biweekly_house_overdue_three_weeks_is_a_gap_not_a_low_reading():
    """EDG-3 S4b: no gap, and liquor 10.7% 'low' against a true 21.4%."""
    lines = weekly("Breakthru", "liquor", 600, date(2026, 7, 6), 6, step=14) \
        + weekly("Chicago", "beer", 229, date(2026, 7, 6), 14)
    sales = daily(date(2026, 7, 6), 100, beer=150, liquor=200)
    pc = pc_of(lines, sales)
    assert [g["distributor"] for g in pc["gaps"]] == ["Breakthru"]
    liq = cat(pc, "liquor")
    assert liq["incomplete"] is True and liq["tone"] is None
    assert "low:liquor" not in keys(bi._insights(pc, {}, 0))


def test_a_biweekly_house_on_its_rhythm_is_no_gap():
    lines = weekly("Breakthru", "liquor", 600, date(2026, 7, 6), 7, step=14) \
        + weekly("Chicago", "beer", 229, date(2026, 7, 6), 14)
    sales = daily(date(2026, 7, 6), 100, beer=150, liquor=200)
    pc = pc_of(lines, sales)
    assert pc["gaps"] == [] and cat(pc, "liquor")["judged"] is True


def test_a_house_with_three_invoices_that_goes_quiet_is_flagged():
    """EDG-3 S4d: a wine house with three invoices then silence read 14.3%
    'low' with no gap."""
    lines = weekly("Winebow", "wine", 400, date(2026, 9, 1), 3) + weekly("Chicago", "beer", 229, date(2026, 9, 1), 6)
    sales = daily(date(2026, 9, 1), 45, beer=150, wine=100)
    pc = pc_of(lines, sales)
    assert [g["distributor"] for g in pc["gaps"]] == ["Winebow"]
    assert cat(pc, "wine")["incomplete"] is True


def test_a_closed_fortnight_is_not_a_missing_invoice_gap():
    """EDG-17 S19b: closed three weeks with deliveries paused said 'Invoices
    from Breakthru aren't on file — forward Fintech's invoice detail'."""
    start = date(2026, 8, 3)
    lines = weekly("Breakthru", "liquor", 300, start, 10, skip=(6, 7)) + weekly("Chicago", "beer", 229, start, 10, skip=(6, 7))
    sales = daily(start, 70, skip=lambda d: date(2026, 9, 7) <= d <= date(2026, 9, 27), beer=150, liquor=200)
    pc = pc_of(lines, sales)
    assert pc["gaps"] == []
    assert "gap" not in keys(bi._insights(pc, {}, 0))


def test_a_trend_window_with_a_house_missing_is_a_gap_and_its_total_is_partial():
    """EDG-24: the old test of this name asserted there were no gaps. A
    window with Breakthru's weeks missing is a liquor gap in the trend, and
    its total is the beer-only figure, labelled partial — the same figure the
    page's total gives for that window (V10)."""
    lines = weekly("Breakthru", "liquor", 300, date(2026, 8, 3), 10, skip=(6, 7)) \
        + weekly("Chicago", "beer", 229, date(2026, 8, 3), 10)
    sales = daily(date(2026, 8, 3), 70, beer=150, liquor=200)
    pc = pc_of(lines, sales)
    last = pc["trend"][-1]
    assert last["liquor"] is None and last["beer"] is not None
    assert last["total_partial"] is True and last["total"] == cat(pc, "beer")["pct"]
    assert any(p["liquor"] is not None for p in pc["trend"][:-3]), "the weeks before the hole read normally"


def test_a_month_with_a_gap_is_marked_incomplete():
    lines = weekly("Breakthru", "liquor", 300, date(2026, 8, 3), 10, skip=(6, 7)) \
        + weekly("Chicago", "beer", 229, date(2026, 8, 3), 10)
    sales = daily(date(2026, 8, 3), 70, beer=150, liquor=200)
    pc = pc_of(lines, sales)
    by = {m["month"]: m for m in pc["months"]}
    assert by["2026-09"]["incomplete"] is True
    assert next(c for c in by["2026-09"]["categories"] if c["key"] == "liquor")["incomplete"] is True
    assert by["2026-08"]["incomplete"] is False and by["2026-08"]["partial"] is True, "starts 8/3"
    assert by["2026-09"]["partial"] is False
    assert by["2026-10"]["partial"] is True


def test_the_owner_saying_we_stopped_buying_retires_a_gap(db_path):
    rid = _rid(db_path)
    lines = weekly("Breakthru", "liquor", 600, date(2026, 7, 6), 6, step=14) \
        + weekly("Chicago", "beer", 229, date(2026, 7, 6), 14)
    sales = daily(date(2026, 7, 6), 100, beer=150, liquor=200)
    assert pc_of(lines, sales)["gaps"]
    bj.set_stopped(rid, "  Breakthru ", user_id=1, db_path=db_path)
    said = bj.owner_stopped(rid, db_path=db_path)
    pc = pc_of(lines, sales, owner_stopped=said)
    assert pc["gaps"] == [] and pc["stopped"][0]["why"] == "owner"
    bj.set_stopped(rid, "Breakthru", stopped_buying=False, db_path=db_path)
    assert bj.owner_stopped(rid, db_path=db_path) == []


def test_an_unbalanced_invoice_marks_its_category_incomplete():
    lines, sales = steady()
    # W1's _lines carry balanced True / False / None (None: not known).
    lines = [dict(l, balanced=None if i % 2 else True) for i, l in enumerate(lines)]
    assert pc_of(lines, sales)["unbalanced"] == []
    lines[-2] = dict(lines[-2], balanced=False)
    pc = pc_of(lines, sales)
    bad = lines[-2]["category"]
    assert cat(pc, bad)["judged"] is False and cat(pc, bad)["not_judged"] == "unbalanced"
    assert pc["complete"] is False and pc["unbalanced"] == [bad]


# ══ 3. trend, context and fiscal months ══════════════════════════════════

def _pc_with_trend(series, key="wine"):
    pts = [{"end": f"2026-09-{i + 1:02d}", "end_mdy": f"9/{i + 1}/26", "from_mdy": "", "beer": None, "liquor": None,
            "wine": None, "total": None, key: v} for i, v in enumerate(series)]
    c = {"key": key, "label": "Wine", "pct": series[-1], "judged": True, "range_tone": "inside", "range": [30, 40],
         "days_with_sales": 28, "judge_target": None}
    return {"available": True, "basis": "purchases", "window": {"days": 28, "open_days": 28, "invoices": 8},
            "freshness": {"lag_days": 1}, "categories": [c], "total": {}, "gaps": [], "trend": pts,
            "trend_noise": {key: bp._noise(series)}}


def test_trend_insight_needs_a_move_beyond_the_series_noise():
    """R1 / AIX-4: EJ's wine read 17.5 -> 33.4 -> 38.0 -> 30.2 from delivery
    timing alone; a fixed 1.5-point rule would fire most weeks."""
    noisy = [17.5, 33.4, 38.0, 30.2, 31.0, 22.4, 35.0]
    assert not any(k.startswith("trend:") for k in keys(bi._insights(_pc_with_trend(noisy), {}, 0)))
    assert bp._noise(noisy) >= bp.TREND_NOISE_FLOOR
    # A steady series that steps up and stays up is said — once, as a move, never "improved".
    steady_up = [20.0, 20.5, 20.0, 20.4, 20.2, 20.1, 20.3, 27.0, 27.5, 27.1]
    ins = bi._insights(_pc_with_trend(steady_up), {}, 0)
    t = next(i for i in ins if i["key"] == "trend:wine")
    assert "moved from" in t["headline"] and "improved" not in json.dumps(ins)
    # A one-week blip does not persist two readings: nothing said.
    blip = [20.0, 20.5, 20.0, 20.4, 20.2, 20.1, 20.3, 20.2, 20.4, 28.0]
    assert not any(k.startswith("trend:") for k in keys(bi._insights(_pc_with_trend(blip), {}, 0)))


def test_the_trend_compares_non_overlapping_windows_and_names_the_noise():
    lines, sales = steady(n_weeks=14, start=date(2026, 6, 29))
    pc = pc_of(lines, sales)
    t = pc["trend"]
    assert len(t) >= 5
    assert (date.fromisoformat(t[-1]["end"]) - date.fromisoformat(t[-5]["end"])).days == 28, \
        "four weeks earlier ends where this window begins: the two don't overlap"
    assert set(pc["trend_noise"]) == {"total", "beer", "liquor", "wine"}


def test_a_13_week_context_reading_rides_beside_the_four_weeks():
    lines, sales = steady(n_weeks=16, start=date(2026, 6, 22))
    pc = pc_of(lines, sales)
    ctx = pc["context_13w"]
    assert ctx and ctx["days"] == 91 and ctx["to"] == pc["window"]["to"]
    assert ctx["liquor"] is not None and ctx["total"] is not None
    assert pc_of(*steady(n_weeks=6, start=date(2026, 9, 1)))["context_13w"] is None


def test_month_rows_follow_the_restaurants_fiscal_periods_when_it_keeps_them():
    """R1: September held five weekly deliveries and read 26.5% while every
    four-week window that month read 21.7–23.8; EJ's books run 4-4-5 from
    12/31/25 (Wednesday weeks)."""
    from models import Restaurant
    r = Restaurant(name="EJ's", owner_email="o@x.test", fiscal_week_start_dow=2, fiscal_year_start="2025-12-31",
                   fiscal_period_scheme="445")
    lines, sales = steady(n_weeks=14, start=date(2026, 6, 29))
    pc = pc_of(lines, sales, restaurant=r)
    rows = pc["months"]
    assert rows and all(m["kind"] == "period" for m in rows)
    full = [m for m in rows if not m["partial"]]
    for m in full:
        n = (date.fromisoformat(m["to"]) - date.fromisoformat(m["from"])).days + 1
        assert n in (28, 35), "a period holds whole weeks"
        assert date.fromisoformat(m["from"]).weekday() == 2
    assert all(m["kind"] == "month" for m in pc_of(lines, sales)["months"])


# ══ 4. names, ranges, copy ═══════════════════════════════════════════════

_POUR = re.compile(r"pour cost", re.I)


def _says_pour_cost_of_a_purchase_ratio(text):
    """'pour cost' that is not 'counted pour cost' / 'Pour cost (counted)'."""
    for m in _POUR.finditer(text):
        before = text[max(0, m.start() - 8):m.start()].lower()
        after = text[m.end():m.end() + 10].lower()
        if "counted" in before or after.startswith(" (counted"):
            continue
        return True
    return False


def test_purchase_ratio_never_labelled_pour_cost_without_counts():
    """V1: the purchase ratio was shown and judged as pour cost. Every
    string literal the bar package can put in front of an owner, and every
    string in a built payload's insights, reasons and labels, says 'bought
    vs sold' unless it is about COUNTED pour cost."""
    import tokenize
    import io
    pkg = os.path.join(ROOT, "bar")
    for f in sorted(os.listdir(pkg)):
        # bar/counts.py is the COUNTED pour cost's own module (W6a).
        if not f.endswith(".py") or f == "counts.py":
            continue
        src = open(os.path.join(pkg, f), encoding="utf-8").read()
        doc_lines = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)) and ast.get_docstring(node):
                d = node.body[0]
                doc_lines.update(range(d.lineno, d.end_lineno + 1))
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            is_str = tok.type == tokenize.STRING or tok.type == getattr(tokenize, "FSTRING_MIDDLE", -1)
            if not is_str or tok.start[0] in doc_lines:
                continue
            text = tok.string
            # The counted basis's own name and copy are allowed to say it.
            if "counted" in src.splitlines()[tok.start[0] - 1]:
                continue
            assert not _says_pour_cost_of_a_purchase_ratio(text), f"bar/{f}:{tok.start[0]}: {text}"
    lines, sales = steady()
    lines = [dict(l, net=900.0) if l["category"] == "liquor" else l for l in lines]
    for targets in ({}, {"liquor": 18.0}, {None: 15.0}):
        pc = pc_of(lines, sales, targets=targets)
        assert pc["basis"] == "purchases" and pc["label"] == "Bought vs sold"
        for i in bi._insights(pc, {}, 0):
            for k in ("headline", "metric", "action"):
                assert not _says_pour_cost_of_a_purchase_ratio(i[k]), i[k]
            assert "improved" not in i["headline"] and "Keep doing what changed" not in i["action"]
        for c in pc["categories"]:
            assert not _says_pour_cost_of_a_purchase_ratio(c.get("not_judged_text") or "")


def test_two_counts_make_the_reading_pour_cost_counted():
    """V1: "Pour cost" is reserved for a counted figure. A counted period
    covering the window (bar.counts.latest_counted, W6a) turns the basis to
    "counted"; the category's figure is what was used; the copy may say
    pour cost — and only then."""
    lines, sales = steady()
    lines = [dict(l, net=900.0) if l["category"] == "liquor" else l for l in lines]
    period = {"from": "2026-09-07", "to": "2026-10-05", "from_mdy": "9/7/26", "to_mdy": "10/5/26", "ok": True,
              "categories": {"beer": {"begin": 900, "purchases": 916, "end": 820, "used": 996, "sales": 4200,
                                      "pct": 23.7},
                             "liquor": {"begin": 2000, "purchases": 3600, "end": 4300, "used": 1300, "sales": 5600,
                                        "pct": 23.2},
                             "wine": {"begin": None, "purchases": None, "end": None, "used": None, "sales": None,
                                      "pct": None}}}
    pc = pc_of(lines, sales, targets={"liquor": 18.0}, counted=period)
    liq = cat(pc, "liquor")
    assert pc["basis"] == "counted" and pc["label"] == "Pour cost (counted)"
    assert liq["basis"] == "counted" and liq["pct"] == 23.2 and liq["purchase_pct"] > 60
    assert liq["counted"]["used"] == 1300 and liq["judged"] is True and liq["stock_up"] is None
    assert liq["over_target_usd"] == round(1300 - 0.18 * 5600, 2)
    card = next(i for i in bi._insights(pc, {}, 0) if i["key"] == "over_target:liquor")
    assert card["basis"] == "counted" and "pour cost" in card["metric"] and "on purchases" not in card["headline"]
    assert "between the two counts" in card["metric"]
    # Without the period the same bar is bought vs sold.
    plain = pc_of(lines, sales, targets={"liquor": 18.0})
    assert plain["basis"] == "purchases" and cat(plain, "liquor")["basis"] == "purchases"


def test_build_takes_the_counted_basis_from_bar_counts(db_path):
    """The wiring: two shelf counts bracketing the four-week window
    (bar.counts.latest_counted) make bar_cost.build's reading counted."""
    from bar import counts as bcounts
    rid = _targets_fixture(db_path)
    out = bc.build(rid, db_path=db_path, today=date(2026, 10, 6))
    assert out["pour_cost"]["basis"] == "purchases"
    w = out["pour_cost"]["window"]
    bcounts.save_count(rid, (date.fromisoformat(w["from"]) - timedelta(days=1)).isoformat(),
                       {"liquor": 2000}, db_path=db_path, today=date(2026, 10, 6))
    bcounts.save_count(rid, w["to"], {"liquor": 4000}, db_path=db_path, today=date(2026, 10, 6))
    out = bc.build(rid, db_path=db_path, today=date(2026, 10, 6))
    pc = out["pour_cost"]
    assert out["counted"]["available"] is True
    assert pc["basis"] == "counted" and pc["label"] == "Pour cost (counted)"
    liq = cat(pc, "liquor")
    assert liq["basis"] == "counted" and liq["counted"]["used"] == 2000 + 3600 - 4000


def test_over_actions_lead_with_purchase_timing():
    lines, sales = steady()
    lines = [dict(l, net=900.0) if l["category"] == "liquor" else l for l in lines]
    pc = pc_of(lines, sales, targets={"liquor": 18.0})
    ins = next(i for i in bi._insights(pc, {}, 0) if i["key"] == "over_target:liquor")
    assert ins["action"].startswith("A delivery or a stock-up moves this; count the shelf to know.")
    assert ins["headline"].endswith("on purchases")
    over = next(i for i in bi._insights(pc_of(lines, sales), {}, 0) if i["key"] == "over:liquor")
    assert over["action"].startswith("A delivery or a stock-up moves this")


def test_bar_ranges_come_from_the_registry():
    """R6 / FIN-8: the ranges were unsourced constants (liquor 16–22 here,
    18–22 in the audit). Each now comes from benchmark_registry with its
    source and year, for COUNTED pour cost, or is Cavnar AI's starting range,
    said so — never an invented source."""
    for c in ("liquor", "wine"):
        r = bp.range_for(c, today=T)
        e = br.lookup(bp._REGISTRY_METRIC[c], None, definition="counted_pour_cost", today=T)
        assert r["range"] == [e["low"], e["high"]] and r["year"] == e["year"] and r["kind"] == "vendor"
        assert r["definition"] == "counted_pour_cost" and e["source"] in r["source"]
    beer = bp.range_for("beer", today=T)
    assert beer["label"] == "Cavnar AI's starting range" and beer["kind"] == "starting" and beer["year"] is None
    assert br.why_absent("pour_cost_beer_pct")
    # A range past its age limit falls back to the starting range, labelled so.
    old = bp.range_for("wine", today=date(2030, 1, 1))
    assert old["kind"] == "starting" and old["label"] == "Cavnar AI's starting range"
    src = open(os.path.join(ROOT, "bar", "pour_cost.py"), encoding="utf-8").read()
    assert "RULE_OF_THUMB = {c: tuple(range_for(c)" in src
    pc = pc_of(*steady())
    for c in pc["categories"]:
        assert c["range_source"]["definition"] == "counted_pour_cost"
        assert c["range_source"]["label"]


def test_insights_carry_basis_and_confidence():
    """R19: rule-based cards had no Evidence/Freshness line."""
    lines, sales = steady()
    lines = [dict(l, net=900.0) if l["category"] == "liquor" else l for l in lines]
    ins = bi._insights(pc_of(lines, sales, targets={"liquor": 18.0}), {}, 3)
    assert ins
    for i in ins:
        assert set(i) == {"key", "tone", "headline", "metric", "action", "basis", "confidence"}
        assert set(i["confidence"]) == {"evidence_pct", "freshness_pct"}
        assert 0 <= i["confidence"]["evidence_pct"] <= 100
    assert next(i for i in ins if i["key"] == "over_target:liquor")["basis"] == "purchases"
    short = bi._insights(pc_of([L("B", "2026-10-01", "liquor", 1200)], daily(date(2026, 9, 30), 5, liquor=400),
                               today=date(2026, 10, 5)), {}, 0)
    assert short[0]["confidence"]["evidence_pct"] < 10
    assert bj.confidence(lag_days=1)["freshness_pct"] == 100 and bj.confidence(lag_days=14)["freshness_pct"] == 0


def test_owner_copy_lint_reads_bar_cost():
    import importlib.util
    spec = importlib.util.spec_from_file_location("check_owner_copy", os.path.join(ROOT, "scripts", "check_owner_copy.py"))
    lint = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lint)
    assert "bar_cost.py" in lint.PY_FILES
    for f in os.listdir(os.path.join(ROOT, "bar")):
        if f.endswith(".py"):
            assert "bar/" + f in lint.PY_FILES, f


# ══ 5. price cards (W5's row contract) ═══════════════════════════════════

def _row_up(i, fwd, kind="increase", persistent=True, **kw):
    return dict({"product_key": f"b|{i}", "name": f"Vodka {i}", "distributor": "Breakthru", "unit": "case",
                 "from": 150.0, "to": 160.0, "pct": 6.7, "kind": kind, "persistent": persistent,
                 "regime": {"reference": 150.0, "since_mdy": "9/1/26"}, "forward_monthly_usd": fwd,
                 "purchases": 4, "impact_90d": fwd * 3}, **kw)


def test_the_price_insight_only_names_a_persistent_same_tier_change():
    """V2 / V24: a deal ending read as '+10.1%' and 'Ask Southern Glazer's
    about it'. A card now needs W5's kind 'increase' AND persistent, worth
    $10 a month; a row without those fields gives no card."""
    pc = {"available": False}
    assert bi._insights(pc, {"increases": [_row_up(1, 40, persistent=False)]}, 0) == []
    assert bi._insights(pc, {"increases": [_row_up(1, 40, kind="deal_ended")]}, 0) == []
    assert bi._insights(pc, {"increases": [_row_up(1, 6)]}, 0) == []
    legacy = {k: v for k, v in _row_up(1, 40).items() if k not in ("kind", "persistent", "forward_monthly_usd")}
    assert bi._insights(pc, {"increases": [legacy]}, 0) == []
    up = bi._insights(pc, {"increases": [_row_up(1, 40)]}, 0)[0]
    assert up["key"] == "price:b|1" and "has held" in up["headline"]
    assert "from a usual $150" in up["metric"] and "9/1/26" in up["metric"]
    assert "Ask " not in up["action"] and "Breakthru" in up["action"]
    down = bi._insights(pc, {"decreases": [_row_up(2, -40, kind="decrease", pct=-6.7, to=140.0)]}, 0)[0]
    assert "has held" in down["headline"] and down["action"].startswith("It has held at the new price")


def test_concentration_needs_more_than_its_arithmetic_floor():
    """V25 / AIX-8: four equal increases gave 'Three products carry 75%'."""
    pc = {"available": False}
    four = [_row_up(i, 20) for i in range(4)]
    assert "concentration" not in keys(bi._insights(pc, {"increases": four}, 0))
    eight_even = [_row_up(i, 20) for i in range(8)]
    assert "concentration" not in keys(bi._insights(pc, {"increases": eight_even}, 0))
    skewed = [_row_up(0, 40), _row_up(1, 35), _row_up(2, 30)] + [_row_up(i, 3) for i in range(3, 9)]
    assert "concentration" in keys(bi._insights(pc, {"increases": skewed}, 0))
    # The price card already covering most of it leaves no room for a second card about it.
    one_big = [_row_up(0, 300)] + [_row_up(i, 5) for i in range(1, 9)]
    assert "concentration" not in keys(bi._insights(pc, {"increases": one_big}, 0))


# ══ 6. targets through thresholds and the model layer ════════════════════

def _targets_fixture(db):
    rid = _rid(db)
    _liquor_db(db, rid, start=date(2026, 8, 3), weeks=10, cost=900.0)
    _sales_db(db, rid, date(2026, 8, 3), 70, Liquor=200)
    return rid


def test_the_owners_targets_judge_a_four_week_window(db_path):
    rid = _targets_fixture(db_path)
    models.update_restaurant(rid, {"liquor_cost_target": 18.0, "bar_cost_target": 25.0}, db_path=db_path)
    out = bc.build(rid, db_path=db_path, today=date(2026, 10, 12))
    pc = out["pour_cost"]
    liq = cat(pc, "liquor")
    assert pc["window"]["days"] == 28 and liq["judged"] is True
    assert liq["target"] == 18.0 and liq["target_source"] == "owner" and liq["target_label"] == "your target"
    assert liq["tone"] == "warn" and liq["over_target_usd"] > 0
    assert "over_target:liquor" in keys(out["insights"])


def test_alcohol_target_set_by_admin_is_not_your_target(db_path):
    """R9: an admin's view-as save read as the owner's own target."""
    rid = _targets_fixture(db_path)
    with change_log.attributed(source="admin"):
        models.update_restaurant(rid, {"liquor_cost_target": 18.0}, db_path=db_path)
    r = models.get_restaurant(rid, db_path=db_path)
    t = thresholds.bar_target_for(r, "liquor")
    assert t["setter"] == "admin" and t["label"] == "the target Cavnar AI set"
    assert t["phrase"] == "the 18% target Cavnar AI set"
    out = bc.build(rid, db_path=db_path, today=date(2026, 10, 12))
    liq = cat(out["pour_cost"], "liquor")
    assert liq["target_label"] == "the target Cavnar AI set"
    ins = next(i for i in out["insights"] if i["key"] == "over_target:liquor")
    assert "your" not in ins["headline"] and "the 18% target Cavnar AI set" in ins["headline"]
    with change_log.attributed(source="owner"):
        models.update_restaurant(rid, {"liquor_cost_target": 19.0}, db_path=db_path)
    assert thresholds.bar_target_for(models.get_restaurant(rid, db_path=db_path), "liquor")["label"] == "your target"


@pytest.mark.parametrize("bad", ["abc", 0, -5, 200, 4.9, 60.5, float("nan"), True])
def test_update_restaurant_refuses_a_bar_target_outside_5_to_60(db_path, bad):
    rid = _rid(db_path)
    with pytest.raises(ValueError):
        models.update_restaurant(rid, {"liquor_cost_target": bad}, db_path=db_path)
    assert models.get_restaurant(rid, db_path=db_path).liquor_cost_target is None


def test_update_restaurant_takes_a_bar_target_from_5_to_60_and_clears_it(db_path):
    rid = _rid(db_path)
    models.update_restaurant(rid, {"wine_cost_target": "35", "bar_cost_target": 5}, db_path=db_path)
    r = models.get_restaurant(rid, db_path=db_path)
    assert r.wine_cost_target == 35.0 and r.bar_cost_target == 5.0
    models.update_restaurant(rid, {"wine_cost_target": "", "bar_cost_target": None}, db_path=db_path)
    r = models.get_restaurant(rid, db_path=db_path)
    assert r.wine_cost_target is None and r.bar_cost_target is None


def test_every_owner_target_field_has_a_module():
    """V20: Ask's target history mapped modules by position; zip stopped at
    four and the alcohol targets fell to the every-login default."""
    for f in models.OWNER_TARGET_FIELDS:
        assert f in thresholds.TARGET_MODULES, f
    for f in models.BAR_TARGET_FIELDS.values():
        assert thresholds.TARGET_MODULES[f] == "inventory"
    src = open(os.path.join(ROOT, "ask_cavnar_tools.py"), encoding="utf-8").read()
    body = src[src.index("def _read_target_history"):src.index("def _read_events(")]
    assert "zip(" not in body and "TARGET_MODULES" in body


def test_target_history_hides_alcohol_targets_without_food_cost(db_path, monkeypatch):
    import ask_cavnar_tools as tools
    monkeypatch.setattr(models, "DB_PATH", db_path)
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    rid = _rid(db_path, liquor_cost_target=18.0, module_inventory=1, module_labor=1)
    monkeypatch.setattr(tools, "_denied", lambda viewer: {"inventory"})
    out = tools._read_target_history(rid, _viewer=object())
    labels = {t["label"] for t in out["now"]}
    assert "Liquor cost target" not in labels and "Alcohol cost target" not in labels
    assert "Food cost target" not in labels and "Labor target" in labels
    monkeypatch.setattr(tools, "_denied", lambda viewer: set())
    labels = {t["label"] for t in tools._read_target_history(rid, _viewer=object())["now"]}
    assert "Liquor cost target" in labels


# ══ 7. dates ═════════════════════════════════════════════════════════════

def test_a_future_dated_invoice_is_held_for_a_check(db_path):
    """V35 / EDG-19: an invoice dated 10/2/27 became 'as of' and the latest
    price."""
    rid = _rid(db_path)
    _liquor_db(db_path, rid, start=date(2026, 9, 1), weeks=6)
    fi.ingest_file(rid, "typo.csv", (HEAD + _row("Breakthru Beverage", "TYPO", date(2027, 10, 2), "9031",
                                                  "TITOS VDK 1L", "CA", 1, 350.0)).encode(), db_path=db_path)
    _sales_db(db_path, rid, date(2026, 9, 1), 40, Liquor=200)
    out = bc.build(rid, db_path=db_path, today=T)
    assert out["as_of"] == "2026-10-06"
    assert out["held_future"][0]["invoice_number"] == "TYPO" and out["held_future"][0]["date_mdy"] == "10/2/27"
    assert not any(r.get("to") == 350.0 for r in out["price_watch"]["increases"])


def test_an_invoice_years_before_joining_is_held_for_a_check(db_path):
    """V35's other half: an invoice dated more than two years before the
    restaurant joined (a mis-typed year: 10/6/16 for 10/6/26) is held like a
    future date — never a month row, a price or the "from" of the invoices."""
    import bar.build as bb
    rid = _rid(db_path, created_at="2026-05-01T09:00:00")
    _liquor_db(db_path, rid, start=date(2026, 9, 1), weeks=6)
    fi.ingest_file(rid, "typo.csv", (HEAD + _row("Breakthru Beverage", "OLD", date(2016, 10, 6), "9031",
                                                  "TITOS VDK 1L", "CA", 1, 99.0)).encode(), db_path=db_path)
    _sales_db(db_path, rid, date(2026, 9, 1), 40, Liquor=200)
    out = bc.build(rid, db_path=db_path, today=T)
    assert [r["invoice_number"] for r in out["held_old"]] == ["OLD"]
    assert out["held_old"][0]["date_mdy"] == "10/6/16" and out["held_future"] == []
    assert out["invoices"]["from_mdy"] == "9/1/26"
    assert out["pour_cost"]["months"] and all(m["from"] >= "2026" for m in out["pour_cost"]["months"])
    events = out["price_watch"]["events"]
    assert not any(e.get("from") == 99.0 or e.get("to") == 99.0 for e in events)
    # The rule, not the fixture: the floor is two years before created_at,
    # and a restaurant whose joining day isn't known holds nothing for age.
    r = models.get_restaurant(rid, db_path=db_path)
    assert bb.oldest_believable(r) == (date(2026, 5, 1) - timedelta(days=bb.OLDEST_BEFORE_JOINING_DAYS)).isoformat()
    assert bb.oldest_believable(None) is None


def test_a_years_old_invoice_does_not_stretch_the_month_table():
    lines, sales = steady()
    lines.append(L("Breakthru", "2016-10-03", "liquor", 300))
    sales.setdefault("liquor", {})["2016-10-03"] = 200.0
    pc = pc_of(lines, sales)
    assert len(pc["months"]) <= bp.MONTH_ROWS


def test_a_biweekly_house_is_not_stale_on_day_eight_even_when_the_feed_says_a_week():
    """The feed's own stale flag is a fixed week; the judge allows the slowest
    active house's rhythm (W2/W4 hand-off, 10/15/26)."""
    from datetime import date, timedelta
    import bar.judge as j
    today = date(2026, 10, 15)
    lines = [{"invoice_date": (today - timedelta(days=8 + 14 * k)).isoformat(), "distributor": "Breakthru",
              "category": "liquor", "net": 1000.0, "kind": "item"} for k in range(8)]
    feed = {"invoices_through": (today - timedelta(days=8)).isoformat(), "stale_days": 8}
    f = j.freshness(lines, {}, today, feed=feed)
    assert f["stale"] is False and f["grace_days"] >= 14


def test_a_night_whose_drink_sales_were_not_sorted_keeps_the_window_unjudged():
    """W3/W4 hand-off (10/15/26): bar.sales leaves a gap night out of the
    per-day figures, which alone is 27 of 28 nights and would still pass the
    85% coverage rule — the named gap keeps the window from being judged."""
    lines, sales = steady()
    end = max(l["invoice_date"] for l in lines)
    gap = (date.fromisoformat(end) - timedelta(days=5)).isoformat()
    pc = pc_of(lines, sales, sales_gaps=(gap,))
    for c in ("beer", "liquor"):
        assert cat(pc, c)["judged"] is False and "sales_gap" in cat(pc, c)["reasons"]
        assert cat(pc, c)["tone"] is None
    assert pc["total"]["judged"] is False
    assert pc_of(lines, sales)["total"]["judged"] is True
