"""The Bar's sales side (barfix-1015 W3; bar audit 10/14/26 V5, V6, V7, R10,
R11, R23, F5, V41, R2, R25, R26): drink sales under the bar mapping as it
stands, net of check discounts allocated per ticket from the archive, a night
with no drink coverage named as a gap, the explicit item-category mapping the
account holder confirms, and the RPOWER archive that feeds it."""
import os
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta, timezone

import pytest
from flask import Flask

import auth
import bar.mapping as bmap
import bar.sales as bsales
import client_api
import mobile_api
import models
import pos
import pos_archive
import rpower
from models import Restaurant, create_restaurant

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEP = " › "


@pytest.fixture
def db(db_path, monkeypatch):
    real, default = models.get_conn, models.DB_PATH

    def redirected(path=None, *a, **k):
        return real(db_path if path in (None, default) else path)
    for mod in (models, auth, client_api, mobile_api):
        monkeypatch.setattr(mod, "get_conn", redirected)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(pos, "PROVIDERS", None)
    rpower.clear_caches()
    return db_path


def _rid(db, **kw):
    kw.setdefault("module_inventory", 1)
    return create_restaurant(Restaurant(name=kw.pop("name", "EJ's"), owner_email="o@x.test", **kw), db_path=db)


def _conn(db):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    return c


def _metrics(db, rid, day, values, source="rpower", report_id=None):
    c = _conn(db)
    try:
        for k, v in values.items():
            c.execute("INSERT INTO dsr_metrics (restaurant_id, business_date, metric, value, status, source, report_id) "
                      "VALUES (?,?,?,?, 'ready', ?, ?)", (rid, day, k, v, source, report_id))
        c.commit()
    finally:
        c.close()


def _lines(db, rid, day, lines):
    """Archived sale lines: (line_id, ticket, kind, sales, department, category, extra dict)."""
    c = _conn(db)
    try:
        for line_id, ticket, kind, sales, dep, cat, *extra in lines:
            row = {"restaurant_id": rid, "provider": "rpower", "line_id": line_id, "ticket_id": ticket,
                   "business_date": day, "kind": kind, "qty": 1, "sales": sales, "department": dep,
                   "sales_category": cat, "item_name": (extra[0] if extra else {}).get("name")}
            row.update({k: v for k, v in (extra[0] if extra else {}).items() if k != "name"})
            c.execute(f"INSERT INTO pos_ticket_lines ({','.join(row)}) VALUES ({','.join('?' * len(row))})",
                      tuple(row.values()))
        c.commit()
    finally:
        c.close()
    bsales.derive_nights(rid, [day], db_path=db)


# ── V6: net of check discounts, allocated per ticket ─────────────────────────

def _discount_night(db, rid, day="2026-10-10"):
    _lines(db, rid, day, [
        # A mixed ticket: a $10 beer, a $20 burger and $15 "Discount All".
        ("a1", "TA", "sale", 10.0, "Beer", "Draft"), ("a2", "TA", "sale", 20.0, "Food", "Burgers"),
        ("a3", "TA", "discount", -15.0, "Discounts", "Discount All", {"name": "Discount All"}),
        # A drinks-only ticket with "$50 Off GD Drinks".
        ("b1", "TB", "sale", 100.0, "Liquor", "38. Cocktails"),
        ("b2", "TB", "discount", -50.0, "Discounts", "Discount Liquor", {"name": "$50 Off GD Drinks"}),
        # A manager's 100% comp of a glass of wine: poured, not paid.
        ("c1", "TC", "sale", 30.0, "Wine", "Wine by the Glass"),
        ("c2", "TC", "discount", -30.0, "Discounts", "Discount All", {"name": "Mgr 100% Comp"}),
        # RPOWER's own `discounted` on the lines adds up to the discount: it is used, not pro rata.
        ("d1", "TD", "sale", 40.0, "Beer", "Draft", {"discounted": 9.0}),
        ("d2", "TD", "sale", 40.0, "Food", "Burgers", {"discounted": 1.0}),
        ("d3", "TD", "discount", -10.0, "Discounts", "Discount All", {"name": "Industry 20%"}),
        # A comp-type line: already outside the department dollars, poured all the same.
        ("e1", "TE", "comp", 0.0, "Beer", "Draft", {"loss_amount": 6.0}),
    ])
    deps = {"Beer": 50.0, "Food": 60.0, "Liquor": 100.0, "Wine": 30.0, "Discounts": -105.0}
    vals = {"sales.net": sum(deps.values())}
    vals.update({f"sales.dep:{k}": v for k, v in deps.items()})
    vals.update({f"sales.depcat:{k}{SEP}{s}": v for k, s, v in (
        ("Beer", "Draft", 50.0), ("Food", "Burgers", 60.0), ("Liquor", "38. Cocktails", 100.0),
        ("Wine", "Wine by the Glass", 30.0), ("Discounts", "Discount All", -55.0),
        ("Discounts", "Discount Liquor", -50.0))})
    _metrics(db, rid, day, vals)
    return day


def test_bar_sales_are_net_of_check_discounts_on_drink_tickets(db):
    rid = _rid(db)
    day = _discount_night(db, rid)
    detail = bsales.bar_sales(rid, db_path=db)
    by = detail["by_category"]
    # beer: $10 less its third of $15 ($5), plus $40 less RPOWER's own $9 = 36
    assert by["beer"][day] == pytest.approx(5.0 + 31.0)
    assert by["liquor"][day] == pytest.approx(50.0), "a drinks-only ticket's $50 comes off the drinks whole"
    assert by["wine"][day] == pytest.approx(0.0), "a 100% comp leaves nothing paid"
    basis = bsales.window_basis(detail, day, day)
    assert basis["discounts_usd"] == pytest.approx(5.0 + 50.0 + 9.0)
    assert basis["comps_usd"] == pytest.approx(30.0 + 6.0), "poured, not paid: 100% comps and comp lines"
    assert basis["gross_usd"] == pytest.approx(180.0) and basis["net_usd"] == pytest.approx(86.0)
    assert basis["label"] == "POS drink sales after comps and check discounts"
    # the old reading — department dollars before discounts — is what the Bar used to divide by
    assert sum(v[day] for v in by.values() if day in v) < 180.0


def test_a_night_without_an_archive_estimates_its_discounts_and_says_so(db):
    rid = _rid(db)
    day = "2026-10-11"
    _metrics(db, rid, day, {"sales.net": 900.0, "sales.dep:Beer": 400.0, "sales.dep:Food": 600.0,
                            "sales.dep:Discounts": -100.0})
    detail = bsales.bar_sales(rid, db_path=db)
    assert detail["by_category"]["beer"][day] == pytest.approx(400.0 - 100.0 * 400.0 / 1000.0)
    basis = bsales.window_basis(detail)
    assert basis["estimated_nights"] == 1 and "estimated for 1 night" in basis["label"]


# ── V5: a night with net sales and no drink coverage is a named gap ─────────

def test_a_night_with_net_but_no_drink_categories_is_a_gap(db):
    rid = _rid(db)
    for d in ("2026-09-26", "2026-09-28"):
        _metrics(db, rid, d, {"sales.net": 1000.0, "sales.dep:Beer": 300.0, "sales.dep:Food": 700.0})
    # Simple EJ's 9/27: the first report, built before the departments were mapped.
    _metrics(db, rid, "2026-09-27", {"sales.net": 11617.28, "sales.cat:Unmapped": 11617.28})
    # A night the report holds only its net for.
    _metrics(db, rid, "2026-09-29", {"sales.net": 900.0})
    detail = bsales.bar_sales(rid, db_path=db)
    assert detail["nights"]["2026-09-27"]["gap"]["why"] == "unmapped_in_report"
    assert detail["nights"]["2026-09-29"]["gap"]["why"] == "no_departments"
    assert "2026-09-27" not in detail["by_category"]["beer"], "a gap night is never read as $0 of drinks"
    basis = bsales.window_basis(detail, "2026-09-26", "2026-09-29")
    assert [g["date_mdy"] for g in basis["gaps"]] == ["9/27/26", "9/29/26"]
    assert basis["covered_nights"] == 2 and basis["nights"] == 4


def test_the_window_with_a_sales_gap_never_reads_complete(db):
    import fintech_invoices as fi
    rid = _rid(db)
    head = ("Retailer Name,Vendor Name,Location ID,Invoice Number,Invoice Date,Invoice Total,Product Number,"
            "Product Description,U.P.C,Unit Of Measure,Item Quantity,Item Cost,Item Adjustment\n")
    csv = head + 'EJs,"Chicago Beverage",1,A1,09/26/2026,$400.00,1,DAISY CUTTER K 1/2BB,0,KE,2,$200.00,$0.00\n' \
               + 'EJs,"Chicago Beverage",1,A2,09/29/2026,$400.00,1,DAISY CUTTER K 1/2BB,0,KE,2,$200.00,$0.00\n'
    fi.ingest_file(rid, "x.csv", csv.encode(), db_path=db)
    for d in ("2026-09-26", "2026-09-28", "2026-09-29"):
        _metrics(db, rid, d, {"sales.net": 1000.0, "sales.dep:Beer": 300.0, "sales.dep:Food": 700.0})
    _metrics(db, rid, "2026-09-27", {"sales.net": 11617.28, "sales.cat:Unmapped": 11617.28})
    out = bmap and __import__("bar_cost").build(rid, db_path=db, today=date(2026, 9, 30))
    pour = out["pour_cost"]
    assert pour["complete"] is False
    assert [g["date"] for g in pour["sales_gaps"]] == ["2026-09-27"]
    assert out["sales_basis"]["gaps"][0]["why"] == "unmapped_in_report"


def test_a_night_without_department_dollars_reads_from_the_archive(db):
    rid = _rid(db)
    day = "2026-09-27"
    _metrics(db, rid, day, {"sales.net": 2650.0, "sales.cat:Unmapped": 2650.0})
    _lines(db, rid, day, [("l1", "T1", "sale", 2217.0, "Beer", "Draft"),
                          ("l2", "T2", "sale", 433.0, "Food", "Burgers")])
    detail = bsales.bar_sales(rid, db_path=db)
    n = detail["nights"][day]
    assert n["gap"] is None and n["source"] == "archive"
    assert detail["by_category"]["beer"][day] == pytest.approx(2217.0)


def test_repair_gives_the_unmapped_night_its_department_dollars_once(db):
    rid = _rid(db)
    from dsr import backfill
    day = "2026-09-27"
    _metrics(db, rid, day, {"sales.net": 2650.0, "sales.cat:Unmapped": 2650.0})
    _lines(db, rid, day, [("l1", "T1", "sale", 2217.0, "Beer", "Draft"),
                          ("l2", "T2", "sale", 433.0, "Food", "Burgers")])
    dry = backfill.repair_department_metrics(rid, db_path=db, dry_run=True)
    assert [r["date"] for r in dry["repaired"]] == [day]
    c = _conn(db)
    assert c.execute("SELECT COUNT(*) FROM dsr_metrics WHERE metric LIKE 'sales.dep%'").fetchone()[0] == 0
    c.close()
    out = backfill.repair_department_metrics(rid, db_path=db)
    assert out["repaired"] == [day]
    c = _conn(db)
    got = {r["metric"]: (r["value"], r["source"]) for r in c.execute(
        "SELECT metric, value, source FROM dsr_metrics WHERE restaurant_id=? AND business_date=? "
        "AND metric >= 'sales.dep' AND metric < 'sales.deq'", (rid, day))}
    c.close()
    assert got["sales.dep:Beer"] == (2217.0, "archive_repair")
    assert got[f"sales.depcat:Beer{SEP}Draft"][0] == 2217.0
    assert backfill.repair_department_metrics(rid, db_path=db)["repaired"] == [], "idempotent"
    assert bsales.bar_sales(rid, db_path=db)["nights"][day]["source"] == "report"


def test_repair_refuses_a_night_whose_archive_does_not_add_up(db):
    rid = _rid(db)
    from dsr import backfill
    _metrics(db, rid, "2026-09-27", {"sales.net": 5000.0, "sales.cat:Unmapped": 5000.0})
    _lines(db, rid, "2026-09-27", [("l1", "T1", "sale", 2217.0, "Beer", "Draft")])
    out = backfill.repair_department_metrics(rid, db_path=db)
    assert out["repaired"] == [] and "add to $2,217.00" in out["skipped"][0]["why"]


# ── V7 / R10: the explicit mapping, never substrings ─────────────────────────

def test_bar_sales_use_the_explicit_bar_mapping_never_substrings(db):
    rid = _rid(db)
    day = "2026-10-05"
    deps = {"Draft": 500.0, "N/A Beer": 40.0, "Root Beer Floats": 30.0, "Wine & Spirits": 200.0, "Bar": 100.0,
            "Food": 1000.0}
    _metrics(db, rid, day, {"sales.net": sum(deps.values()), **{f"sales.dep:{k}": v for k, v in deps.items()}})
    d = bsales.bar_sales(rid, db_path=db)
    assert d["by_category"]["beer"][day] == 500.0, "only Draft: never N/A Beer or Root Beer"
    assert d["by_category"]["na"][day] == 70.0
    basis = bsales.window_basis(d)
    assert basis["mapping_needed"] is True, "a suggestion is never silently final"
    assert {u["key"] for u in basis["unmapped_categories"]} == {"Wine & Spirits", "Bar"}
    # The account holder decides: Draft is a root-beer tap here, Bar is liquor.
    bmap.save_mapping(rid, [{"kind": "department", "key": "Draft", "bar_category": "na"},
                            {"kind": "department", "key": "Bar", "bar_category": "liquor"},
                            {"kind": "department", "key": "Wine & Spirits", "bar_category": "liquor"}],
                      db_path=db)
    d = bsales.bar_sales(rid, db_path=db)
    assert "beer" not in d["by_category"], "a name with 'draft' in it counts where the owner put it"
    assert d["by_category"]["liquor"][day] == 300.0
    assert bsales.window_basis(d)["unmapped_usd"] == 0.0


def test_root_beer_and_na_beer_are_not_beer_sales():
    """EDG list 19, against the rule rather than one night: every
    non-alcoholic name with 'beer' in it suggests NA; plain drink names
    suggest their drink; a name of two drinks is the owner's to say."""
    for name in ("Root Beer", "Root Beer Floats", "N/A Beer", "NA Beer", "Ginger Beer", "Non-Alc Beer",
                 "Zero Proof", "Mocktails"):
        assert bmap.suggest(name)["kind"] == "na", name
    for name, kind in (("Draft", "beer"), ("Bottles & Cans", "beer"), ("Hard Cider", "beer"), ("Shots", "liquor"),
                       ("Cocktails", "liquor"), ("Wine by the Glass", "wine")):
        assert bmap.suggest(name)["kind"] == kind, name
    for name in ("Wine & Spirits", "Beer & Shots"):
        assert bmap.suggest(name)["kind"] is None, name


def test_a_pos_category_renamed_mid_window_is_named_not_lost(db):
    """EDG list 18 / V7: "Beer" renamed mid-window. To a drink name
    ("Draft") its sales keep counting as beer, unconfirmed; to a name that
    says nothing ("Local Pours") they are named with their dollars and the
    mapping asked for — never silently dropped from the beer figure."""
    rid = _rid(db)
    days = [f"2026-10-{d:02d}" for d in range(1, 18)]
    for i, day in enumerate(days):
        name = "Beer" if i < 7 else ("Draft" if i < 11 else "Local Pours")
        vals = {"sales.net": 1500.0, f"sales.dep:{name}": 500.0, "sales.dep:Food": 1000.0}
        if i >= 14:                    # a night that still sells cocktails: not a gap night
            vals.update({"sales.net": 1800.0, "sales.dep:Cocktails": 300.0})
        _metrics(db, rid, day, vals)
    d = bsales.bar_sales(rid, db_path=db)
    beer = d["by_category"]["beer"]
    assert [beer.get(day) for day in days[:11]] == [500.0] * 11
    assert not any(day in beer for day in days[11:])
    basis = bsales.window_basis(d)
    assert basis["mapping_needed"] is True
    # A night whose only drinks are under the new name is a named gap night
    # (V5: it keeps the window from being judged)...
    gaps = {g["date"]: g for g in basis["gaps"]}
    assert sorted(gaps) == days[11:14] and {g["why"] for g in gaps.values()} == {"no_drinks_mapped"}
    # ...and on a night with other drinks, the new name is named with its dollars.
    named = {u["key"]: u for u in basis["unmapped_categories"]}
    assert named["Local Pours"]["sales_usd"] == 1500.0 and basis["unmapped_usd"] == 1500.0


def test_an_item_category_inside_beer_can_be_mapped_to_liquor(db):
    rid = _rid(db)
    day = "2026-10-05"
    _metrics(db, rid, day, {"sales.net": 1000.0, "sales.dep:Beer": 1000.0,
                            f"sales.depcat:Beer{SEP}Draft": 700.0,
                            f"sales.depcat:Beer{SEP}56. Brunch Drinks": 300.0})
    d = bsales.bar_sales(rid, db_path=db)
    assert d["by_category"]["beer"][day] == 1000.0      # the suggestion: its department's
    bmap.save_mapping(rid, [{"kind": "item_category", "key": f"Beer{SEP}56. Brunch Drinks",
                             "bar_category": "liquor"}], db_path=db)
    d = bsales.bar_sales(rid, db_path=db)
    assert (d["by_category"]["beer"][day], d["by_category"]["liquor"][day]) == (700.0, 300.0)


def test_bar_sales_follow_the_current_category_map(db):
    rid = _rid(db)
    day = "2026-10-01"
    _metrics(db, rid, day, {"sales.net": 1000.0, "sales.dep:Cerveza": 400.0, "sales.dep:Food": 600.0,
                            # what the report froze when it was built: never read by the Bar
                            "sales.cat:Beer": 999.0})
    assert "beer" not in bsales._sales(rid, db_path=db)
    bmap.save_mapping(rid, [{"kind": "department", "key": "Cerveza", "bar_category": "beer"}], db_path=db)
    assert bsales._sales(rid, db_path=db)["beer"][day] == 400.0, "a mapping made after the night moves it"


def test_report_category_nights_map_through_the_reports_own_map(db):
    from dsr import store
    rid = _rid(db)
    store.set_category(rid, "Cerveza", "Cold Drinks", db_path=db)
    bmap.save_mapping(rid, [{"kind": "department", "key": "Cerveza", "bar_category": "beer"}], db_path=db)
    _metrics(db, rid, "2026-08-01", {"sales.cat:Cold Drinks": 250.0, "sales.cat:Food": 900.0})
    d = bsales.bar_sales(rid, db_path=db)
    assert d["by_category"]["beer"]["2026-08-01"] == 250.0


def test_a_name_that_says_nothing_takes_the_owners_report_category_as_its_suggestion(db):
    from dsr import store
    rid = _rid(db)
    store.set_category(rid, "Beverage", "NA Beverage", db_path=db)     # Simple EJ's own report map
    store.set_category(rid, "Other", "Darts", db_path=db)
    m = bmap.Mapping(rid, db)
    assert m.resolve("department", "Beverage")["kind"] == "na"
    assert m.resolve("item_category", f"Beverage{SEP}20. Beverages")["kind"] == "na"
    assert m.resolve("department", "Other") == {"kind": "not_bar", "confirmed": False, "source": "suggested",
                                                "why": "your report files it under Darts"}


def test_unmapped_sales_in_the_window_are_named(db):
    rid = _rid(db)
    day = "2026-10-05"
    _metrics(db, rid, day, {"sales.net": 10000.0, "sales.dep:Beer": 2000.0, "sales.dep:Gameday": 500.0,
                            "sales.dep:Food": 7500.0})
    basis = bsales.window_basis(bsales.bar_sales(rid, db_path=db))
    assert basis["unmapped_usd"] == 500.0 and basis["unmapped_pct"] == 5.0 and basis["unmapped_say"] is True
    assert basis["unmapped_categories"][0]["key"] == "Gameday"


def test_the_seed_reads_rpower_is_bar_and_names_and_never_a_mixed_name():
    s = bmap.suggest
    assert s("Beer", "56. Brunch Drinks")["kind"] == "beer"           # a suggestion the owner can move
    assert s("Discounts", "Discount Liquor")["kind"] == "not_bar"     # a discount is never a drink
    assert s("Specials", "House Pours", is_bar=1)["kind"] is None      # RPOWER says bar; which drink is theirs
    assert "RPOWER marks it" in s("Specials", "House Pours", is_bar=1)["why"]
    assert s("Liquor", "House Pours", is_bar=1)["kind"] == "liquor"
    for name in ("Bar", "Alcohol", "Wine & Spirits", "Beverage"):
        assert s(name)["kind"] is None, name
    for name in ("N/A Beer", "Root Beer Floats", "Ginger Beer (NA)", "NA Beverage"):
        assert s(name)["kind"] == "na", name


def test_sales_query_uses_metric_range(db):
    src = open(os.path.join(ROOT, "bar", "sales.py"), encoding="utf-8").read()
    import re
    assert not re.search(r"LIKE\s+['?]", src, re.I), "a range on the series index, never LIKE"
    assert "metric >= 'sales.dep:' AND metric < 'sales.dep;'" in src
    c = _conn(db)
    plan = " ".join(str(tuple(r)) for r in c.execute(
        "EXPLAIN QUERY PLAN SELECT business_date, metric, value FROM dsr_metrics WHERE restaurant_id=? "
        "AND (metric >= 'sales.dep:' AND metric < 'sales.dep;') AND value IS NOT NULL", (1,)))
    c.close()
    assert "idx_dsr_metrics_series" in plan or "PRIMARY" not in plan


# ── the mapping screen's routes ──────────────────────────────────────────────

def _app():
    app = Flask(__name__, template_folder=os.path.join(ROOT, "templates"))
    app.register_blueprint(client_api.client_bp)
    app.register_blueprint(mobile_api.mobile_bp)
    return app


def _as(monkeypatch, rid, role=None):
    user = {"id": 7, "restaurant_id": rid, "is_admin": 0, "username": "u"}
    if role:
        user["role"] = role
    monkeypatch.setattr(auth, "get_current_user", lambda: dict(user))


def test_the_mapping_routes_list_everything_and_only_the_account_holder_changes_them(db, monkeypatch):
    rid = _rid(db)
    day = (date.today() - timedelta(days=3)).isoformat()
    _metrics(db, rid, day, {"sales.net": 1000.0, "sales.dep:Beer": 1000.0,
                            f"sales.depcat:Beer{SEP}Draft": 700.0,
                            f"sales.depcat:Beer{SEP}56. Brunch Drinks": 300.0})
    c = _conn(db)
    c.execute("INSERT INTO pos_sales_categories (restaurant_id, provider, category_id, name, department, is_bar) "
              "VALUES (?, 'rpower', 'K9', 'House Pours', 'Specials', 1)", (rid,))
    c.commit()
    c.close()
    _as(monkeypatch, rid)
    cl = _app().test_client()
    g = cl.get("/api/food-cost/bar/mapping")
    body = g.get_json()
    assert g.status_code == 200 and body["mapping_needed"] is True and body["can_change"] is True
    rows = {e["key"]: e for e in body["entries"]}
    brunch = rows[f"Beer{SEP}56. Brunch Drinks"]
    assert (brunch["choice"], brunch["suggestion"], brunch["confirmed"], brunch["sales_28d"]) == \
        ("beer", "beer", False, 300.0)
    pours = rows[f"Specials{SEP}House Pours"]
    assert pours["is_bar"] is True and pours["choice"] is None and pours["sales_28d"] == 0.0
    assert {o["key"] for o in body["options"]} == set(bmap.BAR_KINDS)
    # A teammate can't change it.
    _as(monkeypatch, rid, role="manager")
    r = cl.post("/api/food-cost/bar/mapping", json={"entries": [], "confirm": True})
    assert r.status_code == 403
    # The account holder moves brunch drinks to liquor and confirms the rest as suggested, once.
    _as(monkeypatch, rid)
    r = cl.post("/api/food-cost/bar/mapping",
                json={"entries": [{"kind": "item_category", "key": f"Beer{SEP}56. Brunch Drinks",
                                   "bar_category": "liquor"}], "confirm": True})
    out = r.get_json()
    assert r.status_code == 200 and out["confirmed"] >= 1 and out["mapping"]["mapping_needed"] is False
    assert out["changes"] == [{"key": f"Beer{SEP}56. Brunch Drinks", "kind": "item_category",
                               "before": "beer", "after": "liquor"}]
    assert cl.post("/api/food-cost/bar/mapping", json={"entries": [{"key": "x", "bar_category": "rum"}]}
                   ).status_code == 400
    c = _conn(db)
    ev = [(r["event_type"]) for r in c.execute("SELECT event_type FROM activity_log WHERE restaurant_id=?", (rid,))]
    c.close()
    assert "bar_mapping_changed" in ev and "bar_mapping_changed" in models.ACCOUNT_EVENT_TYPES
    rules = {r.rule for r in _app().url_map.iter_rules()}
    assert "/api/food-cost/bar/mapping" in rules and "/mobile/api/food-cost/bar/mapping" in rules


# ── F5: the archive keeps what the bar needs ─────────────────────────────────

def _routes():
    return {
        "salestype/getbycg": [{"mid": "S", "name": "Sale", "impacts_sales": 1, "type_sale": 1},
                              {"mid": "D", "name": "Discount", "impacts_sales": 1, "type_discnt": 1}],
        "salescategory/getbycg": [
            {"mid": "K1", "name": "Liquor Drinks", "slsdep_mid": "DP1", "is_bar": 1, "alt_qty_unit": "Oz",
             "alt_rpt_unit": "Oz", "aqu_in_aru": 1},
            {"mid": "K2", "name": "(Liquor Add-Ons)", "slsdep_mid": "DP1", "is_bar": 1}],
        "salesdepartment/getbycg": [{"mid": "DP1", "name": "Liquor"}],
        "menuitem/getbycg": [{"mid": "M1", "name": "Titos Soda`", "slscat_mid": "K1", "last_cost": 1.85,
                              "plu": "2201", "upc": "", "aqu_count": 1.5},
                             {"mid": "M2", "name": "Double`", "slscat_mid": "K2", "is_mod": 0, "last_cost": 0}],
        "voidreason/getbycg": [], "job/getbycg": [], "employee/getbycg": [], "table/getbystore": [],
        "room/getbystore": [], "mealtime/getbycg": [], "profitcenter/getbycg": [], "paymentmethod/getbycg": [],
        "payoutcategory/getbycg": [], "ticketpayment/getbybusinessdate": [], "payout/getbydaterange": [],
        "timeclock/getbydaterange": [],
        "ticket/getbybusinessdate": [{"rid": "TK", "date": "2026-10-10T00:00:00", "ticket": 7,
                                      "time_stamp": "2026-10-11T01:00:00"}],
        "ticketsales/getbybusinessdate": [
            {"rid": "L1", "ticket_rid": "TK", "date": "2026-10-10T00:00:00", "menuitem_mid": "M1", "slstype_mid": "S",
             "qty": 1, "sales": 9.0, "cost": 1.85, "discounted": 0, "aqu_count": 1.5, "fraction": 0,
             "atom": 1539672696, "time_stamp": "2026-10-11T01:00:00"},
            {"rid": "L2", "ticket_rid": "TK", "date": "2026-10-10T00:00:00", "menuitem_mid": "M2", "slstype_mid": "S",
             "qty": 1, "sales": 4.0, "cost": 0, "discounted": 0, "aqu_count": 1.5, "atom": 1540486356,
             "time_stamp": "2026-10-11T01:00:00"}],
        # The receipt's view: the Double is a modifier of the Titos (parent_atom, mod_level).
        "ticketitem/getbybusinessdate": [
            {"rid": "I1", "ticket_rid": "TK", "date": "2026-10-10T00:00:00", "atom": 1539672696, "parent_atom": 0,
             "mod_level": 0, "qty": 1, "disp_total": 9, "real_total": 9},
            {"rid": "I2", "ticket_rid": "TK", "date": "2026-10-10T00:00:00", "atom": 1540486356,
             "parent_atom": 1539672696, "mod_level": 1, "qty": 1, "disp_total": 99, "real_total": 99}],
    }


def _rpower_rid(db):
    c = _conn(db)
    c.execute("INSERT INTO restaurants (id, name, owner_email, rpower_token, rpower_cg, rpower_store_mid, timezone) "
              "VALUES (41, 'RP Co', 'r@x.test', 'tok', 1280, '1123', 'America/Chicago')")
    c.commit()
    c.close()
    return 41


def test_archive_keeps_ticketsales_cost_and_aqu_count(db, monkeypatch):
    rid = _rpower_rid(db)
    routes = _routes()
    monkeypatch.setattr(rpower, "_request", lambda token, path, params=None: routes[path])
    monkeypatch.setattr(pos_archive, "provider_for", lambda r: ("rpower", rpower))
    pos_archive.archive_day(rid, date(2026, 10, 10), db_path=db)
    c = _conn(db)
    rows = {r["line_id"]: dict(r) for r in c.execute(
        "SELECT line_id, department, sales_category, cost, discounted, aqu_count, atom FROM pos_ticket_lines "
        "WHERE restaurant_id=?", (rid,))}
    c.close()
    assert (rows["L1"]["department"], rows["L1"]["sales_category"]) == ("Liquor", "Liquor Drinks")
    assert (rows["L1"]["cost"], rows["L1"]["aqu_count"], rows["L1"]["atom"]) == (1.85, 1.5, "1539672696")
    assert pos_archive.ARCHIVE_VERSION >= 4
    # The catalog beside it: is_bar, the alternate quantity, the POS's cost per item.
    out = pos_archive.sync_catalog(rid, db_path=db)
    assert out["ok"] and out["bar_categories"] == 2 and out["costed_items"] == 1
    c = _conn(db)
    cat = dict(c.execute("SELECT * FROM pos_sales_categories WHERE restaurant_id=? AND category_id='K1'",
                         (rid,)).fetchone())
    item = dict(c.execute("SELECT * FROM pos_menu_catalog WHERE restaurant_id=? AND item_id='M1'", (rid,)).fetchone())
    c.close()
    assert (cat["department"], cat["is_bar"], cat["alt_qty_unit"], cat["aqu_in_aru"]) == ("Liquor", 1, "Oz", 1.0)
    assert (item["last_cost"], item["plu"], item["upc"], item["aqu_count"]) == (1.85, "2201", None, 1.5)
    # ...and the night is derived for the Bar as it is stored.
    c = _conn(db)
    n = c.execute("SELECT SUM(sales) FROM bar_sales_nights WHERE restaurant_id=? AND business_date='2026-10-10'",
                  (rid,)).fetchone()[0]
    c.close()
    assert n == 13.0


def test_a_double_modifier_links_to_its_drink(db, monkeypatch):
    rid = _rpower_rid(db)
    routes = _routes()
    monkeypatch.setattr(rpower, "_request", lambda token, path, params=None: routes[path])
    out = rpower.archive_rows(rid, date(2026, 10, 10))
    lines = {l["line_id"]: l for l in out["lines"]}
    assert (lines["L2"]["parent_line_id"], lines["L2"]["mod_level"]) == ("L1", 1)
    assert lines["L1"].get("parent_line_id") is None and lines["L1"]["mod_level"] == 0
    # ticketitem is never summed: the $99 on the receipt line is nowhere in the sales.
    assert sum(l["sales"] for l in out["lines"]) == 13.0
    # A failed ticketitem read leaves the links empty and the night archived.
    def broken(token, path, params=None):
        if path == "ticketitem/getbybusinessdate":
            raise rpower.RPowerError("HTTP 500")
        return routes[path]
    monkeypatch.setattr(rpower, "_request", broken)
    out = rpower.archive_rows(rid, date(2026, 10, 10))
    assert len(out["lines"]) == 2 and all(l.get("parent_line_id") is None for l in out["lines"])


# ── R2: shared rate, bounded caches, the archive as queue rows ──────────────

def test_rpower_rate_limiter_is_shared_across_threads(db, monkeypatch):
    monkeypatch.setattr(rpower, "REQUEST_SPACING_SECONDS", 0.05)
    rpower._token_locks.clear()
    stamps = []

    def go():
        rpower._sleep_for_spacing("tok-1")
        stamps.append(time.monotonic())
    threads = [threading.Thread(target=go) for _ in range(6)]
    t0 = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert max(stamps) - t0 >= 0.05 * 5 - 0.02, "six requests on one token are spaced, whichever thread asks"
    # Another process (no local state at all) sees the same budget in the database.
    assert rpower._take_shared_slot("tok-2", 10.0) == 0.0
    rpower._token_locks.clear()
    rpower._last_request_at.clear()
    wait = rpower._take_shared_slot("tok-2", 10.0)
    assert wait is not None and wait > 9.0
    c = _conn(db)
    keys = [r[0] for r in c.execute("SELECT bucket FROM rpower_rate_bucket")]
    c.close()
    assert keys and all("tok" not in k for k in keys), "the token itself is never stored"


def test_rpower_caches_are_bounded():
    cache = rpower._LRU(3)
    for i in range(5):
        cache[i] = i
    assert len(cache) == 3 and cache.get(0) is None and cache.get(4) == 4
    cache.get(2)
    cache[9] = 9
    assert cache.get(3) is None and cache.get(2) == 2, "least recently used goes first"
    for name in ("_sales_type_cache", "_menu_cache", "_people_cache", "_void_reason_cache", "_catalog_cache",
                 "_closed_cache", "_job_cat_cache"):
        assert isinstance(getattr(rpower, name), rpower._LRU), name


def test_pos_archive_fans_out_as_queue_rows(db, monkeypatch):
    import job_queue
    import strategy_jobs
    can = _rid(db, name="Archives")
    _rid(db, name="No POS")
    monkeypatch.setattr(pos_archive, "provider_for", lambda r: ("rpower", object()) if r == can else (None, None))
    out = strategy_jobs.run_pos_archive(db_path=db, today=date(2026, 10, 15))
    assert (out["attempted"], out["ok"], out["skipped"]) == (1, 1, 1)
    assert strategy_jobs.run_pos_archive(db_path=db, today=date(2026, 10, 15))["queued"] == 0, "once a day"
    c = _conn(db)
    rows = [dict(r) for r in c.execute("SELECT kind, restaurant_id, priority_class FROM jobs WHERE kind='pos_archive'")]
    c.close()
    assert rows == [{"kind": "pos_archive", "restaurant_id": can, "priority_class": "bulk"}]
    assert job_queue.HANDLERS["pos_archive"] == "strategy_jobs:run_queued_pos_archive"
    # The row's handler runs the restaurant's night and raises when a day failed (the queue retries).
    calls = []
    monkeypatch.setattr(pos_archive, "run_for", lambda rid: calls.append(rid) or {"ok": False, "failed": ["x"]})
    job = type("J", (), {"payload": {"restaurant_id": can}})
    with pytest.raises(RuntimeError):
        strategy_jobs.run_queued_pos_archive(job)
    monkeypatch.setattr(pos_archive, "run_for", lambda rid: {"ok": True, "archived": ["d"], "restated": 0})
    assert strategy_jobs.run_queued_pos_archive(job)["archived"] == 1


# ── R11: the DSR backfill is its own job, restated with the archive ──────────

def test_dsr_backfill_failure_reaches_job_runs(db, monkeypatch):
    import jobs_registry
    import ops
    import strategy_jobs
    from dsr import backfill
    rid = _rid(db, dsr_enabled=1)
    monkeypatch.setattr(pos, "connected_provider", lambda r: ("rpower", object()))

    def boom(*a, **k):
        raise RuntimeError("RPOWER read failed")
    monkeypatch.setattr(backfill, "backfill", boom)
    assert "dsr_backfill" in jobs_registry.JOBS
    out = ops.run_job("dsr_backfill", strategy_jobs.run_dsr_backfill, db_path=db)
    assert out["attempted"] >= 1 and out["failed"] == out["attempted"] and out["ok"] == 0
    c = _conn(db)
    row = c.execute("SELECT ok, error FROM job_runs WHERE job='dsr_backfill' ORDER BY id DESC LIMIT 1").fetchone()
    c.close()
    assert row is not None and row["ok"] == 0 and "failed" in row["error"]
    src = open(os.path.join(ROOT, "rpower.py"), encoding="utf-8").read()
    assert "_dsr_backfill.backfill(" not in src, "never again a swallowed side effect of the POS sync"
    sched = open(os.path.join(ROOT, "scheduler.py"), encoding="utf-8").read()
    assert '_ops.run_job("dsr_backfill", run_dsr_backfill)' in sched
    assert rid


def test_a_restated_backfilled_night_is_read_again_and_a_reported_one_is_not(db, monkeypatch):
    from dsr import backfill
    rid = _rid(db, dsr_enabled=1)
    _metrics(db, rid, "2026-09-20", {"sales.net": 100.0, "sales.dep:Beer": 100.0}, source="rpower_backfill")
    _metrics(db, rid, "2026-09-21", {"sales.net": 200.0}, source="rpower", report_id=9)
    monkeypatch.setattr(pos, "connected_provider", lambda r: ("rpower", object()))
    monkeypatch.setattr(pos, "fetch_day_sales", lambda r, d: (
        {"gross": 160.0, "net": 150.0, "transactions": 9, "by_department": {"Beer": 150.0},
         "by_department_category": {"Beer": {"Draft": 150.0}}}, "rpower"))
    out = backfill.restate(rid, ["2026-09-20", "2026-09-21"], db_path=db)
    assert (out["restated"], out["skipped"]) == (1, 1)
    c = _conn(db)
    got = {(r["business_date"], r["metric"]): r["value"] for r in c.execute(
        "SELECT business_date, metric, value FROM dsr_metrics WHERE restaurant_id=?", (rid,))}
    c.close()
    assert got[("2026-09-20", "sales.net")] == 150.0 and got[("2026-09-20", f"sales.depcat:Beer{SEP}Draft")] == 150.0
    assert got[("2026-09-21", "sales.net")] == 200.0, "a reported night is the report's"


# ── R26: a week at a time ────────────────────────────────────────────────────

def test_changed_business_dates_requests_at_most_a_week(db, monkeypatch):
    rid = _rpower_rid(db)
    asked = []

    def req(token, path, params=None):
        asked.append((params["startdate"], params["enddate"]))
        return [{"date": params["startdate"] + "T00:00:00", "time_stamp": params["startdate"] + "T12:00:00"}]
    monkeypatch.setattr(rpower, "_request", req)
    since = datetime(2026, 9, 20, 3, 0, tzinfo=timezone.utc)
    until = datetime(2026, 10, 10, 3, 0, tzinfo=timezone.utc)
    out = rpower.changed_business_dates(rid, since, until)
    spans = [(date.fromisoformat(a), date.fromisoformat(b)) for a, b in asked]
    assert all((b - a).days <= rpower.MAX_RANGE_DAYS for a, b in spans)
    assert spans[0][0] == date(2026, 9, 20) and spans[-1][1] == date(2026, 10, 11)
    assert all(spans[i][1] == spans[i + 1][0] for i in range(len(spans) - 1)), "no day between two requests"
    assert "2026-09-27" in out


# ── V41: the P-Mix's headers name the lines below them ──────────────────────

PMIX = """Consolidated Product Mix
Page 29
Bottled Beer
2122 Coors Light 369 1,845.00
2123 Miller Lite 200 1,000.00
Total Bottled Beer 569 2,845.00
White Wine
3001 Gl Chardonnay 40 360.00
White Wine Total 40 360.00
"""


def test_pmix_lines_take_the_category_header_above_them(db):
    import plu_map
    rows = plu_map.parse_pmix(PMIX)
    assert [(r["name"], r["category"]) for r in rows] == [
        ("Coors Light", "Bottled Beer"), ("Miller Lite", "Bottled Beer"), ("Gl Chardonnay", "White Wine")]
    rid = _rid(db)
    # The 10/5/26 import: beers filed under the header below them.
    plu_map.save(rid, [{"plu": "2122", "bo_name": "Coors Light", "bo_category": "White Wine", "item_id": "m1",
                        "how": "name"},
                       {"plu": "2123", "bo_name": "Miller Lite", "bo_category": "White Wine", "item_id": "m2",
                        "how": "name"}], source="pmix", db_path=db)
    plu_map.set_manual(rid, "3001", "Gl Chardonnay", "m3", db_path=db)
    out = plu_map.recategorise(rid, PMIX, db_path=db)
    assert (out["changed"], out["manual"]) == (2, 1)
    assert plu_map.recategorise(rid, PMIX, db_path=db)["changed"] == 0, "idempotent"
    c = _conn(db)
    cats = {r["bo_name"]: r["bo_category"] for r in c.execute("SELECT bo_name, bo_category FROM pos_item_plus")}
    c.close()
    assert cats["Coors Light"] == "Bottled Beer" and cats["Miller Lite"] == "Bottled Beer"


# ── R25: the record says what was verified ───────────────────────────────────

def test_pagination_is_recorded_as_verified_and_the_docs_name_the_bar_as_a_fintech_reader():
    assert rpower.PAGINATION_UNVERIFIED is False and "2,599" in rpower.PAGINATION_VERIFIED
    schema = open(os.path.join(ROOT, "DATABASE_SCHEMA.md"), encoding="utf-8").read()
    assert "Nothing owner-facing reads them" not in schema and "bar.purchases._lines" in schema
    fi = open(os.path.join(ROOT, "fintech_invoices.py"), encoding="utf-8").read()
    assert "Nothing here is owner-facing yet" not in fi and "bar.purchases" in fi
