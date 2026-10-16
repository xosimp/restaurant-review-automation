"""Waste measured count to count from Back Office's submitted inventories
(backoffice_counts, 10/16/26): food used = on hand at the first count +
food bought between - on hand at the second; waste = that, less what the
recipes say the dishes sold should have used. One count is a stock value,
never a week of $0 waste on the trend."""
from datetime import timedelta

import backoffice_ap as bo
import backoffice_counts as bc
import backoffice_recipes as br
import waste_trend
from tests.test_backoffice_ap import COA, TODAY, _conn, _rid, db  # noqa: F401
from tests.test_backoffice_recipes import BOOK, _invoice, _sold


def _count(end, food, beer=500.0, status="Submitted"):
    half = round(food * 0.6, 2)
    return {"start": (end - timedelta(days=6)).strftime("%m/%d/%Y"), "end": end.strftime("%m/%d/%Y"), "status": status,
            "gls": [{"gl": "4011", "name": "PRODUCE INVENTORY", "amount": f"{half:.2f}"},
                    {"gl": "4031", "name": "MEAT INVENTORY", "amount": f"{food - half:.2f}"},
                    {"gl": "4081", "name": "NA BEV INVENTORY", "amount": "75.00"},
                    {"gl": "4321", "name": "BEER INVENTORY", "amount": f"{beer:.2f}"}],
            "items": [{"location": "WALK IN COOLER", "item": "PRODUCE ONIONS RED", "gl_name": "PRODUCE INVENTORY",
                       "unit": "Pound", "qty": "12.00", "total": "10.08"}]}


def _kitchen(db):
    """EJ's shape: the chart of accounts, $200 of food sales a night, a
    $2,000 produce invoice five days ago, the recipe book and its sales."""
    rid = _rid(db)
    bo.read_file(rid, "coa.csv", COA.encode(), db_path=db)
    c = _conn(db)
    for d in range(1, 30):
        c.execute("INSERT INTO dsr_metrics (restaurant_id, business_date, metric, value, status, source) "
                  "VALUES (?,?,?,?, 'ready', 'rpower')", (rid, (TODAY - timedelta(days=d)).isoformat(), "sales.cat:Food", 200.0))
    c.commit()
    c.close()
    bo.save_invoice_book(rid, {"invoices": [
        {"vendor": "GREENLEAF PRODUCE", "number": "77001", "date": (TODAY - timedelta(days=5)).isoformat(),
         "total": "$2,000.00", "items": [{"sku": "999001", "name": "PRODUCE LETTUCE", "gl": "4010 PRODUCE",
                                          "unit": "Case", "qty": "100.00", "amt": "20.00", "ext": "2000.00"}]}]},
        db_path=db)
    _invoice(db, rid)
    _sold(db, rid, "Brisket Sammy", 20, 18.0)
    br.load(rid, BOOK, db_path=db)
    return rid


def test_only_submitted_periods_are_stored_and_accounts_sort_by_their_names(db):
    rid = _rid(db)
    out = bc.save_counts(rid, {"inventory_counts": [_count(TODAY - timedelta(days=15), 1000.0),
                                                     _count(TODAY - timedelta(days=8), 1, status="Started")]}, db_path=db)
    assert out["periods"] == 1 and out["accounts"] == 4 and out["items"] == 1
    assert out["skipped"][0]["why"].startswith("status Started"), "a half-entered count is not a count"
    (c,) = bc.counts(rid, db_path=db)
    assert c["food"] == 1000.0, "beer and NA beverages are not the kitchen's"
    assert c["by_category"]["beer"] == 500.0 and c["by_category"]["na_beverage"] == 75.0
    again = bc.save_counts(rid, {"inventory_counts": [_count(TODAY - timedelta(days=15), 1200.0)]}, db_path=db)
    assert again["periods"] == 1 and bc.counts(rid, db_path=db)[0]["food"] == 1200.0, "read again replaces"


def test_waste_is_what_the_kitchen_used_beyond_what_the_recipes_say(db):
    rid = _kitchen(db)
    a, b = TODAY - timedelta(days=15), TODAY - timedelta(days=1)
    bc.save_counts(rid, {"inventory_counts": [_count(a, 1000.0)]}, db_path=db)
    assert bc.waste_periods(rid, db_path=db) == [], "one count measures nothing"
    bc.save_counts(rid, {"inventory_counts": [_count(b, 900.0)]}, db_path=db)
    (p,) = bc.waste_periods(rid, db_path=db)
    assert (p["start"], p["end"], p["days"]) == ((a + timedelta(days=1)).isoformat(), b.isoformat(), 14)
    assert p["bought"] == 2000.0 and p["used"] == 1000.0 + 2000.0 - 900.0
    assert p["food_sales"] == 14 * 200.0 and p["nights"] == 14
    # The sammy is the one fully priced dish: $5.08 of recipe on an $18 plate.
    assert p["recipe_pct"] == round(100 * (4.455 + 0.125 + 0.5) / 18.0, 1)
    assert p["expected"] == round(2800.0 * p["recipe_pct"] / 100, 2)
    assert p["waste"] == round(p["used"] - p["expected"], 2)
    assert p["rate"] == round(100 * p["waste"] / 2000.0, 1)


def test_a_period_short_of_sales_is_not_measured(db):
    rid = _kitchen(db)
    bc.save_counts(rid, {"inventory_counts": [_count(TODAY - timedelta(days=45), 1000.0),
                                               _count(TODAY - timedelta(days=1), 900.0)]}, db_path=db)
    (p,) = bc.waste_periods(rid, db_path=db)
    assert p["waste"] is None and "nights" in p["why_not"], "29 nights of sales over 44 days"


def test_the_trend_charts_measured_periods_and_food_cost_gets_every_count(db):
    rid = _kitchen(db)
    a, b = TODAY - timedelta(days=15), TODAY - timedelta(days=1)
    bc.save_counts(rid, {"inventory_counts": [_count(a, 1000.0)]}, db_path=db)
    weeks, total = waste_trend.load_waste_history(rid, db_path=db)
    assert weeks == [] and total == 0, "the first count is never a $0 week"
    (w,) = waste_trend.load_waste_history(rid, db_path=db, include_unmeasured=True)[0]
    assert w["inv_value"] == 1000.0 and w["waste"] is None
    out = waste_trend.build_waste_trend(rid, db_path=db, is_live=False)
    assert out["empty"]["unit"] == "counts submitted" and out["empty"]["weeks_have"] == 1
    assert "sample" not in out["empty"]["reason"], "a Back Office kitchen is not a sample pantry"
    bc.save_counts(rid, {"inventory_counts": [_count(b, 900.0)]}, db_path=db)
    weeks, total = waste_trend.load_waste_history(rid, db_path=db)
    assert [w["week_end"] for w in weeks] == [b.isoformat()] and total == 1
    assert weeks[0]["waste"] == bc.waste_periods(rid, db_path=db)[0]["waste"] and weeks[0]["purchased"] == 2000.0
    assert weeks[0]["start"] == (a + timedelta(days=1)).isoformat(), "the period the counts bracket, not a guessed week"
    assert len(waste_trend.load_waste_history(rid, db_path=db, include_unmeasured=True)[0]) == 2


def test_a_kitchen_without_back_office_keeps_the_sample_message(db):
    rid = _rid(db)
    out = waste_trend.build_waste_trend(rid, db_path=db, is_live=False)
    assert "sample pantry" in out["empty"]["reason"]


# ── Food Cost's read for a Back Office kitchen (backoffice_read) ──────────

class _R:
    def __init__(self, dow=None, wsd=0):
        self.fiscal_week_start_dow, self.week_start_day = dow, wsd


def test_the_read_covers_the_last_complete_fiscal_week():
    import backoffice_read as brd
    from datetime import date
    # EJ's week runs Wednesday to Tuesday; on Friday 10/16/26 the last whole one is 10/7 - 10/13.
    assert brd.fiscal_week(_R(2), date(2026, 10, 16)) == (date(2026, 10, 7), date(2026, 10, 13))
    assert brd.fiscal_week(_R(2), date(2026, 10, 14)) == (date(2026, 10, 7), date(2026, 10, 13)), "a Wednesday"
    assert brd.fiscal_week(_R(None, 0), date(2026, 10, 16)) == (date(2026, 10, 5), date(2026, 10, 11))


def test_the_read_states_measured_figures_and_never_example_data(db):
    import backoffice_read as brd
    rid = _kitchen(db)
    today = TODAY
    out = brd.food_read(rid, restaurant=_R(today.weekday()), today=today, db_path=db)
    s, e = today - timedelta(days=7), today - timedelta(days=1)
    assert (out["week_start"], out["week_end"]) == (s.isoformat(), e.isoformat())
    f = out["facts"]
    assert f["bought"] == 2000.0 and f["food_sales"] == 1400.0 and f["pct"] == round(100 * 2000 / 1400, 1)
    assert "$2,000 of food bought against $1,400 of food sales" in out["text"]
    assert "Back Office's recipes" in out["text"] and "example" not in out["text"].lower()
    assert "Weekly counts in Back Office" in out["text"], "no counts on file yet"
    assert brd.food_read(_rid(db), restaurant=_R(2), today=today, db_path=db) is None, "no Back Office food"


def test_the_page_steps_aside_for_back_office_recipes_and_prices():
    import os
    html = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates/dashboard.html")).read()
    assert 'data-bm-module="food_cost"' not in html, "How you compare left Food Cost (10/16/26)"
    assert '{% if _boh_recipes %} hidden{% endif %}' in html and '{% if _boh_prices %} hidden{% endif %}' in html
    assert "d.source!=='back_office'" in html, "a Back Office read carries no example-data caveat"
    assert "food_week.start_mdy" in html, "the read names the restaurant's own week"


# ── Food cost % for a Back Office kitchen (cogs._backoffice_components) ───

def test_food_cost_runs_count_to_count_on_back_office_invoices_and_food_sales(db):
    import cogs
    rid = _kitchen(db)
    out = cogs.build_food_cost_pct(rid, db_path=db, today=TODAY)
    assert out["ok"] is False and out["source"] == "back_office"
    assert out["unmeasured_line"] == "Comes with your second Back Office count"
    a, b = TODAY - timedelta(days=15), TODAY - timedelta(days=1)
    bc.save_counts(rid, {"inventory_counts": [_count(a, 1000.0)]}, db_path=db)
    assert cogs.build_food_cost_pct(rid, db_path=db, today=TODAY)["unmeasured_line"] == \
        "Comes with your next Back Office count"
    bc.save_counts(rid, {"inventory_counts": [_count(b, 900.0)]}, db_path=db)
    out = cogs.build_food_cost_pct(rid, db_path=db, today=TODAY)
    assert out["ok"] and (out["start"], out["end"]) == ((a + timedelta(days=1)).isoformat(), b.isoformat())
    assert (out["opening"], out["purchases"], out["closing"]) == (1000.0, 2000.0, 900.0)
    assert out["net_sales"] == 14 * 200.0, "food sales, never net sales with the bar in it"
    assert out["pct"] == round(2100.0 / 2800.0 * 100, 1) and out["window_days"] == 14


def test_a_kitchen_logging_deliveries_here_keeps_its_own_path(db):
    import cogs
    rid = _rid(db)
    assert cogs._backoffice_components(rid, TODAY, 28, db_path=db) is None, "no Back Office invoices"
