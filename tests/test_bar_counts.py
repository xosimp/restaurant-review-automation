"""Bar counts and counted pour cost (barfix-1015 W6a, audit 10/14/26 F1:
FIN-13, AIX-18). Pour cost = (begin count + purchases − end count) ÷ sales,
withheld unless every part was measured; the counts are entered by the
account holder and FOOD_COST_ENTER logins, the dollars shown only with
FOOD_COST_VIEW; Back Office's valuation report arrives by email; a month-end
reminder asks for the count."""
import os
import sqlite3
from datetime import date, datetime, timedelta

import pytest
from flask import Flask

import auth
import bar.counts as bcn
import client_api
import mobile_api
import models
from models import Restaurant, create_restaurant

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def db(db_path, monkeypatch):
    real, default = models.get_conn, models.DB_PATH

    def redirected(path=None, *a, **k):
        return real(db_path if path in (None, default) else path)
    for mod in (models, auth, client_api, mobile_api):
        monkeypatch.setattr(mod, "get_conn", redirected)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    import ops
    ops._claim_fallback.clear()
    return db_path


def _rid(db, **kw):
    kw.setdefault("module_inventory", 1)
    return create_restaurant(Restaurant(name=kw.pop("name", "EJ's"), owner_email="o@x.test", **kw), db_path=db)


def _conn(db):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    return c


def _invoice(db, rid, dist, number, day, lines):
    """One distributor invoice with its lines [(description, uom, cost, deposit, kind)]."""
    c = _conn(db)
    try:
        inv = c.execute("INSERT INTO fintech_invoices (restaurant_id, distributor, invoice_number, invoice_date, total, "
                        "source_format, updated_at) VALUES (?,?,?,?,?, 'export', '2026-10-01 00:00:00')",
                        (rid, dist, number, day, sum(l[2] for l in lines))).lastrowid
        cols = {r["name"] for r in c.execute("PRAGMA table_info(fintech_invoice_lines)")}
        for i, (desc, uom, cost, deposit, kind) in enumerate(lines):
            row = {"invoice_id": inv, "line_no": i + 1, "item_code": f"{number}-{i}", "description": desc,
                   "uom": uom, "qty": 1, "unit_price": cost, "cost": cost, "deposit": deposit, "kind": kind}
            if "restaurant_id" in cols:
                row["restaurant_id"] = rid
            c.execute(f"INSERT INTO fintech_invoice_lines ({','.join(row)}) VALUES ({','.join('?' * len(row))})",
                      tuple(row.values()))
        c.commit()
    finally:
        c.close()


def _sales(db, rid, day, beer=None, liquor=None, wine=None):
    c = _conn(db)
    try:
        for cat, v in (("Beer", beer), ("Liquor", liquor), ("Wine", wine)):
            if v is not None:
                c.execute("INSERT INTO dsr_metrics (restaurant_id, business_date, metric, value, status, source) "
                          "VALUES (?,?,?,?, 'ready', 'rpower')", (rid, day, "sales.cat:" + cat, v))
        c.commit()
    finally:
        c.close()


def _days(a, b):
    d = date.fromisoformat(a)
    while d <= date.fromisoformat(b):
        yield d.isoformat()
        d += timedelta(days=1)


def _october(db, rid, beer_per_day=1000.0):
    """Five weekly beer invoices of $1,200 (6,000) dated 10/3–10/31 and
    beer sales of $1,000 a night 10/1–10/30 (30,000; 10/31 a night with no
    beer sold)."""
    for i, day in enumerate(("2026-10-03", "2026-10-10", "2026-10-17", "2026-10-24", "2026-10-31")):
        _invoice(db, rid, "Chicago Beverage", f"CB{i}", day, [("DAISY CUTTER 1/2BBL KEG", "KE", 1200.0, None, "item")])
    for d in _days("2026-10-01", "2026-10-31"):
        _sales(db, rid, d, beer=0.0 if d == "2026-10-31" else beer_per_day)


def _save(db, rid, day, **values):
    return bcn.save_count(rid, day, values, db_path=db, today=date(2026, 12, 1))


# ── the identity ────────────────────────────────────────────────────────────

def test_true_pour_cost_identity(db):
    """5,000 + 6,000 − 4,500 = 6,500 used, over 30,000 of beer sales = 21.7%."""
    rid = _rid(db)
    _october(db, rid)
    _save(db, rid, "2026-09-30", beer=5000)
    _save(db, rid, "2026-10-31", beer=4500)
    out = bcn.true_pour_cost(rid, "2026-09-30", "2026-10-31", db_path=db)
    beer = out["categories"]["beer"]
    assert (beer["begin"], beer["purchases"], beer["end"], beer["used"], beer["sales"]) == (5000, 6000, 4500, 6500, 30000)
    assert beer["pct"] == 21.7 and beer["missing"] == []
    assert out["ok"] is True and out["begin_date"] == "2026-09-30" and out["end_date"] == "2026-10-31"
    # Liquor and wine were never counted: withheld, and so is the total.
    assert out["categories"]["liquor"]["pct"] is None and out["categories"]["liquor"]["missing"]
    assert out["total"] is None


def test_counted_pour_cost_between_two_counts(db):
    """FIN-13's example: begin 1,000 + bought 500 − end 900 = 600 used; 600 ÷ 3,000 = 20.0%."""
    rid = _rid(db)
    for i, day in enumerate(("2026-10-02", "2026-10-04", "2026-10-06", "2026-10-08", "2026-10-10")):
        _invoice(db, rid, "Southern Glazer's", f"SG{i}", day, [("TITOS VODKA 1L", "CA", 100.0, None, "item")])
    for d in _days("2026-10-01", "2026-10-10"):
        _sales(db, rid, d, liquor=300.0)
    _save(db, rid, "2026-09-30", liquor=1000)
    _save(db, rid, "2026-10-10", liquor=900)
    liquor = bcn.true_pour_cost(rid, "2026-09-30", "2026-10-10", db_path=db)["categories"]["liquor"]
    assert liquor["used"] == 600 and liquor["sales"] == 3000 and liquor["pct"] == 20.0


def test_true_pour_cost_withholds_without_two_counts(db):
    rid = _rid(db)
    _october(db, rid)
    out = bcn.true_pour_cost(rid, "2026-09-30", "2026-10-31", db_path=db)
    assert out["ok"] is False and out["categories"]["beer"]["pct"] is None
    assert out["missing"] and out["missing"][0]["component"] == "two counts"
    _save(db, rid, "2026-09-30", beer=5000)
    out = bcn.true_pour_cost(rid, "2026-09-30", "2026-10-31", db_path=db)
    assert out["categories"]["beer"]["pct"] is None
    assert [m["component"] for m in out["categories"]["beer"]["missing"]] == ["closing count"]
    block = bcn.counted(rid, today=date(2026, 10, 15), db_path=db)
    assert block["available"] is False and block["periods"] == [] and "one bar count so far" in block["missing"][0]["why"]


def test_a_count_off_the_window_edge_withholds(db):
    rid = _rid(db)
    _october(db, rid)
    _save(db, rid, "2026-09-30", beer=5000)
    _save(db, rid, "2026-11-15", beer=4500)          # 15 days past the window's end
    out = bcn.true_pour_cost(rid, "2026-09-30", "2026-10-31", db_path=db)
    beer = out["categories"]["beer"]
    assert beer["pct"] is None and beer["end_date"] is None
    assert any(m["component"] == "closing count" and "within 10 days of 10/31/26" in m["why"] for m in beer["missing"])
    # Ten days off still brackets it.
    _save(db, rid, "2026-11-10", beer=4500)
    assert bcn.true_pour_cost(rid, "2026-09-30", "2026-10-31", db_path=db)["categories"]["beer"]["end_date"] == "2026-11-10"


def test_keg_deposits_are_never_in_purchases(db):
    """A keg's $30 deposit inside its cost, and the empty's credit, are
    never what the beer cost (bar.purchases._lines nets them)."""
    rid = _rid(db)
    for i, day in enumerate(("2026-10-03", "2026-10-10", "2026-10-17", "2026-10-24")):
        _invoice(db, rid, "Chicago Beverage", f"K{i}", day,
                 [("MODELO ESP 1/2 BBL KE", "KE", 210.0, 30.0, "item"), ("POS EMPTY KEG", "EA", -30.0, None, "keg_credit")])
    for d in _days("2026-10-01", "2026-10-28"):
        _sales(db, rid, d, beer=100.0)
    _save(db, rid, "2026-09-30", beer=1000)
    _save(db, rid, "2026-10-28", beer=1000)
    beer = bcn.true_pour_cost(rid, "2026-09-30", "2026-10-28", db_path=db)["categories"]["beer"]
    assert beer["purchases"] == 4 * 180.0, "deposits and empties stay out of beer"
    assert beer["used"] == 720.0 and beer["pct"] == round(720 / 2800 * 100, 1)


def test_a_distributor_gap_and_thin_sales_withhold(db):
    rid = _rid(db)
    # Weekly beer invoices that stop after 10/10: the rest of the month is a gap.
    for i, day in enumerate(("2026-09-12", "2026-09-19", "2026-09-26", "2026-10-03", "2026-10-10")):
        _invoice(db, rid, "Chicago Beverage", f"G{i}", day, [("DAISY CUTTER 1/2BBL KEG", "KE", 1200.0, None, "item")])
    for d in _days("2026-10-01", "2026-10-31"):
        _sales(db, rid, d, beer=1000.0)
    _save(db, rid, "2026-09-30", beer=5000)
    _save(db, rid, "2026-10-31", beer=4500)
    beer = bcn.true_pour_cost(rid, "2026-09-30", "2026-10-31", db_path=db)["categories"]["beer"]
    assert beer["pct"] is None and any("Chicago Beverage" in m["why"] for m in beer["missing"])

    rid2 = _rid(db, name="Thin")
    _october(db, rid2)
    c = _conn(db)
    c.execute("DELETE FROM dsr_metrics WHERE restaurant_id=? AND business_date > '2026-10-26'", (rid2,))
    c.commit(); c.close()
    _save(db, rid2, "2026-09-30", beer=5000)
    _save(db, rid2, "2026-10-31", beer=4500)
    beer = bcn.true_pour_cost(rid2, "2026-09-30", "2026-10-31", db_path=db)["categories"]["beer"]
    assert beer["pct"] is None and any(m["component"] == "sales" and "26 of 31 trading days" in m["why"]
                                       for m in beer["missing"])


def test_counted_block_and_latest_counted(db):
    rid = _rid(db)
    _october(db, rid)
    _save(db, rid, "2026-09-30", beer=5000)
    _save(db, rid, "2026-10-31", beer=4500)
    block = bcn.counted(rid, today=date(2026, 11, 2), db_path=db)
    assert block["available"] is True and block["last_count_mdy"] == "10/31/26"
    assert block["next_due_mdy"] == "11/30/26"
    (p,) = block["periods"]
    assert p["from_mdy"] == "9/30/26" and p["to_mdy"] == "10/31/26" and p["categories"]["beer"]["pct"] == 21.7
    assert set(p["categories"]["beer"]) == {"begin", "purchases", "end", "used", "sales", "pct", "begin_date", "end_date"}
    assert bcn.latest_counted(rid, block=block)["to"] == "2026-10-31"
    assert bcn.latest_counted(rid, block=block, window={"from": "2026-10-04", "to": "2026-10-31"}) is not None
    assert bcn.latest_counted(rid, block=block, window={"from": "2026-11-01", "to": "2026-11-28"}) is None
    # A month already counted at its end points to the next month's end.
    assert bcn.next_due("2026-10-31", date(2026, 10, 31)) == date(2026, 11, 30)


def test_the_bar_payload_carries_counted(db):
    import bar_cost
    rid = _rid(db)
    _october(db, rid)
    out = bar_cost.build(rid, db_path=db, today=date(2026, 11, 2))
    assert out["counted"]["available"] is False and out["counted"]["next_due_mdy"] == "11/30/26"


# ── saving: one value at a time ─────────────────────────────────────────────

def test_a_count_saves_per_value_never_as_a_whole_list(db):
    rid = _rid(db)
    _save(db, rid, "2026-10-31", beer=4500, liquor=7000, wine=2000)
    _save(db, rid, "2026-10-31", liquor="6,950.50")
    (row,) = bcn.history(rid, db_path=db)
    assert row["values"] == {"beer": 4500.0, "liquor": 6950.5, "wine": 2000.0}
    _save(db, rid, "2026-10-31", beer=4400, wine="")   # blank leaves it as it is
    assert bcn.history(rid, db_path=db)[0]["values"] == {"beer": 4400.0, "liquor": 6950.5, "wine": 2000.0}
    with pytest.raises(ValueError):
        _save(db, rid, "2026-10-31", wine="")          # nothing to save is said, not a silent no-op
    with pytest.raises(ValueError):
        _save(db, rid, "2026-10-31", beer=-5)
    with pytest.raises(ValueError):
        _save(db, rid, "10/31/26", beer=5)            # the display format is not a date
    with pytest.raises(ValueError):
        bcn.save_count(rid, "2026-12-31", {"beer": 1}, db_path=db, today=date(2026, 12, 1))
    # Without dollars, which categories were counted — no value.
    (row,) = bcn.history(rid, db_path=db, dollars=False)
    assert "values" not in row and row["categories"] == ["beer", "liquor", "wine"]


def test_an_import_never_replaces_what_the_owner_typed(db):
    rid = _rid(db)
    _save(db, rid, "2026-10-31", beer=4500)
    res = bcn.save_count(rid, "2026-10-31", {"beer": 9999, "wine": 2100}, source="backoffice", method="import",
                         db_path=db, today=date(2026, 12, 1))
    assert res["saved"] == ["wine"] and res["kept_owner"] == ["beer"]
    assert bcn.history(rid, db_path=db)[0]["values"]["beer"] == 4500.0


# ── Back Office valuation by email ──────────────────────────────────────────

VALUATION = (b"Simple EJ's\nInventory Valuation by Category\nInventory Date: 9/30/2026\n\n"
             b"Category,Items,Inventory Value\nDraft Beer,12,\"$1,200.00\"\nBottled Beer,40,800\n"
             b"Liquor,120,$5000.50\nWine,30,1500\nFood,10,900\nTotal,,9400.50\n")


def test_back_office_valuation_is_read_into_counts(db):
    import email_intake
    assert email_intake.READERS["backoffice_valuation"] == "bar.counts:read_valuation_file"
    reader = email_intake._reader("backoffice_valuation")
    rid = _rid(db)
    res = reader(rid, "valuation.csv", VALUATION, 7, db_path=db)
    assert res["status"] == "stored" and res["count_date"] == "2026-09-30"
    (row,) = bcn.history(rid, db_path=db)
    assert row["values"] == {"beer": 2000.0, "liquor": 5000.5, "wine": 1500.0}
    assert set(row["sources"].values()) == {"backoffice"}


@pytest.mark.parametrize("content,why", [
    (b"Category,Inventory Value\nLiquor,5000\n", "no count date"),
    (b"As of 10/31/2026 and 9/30/2026\nCategory,Inventory Value\nLiquor,5000\n", "more than one date"),
    (b"Inventory Date: 10/31/2026\nItem,Qty\nTitos,4\n", "no header row"),
    (b"Inventory Date: 10/31/2026\nCategory,Inventory Value\nFood,900\n", "no beer, liquor or wine"),
    (b"Inventory Date: 10/31/2026\nCategory,Inventory Value\nBeer & Wine,900\n", "more than one of"),
    (b"%PDF-1.4 not a sheet", "not a CSV or XLSX"),
])
def test_an_unreadable_valuation_is_unrecognized_never_guessed(db, content, why):
    rid = _rid(db)
    name = "report.pdf" if content.startswith(b"%PDF") else "report.csv"
    res = bcn.read_valuation_file(rid, name, content, db_path=db)
    assert res["status"] == "unrecognized" and why in res["detail"]
    assert bcn.history(rid, db_path=db) == []


def test_a_period_report_is_read_at_its_end_and_says_so():
    got = bcn.parse_valuation("v.csv", b"Period 10/1/2026 - 10/31/2026\nCategory,Extended Value\nSpirits,10\n")
    assert got["ok"] and got["count_date"] == "2026-10-31" and got["date_from"] == "the end of the report's period"


def test_an_xlsx_valuation_with_a_date_column_is_read():
    from dsr.xlsx import workbook
    data = workbook("Valuation", [["Count Date", "Category", "Value"], ["9/30/2026", "Beer", 1200.0],
                                  ["9/30/2026", "Liquor", 5000.0], ["9/30/2026", "N/A Beverage", 80.0]])
    got = bcn.parse_valuation("valuation.xlsx", data)
    assert got["ok"] and got["count_date"] == "2026-09-30" and got["date_from"] == "the date column"
    assert got["values"] == {"beer": 1200.0, "liquor": 5000.0} and got["ignored"] == ["N/A Beverage"]


# ── the routes: enter vs view ───────────────────────────────────────────────

def _app():
    app = Flask(__name__, template_folder=os.path.join(ROOT, "templates"))
    app.register_blueprint(client_api.client_bp)
    app.register_blueprint(mobile_api.mobile_bp)
    return app


def _as(monkeypatch, rid, role=None):
    user = {"id": 1, "restaurant_id": rid, "is_admin": 0, "username": "u"}
    if role:
        user["role"] = role
    monkeypatch.setattr(auth, "get_current_user", lambda: dict(user))


def test_a_manager_enters_a_count_without_seeing_a_dollar(db, monkeypatch):
    import permissions as p
    assert p.enter_path_allowed({"role": "manager"}, "POST", "/api/food-cost/bar/counts")
    assert p.enter_path_allowed({"role": "manager"}, "GET", "/mobile/api/food-cost/bar/counts")
    assert not p.enter_path_allowed({"role": "manager"}, "DELETE", "/api/food-cost/bar/counts/2026-10-31")
    rid = _rid(db)
    _october(db, rid)
    day = (date.today() - timedelta(days=2)).isoformat()
    _as(monkeypatch, rid, role="manager")
    c = _app().test_client()
    r = c.post("/api/food-cost/bar/counts", json={"count_date": day, "values": {"beer": 4500, "liquor": 7000}})
    body = r.get_json()
    assert r.status_code == 200 and body["ok"] and body["saved"] == ["beer", "liquor"]
    assert "counted" not in body and "4500" not in r.get_data(as_text=True)
    g = c.get("/api/food-cost/bar/counts")
    gb = g.get_json()
    assert g.status_code == 200 and gb["dollars"] is False and "counted" not in gb
    assert gb["counts"][0]["categories"] == ["beer", "liquor"] and "values" not in gb["counts"][0]
    assert "4500" not in g.get_data(as_text=True) and "7000" not in g.get_data(as_text=True)
    # Deleting is the account holder's; the Bar page itself is the margins'.
    assert c.delete(f"/api/food-cost/bar/counts/{day}").status_code == 403
    assert c.get("/api/food-cost/bar").status_code == 403
    # The owner sees the values and the counted block, and may delete.
    _as(monkeypatch, rid)
    gb = c.get("/api/food-cost/bar/counts").get_json()
    assert gb["dollars"] is True and gb["counts"][0]["values"]["beer"] == 4500.0 and "counted" in gb
    d = c.delete(f"/api/food-cost/bar/counts/{day}?category=liquor")
    assert d.status_code == 200 and d.get_json()["deleted"] == 1
    assert c.delete(f"/api/food-cost/bar/counts/{day}?category=liquor").status_code == 404
    events = [r["event_type"] for r in _conn(db).execute(
        "SELECT event_type FROM activity_log WHERE restaurant_id=?", (rid,))]
    assert "bar_counted" in events and "bar_count_removed" in events
    assert models.ACCOUNT_EVENT_LABELS["bar_counted"] == "Bar counted"


def test_a_bad_count_is_refused_with_a_sentence(db, monkeypatch):
    rid = _rid(db)
    _as(monkeypatch, rid)
    c = _app().test_client()
    r = c.post("/api/food-cost/bar/counts", json={"count_date": "10/31/26", "values": {"beer": 1}})
    assert r.status_code == 400 and r.get_json()["error"] == "Pick the count date from the calendar."
    r = c.post("/api/food-cost/bar/counts", json={"count_date": date.today().isoformat(), "values": {"food": 1}})
    assert r.status_code == 400 and "beer, liquor and wine" in r.get_json()["error"]
    future = (date.today() + timedelta(days=3)).isoformat()
    assert c.post("/api/food-cost/bar/counts", json={"count_date": future, "values": {"beer": 1}}).status_code == 400


def test_the_routes_have_their_mobile_twins():
    rules = {r.rule for r in _app().url_map.iter_rules()}
    for path in ("/api/food-cost/bar/counts", "/api/food-cost/bar/counts/<count_date>"):
        assert path in rules and "/mobile" + path in rules, path


# ── the month-end reminder ──────────────────────────────────────────────────

def test_the_reminder_type_is_mapped_everywhere():
    import nav
    import notify
    import push
    ios = open(os.path.join(ROOT, "ios/CavnarAI/CavnarAI/Push/DeepLinkRouter.swift"), encoding="utf-8").read()
    t = bcn.REMINDER_ALERT_TYPE
    assert client_api._NOTIFICATION_LABELS[t] == "Count the bar tonight"
    assert push.NOTIFICATION_MODULE[t] == "inventory" and t in push.PRIORITY and t in push.ACTIONABLE_TYPES
    assert notify.ALERT_TAB[t] == "inventory" and t in models.NON_ALERT_TYPES
    assert t not in notify.BRIEFING_ALWAYS, "a reminder spends the briefing budget like any briefing"
    assert nav.for_notification(t) == "bar" and push.nav_for(t, {}) == "bar"
    assert f'"{t}"' in ios


def test_the_reminder_goes_once_on_the_last_day_to_restaurants_with_invoices(db, monkeypatch):
    import scheduler
    import strategy_jobs
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: True)
    sent = []
    monkeypatch.setattr(strategy_jobs, "_reach",
                        lambda rid, t, title, body, data, db_path, **k: sent.append((rid, t, title, data)) or 1)
    with_invoices = _rid(db)
    _invoice(db, with_invoices, "Chicago Beverage", "A1", "2026-10-03", [("KEG", "KE", 100.0, None, "item")])
    _rid(db, name="No bar")
    # Not the last day: nothing.
    out = bcn.run_count_reminders(db_path=db, now=datetime(2026, 10, 30, 15, 0))
    assert sent == [] and out["attempted"] == 0
    # The last day, before 2pm: nothing yet.
    bcn.run_count_reminders(db_path=db, now=datetime(2026, 10, 31, 11, 0))
    assert sent == []
    out = bcn.run_count_reminders(db_path=db, now=datetime(2026, 10, 31, 15, 0))
    assert sent == [(with_invoices, "bar_count_due", "Count the bar tonight", {"tab": "inventory", "nav": "bar"})]
    assert {"attempted", "ok", "failed", "skipped", "hit_bound"} <= set(out) and out["reminded"] == 1
    # Claimed: the next hour's tick sends nothing more.
    bcn.run_count_reminders(db_path=db, now=datetime(2026, 10, 31, 16, 0))
    assert len(sent) == 1


def test_the_reminder_is_skipped_when_the_bar_is_already_counted(db, monkeypatch):
    import scheduler
    import strategy_jobs
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: True)
    sent = []
    monkeypatch.setattr(strategy_jobs, "_reach", lambda *a, **k: sent.append(a) or 1)
    rid = _rid(db)
    _invoice(db, rid, "Chicago Beverage", "A1", "2026-10-03", [("KEG", "KE", 100.0, None, "item")])
    bcn.save_count(rid, "2026-11-30", {"beer": 1, "liquor": 1, "wine": 1}, db_path=db, today=date(2026, 11, 30))
    out = bcn.run_count_reminders(db_path=db, now=datetime(2026, 11, 30, 15, 0))
    assert sent == [] and out["skipped"] == 1


def test_the_reminder_never_sends_off_the_production_scheduler(db, monkeypatch):
    import scheduler
    import strategy_jobs
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: False)
    monkeypatch.setattr(strategy_jobs, "_reach", lambda *a, **k: pytest.fail("sent off the production scheduler"))
    rid = _rid(db)
    _invoice(db, rid, "Chicago Beverage", "A1", "2026-10-03", [("KEG", "KE", 100.0, None, "item")])
    out = bcn.run_count_reminders(db_path=db, now=datetime(2026, 10, 31, 15, 0))
    assert out["attempted"] == 0 and out["skipped"] == 1


class _Stop(BaseException):
    pass


def test_the_loop_runs_the_reminder_and_the_registry_names_it(db, monkeypatch):
    """Drive one real scheduler tick (CLAUDE.md, drive the loop): the loop's
    run_job("bar_count_reminders") reaches bar.counts.run_count_reminders,
    and the registry entry is the one it runs."""
    import jobs_registry
    import marketing_publish
    import morning_brief
    import scheduler
    spec = jobs_registry.JOBS["bar_count_reminders"]
    assert spec["target"] == ("bar.counts", "run_count_reminders") and spec["sends"] is True
    ran = []
    monkeypatch.setattr(bcn, "run_count_reminders",
                        lambda **k: ran.append(1) or {"attempted": 0, "ok": 0, "failed": 0, "skipped": 0,
                                                      "hit_bound": False})
    monkeypatch.setattr(scheduler._ops, "acquire_scheduler_lease", lambda *a, **k: True)
    monkeypatch.setattr(scheduler._ops, "claim_period", lambda job, period: job == "bar_count_reminders")
    monkeypatch.setattr(scheduler._ops, "run_job",
                        lambda name, fn, *a, **k: fn() if name == "bar_count_reminders" else None)
    monkeypatch.setattr(marketing_publish, "run_due_posts", lambda **k: {})
    monkeypatch.setattr(morning_brief, "run_due", lambda *a, **k: {})
    monkeypatch.setattr(scheduler, "record_scheduler_heartbeat", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(scheduler, "run_health_checks", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(scheduler.time, "sleep", lambda s: (_ for _ in ()).throw(_Stop()))
    with pytest.raises(_Stop):
        scheduler.scheduler_loop()
    assert ran == [1]
