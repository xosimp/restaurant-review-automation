"""The DSR's web views (phase 5): what the screens call beyond reading a
night — the Home card's summary, the owner's seven-night Close day window,
Erik's weekly grid as an .xlsx, and the owner-only settings the grid needs
(budget, category map, fiscal calendar).

Every route is checked for the owner, the manager, a login with no console,
another tenant, and on its mobile twin.
"""
import io
import sys
import types
import zipfile
import xml.etree.ElementTree as ET
from datetime import date, timedelta

import pytest
from flask import Flask

import auth
import models
import pos
import dsr
from dsr import pipeline, rollup, store, xlsx
from models import Restaurant, create_restaurant, update_restaurant, get_restaurant

WED = date(2026, 9, 16)
TUE = date(2026, 9, 22)
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    monkeypatch.setattr(pos, "PROVIDERS", None)
    return db_path


@pytest.fixture
def client(db):
    from strategy_routes import strategy_bp, strategy_mobile_bp
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(strategy_bp)
    app.register_blueprint(strategy_mobile_bp)
    return app.test_client()


def _as(monkeypatch, rid, role="client"):
    monkeypatch.setattr(auth, "get_current_user", lambda: {
        "id": 7, "restaurant_id": rid, "is_admin": 0, "role": role, "username": "u", "email": "u@x.com"})


def _ejs(db, name="Simple EJ's"):
    rid = create_restaurant(Restaurant(name=name, owner_email="e@x.com", timezone="America/Chicago"), db_path=db)
    update_restaurant(rid, {"fiscal_week_start_dow": 2, "fiscal_year_start": "2026-01-14",
                            "fiscal_period_scheme": "4x13"}, db_path=db)
    return get_restaurant(rid, db_path=db)


def _night(db, rid, day, net, gross, cats, unmapped=None, narrative=None, note=None):
    r = store.create_report(rid, day, trigger="sweep", db_path=db)
    metrics = dict({"net": net, "gross": gross, "budget_net": 5200.0}, **{f"cat:{k}": v for k, v in cats.items()})
    store.save_block(r["id"], "sales", dsr.block(dsr.READY, source="rpower", metrics=metrics,
                                                 detail={"unmapped": unmapped or []}), db_path=db)
    store.save_block(r["id"], "labor", dsr.block(dsr.READY, source="rpower", metrics={"cost": 1400.0, "pct": 28.0}),
                     db_path=db)
    if narrative is not None:
        store.save_narrative(r["id"], narrative, db_path=db)
    if note is not None:
        store.note(r["id"], "narrative", note, db_path=db)
    store.set_stage(r["id"], "collecting", db_path=db)
    store.set_stage(r["id"], "final", db_path=db)
    return r


def _mobile_headers(db, rid, role="client"):
    auth.init_auth(db_path=db)
    uid = auth.create_user(rid, f"{role}-{rid}", f"{role}{rid}@x.com", "pw", role=role, db_path=db)
    return {"Authorization": f"Bearer {auth.create_session(uid, db_path=db)}"}


# ── Home's "Last night" card ────────────────────────────────────────────────

def test_the_list_carries_net_and_the_lead_as_this_login_may_read_it(client, db, monkeypatch):
    r = _ejs(db)
    _night(db, r.id, WED, 5000.0, 5300.0, {"Food": 5000.0}, narrative={
        "executive_summary": {"text": "Net sales were $5,000.", "cites": ["sales.net"]}})
    _night(db, r.id, date(2026, 9, 17), 6000.0, 6400.0, {"Food": 6000.0},
           note={"status": "skipped", "reason": "Not enough data tonight for a summary."})
    _as(monkeypatch, r.id, "client")
    rows = client.get("/api/dsr").get_json()["reports"]
    assert [(x["business_date"], x["net"], x["lead"], x["lead_missing"]) for x in rows] == [
        ("2026-09-17", 6000.0, None, "Not enough data tonight for a summary."),
        ("2026-09-16", 5000.0, "Net sales were $5,000.", None)]


def test_each_version_says_when_it_went_out_on_the_restaurant_s_clock(client, db, monkeypatch):
    r = _ejs(db)
    _night(db, r.id, WED, 5000.0, 5300.0, {"Food": 5000.0})
    conn = models.get_conn(db)
    conn.execute("UPDATE dsr_reports SET finalized_at='2026-09-17 09:10:00', created_at='2026-09-17 09:00:00' "
                 "WHERE restaurant_id=?", (r.id,))
    conn.commit()
    conn.close()
    _as(monkeypatch, r.id, "manager")
    v = client.get("/api/dsr/2026-09-16").get_json()["versions"][0]
    assert (v["finalized_at_local"], v["created_at_local"]) == ("2026-09-17T04:10", "2026-09-17T04:00")   # Chicago, CDT


def test_a_night_whose_sales_are_not_ready_has_no_net_never_zero(client, db, monkeypatch):
    r = _ejs(db)
    rep = store.create_report(r.id, WED, trigger="sweep", db_path=db)
    store.save_block(rep["id"], "sales", dsr.block(dsr.AWAITING, source="rpower", block_name="sales"), db_path=db)
    store.set_stage(rep["id"], "provisional", db_path=db)
    _as(monkeypatch, r.id, "manager")
    row = client.get("/api/dsr").get_json()["reports"][0]
    assert row["net"] is None and row["missing"] == ["Awaiting POS synchronization"]


# ── Close day: the owner's seven nights ─────────────────────────────────────

@pytest.fixture
def fake_night(monkeypatch):
    monkeypatch.setattr(pipeline, "_spawn", lambda fn: fn())
    monkeypatch.setattr(pos, "connected_provider", lambda rid: ("fakepos", object()))
    monkeypatch.setattr(pos, "fetch_day_sales", lambda rid, day: ({
        "gross": 1000.0, "net": 950.0, "transactions": 40, "guests": None, "discounts": 50.0, "comps": 0.0,
        "voids": 0.0, "refunds": 0.0, "tax": 80.0, "by_department": {}, "by_hour": {}, "items": [],
        "net_deductions": ["discounts", "comps"], "source_checks": {}}, "fakepos"))
    for name in ("food", "reviews", "marketing", "intel", "closeout"):
        mod = types.ModuleType(f"dsr.block_{name}")
        mod.collect = lambda ctx: dsr.block(dsr.READY, source="fake")
        monkeypatch.setitem(sys.modules, f"dsr.block_{name}", mod)
    narrative = types.ModuleType("dsr.narrative")
    narrative.write = lambda ctx, facts: {"ok": False, "narrative": None, "reason": "test"}
    monkeypatch.setitem(sys.modules, "dsr.narrative", narrative)


def _today(db, rid):
    import closeout
    return closeout.business_date_for(get_restaurant(rid, db_path=db))


def test_an_owner_may_run_any_of_the_last_seven_nights(client, db, monkeypatch, fake_night):
    r = _ejs(db)
    today = _today(db, r.id)
    _as(monkeypatch, r.id, "client")
    six_back = today - timedelta(days=6)
    resp = client.post("/api/dsr/close", json={"date": six_back.isoformat()})
    assert resp.status_code == 202 and resp.get_json()["business_date"] == six_back.isoformat()
    assert store.get_report(r.id, six_back, db_path=db)["trigger"] == "manual"
    # A finished night is re-run as a new version, not silently rewritten.
    assert client.post("/api/dsr/close", json={"date": six_back.isoformat()}).get_json()["started"] is False
    assert client.post("/api/dsr/close", json={"date": six_back.isoformat(), "rerun": True}).status_code == 202
    assert [v["version"] for v in store.versions(r.id, six_back, db_path=db)] == [1, 2]


@pytest.mark.parametrize("days_back", [7, 30, -1])
def test_an_owner_is_refused_outside_the_window(client, db, monkeypatch, fake_night, days_back):
    r = _ejs(db)
    today = _today(db, r.id)
    _as(monkeypatch, r.id, "client")
    resp = client.post("/api/dsr/close", json={"date": (today - timedelta(days=days_back)).isoformat()})
    assert resp.status_code == 400 and "last 7 nights" in resp.get_json()["error"]
    assert store.get_report(r.id, today - timedelta(days=days_back), db_path=db) is None


def test_a_manager_keeps_tonight_and_last_night_and_may_never_rerun(client, db, monkeypatch, fake_night):
    r = _ejs(db)
    today = _today(db, r.id)
    _as(monkeypatch, r.id, "manager")
    assert client.post("/api/dsr/close", json={"date": (today - timedelta(days=2)).isoformat()}).status_code == 400
    assert client.post("/api/dsr/close", json={"date": (today - timedelta(days=6)).isoformat()}).status_code == 400
    assert client.post("/api/dsr/close", json={"date": (today - timedelta(days=1)).isoformat()}).status_code == 202
    # A re-run is refused before anything else is looked at — in the window or out of it.
    for back in (1, 3):
        resp = client.post("/api/dsr/close", json={"date": (today - timedelta(days=back)).isoformat(), "rerun": True})
        assert resp.status_code == 403
    assert [v["version"] for v in store.versions(r.id, today - timedelta(days=1), db_path=db)] == [1]


def test_close_day_on_the_mobile_twin(client, db, fake_night):
    r = _ejs(db)
    today = _today(db, r.id)
    headers = _mobile_headers(db, r.id, "client")
    resp = client.post("/mobile/api/dsr/close", json={"date": (today - timedelta(days=5)).isoformat()},
                       headers=headers)
    assert resp.status_code == 202
    mgr = _mobile_headers(db, r.id, "manager")
    assert client.post("/mobile/api/dsr/close", json={"date": (today - timedelta(days=5)).isoformat()},
                       headers=mgr).status_code == 400


# ── the .xlsx writer ────────────────────────────────────────────────────────

def _cells(data):
    """{ref: (value, style)} from the workbook's one sheet, parsed as XML."""
    z = zipfile.ZipFile(io.BytesIO(data))
    sheet = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
    out = {}
    for c in sheet.iter("{%s}c" % NS["m"]):
        t = c.find("m:is/m:t", NS)
        v = c.find("m:v", NS)
        val = t.text if t is not None else (float(v.text) if v is not None else None)
        out[c.get("r")] = (val, int(c.get("s") or 0))
    return out, sheet, z


def test_the_writer_makes_a_real_workbook():
    data = xlsx.workbook("Period 9 · Week 4", [
        [("Title", "title")],
        [("Day", "header"), ("Net", "header")],
        ["Wed 9/16/26", (5000.0, "money")],
        ["Thu 9/17/26", None],
        ["A & B <c>", (0.274, "pct")],
    ], widths=[16, 13], freeze_rows=2, merges=["A1:B1"])
    z = zipfile.ZipFile(io.BytesIO(data))
    assert z.testzip() is None
    assert set(z.namelist()) == {"[Content_Types].xml", "_rels/.rels", "xl/workbook.xml",
                                 "xl/_rels/workbook.xml.rels", "xl/styles.xml", "xl/worksheets/sheet1.xml"}
    for part in z.namelist():
        ET.fromstring(z.read(part))                                  # every part is well-formed XML
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    assert wb.find("m:sheets/m:sheet", NS).get("name") == "Period 9 · Week 4"
    cells, sheet, _z = _cells(data)
    assert cells["A1"] == ("Title", xlsx.STYLES["title"])
    assert cells["B3"] == (5000.0, xlsx.STYLES["money"])
    assert "B4" not in cells                                         # unknown is an empty cell, never a 0
    assert cells["A5"][0] == "A & B <c>" and cells["B5"] == (0.274, xlsx.STYLES["pct"])
    pane = sheet.find("m:sheetViews/m:sheetView/m:pane", NS)
    assert (pane.get("ySplit"), pane.get("state")) == ("2", "frozen")
    assert [c.get("width") for c in sheet.find("m:cols", NS)] == ["16.0", "13.0"]
    assert sheet.find("m:mergeCells/m:mergeCell", NS).get("ref") == "A1:B1"
    styles = ET.fromstring(z.read("xl/styles.xml"))
    fmts = {f.get("numFmtId"): f.get("formatCode") for f in styles.find("m:numFmts", NS)}
    assert fmts["164"] == '"$"#,##0' and fmts["165"] == "0.0%"
    xfs = styles.find("m:cellXfs", NS)
    assert int(xfs.get("count")) == len(list(xfs)) == len(xlsx.STYLES)
    assert xfs[xlsx.STYLES["money"]].get("numFmtId") == "164" and xfs[xlsx.STYLES["pct"]].get("numFmtId") == "165"
    assert xfs[xlsx.STYLES["header"]].get("fontId") == "1"           # bold
    assert [xlsx.col_letter(i) for i in (0, 25, 26, 27)] == ["A", "Z", "AA", "AB"]
    with pytest.raises(ValueError):
        xlsx.workbook("x", [[(1, "nope")]])


def test_the_week_workbook_is_the_grid_s_layout(db):
    r = _ejs(db)
    _night(db, r.id, WED, 5000.0, 5300.0, {"Food": 3500.0, "Liquor": 1500.0})
    store.set_budget(r.id, WED, gross=5500, net=5200, db_path=db)
    grid = rollup.week(r, WED)
    cells, _sheet, _z = _cells(xlsx.week_workbook(grid, "Simple EJ's"))
    assert cells["A1"][0] == "Simple EJ's · Period 9 · Week 4 · 9/16/26 – 9/22/26"
    header = [cells[f"{xlsx.col_letter(i)}2"][0] for i in range(len(xlsx.columns(grid)) + 4)]
    assert header == ["Day", "Food", "Liquor", "Beer", "Wine", "Retail", "NA Beverage", "Gross", "Net",
                      "Budget gross", "Budget net", "vs Budget", "vs Budget %", "Last year", "vs LY", "vs LY %",
                      "Labor %", "Weather", "Event", "Influence"]
    col = {h: xlsx.col_letter(i) for i, h in enumerate(header)}
    assert cells["A3"][0] == "Wed 9/16/26" and cells["A9"][0] == "Tue 9/22/26"
    assert cells[col["Net"] + "3"] == (5000.0, xlsx.STYLES["money"])
    assert cells[col["Budget net"] + "3"] == (5200.0, xlsx.STYLES["money"])
    assert cells[col["vs Budget"] + "3"] == (-200.0, xlsx.STYLES["delta"])
    assert cells[col["vs Budget %"] + "3"] == (-0.038, xlsx.STYLES["pct"])
    assert cells[col["Labor %"] + "3"] == (0.28, xlsx.STYLES["pct"])
    assert col["Net"] + "4" not in cells                             # Thursday wasn't measured: empty
    assert cells["A10"] == ("Week total", xlsx.STYLES["bold"])
    assert cells[col["Net"] + "10"] == (5000.0, xlsx.STYLES["money_bold"])
    assert cells["A11"][0] == "Period to date (8/26/26 – 9/22/26)"
    assert "not $0" in cells["A13"][0]


def test_the_manager_workbook_has_no_budget(db):
    from dsr import access
    r = _ejs(db)
    _night(db, r.id, WED, 5000.0, 5300.0, {"Food": 5000.0})
    store.set_budget(r.id, WED, gross=5500, net=5200, db_path=db)
    grid = access.redact_grid(rollup.week(r, WED), {"role": "manager"})
    cells, _s, _z = _cells(xlsx.week_workbook(grid, "Simple EJ's"))
    header = [v for ref, (v, _st) in cells.items() if ref[1:] == "2"]
    assert header[:1] == ["Day"] and "Net" in header and "Last year" in header
    assert not [h for h in header if "Budget" in h]
    assert 5200.0 not in {v for v, _st in cells.values()} and 5500.0 not in {v for v, _st in cells.values()}


# ── the .xlsx route ─────────────────────────────────────────────────────────

def test_the_xlsx_route_for_each_login(client, db, monkeypatch):
    r = _ejs(db)
    _night(db, r.id, WED, 5000.0, 5300.0, {"Food": 5000.0})
    store.set_budget(r.id, WED, gross=5500, net=5200, db_path=db)
    _as(monkeypatch, r.id, "client")
    resp = client.get("/api/dsr/week.xlsx?date=2026-09-18")
    assert resp.status_code == 200
    assert resp.mimetype == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert resp.headers["Content-Disposition"] == 'attachment; filename="Simple-EJ-s-Period-9-Week-4-9-16-26.xlsx"'
    assert resp.headers["Cache-Control"] == "no-store"
    cells, _s, _z = _cells(resp.data)
    assert 5200.0 in {v for v, _st in cells.values()}
    assert client.get("/api/dsr/week.xlsx?date=9-16-26").status_code == 400
    _as(monkeypatch, r.id, "manager")
    cells, _s, _z = _cells(client.get("/api/dsr/week.xlsx?date=2026-09-16").data)
    assert 5000.0 in {v for v, _st in cells.values()} and 5200.0 not in {v for v, _st in cells.values()}
    for role in ("employee", "support"):
        _as(monkeypatch, r.id, role)
        resp = client.get("/api/dsr/week.xlsx?date=2026-09-16")
        assert resp.status_code == 403 and resp.mimetype == "application/json"
    other = _ejs(db, "Elsewhere")
    _as(monkeypatch, other.id, "client")
    cells, _s, _z = _cells(client.get("/api/dsr/week.xlsx?date=2026-09-16").data)
    assert cells["A1"][0].startswith("Elsewhere")
    assert not [v for v, _st in cells.values() if isinstance(v, float)]      # nothing of Simple EJ's


def test_the_xlsx_route_on_the_mobile_twin(client, db):
    r = _ejs(db)
    _night(db, r.id, WED, 5000.0, 5300.0, {"Food": 5000.0})
    resp = client.get("/mobile/api/dsr/week.xlsx?date=2026-09-16", headers=_mobile_headers(db, r.id))
    assert resp.status_code == 200 and zipfile.ZipFile(io.BytesIO(resp.data)).testzip() is None
    assert client.get("/mobile/api/dsr/week.xlsx?date=2026-09-16").status_code == 401


# ── budget ──────────────────────────────────────────────────────────────────

def test_the_owner_sets_a_week_of_budget(client, db, monkeypatch):
    r = _ejs(db)
    _as(monkeypatch, r.id, "client")
    resp = client.post("/api/dsr/budget", json={"date": "2026-09-16", "gross": 7500, "net": "7000"})
    assert resp.status_code == 200 and resp.get_json()["saved"] == [
        {"date": "2026-09-16", "label": "9/16/26", "gross": 7500.0, "net": 7000.0}]
    resp = client.post("/api/dsr/budget", json={"days": [{"date": "2026-09-17", "gross": 9500, "net": None},
                                                         {"date": "2026-09-16", "gross": "", "net": 6900}]})
    assert resp.status_code == 200
    assert store.budgets_for(r.id, WED, TUE, db_path=db) == {
        "2026-09-16": {"gross": None, "net": 6900.0}, "2026-09-17": {"gross": 9500.0, "net": None}}
    for bad in ({"date": "9/16/26", "gross": 1}, {"date": "2026-09-16", "gross": -5},
                {"date": "2026-09-16", "net": "lots"}, {"date": "2026-09-16", "gross": True},
                {"days": [{"date": "2026-09-16"}] * 15}, {"days": ["x"]}):
        assert client.post("/api/dsr/budget", json=bad).status_code == 400, bad
    assert store.budgets_for(r.id, WED, TUE, db_path=db)["2026-09-16"]["net"] == 6900.0


@pytest.mark.parametrize("role, status", [("manager", 403), ("member", 403), ("employee", 403), ("support", 403)])
def test_only_the_owner_sets_the_budget(client, db, monkeypatch, role, status):
    r = _ejs(db)
    _as(monkeypatch, r.id, role)
    assert client.post("/api/dsr/budget", json={"date": "2026-09-16", "gross": 1}).status_code == status
    assert store.budgets_for(r.id, WED, TUE, db_path=db) == {}


def test_a_budget_is_always_the_session_s_own_restaurant(client, db, monkeypatch):
    mine, theirs = _ejs(db, "Lakeview"), _ejs(db, "Wicker Park")
    _as(monkeypatch, mine.id, "client")
    client.post("/api/dsr/budget", json={"date": "2026-09-16", "gross": 100, "restaurant_id": theirs.id})
    assert store.budgets_for(theirs.id, WED, TUE, db_path=db) == {}
    assert store.budgets_for(mine.id, WED, TUE, db_path=db)["2026-09-16"]["gross"] == 100.0
    resp = client.post("/mobile/api/dsr/budget", json={"date": "2026-09-17", "net": 50},
                       headers=_mobile_headers(db, theirs.id))
    assert resp.status_code == 200 and store.budgets_for(theirs.id, WED, TUE, db_path=db)["2026-09-17"]["net"] == 50.0
    assert client.post("/mobile/api/dsr/budget", json={"date": "2026-09-17", "net": 50},
                       headers=_mobile_headers(db, theirs.id, "manager")).status_code == 403


# ── category map ────────────────────────────────────────────────────────────

def test_the_owner_maps_a_department(client, db, monkeypatch):
    r = _ejs(db)
    _as(monkeypatch, r.id, "client")
    resp = client.post("/api/dsr/category", json={"pos_name": "DRAFT BEER", "category": "beer"})
    assert resp.status_code == 200 and resp.get_json()["category"] == "Beer"       # Erik's label, his casing
    assert client.post("/api/dsr/category", json={"pos_name": "ROOM", "category": " Room  rental "}).get_json()[
        "category"] == "Room rental"
    assert store.category_for("draft beer", store.category_map(r.id, db_path=db)) == "Beer"
    for bad in ({"pos_name": "", "category": "Beer"}, {"pos_name": "X", "category": ""},
                {"pos_name": "X", "category": "unmapped"}, {"pos_name": "X" * 121, "category": "Beer"},
                {"pos_name": 5, "category": "Beer"}):
        assert client.post("/api/dsr/category", json=bad).status_code == 400, bad
    for role in ("manager", "employee"):
        _as(monkeypatch, r.id, role)
        assert client.post("/api/dsr/category", json={"pos_name": "WINE", "category": "Wine"}).status_code == 403
    assert "wine" not in store.category_map(r.id, db_path=db)
    other = _ejs(db, "Elsewhere")
    assert store.category_map(other.id, db_path=db) == {}
    resp = client.post("/mobile/api/dsr/category", json={"pos_name": "WINE", "category": "Wine"},
                       headers=_mobile_headers(db, other.id))
    assert resp.status_code == 200 and store.category_map(other.id, db_path=db) == {"wine": "Wine"}
    assert "wine" not in store.category_map(r.id, db_path=db)


# ── settings ────────────────────────────────────────────────────────────────

def test_the_owner_reads_the_settings_and_the_latest_unmapped(client, db, monkeypatch):
    r = _ejs(db)
    store.set_category(r.id, "FOOD", "Food", db_path=db)
    _night(db, r.id, WED, 5000.0, 5300.0, {"Food": 4000.0}, unmapped=[{"department": "KIOSK", "net": 1000.0}])
    _as(monkeypatch, r.id, "client")
    body = client.get("/api/dsr/settings").get_json()
    assert body["settings"]["fiscal_week_start_dow"] == 2 and body["settings"]["fiscal_year_start"] == "2026-01-14"
    assert body["settings"]["fiscal_period_scheme"] == "4x13" and body["settings"]["dsr_deadline_hour"] == 4
    assert body["settings"]["dsr_enabled"] is True and body["settings"]["calendar_label"].startswith("Period ")
    assert body["categories"] == list(dsr.DEFAULT_CATEGORIES)
    assert body["category_map"] == [{"pos_name": "food", "category": "Food"}]
    assert body["unmapped"] == [{"department": "KIOSK", "net": 1000.0}] and body["unmapped_as_of"] == "9/16/26"
    for role in ("manager", "employee"):
        _as(monkeypatch, r.id, role)
        assert client.get("/api/dsr/settings").status_code == 403
    assert client.get("/mobile/api/dsr/settings", headers=_mobile_headers(db, r.id)).get_json()["can_edit"] is True


def test_the_owner_changes_the_calendar(client, db, monkeypatch):
    r = _ejs(db)
    _as(monkeypatch, r.id, "client")
    resp = client.post("/api/dsr/settings", json={"fiscal_week_start_dow": 0, "fiscal_year_start": "2026-01-05",
                                                  "fiscal_period_scheme": "445", "dsr_deadline_hour": 5,
                                                  "dsr_enabled": False})
    assert resp.status_code == 200
    got = get_restaurant(r.id, db_path=db)
    assert (got.fiscal_week_start_dow, got.fiscal_year_start, got.fiscal_period_scheme, got.dsr_deadline_hour,
            got.dsr_enabled) == (0, "2026-01-05", "445", 5, 0)
    # Period 1 must start on the week's first day (1/14/26 is a Wednesday, the week now starts Monday).
    bad = client.post("/api/dsr/settings", json={"fiscal_year_start": "2026-01-14"})
    assert bad.status_code == 400 and "Monday" in bad.get_json()["error"]
    for payload in ({"fiscal_week_start_dow": 7}, {"fiscal_week_start_dow": "Wed"}, {"fiscal_period_scheme": "5x10"},
                    {"dsr_deadline_hour": 14}, {"fiscal_year_start": "1/14/26"}, {}):
        assert client.post("/api/dsr/settings", json=payload).status_code == 400, payload
    resp = client.post("/api/dsr/settings", json={"fiscal_year_start": ""})
    assert resp.status_code == 200 and resp.get_json()["settings"]["calendar_label"].startswith("Week of ")
    assert get_restaurant(r.id, db_path=db).fiscal_year_start is None
    _as(monkeypatch, r.id, "manager")
    assert client.post("/api/dsr/settings", json={"dsr_enabled": True}).status_code == 403
    assert get_restaurant(r.id, db_path=db).dsr_enabled == 0
