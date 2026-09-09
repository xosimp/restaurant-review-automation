"""The two savings figures owners are asked to trust, with no flat
assumptions left in them (Sep 7 2026).

Food cost "recoverable / month" used to be 65% of all logged waste. It is
now only the waste above each category's tolerance band, per item.

Labor "potential savings" is the gap above target over the synced period;
callers used to multiply it by 4.33 as if every period were one week. It
now carries per-week and per-month figures normalized by the calendar days
the data covers, and every consumer reads those."""
from types import SimpleNamespace

import inventory
import labor
import value_delivered


def _item(name, category, waste, order, cost, **k):
    d = {"item": name, "category": category, "par_level": 10, "current_stock": 8, "unit_cost": cost,
         "avg_daily_usage": 1, "last_order_qty": order, "waste_last_week": waste, "unit": "lb", "case_size": 1}
    d.update(k)
    return d


def test_recoverable_is_only_waste_above_the_category_band():
    # produce band is 28%: 40% waste → 12 of 40 points recoverable
    items = [_item("Lettuce", "produce", waste=4, order=10, cost=5.0)]
    a = inventory.analyse_inventory(items)
    weekly_waste = 4 * 5.0                       # $20
    expected_week = weekly_waste * (40 - 28) / 40  # $6
    assert a["total_waste_cost_week"] == 20.0
    assert a["recoverable_weekly"] == round(expected_week, 2)
    assert a["recoverable_monthly"] == round(expected_week * 52 / 12)
    assert a["annual_recoverable"] == round(expected_week * 52 / 12 * 12, 2)
    assert items[0]["recoverable_cost"] == 6.0 and items[0]["waste_tolerance_pct"] == 28


def test_waste_inside_the_band_recovers_nothing():
    items = [_item("Chicken", "protein", waste=1, order=10, cost=8.0),   # 10% < 15% band
             _item("Flour", "pantry", waste=2, order=10, cost=2.0)]      # 20% == band, not above
    a = inventory.analyse_inventory(items)
    assert a["total_waste_cost_week"] == 12.0
    assert a["recoverable_weekly"] == 0 and a["recoverable_monthly"] == 0
    assert all(i["recoverable_cost"] == 0 for i in items)


def test_recoverable_sums_per_item_and_never_uses_a_flat_share():
    items = [_item("Lettuce", "produce", waste=5, order=10, cost=4.0),   # 50% vs 28 → 22/50 of $20 = $8.80
             _item("Ribeye", "protein", waste=3, order=10, cost=20.0),   # 30% vs 15 → 15/30 of $60 = $30
             _item("Butter", "dairy", waste=1, order=20, cost=6.0)]      # 5% → nothing
    a = inventory.analyse_inventory(items)
    assert a["recoverable_weekly"] == round(8.8 + 30.0, 2)
    assert a["recoverable_monthly"] != round(a["monthly_waste_projection"] * 0.65)
    assert a["monthly_waste_projection"] == round(a["total_waste_cost_week"] * 52 / 12, 2)
    assert "tolerance band" in a["recoverable_basis"]


def test_no_order_history_means_nothing_is_claimed():
    items = [_item("Mystery", "produce", waste=4, order=0, cost=5.0)]
    a = inventory.analyse_inventory(items)
    assert a["recoverable_monthly"] == 0


def _shifts(days, daily_labor_hours, daily_sales, rate=20.0):
    out = []
    for i in range(days):
        d = "2026-08-%02d" % (1 + i)
        out.append({"date": d, "employee": "A", "role": "Server", "scheduled_hours": daily_labor_hours,
                    "actual_hours": daily_labor_hours, "hourly_rate": rate, "sales_that_day": daily_sales})
    return out


def test_labor_savings_are_normalized_by_the_period_covered():
    # 14 days, $400 labor a day on $1,000 sales = 40% vs a 30% target
    a14 = labor.analyse_shifts(_shifts(14, 20, 1000), hourly_rate=20.0, labor_target=30.0)
    a7 = labor.analyse_shifts(_shifts(7, 20, 1000), hourly_rate=20.0, labor_target=30.0)
    assert a14["period_days"] == 14 and a7["period_days"] == 7
    # whole-period gap doubles with the period; the weekly figure does not
    assert a14["potential_savings"] == 2 * a7["potential_savings"]
    assert a14["potential_savings_weekly"] == a7["potential_savings_weekly"]
    assert a14["potential_savings_weekly"] == round(a14["potential_savings"] / 14 * 7, 2)
    assert a14["potential_savings_monthly"] == round(a14["potential_savings_weekly"] * 52 / 12, 2)
    # the old 4.33-per-period shortcut would have said twice this for 14 days
    assert abs(a14["potential_savings"] * 4.33 - 2 * a14["potential_savings_monthly"]) < a14["potential_savings_monthly"] * 0.01


def test_labor_gap_covers_closed_days_in_the_span():
    # Shifts on 12 of 14 calendar days: the week still has 7 days in it.
    shifts = [s for s in _shifts(14, 20, 1000) if s["date"] not in ("2026-08-03", "2026-08-10")]
    a = labor.analyse_shifts(shifts, hourly_rate=20.0, labor_target=30.0)
    assert a["period_days"] == 14
    assert a["potential_savings_weekly"] == round(a["potential_savings"] / 14 * 7, 2)


def test_under_target_labor_has_no_savings():
    a = labor.analyse_shifts(_shifts(7, 10, 1000), hourly_rate=20.0, labor_target=30.0)  # 20% labor
    assert a["potential_savings"] == 0 and a["potential_savings_weekly"] == 0 and a["potential_savings_monthly"] == 0


def test_total_value_delivered_uses_the_normalized_figures(monkeypatch):
    monkeypatch.setattr(value_delivered, "get_restaurant", lambda rid, db_path=None: SimpleNamespace(
        module_reviews=1, module_labor=1, module_inventory=1, module_marketing=0))
    monkeypatch.setattr(value_delivered, "get_review_stats", lambda rid: {"responded": 3})
    monkeypatch.setattr(labor, "analyse_shifts_for_restaurant", lambda rid: {
        "is_live": True, "potential_savings": 4000.0, "potential_savings_weekly": 2000.0, "potential_savings_monthly": 8666.67})
    monkeypatch.setattr(inventory, "load_inventory_for_restaurant", lambda rid: ([], True))
    monkeypatch.setattr(inventory, "analyse_inventory", lambda items: {"recoverable_monthly": 150})
    total = value_delivered.compute_total_value_delivered(1)
    assert total == 3 * 5 + 8667 + 150
    # a sample (not live) labor set counts nothing
    monkeypatch.setattr(labor, "analyse_shifts_for_restaurant", lambda rid: {
        "is_live": False, "potential_savings": 4000.0, "potential_savings_monthly": 8666.67})
    assert value_delivered.compute_total_value_delivered(1) == 15 + 150


# ── labor savings cannot be conjured from missing sales ─────────────────────
#
# potential_savings is a gap to a PERCENTAGE of sales. When a Toast sync
# brought shifts across without sales — a real and common shape — the missing
# sales were treated as $0, so target_labor_cost was 0 and the "savings"
# became the entire payroll: two 8-hour shifts reported "$6,309/month in
# savings" beside "0% labor".

from labor import analyse_shifts


def _days(*sales_per_day, hours=8.0):
    return [{"date": f"2026-09-0{i+1}", "employee": "A", "role": "Server",
             "actual_hours": hours, "sales_that_day": s}
            for i, s in enumerate(sales_per_day)]


def test_no_sales_means_no_savings_not_the_whole_payroll():
    r = analyse_shifts(_days(0, 0), hourly_rate=26.0, labor_target=30.0)
    assert r["potential_savings"] == 0.0
    assert r["potential_savings_monthly"] == 0.0
    assert r["sales_data_missing"] is True
    assert len(r["days_missing_sales"]) == 2


def test_the_percentage_stays_a_number_when_sales_are_missing():
    """A dozen callers do arithmetic on this and iOS decodes it as a
    non-optional Double — None would break the Labor tab outright."""
    r = analyse_shifts(_days(0, 0), hourly_rate=26.0, labor_target=30.0)
    assert isinstance(r["overall_labor_pct"], (int, float))


def test_a_partial_sync_costs_only_the_days_it_can():
    """Labor from a day with no sales must not be charged against another
    day's sales — that inflates the ratio and invents a gap."""
    r = analyse_shifts(_days(2000, 0), hourly_rate=26.0, labor_target=30.0)
    assert r["overall_labor_pct"] == 10.4          # 208 / 2000, one day only
    assert r["potential_savings"] == 0.0
    assert r["days_missing_sales"] == ["2026-09-02"]
    assert r["sales_data_missing"] is False        # some sales did arrive


def test_a_real_overage_still_reports_savings():
    r = analyse_shifts(_days(300, 300), hourly_rate=26.0, labor_target=30.0)
    # 416 labor against 600 sales, target 30% = 180 -> 236 over
    assert r["potential_savings"] == 236.0
    assert r["potential_savings_monthly"] > 0
    assert r["sales_data_missing"] is False


def test_under_target_reports_nothing_rather_than_a_negative():
    r = analyse_shifts(_days(2000, 2000), hourly_rate=26.0, labor_target=30.0)
    assert r["potential_savings"] == 0.0


def test_full_sales_is_unchanged_by_the_partial_sync_handling():
    """The normal case must compute exactly as it always did."""
    r = analyse_shifts(_days(1000, 1000), hourly_rate=26.0, labor_target=30.0)
    assert r["total_sales"] == 2000
    assert r["overall_labor_pct"] == 20.8          # 416 / 2000
    assert r["days_missing_sales"] == []
