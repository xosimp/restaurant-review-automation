"""Fix round B1 — data-layer integrity and POS connect (#130, #142, #143, #156).

- save_client_data refuses a data_type it doesn't hold: the value becomes a
  COLUMN NAME in its SQL, and /admin/upload-data passed a form field straight
  through (#130).
- A second scheduling constraint for someone is added to the first, never
  written over it; a removal returns what it removed (#142).
- Every CSV export neutralises spreadsheet formulas through one writer (#156).
- One POS store feeds one restaurant; the Toast demo guard holds whether or
  not the credential test runs; a new RPOWER token clears the old binding
  (#143).
"""
import csv
import io
import os

import pytest
from flask import Flask

import auth
import auth_routes
import clover_routes
import models
import rpower
import rpower_routes
import square_routes
import toast_routes
from auth import create_session, create_user, init_auth
from models import Restaurant, create_restaurant, get_restaurant, update_restaurant

CSRF = "b1-data-csrf"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def _redirect_db(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, auth_routes):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    init_auth(db_path=db_path)
    models.init_staff_notes(db_path=db_path)
    auth_routes._login_attempts.clear()
    yield


def _rid(db_path, name="Grill", **kw):
    return create_restaurant(Restaurant(name=name, owner_email=f"{name.lower().replace(' ', '')}@x.test", **kw),
                             db_path=db_path)


# ── #130: save_client_data allow-lists the column it writes ────────────────

@pytest.mark.parametrize("bad", ["reviews", "shifts_csv=NULL, inventory", "", None, "Shifts"])
def test_save_client_data_refuses_a_data_type_it_does_not_hold(db_path, bad):
    rid = _rid(db_path)
    with pytest.raises(ValueError):
        models.save_client_data(rid, bad, "date,employee\n", db_path=db_path)
    assert models.get_client_data(rid, db_path=db_path) is None


def test_save_client_data_still_saves_both_datasets(db_path):
    rid = _rid(db_path)
    models.save_client_data(rid, "shifts", "date,employee,actual_hours\n2026-09-01,Ann,5\n", db_path=db_path)
    models.save_client_data(rid, "inventory", "item,current_stock\nSalt,3\n", source="manual", db_path=db_path)
    row = models.get_client_data(rid, db_path=db_path)
    assert row["shifts_csv"].startswith("date,") and row["inventory_source"] == "manual"


# ── #142: staff notes add, never replace ────────────────────────────────────

def test_a_second_constraint_is_added_to_the_first(db_path):
    rid = _rid(db_path)
    first = models.save_staff_note(rid, "Maria", "mornings only", db_path=db_path)
    second = models.save_staff_note(rid, "maria", "no Sundays", db_path=db_path)
    assert first["appended"] is False and second["appended"] is True
    notes = models.get_staff_notes(rid, db_path=db_path)
    assert len(notes) == 1 and notes[0]["notes"] == "mornings only; no Sundays"
    # The same text again adds nothing.
    again = models.save_staff_note(rid, "Maria", "No Sundays", db_path=db_path)
    assert again["appended"] is False and again["notes"] == "mornings only; no Sundays"


def test_replace_sets_the_text_outright(db_path):
    rid = _rid(db_path)
    models.save_staff_note(rid, "Sam", "closer", db_path=db_path)
    out = models.save_staff_note(rid, "Sam", "opener", db_path=db_path, replace=True)
    assert out["notes"] == "opener"


def test_a_removal_returns_what_it_removed_and_respects_the_restaurant(db_path):
    rid, other = _rid(db_path), _rid(db_path, "Other")
    note = models.save_staff_note(rid, "Maria", "mornings only", db_path=db_path)
    assert models.delete_staff_note(note["id"], db_path=db_path, restaurant_id=other) is None
    gone = models.delete_staff_note(note["id"], db_path=db_path)
    assert gone["employee_name"] == "Maria" and gone["notes"] == "mornings only" and gone["restaurant_id"] == rid
    assert models.get_staff_notes(rid, db_path=db_path) == []
    assert models.delete_staff_note(note["id"], db_path=db_path) is None


# ── #156: one CSV writer neutralises formulas ───────────────────────────────

@pytest.mark.parametrize("cell,expected", [
    ("=HYPERLINK(\"http://x\",\"y\")", "'=HYPERLINK(\"http://x\",\"y\")"),
    ("+cmd|' /C calc'!A0", "'+cmd|' /C calc'!A0"),
    ("-2+3", "'-2+3"),
    ("@SUM(A1:A9)", "'@SUM(A1:A9)"),
    ("\t=1", "'\t=1"),
    ("\r=1", "'\r=1"),
    ("-5.25", "-5.25"),
    ("+1", "+1"),
    ("Great pasta", "Great pasta"),
    (" =1", " =1"),
    (None, ""),
    (-3, -3),
    (4.5, 4.5),
])
def test_csv_safe_cell(cell, expected):
    assert models.csv_safe_cell(cell) == expected


def _rows(text):
    return list(csv.reader(io.StringIO(text)))


def test_every_export_writes_formulas_as_text(db_path, monkeypatch):
    rid = _rid(db_path)
    models.save_reviews([models.Review(restaurant_id=rid, platform="google", external_id="g1", author="=Ann",
                                       rating=2, text="=HYPERLINK(\"http://evil\",\"click\")")], db_path=db_path)
    monkeypatch.setattr(models, "get_reviews_data",
                        lambda r: [{"review_date": "2026-09-01", "rating": 2, "text": "@SUM(1)", "response_status": "-x"}])
    rows = _rows(models.build_reviews_export_csv(rid))
    assert rows[1] == ["2026-09-01", "2", "'@SUM(1)", "'-x"]
    conn = models.get_conn(db_path)
    conn.execute("INSERT INTO ingredients (restaurant_id, name, unit, current_stock, par_level, unit_cost) "
                 "VALUES (?, '=cmd()', '+lb', -2, 3, 1.5)", (rid,))
    conn.commit()
    conn.close()
    food = _rows(models.build_food_cost_export_csv(rid, db_path=db_path))
    assert food[1][:3] == ["'=cmd()", "'+lb", "-2.0"]


def test_the_review_export_route_neutralises_reviewer_text(db_path, monkeypatch):
    import admin_routes
    rid = _rid(db_path)
    monkeypatch.setattr(auth, "get_current_user", lambda: {"id": 5, "restaurant_id": rid, "is_admin": 0,
                                                           "role": "client", "username": "o"})
    monkeypatch.setattr(admin_routes, "get_reviews_data", lambda r: [
        {"review_date": "2026-09-01", "author": "=1+1", "platform": "google", "rating": 1,
         "text": "+SUM(A1)", "draft_response": "@x", "response_status": "pending"}])
    app = Flask(__name__)
    app.register_blueprint(admin_routes.admin_bp)
    with app.test_request_context("/api/export-reviews"):
        monkeypatch.setattr(auth, "_billing_blocked", lambda u: False)
        resp = admin_routes.export_reviews()
    rows = _rows(resp.get_data(as_text=True))
    assert rows[1][1] == "'=1+1" and rows[1][6] == "'+SUM(A1)" and rows[1][7] == "'@x"


def test_no_export_uses_a_bare_csv_writer():
    """Ratchet: the user-facing exports go through models.safe_csv_writer."""
    import inspect
    import admin_routes
    import client_api
    for fn in (models.build_reviews_export_csv, models.build_labor_export_csv, models.build_food_cost_export_csv,
               admin_routes.export_reviews, client_api.download_schedule):
        src = inspect.getsource(fn)
        assert "safe_csv_writer" in src and "csv.writer(" not in src, fn.__name__


# ── #143: one POS store, one restaurant ─────────────────────────────────────

def test_pos_binding_conflict_names_the_live_restaurant_and_exempts_demos(db_path):
    live = _rid(db_path, "Erik Live")
    update_restaurant(live, {"toast_restaurant_guid": "ABC-123", "rpower_cg": 1280, "rpower_store_mid": "77"})
    other = _rid(db_path, "Other")
    assert models.pos_binding_conflict("toast", "abc-123 ", exclude_id=other) == "Erik Live"
    assert models.pos_binding_conflict("toast", "ABC-123", exclude_id=live) is None
    assert models.pos_binding_conflict("rpower", (1280, "77"), exclude_id=other) == "Erik Live"
    assert models.pos_binding_conflict("rpower", (99, "77"), exclude_id=other) is None
    assert models.pos_binding_conflict("toast", "", exclude_id=other) is None
    update_restaurant(live, {"is_demo": 1})
    assert models.pos_binding_conflict("toast", "ABC-123", exclude_id=other) is None


@pytest.fixture
def admin_client(db_path):
    app = Flask(__name__)
    app.register_blueprint(toast_routes.toast_bp)
    app.register_blueprint(rpower_routes.rpower_bp)
    app.register_blueprint(square_routes.square_bp)
    app.register_blueprint(clover_routes.clover_bp)
    home = _rid(db_path, "HQ")
    uid = create_user(home, "will", "will@cavnar.test", "Admin-pass-2026", is_admin=True, db_path=db_path)
    c = app.test_client()
    c.set_cookie("session_token", create_session(uid, password_verified_at=True, db_path=db_path))
    c.set_cookie("csrf_js", CSRF)
    return c


def _post(c, url, body):
    return c.post(url, json=body, headers={"X-CSRF": CSRF})


def test_demo_toast_credentials_are_refused_on_a_live_restaurant_even_untested(db_path, admin_client):
    rid = _rid(db_path, "Live Grill")
    r = _post(admin_client, f"/admin/toast/save/{rid}",
              {"client_id": "demo", "client_secret": "demo", "restaurant_guid": "demo", "test": False})
    assert r.status_code == 400 and "demo" in r.get_json()["error"]
    assert get_restaurant(rid).toast_client_id is None
    demo = _rid(db_path, "Demo Grill", is_demo=1)
    ok = _post(admin_client, f"/admin/toast/save/{demo}",
               {"client_id": "demo", "client_secret": "demo", "restaurant_guid": "demo", "test": False})
    assert ok.get_json()["ok"] is True


def test_a_toast_guid_bound_elsewhere_is_refused(db_path, admin_client):
    live = _rid(db_path, "First Grill")
    update_restaurant(live, {"toast_restaurant_guid": "guid-1"})
    rid = _rid(db_path, "Second Grill")
    r = _post(admin_client, f"/admin/toast/save/{rid}",
              {"client_id": "id", "client_secret": "secret", "restaurant_guid": "GUID-1", "test": False})
    assert r.status_code == 400 and "First Grill" in r.get_json()["error"]
    assert get_restaurant(rid).toast_restaurant_guid is None


def test_square_and_clover_stores_bound_elsewhere_are_refused(db_path, admin_client):
    live = _rid(db_path, "Sq Live")
    update_restaurant(live, {"square_location_id": "LOC1", "clover_merchant_id": "M1"})
    rid = _rid(db_path, "Sq Two")
    sq = _post(admin_client, f"/admin/square/save/{rid}", {"access_token": "t", "location_id": "loc1"})
    cl = _post(admin_client, f"/admin/clover/save/{rid}", {"merchant_id": "M1", "api_token": "t"})
    assert sq.status_code == 400 and "Sq Live" in sq.get_json()["error"]
    assert cl.status_code == 400 and "Sq Live" in cl.get_json()["error"]


_STORE = {"cg": 1280, "store_mid": "1123", "name": "Simple EJ's", "timezone": "America/Chicago", "ot_dow": 1}


def test_an_rpower_store_bound_elsewhere_is_refused_before_anything_is_written(db_path, admin_client, monkeypatch):
    live = _rid(db_path, "EJ Live")
    update_restaurant(live, {"rpower_token": "t1", "rpower_cg": 1280, "rpower_store_mid": "1123"})
    rid = _rid(db_path, "EJ Copy")
    monkeypatch.setattr(rpower, "test_token", lambda token: {"ok": True, "stores": [dict(_STORE)]})
    r = _post(admin_client, f"/admin/rpower/save/{rid}", {"token": "t2"})
    assert r.status_code == 400 and "EJ Live" in r.get_json()["error"]
    assert get_restaurant(rid).rpower_token is None
    # And through the chooser (bootstrap) as well.
    update_restaurant(rid, {"rpower_token": "t2"})
    out = rpower.bootstrap(rid, store_mid="1123")
    assert out["ok"] is False and "EJ Live" in out["error"]
    assert get_restaurant(rid).rpower_store_mid is None


def test_a_new_rpower_token_clears_the_old_binding(db_path, admin_client, monkeypatch):
    rid = _rid(db_path, "Rebind")
    update_restaurant(rid, {"rpower_token": "old", "rpower_cg": 5, "rpower_store_mid": "55",
                            "rpower_store_name": "Old Store", "rpower_verified_at": "2026-09-01T00:00:00",
                            "rpower_last_synced": "2026-09-28T05:00:00"})
    two = [dict(_STORE, store_mid="901", cg=9), dict(_STORE, store_mid="902", cg=9, name="Other")]
    monkeypatch.setattr(rpower, "test_token", lambda token: {"ok": True, "stores": two})
    r = _post(admin_client, f"/admin/rpower/save/{rid}", {"token": "new"})
    assert r.get_json()["needs_choice"] is True
    row = get_restaurant(rid)
    assert row.rpower_token == "new"
    assert (row.rpower_cg, row.rpower_store_mid, row.rpower_store_name, row.rpower_verified_at) == (None,) * 4
    assert row.rpower_last_synced is None, "the old store's sync stamp is not this connection's"


def test_re_saving_the_same_rpower_token_keeps_its_binding(db_path, admin_client, monkeypatch):
    rid = _rid(db_path, "Same Token")
    update_restaurant(rid, {"rpower_token": "tok", "rpower_cg": 1280, "rpower_store_mid": "1123",
                            "rpower_last_synced": "2026-09-28T05:00:00"})
    monkeypatch.setattr(rpower, "test_token", lambda token: {"ok": True, "stores": [dict(_STORE)]})
    monkeypatch.setattr(rpower, "bootstrap", lambda rid_, store_mid=None: {"ok": True, "store": dict(_STORE)})
    r = _post(admin_client, f"/admin/rpower/save/{rid}", {"token": "tok"})
    assert r.get_json()["ok"] is True
    row = get_restaurant(rid)
    assert row.rpower_store_mid == "1123" and row.rpower_last_synced == "2026-09-28T05:00:00"
