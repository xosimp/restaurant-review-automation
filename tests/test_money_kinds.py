"""Workstream M (9/24/26): every money figure says what kind it is, and no
surface calls an opportunity, a projection or a budget money saved.

Each test replays a probe from the "never say" audit (scratchpad ns3/:
labor/, food/, dsr/, sales/, avoided_probe.py) that returned the wrong
figure or the wrong word before the fix. The rule behind all of them is
CLAUDE.md's: value delivered is only what was measured — delivered,
avoided, surfaced and opportunity are never summed, and an opportunity is
never rendered as delivered value.
"""
import os
import re
import types
from datetime import date, timedelta

import pytest

import labor
import metrics

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def _shifts(ndays, start="2026-09-01", staff=6, hrs=6, sales=2000, no_sales_from=None):
    out = []
    d0 = date.fromisoformat(start)
    for i in range(ndays):
        d = (d0 + timedelta(days=i)).isoformat()
        s = 0 if (no_sales_from and d >= no_sales_from) else sales
        for e in range(staff):
            out.append({"date": d, "day": "", "employee": f"E{i}_{e}", "role": "Server",
                        "scheduled_hours": hrs, "actual_hours": hrs, "sales_that_day": s})
    return out


# ── labor ────────────────────────────────────────────────────────────────────

def test_the_projection_floor_counts_days_with_data_not_the_calendar_span():
    """NS3 labor #9 / R12: two shift days eight calendar days apart projected
    "$576/mo"; a 7-day span with two days of sales projected "$658/mo"."""
    spread = _shifts(1) + _shifts(1, start="2026-09-08")
    a = labor.analyse_shifts(spread, hourly_rate=16, labor_target=25)
    assert a["period_days"] == 8 and a["data_days"] == 2
    assert a["period_too_short_to_project"] is True
    assert a["potential_savings_monthly"] == 0 and a["potential_savings"] > 0   # the period gap still stands
    two_with_sales = _shifts(7, no_sales_from="2026-09-03")
    a = labor.analyse_shifts(two_with_sales, hourly_rate=16, labor_target=25)
    assert a["data_days"] == 2 and a["potential_savings_monthly"] == 0
    # a real week still projects, at one month definition
    a = labor.analyse_shifts(_shifts(7), hourly_rate=16, labor_target=25)
    assert a["potential_savings_monthly"] == round(a["potential_savings_weekly"] * metrics.WEEKS_PER_MONTH, 2) > 0


def test_labor_shown_beside_sales_covers_the_same_days():
    """NS3 H4: "Labor $8,160 on $20,000 in sales" displayed 29.1% — the
    labor counted every day, the sales only days with sales (40.8%)."""
    a = labor.analyse_shifts(_shifts(14, no_sales_from="2026-09-11"), hourly_rate=16, labor_target=25)
    assert a["costed_labor"] < a["total_labor_cost"]
    assert abs(a["costed_labor"] / a["total_sales"] * 100 - a["overall_labor_pct"]) <= 0.1
    # every display that pairs labor with sales reads costed_labor
    assert "a.get('costed_labor'" in _src("ask_cavnar.py")
    assert "analysis.get('costed_labor'" in _src("labor.py")
    assert "labor.get('costed_labor'" in _src("home_brief.py")


def test_the_monthly_gap_chip_is_the_if_optimized_tile():
    """NS3 H4 / R11 probe: the chip read $6,771 beside a $1,765 tile, and a
    restaurant under target on its sales days got a $3,686 gap."""
    a = labor.analyse_shifts(_shifts(14, no_sales_from="2026-09-11"), hourly_rate=16, labor_target=25)
    g = labor.calculate_monthly_gap(a)
    assert g["over_target"] and abs(g["monthly_gap"] - a["potential_savings_monthly"]) <= 1
    under = labor.analyse_shifts(_shifts(14, hrs=5, no_sales_from="2026-09-11"), hourly_rate=16, labor_target=25)
    g = labor.calculate_monthly_gap(under)
    assert g["over_target"] is False and g["monthly_gap"] == 0
    assert g["kind"] == "opportunity"


def test_every_labor_dollar_field_carries_its_kind():
    a = labor.analyse_shifts(_shifts(7), hourly_rate=16, labor_target=25)
    k = a["money_kinds"]
    assert k["potential_savings_monthly"] == "opportunity" and k["total_sales"] == "measured"
    assert k["total_labor_cost"] == k["costed_labor"] == k["overtime_premium"] == "estimate"


def test_the_sample_week_carries_no_labor_dollars_on_web_or_ios(monkeypatch):
    """NS3 C3 probe: the sample week showed "If optimized / mo $12,770",
    "Per year $153,240" and "Overtime premium $2,565" to a new account."""
    a = labor.analyse_shifts(labor.load_shifts(), hourly_rate=26.0, labor_target=30.0)
    a["is_live"] = False
    b = labor.savings_breakdown(a)
    assert b["dollars_withheld"] == "sample"
    assert all(b[k] == 0 for k in ("labor_monthly", "labor_annual", "labor_overtime",
                                   "labor_vs_industry_monthly", "labor_vs_industry_annual"))
    import mobile_api
    monkeypatch.setattr(mobile_api, "analyse_shifts_for_restaurant", lambda rid: a, raising=False)
    monkeypatch.setattr(labor, "analyse_shifts_for_restaurant", lambda rid, **k: a)
    monkeypatch.setattr(mobile_api, "get_restaurant", lambda rid: types.SimpleNamespace(
        labor_target_pct=30.0, hourly_rate=26.0, timezone="America/Chicago"))
    monkeypatch.setattr(mobile_api, "_staff_constraints_index", lambda rid: {})
    p, _ = mobile_api._do_mobile_labor(1)
    assert p["potential_savings"] == 0 and p["savings_breakdown"]["labor_monthly"] == 0
    assert p["savings_breakdown"]["labor_overtime"] == 0 and p["costed_labor"] is None
    # the web reads the same function (hosted_dashboard used its own loop)
    web = _src("hosted_dashboard.py")
    assert "savings_breakdown(labor" in web and "* _hourly_rate * 0.5" not in web


def test_web_and_ios_show_one_overtime_premium():
    """NS3 M9 probe: cooks at $18 and a manager at $32 — labor.py $306, the
    web tile $351 (flat hourly_rate)."""
    rows = []
    for i in range(7):
        d = (date(2026, 9, 7) + timedelta(days=i)).isoformat()
        for emp, role in (("Cook A", "Cook"), ("Cook B", "Cook"), ("Mgr", "Manager")):
            rows.append({"date": d, "day": "", "employee": emp, "role": role, "scheduled_hours": 7,
                         "actual_hours": 7, "sales_that_day": 4000})
    a = labor.analyse_shifts(rows, hourly_rate=22.0, labor_target=30,
                             role_rates={"Cook": 18.0, "Manager": 32.0, "_default": 22.0})
    a["is_live"] = True
    b = labor.savings_breakdown(a)
    assert b["labor_overtime"] == round(a["overtime_premium"]) == 306
    assert b["kinds"]["labor_overtime"] == "estimate" and b["overtime_period_days"] == a["period_days"]
    assert b["kinds"]["labor_monthly"] == "opportunity" and b["kinds"]["labor_vs_industry_monthly"] == "benchmark"


def test_the_mobile_labor_gap_is_gated_on_sample_data(monkeypatch):
    """NS3 labor #4: /mobile/api/labor/gap returned the sample week's gap raw
    while its web twin gated it."""
    import mobile_api
    from flask import Flask
    a = labor.analyse_shifts(labor.load_shifts(), hourly_rate=26.0, labor_target=30.0)
    a["is_live"] = False
    monkeypatch.setattr(labor, "analyse_shifts_for_restaurant", lambda rid, **k: a)
    app = Flask(__name__)
    with app.test_request_context("/mobile/api/labor/gap"):
        body = mobile_api.mobile_labor_gap.__wrapped__({"restaurant_id": 1}).get_json()
    assert body["monthly_gap"] == 0 and body["projectable"] is False and body["is_live"] is False


def test_the_labor_note_prompt_calls_the_gap_an_opportunity(monkeypatch):
    """NS3 labor #2 probe: the prompt fed "Estimated monthly savings with
    optimized scheduling" and the model wrote "you saved $2,305 a month"."""
    a = labor.analyse_shifts(_shifts(8), hourly_rate=16, labor_target=25)
    a["is_live"] = True
    cap = {}

    def fake(client, **kw):
        cap["prompt"] = kw["messages"][0]["content"]
        return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text="Sam, labor ran 28.8%.")],
                                     stop_reason="end_turn")
    monkeypatch.setattr(labor, "create_with_retry", fake)
    monkeypatch.setattr(labor, "get_client", lambda *a, **k: None)
    labor.get_claude_insights(a, restaurant_name="Somewhere", owner_name="Sam")
    p = cap["prompt"]
    assert "Opportunity (gap above target, not money saved)" in p
    assert "Estimated monthly savings" not in p and "never \"saved\"" in p
    assert f"${a['costed_labor']:,.0f} on ${a['total_sales']:,.0f} in sales" in p


def test_asks_labor_context_states_a_short_periods_gap_never_zero_savings(monkeypatch):
    """NS3 labor #10 probe: a 5-day period read "Estimated monthly savings
    available from optimized scheduling: $0" over a real $840 gap."""
    import ask_cavnar
    rows = _shifts(5, hrs=8)
    a = labor.analyse_shifts(rows, hourly_rate=16, labor_target=30)
    a["is_live"] = True
    monkeypatch.setattr(labor, "analyse_shifts_for_restaurant", lambda rid, **k: a)
    monkeypatch.setattr(ask_cavnar, "_recent_days_lines", lambda rid: [])
    ctx = ask_cavnar._labor_context(1)
    assert "savings" not in ctx.lower().replace("not money saved", "")
    assert f"${a['potential_savings']:,.0f} over this 5-day period" in ctx
    assert "Opportunity (gap above target, not money saved)" in ctx


def test_the_home_labor_card_says_what_its_figure_covers():
    """NS3 M10: "$975/week recoverable by trimming the overstaffed days" when
    those days carried $600 — the figure is the whole schedule's gap."""
    src = _src("home_brief.py")
    assert "recoverable by trimming the overstaffed days" not in src
    assert "above target across the whole schedule — an opportunity, not money saved" in src


# ── food ─────────────────────────────────────────────────────────────────────

_DRIVERS = [
    {"kind": "menu", "label": "Salmon Plate runs at 41% food cost", "dollars_monthly": 400.0,
     "item": "Salmon Plate", "ingredients": ["Salmon", "Lemon"]},
    {"kind": "waste", "label": "Salmon waste above tolerance", "dollars_monthly": 300.0, "item": "Salmon"},
    {"kind": "price", "label": "Lemon price up 30%", "dollars_monthly": 50.0, "item": "Lemon"},
]


def test_home_at_stake_is_the_food_cost_cards_total():
    """NS3 H3 / R11 probe: Home $750/mo, the Food Cost card $400/month."""
    import home_brief
    import food_cost_intelligence as fci
    assert home_brief.at_stake_monthly(_DRIVERS) == fci.deduplicated_total(_DRIVERS)["total"] == 400.0


def test_driver_totals_are_stated_per_kind_never_blended():
    """NS3 H3 probe5: waste, a price rise, a menu gap and a supplier spread
    summed into one "measured" figure."""
    import food_cost_intelligence as fci
    drivers = [
        {"kind": "waste", "label": "Salmon waste", "dollars_monthly": 182.0, "item": "Salmon",
         "confidence": "low", "difficulty": "low", "evidence": "e", "if_ignored": "i"},
        {"kind": "price", "label": "Beef price up 18%", "dollars_monthly": 540.0, "item": "Beef",
         "confidence": "medium", "difficulty": "medium", "evidence": "e", "if_ignored": "i"},
        {"kind": "sourcing", "label": "Butter cheaper", "dollars_monthly": 60.0, "item": "Butter",
         "confidence": "medium", "difficulty": "medium", "evidence": "e", "if_ignored": "i"},
    ]
    d = fci.deduplicated_total(drivers)
    assert d["by_kind"] == {"opportunity": 242.0, "estimate": 540.0}
    assert sum(d["by_kind"].values()) == d["total"]
    block = fci._drivers_block({"drivers": drivers, "total_monthly": 782.0, "total_monthly_deduplicated": 782.0,
                                "degraded_sources": []})
    assert "Combined:" not in block and "$782" not in block
    assert "$242/month of opportunity" in block and "$540/month of estimated cost" in block
    assert "NEVER add these together" in block


def test_business_intelligence_never_calls_the_food_or_labor_line_computed():
    import business_intelligence as bi
    data = {"food_cost": {"brief": {"money_involved": {"monthly_at_stake": 800.0,
                                                        "totals_by_kind": {"opportunity": 300.0, "estimate": 500.0}}}},
            "labor": {"is_live": True, "potential_savings_monthly": 900.0, "labor_target": 30, "period_days": 14},
            "reviews": {}}
    m = bi.money_at_stake(1, data=data)
    kinds = {l["module"]: l["claim_kind"] for l in m["ranked"]}
    assert kinds == {"food_cost": "opportunity", "labor": "opportunity"}
    assert "measured cost drivers" not in m["total_note"] and "none of them is money saved" in m["total_note"]


def test_the_food_prompt_never_calls_a_per_order_difference_saved():
    """NS3 M6 probe: "$168.00 saved by ordering 8 ..." came back as
    "$168/month"."""
    import inventory

    def it(name, cat, waste, order, cost):
        return {"item": name, "category": cat, "par_level": 10, "current_stock": 8, "unit_cost": cost,
                "avg_daily_usage": 1, "last_order_qty": order, "waste_last_week": waste, "unit": "lb", "case_size": 1}
    a = inventory.analyse_inventory([it("Salmon", "protein", 6, 20, 14.0), it("Lettuce", "produce", 5, 10, 4.0)])
    block = inventory._supported_savings_block(a)
    assert "saved by ordering" not in block and "less per order" in block and "not a monthly figure" in block
    src = _src("inventory.py")
    assert "Recoverable with better ordering" not in src and "per order, low effort" in src


def test_the_waste_alert_counts_every_flagged_item(db_path, monkeypatch):
    """NS3 M8 probe4: nine items, $450 of waste — the alert said "$345 of
    waste across 6 items" (the display slice), and fed $345 to surfaced."""
    import inventory
    import models
    import notify
    from models import Restaurant, create_restaurant
    rid = create_restaurant(Restaurant(name="Waste Co", owner_email="w@x.test"), db_path=db_path)
    models.update_restaurant(rid, {"alert_food_waste": 1}, db_path=db_path)
    items = [{"item": f"Item{i}", "waste_cost": 50.0} for i in range(9)]
    analysis = {"waste_items": items[:6], "waste_items_total": 450.0, "waste_items_count": 9}
    monkeypatch.setattr(inventory, "analysis_for", lambda rid_: ([{"item": "x"}], True, analysis))
    monkeypatch.setattr(notify, "_waste_alert_worsened", lambda *a, **k: True)
    fired = []
    monkeypatch.setattr(notify, "raise_alert", lambda rid_, t, sms, subj, lines=None, **k: fired.append(
        (t, sms, lines, k.get("value"))))
    notify.check_extra_daily_alerts(db_path=db_path)
    t, sms, lines, value = next(f for f in fired if f[0] == "food_waste")
    assert "$450 of waste" in sms and "across 9 items" in lines[0] and value == 450.0


def test_the_waste_trend_weekly_and_annual_figures_agree():
    """NS3 M7 probe6: "$50 a week under ... roughly $260 a year" ($50 x 52 is
    $2,600) — the latest week beside the 4-week average's year."""
    import waste_trend as wt
    vals = [100, 100, 100, 40]
    weeks = [{"waste": v, "label": f"9/{i + 1}/26", "start_label": f"8/{i + 1}/26",
              "week_end": f"2026-09-0{i + 1}", "top_items": []} for i, v in enumerate(vals)]
    s = wt.waste_trend_stats(weeks, target_weekly=90.0)
    line = next(o["text"] for o in wt.waste_trend_observations(s, weeks) if "under the" in o["text"])
    weekly = int(re.search(r"averaging \$(\d+) a week", line).group(1))
    yearly = int(re.search(r"about \$([\d,]+) a year", line).group(1).replace(",", ""))
    assert weekly * 52 == yearly and "projection" in line
    assert s["money_kinds"]["savings_if_at_target"] == "opportunity"


def test_reprice_says_below_cost_only_when_the_price_is_below_cost():
    """NS3 M14: every reprice read "priced below its new cost"."""
    import action_queue
    ok = action_queue.reprice_title({"dish": "Burger", "sell_price": 14.0, "plate_cost": 5.2,
                                     "food_cost_pct_now": 37.1})
    assert "below its new cost" not in ok and "37.1%" in ok
    loss = action_queue.reprice_title({"dish": "Steak", "sell_price": 20.0, "plate_cost": 22.0})
    assert "priced below its new cost" in loss


def test_money_code_uses_one_month_definition():
    """NS3 L5: the month was 52/12, 4.33, x30 and 30.44 in money code."""
    pat = re.compile(r"4\.33|\*\s*30\b|30\.0\s*/|52\.0\s*/\s*12\.0")
    for f in ("labor.py", "food_cost_intelligence.py", "thresholds.py", "menu_intelligence.py",
              "inventory_ledger.py", "home_brief.py"):
        code = [l.split("#", 1)[0] for l in _src(f).splitlines()]
        hits = [l.strip() for l in code if pat.search(l)]
        assert not hits, (f, hits)


# ── reports and value ────────────────────────────────────────────────────────

def test_avoided_content_is_priced_per_piece(db_path):
    """NS3 M1 avoided_probe: three captions, one per month, read "$4,500
    you'd otherwise have paid for"."""
    import models
    import value_delivered as vd
    from models import Restaurant, create_restaurant
    rid = create_restaurant(Restaurant(name="Probe", owner_email="o@example.com"), db_path=db_path)
    models.update_restaurant(rid, {"module_marketing": 1}, db_path=db_path)
    c = models.get_conn(db_path)
    for m in ("2026-07-03", "2026-08-14", "2026-09-20"):
        c.execute("INSERT INTO marketing_content_log (restaurant_id, content_type, topic, created_at) VALUES (?,?,?,?)",
                  (rid, "instagram_caption", "Taco Tuesday", m + " 10:00:00"))
    c.commit()
    c.close()
    item = [i for i in vd.avoided(rid, db_path=db_path)["items"] if i["key"] == "content"][0]
    assert item["dollars"] == 3 * vd.AGENCY_PER_PIECE < 3 * vd.AGENCY_MONTHLY
    assert "a piece" in item["rate"]


def test_milestones_and_the_win_push_never_call_an_estimate_measured():
    """NS1 H4, M7 / NS3 L1: "measured, not an estimate" and "about
    $X/month, measured" — the dollars are metrics.monthly_dollars, which
    says "always an estimate"."""
    assert "measured, not an" not in _src("milestones.py")
    assert "/month, measured\")" not in _src("strategy_jobs.py")
    assert "an estimate from the measured move" in _src("strategy_jobs.py")


# ── sales audit and pricing ─────────────────────────────────────────────────

def test_a_note_insight_needs_its_figures_in_the_note():
    """NS3 C4 probe: "You are losing $4,000/month on labor because the GM is
    on the floor." from a note that said no figure."""
    import sales_audit_notes_ai as notes_ai
    notes = [{"section": "labor", "source": "audit", "text": "GM works the floor most nights.", "in_report": True},
             {"section": "food", "source": "audit", "text": "Chef orders by feel; waste was $600 last week.",
              "in_report": True}]
    parsed = {"insights": [
        {"note": 0, "category": "labor", "effect": "context", "report_safe": True,
         "text": "You are losing $4,000/month on labor because the GM is on the floor."},
        {"note": 1, "category": "food", "effect": "context", "report_safe": True,
         "text": "Ordering by feel left $600 of waste last week."}],
        "caveats": ["Labor overspend is roughly $48,000 a year.", "Waste was $600 last week."]}
    ins, _sg, cav = notes_ai._sanitize(parsed, notes, {})
    assert [i["note"] for i in ins] == [1]
    assert cav == ["Waste was $600 last week."]


def test_only_a_ticked_note_reaches_the_report():
    """NS3 C4: apply_notes appended an insight to the prospect's copy from a
    note with audit_in_report False."""
    import sales_audit_engine as engine
    cats = {"labor": {"status": "ok", "confidence": "high", "calc": {"assumptions": []}}}
    read = {"insights": [{"category": "labor", "effect": "context", "text": "The GM is on the floor.",
                          "report_safe": True, "source": "audit", "in_report": False}]}
    engine.apply_notes(cats, read)
    assert cats["labor"]["calc"]["assumptions"] == []


def test_billing_reads_its_suffix_from_the_stripe_interval():
    """NS3 H7: an annual subscription read "$11,990/mo"."""
    import pricing
    assert pricing.billing_amount_label(11990, "year") == "$11,990/yr"
    assert pricing.billing_amount_label(349, "month") == "$349/mo"
    sub = {"items": types.SimpleNamespace(data=[types.SimpleNamespace(
        price=types.SimpleNamespace(recurring={"interval": "year", "interval_count": 1}))])}
    assert pricing.subscription_interval(sub) == ("year", 1)
    for f in ("client_api.py", "mobile_api.py"):
        assert "amount:,.0f}/mo" not in _src(f)


def test_no_prospect_page_teaches_the_summed_value_model():
    """NS3 H7: the public site showed "Total value delivered $18,240" and
    "Saves $1,200-2,400/mo"; the cheat sheet taught the retired sum."""
    site = _src("public", "index.html")
    assert "Total value delivered" not in site and "Saves $" not in site and "Recovers $" not in site
    assert "never added together" in site
    sheet = _src("sales_audit_cheatsheet.py")
    assert "Total Value Delivered on Home is the sum" not in sheet
    assert "What is costing the most today" not in _src("templates", "audit_report.html")
