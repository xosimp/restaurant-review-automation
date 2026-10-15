"""The Bar's routes, cache and scale (barfix-1015 W7, audit 10/14/26: V17,
V21, V38, V40; SEC-2, SEC-4, SEC-7, SEC-14, RPW-18).

- Every Bar write is the account holder's, stored under the acting login,
  logged with before/after; an owner sorts a product only into beer, liquor,
  wine, non-alcoholic or bar supplies; a key must be one of this
  restaurant's products.
- A failure is captured and answered with a fixed sentence.
- The payload is kept until the data under it changes, and its long lists
  are paged; the engine reads each line a constant number of times however
  long the history.
- One restaurant never sees, or writes into, another's Bar.
"""
import json
import os
import sqlite3
from datetime import date, timedelta

import pytest
from flask import Flask

import auth
import bar.cache as bcache
import bar.pour_cost as bpc
import client_api
import mobile_api
import models
from bar.ledger import Lines
from models import Restaurant, create_restaurant

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TODAY = date.today()


@pytest.fixture
def db(db_path, monkeypatch):
    real, default = models.get_conn, models.DB_PATH

    def redirected(path=None, *a, **k):
        return real(db_path if path in (None, default) else path)
    for mod in (models, auth, client_api, mobile_api):
        monkeypatch.setattr(mod, "get_conn", redirected)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    auth.init_auth(db_path)
    import ops
    ops._claim_fallback.clear()
    bcache.clear()
    yield db_path
    bcache.clear()


def _conn(db):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    return c


def _rid(db, name="EJ's"):
    return create_restaurant(Restaurant(name=name, owner_email=f"{name[:3].lower()}@x.test", module_inventory=1),
                             db_path=db)


def _invoice(db, rid, dist, number, day, lines):
    """One invoice: lines [(item_code, description, uom, qty, unit_price)]."""
    c = _conn(db)
    try:
        inv = c.execute("INSERT INTO fintech_invoices (restaurant_id, distributor, invoice_number, invoice_date, total, "
                        "source_format, updated_at, balanced) VALUES (?,?,?,?,?, 'export', '2026-10-01 00:00:00', 1)",
                        (rid, dist, number, day, sum(q * p for _c, _d, _u, q, p in lines))).lastrowid
        for i, (code, desc, uom, qty, price) in enumerate(lines):
            c.execute("INSERT INTO fintech_invoice_lines (invoice_id, line_no, item_code, description, uom, qty, "
                      "unit_price, cost, kind, restaurant_id) VALUES (?,?,?,?,?,?,?,?, 'item', ?)",
                      (inv, i + 1, code, desc, uom, qty, price, round(qty * price, 2), rid))
        c.commit()
    finally:
        c.close()


def _sales(db, rid, day, beer=0, liquor=0, wine=0):
    c = _conn(db)
    try:
        for cat, v in (("Beer", beer), ("Liquor", liquor), ("Wine", wine)):
            c.execute("INSERT INTO dsr_metrics (restaurant_id, business_date, metric, value, status, source) "
                      "VALUES (?,?,?,?, 'ready', 'rpower')", (rid, day, "sales.cat:" + cat, v))
        c.commit()
    finally:
        c.close()


def _history(db, rid, dist="Breakthru Beverage Illinois", code="9031", desc="TITOS VDK 1L", weeks=6, tag="A",
             with_sales=True):
    for w in range(weeks):
        day = (TODAY - timedelta(days=7 * (weeks - w))).isoformat()
        price = 300.0 if w < weeks // 2 else 330.0
        _invoice(db, rid, dist, f"{tag}{w}", day, [(code, desc, "CA", 2, price),
                                                    ("777", "MILLER LITE 1/2BB", "KE", 1, 180.0)])
    for d in range(7 * weeks + 1 if with_sales else 0):
        _sales(db, rid, (TODAY - timedelta(days=d + 1)).isoformat(), beer=900, liquor=1400, wine=0)


def _app():
    app = Flask(__name__, template_folder=os.path.join(ROOT, "templates"))
    app.register_blueprint(client_api.client_bp)
    app.register_blueprint(mobile_api.mobile_bp)
    return app


def _as(monkeypatch, rid, role=None, grants=(), uid=1, **extra):
    user = {"id": uid, "restaurant_id": rid, "is_admin": 0, "username": "erik@ejs.test", "grants": list(grants)}
    if role:
        user["role"] = role
    user.update(extra)
    monkeypatch.setattr(auth, "get_current_user", lambda: dict(user))
    monkeypatch.setattr(auth, "get_session_user", lambda token=None, *a, **k: dict(user))
    return user


def _key(dist="Breakthru Beverage Illinois", code="9031"):
    from bar.classify import product_key
    return product_key(dist, code, "")


def _events(db, rid, kind):
    c = _conn(db)
    try:
        return [json.loads(r["event_data"] or "{}") for r in c.execute(
            "SELECT event_data FROM activity_log WHERE restaurant_id=? AND event_type=?", (rid, kind))]
    finally:
        c.close()


MOBILE = {"Authorization": "Bearer t"}


# ── V21: who may change the Bar ─────────────────────────────────────────────

def test_category_post_refused_for_view_only_grant(db, monkeypatch):
    rid = _rid(db)
    _history(db, rid)
    cl = _app().test_client()
    body = {"product_key": _key(), "category": "wine"}
    # A manager given only "Food cost & margins" sees the Bar but can't re-sort it.
    _as(monkeypatch, rid, role="manager", grants=["foodcost.view"])
    assert cl.get("/api/food-cost/bar").status_code == 200
    r = cl.post("/api/food-cost/bar/category", json=body)
    assert r.status_code == 403 and r.get_json()["owner_only"] is True
    assert cl.post("/mobile/api/food-cost/bar/category", json=body, headers=MOBILE).status_code == 403
    # The legacy member role sees margins and still can't write.
    _as(monkeypatch, rid, role="member")
    assert cl.post("/api/food-cost/bar/category", json=body).status_code == 403
    # A manager without the grant and Cavnar AI's support login never reach it.
    for role in ("manager", "support"):
        _as(monkeypatch, rid, role=role)
        assert cl.get("/api/food-cost/bar").status_code == 403
        assert cl.post("/api/food-cost/bar/category", json=body).status_code == 403
    c = _conn(db)
    assert c.execute("SELECT COUNT(*) FROM bar_product_categories").fetchone()[0] == 0
    c.close()


def test_every_bar_write_is_the_account_holders(db, monkeypatch):
    rid = _rid(db)
    _history(db, rid)
    cl = _app().test_client()
    writes = [("post", "/api/food-cost/bar/category", {"product_key": _key(), "category": "wine"}),
              ("post", "/api/food-cost/bar/stopped", {"distributor": "Breakthru Beverage Illinois"}),
              ("delete", "/api/food-cost/bar/stopped", {"distributor": "Breakthru Beverage Illinois"}),
              ("post", "/api/food-cost/bar/settings", {"count_mixers": True}),
              ("post", "/api/food-cost/bar/pack", {"product_key": _key(), "units_per_case": 12}),
              ("post", "/api/food-cost/bar/link", {"product_key": _key(), "same_as": None}),
              ("post", "/api/food-cost/bar/expected-price", {"product_key": _key(), "min_qty": 1, "price": 290}),
              ("delete", "/api/food-cost/bar/expected-price/1", None),
              ("post", "/api/food-cost/bar/drink-map", {"provider": "rpower", "item_id": "1", "product_key": None})]
    for role, grants in (("manager", ["foodcost.view"]), ("member", []), ("manager", []), ("support", [])):
        _as(monkeypatch, rid, role=role, grants=grants)
        for method, path, body in writes:
            r = getattr(cl, method)(path, json=body)
            assert r.status_code == 403, (role, path, r.status_code)
            m = getattr(cl, method)("/mobile" + path, json=body, headers=MOBILE)
            assert m.status_code == 403, (role, "/mobile" + path, m.status_code)


def test_category_change_logged_with_actor(db, monkeypatch):
    rid = _rid(db)
    _history(db, rid)
    cl = _app().test_client()
    owner = _as(monkeypatch, rid, role="client", uid=11)
    r = cl.post("/api/food-cost/bar/category", json={"product_key": _key(), "category": "wine"})
    out = r.get_json()
    assert r.status_code == 200 and (out["before"], out["after"]) == (None, "wine")
    ev = _events(db, rid, "bar_category_changed")
    assert len(ev) == 1 and ev[0]["detail"] == f"{_key()}: guess -> wine" and ev[0]["actor"] == owner["username"]
    assert "bar_category_changed" in models.ACCOUNT_EVENT_TYPES
    assert models.ACCOUNT_EVENT_LABELS["bar_category_changed"] == "Bar product re-sorted"
    c = _conn(db)
    assert c.execute("SELECT set_by FROM bar_product_categories").fetchone()[0] == 11
    c.close()
    page = cl.get("/api/food-cost/bar").get_json()
    mine = next(p for p in page["products"] if p["product_key"] == _key())
    assert mine["owner_set"] and mine["set_by"] == 11 and mine["set_by_you"] is True
    # An admin in view-as re-sorts it: stored and named as support, never
    # as the owner it views as; the owner no longer reads "set by you".
    _as(monkeypatch, rid, role="client", uid=11, acting_admin_id=99)
    c = _conn(db)
    c.execute("INSERT INTO users (id, username, email, password_hash, is_admin, restaurant_id) "
              "VALUES (99, 'will', 'w@x.test', 'x', 1, ?)", (rid,))
    c.commit()
    c.close()
    r = cl.post("/api/food-cost/bar/category", json={"product_key": _key(), "category": "liquor"})
    assert r.status_code == 200 and r.get_json()["set_by_name"] == "Cavnar AI support"
    ev = _events(db, rid, "bar_category_changed")
    assert ev[-1]["detail"] == f"{_key()}: wine -> liquor"
    _as(monkeypatch, rid, role="client", uid=11)
    p = next(p for p in cl.get("/api/food-cost/bar").get_json()["products"] if p["product_key"] == _key())
    assert p["category"] == "liquor" and p["set_by"] == 99 and p["set_by_name"] == "Cavnar AI support"
    assert p["set_by_you"] is False
    # Clearing it goes back to the guess, logged.
    assert cl.post("/api/food-cost/bar/category", json={"product_key": _key(), "category": ""}).get_json()["after"] is None
    assert _events(db, rid, "bar_category_changed")[-1]["detail"] == f"{_key()}: liquor -> guess"


def test_owner_may_sort_only_into_the_five_drink_and_supply_categories(db, monkeypatch):
    rid = _rid(db)
    _history(db, rid)
    _as(monkeypatch, rid)
    cl = _app().test_client()
    for cat in ("fee", "deposit", "other", "rum"):
        r = cl.post("/api/food-cost/bar/category", json={"product_key": _key(), "category": cat})
        assert r.status_code == 400 and "Pick beer" in r.get_json()["error"]
    for cat in ("beer", "liquor", "wine", "na", "supplies"):
        assert cl.post("/api/food-cost/bar/category", json={"product_key": _key(), "category": cat}).status_code == 200
    page = cl.get("/api/food-cost/bar").get_json()
    assert [c["key"] for c in page["owner_categories"]] == ["beer", "liquor", "wine", "na", "supplies"]
    assert page["can_change"] is True


# ── V40: the key must be one of this restaurant's products ──────────────────

def test_unknown_product_key_refused(db, monkeypatch):
    rid = _rid(db)
    _history(db, rid)
    _as(monkeypatch, rid)
    cl = _app().test_client()
    for key in ("nobody|nothing", "x" * 301, "", None, _key(code="0000")):
        r = cl.post("/api/food-cost/bar/category", json={"product_key": key, "category": "wine"})
        assert r.status_code == 400, key
    for path, body in (("/api/food-cost/bar/pack", {"product_key": "nobody|nothing", "units_per_case": 6}),
                       ("/api/food-cost/bar/link", {"product_key": _key(), "same_as": "nobody|nothing"}),
                       ("/api/food-cost/bar/expected-price", {"product_key": "x" * 400, "min_qty": 1, "price": 5})):
        assert cl.post(path, json=body).status_code == 400, path
    c = _conn(db)
    for t in ("bar_product_categories", "bar_product_packs", "bar_product_links", "bar_expected_prices"):
        assert c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0, t
    c.close()


# ── V38: failures are captured, never shown ─────────────────────────────────

def test_build_failure_is_captured_and_generic(db, monkeypatch):
    rid = _rid(db)
    _history(db, rid)
    _as(monkeypatch, rid)
    import bar.build
    import ops

    def boom(*a, **k):
        raise RuntimeError("secret detail: no such column pct")
    captured = []
    monkeypatch.setattr(bar.build, "build", boom)
    monkeypatch.setattr(ops, "capture", lambda e, **k: captured.append((str(e), k)))
    cl = _app().test_client()
    for path, headers in (("/api/food-cost/bar", {}), ("/mobile/api/food-cost/bar", MOBILE)):
        r = cl.get(path, headers=headers)
        text = r.get_data(as_text=True)
        assert r.status_code == 500 and "secret" not in text and "column" not in text
        assert r.get_json()["error"].startswith("The bar's numbers couldn't be read just now.")
    assert len(captured) == 2 and captured[0][1]["job"] == "bar_page"
    assert captured[0][1]["restaurant_id"] == rid
    # A write that fails on the database is captured the same way.
    import bar.classify

    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(bar.classify, "set_category", locked)
    monkeypatch.setattr("bar.edits.set_category", locked)
    r = cl.post("/api/food-cost/bar/category", json={"product_key": _key(), "category": "wine"})
    assert r.status_code == 500 and "locked" not in r.get_data(as_text=True)
    assert captured[-1][1]["job"] == "bar_write"


# ── the stopped distributor (W4's store, W7's route) ────────────────────────

def _bar_route_bodies():
    """{function name: source} of every mobile_api route under /food-cost/bar
    (one body each for web and phone — client_api's twins call these)."""
    import re
    src = open(os.path.join(ROOT, "mobile_api.py")).read()
    out = {}
    for m in re.finditer(r'@mobile_bp\.route\("/food-cost/bar[^"]*"[^\n]*\n(?:@[^\n]*\n)*def (\w+)\(', src):
        start = m.start()
        nxt = re.search(r"\n(?:@|def |# ──)", src[m.end():])
        out[m.group(1)] = src[start:m.end() + (nxt.start() if nxt else len(src))]
    return out


def test_no_bar_route_answers_a_failure_with_the_exceptions_text():
    """V38 everywhere on the Bar (W6b's counts, mapping, waste and drink-spec
    routes included): a failure is captured and answered with a fixed
    sentence — read from the source, so a route added later is held to it."""
    bodies = _bar_route_bodies()
    assert len(bodies) >= 22, sorted(bodies)
    for name, body in bodies.items():
        assert "_safe_err" not in body and "str(e)" not in body, name
        if "except Exception" in body:
            assert "_bar_capture(" in body, name
            assert "_BAR_READ_FAILED" in body or "_BAR_WRITE_FAILED" in body, name


def test_waste_and_drink_spec_failures_are_captured_and_generic(db, monkeypatch):
    rid = _rid(db)
    _history(db, rid)
    _as(monkeypatch, rid)
    import bar.variance
    import bar.waste
    import ops

    def boom(*a, **k):
        raise sqlite3.OperationalError("no such table: bar_waste_secret")
    captured = []
    monkeypatch.setattr(ops, "capture", lambda e, **k: captured.append(k))
    monkeypatch.setattr(bar.waste, "waste_rows", boom)
    monkeypatch.setattr(bar.waste, "log_waste", boom)
    monkeypatch.setattr(bar.variance, "drink_specs", boom)
    cl = _app().test_client()
    for method, path, body, job in (
            ("get", "/api/food-cost/bar/waste", None, "bar_waste"),
            ("post", "/api/food-cost/bar/waste", {"waste_date": TODAY.isoformat(), "product_key": _key(),
                                                  "qty": 1, "unit": "each", "reason": "spill"}, "bar_write"),
            ("get", "/mobile/api/food-cost/bar/drink-specs", None, "bar_drink_specs")):
        r = getattr(cl, method)(path, json=body, headers=MOBILE if path.startswith("/mobile") else {})
        text = r.get_data(as_text=True)
        assert r.status_code == 500 and "secret" not in text and "no such table" not in text, path
        assert r.get_json()["error"] in (mobile_api._BAR_READ_FAILED, mobile_api._BAR_WRITE_FAILED), path
        assert captured[-1]["job"] == job and captured[-1]["restaurant_id"] == rid, path


def test_owner_marks_a_distributor_stopped_and_back(db, monkeypatch):
    import bar.judge as bj
    rid = _rid(db)
    _history(db, rid)
    _as(monkeypatch, rid)
    cl = _app().test_client()
    r = cl.post("/api/food-cost/bar/stopped", json={"distributor": "  breakthru   beverage illinois "})
    assert r.status_code == 200 and r.get_json()["stopped"] is True
    assert bj.owner_stopped(rid) == ["breakthru beverage illinois"]
    assert _events(db, rid, "bar_stopped_changed")[-1]["detail"] == "breakthru beverage illinois: stopped"
    stopped = cl.get("/api/food-cost/bar").get_json()["pour_cost"]["stopped"]
    assert any(s["why"] == "owner" for s in stopped)
    r = cl.delete("/mobile/api/food-cost/bar/stopped", json={"distributor": "Breakthru Beverage Illinois"},
                  headers=MOBILE)
    assert r.status_code == 200 and bj.owner_stopped(rid) == []
    assert cl.delete("/api/food-cost/bar/stopped", json={"distributor": "Breakthru Beverage Illinois"}).status_code == 404
    assert cl.post("/api/food-cost/bar/stopped", json={"distributor": "Nobody Wines"}).status_code == 400
    assert cl.post("/api/food-cost/bar/stopped", json={}).status_code == 400


# ── every route's 400s ──────────────────────────────────────────────────────

def test_every_bar_route_answers_bad_input_with_a_sentence(db, monkeypatch):
    rid = _rid(db)
    _history(db, rid)
    _as(monkeypatch, rid)
    cl = _app().test_client()
    bad = [
        ("post", "/api/food-cost/bar/category", "nope"),
        ("post", "/api/food-cost/bar/settings", {}),
        ("post", "/api/food-cost/bar/settings", {"liquor_oz": "a lot"}),
        ("post", "/api/food-cost/bar/settings", {"liquor_oz": 50}),
        ("post", "/api/food-cost/bar/settings", {"count_mixers": "maybe"}),
        ("post", "/api/food-cost/bar/settings", {"pour_specs": {"keg_oz": 2}}),
        ("post", "/api/food-cost/bar/settings", {"pour_specs": "big"}),
        ("post", "/api/food-cost/bar/pack", {"product_key": _key(), "units_per_case": 2.5}),
        ("post", "/api/food-cost/bar/pack", {"product_key": _key(), "units_per_case": 500}),
        ("post", "/api/food-cost/bar/pack", {"product_key": _key(), "container_ml": "big"}),
        ("post", "/api/food-cost/bar/link", {"product_key": _key(), "same_as": _key()}),
        ("post", "/api/food-cost/bar/expected-price", {"product_key": _key(), "min_qty": 1}),
        ("post", "/api/food-cost/bar/expected-price", {"product_key": _key(), "min_qty": 1, "price": "abc"}),
        ("post", "/api/food-cost/bar/expected-price", {"product_key": _key(), "min_qty": 0, "price": 5}),
        ("post", "/api/food-cost/bar/expected-price",
         {"product_key": _key(), "min_qty": 1, "price": 5, "valid_from": "next tuesday"}),
        ("post", "/api/food-cost/bar/expected-price",
         {"product_key": _key(), "min_qty": 1, "price": 5, "valid_from": "2026-10-10", "valid_to": "2026-10-01"}),
        ("post", "/api/food-cost/bar/drink-map", {"provider": "rpower", "item_id": "404", "product_key": _key()}),
        ("post", "/api/food-cost/bar/drink-map", {"provider": "", "item_id": ""}),
        ("post", "/api/food-cost/bar/stopped", {"distributor": "x" * 300}),
    ]
    for method, path, body in bad:
        for p, headers in ((path, {}), ("/mobile" + path, MOBILE)):
            r = getattr(cl, method)(p, json=body, headers=headers) if not isinstance(body, str) else \
                getattr(cl, method)(p, data=body, content_type="application/json", headers=headers)
            out = r.get_json()
            assert r.status_code == 400 and out["ok"] is False, (p, body, r.status_code, out)
            err = out["error"]
            assert err and "Traceback" not in err and "could not convert" not in err and "isoformat" not in err, err
    for q in ("dir=sideways", "sort=loudest", "offset=-x", "limit=many", "list=secrets"):
        for p, headers in (("/api/food-cost/bar/prices", {}), ("/mobile/api/food-cost/bar/prices", MOBILE)):
            assert cl.get(f"{p}?{q}", headers=headers).status_code == 400, q
    assert cl.get("/api/food-cost/bar/products?limit=many").status_code == 400
    assert cl.delete("/api/food-cost/bar/expected-price/9999").status_code == 404
    assert models.ACCOUNT_EVENT_TYPES  # (nothing above wrote an event)
    c = _conn(db)
    assert c.execute("SELECT COUNT(*) FROM activity_log WHERE event_type LIKE 'bar_%'").fetchone()[0] == 0
    c.close()


def test_the_owner_writes_save_and_log(db, monkeypatch):
    rid = _rid(db)
    _history(db, rid)
    _history(db, rid, code="9032", desc="TITOS VDK 1L GIFT", weeks=2, tag="G", with_sales=False)
    c = _conn(db)
    c.execute("INSERT INTO pos_menu_prices (restaurant_id, provider, item_id, level_id, item_name, level, price) "
              "VALUES (?, 'rpower', '501', '1', 'Titos Soda', 'Regular', 9.0)", (rid,))
    c.commit()
    c.close()
    _as(monkeypatch, rid)
    cl = _app().test_client()
    r = cl.post("/api/food-cost/bar/settings", json={"count_mixers": True, "pour_specs": {"liquor_oz": "1.25"}})
    assert r.status_code == 200 and r.get_json()["count_mixers"] and r.get_json()["liquor_oz"] == 1.25
    assert cl.post("/api/food-cost/bar/pack", json={"product_key": _key(), "units_per_case": 12,
                                                     "container_ml": 1000}).status_code == 200
    assert cl.post("/api/food-cost/bar/link", json={"product_key": _key(code="9032"),
                                                     "same_as": _key()}).get_json()["same_as"] == _key()
    ep = cl.post("/api/food-cost/bar/expected-price", json={"product_key": _key(), "min_qty": 2, "price": "$295",
                                                             "valid_from": "2026-10-01", "note": "Q4 deal"})
    assert ep.status_code == 200
    dm = cl.post("/mobile/api/food-cost/bar/drink-map", json={"provider": "rpower", "item_id": "501",
                                                              "product_key": _key(), "pour_oz": 1.5}, headers=MOBILE)
    assert dm.status_code == 200 and dm.get_json()["product_key"] == _key()
    assert cl.delete(f"/api/food-cost/bar/expected-price/{ep.get_json()['id']}").status_code == 200
    for kind in ("bar_settings_changed", "bar_pack_changed", "bar_link_changed", "bar_deal_sheet_changed",
                 "bar_drink_map_changed"):
        assert _events(db, rid, kind), kind
        assert kind in models.ACCOUNT_EVENT_TYPES and kind in models.ACCOUNT_EVENT_LABELS
    assert len(_events(db, rid, "bar_deal_sheet_changed")) == 2
    c = _conn(db)
    assert c.execute("SELECT set_by FROM bar_settings WHERE restaurant_id=?", (rid,)).fetchone()[0] == 1
    c.close()


# ── cross-tenant isolation ──────────────────────────────────────────────────

def test_one_restaurant_never_reads_or_writes_anothers_bar(db, monkeypatch):
    a, b = _rid(db, "Alpha Tap"), _rid(db, "Beta Bar")
    _history(db, a, dist="Breakthru Beverage Illinois", code="9031", tag="A")
    _history(db, b, dist="Southern Glazer's", code="5555", desc="JAMESON IRISH WHSKY 1L", tag="B")
    cl = _app().test_client()
    _as(monkeypatch, b, uid=2)
    pb = cl.get("/api/food-cost/bar").get_json()
    _as(monkeypatch, a, uid=1)
    pa = cl.get("/api/food-cost/bar").get_json()
    assert {d["distributor"] for d in pa["distributors"]} == {"Breakthru Beverage Illinois"}
    assert {d["distributor"] for d in pb["distributors"]} == {"Southern Glazer's"}
    rows = cl.get("/api/food-cost/bar/products?limit=200").get_json()["rows"]
    assert rows and all("southern" not in r["product_key"] for r in rows)
    prices = cl.get("/api/food-cost/bar/prices?limit=200").get_json()["rows"]
    assert all("southern" not in r["product_key"] for r in prices)
    # B's product, distributor and deal-sheet line are not A's to change.
    b_key = _key("Southern Glazer's", "5555")
    assert cl.post("/api/food-cost/bar/category", json={"product_key": b_key, "category": "wine"}).status_code == 400
    assert cl.post("/api/food-cost/bar/stopped", json={"distributor": "Southern Glazer's"}).status_code == 400
    _as(monkeypatch, b, uid=2)
    eid = cl.post("/api/food-cost/bar/expected-price",
                  json={"product_key": b_key, "min_qty": 1, "price": 30}).get_json()["id"]
    _as(monkeypatch, a, uid=1)
    assert cl.delete(f"/api/food-cost/bar/expected-price/{eid}").status_code == 404
    c = _conn(db)
    assert c.execute("SELECT COUNT(*) FROM bar_expected_prices WHERE id=?", (eid,)).fetchone()[0] == 1
    assert c.execute("SELECT COUNT(*) FROM bar_product_categories").fetchone()[0] == 0
    c.close()


# ── V17: kept until the data changes; paged; linear in the history ─────────

def test_build_cached_until_stamp_changes(db, monkeypatch):
    import bar.build
    rid = _rid(db)
    _history(db, rid)
    calls = []

    def counting(*a, **k):
        calls.append(1)
        return bar.build.build(*a, **k)
    today = TODAY
    first = bcache.page(rid, today=today, build=counting)
    assert first["available"] and len(calls) == 1
    bcache.page(rid, today=today, build=counting)
    bcache.full(rid, today=today, build=counting)
    assert len(calls) == 1, "an unchanged restaurant is served the kept payload"

    def changed(step):
        before = len(calls)
        bcache.page(rid, today=today, build=counting)
        assert len(calls) == before + 1, step
        bcache.page(rid, today=today, build=counting)
        assert len(calls) == before + 1, step + " (kept again)"
    _invoice(db, rid, "Winebow", "W1", (TODAY - timedelta(days=2)).isoformat(), [("1", "SON CUTRER CHARD 750", "CA", 1, 150.0)])
    changed("a new invoice")
    _sales(db, rid, TODAY.isoformat(), beer=10)
    changed("a night's sales")
    from bar.classify import set_category
    set_category(rid, _key(), "wine", user_id=1)
    changed("a product re-sorted")
    set_category(rid, _key(), "liquor", user_id=1)           # same second, same count: the route's invalidate
    bcache.invalidate(rid)
    changed("a re-sort inside the same second, through invalidate")
    models.update_restaurant(rid, {models.BAR_TARGET_FIELDS["liquor"]: 18.0})
    changed("the owner's liquor target")
    c = _conn(db)
    c.execute("UPDATE restaurants SET timezone='America/Denver' WHERE id=?", (rid,))
    c.commit()
    c.close()
    changed("the restaurant's own row (hours, timezone, fiscal calendar)")
    import bar.judge as bj
    bj.set_stopped(rid, "Winebow", user_id=1)
    changed("a distributor stopped")
    before = len(calls)
    bcache.page(rid, today=today + timedelta(days=1), build=counting)
    assert len(calls) == before + 1, "a new day"
    # Another process's write moves the shared generation this process reads.
    c = _conn(db)
    try:
        c.execute("UPDATE job_cursors SET value='other-process' WHERE key=?", (f"bar_gen:{rid}",))
        assert c.total_changes == 1
        c.commit()
    finally:
        c.close()
    before = len(calls)
    bcache.page(rid, today=today + timedelta(days=1), build=counting)
    assert len(calls) == before + 1


def test_a_route_write_drops_the_kept_payload(db, monkeypatch):
    rid = _rid(db)
    _history(db, rid)
    _as(monkeypatch, rid)
    cl = _app().test_client()
    p1 = next(p for p in cl.get("/api/food-cost/bar").get_json()["products"] if p["product_key"] == _key())
    assert cl.post("/api/food-cost/bar/category", json={"product_key": _key(), "category": "wine"}).status_code == 200
    assert cl.post("/api/food-cost/bar/category", json={"product_key": _key(), "category": "na"}).status_code == 200
    p2 = next(p for p in cl.get("/api/food-cost/bar").get_json()["products"] if p["product_key"] == _key())
    assert p1["category"] == "liquor" and p2["category"] == "na"


def test_page_payload_caps_long_lists_and_the_routes_page_through_them():
    rows = [{"product_key": f"d|{i}", "name": f"Item {i}", "product": f"ITEM {i}", "distributor": "Breakthru" if i % 2 else "Euclid",
             "category": "liquor" if i % 3 else "beer", "kind": "increase" if i % 4 else "deal", "change": (i % 7) - 3,
             "pct": float(i % 11), "forward_monthly_usd": float(i), "since": f"2026-0{1 + i % 9}-01"} for i in range(130)]
    prods = [{"product_key": f"d|{i}", "product": f"ITEM {i}", "distributor": "Euclid", "category": "beer"}
             for i in range(240)]
    full = {"ok": True, "available": True, "products": prods,
            "price_watch": {"increases": rows, "decreases": rows[:10], "events": rows, "credits": rows[:3],
                            "summary": {"up_n": 130}}}
    page = bcache.trim(full)
    pw = page["price_watch"]
    assert len(pw["increases"]) == bcache.PRICE_CAP and len(pw["events"]) == bcache.PRICE_CAP
    assert pw["totals"]["increases"] == 130 and pw["totals"]["decreases"] == 10 and pw["truncated"] is True
    assert pw["summary"] == {"up_n": 130} and len(full["price_watch"]["events"]) == 130, "the kept payload is untouched"
    assert len(page["products"]) == bcache.PRODUCT_CAP and page["products_total"] == 240
    got = bcache.price_rows(full, offset=0, limit=500, dir="up")
    assert got["limit"] == bcache.PAGE_LIMIT_MAX and got["total"] == sum(1 for r in rows if r["change"] > 0)
    assert [r["forward_monthly_usd"] for r in got["rows"]] == sorted((r["forward_monthly_usd"] for r in got["rows"]),
                                                                     reverse=True)
    deals = bcache.price_rows(full, kind="deal", cat="beer", dist="euclid", sort="newest")
    assert deals["rows"] and all(r["kind"] == "deal" and r["category"] == "beer" and r["distributor"] == "Euclid"
                                 for r in deals["rows"])
    a = bcache.price_rows(full, offset=0, limit=20)
    b = bcache.price_rows(full, offset=20, limit=20)
    assert not {r["product_key"] for r in a["rows"]} & {r["product_key"] for r in b["rows"]}
    assert bcache.price_rows(full, q="item 12")["total"] == sum(1 for r in rows if "item 12" in r["name"].lower())
    pr = bcache.product_rows(full, offset=230, limit=50)
    assert pr["total"] == 240 and len(pr["rows"]) == 10
    with pytest.raises(ValueError):
        bcache.price_rows(full, dir="sideways")


def _synthetic(months, per_week=60):
    """`months` of weekly invoices across three houses and nightly sales."""
    lines, sales = [], {"beer": {}, "liquor": {}, "wine": {}}
    end = date(2026, 10, 1)
    start = end - timedelta(days=30 * months)
    d, n = start, 0
    while d <= end:
        for house, cat in (("Euclid", "beer"), ("Breakthru", "liquor"), ("Winebow", "wine")):
            if (d - start).days % 7 == {"beer": 0, "liquor": 2, "wine": 4}[cat]:
                n += 1
                for i in range(per_week // 3):
                    lines.append({"distributor": house, "invoice_number": str(n), "invoice_date": d.isoformat(),
                                  "category": cat, "net": 40.0 + i, "balanced": True, "kind": "item"})
        for cat, v in (("beer", 900.0), ("liquor", 1300.0), ("wine", 400.0)):
            sales[cat][d.isoformat()] = v
        d += timedelta(days=1)
    return lines, sales, end


class _Counted(dict):
    reads = 0

    def __getitem__(self, k):
        _Counted.reads += 1
        return dict.__getitem__(self, k)

    def get(self, k, default=None):
        _Counted.reads += 1
        return dict.get(self, k, default)


def test_build_scales_linearly(monkeypatch):
    """The engine reads each line a constant number of times, however long
    the history: 6, 18 and 36 months cost the same per line, and the number
    of windows read is the same once the month table is full (SEC-2: it was
    windows x lines, ~1 s at three years)."""
    reads_per_line, windows = {}, {}
    real_read = bpc._read
    for months in (6, 18, 36):
        lines, sales, end = _synthetic(months)
        lines = Lines(_Counted(l) for l in lines)
        calls = []

        def counting(*a, **k):
            calls.append(1)
            return real_read(*a, **k)
        monkeypatch.setattr(bpc, "_read", counting)
        _Counted.reads = 0
        out = bpc._pour_cost(lines, sales, end + timedelta(days=1))
        assert out["available"] and len(out["months"]) >= min(months, 12) - 1
        reads_per_line[months] = _Counted.reads / len(lines)
        windows[months] = len(calls)
    assert windows[18] == windows[36], windows
    assert max(reads_per_line.values()) < 40, reads_per_line
    assert reads_per_line[36] < 1.5 * reads_per_line[6], reads_per_line


def test_price_kinds_filter_as_a_comma_list():
    rows = [{"product_key": f"d|{i}", "name": f"Item {i}", "kind": k, "change": 1, "since": "2026-10-01"}
            for i, k in enumerate(["increase", "deal", "deal_ended", "tier", "decrease"])]
    got = bcache.price_rows({"price_watch": {"events": rows}}, kind="deal_ended, deal,tier", list_name="events")
    assert sorted(r["kind"] for r in got["rows"]) == ["deal", "deal_ended", "tier"] and got["total"] == 3
    assert set(got) == {"ok", "total", "offset", "limit", "rows"}


def test_price_directions_read_the_pages_own_segments():
    """up = the increases that held, down = the decreases, all = those plus
    unconfirmed, deals and tiers — never the minor rows; `list` names one."""
    def row(i, kind, ch):
        return {"product_key": f"d|{i}", "name": f"Item {i}", "kind": kind, "change": ch, "since": "2026-10-01"}
    pw = {"increases": [row(1, "increase", 5)], "decreases": [row(2, "decrease", -5)],
          "unconfirmed": [row(3, "increase", 4)], "deals": [row(4, "deal", -9), row(5, "deal_ended", 9)],
          "tiers": [row(6, "tier", -2)], "minor": [row(7, "increase", 0.1)]}
    pw["events"] = [r for k in ("increases", "decreases", "unconfirmed", "deals", "tiers", "minor") for r in pw[k]]
    p = {"price_watch": pw}
    keys = lambda got: sorted(r["product_key"] for r in got["rows"])
    assert keys(bcache.price_rows(p, dir="up")) == ["d|1"]
    assert keys(bcache.price_rows(p, dir="down")) == ["d|2"]
    assert keys(bcache.price_rows(p, dir="all")) == ["d|1", "d|2", "d|3", "d|4", "d|5", "d|6"]
    assert keys(bcache.price_rows(p, dir="all", kind="deal_ended,deal,tier")) == ["d|4", "d|5", "d|6"]
    assert keys(bcache.price_rows(p, dir="up", list_name="events")) == ["d|1", "d|3", "d|5", "d|7"]


def test_the_deal_sheet_is_on_the_page_and_its_own_route(db, monkeypatch):
    rid = _rid(db)
    _history(db, rid)
    _as(monkeypatch, rid)
    cl = _app().test_client()
    eid = cl.post("/api/food-cost/bar/expected-price", json={"product_key": _key(), "min_qty": 3, "price": 280,
                                                              "valid_from": "2026-10-01", "valid_to": "2026-12-31",
                                                              "note": "Q4"}).get_json()["id"]
    page = cl.get("/api/food-cost/bar").get_json()
    row = page["expected_prices"][0]
    assert row["id"] == eid and row["product"] == "Titos VDK 1L" and row["min_qty"] == 3 and row["price"] == 280
    assert (row["valid_from_mdy"], row["valid_to_mdy"], row["note"]) == ("10/1/26", "12/31/26", "Q4")
    got = cl.get("/mobile/api/food-cost/bar/expected-prices", headers=MOBILE).get_json()
    assert got["ok"] and [r["id"] for r in got["rows"]] == [eid] and got["can_change"] is True
    assert cl.delete(f"/api/food-cost/bar/expected-price/{eid}").status_code == 200
    assert cl.get("/api/food-cost/bar").get_json()["expected_prices"] == []


def test_comps_are_filled_per_viewer_and_never_kept(db, monkeypatch):
    import bar.comps
    rid = _rid(db)
    _history(db, rid)
    monkeypatch.setattr(bar.comps, "comps_block",
                        lambda r, today=None, db_path=None: {"available": True, "shifts": ["owner-only detail"]})
    cl = _app().test_client()
    _as(monkeypatch, rid)
    owner = cl.get("/api/food-cost/bar").get_json()
    assert owner["bar_ops"]["comps"]["shifts"] == ["owner-only detail"]
    _as(monkeypatch, rid, role="manager", grants=["foodcost.view"])
    mgr = cl.get("/api/food-cost/bar")
    assert mgr.get_json()["bar_ops"]["comps"] == {"owner_only": True}
    assert "owner-only detail" not in mgr.get_data(as_text=True)
    kept = bcache.full(rid, today=TODAY)
    assert "shifts" not in ((kept.get("bar_ops") or {}).get("comps") or {}), "the kept payload is viewer-neutral"
