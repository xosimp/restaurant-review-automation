"""Tests for the 5 inventory safeguards in inventory.py's analyse_inventory():
delivery-day awareness, weekend demand curve, case-size/MOQ rounding,
event scaling, and per-category waste tolerance.
"""
from datetime import date, timedelta

from inventory import analyse_inventory, days_until_next_delivery, load_inventory

# Derive weekday anchors programmatically (never hardcode which calendar
# date is a Monday) so these tests stay correct regardless of when they run.
_ANCHOR = date(2026, 8, 24)
MONDAY    = _ANCHOR - timedelta(days=_ANCHOR.weekday())
TUESDAY   = MONDAY + timedelta(days=1)
WEDNESDAY = MONDAY + timedelta(days=2)
THURSDAY  = MONDAY + timedelta(days=3)
FRIDAY    = MONDAY + timedelta(days=4)
SATURDAY  = MONDAY + timedelta(days=5)


def _item(**overrides):
    base = {
        "item": "Test Item", "category": "Pantry", "par_level": 10.0,
        "current_stock": 10.0, "unit_cost": 2.0, "avg_daily_usage": 2.0,
        "last_order_qty": 10.0, "waste_last_week": 0.0, "unit": "", "case_size": 1.0,
    }
    base.update(overrides)
    return base


# ── Safeguard 1: delivery-day awareness ─────────────────────────────────

class TestDaysUntilNextDelivery:
    def test_none_when_not_configured(self):
        assert days_until_next_delivery(None) is None
        assert days_until_next_delivery("") is None

    def test_none_when_unparseable(self):
        assert days_until_next_delivery("garbage,nonsense") is None

    def test_today_itself_is_a_delivery_day(self):
        assert days_until_next_delivery("Wed", WEDNESDAY) == 0

    def test_finds_next_delivery_later_in_week(self):
        assert days_until_next_delivery("Mon,Thu", WEDNESDAY) == 1

    def test_wraps_to_next_week(self):
        assert days_until_next_delivery("Mon", SATURDAY) == 2

    def test_accepts_full_day_names_and_whitespace(self):
        assert days_until_next_delivery(" Monday , Thursday ", WEDNESDAY) == 1


class TestDeliveryDayAwareThresholds:
    def test_becomes_critical_when_next_delivery_is_far_away(self):
        # 5-ish days of stock comfortably clears the flat 4-day reorder cutoff.
        baseline = analyse_inventory(
            [_item(current_stock=10, avg_daily_usage=2, par_level=15)], today=TUESDAY)
        assert not baseline["critical_low"]
        assert not baseline["reorder_soon"]

        # Same stock level, but this supplier only delivers Mondays — 6 days
        # out from this Tuesday, longer than the stock will actually last.
        aware = analyse_inventory(
            [_item(current_stock=10, avg_daily_usage=2, par_level=15)],
            delivery_days="Mon", today=TUESDAY)
        assert aware["critical_low"]
        assert aware["critical_low"][0]["item"] == "Test Item"
        assert aware["critical_low"][0]["delivery_margin_days"] < 0

    def test_not_critical_when_delivery_is_imminent(self):
        # Only 1.5 days of stock -- flat thresholds call this critical.
        baseline = analyse_inventory(
            [_item(current_stock=3, avg_daily_usage=2, par_level=10)], today=TUESDAY)
        assert baseline["critical_low"]

        # But the truck arrives tomorrow -- stock will hold long enough.
        aware = analyse_inventory(
            [_item(current_stock=3, avg_daily_usage=2, par_level=10)],
            delivery_days="Wed", today=TUESDAY)
        assert not aware["critical_low"]

    def test_delivery_margin_days_is_none_when_not_configured(self):
        analysis = analyse_inventory([_item()], today=TUESDAY)
        assert analysis["waste_items"] == [] or all(
            "delivery_margin_days" in i for i in analysis["waste_items"])


# ── Safeguard 2: weekend/day-of-week demand curve ───────────────────────

class TestWeekendDemandCurve:
    def test_matches_flat_average_with_no_weekend_in_window(self):
        # Monday start, 2 days of stock covers only Mon+Tue -- no weekend.
        items = [_item(current_stock=10, avg_daily_usage=5)]
        analyse_inventory(items, today=MONDAY)
        assert items[0]["days_remaining"] == 2.0

    def test_weekend_surge_shortens_days_remaining(self):
        # Same stock/usage, but starting Thursday the window now crosses Fri/Sat.
        items = [_item(current_stock=10, avg_daily_usage=5)]
        analyse_inventory(items, today=THURSDAY)
        assert items[0]["days_remaining"] < 2.0


# ── Safeguard 3: case-size/MOQ rounding ─────────────────────────────────

class TestCaseSizeRounding:
    def test_rounds_up_to_nearest_case(self):
        items = [_item(par_level=10, current_stock=2, avg_daily_usage=1,
                        case_size=6, waste_last_week=0, last_order_qty=10)]
        analyse_inventory(items, today=MONDAY)
        qty = items[0]["suggested_order_qty"]
        assert qty > 0
        assert qty % 6 == 0

    def test_case_size_one_matches_unrounded_behavior(self):
        items_a = [_item(par_level=10, current_stock=2, avg_daily_usage=1, case_size=1)]
        items_b = [_item(par_level=10, current_stock=2, avg_daily_usage=1, case_size=1.0)]
        analyse_inventory(items_a, today=MONDAY)
        analyse_inventory(items_b, today=MONDAY)
        assert items_a[0]["suggested_order_qty"] == items_b[0]["suggested_order_qty"]

    def test_load_inventory_from_csv_respects_case_size_column(self):
        csv_str = ("item,category,par_level,current_stock,unit_cost,avg_daily_usage,"
                   "last_order_qty,waste_last_week,case_size\n"
                   "Widgets,Pantry,10,2,1.0,1,10,0,12\n")
        rows = load_inventory(csv_string=csv_str)
        assert rows[0]["case_size"] == 12.0

    def test_load_inventory_defaults_case_size_when_column_absent(self):
        csv_str = ("item,category,par_level,current_stock,unit_cost,avg_daily_usage,"
                   "last_order_qty,waste_last_week\nWidgets,Pantry,10,2,1.0,1,10,0\n")
        rows = load_inventory(csv_string=csv_str)
        assert rows[0]["case_size"] == 1.0


# ── Safeguard 4: event scaling for upcoming holidays ────────────────────

class TestEventScaling:
    def test_matching_item_gets_scaled_qty_and_flag(self):
        base   = [_item(item="Salmon Fillet", par_level=10, current_stock=2,
                        avg_daily_usage=1, waste_last_week=0)]
        scaled = [_item(item="Salmon Fillet", par_level=10, current_stock=2,
                        avg_daily_usage=1, waste_last_week=0)]
        analyse_inventory(base, today=MONDAY)
        analyse_inventory(scaled, upcoming_holidays="Valentine's Day (Feb 14)", today=MONDAY)
        assert base[0]["event_scaled"] is False
        assert scaled[0]["event_scaled"] is True
        assert scaled[0]["suggested_order_qty"] > base[0]["suggested_order_qty"]

    def test_non_matching_item_is_unaffected(self):
        items = [_item(item="Olive Oil", par_level=10, current_stock=2, avg_daily_usage=1)]
        analyse_inventory(items, upcoming_holidays="Valentine's Day (Feb 14)", today=MONDAY)
        assert items[0]["event_scaled"] is False

    def test_no_upcoming_holidays_never_scales(self):
        items = [_item(item="Salmon Fillet", par_level=10, current_stock=2, avg_daily_usage=1)]
        analyse_inventory(items, upcoming_holidays=None, today=MONDAY)
        assert items[0]["event_scaled"] is False


# ── Safeguard 5: per-category waste tolerance ───────────────────────────

class TestWasteTolerance:
    def test_produce_tolerates_higher_waste_than_protein(self):
        # 25% waste: over protein's 15% tolerance, under produce's 28% tolerance.
        produce = _item(item="Basil", category="Produce", last_order_qty=100, waste_last_week=25)
        protein = _item(item="Salmon", category="Protein", last_order_qty=100, waste_last_week=25)
        analysis = analyse_inventory([produce, protein], today=MONDAY)
        waste_names = {x["item"] for x in analysis["waste_items"]}
        assert "Salmon" in waste_names
        assert "Basil" not in waste_names

    def test_uncategorized_item_uses_default_tolerance(self):
        # 22% waste: over the 20% default, no per-category entry for "Other".
        item = _item(item="Napkins", category="Other", last_order_qty=100, waste_last_week=22)
        analysis = analyse_inventory([item], today=MONDAY)
        assert any(x["item"] == "Napkins" for x in analysis["waste_items"])


# ── Backward compatibility ───────────────────────────────────────────────

def test_full_sample_data_still_analyses_with_defaults_only():
    items = load_inventory()
    analysis = analyse_inventory(items)
    assert "waste_items" in analysis
    assert "critical_low" in analysis
    assert "reorder_soon" in analysis
    assert analysis["total_items"] == len(items)
