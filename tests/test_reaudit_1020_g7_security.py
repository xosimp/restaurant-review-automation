"""Blind re-audit 10/20/26, R7 (multi-tenant security) — the fix round's tests.

Each section names the finding it holds (/Users/simp/scale-reaudit-1020/R7_security.md).
"""
import os

import pytest
from flask import Flask

import auth
import auth_routes
import client_api
import mobile_api
import models
import security
from auth import create_user, init_auth, upsert_membership
from models import Restaurant, create_restaurant, update_restaurant

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = os.path.join(ROOT, "templates")


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    import admin_routes
    import webhook_routes
    for mod in (models, auth, auth_routes, mobile_api, client_api, admin_routes, webhook_routes):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    init_auth(db_path=db_path)
    models.init_two_fa_backup_codes(db_path=db_path)
    auth_routes._login_attempts.clear()
    yield
    auth_routes._login_attempts.clear()


@pytest.fixture
def sent(monkeypatch):
    import emails
    import notify
    box = {"email": [], "sms": [], "notice": []}
    monkeypatch.setattr(emails, "send_2fa_code",
                        lambda to, name, code, owner=None, **k: box["email"].append((to, code)) or True)
    monkeypatch.setattr(notify, "send_2fa_sms", lambda to, name, code: box["sms"].append((to, code)) or True)
    monkeypatch.setattr(emails, "send_security_notice_email",
                        lambda to, name, kind, detail=None, **k: box["notice"].append((to, kind, detail)) or True)
    return box


@pytest.fixture
def client():
    app = Flask(__name__, template_folder=TEMPLATES)
    app.secret_key = "test-secret"
    for bp in (auth_routes.auth_bp, mobile_api.mobile_bp, client_api.client_bp):
        app.register_blueprint(bp)
    return app.test_client()


def _setup(db_path, two_fa=True):
    rid = create_restaurant(Restaurant(name="R", owner_email="owner@x.test"), db_path=db_path)
    owner = create_user(rid, "owner", "owner@x.test", "ownerpass1", db_path=db_path)
    upsert_membership(owner, rid, "client", db_path=db_path)
    if two_fa:
        update_restaurant(rid, {"two_fa_enabled": 1}, db_path=db_path)
    return rid, owner


def _phone_login(client, username="owner", password="ownerpass1"):
    return client.post("/mobile/api/login", json={"username": username, "password": password}).get_json()


# ── R7-01: a challenge dies after N wrong codes; the login locks ────────────

def test_a_challenge_dies_on_its_fifth_wrong_code(db_path):
    rid, owner = _setup(db_path)
    pending, code = auth.issue_two_fa_challenge(rid, owner, db_path=db_path)
    wrong = "100000" if code != "100000" else "100001"
    for _ in range(auth.TWO_FA_CHALLENGE_MAX_WRONG - 1):
        assert auth.check_two_fa_code(rid, owner, wrong, pending=pending, consume=False, db_path=db_path) == "wrong"
    assert auth.two_fa_challenge_exists(rid, owner, pending, db_path=db_path)
    assert auth.check_two_fa_code(rid, owner, wrong, pending=pending, consume=False, db_path=db_path) == "wrong"
    # Gone: the right code no longer passes; the sign-in starts again.
    assert not auth.two_fa_challenge_exists(rid, owner, pending, db_path=db_path)
    assert auth.check_two_fa_code(rid, owner, code, pending=pending, db_path=db_path) == "missing"


def test_a_resend_keeps_the_wrong_count(db_path):
    rid, owner = _setup(db_path)
    pending, code = auth.issue_two_fa_challenge(rid, owner, db_path=db_path)
    for _ in range(auth.TWO_FA_CHALLENGE_MAX_WRONG - 1):
        auth.check_two_fa_code(rid, owner, "000000", pending=pending, consume=False, db_path=db_path)
    new_code = auth.reissue_two_fa_code(rid, owner, pending, db_path=db_path)
    assert new_code
    bad = "000000" if new_code != "000000" else "000001"
    assert auth.check_two_fa_code(rid, owner, bad, pending=pending, consume=False, db_path=db_path) == "wrong"
    assert not auth.two_fa_challenge_exists(rid, owner, pending, db_path=db_path)


def test_the_login_locks_after_a_days_wrong_codes_from_any_address(db_path, client, sent):
    rid, owner = _setup(db_path)
    for i in range(security.SECOND_FACTOR_DAY_MAX):
        r = _phone_login(client)
        auth_routes._login_attempts.clear()
        security.clear_login_failures(ip="2fa:127.0.0.1", db_path=db_path)   # a fresh address each time
        v = client.post("/mobile/api/verify-2fa", json={"pending_token": r["pending_token"], "code": "000000"})
        assert v.status_code == 401, i
    # Locked: even the right code is refused, from a clean address.
    security.clear_login_failures(ip="2fa:127.0.0.1", db_path=db_path)
    r = _phone_login(client)
    right = sent["email"][-1][1]
    v = client.post("/mobile/api/verify-2fa", json={"pending_token": r["pending_token"], "code": right})
    assert v.status_code == 429 and v.get_json()["second_factor_locked"] is True
    # Told once, at the lock.
    assert [k for _to, k, _d in sent["notice"]] == ["two_factor_locked"]
    ev = [e for e in models.get_conn(db_path).execute(
        "SELECT event_type FROM activity_log WHERE restaurant_id=?", (rid,)).fetchall()]
    assert any(e["event_type"] == "two_factor_locked" for e in ev)
    # The console's Clear lockout (and the break-glass unlock) lifts it.
    assert security.lockout_state("owner", db_path=db_path)["second_factor_seconds_left"] > 0
    assert any(s["username"] == "owner" for s in security.active_lockouts(db_path=db_path))
    security.clear_account_lock("owner", db_path=db_path)
    assert auth.second_factor_lock_left(owner, db_path=db_path) == 0


def test_the_web_second_step_honours_the_login_lock(db_path, client, sent):
    rid, owner = _setup(db_path)
    for _ in range(security.SECOND_FACTOR_DAY_MAX):
        security.record_second_factor_failure(owner, "1.2.3.4", db_path=db_path)
    client.get("/login")
    csrf = client.get_cookie("csrf_token").value
    html = client.post("/login", data={"username": "owner", "password": "ownerpass1",
                                       "csrf_token": csrf}).get_data(as_text=True)
    marker = 'name="pending_token" value="'
    start = html.index(marker) + len(marker)
    pending = html[start:html.index('"', start)]
    csrf = client.get_cookie("csrf_token").value
    resp = client.post("/verify-2fa", data={"pending_token": pending, "code": sent["email"][-1][1],
                                            "next_url": "/", "csrf_token": csrf})
    assert resp.status_code == 429 and "Too many wrong codes" in resp.get_data(as_text=True)
    assert not client.get_cookie("session_token")


# ── R7-02: a share of the request threads, not only of the starts ─────────

def test_one_login_never_holds_every_thread(monkeypatch):
    import request_limits as rl
    monkeypatch.setenv("REQUEST_LIMITS", "1")
    me = {"id": 7, "restaurant_id": 1, "role": "client"}
    slots = []
    for _ in range(3):                                    # alone: CAPACITY-1 at once
        wait, slot = rl.inflight_enter(me, "/api/monthly-review")
        assert wait == 0 and slot
        slots.append(slot)
    wait, slot = rl.inflight_enter(me, "/api/monthly-review")
    assert wait > 0 and slot is None                       # the last thread stays free
    other = {"id": 8, "restaurant_id": 2, "role": "client"}
    wait, oslot = rl.inflight_enter(other, "/api/home/brief")
    assert wait == 0                                        # someone else gets it
    for s in slots:
        rl.inflight_leave(s)
    rl.inflight_leave(oslot)
    assert rl._inflight == {}


def test_under_contention_a_login_is_held_to_its_share(monkeypatch):
    import request_limits as rl
    monkeypatch.setenv("REQUEST_LIMITS", "1")
    a = {"id": 1, "restaurant_id": 10}
    b = {"id": 2, "restaurant_id": 20}
    _, s1 = rl.inflight_enter(b, "/api/x")
    _, s2 = rl.inflight_enter(a, "/api/x")
    _, s3 = rl.inflight_enter(a, "/api/x")
    wait, _ = rl.inflight_enter(a, "/api/x")              # 3 busy of 4: a holds its 2
    assert wait > 0
    for s in (s1, s2, s3):
        rl.inflight_leave(s)


def test_heavy_routes_and_restaurants_are_capped_in_flight(monkeypatch):
    import request_limits as rl
    monkeypatch.setenv("REQUEST_LIMITS", "1")
    me = {"id": 3, "restaurant_id": 30}
    held = [rl.inflight_enter(me, "/api/morning-brief")[1] for _ in range(rl.heavy_inflight())]
    assert all(held)
    assert rl.inflight_enter(me, "/api/morning-brief")[0] > 0
    for s in held:
        rl.inflight_leave(s)
    mates = [{"id": 100 + i, "restaurant_id": 40} for i in range(rl.restaurant_inflight() + 1)]
    taken = [rl.inflight_enter(m, "/api/x")[1] for m in mates[:-1]]
    assert rl.inflight_enter(mates[-1], "/api/x")[0] > 0
    for s in taken:
        rl.inflight_leave(s)


def test_a_route_measured_slow_counts_as_heavy(monkeypatch):
    import request_limits as rl
    monkeypatch.setattr(rl, "measured_heavy_routes", lambda now=None: frozenset({"/api/monthly-review"}))
    assert rl.is_heavy("/api/monthly-review", "/api/monthly-review")
    assert not rl.is_heavy("/api/x", "/api/x")


def test_the_decorators_give_the_slot_back_and_staff_sessions_are_counted(db_path, client, monkeypatch):
    import inspect
    import request_limits as rl
    src = inspect.getsource(auth.staff_login_required)
    assert "session_refusal" in src and "run_counted" in src
    assert inspect.getsource(auth.login_required).count("_rl.run_counted(user") == 2
    assert inspect.getsource(auth.mobile_login_required).count("_rl.run_counted(user") == 2
    monkeypatch.setenv("REQUEST_LIMITS", "1")
    _setup(db_path, two_fa=False)
    tok = _phone_login(client)["token"]
    for _ in range(5):
        assert client.get("/mobile/api/me", headers={"Authorization": f"Bearer {tok}"}).status_code == 200
    assert rl._inflight == {}


def test_both_clients_retry_an_in_flight_refusal():
    js = open(os.path.join(TEMPLATES, "_csrf_fetch.html"), encoding="utf-8").read()
    assert "in_flight" in js and "_sendCounted" in js
    swift = open(os.path.join(ROOT, "ios/CavnarAI/CavnarAI/Core/APIClient.swift"), encoding="utf-8").read()
    assert "isInFlightRefusal" in swift


# ── R7-03: step-up before an account holder's security changes ─────────────

def _bearer(tok):
    return {"Authorization": f"Bearer {tok}"}


def _stale(db_path, tok):
    """A session signed in an hour ago whose password was never typed again."""
    conn = models.get_conn(db_path)
    conn.execute("UPDATE sessions SET created_at=datetime('now', '-1 hour'), reauth_at=NULL WHERE token=?",
                 (auth.hash_session_token(tok),))
    conn.commit()
    conn.close()


def test_turning_2fa_off_asks_for_the_password_and_tells_the_owners(db_path, client, sent):
    rid, owner = _setup(db_path, two_fa=False)
    tok = auth.create_session(owner, device_type="ios", db_path=db_path)
    _stale(db_path, tok)
    r = client.post("/mobile/api/account/2fa/disable", headers=_bearer(tok))
    body = r.get_json()
    assert r.status_code == 403 and body["reauth_required"] is True
    assert body["reauth_url"] == "/mobile/api/account/reauth"
    assert client.post("/mobile/api/account/reauth", headers=_bearer(tok),
                       json={"password": "nope-nope"}).status_code == 403
    ok = client.post("/mobile/api/account/reauth", headers=_bearer(tok), json={"password": "ownerpass1"})
    assert ok.status_code == 200 and ok.get_json()["valid_minutes"] == auth.RECENT_AUTH_MINUTES
    assert client.post("/mobile/api/account/2fa/disable", headers=_bearer(tok)).get_json()["ok"] is True
    assert ("owner@x.test", "two_fa_disabled", "owner") in sent["notice"]


def test_backup_codes_and_revoke_others_need_the_step_up_too(db_path, client, sent):
    rid, owner = _setup(db_path)
    tok = auth.create_session(owner, device_type="ios", db_path=db_path)
    _stale(db_path, tok)
    for path in ("/mobile/api/account/2fa/backup-codes", "/mobile/api/sessions/revoke-others",
                 "/mobile/api/account/request-deletion"):
        r = client.post(path, headers=_bearer(tok))
        assert r.status_code == 403 and r.get_json()["reauth_required"] is True, path


def test_five_wrong_step_up_passwords_end_the_session(db_path, client):
    rid, owner = _setup(db_path, two_fa=False)
    tok = auth.create_session(owner, device_type="ios", db_path=db_path)
    for i in range(auth.ACCOUNT_REAUTH_MAX_MISSES - 1):
        assert client.post("/mobile/api/account/reauth", headers=_bearer(tok),
                           json={"password": f"wrong-{i}"}).status_code == 403
    last = client.post("/mobile/api/account/reauth", headers=_bearer(tok), json={"password": "wrong-x"})
    assert last.status_code == 401 and last.get_json()["session_expired"] is True
    assert auth.get_session_user(tok, db_path=db_path) is None


def test_a_view_as_never_changes_sign_in_security(db_path, client):
    rid, owner = _setup(db_path, two_fa=False)
    hq = create_restaurant(Restaurant(name="HQ", owner_email="w@x.test"), db_path=db_path)
    admin = create_user(hq, "will", "w@x.test", "Admin-pass-2026", is_admin=True, db_path=db_path)
    view = auth.create_view_as_session(owner, {"id": admin}, read_only=False, db_path=db_path)
    for path in ("/mobile/api/account/2fa/backup-codes", "/mobile/api/account/reauth"):
        r = client.post(path, headers=_bearer(view), json={"password": "ownerpass1"})
        assert r.status_code == 403 and r.get_json().get("view_as") is True, path


def test_inviting_a_co_owner_asks_for_the_password_a_teammate_does_not(db_path, client, sent, monkeypatch):
    import emails
    monkeypatch.setattr(emails, "send_team_invite_email", lambda *a, **k: True)
    rid, owner = _setup(db_path, two_fa=False)
    tok = auth.create_session(owner, device_type="ios", db_path=db_path)
    _stale(db_path, tok)
    r = client.post("/mobile/api/account/team/invite", headers=_bearer(tok),
                    json={"name": "Mate", "email": "mate@x.test", "role": "member"})
    assert r.get_json()["ok"] is True
    r = client.post("/mobile/api/account/team/invite", headers=_bearer(tok),
                    json={"name": "Eve", "email": "eve@x.test", "role": "client"})
    assert r.status_code == 403 and r.get_json()["reauth_required"] is True
    client.post("/mobile/api/account/reauth", headers=_bearer(tok), json={"password": "ownerpass1"})
    r = client.post("/mobile/api/account/team/invite", headers=_bearer(tok),
                    json={"name": "Eve", "email": "eve@x.test", "role": "client"})
    assert r.get_json()["ok"] is True
    assert [k for _to, k, _d in sent["notice"]] == ["principal_added"]
    assert sent["notice"][0][0] == "owner@x.test" and "eve@x.test" in sent["notice"][0][2]


def test_a_fresh_sign_in_counts_as_the_step_up(db_path, client):
    rid, owner = _setup(db_path, two_fa=False)
    tok = _phone_login(client)["token"]
    assert client.post("/mobile/api/sessions/revoke-others", headers=_bearer(tok)).get_json()["ok"] is True


def test_not_me_suspends_co_owners_added_after_the_reporter(db_path, client):
    rid, owner = _setup(db_path, two_fa=False)
    old = create_user(rid, "partner", "partner@x.test", "partner-pass-1", db_path=db_path)
    upsert_membership(old, rid, "client", db_path=db_path)
    conn = models.get_conn(db_path)
    conn.execute("UPDATE users SET created_at=datetime('now', '-30 days') WHERE id IN (?, ?)", (owner, old))
    conn.commit()
    conn.close()
    eve = create_user(rid, "eve", "eve@x.test", "eve-pass-123", db_path=db_path)
    upsert_membership(eve, rid, "client", db_path=db_path)
    mate = create_user(rid, "mate", "mate@x.test", "mate-pass-123", role="member", db_path=db_path)
    eve_tok = auth.create_session(eve, db_path=db_path)
    report = auth.create_login_report(owner, None, db_path=db_path)
    user = auth.consume_login_report(report, db_path=db_path)
    assert user["suspended_logins"] == ["eve"]
    assert auth.get_session_user(eve_tok, db_path=db_path) is None
    conn = models.get_conn(db_path)
    active = {r["username"]: r["is_active"] for r in conn.execute("SELECT username, is_active FROM users")}
    events = [r["event_type"] for r in conn.execute("SELECT event_type FROM activity_log WHERE restaurant_id=?",
                                                    (rid,))]
    conn.close()
    assert active["partner"] == 1 and active["mate"] == 1 and active["eve"] == 0
    assert "principal_logins_suspended" in events
    assert "principal_logins_suspended" in models.ACCOUNT_EVENT_TYPES


def test_a_writable_view_as_asks_the_admin_for_the_password(db_path, monkeypatch):
    import admin_routes
    hq = create_restaurant(Restaurant(name="HQ", owner_email="w@x.test"), db_path=db_path)
    admin = create_user(hq, "will", "w@x.test", "Admin-pass-2026", is_admin=True, db_path=db_path)
    rid, owner = _setup(db_path, two_fa=False)
    tok = auth.create_session(admin, db_path=db_path)              # no password typed this session
    app = Flask(__name__, template_folder=TEMPLATES)
    app.secret_key = "x"
    app.register_blueprint(admin_routes.admin_bp)
    monkeypatch.setattr(admin_routes, "tell_tenant_support_access", lambda *a, **k: None)
    c = app.test_client()
    c.set_cookie("session_token", tok)
    c.set_cookie("csrf_js", "t")
    page = c.get(f"/admin/view-as/{rid}").get_data(as_text=True)
    assert "name='password'" in page
    r = c.post(f"/admin/view-as/{rid}", headers={"X-CSRF": "t"}, data={"reason": "checking a ticket for them"})
    assert r.status_code == 403
    r = c.post(f"/admin/view-as/{rid}", headers={"X-CSRF": "t"},
               data={"reason": "checking a ticket for them", "password": "Admin-pass-2026"})
    assert r.status_code == 302


def test_the_mobile_writable_view_as_asks_for_the_step_up(db_path, client):
    hq = create_restaurant(Restaurant(name="HQ", owner_email="w@x.test"), db_path=db_path)
    admin = create_user(hq, "will", "w@x.test", "Admin-pass-2026", is_admin=True, db_path=db_path)
    rid, owner = _setup(db_path, two_fa=False)
    tok = auth.create_session(admin, device_type="ios", db_path=db_path)
    r = client.post(f"/mobile/api/admin/view-as/{rid}", headers=_bearer(tok))
    assert r.status_code == 403 and r.get_json()["reauth_required"] is True


# ── R7-06: an admin's sign-out ends its views; a demoted admin's is read-only

def test_signing_an_admin_out_everywhere_ends_its_view_as(db_path):
    hq = create_restaurant(Restaurant(name="HQ", owner_email="w@x.test"), db_path=db_path)
    admin = create_user(hq, "will", "w@x.test", "Admin-pass-2026", is_admin=True, db_path=db_path)
    rid, owner = _setup(db_path, two_fa=False)
    view = auth.create_view_as_session(owner, {"id": admin}, read_only=False, db_path=db_path)
    assert auth.get_session_user(view, db_path=db_path)
    auth.end_login_access(admin, db_path=db_path)
    assert auth.get_session_user(view, db_path=db_path) is None


def test_a_demoted_admins_view_is_read_only(db_path):
    hq = create_restaurant(Restaurant(name="HQ", owner_email="w@x.test"), db_path=db_path)
    admin = create_user(hq, "sam", "s@x.test", "Admin-pass-2026", is_admin=True, db_path=db_path)
    rid, owner = _setup(db_path, two_fa=False)
    view = auth.create_view_as_session(owner, {"id": admin}, read_only=False, db_path=db_path)
    assert auth.get_session_user(view, db_path=db_path)["view_as_read_only"] is False
    conn = models.get_conn(db_path)
    conn.execute("UPDATE users SET is_admin=0, role='support' WHERE id=?", (admin,))
    conn.commit()
    conn.close()
    assert auth.get_session_user(view, db_path=db_path)["view_as_read_only"] is True


# ── R7-23: the staff-account routes never reach an account holder ──────────

def test_a_co_owner_cannot_demote_or_deactivate_the_owner_through_staff_routes(db_path, client):
    rid, owner = _setup(db_path, two_fa=False)
    co = create_user(rid, "co", "co@x.test", "co-owner-pass", db_path=db_path)
    upsert_membership(co, rid, "client", db_path=db_path)
    tok = auth.create_session(co, device_type="ios", db_path=db_path)
    conn = models.get_conn(db_path)
    owner_m = conn.execute("SELECT id FROM memberships WHERE user_id=? AND restaurant_id=?", (owner, rid)).fetchone()[0]
    own_m = conn.execute("SELECT id FROM memberships WHERE user_id=? AND restaurant_id=?", (co, rid)).fetchone()[0]
    conn.close()
    for path, body in ((f"/mobile/api/account/staff/{owner_m}", {"role": "member"}),
                       (f"/mobile/api/account/staff/{owner_m}/deactivate", {}),
                       (f"/mobile/api/account/staff/{owner_m}/unlink", {}),
                       (f"/mobile/api/account/staff/{owner_m}/pin", {"pin": "4826"}),
                       (f"/mobile/api/account/staff/{own_m}", {"role": "member"})):
        r = client.post(path, headers=_bearer(tok), json=body)
        assert r.status_code == 403, path
    conn = models.get_conn(db_path)
    row = conn.execute("SELECT role, is_active FROM memberships WHERE id=?", (owner_m,)).fetchone()
    conn.close()
    assert (row["role"], row["is_active"]) == ("client", 1)


# ── R7-19: Ask's staff-contact tool keeps the REST route's rules ──────────

def test_asks_staff_contact_tool_runs_the_routes_permission_and_record(db_path, monkeypatch):
    import ask_cavnar_tools as tools
    import json
    import people
    rid = create_restaurant(Restaurant(name="R", owner_email="o@x.test", module_labor=1), db_path=db_path)
    models.set_staff_contact(rid, "Maria G.", "maria@x.test", None, db_path=db_path)
    told = []
    monkeypatch.setattr(people, "_send_staff_email", lambda *a, **k: told.append(a[1]))
    view = models.get_restaurant(rid, db_path=db_path)
    view._ask_dsr_user = {"id": 5, "role": "member", "restaurant_id": rid}
    assert not tools.tool_allowed("set_staff_contact", view)
    out = json.loads(tools.run_read_tool("set_staff_contact", rid,
                                         {"employee_name": "Maria G.", "email": "me@evil.test"}, view))
    assert "not available" in out.get("error", "")
    assert models.get_staff_contacts(rid, db_path=db_path)[0]["email"] == "maria@x.test"
    view._ask_dsr_user = {"id": 1, "role": "client", "restaurant_id": rid}
    out = json.loads(tools.run_read_tool("set_staff_contact", rid,
                                         {"employee_name": "Maria G.", "email": "maria.g@x.test"}, view))
    assert out["ok"] is True and told == ["maria@x.test"]          # told at the address she had
    conn = models.get_conn(db_path)
    n = conn.execute("SELECT COUNT(*) FROM change_log WHERE restaurant_id=?", (rid,)).fetchone()[0]
    conn.close()
    assert n == 1


# ── R7-25: remembered devices are the account holder's to manage ───────────

def test_a_manager_sees_and_clears_only_their_own_remembered_devices(db_path, client):
    rid, owner = _setup(db_path, two_fa=False)
    mgr = create_user(rid, "mgr", "mgr@x.test", "manager-pass-1", role="manager", db_path=db_path)
    upsert_membership(mgr, rid, "manager", db_path=db_path)
    auth.create_trusted_device(rid, owner, "Owner's Mac", db_path=db_path)
    auth.create_trusted_device(rid, mgr, "Manager's phone", db_path=db_path)
    tok = auth.create_session(mgr, device_type="ios", db_path=db_path)
    seen = client.get("/mobile/api/account/2fa/trusted-devices", headers=_bearer(tok)).get_json()["devices"]
    assert [d["label"] for d in seen] == ["Manager's phone"]
    owners = [d for d in auth.get_trusted_devices(rid, db_path=db_path) if d["label"] == "Owner's Mac"]
    assert client.post(f"/mobile/api/account/2fa/trusted-devices/{owners[0]['id']}/revoke",
                       headers=_bearer(tok)).status_code == 404
    assert client.post("/mobile/api/account/2fa/trusted-devices/revoke-all", headers=_bearer(tok)).get_json()["own_only"]
    assert [d["label"] for d in auth.get_trusted_devices(rid, db_path=db_path)] == ["Owner's Mac"]


# ── R7-05: staff code shares sized for onboarding, lanes apart ─────────────

def test_staff_code_shares_grow_with_the_fleet_and_the_staff(db_path, monkeypatch):
    monkeypatch.delenv("STAFF_OTP_HOURLY_CAP", raising=False)
    rid, _owner = _setup(db_path, two_fa=False)
    conn = models.get_conn(db_path)
    try:
        monkeypatch.setattr(auth, "_otp_join_restaurants", lambda c: 200)
        assert auth._otp_hourly_cap(conn) == max(auth.STAFF_OTP_HOURLY_CAP_DEFAULT,
                                                 auth.STAFF_OTP_PER_RESTAURANT_PLATFORM_DEFAULT * 200)
        for i in range(40):
            conn.execute("INSERT INTO staff_contacts (restaurant_id, employee_name) VALUES (?, ?)", (rid, f"P{i}"))
        conn.commit()
        assert auth._otp_restaurant_cap(conn, rid) == 50                  # 40 staff x 125%
        # Sign-ups spending the restaurant's share leave forgot-PIN its own.
        for _ in range(50):
            assert auth._otp_cap_reserve(conn, "signup", restaurant_id=rid)
        assert not auth._otp_cap_reserve(conn, "signup", restaurant_id=rid)
        assert auth._otp_cap_reserve(conn, "pin_reset", restaurant_id=rid)
    finally:
        conn.close()
        auth._otp_platform_memo.update(at=0.0, n=0)


def test_no_sign_up_code_without_a_name_to_claim(db_path, monkeypatch):
    import staff_roster
    rid, _owner = _setup(db_path, two_fa=False)
    code = auth.get_join_code(rid, db_path=db_path)
    monkeypatch.setattr(staff_roster, "roster_names_for_restaurant", lambda r, db_path=None: [])
    with pytest.raises(auth.SignupError, match="already has an account"):
        auth.start_staff_signup("+13125550100", optin=True, join_code=code, db_path=db_path)


def test_deactivating_a_staff_login_rotates_the_join_code(db_path, client):
    rid, owner = _setup(db_path, two_fa=False)
    old = auth.get_join_code(rid, db_path=db_path)
    staff = create_user(rid, "ana.1", "ana.1@staff.invalid", "x" * 20, role="employee", db_path=db_path)
    m = upsert_membership(staff, rid, "employee", employee_name="Ana", db_path=db_path)
    tok = auth.create_session(owner, device_type="ios", db_path=db_path)
    body = client.post(f"/mobile/api/account/staff/{m['id']}/deactivate", headers=_bearer(tok)).get_json()
    assert body["ok"] and body["join_code_rotated"] and body["join_code"] != old
    assert auth.restaurant_for_join_code(old, db_path=db_path) is None
    assert auth.restaurant_for_join_code(body["join_code"], db_path=db_path) == rid


# ── R7-07: passkey options are filtered before they write ──────────────────

def test_passkey_options_are_throttled_in_memory_before_a_row_is_written(db_path, client, monkeypatch):
    import passkeys
    calls = []
    monkeypatch.setattr(passkeys, "authentication_options", lambda host: calls.append(host) or {"challenge": "x"})
    codes = [client.post("/mobile/api/passkey/options").status_code
             for _ in range(auth_routes.PASSKEY_OPTIONS_PER_MINUTE + 3)]
    assert codes.count(200) == auth_routes.PASSKEY_OPTIONS_PER_MINUTE and codes[-1] == 429
    assert len(calls) == auth_routes.PASSKEY_OPTIONS_PER_MINUTE


# ── R7-08: the billing portal needs the account holder signed in ──────────

def test_the_card_link_asks_for_a_sign_in_before_the_billing_portal(db_path, monkeypatch):
    import billing_jobs
    import emails
    import webhook_routes
    real = models.get_conn
    monkeypatch.setattr(webhook_routes, "get_conn", lambda *a, **k: real(db_path), raising=False)
    rid, owner = _setup(db_path, two_fa=False)
    update_restaurant(rid, {"billing_status": "active", "stripe_customer_id": "cus_1"}, db_path=db_path)
    monkeypatch.setattr(billing_jobs, "fix_payment_url", lambda r, prefer_invoice=True: "https://billing.stripe.com/p")
    app = Flask(__name__)
    app.register_blueprint(webhook_routes.webhook_bp)
    c = app.test_client()
    path = "/" + emails.pay_link(rid, "card").split("://", 1)[1].split("/", 1)[1]
    r = c.get(path)
    assert r.status_code == 302 and r.headers["Location"].startswith("/login?next=")
    c.set_cookie("session_token", auth.create_session(owner, db_path=db_path))
    r = c.get(path)
    assert r.status_code == 302 and r.headers["Location"] == "https://billing.stripe.com/p"
    other_rid = create_restaurant(Restaurant(name="Other", owner_email="z@x.test"), db_path=db_path)
    stranger = create_user(other_rid, "z", "z@x.test", "stranger-pass", db_path=db_path)
    upsert_membership(stranger, other_rid, "client", db_path=db_path)
    c.set_cookie("session_token", auth.create_session(stranger, db_path=db_path))
    assert c.get(path).headers["Location"].startswith("/login?next=")


# ── R7-09: sign-in texts only to allowed countries, capped per number ─────

def test_sign_in_texts_follow_the_country_list_and_a_per_number_cap(db_path, monkeypatch):
    import notify
    monkeypatch.setattr(notify, "TWILIO_SID", "")
    res = notify.send_sms_result("+447700900123", "code 123456", use_case="otp", db_path=db_path)
    assert res.status == "blocked" and res.error_code == notify.SMS_COUNTRY_ERROR_CODE
    conn = models.get_conn(db_path)
    for _ in range(notify.OTP_SMS_PER_NUMBER_HOURLY):
        conn.execute("INSERT INTO sms_log (use_case, to_hash, status) VALUES ('otp', ?, 'sent')",
                     (notify._sms_hash("+13125550100"),))
    conn.commit()
    conn.close()
    res = notify.send_sms_result("+13125550100", "code 123456", use_case="otp", db_path=db_path)
    assert res.status == "blocked" and res.error_code == notify.OTP_NUMBER_CAP_ERROR_CODE


def test_the_owner_phone_must_be_a_number_codes_can_reach(db_path, client):
    rid, owner = _setup(db_path, two_fa=False)
    tok = _phone_login(client)["token"]
    r = client.post("/mobile/api/account/update-profile", headers=_bearer(tok), json={"owner_phone": "+44 7700 900123"})
    assert r.status_code == 400
    r = client.post("/mobile/api/account/update-profile", headers=_bearer(tok), json={"owner_phone": "(312) 555-0100"})
    assert r.get_json()["ok"] is True


# ── R7-15: bounded, LRU, /64 ──────────────────────────────────────────────

def test_ipv6_counts_as_its_64_and_full_stores_drop_what_is_idle(monkeypatch):
    import request_limits as rl
    assert rl.ip_key("portal:2001:db8:1:2:aaaa::1") == rl.ip_key("portal:2001:db8:1:2:bbbb::9") \
        == "portal:2001:db8:1:2::/64"
    assert rl.ip_key("portal:1.2.3.4") == "portal:1.2.3.4" and rl.ip_key("2fa-app:5") == "2fa-app:5"
    assert rl.ip_key("::ffff:1.2.3.4") == "1.2.3.4"
    monkeypatch.setattr(rl, "_MAX_KEYS", 50)
    busy = "portal:9.9.9.9"
    for i in range(5):
        rl.memory_limited(busy, 100, 60, now=1000.0 + i)
    for i in range(200):                      # a flood of one-off addresses
        rl.memory_limited(f"portal:10.0.{i // 250}.{i % 250}", 100, 60, now=1001.0 + i * 0.001)
    assert busy in rl._windows                # the live window survived
    assert len(rl._windows) <= 50


def test_the_login_fallback_memory_is_pruned_on_write(monkeypatch):
    monkeypatch.setattr(auth_routes, "_LOGIN_ATTEMPTS_MAX_KEYS", 20)
    for i in range(100):
        auth_routes._record_failed_attempt(f"203.0.113.{i}")
    assert len(auth_routes._login_attempts) <= 20


# ── R7-11: support's masking reaches every /admin JSON read, fails closed ──

def _support_app(monkeypatch):
    import admin_ops
    import admin_routes
    from flask import Blueprint, jsonify
    bp = Blueprint("other_admin", __name__)

    @bp.route("/admin/alert-contacts/<int:rid>")
    def contacts(rid):
        return jsonify(ok=True, contacts=[{"name": "Ana", "phone": "+13125550100", "email": "ana@x.test"}])

    app = Flask(__name__)
    app.register_blueprint(bp)
    app.after_request(admin_routes._admin_support_redaction)
    monkeypatch.setattr(admin_ops, "viewer_role", lambda: "support")
    return app.test_client(), admin_ops


def test_support_reads_are_masked_on_any_admin_blueprint(monkeypatch):
    c, _ = _support_app(monkeypatch)
    r = c.get("/admin/alert-contacts/3")
    body = r.get_data(as_text=True)
    assert r.headers.get("X-Redacted") == "support"
    assert "ana@x.test" not in body and "5550100" not in body


def test_support_masking_fails_closed(monkeypatch):
    c, admin_ops = _support_app(monkeypatch)

    def boom(_data):
        raise RuntimeError("cannot mask")
    monkeypatch.setattr(admin_ops, "redact_for_support", boom)
    r = c.get("/admin/alert-contacts/3")
    assert r.status_code == 500 and "ana@x.test" not in r.get_data(as_text=True)


def test_the_app_registers_the_masking_hook_app_wide():
    src = open(os.path.join(ROOT, "hosted_dashboard.py"), encoding="utf-8").read()
    assert "app.after_request(_support_mask)" in src


# ── R7-14: Sentry events leave scrubbed, without frame locals ──────────────

def test_sentry_events_are_scrubbed():
    import sentry_setup
    event = {"exception": {"values": [{
        "value": "GET https://maps.googleapis.com/x?key=AIzaSECRET&q=1 failed",
        "stacktrace": {"frames": [{"function": "f", "vars": {"client_secret": "shh"}}]}}]},
        "request": {"url": "https://x.test/cb?token=abc123", "query_string": "access_token=zzz"},
        "extra": {"note": "Authorization: Bearer sk-live-123"}}
    out = sentry_setup.scrub_event(event)
    flat = repr(out)
    for secret in ("AIzaSECRET", "shh", "abc123", "zzz", "sk-live-123"):
        assert secret not in flat, secret
    src = open(os.path.join(ROOT, "sentry_setup.py"), encoding="utf-8").read()
    assert "include_local_variables=False" in src and "before_send=scrub_event" in src


# ── R7-13: the remaining admin writes record themselves, typed ─────────────

def test_the_audited_decorator_writes_one_typed_row_without_values(db_path, monkeypatch):
    import admin_events
    from flask import jsonify
    rows = []
    monkeypatch.setattr(admin_events, "record_admin_action",
                        lambda actor, action, **k: rows.append((actor.get("username"), action, k)))

    @admin_events.audited("thing.saved", snapshot=lambda kw: {"n": 1})
    def route(restaurant_id, current_user):
        return jsonify(ok=True)

    app = Flask(__name__)
    with app.test_request_context("/admin/x/4", method="POST", json={"token": "SECRET-VALUE", "note": "x"}):
        route(restaurant_id=4, current_user={"id": 9, "username": "will"})
    (who, action, k), = rows
    assert (who, action, k["restaurant_id"], k["result"]) == ("will", "thing.saved", 4, "ok")
    assert k["after"]["fields"] == ["note", "token"] and "SECRET-VALUE" not in repr(k)
    assert k["before"] == {"n": 1}


def test_the_listed_admin_writes_carry_a_typed_audit_and_the_step_ups():
    import ast
    # (Staff notes, the Instagram refresh and the review import already
    # recorded themselves through their own typed calls.)
    want = {"admin_routes.py": ("save_staff_availability_route", "delete_staff_availability_route", "upload_data",
                                "upload_menu_pdf", "fetch_menu_from_url_route", "refresh_menu_notes",
                                "admin_create_changelog", "admin_delete_changelog", "admin_create_template",
                                "admin_delete_template"),
            "status_routes.py": ("admin_update_status", "admin_create_incident", "admin_update_incident",
                                 "admin_resolve_incident")}
    for rel, names in want.items():
        tree = ast.parse(open(os.path.join(ROOT, rel), encoding="utf-8").read())
        fns = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        for name in names:
            decos = [ast.unparse(d) for d in fns[name].decorator_list]
            assert any(".audited(" in d for d in decos), (rel, name)
    rp = open(os.path.join(ROOT, "rpower_routes.py"), encoding="utf-8").read()
    assert rp.count("_audited_rpower(\"rpower.") == 3
    ar = open(os.path.join(ROOT, "admin_routes.py"), encoding="utf-8").read()
    assert "@recent_auth_required()\ndef create_client(" in ar and "@recent_auth_required()\ndef reactivate_client(" in ar
    assert "\"client.created\"" in ar
    import admin_routes
    assert "owner_phone" in admin_routes._SETTINGS_STEP_UP_FIELDS


# ── R7-24: wages stay with the logins that see pay ────────────────────────

def test_a_login_that_does_not_see_pay_gets_no_wage_from_the_schedule_checks(monkeypatch):
    import inspect
    import floor_api
    import strategy_routes
    src = inspect.getsource(strategy_routes._do_schedule_violations)
    assert "sees_pay = _sees_pay(u)" in src and 'out["cost"] = None' in src
    assert "_grr(_rid(u)) if sees_pay else {}" in src
    assert "_sees_pay(u)" in inspect.getsource(floor_api.shift_edit_plan)
    app = Flask(__name__)
    monkeypatch.setattr(strategy_routes, "_may_rate", lambda u: True)
    monkeypatch.setattr(strategy_routes, "_sees_pay", lambda u: False)
    for sent in (14.0, 99.0):
        with app.test_request_context("/api/people/x", method="POST", json={"pay_rate": sent}):
            body, status = strategy_routes._do_person_set({"id": 1, "restaurant_id": 1}, "x")
        assert status == 400 and body["error"] == "Pay is set per role in Labor → Targets & rates."
