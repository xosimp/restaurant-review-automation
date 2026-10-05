"""A mapped POS department that starts selling something new (Simple EJ's,
10/5/26): RPOWER's "Other" department held only "Darts" when Erik mapped it
to Darts. Pool time rung into "Other" later would have counted as darts with
nobody told. Now the night records what is inside each department, a mapping
remembers what its department held, and anything new inside it is listed on
its own ("Other › Pool"), named, until the owner places it."""
from datetime import date

import pytest

import models
import pos
import rpower
from dsr import block_sales, rollup, store
import dsr


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


def _rid(db_path):
    return models.create_restaurant(models.Restaurant(name="EJ", owner_email="e@x.test"), db_path=db_path)


DARTS = {"other": "Darts", "food": "Food"}


def test_a_new_item_category_inside_a_mapped_department_is_not_counted_toward_it():
    cats, unmapped = block_sales.categorize(
        DARTS, {"Other": 80.0, "Food": 500.0}, 580.0,
        contents={"Other": {"Darts": 50.0, "Pool": 30.0}}, held={"other": {"darts"}})
    by = {c["category"]: c["net"] for c in cats}
    assert by == {"Food": 500.0, "Darts": 50.0}
    assert unmapped == [{"department": "Other › Pool", "net": 30.0, "new_in": "Other",
                         "mapped_to": "Darts", "pos_category": "Pool"}]


def test_placing_it_by_name_counts_it_where_the_owner_put_it():
    mapping = dict(DARTS, **{"other › pool": "Darts"})       # "still Darts"
    cats, unmapped = block_sales.categorize(mapping, {"Other": 80.0}, 80.0,
                                            contents={"Other": {"Darts": 50.0, "Pool": 30.0}},
                                            held={"other": {"darts"}})
    assert {c["category"]: c["net"] for c in cats} == {"Darts": 80.0} and unmapped == []
    mapping = dict(DARTS, **{"other › pool": "Pool"})        # its own line
    cats, unmapped = block_sales.categorize(mapping, {"Other": 80.0}, 80.0,
                                            contents={"Other": {"Darts": 50.0, "Pool": 30.0}},
                                            held={"other": {"darts"}})
    assert {c["category"]: c["net"] for c in cats} == {"Darts": 50.0, "Pool": 30.0} and unmapped == []


def test_unknown_contents_and_unmapped_departments_behave_as_before():
    # Not recorded yet: the department counts whole, as it always did.
    cats, unmapped = block_sales.categorize(DARTS, {"Other": 80.0}, 80.0,
                                            contents={"Other": {"Darts": 50.0, "Pool": 30.0}}, held={})
    assert {c["category"]: c["net"] for c in cats} == {"Darts": 80.0} and unmapped == []
    # An unmapped department stays one unmapped line, whatever is inside it.
    cats, unmapped = block_sales.categorize({}, {"Retail": 40.0}, 40.0, contents={"Retail": {"Shirts": 40.0}})
    assert cats == [] and unmapped == [{"department": "Retail", "net": 40.0}]
    # Dollars the contents don't explain still count with their department.
    cats, _ = block_sales.categorize(DARTS, {"Other": 100.0}, 100.0, contents={"Other": {"Darts": 60.0}},
                                     held={"other": {"darts"}})
    assert {c["category"]: c["net"] for c in cats} == {"Darts": 100.0}
    # No contents at all (a Toast night, an old night): exactly as before.
    cats, unmapped = block_sales.categorize(DARTS, {"Other": 80.0, "Beer": 5.0}, 85.0)
    assert {c["category"]: c["net"] for c in cats} == {"Darts": 80.0}
    assert unmapped == [{"department": "Beer", "net": 5.0}]


def test_the_store_records_what_a_department_held_and_never_widens_it(db_path):
    rid = _rid(db_path)
    store.set_category(rid, "Other", "Darts")                        # mapped before contents were known
    assert store.held_map(rid) == {}
    assert store.note_held(rid, {"Other": {"Darts": 50.0}, "Food": {"Entrees": 9.0}}) == ["Other"]
    assert store.held_map(rid) == {"other": {"darts"}}
    assert store.note_held(rid, {"Other": {"Darts": 50.0, "Pool": 30.0}}) == [], "a later night never widens it"
    assert store.held_map(rid) == {"other": {"darts"}}
    store.set_category(rid, "Other", "Entertainment")                # a re-map keeps what was recorded
    assert store.held_map(rid) == {"other": {"darts"}} and store.held_rows(rid) == {"Other": ["Darts"]}
    store.set_category(rid, "Beer", "Beer", held=["Draft", "Bottles"])
    assert store.held_map(rid)["beer"] == {"draft", "bottles"}
    assert store.sub_name("Other", "Pool") == "Other › Pool"
    assert store.split_sub("Other › Pool") == ("Other", "Pool") and store.split_sub("Other") == ("Other", None)


def test_rpower_reports_the_item_categories_inside_each_department(db_path, monkeypatch):
    import test_dsr_pos_day as day
    monkeypatch.setattr(pos, "PROVIDERS", None)
    monkeypatch.setattr(rpower, "REQUEST_SPACING_SECONDS", 0)
    rid = day._rpower(db_path)
    day._stub(monkeypatch)
    data, _ = pos.fetch_day_sales(rid, day.DAY)
    inside = data["by_department_category"]
    assert inside["Food"] == {"1 Entrees": 32.0} and inside["Beer"] == {"Draft": 12.0}
    for dep, total in data["by_department"].items():
        assert round(sum(inside[dep].values()), 2) == total, dep


def test_a_week_or_period_sees_the_new_item_category_too(db_path):
    rid = _rid(db_path)
    store.set_category(rid, "Other", "Darts", held=["Darts"])
    m = {"sales.net": 80.0, "sales.dep:Other": 80.0,
         "sales.depcat:Other › Darts": 50.0, "sales.depcat:Other › Pool": 30.0}
    out = rollup._day_categories(rid, m, None, db_path)
    assert out == {"Darts": 50.0, dsr.UNMAPPED: 30.0}


def test_the_mapping_route_records_what_the_department_holds(db_path, monkeypatch):
    import strategy_routes
    rid = _rid(db_path)
    monkeypatch.setattr(strategy_routes, "_dsr_owner_only", lambda u: None)
    monkeypatch.setattr(strategy_routes, "_rid", lambda u: rid)
    monkeypatch.setattr(strategy_routes, "_body", lambda: {"pos_name": "Other", "category": "Darts"})
    monkeypatch.setattr(strategy_routes, "_dsr_contents", lambda r, sold=False: {"Other": ["Darts"]})
    out, status = strategy_routes._do_dsr_category({"id": 1})
    assert status == 200 and store.held_map(rid) == {"other": {"darts"}}
    src = open(strategy_routes.__file__, encoding="utf-8").read()
    assert 'out.update(new_in=x["new_in"], mapped_to=x.get("mapped_to"))' in src
    assert '"held": store.held_rows(_rid(u)), "contents": _dsr_contents(_rid(u))' in src


def test_the_web_report_and_settings_name_what_is_new_and_what_a_department_holds():
    from pathlib import Path
    src = Path(__file__).resolve().parents[1].joinpath("templates", "dashboard.html").read_text(encoding="utf-8")
    cats = src[src.index("  function cats(d,p){"):]
    cats = cats[:cats.index("\n  }\n")]
    assert "(u.new_in?'new':'unmapped')" in cats
    assert "' is new inside '+esc(fresh[i].new_in)+', which counts as '" in cats and "Place it" in cats
    rows = src[src.index("function _dsrRenderCats(d) {"):]
    rows = rows[:rows.index("\n}\n")]
    assert "held = d.held || {}, inside = d.contents || {}" in rows
    assert "'New inside ' + _ehEsc(u.new_in) + ', which counts toward '" in rows
    assert "' &middot; holds ' + _ehEsc(shownHold)" in rows and "hold.slice(0, 3)" in rows


def test_what_a_department_holds_is_what_it_sold_not_the_pos_catalog(db_path, monkeypatch):
    """RPOWER files Gratuity, Received on Account and Tax Exempt under Simple
    EJ's "Other" beside Darts; Erik mapped "Other" to Darts for the darts.
    Held = what the department SOLD over the archive's last 90 days, so a
    category filed there but never sold is new when it first sells - and a
    monthly one (Catering) that did sell is not."""
    from datetime import date as _d
    rid = _rid(db_path)
    conn = models.get_conn(db_path)
    rows = [("L1", "2026-09-20", "M_DARTS", "sale"), ("L2", "2026-10-01", "M_CATER", "sale"),
            ("L3", "2026-06-01", "M_OLD", "sale"), ("L4", "2026-10-02", "M_DISC", "discount"),
            ("L5", "2026-10-02", "M_VOID", "void")]
    for lid, day_, item, kind in rows:
        conn.execute("INSERT INTO pos_ticket_lines (restaurant_id, provider, line_id, business_date, item_id, kind, "
                     "qty, sales) VALUES (?, 'rpower', ?, ?, ?, ?, 1, 10)", (rid, lid, day_, item, kind))
    conn.commit(); conn.close()
    menu = {"M_DARTS": {"slscat_mid": "K_D"}, "M_CATER": {"slscat_mid": "K_C"}, "M_OLD": {"slscat_mid": "K_O"},
            "M_DISC": {"slscat_mid": "K_X"}, "M_VOID": {"slscat_mid": "K_V"}}
    cats = {"K_D": {"name": "Darts", "slsdep_mid": "OTHER"}, "K_G": {"name": "Gratuity", "slsdep_mid": "OTHER"},
            "K_C": {"name": "Catering", "slsdep_mid": "FOOD"}, "K_O": {"name": "Old Menu", "slsdep_mid": "FOOD"}}
    cats.update({"K_X": {"name": "Discount All", "slsdep_mid": "DISC"}, "K_V": {"name": "Voided", "slsdep_mid": "DISC"}})
    deps = {"OTHER": {"name": "Other"}, "FOOD": {"name": "Food"}, "DISC": {"name": "Discounts"}}
    lists = {"menuitem/getbycg": menu, "salescategory/getbycg": cats, "salesdepartment/getbycg": deps}
    monkeypatch.setattr(rpower, "_catalog", lambda r, path, by_store=False: lists[path])
    sold = rpower.department_sold(rid, today=_d(2026, 10, 5))
    assert sold == {"Other": ["Darts"], "Food": ["Catering"], "Discounts": ["Discount All"]}, \
        "Gratuity never sold; Old Menu is past 90 days; a discount line counts, a void does not"
    store.set_category(rid, "Other", "Darts")
    store.note_held(rid, sold)
    cats_, unmapped = block_sales.categorize({"other": "Darts"}, {"Other": 60.0}, 60.0,
                                             contents={"Other": {"Darts": 40.0, "Gratuity": 20.0}},
                                             held=store.held_map(rid))
    assert {c["category"]: c["net"] for c in cats_} == {"Darts": 40.0}
    assert unmapped[0]["department"] == "Other \u203a Gratuity" and unmapped[0]["new_in"] == "Other"
    src = open(block_sales.__file__, encoding="utf-8").read()
    assert "if store.unknown_held(ctx.restaurant_id, db_path=ctx.db_path):" in src
    assert "store.note_held(ctx.restaurant_id, _sold(ctx.restaurant_id, contents)" in src
    import strategy_routes
    rsrc = open(strategy_routes.__file__, encoding="utf-8").read()
    assert "held=_dsr_contents(_rid(u), sold=True).get(name)" in rsrc
    assert '"contents": _dsr_contents(_rid(u))' in rsrc, "a page load reads no POS catalog"
