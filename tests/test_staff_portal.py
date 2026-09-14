"""The staff portal, end to end — and the security boundary around it.

Half of this file is the happy path. The other half is deliberate attack:
an employee session pointed at owner routes, a tampered membership_id, a
guessed task id from another role, a portal token from another restaurant.
Those are the tests that matter — the whole employee tier is only safe if
the console stays closed to it.
"""
import pytest
from flask import Flask

import auth
import client_api
import mobile_api
import models
from auth import (create_session, create_staff_session, create_user,
                  get_or_create_staff_portal_token, init_auth,
                  rotate_staff_portal_token, set_membership_pin,
                  upsert_membership)
from models import Restaurant, create_restaurant
from staff_routes import staff_bp


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, client_api, mobile_api):
        monkeypatch.setattr(mod, "get_conn", redirect)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    init_auth(db_path=db_path)


@pytest.fixture
def app(db_path, monkeypatch):
    flask_app = Flask(__name__, template_folder="../templates")
    flask_app.register_blueprint(staff_bp)
    flask_app.register_blueprint(client_api.client_bp)
    flask_app.register_blueprint(mobile_api.mobile_bp)
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


def _restaurant(db_path, name="Simple EJ's"):
    return create_restaurant(Restaurant(name=name, owner_email="o@x.test"), db_path=db_path)


def _staff(db_path, rid, username="jordan", name="Jordan P.", pin="8317"):
    uid = create_user(rid, username, f"{username}@x.test", "unused", db_path=db_path)
    m = upsert_membership(uid, rid, "employee", employee_name=name, db_path=db_path)
    if pin:
        set_membership_pin(m["id"], rid, pin, db_path=db_path)
    return uid, m["id"]


def _owner(db_path, rid, username="erik"):
    return create_user(rid, username, f"{username}@x.test", "owner-pw", db_path=db_path)


def _sign_in_staff(client, db_path, rid, uid):
    client.set_cookie("staff_session", create_staff_session(uid, rid, db_path=db_path))


# ── sign in ────────────────────────────────────────────────────────────────

def test_the_portal_link_lists_only_staff_with_a_pin(client, db_path):
    rid = _restaurant(db_path)
    _staff(db_path, rid, username="jordan", name="Jordan P.", pin="8317")
    _staff(db_path, rid, username="dana", name="Dana K.", pin=None)
    token = get_or_create_staff_portal_token(rid, db_path=db_path)

    body = client.get(f"/staff/r/{token}").data.decode()
    assert "Jordan P." in body
    assert "Dana K." not in body


def test_a_revoked_portal_link_stops_working(client, db_path):
    rid = _restaurant(db_path)
    _staff(db_path, rid)
    old = get_or_create_staff_portal_token(rid, db_path=db_path)
    new = rotate_staff_portal_token(rid, db_path=db_path)
    assert old != new
    assert client.get(f"/staff/r/{old}").status_code == 404
    assert client.get(f"/staff/r/{new}").status_code == 200


def test_signing_in_with_the_right_pin_starts_a_shift_session(client, db_path):
    rid = _restaurant(db_path)
    _uid, mid = _staff(db_path, rid, pin="8317")
    token = get_or_create_staff_portal_token(rid, db_path=db_path)
    resp = client.post(f"/staff/r/{token}/login",
                       json={"membership_id": mid, "pin": "8317"})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    assert client.get_cookie("staff_session") is not None


def test_signing_in_with_the_wrong_pin_is_refused(client, db_path):
    rid = _restaurant(db_path)
    _uid, mid = _staff(db_path, rid, pin="8317")
    token = get_or_create_staff_portal_token(rid, db_path=db_path)
    resp = client.post(f"/staff/r/{token}/login",
                       json={"membership_id": mid, "pin": "0000"})
    assert resp.status_code == 401
    assert client.get_cookie("staff_session") is None


def test_a_membership_from_another_restaurant_cannot_sign_in_through_this_link(client, db_path):
    """The portal token names the restaurant; the membership_id comes from
    the client. Pairing a real foreign membership with this token must fail."""
    rid_a = _restaurant(db_path, "Simple EJ's")
    rid_b = _restaurant(db_path, "Gia Mia")
    _uid, foreign_mid = _staff(db_path, rid_b, username="stranger", pin="8317")
    token_a = get_or_create_staff_portal_token(rid_a, db_path=db_path)

    resp = client.post(f"/staff/r/{token_a}/login",
                       json={"membership_id": foreign_mid, "pin": "8317"})
    assert resp.status_code == 401
    assert client.get_cookie("staff_session") is None


# ── the portal itself ──────────────────────────────────────────────────────

def test_the_portal_needs_a_session(client, db_path):
    for path in ("/staff/api/me", "/staff/api/shifts", "/staff/api/tasks"):
        assert client.get(path).status_code == 401, path


def test_me_returns_only_this_employees_own_details(client, db_path):
    rid = _restaurant(db_path, "Simple EJ's")
    uid, _mid = _staff(db_path, rid, name="Jordan P.")
    _sign_in_staff(client, db_path, rid, uid)
    body = client.get("/staff/api/me").get_json()
    assert body["ok"] is True
    assert body["employee"]["name"] == "Jordan P."
    assert body["employee"]["restaurant"] == "Simple EJ's"
    assert "pin_hash" not in str(body)


def test_shifts_are_empty_rather_than_sample_data_when_nothing_is_published(client, db_path):
    """labor.load_shifts_for_restaurant falls back to bundled SAMPLE shifts.
    Showing an employee invented shifts as their own roster would be the
    worst possible version of this feature."""
    rid = _restaurant(db_path)
    uid, _mid = _staff(db_path, rid)
    _sign_in_staff(client, db_path, rid, uid)
    body = client.get("/staff/api/shifts").get_json()
    assert body["ok"] is True
    assert body["published"] is False
    assert body["upcoming"] == []


# ── the console stays closed ───────────────────────────────────────────────

def test_an_employee_session_cannot_reach_owner_web_routes(client, db_path):
    """The single most important test in this file. A staff PIN session is a
    real row in the same sessions table; without the console gate it would
    open every @login_required route in the app."""
    rid = _restaurant(db_path)
    uid, _mid = _staff(db_path, rid)
    client.set_cookie("session_token", create_staff_session(uid, rid, db_path=db_path))

    for path in ("/api/home/brief", "/api/labor/team", "/api/account/staff",
                 "/api/team/inbox", "/api/food-cost/menu-profitability"):
        resp = client.get(path)
        assert resp.status_code == 403, f"{path} returned {resp.status_code}"
        assert resp.get_json().get("staff_account") is True, path


def test_an_employee_session_cannot_reach_owner_mobile_routes(client, db_path):
    rid = _restaurant(db_path)
    uid, _mid = _staff(db_path, rid)
    token = create_staff_session(uid, rid, db_path=db_path)
    headers = {"Authorization": f"Bearer {token}"}
    for path in ("/mobile/api/labor/team", "/mobile/api/account/staff"):
        resp = client.get(path, headers=headers)
        assert resp.status_code == 403, f"{path} returned {resp.status_code}"


def test_an_employee_cannot_create_staff_accounts_or_reset_pins(client, db_path):
    """Privilege escalation: the employee tier must not be able to mint
    itself a console account or take over a colleague's PIN."""
    rid = _restaurant(db_path)
    uid, mid = _staff(db_path, rid)
    client.set_cookie("session_token", create_staff_session(uid, rid, db_path=db_path))

    assert client.post("/api/account/staff",
                       json={"employee_name": "Mole", "pin": "8317"}).status_code == 403
    assert client.post(f"/api/account/staff/{mid}/pin", json={"pin": "5063"}).status_code == 403
    assert client.post(f"/api/account/staff/{mid}/unlock").status_code == 403


def test_an_owner_session_cannot_use_the_staff_portal(client, db_path):
    """The mirror image: two products, and a session does not drift between
    them. An owner who wants the staff view signs in with their own PIN."""
    rid = _restaurant(db_path)
    owner_id = _owner(db_path, rid)
    client.set_cookie("staff_session", create_session(owner_id, db_path=db_path))
    assert client.get("/staff/api/me").status_code == 403


# ── cross-employee and cross-tenant ────────────────────────────────────────

def test_an_employee_cannot_check_off_another_roles_task(client, db_path):
    """template_id is a guessable integer. It is checked against the caller's
    own job role rather than trusted."""
    rid = _restaurant(db_path)
    uid, _mid = _staff(db_path, rid, name="Jordan P.")
    from models import add_task_template
    other = add_task_template(rid, "Line Cook", "Check walk-in temp", db_path=db_path)
    _sign_in_staff(client, db_path, rid, uid)

    resp = client.post("/staff/api/tasks/complete",
                       json={"template_id": other["id"], "task_date": "2026-09-14", "done": True})
    assert resp.status_code == 403
    from models import get_todays_tasks
    assert get_todays_tasks(rid, "Line Cook", task_date="2026-09-14",
                            db_path=db_path)[0]["done"] is False


def test_an_employee_cannot_check_off_another_restaurants_task(client, db_path):
    rid_a = _restaurant(db_path, "Simple EJ's")
    rid_b = _restaurant(db_path, "Gia Mia")
    uid, _mid = _staff(db_path, rid_a, name="Jordan P.")
    from models import add_task_template
    foreign = add_task_template(rid_b, "Server", "Roll silverware", db_path=db_path)
    _sign_in_staff(client, db_path, rid_a, uid)

    resp = client.post("/staff/api/tasks/complete",
                       json={"template_id": foreign["id"], "task_date": "2026-09-14", "done": True})
    assert resp.status_code == 403


def test_changing_your_pin_requires_the_current_one(client, db_path):
    rid = _restaurant(db_path)
    uid, _mid = _staff(db_path, rid, pin="8317")
    _sign_in_staff(client, db_path, rid, uid)
    resp = client.post("/staff/api/pin", json={"current_pin": "0000", "new_pin": "5063"})
    assert resp.status_code == 401


def test_changing_your_pin_works_and_ends_the_session(client, db_path):
    rid = _restaurant(db_path)
    uid, mid = _staff(db_path, rid, pin="8317")
    _sign_in_staff(client, db_path, rid, uid)
    resp = client.post("/staff/api/pin", json={"current_pin": "8317", "new_pin": "5063"})
    assert resp.status_code == 200
    from auth import verify_membership_pin
    assert verify_membership_pin(mid, rid, "5063", db_path=db_path)["ok"] is True
    # The session that made the change is gone — the portal re-authenticates.
    assert client.get("/staff/api/me").status_code == 401


# ── owner management ───────────────────────────────────────────────────────

def test_an_owner_can_create_a_staff_account_and_it_can_sign_in(client, db_path):
    rid = _restaurant(db_path)
    owner_id = _owner(db_path, rid)
    client.set_cookie("session_token", create_session(owner_id, db_path=db_path))

    made = client.post("/api/account/staff",
                       json={"employee_name": "Jordan P.", "pin": "8317"}).get_json()
    assert made["ok"] is True

    listed = client.get("/api/account/staff").get_json()
    assert listed["ok"] is True
    row = [s for s in listed["staff"] if s["name"] == "Jordan P."][0]
    assert row["has_pin"] is True
    assert "pin_hash" not in str(listed)

    client.set_cookie("session_token", "")
    token = get_or_create_staff_portal_token(rid, db_path=db_path)
    signin = client.post(f"/staff/r/{token}/login",
                         json={"membership_id": made["membership_id"], "pin": "8317"})
    assert signin.status_code == 200


def test_a_staff_account_gets_an_unusable_password(client, db_path):
    """A PIN identity must not also be a password login — that would be a
    second, weaker way into the same account."""
    rid = _restaurant(db_path)
    owner_id = _owner(db_path, rid)
    client.set_cookie("session_token", create_session(owner_id, db_path=db_path))
    made = client.post("/api/account/staff",
                       json={"employee_name": "Jordan P.", "pin": "8317"}).get_json()

    from auth import get_user_by_id, verify_password
    user = get_user_by_id(made["user_id"], db_path=db_path)
    for guess in ("", "8317", "password", "staff"):
        assert verify_password(user["username"], guess, db_path=db_path) is None


def test_an_owner_cannot_reset_a_pin_at_another_restaurant(client, db_path):
    rid_a = _restaurant(db_path, "Simple EJ's")
    rid_b = _restaurant(db_path, "Gia Mia")
    owner_a = _owner(db_path, rid_a, username="erik")
    _uid, foreign_mid = _staff(db_path, rid_b, username="stranger", pin="8317")
    client.set_cookie("session_token", create_session(owner_a, db_path=db_path))

    resp = client.post(f"/api/account/staff/{foreign_mid}/pin", json={"pin": "5063"})
    assert resp.status_code == 404
    from auth import verify_membership_pin
    assert verify_membership_pin(foreign_mid, rid_b, "8317", db_path=db_path)["ok"] is True


def test_deactivating_a_staff_account_ends_its_session_immediately(client, db_path):
    rid = _restaurant(db_path)
    owner_id = _owner(db_path, rid)
    uid, mid = _staff(db_path, rid)
    staff_token = create_staff_session(uid, rid, db_path=db_path)
    client.set_cookie("staff_session", staff_token)
    assert client.get("/staff/api/me").status_code == 200

    client.set_cookie("staff_session", "")
    client.set_cookie("session_token", create_session(owner_id, db_path=db_path))
    assert client.post(f"/api/account/staff/{mid}/deactivate").status_code == 200

    client.set_cookie("session_token", "")
    client.set_cookie("staff_session", staff_token)
    assert client.get("/staff/api/me").status_code == 401


def test_the_roster_only_shows_this_restaurants_staff(client, db_path):
    rid_a = _restaurant(db_path, "Simple EJ's")
    rid_b = _restaurant(db_path, "Gia Mia")
    owner_a = _owner(db_path, rid_a, username="erik")
    _staff(db_path, rid_a, username="jordan", name="Jordan P.")
    _staff(db_path, rid_b, username="stranger", name="Someone Else")
    client.set_cookie("session_token", create_session(owner_a, db_path=db_path))

    listed = client.get("/api/account/staff").get_json()
    names = [s["name"] for s in listed["staff"]]
    assert names == ["Jordan P."]
