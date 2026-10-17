"""Fix round A2 of the Food Cost + Bar audit (10/16/26): Back Office counts,
plate costing and the Back Office read. Finding IDs are the registers' in
/Users/simp/fc_bar_audit_1016 (01-fc-calc, 03-match-ingest, 05-ai,
07-sec-scale)."""
import json
import time
from datetime import date, timedelta

import pytest

import backoffice_ap as bo
import backoffice_counts as bc
import backoffice_recipes as br
import waste_trend
from tests.test_backoffice_ap import COA, TODAY, _conn, _rid, db  # noqa: F401
from tests.test_backoffice_counts import _count, _kitchen
from tests.test_backoffice_recipes import BOOK, _invoice, _v

D = date(2026, 10, 16)


def _price(item, invoices, as_of=None):
    return br.item_price(item, invoices, today=D, as_of=as_of)


def _food_nights(db, rid, first, last, value=200.0, skip_weekdays=()):
    c = _conn(db)
    d = first
    while d <= last:
        if d.weekday() not in skip_weekdays:
            c.execute("INSERT INTO dsr_metrics (restaurant_id, business_date, metric, value, status, source) "
                      "VALUES (?,?,?,?, 'ready', 'rpower')", (rid, d.isoformat(), "sales.cat:Food", value))
        d += timedelta(days=1)
    c.commit()
    c.close()


def _sell(db, rid, name, first, last, per_night, price, tag="x", skip_weekdays=()):
    c = _conn(db)
    d, i = first, 0
    while d <= last:
        if d.weekday() not in skip_weekdays:
            for _ in range(per_night):
                c.execute("INSERT INTO pos_ticket_lines (restaurant_id, provider, line_id, business_date, item_name, "
                          "kind, qty, sales) VALUES (?, 'rpower', ?, ?, ?, 'sale', 1, ?)",
                          (rid, f"{tag}-{name}-{i}", d.isoformat(), name, price))
                i += 1
        d += timedelta(days=1)
    c.commit()
    c.close()


def _bare_kitchen(db, sales_from, sales_to, per_night=10, skip_weekdays=()):
    """The chart of accounts, the recipe book, GFS's brisket line, $200 of
    food a night and `per_night` sammies at $18 - no counts yet."""
    rid = _rid(db)
    bo.read_file(rid, "coa.csv", COA.encode(), db_path=db)
    bo.save_invoice_book(rid, {"invoices": [
        {"vendor": "GREENLEAF PRODUCE", "number": "77001", "date": (TODAY - timedelta(days=5)).isoformat(),
         "total": "$2,000.00", "items": [{"sku": "999001", "name": "PRODUCE LETTUCE", "gl": "4010 PRODUCE",
                                          "unit": "Case", "qty": "100.00", "amt": "20.00", "ext": "2000.00"}]}]},
        db_path=db)
    _invoice(db, rid)
    _food_nights(db, rid, sales_from, sales_to, skip_weekdays=skip_weekdays)
    _sell(db, rid, "Brisket Sammy", sales_from, sales_to, per_night, 18.0, skip_weekdays=skip_weekdays)
    br.load(rid, BOOK, db_path=db)
    return rid


# ── SCALE-01: costed once, kept, read ───────────────────────────────────

def test_scale_01_a_year_of_counts_is_costed_once_kept_and_read_fast(db, monkeypatch):
    rid = _bare_kitchen(db, TODAY - timedelta(days=380), TODAY - timedelta(days=1))
    bc.save_counts(rid, {"inventory_counts": [_count(TODAY - timedelta(days=1 + 7 * k), 1000.0 + 500 * k)
                                               for k in range(52)]}, db_path=db)
    calls = {"book": 0, "prices": 0, "pos": 0}
    real = (br.book, br._invoice_prices, br._pos_sales)

    def wrap(name, fn):
        def inner(*a, **k):
            calls[name] += 1
            return fn(*a, **k)
        return inner
    monkeypatch.setattr(br, "book", wrap("book", real[0]))
    monkeypatch.setattr(br, "_invoice_prices", wrap("prices", real[1]))
    monkeypatch.setattr(br, "_pos_sales", wrap("pos", real[2]))
    t = time.perf_counter()
    periods = bc.waste_periods(rid, db_path=db)
    cold = time.perf_counter() - t
    assert len(periods) == 51 and sum(1 for p in periods if p["recipe_pct"] is not None) == 51
    assert calls == {"book": 1, "prices": 1, "pos": 1}, calls
    assert cold < 3.0, cold
    # Kept: the next read recomputes nothing, and the trend is quick.
    assert bc.waste_periods(rid, db_path=db) == periods and calls == {"book": 1, "prices": 1, "pos": 1}
    t = time.perf_counter()
    weeks, total = waste_trend.load_waste_history(rid, 8, db_path=db)
    warm = time.perf_counter() - t
    assert calls["book"] == 1 and len(weeks) == 8 and total == 51
    assert warm < 0.2, warm
    c = _conn(db)
    assert c.execute("SELECT COUNT(*) FROM bo_waste_periods WHERE restaurant_id=?", (rid,)).fetchone()[0] == 51
    c.close()
    # An input moving (a count read again) recomputes, once.
    bc.save_counts(rid, {"inventory_counts": [_count(TODAY - timedelta(days=1), 990.0)]}, db_path=db)
    bc.waste_periods(rid, db_path=db)
    bc.waste_periods(rid, db_path=db)
    assert calls["book"] == 2
    # So does a new night of food sales and a recipe book loaded again.
    _food_nights(db, rid, TODAY, TODAY)
    bc.waste_periods(rid, db_path=db)
    assert calls["book"] == 3
    br.load(rid, BOOK, db_path=db)
    stamp = bc.inputs_stamp(rid, db_path=db)
    time.sleep(1.05)
    br.load(rid, BOOK, db_path=db)
    assert bc.inputs_stamp(rid, db_path=db) != stamp


def test_scale_01_since_and_until_bound_the_periods_read(db):
    rid = _kitchen(db)
    bc.save_counts(rid, {"inventory_counts": [_count(TODAY - timedelta(days=d), 1000.0) for d in (22, 15, 8, 1)]},
                   db_path=db)
    ends = [p["end"] for p in bc.waste_periods(rid, db_path=db)]
    assert len(ends) == 3
    assert [p["end"] for p in bc.waste_periods(rid, db_path=db, since=ends[1])] == ends[1:]
    assert [p["end"] for p in bc.waste_periods(rid, db_path=db, until=ends[1])] == ends[:2]
    assert [p["end"] for p in bc.waste_periods(rid, db_path=db, limit=1)] == ends[-1:]


# ── FC-CALC-06: a count with no food account is no kitchen count ───────

def test_fc_calc_06_a_bar_only_count_never_pairs_as_zero_food(db):
    rid = _kitchen(db)
    bar_only = _count(TODAY - timedelta(days=8), 0)
    bar_only["gls"] = [g for g in bar_only["gls"] if "BEER" in g["name"]]
    bc.save_counts(rid, {"inventory_counts": [_count(TODAY - timedelta(days=15), 1000.0), bar_only,
                                               _count(TODAY - timedelta(days=1), 900.0)]}, db_path=db)
    every = bc.counts(rid, db_path=db, food_only=False)
    assert [c["has_food"] for c in every] == [True, False, True] and every[1]["food"] is None
    assert len(bc.counts(rid, db_path=db)) == 2, "food counts only, by default"
    (p,) = bc.waste_periods(rid, db_path=db)
    assert p["opening"] == 1000.0 and p["closing"] == 900.0 and p["days"] == 14, "paired across the bar-only week"


# ── FC-CALC-07: coverage, and prices as of the period ──────────────────

def test_fc_calc_07_one_dish_never_stands_for_all_food_sales(db):
    """The old fixture: 20 sammies, $360 of $2,800 of food sales (13%)."""
    rid = _bare_kitchen(db, TODAY - timedelta(days=29), TODAY - timedelta(days=1), per_night=0)
    _sell(db, rid, "Brisket Sammy", TODAY - timedelta(days=14), TODAY - timedelta(days=1), 1, 18.0, tag="few")
    bc.save_counts(rid, {"inventory_counts": [_count(TODAY - timedelta(days=15), 1000.0),
                                               _count(TODAY - timedelta(days=1), 900.0)]}, db_path=db)
    (p,) = bc.waste_periods(rid, db_path=db)
    assert p["waste"] is None and p["expected"] is None and p["recipe_pct"] is not None
    assert p["coverage_pct"] == 9.0 and "9% of the period's food sales" in p["why_not"]


def test_fc_calc_07_a_period_is_priced_as_of_its_end(db):
    """Brisket billed $5.94/lb on 10/1 and $11.88/lb on 10/14: a 9/1-10/7
    period costs it at $5.94, today's plate at $11.88."""
    rid = _rid(db)
    bo.save_lines(rid, "GORDON FOOD SERVICE", "1", [
        {"item_code": "197761", "description": "BEEF BRISKET", "gl": "4030", "purchase_unit": "Case", "qty": 128.6,
         "unit_amount": 5.94, "ext_amount": 763.88}], invoice_date="2026-10-01", db_path=db)
    bo.save_lines(rid, "GORDON FOOD SERVICE", "2", [
        {"item_code": "197761", "description": "BEEF BRISKET", "gl": "4030", "purchase_unit": "Case", "qty": 110.2,
         "unit_amount": 11.88, "ext_amount": 1309.18}], invoice_date="2026-10-14", db_path=db)
    inv = br._invoice_prices(rid, db_path=db)
    item = BOOK["items"][0]
    assert abs(_price(item, inv)["per_base"] - 11.88) < 1e-9
    assert abs(_price(item, inv, as_of="2026-10-07")["per_base"] - 5.94) < 1e-9
    early = _price(item, inv, as_of="2026-09-01")
    assert abs(early["per_base"] - 5.94) < 1e-9 and "later invoice" in early["flag"], "the nearest price, said so"


# ── FC-CALC-13: a longer period is put per week ────────────────────────

def test_fc_calc_13_a_two_week_period_charts_as_its_weekly_rate(db):
    rid = _kitchen(db)
    bc.save_counts(rid, {"inventory_counts": [_count(TODAY - timedelta(days=15), 1000.0),
                                               _count(TODAY - timedelta(days=1), 900.0)]}, db_path=db)
    (p,) = bc.waste_periods(rid, db_path=db)
    w = bc.waste_weeks(rid, db_path=db)[-1]
    assert p["days"] == 14 and w["waste"] == round(p["waste"] / 2, 2) and w["period_waste"] == p["waste"]
    assert w["purchased"] == round(p["bought"] / 2, 2) and w["rate"] == p["rate"], "a ratio is the same either way"


# ── FC-CALC-16: closed nights are not missing nights ───────────────────

def test_fc_calc_16_a_restaurant_closed_sunday_and_monday_is_measured(db):
    rid = _bare_kitchen(db, TODAY - timedelta(days=60), TODAY - timedelta(days=1), skip_weekdays=(6, 0))
    bc.save_counts(rid, {"inventory_counts": [_count(TODAY - timedelta(days=29), 1000.0),
                                               _count(TODAY - timedelta(days=1), 900.0)]}, db_path=db)
    (p,) = bc.waste_periods(rid, db_path=db)
    assert p["nights"] == p["trading_nights"] and p["nights"] < 0.8 * p["days"]
    assert "nights" not in (p.get("why_not") or ""), p.get("why_not")


# ── FC-CALC-19: negative waste is a problem, never a figure ────────────

def test_fc_calc_19_recipes_above_what_was_used_withhold_waste(db):
    rid = _kitchen(db)
    # 1,000 on hand + 2,000 bought - 2,950 on hand: $50 used, against ~$789 the recipes call for.
    bc.save_counts(rid, {"inventory_counts": [_count(TODAY - timedelta(days=15), 1000.0),
                                               _count(TODAY - timedelta(days=1), 2950.0)]}, db_path=db)
    (p,) = bc.waste_periods(rid, db_path=db)
    assert p["waste"] is None and p["shortfall"] > 0 and "a count, an invoice or a recipe is off" in p["why_not"]
    assert waste_trend.load_waste_history(rid, db_path=db)[0] == [], "never charted"


# ── FC-CALC-08 / ING-07: catch weight decided for the item ─────────────

CHEDDAR = {"name": "CHEESE CHEDDAR", "gl": "4050 DAIRY", "vendors": [
    {"vendor": "GORDON FOOD SERVICE", "sku": "555", "purchase_unit": "Case", "pack_unit": "Pound", "per_pack": "20",
     "price": "90.00", "last_purchased": "10/01/2026"}]}


def _line(code, qty, amt, day, vendor="gordon food service", ext=None):
    return {"item_code": code, "purchase_unit": "Case", "qty": qty, "unit_amount": amt, "invoice_date": day,
            "vendor_key": vendor, "ext_amount": ext if ext is not None else (qty * amt if amt else 0)}


def test_fc_calc_08_a_weight_on_any_line_makes_the_item_catch_weight():
    """20.4 lb in September, 20.00 lb in October: October is $4.50/lb too."""
    inv = br._Invoices([_line("555", 20.4, 4.50, "2026-09-20"), _line("555", 20.0, 4.60, "2026-10-10")])
    p = _price(CHEDDAR, inv)
    assert abs(p["per_base"] - 4.60) < 1e-9 and p["flag"] == "billed by weight"


def test_fc_calc_08_a_price_far_from_back_offices_is_refused():
    """A case of 20 lb listed at $90 ($4.50/lb) invoiced at $900: refused,
    never $45/lb or a quiet $4.50 x 10."""
    item = dict(CHEDDAR, gl="4010 PRODUCE")
    inv = br._Invoices([_line("555", 1, 290.0, "2026-10-10")])
    p = _price(item, inv)
    assert p["source"] == "back_office", "the invoice refused, Back Office's price used"
    assert "3x apart" in p["flag"] or "refused" in p["flag"]


def test_fc_calc_08_every_weight_item_has_a_floor():
    flour = {"name": "FLOUR", "gl": "4050 DRY GOODS", "vendors": [
        {"vendor": "GFS", "sku": "1", "purchase_unit": "Bag", "pack_unit": "Pound", "per_pack": "50", "price": "5.00"}]}
    assert "less than any food costs" in _price(flour, {})["error"], "ten cents a pound"
    flour["vendors"][0]["price"] = "20.40"
    assert abs(_price(flour, {})["per_base"] - 0.408) < 1e-9, "EJ's real flour, 41 cents a pound"


# ── FC-CALC-09 / ING-08: never a $0 or negative price ──────────────────

def test_fc_calc_09_a_line_with_no_unit_amount_is_its_extended_amount_over_quantity():
    inv = br._Invoices([_line("555", 20.4, None, "2026-10-10", ext=91.80)])
    assert abs(_price(CHEDDAR, inv)["per_base"] - 4.50) < 1e-9
    for bad in (_line("555", 0, 90.0, "2026-10-12"), _line("555", 2, 0.0, "2026-10-12"),
                _line("555", -1, -90.0, "2026-10-12")):
        p = _price(CHEDDAR, br._Invoices([bad]))
        assert p["source"] == "back_office" and p["per_base"] > 0, bad
    assert "not a price" in br._sane(0.0, "Pound", "4050 DAIRY") and br._sane(-1, "Each", None)


# ── FC-CALC-10: Each prices and plates over 70% ─────────────────────────

def test_fc_calc_10_an_each_price_is_checked_against_back_office():
    bun = {"name": "BUN", "gl": "4050 DRY GOODS", "vendors": [
        {"vendor": "GORDON FOOD SERVICE", "sku": "5", "purchase_unit": "Case", "pack_unit": "Each", "per_pack": "96",
         "price": "30.00"}]}
    inv = br._Invoices([dict(_line("5", 1, 30.0, "2026-10-10"), purchase_unit="Each")])
    p = _price(bun, inv)
    assert p["source"] == "back_office" and abs(p["per_base"] - 30 / 96) < 1e-9, "$30 a bun refused"


def test_fc_calc_10_a_plate_over_seventy_percent_is_suspect_and_left_out(db):
    rid = _rid(db)
    b = json.loads(json.dumps(BOOK))
    b["items"].append({"name": "BUN SETUP WRONG", "gl": "4050 DRY GOODS",
                       "vendors": [_v(sku="96", purchase_unit="Each", price="$30.00")]})
    b["recipes"].append({"name": "BIG BUN BURGER", "group": "MENU ITEM>PLATES", "batch_yield": {},
                         "ingredients": [{"name": "BUN SETUP WRONG", "qty": "1.00", "unit": "Each",
                                          "item": len(b["items"]) - 1}]})
    b["pos_names"]["BIG BUN BURGER"] = ["Big Bun"]
    br.load(rid, b, db_path=db)
    _invoice(db, rid)
    _sell(db, rid, "Big Bun", TODAY - timedelta(days=10), TODAY - timedelta(days=1), 1, 12.0)
    _sell(db, rid, "Brisket Sammy", TODAY - timedelta(days=10), TODAY - timedelta(days=1), 1, 18.0)
    out = br.plates(rid, today=TODAY, db_path=db)
    big = next(p for p in out["plates"] if p["name"] == "BIG BUN BURGER")
    assert big["cost_pct"] == 250.0 and "set up wrong" in big["suspect"]
    sammy = next(p for p in out["plates"] if p["name"] == "MELTY BRISKET SAMMY")
    assert out["summary"]["cost_pct"] == sammy["cost_pct"]


# ── ING-06 / FC-CALC-21: one supplier's number, one supplier's price ────

def test_ing_06_vendor_matching():
    assert br._same_vendor("sysco", "sysco chicago") and br._same_vendor("gfs", "gordon food service")
    assert not br._same_vendor("gfs food store", "gordon food service")
    assert not br._same_vendor("chicago beverage", "chicago produce"), "one shared first word is not the vendor"
    inv = br._Invoices([_line("0012345", 1, 39.26, "2026-10-10", vendor="gordon food service"),
                        _line("12345", 1, 48.0, "2026-10-01", vendor="sysco chicago")])
    assert len(inv.lines_for("SYSCO", "12345")) == 1 and inv.lines_for("SYSCO", "12345")[0]["unit_amount"] == 48.0
    assert inv.lines_for(None, "12345") == [], "two suppliers bill it: a row naming none takes neither"


# ── ING-19: one POS item, one recipe; names matched loosely ─────────────

def test_ing_19_a_pos_item_mapped_twice_is_refused_and_names_match_loosely(db):
    b = json.loads(json.dumps(BOOK))
    b["pos_names"]["SMASHBURGER- EJ'S STYLE"] = ["  brisket   SAMMY "]
    with pytest.raises(ValueError, match="mapped to both"):
        br.validate(b)
    rid = _rid(db)
    b = json.loads(json.dumps(BOOK))
    b["pos_names"]["MELTY BRISKET SAMMY"] = ["BRISKET SAMMY"]
    br.load(rid, b, db_path=db)
    _invoice(db, rid)
    _sell(db, rid, "Brisket  Sammy ", TODAY - timedelta(days=5), TODAY - timedelta(days=1), 2, 18.0)
    out = br.plates(rid, today=TODAY, db_path=db)
    sammy = next(p for p in out["plates"] if p["name"] == "MELTY BRISKET SAMMY")
    assert sammy["units"] == 10, "case and spacing never zero a dish"
    assert out["summary"]["unsold_names"] == ["EJ's Smash Burger"], "a mapped name that sold nothing is named"


# ── ING-17: counts categorised by GL number; a gap is caught ───────────

def test_ing_17_an_unnamed_account_is_food_through_the_chart_of_accounts(db):
    rid = _kitchen(db)
    coa = COA + "Purchase Inventory,Income and Expense Statement,7101,4031,4031 MEAT INVENTORY,True,7101\n"
    bo.read_file(rid, "coa2.csv", coa.encode(), db_path=db)
    mid = _count(TODAY - timedelta(days=8), 800.0)
    for g in mid["gls"]:
        if g["gl"] == "4031":
            g["name"] = None
    bc.save_counts(rid, {"inventory_counts": [_count(TODAY - timedelta(days=15), 1000.0), mid,
                                               _count(TODAY - timedelta(days=1), 700.0)]}, db_path=db)
    cs = bc.counts(rid, db_path=db)
    assert cs[1]["food"] == 800.0 and cs[1]["gl_gap"] is None, "the meat stays food"


def test_ing_17_a_count_missing_an_account_its_neighbours_carry_is_not_measured(db):
    rid = _kitchen(db)
    mid = _count(TODAY - timedelta(days=8), 800.0)
    mid["gls"] = [dict(g, name=None) if g["gl"] == "4031" else g for g in mid["gls"]]
    bc.save_counts(rid, {"inventory_counts": [_count(TODAY - timedelta(days=15), 1000.0), mid,
                                               _count(TODAY - timedelta(days=1), 700.0)]}, db_path=db)
    cs = bc.counts(rid, db_path=db)
    assert cs[1]["gl_gap"] == ["MEAT INVENTORY"] and cs[0]["gl_gap"] is None and cs[2]["gl_gap"] is None
    ps = bc.waste_periods(rid, db_path=db)
    assert all(p["waste"] is None and "MEAT INVENTORY" in p["why_not"] for p in ps)


def test_ing_17_the_count_reader_keeps_the_gl_number():
    import os
    js = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "scripts/backoffice_read_counts.js")).read()
    assert r"^(\d{3,6})(?:\s*[-–:]?\s*(.*))?$" in js and '[name$="Number"]' in js


# ── AI-12: a week whose invoices are not all in ─────────────────────────

class _R:
    fiscal_week_start_dow, week_start_day = None, 0

    def __init__(self, dow):
        self.fiscal_week_start_dow = dow


def test_ai_12_a_lagging_week_leads_with_the_lag_and_never_compares(db):
    import backoffice_read as brd
    rid = _kitchen(db)
    # A week before with very different purchases, so a comparison would show.
    bo.save_invoice_book(rid, {"invoices": [
        {"vendor": "GREENLEAF PRODUCE", "number": "77000", "date": (TODAY - timedelta(days=10)).isoformat(),
         "total": "$300.00", "items": [{"sku": "999001", "name": "PRODUCE LETTUCE", "gl": "4010 PRODUCE",
                                        "unit": "Case", "qty": "15.00", "amt": "20.00", "ext": "300.00"}]}]},
        db_path=db)
    out = brd.food_read(rid, restaurant=_R(TODAY.weekday()), today=TODAY, db_path=db)
    assert out["lead"].startswith("Invoices are on file through") and "so far" in out["lead"]
    assert "down from" not in out["lead"] and "up from" not in out["lead"]
    assert out["facts"]["invoices_lag"] is True


def test_fc_calc_07_the_read_never_sets_one_dishes_recipe_against_all_sales(db):
    import backoffice_read as brd
    rid = _bare_kitchen(db, TODAY - timedelta(days=29), TODAY - timedelta(days=1), per_night=1)
    out = brd.food_read(rid, restaurant=_R(TODAY.weekday()), today=TODAY, db_path=db)
    labels = [p["label"] for p in out["points"]]
    assert "Not in a recipe" not in labels and out["facts"]["recipe_coverage_pct"] == 9.0
    assert "too few" in next(p["text"] for p in out["points"] if p["label"] == "By recipe")


# ── SCALE-12: the read's cache moves with a count ───────────────────────

def test_scale_12_the_back_office_read_cache_moves_with_counts_and_recipes(db):
    import client_api
    rid = _kitchen(db)
    keys = []
    real = client_api._cache_get
    client_api_get = lambda k: keys.append(k) or real(k)  # noqa: E731
    try:
        client_api._cache_get = client_api_get
        client_api.food_read_backoffice(rid)
        bc.save_counts(rid, {"inventory_counts": [_count(TODAY - timedelta(days=8), 900.0)]}, db_path=db)
        client_api.food_read_backoffice(rid)
    finally:
        client_api._cache_get = real
    assert len(keys) == 2 and keys[0] != keys[1]


# ── SCALE-11: a failure is reported, not read as "no sales" ─────────────

def test_scale_11_a_failing_pos_read_is_captured(db, monkeypatch):
    import ops
    seen = []
    monkeypatch.setattr(ops, "capture", lambda e, **k: seen.append(k.get("job")))

    class _Boom:
        def execute(self, *a, **k):
            raise RuntimeError("translation miss")

        def close(self):
            pass
    monkeypatch.setattr(br, "get_conn", lambda *a, **k: _Boom())
    assert br._pos_sales(1, ["x"], "2026-10-01", "2026-10-02") == {}
    assert seen == ["backoffice_recipes._pos_sales"]


# ── FC-CALC-24: the restaurant's own date ───────────────────────────────

def test_fc_calc_24_plates_default_to_the_restaurants_date(db, monkeypatch):
    import models
    rid = _rid(db)
    br.load(rid, BOOK, db_path=db)
    monkeypatch.setattr(models, "_restaurant_today", lambda r: date(2026, 10, 10))
    out = br.plates(rid, db_path=db)
    assert out["window"]["to"] == "2026-10-09"


def test_fc_calc_07_the_read_names_the_coverage_beside_counted_waste(db):
    import backoffice_read as brd
    rid = _kitchen(db)
    bc.save_counts(rid, {"inventory_counts": [_count(TODAY - timedelta(days=8), 1000.0),
                                               _count(TODAY - timedelta(days=1), 900.0)]}, db_path=db)
    out = brd.food_read(rid, restaurant=_R(TODAY.weekday()), today=TODAY, db_path=db)
    beyond = next(p["text"] for p in out["points"] if p["label"] == "Beyond recipes")
    assert "on dishes that are 90.0% of food sales" in beyond and out["facts"]["recipe_coverage_pct"] == 90.0
