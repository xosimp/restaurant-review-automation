"""The lead's after-merge fixes for the Food Cost + Bar audit (10/16/26):
the items no single workstream owned."""
import backoffice_recipes as br
from tests.test_backoffice_ap import db  # noqa: F401


# ── FC-CALC-10: over 70% — or over twice the target, when that is higher ──

def test_fc_calc_10_the_high_flag_is_twice_the_target_when_that_is_higher(monkeypatch):
    import cogs
    monkeypatch.setattr(cogs, "dish_reference", lambda r: {"pct": 40.0})
    assert br.suspect_high(1) == 80.0, "a 40% kitchen's honest 75% plate is no setup error"
    monkeypatch.setattr(cogs, "dish_reference", lambda r: {"pct": 28.0})
    assert br.suspect_high(1) == br.SUSPECT_HIGH_PCT
    monkeypatch.setattr(cogs, "dish_reference", lambda r: None)
    assert br.suspect_high(1) == br.SUSPECT_HIGH_PCT


def test_fc_calc_10_build_judges_against_the_threshold_it_is_given():
    class _C:
        def recipe(self, name):
            return {"cost": 7.5, "missing": [], "lines": []}
    bk = {"pos_names": {"STEAK": ["Steak"]}, "pos_notes": {}}
    menu = [{"name": "STEAK", "group": "MENU ITEM>PLATES"}]
    sold = {br._pos_key("Steak"): (1, 10.0)}
    out, summary = br._build(bk, _C(), sold, menu)
    assert out[0]["cost_pct"] == 75.0 and out[0].get("suspect")
    out, summary = br._build(bk, _C(), sold, menu, high=80.0)
    assert not out[0].get("suspect") and summary["cost_pct"] == 75.0


# ── ING-17 in cogs: a count not read whole is never an edge ──────────────

def _bo_kitchen(monkeypatch, counts):
    import backoffice_ap as bo
    import backoffice_counts as bc
    monkeypatch.setattr(bo, "feed_state", lambda rid, db_path=None: {"invoices_through": "2026-10-14"})
    monkeypatch.setattr(bc, "counts", lambda rid, db_path=None: counts)
    monkeypatch.setattr(bo, "entries", lambda *a, **k: [])


def _c(end, gap=None):
    return {"end": end, "food": 1000.0, "has_food": True, "by_category": {"food": 1000.0}, "gl_gap": gap}


def test_a_closing_count_missing_an_account_is_refused(db_path, monkeypatch):
    import cogs
    from datetime import date
    _bo_kitchen(monkeypatch, [_c("2026-10-07"), _c("2026-10-14", ["5010 Produce"])])
    out = cogs._backoffice_components(1, date(2026, 10, 14), 7, db_path)
    assert out["closing"] is None and "5010 Produce" in out["missing"][0]["why"]


def test_an_opening_count_missing_an_account_is_refused(db_path, monkeypatch):
    import cogs
    from datetime import date
    _bo_kitchen(monkeypatch, [_c("2026-10-07", ["5010 Produce"]), _c("2026-10-14")])
    out = cogs._backoffice_components(1, date(2026, 10, 14), 7, db_path)
    assert out["opening"] is None and "not read whole" in out["missing"][0]["why"]


# ── FC-CALC-03 in the weekly read: a week's invoices over three nights ───

def test_fc_calc_03_the_weekly_read_withholds_a_week_its_sales_do_not_cover(db):
    from datetime import timedelta
    import backoffice_ap as bo
    import backoffice_read as brd
    from tests.test_backoffice_ap import TODAY, _rid
    from tests.test_fix_fcbar_a2 import _R, _food_nights
    rid = _rid(db)
    start, end = brd.fiscal_week(_R(TODAY.weekday()), TODAY)
    # Every night for ten weeks before the week, then three of its seven.
    _food_nights(db, rid, start - timedelta(days=70), start - timedelta(days=1))
    _food_nights(db, rid, start, start + timedelta(days=2))
    bo.save_invoice_book(rid, {"invoices": [
        {"vendor": "GREENLEAF PRODUCE", "number": "88001", "date": (start + timedelta(days=4)).isoformat(),
         "total": "$700.00", "items": [{"sku": "999001", "name": "PRODUCE LETTUCE", "gl": "4010 PRODUCE",
                                        "unit": "Case", "qty": "35.00", "amt": "20.00", "ext": "700.00"}]}]},
        db_path=db)
    out = brd.food_read(rid, restaurant=_R(TODAY.weekday()), today=TODAY, db_path=db)
    assert "pct" not in out["facts"] and "3 of the 7 days you trade" in out["text"]


# ── AI-08: a present-tense move against the fact's direction is X1 ───────

def test_ai_08_waste_is_climbing_when_the_data_says_down_is_x1():
    import response_validation as rv
    ctx = rv.ValidationContext(restaurant_id=1, surface="food_insight",
                               facts=[rv.Fact("waste.wow", -12.0, "%", period="week", direction="down"),
                                      rv.Fact("waste.week", 120, "$", period="week")])
    for text in ("Waste is climbing week over week.", "Waste is rising.", "Waste keeps climbing."):
        assert "X1" in rv.validate(text, ctx).codes, text
    for text in ("Waste is falling week over week.", "Waste is higher than the recipes call for."):
        assert "X1" not in rv.validate(text, ctx).codes, text


# ── LEAD-01: the Bar asks about each held Back Office entry ──────────────

def test_lead_01_the_bar_lists_held_entries_with_count_and_set_aside():
    import os
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates",
                            "dashboard.html"), encoding="utf-8").read()
    alerts = src[src.index("  function alerts(d, pc) {"):src.index("/* ── the three categories")]
    assert "(d.outside_fintech || {}).to_confirm" in alerts
    assert 'data-act="count">Count it</button>' in alerts and 'data-act="not_purchase">Not a purchase</button>' in alerts
    assert "ownerWrite('/api/food-cost/purchases/entry', {vendor_key: a('data-vendor'), entry_key: a('data-br-entry'), " \
           "decision: ea}" in src
    assert "[data-br-entry]" in src and "'data-br-entry'];" in src


# ── FC-CALC-02 in retention: a counted snapshot survives the weekly thin ─

def test_thinning_keeps_a_snapshot_taken_on_a_real_count(db_path):
    from datetime import date, timedelta
    import ops
    from models import get_conn
    from tests.test_edge_mod_a_food_cogs import _rid, _snapshot
    rid = _rid(db_path)
    mon = date.today() - timedelta(days=90)
    mon -= timedelta(days=mon.weekday())
    item = lambda recount: [{"name": "Beef", "category": "Meat", "current_stock": 10, "unit_cost": 5.0,
                             "last_recount_at": recount}]
    _snapshot(db_path, rid, mon, items=item(mon.isoformat()))                               # the count
    _snapshot(db_path, rid, mon + timedelta(days=1), items=item(mon.isoformat()))           # its day after
    _snapshot(db_path, rid, mon + timedelta(days=3), items=item(None))                      # a ledger day
    _snapshot(db_path, rid, mon + timedelta(days=6), items=item(None))                      # the week's last
    c = get_conn(db_path)
    ops._prune_inventory_history(c)
    left = [r[0] for r in c.execute("SELECT week_end FROM inventory_history WHERE restaurant_id=? ORDER BY week_end",
                                    (rid,)).fetchall()]
    c.close()
    assert left == [mon.isoformat(), (mon + timedelta(days=1)).isoformat(), (mon + timedelta(days=6)).isoformat()]


# ── EJ's 10/16/26: a house billing through a sister house's Fintech account ─

def test_a_number_and_amount_at_a_sister_house_is_that_fintech_invoice(monkeypatch):
    import backoffice_ap as bo
    from fintech_invoices import distributor_key as dk
    fins = [{"id": 1, "distributor": "Euclid Beverage, LLC. - North Aurora", "invoice_number": "W-4638131",
             "invoice_date": "2026-05-06", "total": 2249.45},
            {"id": 2, "distributor": "Relax LLC - North Aurora, IL (H)", "invoice_number": "W-4638130",
             "invoice_date": "2026-05-06", "total": 208.0}]
    for f in fins:
        f["distributor_key"] = dk(f["distributor"])
    monkeypatch.setattr(bo, "_fintech_invoices", lambda rid, db_path=None: fins)
    monkeypatch.setattr(bo, "_decisions", lambda rid, db_path=None: {})

    def entry(number, amount):
        return {"id": number, "vendor": "EUCLID BEVERAGE", "vendor_key": dk("EUCLID BEVERAGE"), "entry_key": f"inv|{number}",
                "entry_kind": "invoice", "invoice_number": number, "date": "2026-05-06", "amount": amount,
                "category": "beer", "gl": "4320"}
    out = bo.outside_fintech(1, es=[entry("W-4638130", 208.0)])
    assert out["matched"] == 1 and not out["to_confirm"] and not out["entries"]
    # The number alone at another house is not enough: a different amount is held.
    out = bo.outside_fintech(1, es=[entry("W-4638130", 210.0)])
    assert out["matched"] == 0 and len(out["to_confirm"]) == 1
