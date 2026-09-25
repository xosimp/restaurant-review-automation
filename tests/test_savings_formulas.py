"""The two savings figures owners are asked to trust, with no flat
assumptions left in them (Sep 7 2026).

Food cost "recoverable / month" used to be 65% of all logged waste. It is
now only the waste above each category's tolerance band, per item.

Labor "potential savings" is the gap above target over the synced period;
callers used to multiply it by 4.33 as if every period were one week. It
now carries per-week and per-month figures normalized by the calendar days
the data covers, and every consumer reads those."""
from datetime import date, timedelta
from types import SimpleNamespace

import inventory
import labor
import models
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


def _shifts(days, daily_labor_hours, daily_sales, rate=20.0, crew=5):
    """The day's hours spread across a crew, not piled on one person.

    This used to book every hour of the day to a single employee, which at
    20h/day is 140h a week — so once overtime was priced at 1.5x the way
    federal law requires, the "gap doubles with the period" assertion below
    stopped holding for a reason that had nothing to do with period
    normalization. A realistic crew keeps everyone under 40 and leaves the
    arithmetic these tests are actually about unchanged."""
    out = []
    per_person = daily_labor_hours / float(crew)
    for i in range(days):
        d = "2026-08-%02d" % (1 + i)
        for c in range(crew):
            out.append({"date": d, "employee": f"E{c}", "role": "Server",
                        "scheduled_hours": per_person, "actual_hours": per_person,
                        "hourly_rate": rate, "sales_that_day": daily_sales})
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


def test_the_headline_figure_is_measured_results_not_the_gap_to_target(monkeypatch):
    """The ROI audit's central finding, pinned.

    "Total Value Delivered" used to be reviews-responded x $5, PLUS labor's
    potential_savings_monthly, PLUS recoverable food cost. The last two are
    gaps ABOVE target: money the restaurant is still losing. Counting them as
    delivered ran the arithmetic backwards - fixing your scheduling LOWERED
    your Total Value Delivered, and the worst-run restaurant on the platform
    showed the biggest number.

    The headline is now only what outcomes.py actually measured.
    """
    # The owner's own blended rate: a gap-to-target in dollars needs a real
    # labor cost, not the $26/hr default (Benchmarking audit #14).
    monkeypatch.setattr(value_delivered, "get_restaurant", lambda rid, db_path=None: SimpleNamespace(
        module_reviews=1, module_labor=1, module_inventory=1, module_marketing=0, hourly_rate=19.0))
    monkeypatch.setattr(value_delivered, "get_review_stats", lambda rid: {"responded": 3})
    # A current labor period: an opportunity on an undated or stale period
    # is withheld now (DH1-4).
    _lend = (date.today() - timedelta(days=1)).isoformat()
    monkeypatch.setattr(labor, "analyse_shifts_for_restaurant", lambda rid: {
        "is_live": True, "potential_savings": 4000.0, "potential_savings_weekly": 2000.0,
        "potential_savings_monthly": 8666.67,
        "date_range": {"start": (date.today() - timedelta(days=28)).isoformat(), "end": _lend, "days": 28}})
    monkeypatch.setattr(inventory, "analysis_for",
                        lambda rid, items=None, is_live=None: ([], True, {"recoverable_monthly": 150}))

    import outcomes
    # Nothing measured yet: an $8,817/month gap is not value delivered.
    monkeypatch.setattr(outcomes, "total_value", lambda rid, db_path=None, denied_modules=None: {
        "monthly": 0.0, "annual": 0.0, "wins": 0, "evaluated": 0, "in_flight": 2,
        "unmeasurable": 0, "no_clear_change": 0, "by_module": {}, "caveat": "c"})
    monkeypatch.setattr(outcomes, "best_ever", lambda rid, db_path=None, denied_modules=None: None)
    assert value_delivered.compute_total_value_delivered(1) == 0

    # ...and it is still reported, under its own name, as opportunity.
    opp = value_delivered.opportunity(1)
    assert opp["monthly"] == round(8666.67 + 150, 2)

    # One measured win is what the headline counts.
    monkeypatch.setattr(outcomes, "total_value", lambda rid, db_path=None, denied_modules=None: {
        "monthly": 410.0, "annual": 4920.0, "wins": 1, "evaluated": 3, "in_flight": 1,
        "unmeasurable": 1, "no_clear_change": 1, "by_module": {"labor": 410.0}, "caveat": "c"})
    assert value_delivered.compute_total_value_delivered(1) == 410


def test_cost_avoidance_counts_work_that_happened_and_carries_its_rate(monkeypatch):
    """The marketing figure used to be months_since_signup x $1,500, fired by
    a single generated post ever - so one post in month one billed $18,000 of
    "value" by month twelve. It now counts the months content was produced,
    and every rate ships with the number."""
    monkeypatch.setattr(value_delivered, "get_restaurant", lambda rid, db_path=None: SimpleNamespace(
        module_reviews=1, module_labor=0, module_inventory=0, module_marketing=1))
    monkeypatch.setattr(value_delivered, "get_review_stats", lambda rid: {"responded": 4})

    class _Conn:
        def execute(self, sql, args=()):
            # two months: one with 20 pieces (capped at a month), one with 3
            rows = [("2026-08", 20), ("2026-09", 3)] if "GROUP BY" in sql else [[9]]
            return SimpleNamespace(fetchone=lambda: rows[0], fetchall=lambda: rows)

        def close(self):
            pass

    monkeypatch.setattr(models, "get_conn", lambda db_path=None: _Conn())
    out = value_delivered.avoided(1)
    by = {i["key"]: i for i in out["items"]}
    # NS3 M1: the rate counts pieces, never more than a month's fee a month
    assert by["content"]["dollars"] == round(value_delivered.AGENCY_MONTHLY + 3 * value_delivered.AGENCY_PER_PIECE, 2)
    assert by["content"]["pieces"] == 23
    assert by["replies"]["dollars"] == 4 * value_delivered.REPLY_RATE
    # Every item states the assumption it rests on.
    for item in out["items"]:
        assert item["rate"] and item["basis"]


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
    assert r["sales_data_missing"] is False
    # Two days is not a week. The period gap above is real and is still
    # reported; a monthly rate built from it would not be.
    assert r["period_too_short_to_project"] is True
    assert r["potential_savings_monthly"] == 0.0


def test_a_real_overage_over_a_full_week_does_report_a_monthly_figure():
    r = analyse_shifts(_days(*([300] * 8)), hourly_rate=26.0, labor_target=30.0)
    assert r["potential_savings"] > 0
    assert r["period_too_short_to_project"] is False
    assert r["potential_savings_monthly"] > 0


def test_under_target_reports_nothing_rather_than_a_negative():
    r = analyse_shifts(_days(2000, 2000), hourly_rate=26.0, labor_target=30.0)
    assert r["potential_savings"] == 0.0


def test_full_sales_is_unchanged_by_the_partial_sync_handling():
    """The normal case must compute exactly as it always did."""
    r = analyse_shifts(_days(1000, 1000), hourly_rate=26.0, labor_target=30.0)
    assert r["total_sales"] == 2000
    assert r["overall_labor_pct"] == 20.8          # 416 / 2000
    assert r["days_missing_sales"] == []
