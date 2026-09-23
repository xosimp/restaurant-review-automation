"""Edge cases on the staff portal's PIN sign-in, from the SEC audit.

The staff portal is the product's widest door: a 4-digit PIN, a join code on
a whiteboard, a shared tablet on the pass. These tests protect what must stay
true behind that door:

- a PIN session acts only at the restaurant it was minted for, as an
  employee, and never falls back to an owner role (SEC-1, Permissions #14);
- a membership promoted out of the employee tier can no longer use its PIN
  (SEC-14);
- a busy shift change on one restaurant Wi-Fi is not refused, while a
  spray of wrong guesses from one address still is (SEC-18);
- repeated lockouts escalate instead of resetting every 15 minutes (SEC-19);
- a staff identity is never shown or counted as an owner (SEC-29,
  Permissions #15);
- a browser sign-in never hands the session token to page JavaScript
  (SEC-36);
- a roster login nonce works exactly once (appendix item 17).

Tests marked xfail(strict=True) assert the CORRECT behaviour for a confirmed
defect; they flip to failures the day the defect is fixed, so the marker is
removed with the fix.
"""
from datetime import datetime, timedelta

import pytest
from flask import Flask

import auth
import client_api
import mobile_api
import models
from auth import (PIN_LOCKOUT_MINUTES, PIN_MAX_ATTEMPTS, create_session,
                  create_user, get_join_code, get_memberships_for_restaurant,
                  get_or_create_staff_portal_token, get_session_user, init_auth,
                  pin_lockout_state, set_membership_active, set_membership_pin,
                  unlink_claimed_membership, upsert_membership,
                  verify_membership_pin)
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
def app(db_path):
    flask_app = Flask(__name__, template_folder="../templates")
    flask_app.register_blueprint(staff_bp)
    flask_app.register_blueprint(client_api.client_bp)
    flask_app.register_blueprint(mobile_api.mobile_bp)
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


# ── helpers ──────────────────────────────────────────────────────────────

def _restaurant(db_path, name, owner_email):
    rid = create_restaurant(Restaurant(name=name, owner_email=owner_email), db_path=db_path)
    models.init_manual_team_members(db_path)
    return rid


def _owner(db_path, rid, username):
    uid = create_user(rid, username, f"{username}@x.test", "owner-pw-123", db_path=db_path)
    upsert_membership(uid, rid, "client", db_path=db_path)
    return uid


def _staff(db_path, rid, username, name, pin="8317"):
    uid = create_user(rid, username, f"{username}@staff.invalid", "x" * 24, db_path=db_path)
    m = upsert_membership(uid, rid, "employee", employee_name=name, db_path=db_path)
    if pin:
        set_membership_pin(m["id"], rid, pin, db_path=db_path)
    return uid, m["id"]


def _signup(client, db_path, rid, phone, name, pin):
    """Self-signup the way the portal does it; dev_code comes back because
    Twilio is unconfigured in tests, so no SMS is sent."""
    start = client.post("/staff/api/signup/start", json={"phone": phone, "optin": True}).get_json()
    tok = client.post("/staff/api/signup/verify",
                      json={"phone": phone, "code": start["dev_code"]}).get_json()["signup_token"]
    return client.post("/staff/api/signup/claim", json={
        "signup_token": tok, "join_code": get_join_code(rid, db_path=db_path),
        "employee_name": name, "pin": pin})


def _pin_login(client, code, membership_id, pin, ip=None):
    env = {"REMOTE_ADDR": ip} if ip else {}
    roster = client.get(f"/staff/api/roster/{code}", environ_base=env)
    nonce = (roster.get_json() or {}).get("login_nonce", "")
    return client.post(f"/staff/r/{code}/login",
                       json={"membership_id": membership_id, "pin": pin, "nonce": nonce},
                       environ_base=env)


def _employee_membership_id(db_path, rid):
    return get_memberships_for_restaurant(rid, role="employee", db_path=db_path)[0]["id"]


def _two_job_employee(client, db_path):
    """Jordan works at A and B: one identity (same phone), two memberships.
    Returns (A, B, membership at A, membership at B)."""
    a = _restaurant(db_path, "Alpha Cafe", "a@x.test")
    b = _restaurant(db_path, "Bravo Bistro", "b@x.test")
    models.add_manual_team_member(a, "Jordan P.", "Bartender", db_path=db_path)
    models.add_manual_team_member(b, "Jordan P.", "Server", db_path=db_path)
    _owner(db_path, a, "ownera")
    _owner(db_path, b, "ownerb")
    r1 = _signup(client, db_path, a, "5550142233", "Jordan P.", "5063").get_json()
    r2 = _signup(client, db_path, b, "5550142233", "Jordan P.", "7412").get_json()
    assert r1["ok"] and r2["ok"], (r1, r2)
    return a, b, _employee_membership_id(db_path, a), _employee_membership_id(db_path, b)


def _unlinked_at_home_signed_in_elsewhere(client, db_path):
    """A's owner unlinks Jordan; Jordan PIN-signs-in on B's tablet.
    Returns (A, B, membership at B, session token)."""
    a, b, mid_a, mid_b = _two_job_employee(client, db_path)
    assert unlink_claimed_membership(mid_a, a, db_path=db_path)
    resp = _pin_login(client, get_join_code(b, db_path=db_path), mid_b, "7412")
    assert resp.status_code == 200, resp.get_json()
    token = client.get_cookie("staff_session").value
    return a, b, mid_b, token


# ── SEC-1 / appendix 12-13 / Permissions 14: a PIN session's restaurant and role ──

def test_a_pin_session_minted_at_the_second_restaurant_acts_there(client, db_path):
    a, b, mid_a, mid_b = _two_job_employee(client, db_path)
    resp = _pin_login(client, get_join_code(b, db_path=db_path), mid_b, "7412")
    assert resp.status_code == 200
    user = get_session_user(client.get_cookie("staff_session").value, db_path=db_path)
    assert user["restaurant_id"] == b
    assert user["membership_id"] == mid_b


def test_a_self_signed_up_staff_identity_is_stored_as_an_employee(client, db_path):
    rid = _restaurant(db_path, "Alpha Cafe", "a@x.test")
    models.add_manual_team_member(rid, "Dana K.", "Server", db_path=db_path)
    assert _signup(client, db_path, rid, "5550149988", "Dana K.", "5063").get_json()["ok"]
    identity = auth.get_user_by_phone("5550149988", db_path=db_path)
    assert identity["role"] == "employee"


def test_an_owner_created_staff_identity_is_stored_as_an_employee(client, db_path):
    rid = _restaurant(db_path, "Alpha Cafe", "a@x.test")
    owner = _owner(db_path, rid, "erik")
    h = {"Authorization": "Bearer " + create_session(owner, device_type="ios", db_path=db_path)}
    made = client.post("/mobile/api/account/staff",
                       json={"employee_name": "Dana K.", "pin": "5063"}, headers=h).get_json()
    assert made["ok"], made
    identity = auth.get_user_by_id(made["user_id"], db_path=db_path)
    assert identity["role"] == "employee"


def test_an_identity_unlinked_at_home_cannot_open_the_home_staff_and_team_screens(client, db_path):
    _a, _b, _mid_b, token = _unlinked_at_home_signed_in_elsewhere(client, db_path)
    h = {"Authorization": f"Bearer {token}"}
    statuses = {path: client.get(path, headers=h).status_code for path in ("/mobile/api/account/staff", "/mobile/api/account/team")}
    assert all(code in (401, 403) for code in statuses.values()), statuses


def test_an_identity_unlinked_at_home_cannot_read_home_margins_or_billing(client, db_path):
    _a, _b, _mid_b, token = _unlinked_at_home_signed_in_elsewhere(client, db_path)
    h = {"Authorization": f"Bearer {token}"}
    statuses = {path: client.get(path, headers=h).status_code for path in ("/mobile/api/food-cost/cogs", "/mobile/api/account/billing")}
    assert all(code in (401, 403) for code in statuses.values()), statuses


def test_an_identity_unlinked_at_home_still_works_its_shift_at_the_second_restaurant(client, db_path):
    _a, b, mid_b, token = _unlinked_at_home_signed_in_elsewhere(client, db_path)
    user = get_session_user(token, db_path=db_path)
    assert (user["restaurant_id"], user["role"], user["membership_id"]) == (b, "employee", mid_b)
    assert client.get("/staff/api/me").status_code == 200


def test_an_inactive_home_membership_grants_no_console_role(db_path):
    rid = _restaurant(db_path, "Alpha Cafe", "a@x.test")
    uid, mid = _staff(db_path, rid, "dana", "Dana K.")
    assert set_membership_active(mid, rid, False, db_path=db_path)
    user = get_session_user(create_session(uid, db_path=db_path), db_path=db_path)
    assert user is None or user["role"] not in ("client", "owner")


# ── SEC-14 / appendix 14: a promoted membership and its PIN ─────────────

def _promoted_staff(client, db_path):
    rid = _restaurant(db_path, "Alpha Cafe", "a@x.test")
    owner = _owner(db_path, rid, "erik")
    h = {"Authorization": "Bearer " + create_session(owner, device_type="ios", db_path=db_path)}
    made = client.post("/mobile/api/account/staff",
                       json={"employee_name": "Dana K.", "pin": "5063"}, headers=h).get_json()
    assert made["ok"], made
    up = client.post(f"/mobile/api/account/staff/{made['membership_id']}",
                     json={"role": "manager"}, headers=h).get_json()
    assert up["ok"] and up["staff"]["role"] == "manager", up
    return rid, made["membership_id"]


def test_a_promoted_membership_drops_off_the_pin_roster(client, db_path):
    rid, mid = _promoted_staff(client, db_path)
    code = get_or_create_staff_portal_token(rid, db_path=db_path)
    roster = client.get(f"/staff/api/roster/{code}").get_json()["roster"]
    assert mid not in [r["membership_id"] for r in roster]


def test_a_promoted_membership_cannot_sign_in_with_its_pin(client, db_path):
    rid, mid = _promoted_staff(client, db_path)
    code = get_or_create_staff_portal_token(rid, db_path=db_path)
    resp = _pin_login(client, code, mid, "5063")
    assert resp.status_code in (401, 403), resp.get_json()
    assert "token" not in (resp.get_json() or {})
    assert client.get_cookie("staff_session") is None


# ── SEC-18 / appendix 15: the per-address throttle at shift change ──────

def _roster_of(db_path, rid, n, pin="5063"):
    """n employees who all hold `pin`. The hash is computed once and copied,
    so the setup does not pay the KDF n times."""
    mids = []
    for i in range(n):
        _uid, mid = _staff(db_path, rid, f"emp{i}", f"Emp {i}", pin=None)
        mids.append(mid)
    set_membership_pin(mids[0], rid, pin, db_path=db_path)
    conn = models.get_conn(db_path)
    try:
        h = conn.execute("SELECT pin_hash FROM memberships WHERE id=?", (mids[0],)).fetchone()[0]
        conn.executemany("UPDATE memberships SET pin_hash=? WHERE id=?", [(h, m) for m in mids])
        conn.commit()
    finally:
        conn.close()
    return mids


def test_forty_successful_sign_ins_from_one_address_in_five_minutes_are_all_accepted(client, db_path):
    rid = _restaurant(db_path, "Big Kitchen", "o@x.test")
    mids = _roster_of(db_path, rid, 40)
    code = get_or_create_staff_portal_token(rid, db_path=db_path)
    statuses = [_pin_login(client, code, mid, "5063", ip="203.0.113.7").status_code
                for mid in mids]
    assert statuses == [200] * 40, statuses


def test_a_spray_of_wrong_pins_from_one_address_is_still_throttled(client, db_path):
    rid = _restaurant(db_path, "Big Kitchen", "o@x.test")
    mids = _roster_of(db_path, rid, 10)
    code = get_or_create_staff_portal_token(rid, db_path=db_path)
    statuses = []
    for i in range(40):
        resp = _pin_login(client, code, mids[i % len(mids)], "2580", ip="198.51.100.9")
        statuses.append(resp.status_code)
        if resp.status_code == 429:
            break
    assert 429 in statuses, statuses
    # The ceiling is on the address, not the portal: another device is unaffected.
    other = client.get(f"/staff/api/roster/{code}", environ_base={"REMOTE_ADDR": "203.0.113.50"})
    assert other.status_code == 200


# ── SEC-19 / appendix 16: lockout escalation ─────────────────────────────

def _expire_lock(db_path, mid):
    past = (datetime.utcnow() - timedelta(seconds=1)).isoformat()
    conn = models.get_conn(db_path)
    try:
        conn.execute("UPDATE membership_pin_attempts SET locked_until=? WHERE membership_id=?",
                     (past, mid))
        conn.commit()
    finally:
        conn.close()


def _miss_until_locked(db_path, rid, mid):
    for _ in range(PIN_MAX_ATTEMPTS):
        result = verify_membership_pin(mid, rid, "0000", db_path=db_path)
    assert result["locked"], result
    return pin_lockout_state(mid, db_path=db_path)


def test_a_lock_that_has_run_out_lets_the_right_pin_in(db_path):
    """Control for the escalation test: expiring locked_until really does
    end the lock, so the rewrite below simulates time passing faithfully."""
    rid = _restaurant(db_path, "Alpha Cafe", "a@x.test")
    _uid, mid = _staff(db_path, rid, "dana", "Dana K.", pin="8317")
    _miss_until_locked(db_path, rid, mid)
    assert verify_membership_pin(mid, rid, "8317", db_path=db_path)["locked"] is True
    _expire_lock(db_path, mid)
    assert pin_lockout_state(mid, db_path=db_path)["locked"] is False
    assert verify_membership_pin(mid, rid, "8317", db_path=db_path)["ok"] is True


def test_a_second_lockout_the_same_day_lasts_longer_than_the_first(db_path):
    rid = _restaurant(db_path, "Alpha Cafe", "a@x.test")
    _uid, mid = _staff(db_path, rid, "dana", "Dana K.", pin="8317")
    first = _miss_until_locked(db_path, rid, mid)
    assert first["seconds_remaining"] <= PIN_LOCKOUT_MINUTES * 60
    _expire_lock(db_path, mid)
    second = _miss_until_locked(db_path, rid, mid)
    assert second["seconds_remaining"] > PIN_LOCKOUT_MINUTES * 60, (first, second)


# ── SEC-29 / appendix 18 / Permissions 15: staff identities are not owners ──

def _owner_and_staff(client, db_path):
    rid = _restaurant(db_path, "Alpha Cafe", "a@x.test")
    owner = _owner(db_path, rid, "erik")
    h = {"Authorization": "Bearer " + create_session(owner, device_type="ios", db_path=db_path)}
    made = client.post("/mobile/api/account/staff",
                       json={"employee_name": "Dana K.", "pin": "5063"}, headers=h).get_json()
    assert made["ok"], made
    return rid, owner, made["user_id"], h


def test_a_staff_identity_never_appears_in_the_team_list(client, db_path):
    _rid, owner, staff_uid, h = _owner_and_staff(client, db_path)
    team = client.get("/mobile/api/account/team", headers=h).get_json()
    ids = [m["id"] for m in team["members"]]
    assert owner in ids
    assert staff_uid not in ids


def test_a_staff_identity_is_not_counted_as_an_owner(client, db_path):
    rid, owner, _staff_uid, _h = _owner_and_staff(client, db_path)
    conn = models.get_conn(db_path)
    try:
        assert auth._principal_count(conn, rid, excluding=owner) == 0
    finally:
        conn.close()


# ── SEC-36: the session token stays out of page JavaScript ──────────────

def test_a_browser_pin_sign_in_response_carries_no_token_field(client, db_path):
    rid = _restaurant(db_path, "Alpha Cafe", "a@x.test")
    _uid, mid = _staff(db_path, rid, "dana", "Dana K.", pin="8317")
    code = get_or_create_staff_portal_token(rid, db_path=db_path)
    resp = _pin_login(client, code, mid, "8317")
    assert resp.status_code == 200
    assert client.get_cookie("staff_session") is not None
    assert "token" not in resp.get_json()


def test_a_browser_signup_claim_response_carries_no_token_field(client, db_path):
    rid = _restaurant(db_path, "Alpha Cafe", "a@x.test")
    models.add_manual_team_member(rid, "Dana K.", "Server", db_path=db_path)
    resp = _signup(client, db_path, rid, "5550149988", "Dana K.", "5063")
    assert resp.status_code == 200 and resp.get_json()["ok"]
    assert client.get_cookie("staff_session") is not None
    assert "token" not in resp.get_json()


# ── appendix 17: nonce replay ────────────────────────────────────────────

def test_a_login_nonce_cannot_be_replayed(client, db_path):
    rid = _restaurant(db_path, "Alpha Cafe", "a@x.test")
    _uid, mid = _staff(db_path, rid, "dana", "Dana K.", pin="8317")
    code = get_or_create_staff_portal_token(rid, db_path=db_path)
    nonce = client.get(f"/staff/api/roster/{code}").get_json()["login_nonce"]
    body = {"membership_id": mid, "pin": "8317", "nonce": nonce}
    first = client.post(f"/staff/r/{code}/login", json=body)
    assert first.status_code == 200
    replay = client.post(f"/staff/r/{code}/login", json=body)
    assert replay.status_code == 409
    assert replay.get_json().get("nonce_expired") is True
    assert "token" not in replay.get_json()


def test_a_login_nonce_from_another_restaurant_is_refused(client, db_path):
    a = _restaurant(db_path, "Alpha Cafe", "a@x.test")
    b = _restaurant(db_path, "Bravo Bistro", "b@x.test")
    _uid, mid_b = _staff(db_path, b, "dana", "Dana K.", pin="8317")
    code_a = get_or_create_staff_portal_token(a, db_path=db_path)
    code_b = get_or_create_staff_portal_token(b, db_path=db_path)
    nonce_a = client.get(f"/staff/api/roster/{code_a}").get_json()["login_nonce"]
    resp = client.post(f"/staff/r/{code_b}/login",
                       json={"membership_id": mid_b, "pin": "8317", "nonce": nonce_a})
    assert resp.status_code == 409
