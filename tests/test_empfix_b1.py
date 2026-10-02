"""Employee app audit, fix wave B1: staff identity, account and security.

C9   one person, one login: unlinks recorded and never restored by the
     roster switch; one active employee login per name (code and a partial
     unique index); a phone with a login here cannot claim again.
C10  an employee can delete the login they created (App Store 5.1.1(v)).
C11  paid-SMS and code-leak holes: +1 only, an hourly ceiling, the dev code
     only on purpose, throttled claimable list and counted bad join codes.
M4   PIN hardening: common-PIN denylist, attempts counted before the KDF,
     owner notices on locks, sprays and claims, server sign-out, and a
     Change PIN screen with its own counter.
H10  forgot PIN by text.
     Sessions: revoked per restaurant, the roster sync reports failures, the
     JSON-object guard, no pepper-less "upgrade", and the Me tab's fields.
"""
import sqlite3
import threading

import pytest
from flask import Flask

import auth
import client_api
import mobile_api
import models
from auth import (create_session, create_staff_session, create_user, get_join_code,
                  get_session_user, init_auth, set_membership_pin, upsert_membership)
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
    monkeypatch.setenv("STAFF_SIGNUP_DEV_CODE", "1")
    for var in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER",
                "STAFF_OTP_HOURLY_CAP", "STAFF_OTP_COUNTRY_CODES"):
        monkeypatch.delenv(var, raising=False)
    init_auth(db_path=db_path)


@pytest.fixture
def alerts(monkeypatch):
    """Every owner notice, captured instead of sent."""
    sent = []
    monkeypatch.setattr(auth, "alert_staff_admins",
                        lambda rid, kind, title, body, db_path=None: sent.append(
                            {"rid": rid, "kind": kind, "title": title, "body": body}) or "push")
    return sent


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


# ── helpers ────────────────────────────────────────────────────────────────

def _restaurant(db_path, name="Simple EJ's", owner="erik@x.test",
                people=(("Maria Lopez", "Server"), ("Mario Lopez", "Cook"), ("Dana K.", "Server"))):
    rid = create_restaurant(Restaurant(name=name, owner_email=owner), db_path=db_path)
    models.init_manual_team_members(db_path)
    for n, role in people:
        models.add_manual_team_member(rid, n, role, db_path=db_path)
    return rid


def _owner(db_path, rid, username="erik", role="client"):
    uid = create_user(rid, username, f"{username}@x.test", "owner-pw-123456", db_path=db_path)
    upsert_membership(uid, rid, role, db_path=db_path)
    return uid


def _staff(db_path, rid, username, name, pin="8317"):
    uid = create_user(rid, username, f"{username}@staff.invalid", "x" * 24, db_path=db_path, generated=True)
    m = upsert_membership(uid, rid, "employee", employee_name=name, db_path=db_path)
    if pin:
        set_membership_pin(m["id"], rid, pin, db_path=db_path)
    return uid, m["id"]


def _verified(client, phone="5550142233"):
    started = client.post("/staff/api/signup/start", json={"phone": phone, "optin": True}).get_json()
    assert started["ok"], started
    done = client.post("/staff/api/signup/verify", json={"phone": phone, "code": started["dev_code"]}).get_json()
    assert done["ok"], done
    return done["signup_token"]


def _signup(client, db_path, rid, phone="5550142233", name="Maria Lopez", pin="5063", device="dev-1"):
    token = _verified(client, phone)
    resp = client.post("/staff/api/signup/claim", json={
        "signup_token": token, "join_code": get_join_code(rid, db_path=db_path),
        "employee_name": name, "pin": pin, "device_id": device})
    # As the app: it holds the bearer token, not the browser cookie (which
    # staff_login_required would read first).
    client.delete_cookie("staff_session")
    return resp


def _membership(db_path, mid):
    conn = models.get_conn(db_path)
    try:
        return dict(conn.execute("SELECT * FROM memberships WHERE id=?", (mid,)).fetchone())
    finally:
        conn.close()


def _mid_for(db_path, rid, name):
    return [m for m in auth.get_memberships_for_restaurant(rid, role="employee", include_inactive=True,
                                                           db_path=db_path)
            if m["employee_name"] == name and m["is_active"]][0]["id"]


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


# ── C9: one person, one login ──────────────────────────────────────────────

def test_the_roster_switch_never_restores_a_login_the_owner_unlinked(client, db_path, alerts):
    """LG-05: impostor X claims Maria, the owner unlinks X, the real Maria
    claims; off the roster in October, back on in November — X used to come
    back with its old PIN."""
    import staff_settings
    rid = _restaurant(db_path)
    x = _signup(client, db_path, rid, phone="5550140001", pin="2963").get_json()
    x_mid = _mid_for(db_path, rid, "Maria Lopez")
    assert auth.unlink_claimed_membership(x_mid, rid, db_path=db_path)
    assert _membership(db_path, x_mid)["unlinked_at"]
    maria = _signup(client, db_path, rid, phone="5550140002", pin="5063").get_json()
    assert x["ok"] and maria["ok"]
    maria_mid = _mid_for(db_path, rid, "Maria Lopez")

    staff_settings.upsert(rid, "Maria Lopez", active=False, db_path=db_path)
    assert not _membership(db_path, maria_mid)["is_active"]
    staff_settings.upsert(rid, "Maria Lopez", active=True, db_path=db_path)

    assert _membership(db_path, maria_mid)["is_active"] == 1
    assert _membership(db_path, x_mid)["is_active"] == 0
    assert auth.verify_membership_pin(x_mid, rid, "2963", db_path=db_path)["ok"] is False


def test_an_unlinked_login_stays_off_even_when_it_is_the_only_one(client, db_path, alerts):
    """The name's only login is the one the owner unlinked: the roster
    switch must leave the name claimable, not hand it back to the impostor."""
    import staff_settings
    rid = _restaurant(db_path)
    _signup(client, db_path, rid, phone="5550140001", pin="2963")
    x_mid = _mid_for(db_path, rid, "Maria Lopez")
    auth.unlink_claimed_membership(x_mid, rid, db_path=db_path)
    staff_settings.upsert(rid, "Maria Lopez", active=False, db_path=db_path)
    staff_settings.upsert(rid, "Maria Lopez", active=True, db_path=db_path)
    assert _membership(db_path, x_mid)["is_active"] == 0
    assert "Maria Lopez" in [c["name"] for c in auth.claimable_names(rid, db_path=db_path)]


def test_an_ended_login_ends_the_persons_links_and_feed(db_path, monkeypatch):
    """B6's staff_insights.expire_links_for (the /s/ links and the calendar
    feed) runs wherever a login ends, when it exists."""
    import sys
    import types
    ended = []
    monkeypatch.setitem(sys.modules, "staff_insights", types.SimpleNamespace(
        expire_links_for=lambda rid, name, db_path=None: ended.append((rid, name))))
    rid = _restaurant(db_path)
    _u, mid = _staff(db_path, rid, "dana", "Dana K.")
    auth.set_membership_active(mid, rid, False, db_path=db_path)
    assert ended == [(rid, "Dana K.")]


def test_the_roster_switch_brings_back_one_login_for_a_name(db_path):
    """Two old inactive rows under one name (a re-hire's history): only the
    most recent comes back, and none while the name has an active login."""
    import staff_settings
    rid = _restaurant(db_path)
    _u1, old = _staff(db_path, rid, "sam1", "Sam Reed")
    auth.set_membership_active(old, rid, False, db_path=db_path)
    _u2, new = _staff(db_path, rid, "sam2", "Sam Reed")
    staff_settings.upsert(rid, "Sam Reed", active=False, db_path=db_path)
    staff_settings.upsert(rid, "Sam Reed", active=True, db_path=db_path)
    active = [m["id"] for m in auth.get_memberships_for_restaurant(rid, role="employee", db_path=db_path)
              if m["employee_name"] == "Sam Reed"]
    assert active == [new]


def test_a_rename_onto_another_active_login_is_refused(client, db_path):
    """SEC-05: two active logins under one name read and act on each other's
    requests. Refused in code (folded like name_key) and at the route."""
    rid = _restaurant(db_path)
    owner = _owner(db_path, rid)
    _staff(db_path, rid, "chris1", "Chris M.")
    _u, other = _staff(db_path, rid, "chris2", "Chris")
    with pytest.raises(auth.NameTakenError):
        auth.update_membership_details(other, rid, employee_name="  chris   m. ", db_path=db_path)
    client.set_cookie("session_token", create_session(owner, db_path=db_path))
    resp = client.post(f"/api/account/staff/{other}", json={"employee_name": "CHRIS M."})
    assert resp.status_code == 409 and resp.get_json()["name_taken"] is True
    assert _membership(db_path, other)["employee_name"] == "Chris"


def test_reactivating_onto_an_active_name_is_refused(client, db_path):
    rid = _restaurant(db_path)
    owner = _owner(db_path, rid)
    _u1, old = _staff(db_path, rid, "sam1", "Sam Reed")
    auth.set_membership_active(old, rid, False, db_path=db_path)
    _staff(db_path, rid, "sam2", "Sam Reed")
    with pytest.raises(auth.NameTakenError):
        auth.set_membership_active(old, rid, True, db_path=db_path)
    client.set_cookie("session_token", create_session(owner, db_path=db_path))
    assert client.post(f"/api/account/staff/{old}", json={"active": True}).status_code == 409


def test_the_database_itself_refuses_two_active_logins_under_one_name(db_path):
    """The backstop for the race the code check cannot see."""
    rid = _restaurant(db_path)
    _staff(db_path, rid, "ana1", "Ana M.", pin=None)
    uid = create_user(rid, "ana2", "ana2@staff.invalid", "x" * 24, db_path=db_path, generated=True)
    conn = models.get_conn(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO memberships (user_id, restaurant_id, role, employee_name) "
                         "VALUES (?,?,?,?)", (uid, rid, "employee", "ana m."))
        # A console login under the same name is a different thing, and allowed.
        conn.execute("INSERT INTO memberships (user_id, restaurant_id, role, employee_name) "
                     "VALUES (?,?,?,?)", (uid, rid, "manager", "Ana M."))
    finally:
        conn.close()


def test_a_phone_with_a_login_here_is_sent_to_forgot_pin_not_a_second_claim(client, db_path, alerts):
    """LG-14: Maria forgets her PIN, runs signup again and picks Mario — her
    login used to become Mario's, with his shifts and tasks."""
    rid = _restaurant(db_path)
    assert _signup(client, db_path, rid, name="Maria Lopez").status_code == 200
    again = _signup(client, db_path, rid, name="Mario Lopez", pin="2963")
    assert again.status_code == 409
    body = again.get_json()
    assert body["has_account"] is True and body["employee_name"] == "Maria Lopez"
    assert "Forgot PIN" in body["error"]
    names = [m["employee_name"] for m in auth.get_memberships_for_restaurant(rid, role="employee", db_path=db_path)]
    assert names == ["Maria Lopez"]


def test_two_claims_of_one_name_at_once_land_once(client, db_path):
    """LG-13: the claim is one write transaction that re-checks the name."""
    rid = _restaurant(db_path)
    tokens = [_verified(client, p) for p in ("5550140011", "5550140012")]
    barrier, results = threading.Barrier(2), []

    def claim(tok):
        barrier.wait()
        try:
            results.append(auth.claim_staff_name(tok, rid, "Dana K.", "5063", db_path=db_path))
        except auth.SignupError as e:
            results.append(e)

    threads = [threading.Thread(target=claim, args=(t,)) for t in tokens]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for r in results if isinstance(r, dict)) == 1
    assert sum(1 for r in results if isinstance(r, auth.SignupError)) == 1


# ── C10: delete my account ─────────────────────────────────────────────────

def _share(db_path, rid, name):
    hid = models.save_schedule_history(rid, "2026-09-12", "2026-09-12", 6, 40, 28,
                                       "date,employee\n2026-09-12," + name + "\n", [], db_path=db_path)
    return models.create_schedule_share(rid, hid, name, db_path=db_path)


def _share_live(db_path, token):
    conn = models.get_conn(db_path)
    try:
        # Only the link's hash is stored (B6, SEC-09).
        row = conn.execute("SELECT expires_at > datetime('now') AS live FROM schedule_shares WHERE token=?",
                           (models._share_hash(token),)).fetchone()
        return bool(row["live"])
    finally:
        conn.close()


def test_an_employee_can_delete_the_login_they_created(client, db_path, alerts):
    rid = _restaurant(db_path)
    token = _signup(client, db_path, rid).get_json()["token"]
    mid = _mid_for(db_path, rid, "Maria Lopez")
    link = _share(db_path, rid, "Maria Lopez")

    refused = client.post("/staff/api/account/delete", json={}, headers=_bearer(token))
    assert refused.status_code == 400
    assert get_session_user(token, db_path=db_path) is not None

    resp = client.post("/staff/api/account/delete", json={"confirm": True}, headers=_bearer(token))
    assert resp.status_code == 200 and resp.get_json()["deleted"] is True
    row = _membership(db_path, mid)
    assert row["is_active"] == 0 and row["deleted_at"] and row["pin_hash"] is None
    assert row["claimed_by_phone"] is None
    assert get_session_user(token, db_path=db_path) is None
    assert not _share_live(db_path, link)
    assert auth.get_user_by_phone("5550142233", db_path=db_path) is None    # identity closed
    assert [a["kind"] for a in alerts if a["kind"] != "staff_claim"] == ["staff_account_deleted"]
    activity = models.get_conn(db_path).execute(
        "SELECT event_data FROM activity_log WHERE restaurant_id=? AND event_type='staff_account_deleted'",
        (rid,)).fetchall()
    assert activity and "Maria Lopez" in activity[0]["event_data"]

    # The person may come back: the same phone signs up from scratch, and the
    # roster switch never restores the deleted row.
    assert _signup(client, db_path, rid).status_code == 200


def test_deleting_one_location_keeps_the_others(client, db_path, alerts):
    a = _restaurant(db_path, "Alpha")
    b = _restaurant(db_path, "Bravo", owner="b@x.test")
    tok_a = _signup(client, db_path, a).get_json()["token"]
    _signup(client, db_path, b, pin="2963", device="dev-2")
    assert client.post("/staff/api/account/delete", json={"confirm": True},
                       headers=_bearer(tok_a)).status_code == 200
    user = auth.get_user_by_phone("5550142233", db_path=db_path)
    assert user and [m["restaurant_id"] for m in auth.get_memberships_for_user(user["id"], db_path=db_path)] == [b]


def test_an_account_deactivation_expires_schedule_links(client, db_path):
    """LG-18(b) / SEC-09: Account → Staff deactivate used to leave the links."""
    rid = _restaurant(db_path)
    _u, mid = _staff(db_path, rid, "dana", "Dana K.")
    link = _share(db_path, rid, "Dana K.")
    assert auth.set_membership_active(mid, rid, False, db_path=db_path)
    assert not _share_live(db_path, link)


# ── C11: paid SMS and the code leak ────────────────────────────────────────

def test_signup_texts_us_numbers_only(client, db_path):
    resp = client.post("/staff/api/signup/start", json={"phone": "+447700900123", "optin": True})
    assert resp.status_code == 400 and "US" in resp.get_json()["error"]
    assert client.post("/staff/api/signup/start",
                       json={"phone": "+15550142233", "optin": True}).status_code == 200


def test_the_country_list_is_configurable(db_path, monkeypatch):
    monkeypatch.setenv("STAFF_OTP_COUNTRY_CODES", "1,44")
    assert auth.otp_country_allowed("+447700900123")
    assert not auth.otp_country_allowed("+8801711000000")


def test_verification_texts_stop_at_the_hourly_ceiling(client, db_path, monkeypatch):
    monkeypatch.setenv("STAFF_OTP_HOURLY_CAP", "2")
    sent = []
    import notify
    monkeypatch.setattr(notify, "send_sms", lambda *a, **k: sent.append(a[0]) or True)
    for p in ("5550140021", "5550140022"):
        assert client.post("/staff/api/signup/start", json={"phone": p, "optin": True}).status_code == 200
    third = client.post("/staff/api/signup/start", json={"phone": "5550140023", "optin": True})
    assert third.status_code == 400 and "can't send codes" in third.get_json()["error"]
    assert len(sent) == 2


def test_the_dev_code_needs_the_explicit_flag(client, db_path, monkeypatch):
    monkeypatch.delenv("STAFF_SIGNUP_DEV_CODE")
    for var in ("RAILWAY_ENVIRONMENT", "RAILWAY_PROJECT_ID", "CAVNAR_FORCE_SECURE_COOKIES"):
        monkeypatch.delenv(var, raising=False)
    body = client.post("/staff/api/signup/start", json={"phone": "5550142233", "optin": True}).get_json()
    assert body["ok"] and "dev_code" not in body


def test_no_log_line_carries_the_whole_number(client, db_path, monkeypatch, capsys):
    """SEC-14: the dev line and an SMS failure print the last four only."""
    import notify

    def boom(*a, **k):
        raise RuntimeError("twilio down")

    monkeypatch.setattr(notify, "send_sms", boom)
    client.post("/staff/api/signup/start", json={"phone": "5550142233", "optin": True})
    out = capsys.readouterr().out
    assert "…2233" in out and "5550142233" not in out and "+15550142233" not in out


def test_the_claimable_list_is_throttled_and_takes_the_token_in_a_header(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    token = _verified(client)
    code = get_join_code(rid, db_path=db_path)
    by_header = client.get(f"/staff/api/signup/claimable/{code}", headers={"X-Signup-Token": token})
    assert by_header.status_code == 200 and by_header.get_json()["names"]
    seen = []
    real = auth.record_portal_attempt
    monkeypatch.setattr(auth, "record_portal_attempt", lambda ip, **k: seen.append(ip) or real(ip, **k))
    import staff_routes
    monkeypatch.setattr(staff_routes, "record_portal_attempt", auth.record_portal_attempt)
    client.get(f"/staff/api/signup/claimable/ZZZZZZ", headers={"X-Signup-Token": token})
    assert seen, "a join-code guess on the claimable list was not counted"
    monkeypatch.setattr(staff_routes, "portal_attempts_exceeded", lambda ip: True)
    assert client.get(f"/staff/api/signup/claimable/{code}",
                      headers={"X-Signup-Token": token}).status_code == 429


def test_a_bad_join_code_on_pin_sign_in_is_counted(client, db_path, monkeypatch):
    """LG-17: an invalid code answered 404 against a valid one's 400 and cost
    nothing from the failure budget."""
    import staff_routes
    seen = []
    monkeypatch.setattr(staff_routes, "record_portal_attempt", lambda ip: seen.append(ip) or None)
    resp = client.post("/staff/r/AAAAAA/login", json={})
    assert resp.status_code == 404
    assert seen == ["127.0.0.1"]


# ── M4: PIN hardening ──────────────────────────────────────────────────────

@pytest.mark.parametrize("pin", ["2580", "1212", "6969", "1004", "0852", "1122", "1987", "2024", "5683"])
def test_the_most_common_pins_and_years_are_refused(pin):
    with pytest.raises(auth.PinError):
        auth.validate_pin(pin)


def test_parallel_guesses_cannot_outrun_the_lockout(db_path, monkeypatch):
    """LG-24: eight guesses at once used to all pass the "not locked" check
    before any failure was recorded. Now each is counted before its KDF, so
    at most PIN_MAX_ATTEMPTS ever reach the hash."""
    import time
    rid = _restaurant(db_path)
    _u, mid = _staff(db_path, rid, "dana", "Dana K.")
    real, checked = auth.check_password_hash, []

    def slow(h, p):
        checked.append(1)
        time.sleep(0.15)
        return real(h, p)

    monkeypatch.setattr(auth, "check_password_hash", slow)
    barrier = threading.Barrier(8)

    def guess():
        barrier.wait()
        auth.verify_membership_pin(mid, rid, "0000", db_path=db_path)

    threads = [threading.Thread(target=guess) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(checked) <= auth.PIN_MAX_ATTEMPTS, len(checked)
    assert auth.pin_lockout_state(mid, db_path=db_path)["locked"] is True


def test_a_lock_reaches_the_owner(db_path, alerts):
    rid = _restaurant(db_path)
    _u, mid = _staff(db_path, rid, "dana", "Dana K.")
    for _ in range(auth.PIN_MAX_ATTEMPTS):
        auth.verify_membership_pin(mid, rid, "0000", ip_address="203.0.113.5", db_path=db_path)
    locks = [a for a in alerts if a["kind"] == "pin_locked"]
    assert len(locks) == 1 and "Dana K." in locks[0]["body"]
    # Further misses while locked do not alert again.
    auth.verify_membership_pin(mid, rid, "0000", ip_address="203.0.113.5", db_path=db_path)
    assert len([a for a in alerts if a["kind"] == "pin_locked"]) == 1


def test_a_spray_across_the_roster_reaches_the_owner_once(db_path, alerts):
    rid = _restaurant(db_path)
    mids = [_staff(db_path, rid, f"e{i}", f"Emp {i}")[1] for i in range(4)]
    for i in range(auth.PIN_SPRAY_FAILURES + 4):
        auth.verify_membership_pin(mids[i % 4], rid, "9031", ip_address="198.51.100.9", db_path=db_path)
    sprays = [a for a in alerts if a["kind"] == "pin_spray"]
    assert len(sprays) == 1 and "rotate" in sprays[0]["body"]


def test_owner_notices_go_to_owners_and_managers_by_name(db_path, monkeypatch):
    """The audience is explicit user ids — never an employee's phone at the
    same restaurant — and the owner is emailed when none has the app."""
    import emails
    import push
    import scheduler
    rid = _restaurant(db_path)
    owner = _owner(db_path, rid)
    mgr = _owner(db_path, rid, "mgr", role="manager")
    emp, _mid = _staff(db_path, rid, "dana", "Dana K.")
    pushed, mailed = [], []
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: True)
    monkeypatch.setattr(push, "fire_push", lambda rid, t, title, body, **k: pushed.append(k["user_ids"]) or 1)
    assert auth.alert_staff_admins(rid, "x", "T", "B", db_path=db_path) == "push"
    assert sorted(pushed[0]) == sorted([owner, mgr]) and emp not in pushed[0]

    monkeypatch.setattr(push, "fire_push", lambda *a, **k: 0)
    monkeypatch.setattr(emails, "deliver", lambda **k: mailed.append(k) or emails.SendResult(True))
    assert auth.alert_staff_admins(rid, "x", "Staff PIN locked", "B", db_path=db_path) == "email"
    assert mailed[0]["payload"]["to"] == ["erik@x.test"]


def test_owner_notices_are_not_sent_off_railway(db_path, monkeypatch):
    import push
    import scheduler
    rid = _restaurant(db_path)
    _owner(db_path, rid)
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: False)
    monkeypatch.setattr(push, "fire_push", lambda *a, **k: pytest.fail("pushed from a local backend"))
    assert auth.alert_staff_admins(rid, "x", "T", "B", db_path=db_path) == "skipped"


def test_every_claim_reaches_the_owner_with_the_last_four(client, db_path, alerts):
    rid = _restaurant(db_path)
    _signup(client, db_path, rid)
    claim = [a for a in alerts if a["kind"] == "staff_claim"]
    assert len(claim) == 1
    assert "Maria Lopez" in claim[0]["body"] and "2233" in claim[0]["body"]
    assert "555014" not in claim[0]["body"]


def test_change_pin_has_its_own_counter_and_says_when_it_locks(client, db_path, alerts):
    """SEC-16 / LG-26: wrong "current PIN"s used to lock the session owner
    out of sign-in, and a real lock read "didn't match"."""
    rid = _restaurant(db_path)
    uid, mid = _staff(db_path, rid, "dana", "Dana K.")
    token = create_staff_session(uid, rid, db_path=db_path)
    for _ in range(auth.CHANGE_PIN_MAX_ATTEMPTS - 1):
        r = client.post("/staff/api/pin", json={"current_pin": "0000", "new_pin": "4827"}, headers=_bearer(token))
        assert r.status_code == 401 and r.get_json()["locked"] is False
    last = client.post("/staff/api/pin", json={"current_pin": "0000", "new_pin": "4827"}, headers=_bearer(token))
    assert last.status_code == 401
    assert last.get_json()["locked"] is True and last.get_json()["signed_out"] is True
    assert get_session_user(token, db_path=db_path) is None
    # Sign-in is untouched: the right PIN still works on the PIN pad.
    assert auth.pin_lockout_state(mid, db_path=db_path)["failed_count"] == 0
    assert auth.verify_membership_pin(mid, rid, "8317", db_path=db_path)["ok"] is True
    assert not alerts


def test_sign_out_ends_the_bearer_session_on_the_server(client, db_path):
    """LG-27 / WF-31: the app's sign-out left the token valid for 14 hours."""
    rid = _restaurant(db_path)
    uid, _mid = _staff(db_path, rid, "dana", "Dana K.")
    token = create_staff_session(uid, rid, db_path=db_path)
    resp = client.post("/staff/api/logout", headers=_bearer(token))
    assert resp.status_code == 200 and resp.get_json()["signed_out"] is True
    assert get_session_user(token, db_path=db_path) is None
    assert client.get("/staff/api/me", headers=_bearer(token)).status_code == 401
    assert client.post("/staff/api/logout").get_json()["ok"] is True       # nothing to end is fine


# ── H10: forgot PIN ────────────────────────────────────────────────────────

def test_forgot_pin_by_text_sets_a_new_pin_and_signs_in(client, db_path, alerts):
    rid = _restaurant(db_path)
    old_token = _signup(client, db_path, rid).get_json()["token"]
    mid = _mid_for(db_path, rid, "Maria Lopez")
    for _ in range(auth.PIN_MAX_ATTEMPTS):
        auth.verify_membership_pin(mid, rid, "0000", db_path=db_path)
    assert auth.pin_lockout_state(mid, db_path=db_path)["locked"]

    code = get_join_code(rid, db_path=db_path)
    start = client.post("/staff/api/pin/forgot/start", json={"join_code": code, "phone": "(555) 014-2233"}).get_json()
    assert start["ok"] and start["dev_code"]
    found = client.post("/staff/api/pin/forgot/verify",
                        json={"phone": "5550142233", "code": start["dev_code"]}).get_json()
    assert found["ok"] and found["employee_name"] == "Maria Lopez" and found["restaurant"] == "Simple EJ's"
    weak = client.post("/staff/api/pin/forgot/set", json={"reset_token": found["reset_token"], "pin": "2580"})
    assert weak.status_code == 400
    done = client.post("/staff/api/pin/forgot/set",
                       json={"reset_token": found["reset_token"], "pin": "4827", "device_id": "dev-2"})
    assert done.status_code == 200, done.get_json()
    body = done.get_json()
    assert body["signed_in"] and body["token"] and body["membership_id"] == mid
    assert get_session_user(old_token, db_path=db_path) is None           # other sessions ended
    assert get_session_user(body["token"], db_path=db_path) is not None
    assert auth.pin_lockout_state(mid, db_path=db_path)["locked"] is False
    assert auth.verify_membership_pin(mid, rid, "4827", db_path=db_path)["ok"] is True
    reused = client.post("/staff/api/pin/forgot/set", json={"reset_token": found["reset_token"], "pin": "4821"})
    assert reused.status_code == 400 and reused.get_json()["reset_expired"] is True


def test_forgot_pin_answers_the_same_for_a_number_with_no_login(client, db_path, monkeypatch):
    import notify
    texts = []
    monkeypatch.setattr(notify, "send_sms", lambda *a, **k: texts.append(a[0]) or True)
    rid = _restaurant(db_path)
    code = get_join_code(rid, db_path=db_path)
    nobody = client.post("/staff/api/pin/forgot/start", json={"join_code": code, "phone": "5550149999"})
    assert nobody.status_code == 200
    assert nobody.get_json()["message"] == "If that number is on file here, we've texted it a code."
    assert "dev_code" not in nobody.get_json() and texts == []
    wrong = client.post("/staff/api/pin/forgot/verify", json={"phone": "5550149999", "code": "123456"})
    assert wrong.get_json()["error"] == auth.PIN_RESET_CODE_MISMATCH


def test_forgot_pin_reaches_an_owner_created_login_by_its_roster_phone(client, db_path):
    """Owner-created logins have no verified phone; the number the owner
    typed for that name on the roster is the phone on file."""
    rid = _restaurant(db_path)
    _u, mid = _staff(db_path, rid, "dana", "Dana K.")
    models.set_staff_contact(rid, "Dana K.", None, "(555) 014-7777", db_path=db_path)
    code = get_join_code(rid, db_path=db_path)
    start = client.post("/staff/api/pin/forgot/start", json={"join_code": code, "phone": "5550147777"}).get_json()
    found = client.post("/staff/api/pin/forgot/verify",
                        json={"phone": "5550147777", "code": start["dev_code"]}).get_json()
    assert found["employee_name"] == "Dana K."


# ── Sessions ───────────────────────────────────────────────────────────────

def test_a_pin_reset_at_one_location_keeps_the_shift_at_the_other(db_path):
    """SEC-12 / LG-28."""
    a = _restaurant(db_path, "Alpha")
    b = _restaurant(db_path, "Bravo", owner="b@x.test")
    uid, mid_a = _staff(db_path, a, "dana", "Dana K.")
    mid_b = upsert_membership(uid, b, "employee", employee_name="Dana K.", db_path=db_path)["id"]
    set_membership_pin(mid_b, b, "4827", db_path=db_path)
    at_a = create_staff_session(uid, a, db_path=db_path)
    at_b = create_staff_session(uid, b, db_path=db_path)
    set_membership_pin(mid_b, b, "4092", db_path=db_path)
    assert get_session_user(at_b, db_path=db_path) is None
    assert get_session_user(at_a, db_path=db_path) is not None


def test_a_roster_sync_failure_is_reported(db_path, monkeypatch):
    """SEC-15: it was only printed."""
    import ops
    import staff_settings
    rid = _restaurant(db_path)
    _staff(db_path, rid, "dana", "Dana K.")
    captured = []
    monkeypatch.setattr(ops, "capture", lambda exc, **k: captured.append((exc, k)))

    def boom(*a, **k):
        raise RuntimeError("revoke failed")

    monkeypatch.setattr(auth, "set_membership_active", boom)
    staff_settings.upsert(rid, "Dana K.", active=False, db_path=db_path)
    assert captured and captured[0][1]["job"] == "staff_portal_access_sync"


def test_a_non_object_body_is_a_400_not_a_500(client, db_path):
    """SEC-11."""
    for path in ("/staff/api/signup/start", "/staff/api/signup/claim", "/staff/api/pin/forgot/start"):
        assert client.post(path, json=[1]).status_code == 400, path


def test_no_pepper_means_no_upgrade_and_no_half_claim(client, db_path, monkeypatch):
    """SEC-10: with the pepper unset, a v0 sign-in was re-written as pepv1
    of "::pin" — reported protected, and dead the day the pepper was set; and
    a claim 500'd after the membership was written."""
    from werkzeug.security import generate_password_hash
    rid = _restaurant(db_path)
    _u, mid = _staff(db_path, rid, "dana", "Dana K.", pin=None)
    v0 = generate_password_hash("::4827")
    conn = models.get_conn(db_path)
    conn.execute("UPDATE memberships SET pin_hash=? WHERE id=?", (v0, mid))
    conn.commit()
    conn.close()
    monkeypatch.setenv("CAVNAR_PIN_PEPPER", "")
    assert auth.verify_membership_pin(mid, rid, "4827", db_path=db_path)["ok"] is True
    assert _membership(db_path, mid)["pin_hash"] == v0

    token = _verified(client)
    resp = client.post("/staff/api/signup/claim", json={
        "signup_token": token, "join_code": get_join_code(rid, db_path=db_path),
        "employee_name": "Maria Lopez", "pin": "5063"})
    assert resp.status_code == 400
    assert "Maria Lopez" in [c["name"] for c in auth.claimable_names(rid, db_path=db_path)]


# ── The Me tab ─────────────────────────────────────────────────────────────

def test_me_carries_phone_email_channels_and_locations(client, db_path, alerts):
    a = _restaurant(db_path, "Alpha")
    b = _restaurant(db_path, "Bravo", owner="b@x.test")
    token = _signup(client, db_path, a).get_json()["token"]
    _signup(client, db_path, b, pin="2963", device="dev-2")
    me = client.get("/staff/api/me", headers=_bearer(token)).get_json()["employee"]
    assert me["phone_masked"].endswith("2233") and "555" not in me["phone_masked"]
    assert me["email"] == "" and me["notifications"] == {"push": False, "texts": False,
                                                         "texts_consent": False, "email": False}
    locs = {l["restaurant"]: l for l in me["locations"]}
    assert set(locs) == {"Alpha", "Bravo"}
    assert locs["Alpha"]["current"] is True and locs["Bravo"]["current"] is False
    assert locs["Bravo"]["join_code"] == get_join_code(b, db_path=db_path)

    assert client.post("/staff/api/me/email", json={"email": "not-an-email"},
                       headers=_bearer(token)).status_code == 400
    assert client.post("/staff/api/me/email", json={"email": "maria@example.com"},
                       headers=_bearer(token)).get_json()["email"] == "maria@example.com"
    me = client.get("/staff/api/me", headers=_bearer(token)).get_json()["employee"]
    assert me["email"] == "maria@example.com" and me["notifications"]["email"] is True
    client.post("/staff/api/me/email", json={"email": ""}, headers=_bearer(token))
    assert client.get("/staff/api/me", headers=_bearer(token)).get_json()["employee"]["email"] == ""


def test_switch_opens_the_other_locations_pin_pad_never_a_session(client, db_path, alerts):
    a = _restaurant(db_path, "Alpha")
    b = _restaurant(db_path, "Bravo", owner="b@x.test")
    other = _restaurant(db_path, "Charlie", owner="c@x.test")
    token = _signup(client, db_path, a).get_json()["token"]
    _signup(client, db_path, b, pin="2963", device="dev-2")
    resp = client.post("/staff/api/switch", json={"restaurant_id": b}, headers=_bearer(token)).get_json()
    assert resp["requires_pin"] is True and resp["login_nonce"] and "token" not in resp
    login = client.post(f"/staff/r/{resp['portal_token']}/login", json={
        "membership_id": resp["membership_id"], "pin": "2963", "nonce": resp["login_nonce"], "device_id": "d"})
    assert login.status_code == 200 and login.get_json()["token"]
    assert client.post("/staff/api/switch", json={"restaurant_id": other},
                       headers=_bearer(token)).status_code == 404


def test_ending_staff_sessions_unregisters_the_phones_pushes(client, db_path, monkeypatch):
    """B2's push.unregister_staff_devices runs wherever staff sessions end —
    sign-out with that phone's token, and the one revoke every PIN set,
    deactivation, unlink and deletion goes through."""
    import push
    calls = []
    monkeypatch.setattr(push, "unregister_staff_devices",
                        lambda uid, restaurant_id=None, apns_token=None: calls.append((uid, restaurant_id, apns_token)),
                        raising=False)
    rid = _restaurant(db_path)
    uid, mid = _staff(db_path, rid, "dana", "Dana K.")
    calls.clear()
    token = create_staff_session(uid, rid, db_path=db_path)
    client.post("/staff/api/logout", json={"apns_token": "abc"}, headers=_bearer(token))
    assert calls == [(uid, rid, "abc")]
    auth.set_membership_active(mid, rid, False, db_path=db_path)
    assert calls[-1] == (uid, rid, None)
