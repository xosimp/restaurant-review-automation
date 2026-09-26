"""Staff availability is scoped to the session's restaurant.

The web Labor roster used to read and write `/admin/staff-availability/<id>`,
routes guarded only by `@login_required` with the restaurant taken from the
URL — so any client login could read, overwrite or delete another
restaurant's staff availability (a cross-tenant IDOR). What these protect:

  * the admin routes refuse a client login outright (admin only);
  * the web roster's `/api/labor/availability` reads and writes only the
    session's own restaurant, and shares one body with the phone's
    `/mobile/api/labor/availability`;
  * a manager's save follows the same rule as an employee's own
    (staff_routes.availability_from_submission, CLIENT-11);
  * dashboard.html calls the scoped route and never takes a restaurant id
    from the page;
  * no admin_routes route takes `<int:restaurant_id>` from the URL behind
    `@login_required` alone.
"""
import json
import os
import re
import sys

import pytest
from flask import Flask

import admin_routes
import auth
import auth_routes
import client_api
import mobile_api
import models
from admin_routes import admin_bp
from auth import create_session, create_user, init_auth
from client_api import client_bp
from mobile_api import mobile_bp
from models import Restaurant, create_restaurant, get_staff_availability, save_staff_availability

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in list(sys.modules.values()):
        try:
            if getattr(mod, "get_conn", None) is real:
                monkeypatch.setattr(mod, "get_conn", redirect)
        except Exception:
            pass
    for mod in (models, auth, client_api, mobile_api, admin_routes):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    for mod in (models, auth):
        monkeypatch.setattr(mod, "DB_PATH", db_path, raising=False)
    init_auth(db_path=db_path)
    models.init_staff_availability(db_path=db_path)
    yield


@pytest.fixture
def app():
    flask_app = Flask(__name__, template_folder=os.path.join(ROOT, "templates"))
    flask_app.register_blueprint(client_bp)
    flask_app.register_blueprint(mobile_bp)
    flask_app.register_blueprint(admin_bp)
    flask_app.register_blueprint(auth_routes.auth_bp)   # admin_required redirects to auth.login
    return flask_app


def _tenant(app, name, username):
    rid = create_restaurant(Restaurant(name=name, owner_email=f"{username}@x.test"))
    uid = create_user(rid, username, f"{username}@x.test", "correct-horse-1")
    c = app.test_client()
    c.set_cookie("session_token", create_session(uid))
    return rid, c


# ── the IDOR: a client login against another restaurant's id ────────────────

def test_admin_availability_routes_refuse_a_client_login_for_another_restaurant(app):
    rid_a, client_a = _tenant(app, "Restaurant A", "alice")
    rid_b, _ = _tenant(app, "Restaurant B", "bob")
    save_staff_availability(rid_b, "Bob's Cook", DAYS[:5], DAYS[5:], notes="no weekends")

    # A GET off /api/ is a page navigation to admin_required: a redirect
    # to sign in, never the rows.
    r = client_a.get(f"/admin/staff-availability/{rid_b}")
    assert r.status_code in (302, 401, 403, 404)
    assert "Bob's Cook" not in r.get_data(as_text=True)

    r = client_a.post(f"/admin/staff-availability/{rid_b}",
                      json={"employee_name": "Bob's Cook", "available_days": [], "unavailable_days": DAYS})
    assert r.status_code in (401, 403, 404)
    r = client_a.post(f"/admin/staff-availability/{rid_b}/delete", json={"employee_name": "Bob's Cook"})
    assert r.status_code in (401, 403, 404)

    rows = get_staff_availability(rid_b)
    assert len(rows) == 1 and json.loads(rows[0]["unavailable_days"]) == ["Saturday", "Sunday"]


def test_admin_availability_routes_refuse_a_client_login_for_its_own_restaurant_too(app):
    # Not a tenant check bolted on: the admin routes are internal-admin only.
    rid_a, client_a = _tenant(app, "Restaurant A", "alice")
    r = client_a.get(f"/admin/staff-availability/{rid_a}")
    assert r.status_code in (302, 401, 403) and "availability" not in (r.get_json(silent=True) or {})
    r = client_a.post(f"/admin/staff-availability/{rid_a}", json={"employee_name": "Ana"})
    assert r.status_code in (401, 403)


def test_the_web_route_reads_and_writes_only_its_own_restaurant(app):
    rid_a, client_a = _tenant(app, "Restaurant A", "alice")
    rid_b, client_b = _tenant(app, "Restaurant B", "bob")
    save_staff_availability(rid_b, "Bob's Cook", DAYS, None, notes="private to B")

    r = client_a.post("/api/labor/availability",
                      json={"employee_name": "Ana", "available_days": DAYS[:5], "unavailable_days": DAYS[5:],
                            "notes": "school on weekends"})
    assert r.get_json()["ok"] is True

    mine = client_a.get("/api/labor/availability").get_json()["availability"]
    assert [e["employee_name"] for e in mine] == ["Ana"]
    # Decoded lists, the same shape the phone gets — not JSON strings.
    assert mine[0]["available_days"] == DAYS[:5]
    assert mine[0]["unavailable_days"] == ["Saturday", "Sunday"]
    assert mine[0]["notes"] == "school on weekends"

    theirs = client_b.get("/api/labor/availability").get_json()["availability"]
    assert [e["employee_name"] for e in theirs] == ["Bob's Cook"]

    # A delete names only an employee; it can only ever reach its own rows.
    client_a.post("/api/labor/availability/delete", json={"employee_name": "Bob's Cook"})
    assert len(get_staff_availability(rid_b)) == 1
    client_a.post("/api/labor/availability/delete", json={"employee_name": "Ana"})
    assert get_staff_availability(rid_a) == []


def test_web_and_phone_share_one_body():
    src = _read("client_api.py")
    for web, mob in (("labor_availability", "mobile_labor_availability"),
                     ("labor_availability_save", "mobile_labor_availability_save"),
                     ("labor_availability_delete", "mobile_labor_availability_delete")):
        m = re.search(r"def %s\(current_user\):\n    return _m\(\"%s\"\)\(current_user\)" % (web, mob), src)
        assert m, f"{web} must delegate to mobile_api.{mob}"


# ── a manager's save follows the one availability rule ──────────────────────

def test_manager_save_makes_available_the_complement_of_blocked(app):
    rid, c = _tenant(app, "Restaurant A", "alice")
    # Only available days sent: every day not ticked is blocked.
    c.post("/api/labor/availability", json={"employee_name": "Ana", "available_days": ["monday", "Tuesday "]})
    row = c.get("/api/labor/availability").get_json()["availability"][0]
    assert row["available_days"] == ["Monday", "Tuesday"]
    assert row["unavailable_days"] == DAYS[2:]
    # Lists that disagree: a day in neither is not assumed workable.
    c.post("/api/labor/availability", json={"employee_name": "Ana", "available_days": ["Monday", "Tuesday"],
                                            "unavailable_days": ["Saturday"]})
    row = c.get("/api/labor/availability").get_json()["availability"][0]
    assert row["available_days"] == ["Monday", "Tuesday"]
    assert set(row["available_days"]).isdisjoint(row["unavailable_days"])
    assert sorted(row["available_days"] + row["unavailable_days"], key=DAYS.index) == DAYS


def test_manager_save_refuses_every_day_blocked(app):
    rid, c = _tenant(app, "Restaurant A", "alice")
    r = c.post("/api/labor/availability", json={"employee_name": "Ana", "available_days": [],
                                                "unavailable_days": DAYS})
    assert r.status_code == 400 and r.get_json()["ok"] is False
    assert get_staff_availability(rid) == []


def test_manager_save_trims_the_note_and_blanks_to_none(app):
    rid, c = _tenant(app, "Restaurant A", "alice")
    c.post("/api/labor/availability", json={"employee_name": "Ana", "available_days": DAYS, "notes": "x" * 400})
    assert len(get_staff_availability(rid)[0]["notes"]) == 300
    c.post("/api/labor/availability", json={"employee_name": "Ana", "available_days": DAYS, "notes": "   "})
    assert get_staff_availability(rid)[0]["notes"] is None


# ── the web roster calls the scoped route ───────────────────────────────────

def _fn(src, name):
    i = src.index("function %s(" % name)
    j = src.index("\nfunction ", i + 1)
    return src[i:j]


def test_dashboard_roster_calls_the_scoped_route_and_escapes_names():
    src = _read("templates", "dashboard.html")
    assert "/admin/staff-availability" not in src
    assert "_restaurantId" not in src
    load = _fn(src, "loadAvailability")
    assert "fetch('/api/labor/availability')" in load
    assert "JSON.parse" not in load                       # lists arrive decoded
    assert "_escHtml(av.employee_name" in load and "_escHtml(String(av.notes))" in load
    assert "av.employee_name.replace" not in load          # no name in an onclick
    assert "fetch('/api/labor/availability'," in _fn(src, "saveAvailability")
    assert "fetch('/api/labor/availability/delete'," in _fn(src, "deleteAvailability")


# ── the sweep: no admin route trusts a URL restaurant behind login alone ────

def test_no_admin_route_takes_a_restaurant_id_behind_login_required_alone():
    src = _read("admin_routes.py").split("\n")
    offenders = []
    for i, line in enumerate(src):
        if line.startswith("@admin_bp.route") and "<int:restaurant_id>" in line:
            j, decs = i + 1, []
            while src[j].startswith("@"):
                decs.append(src[j]); j += 1
            if not any("admin_required" in d for d in decs):
                offenders.append(line)
    assert offenders == []
