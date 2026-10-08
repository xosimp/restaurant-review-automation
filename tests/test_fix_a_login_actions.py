"""Fix round A: what the console does to one login.

  #60/#93  role change writes users.role AND the membership (the session
           reads the membership), accepts Teammate, keeps the last owner,
           refuses admin, support and staff rows, is audited, needs step-up;
           reactivate refuses an admin row like deactivate always did.
  #139     deactivate ends the login's sessions and remembered devices, so
           reactivating revives nothing; freeze reaches members of the
           restaurant homed elsewhere and sessions switched into it.
  #55      2FA reset, lockout clear, single-session revoke: step-up + audit.
  #99      support logins are provisioned from the console.
  #87      support reaches no write route, and the role is on the request.
  #88      a per-session ceiling on /admin.
"""
import json

import pytest
from flask import Flask

import admin_routes
import auth
import auth_routes
import client_api
import models
import security
from auth import create_session, create_user, get_session_user, hash_session_token, init_auth, upsert_membership
from models import Restaurant, create_restaurant, update_restaurant

CSRF = "fix-a-actions-csrf"


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, auth_routes, admin_routes, client_api):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    monkeypatch.delenv("ADMIN_REQUIRE_2FA", raising=False)
    init_auth(db_path=db_path)
    models.init_two_fa_backup_codes(db_path=db_path)
    models.init_email_log(db_path=db_path)


@pytest.fixture
def app():
    a = Flask(__name__, template_folder="../templates")
    a.secret_key = "fix-a"
    for bp in (auth_routes.auth_bp, admin_routes.admin_bp, client_api.client_bp):
        a.register_blueprint(bp)
    return a


def _hq(db_path):
    return create_restaurant(Restaurant(name="Cavnar AI Admin", owner_email="w@x.test"), db_path=db_path)


def _admin_client(app, db_path, stepped_up=True, support=False, username=None):
    hq = _hq(db_path)
    if support:
        name = username or "sup"
        uid = create_user(hq, name, f"{name}@x.test", "Support-pass-2026", role="support", db_path=db_path)
    else:
        name = username or "will"
        uid = create_user(hq, name, f"{name}@x.test", "Admin-pass-2026", is_admin=True, db_path=db_path)
    tok = create_session(uid, password_verified_at=True if stepped_up else None, db_path=db_path)
    c = app.test_client()
    c.set_cookie("session_token", tok)
    c.set_cookie("csrf_js", CSRF)
    return c, uid, tok


def _post(c, url, body=None):
    return c.post(url, json=body or {}, headers={"X-CSRF": CSRF})


def _restaurant(db_path, name="Client Grill", email="owner@client.test"):
    return create_restaurant(Restaurant(name=name, owner_email=email), db_path=db_path)


def _login(db_path, rid, username, role="client", email=None):
    uid = create_user(rid, username, email or f"{username}@client.test", "Login-pass-2026", role=role, db_path=db_path)
    upsert_membership(uid, rid, role, db_path=db_path)
    return uid


def _one(db_path, sql, args=()):
    conn = models.get_conn(db_path)
    try:
        return conn.execute(sql, args).fetchone()
    finally:
        conn.close()


def _events(db_path, kind):
    conn = models.get_conn(db_path)
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM admin_events WHERE event_type=? ORDER BY id", (kind,))]
    finally:
        conn.close()


# ── #60 / #93: role change ──────────────────────────────────────────────────

def test_a_role_change_takes_effect_on_the_session(app, db_path):
    c, _, _ = _admin_client(app, db_path)
    rid = _restaurant(db_path)
    _login(db_path, rid, "owner")
    gm = _login(db_path, rid, "gm")
    gm_tok = create_session(gm, db_path=db_path)
    r = _post(c, "/admin/api/set-user-role", {"user_id": gm, "role": "member"})
    assert r.status_code == 200 and r.get_json()["previous_role"] == "client"
    assert get_session_user(gm_tok, db_path=db_path)["role"] == "member"      # the membership moved too
    ev = _events(db_path, "login_role_changed")[-1]
    # Typed columns (integration wave: the one audit call, record_admin_action).
    assert json.loads(ev["before_json"]) == {"role": "client"} and json.loads(ev["after_json"]) == {"role": "member"}
    assert ev["actor"] == "will" and ev["target"] == f"user_id:{gm},username:gm"


def test_teammate_and_manager_are_accepted_and_junk_is_not(app, db_path):
    c, _, _ = _admin_client(app, db_path)
    rid = _restaurant(db_path)
    _login(db_path, rid, "owner")
    mate = _login(db_path, rid, "mate")
    assert _post(c, "/admin/api/set-user-role", {"user_id": mate, "role": "manager"}).status_code == 200
    assert _post(c, "/admin/api/set-user-role", {"user_id": mate, "role": "superuser"}).status_code == 400


def test_the_last_owner_cannot_be_demoted(app, db_path):
    c, _, _ = _admin_client(app, db_path)
    rid = _restaurant(db_path)
    only = _login(db_path, rid, "owner")
    r = _post(c, "/admin/api/set-user-role", {"user_id": only, "role": "manager"})
    assert r.status_code == 400 and "at least one owner" in r.get_json()["error"]


def test_an_admin_row_can_never_be_rewritten(app, db_path):
    c, admin_id, _ = _admin_client(app, db_path)
    other_admin = create_user(_hq(db_path), "ops", "ops@x.test", "Admin-pass-2026", is_admin=True,
                              role="client", db_path=db_path)
    r = _post(c, "/admin/api/set-user-role", {"user_id": other_admin, "role": "owner"})
    assert r.status_code == 400 and "admin" in r.get_json()["error"]
    assert _one(db_path, "SELECT role, is_admin FROM users WHERE id=?", (other_admin,))[:] == ("client", 1)


def test_the_legacy_role_writer_skips_admin_rows(db_path):
    hq = _hq(db_path)
    admin = create_user(hq, "ops", "ops@x.test", "Admin-pass-2026", is_admin=True, role="client", db_path=db_path)
    auth.set_user_role(admin, "owner", db_path=db_path)
    assert _one(db_path, "SELECT role FROM users WHERE id=?", (admin,))[0] == "client"


def test_a_role_change_needs_a_recent_password(app, db_path):
    c, _, _ = _admin_client(app, db_path, stepped_up=False)
    rid = _restaurant(db_path)
    _login(db_path, rid, "owner")
    gm = _login(db_path, rid, "gm")
    r = _post(c, "/admin/api/set-user-role", {"user_id": gm, "role": "manager"})
    assert r.status_code == 403 and r.get_json()["reauth_required"] is True


def test_reactivate_refuses_an_admin_row(app, db_path):
    c, _, _ = _admin_client(app, db_path)
    other_admin = create_user(_hq(db_path), "ops", "ops@x.test", "Admin-pass-2026", is_admin=True, db_path=db_path)
    conn = models.get_conn(db_path)
    conn.execute("UPDATE users SET is_active=0 WHERE id=?", (other_admin,))
    conn.commit()
    conn.close()
    assert _post(c, "/admin/reactivate-client/%d" % other_admin).status_code == 404
    assert _one(db_path, "SELECT is_active FROM users WHERE id=?", (other_admin,))[0] == 0


def test_reactivate_never_revives_a_login_its_holder_deleted(app, db_path):
    """Re-audit 10/8/26, #5: Delete my login is the holder's decision; the
    console says to invite them again instead of switching the row back on."""
    c, _, _ = _admin_client(app, db_path)
    rid = _restaurant(db_path)
    _login(db_path, rid, "owner")
    mate = _login(db_path, rid, "mate", role="member")
    assert auth.delete_own_login(mate, rid, db_path=db_path)["ok"]
    r = _post(c, "/admin/reactivate-client/%d" % mate)
    assert r.status_code == 409 and r.get_json()["deleted_by_holder"] is True
    assert _one(db_path, "SELECT is_active FROM users WHERE id=?", (mate,))[0] == 0


# ── #139: deactivate ends everything; freeze reaches the whole restaurant ───

def test_deactivate_ends_sessions_and_devices_and_reactivate_revives_nothing(app, db_path):
    c, _, _ = _admin_client(app, db_path)
    rid = _restaurant(db_path)
    owner = _login(db_path, rid, "owner")
    phone = create_session(owner, device_type="ios", db_path=db_path)
    auth.create_trusted_device(rid, owner, "phone", db_path=db_path)
    r = _post(c, "/admin/deactivate-client/%d" % owner)
    assert r.status_code == 200 and r.get_json()["sessions_ended"] == 1 and r.get_json()["devices_forgotten"] == 1
    assert _post(c, "/admin/reactivate-client/%d" % owner).status_code == 200
    assert get_session_user(phone, db_path=db_path) is None                 # not revived
    assert _events(db_path, "login_deactivated") and _events(db_path, "login_reactivated")


def test_the_reactivation_email_goes_to_the_login_not_the_owner(app, db_path, monkeypatch):
    import emails
    import scheduler
    sent = []
    monkeypatch.setattr(emails, "send_reactivation_email", lambda **k: sent.append(k["to_email"]))
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: True)
    c, _, _ = _admin_client(app, db_path)
    rid = _restaurant(db_path)
    _login(db_path, rid, "owner", email="owner@client.test")
    coowner = _login(db_path, rid, "second", email="second@client.test")
    mate = _login(db_path, rid, "mate", role="member", email="mate@client.test")
    for uid in (coowner, mate):
        _post(c, "/admin/deactivate-client/%d" % uid)
        _post(c, "/admin/reactivate-client/%d" % uid)
    assert sent == ["second@client.test"]              # the teammate is not told the account is back on


def test_freeze_reaches_a_member_homed_elsewhere_and_a_switched_session(db_path):
    a = _restaurant(db_path, "Loc A")
    b = _restaurant(db_path, "Loc B")
    boss = _login(db_path, a, "boss", role="owner")
    upsert_membership(boss, b, "owner", db_path=db_path)
    boss_tok = create_session(boss, db_path=db_path)
    visitor = _login(db_path, a, "visitor")
    switched = create_session(visitor, db_path=db_path)
    conn = models.get_conn(db_path)
    conn.execute("UPDATE sessions SET active_restaurant_id=? WHERE token=?", (b, hash_session_token(switched)))
    conn.commit()
    conn.close()
    n = security.freeze_restaurant(b, actor={"username": "will"}, reason="takeover", db_path=db_path)
    assert n == 1                                             # boss, through the membership
    assert get_session_user(boss_tok, db_path=db_path) is None
    assert _one(db_path, "SELECT must_reset_password FROM users WHERE id=?", (boss,))[0] == 1
    assert _one(db_path, "SELECT COUNT(*) FROM sessions WHERE token=?", (hash_session_token(switched),))[0] == 0


# ── #55: 2FA reset, lockout clear, session revoke ───────────────────────────

def test_a_2fa_reset_turns_the_restaurants_switch_off_and_says_so(app, db_path):
    c, _, _ = _admin_client(app, db_path)
    rid = _restaurant(db_path)
    owner = _login(db_path, rid, "owner")
    update_restaurant(rid, {"two_fa_enabled": 1}, db_path=db_path)
    models.generate_backup_codes(rid, db_path=db_path)
    r = _post(c, "/admin/api/users/%d/reset-2fa" % owner)
    assert r.status_code == 200 and r.get_json()["scope"] == "restaurant"
    assert not models.get_restaurant(rid, db_path=db_path).two_fa_enabled
    assert models.count_unused_backup_codes(rid, db_path=db_path) == 0
    ev = _events(db_path, "two_factor_reset")[-1]
    assert json.loads(ev["before_json"])["two_fa_enabled"] is True
    assert json.loads(ev["after_json"])["two_fa_enabled"] is False


def test_a_2fa_reset_on_an_internal_login_clears_its_own_second_factor(app, db_path):
    c, _, _ = _admin_client(app, db_path)
    sup = create_user(_hq(db_path), "sup", "sup@x.test", "Support-pass-2026", role="support", db_path=db_path)
    conn = models.get_conn(db_path)
    conn.execute("UPDATE users SET two_fa_enabled=1, two_fa_method='email' WHERE id=?", (sup,))
    conn.commit()
    conn.close()
    auth.generate_user_backup_codes(sup, db_path=db_path)
    r = _post(c, "/admin/api/users/%d/reset-2fa" % sup)
    assert r.get_json()["scope"] == "login"
    assert _one(db_path, "SELECT two_fa_enabled FROM users WHERE id=?", (sup,))[0] == 0
    assert auth.count_unused_user_backup_codes(sup, db_path=db_path) == 0


def test_a_lockout_is_listed_cleared_and_audited(app, db_path):
    c, _, _ = _admin_client(app, db_path)
    rid = _restaurant(db_path)
    owner = _login(db_path, rid, "owner")
    for _ in range(security.ACCOUNT_MAX):
        security.record_login_failure("9.9.9.9", "owner")
    assert security.login_throttled("1.1.1.1", "owner")[0] is True
    listed = c.get("/admin/api/lockouts").get_json()["lockouts"]
    assert any(l["username"] == "owner" for l in listed)
    r = _post(c, "/admin/api/users/%d/clear-lockout" % owner)
    assert r.status_code == 200
    assert security.login_throttled("1.1.1.1", "owner")[0] is False
    assert _events(db_path, "lockout_cleared")


def test_one_session_is_revoked_by_its_handle_and_the_rest_stay(app, db_path):
    c, _, _ = _admin_client(app, db_path)
    rid = _restaurant(db_path)
    owner = _login(db_path, rid, "owner")
    web = create_session(owner, db_path=db_path)
    phone = create_session(owner, device_type="ios", db_path=db_path)
    view = c.get("/admin/api/users/%d/security" % owner).get_json()
    handles = {s["device_type"]: s["session_id"] for s in view["sessions"]}
    r = _post(c, "/admin/api/users/%d/sessions/%s/revoke" % (owner, handles["ios"]))
    assert r.status_code == 200
    assert get_session_user(phone, db_path=db_path) is None and get_session_user(web, db_path=db_path) is not None
    assert _post(c, "/admin/api/users/%d/sessions/%s/revoke" % (owner, handles["ios"])).status_code == 404
    assert _events(db_path, "session_revoked")


def test_the_support_actions_all_need_a_recent_password(app, db_path):
    c, _, _ = _admin_client(app, db_path, stepped_up=False)
    rid = _restaurant(db_path)
    owner = _login(db_path, rid, "owner")
    for url in ("/admin/api/users/%d/reset-2fa", "/admin/api/users/%d/clear-lockout",
                "/admin/api/users/%d/revoke-sessions", "/admin/deactivate-client/%d"):
        r = _post(c, url % owner)
        assert r.status_code == 403 and r.get_json()["reauth_required"] is True, url


# ── #99 / #87: support logins ───────────────────────────────────────────────

def test_a_support_login_is_provisioned_from_the_console_and_can_only_read(app, db_path, monkeypatch):
    import scheduler
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: False)     # a local backend sends nothing
    c, admin_id, _ = _admin_client(app, db_path)
    r = _post(c, "/admin/api/support-logins", {"username": "helper", "email": "helper@cavnar.test"})
    assert r.status_code == 200 and r.get_json()["reset_link_sent"] is False
    uid = r.get_json()["user_id"]
    row = _one(db_path, "SELECT role, is_admin, restaurant_id FROM users WHERE id=?", (uid,))
    assert row["role"] == "support" and row["is_admin"] == 0
    assert _events(db_path, "support_login_created")
    listed = c.get("/admin/api/support-logins").get_json()["logins"]
    assert [l["username"] for l in listed] == ["helper"]
    # The new login reads the console and writes nothing.
    tok = create_session(uid, password_verified_at=True, db_path=db_path)
    s = app.test_client()
    s.set_cookie("session_token", tok)
    s.set_cookie("csrf_js", CSRF)
    assert s.get("/admin/api/support-logins").status_code == 200
    rid = _restaurant(db_path)
    owner = _login(db_path, rid, "owner")
    for url in ("/admin/api/users/%d/reset-2fa" % owner, "/admin/deactivate-client/%d" % owner,
                "/admin/api/set-user-role", "/admin/api/reauth"):
        assert _post(s, url, {"user_id": owner, "role": "member"}).status_code == 403, url


def test_a_support_login_is_not_shown_sign_in_addresses(app, db_path):
    """The role reaches the data layer (auth.current_admin_role, set on the
    request by admin_required): the security view withholds IPs and user
    agents from support; an admin sees them."""
    sup, _, _ = _admin_client(app, db_path, support=True)
    rid = _restaurant(db_path)
    owner = _login(db_path, rid, "owner")
    create_session(owner, ip_address="203.0.113.9", user_agent="Mozilla/5.0", db_path=db_path)
    view = sup.get("/admin/api/users/%d/security" % owner).get_json()
    assert view["sessions"] and "ip_address" not in view["sessions"][0] and "user_agent" not in view["sessions"][0]
    adm, _, _ = _admin_client(app, db_path, username="ops")
    assert adm.get("/admin/api/users/%d/security" % owner).get_json()["sessions"][0]["ip_address"] == "203.0.113.9"


def test_current_admin_role_answers_for_admin_and_support(monkeypatch):
    app = Flask(__name__)
    with app.test_request_context("/admin"):
        monkeypatch.setattr(auth, "get_current_user", lambda: {"id": 1, "is_admin": 0, "role": "support"})
        assert auth.current_admin_role() == "support"
    with app.test_request_context("/admin"):
        monkeypatch.setattr(auth, "get_current_user", lambda: {"id": 1, "is_admin": 1})
        assert auth.current_admin_role() == "admin"
    with app.test_request_context("/admin"):
        monkeypatch.setattr(auth, "get_current_user", lambda: {"id": 1, "is_admin": 0, "role": "client"})
        assert auth.current_admin_role() is None


# ── #88: the per-session ceiling ────────────────────────────────────────────

def test_an_admin_session_is_rate_limited_and_writes_have_a_lower_ceiling(app, db_path, monkeypatch):
    monkeypatch.setattr(security, "ADMIN_REQUESTS_PER_MINUTE", 6)
    monkeypatch.setattr(security, "ADMIN_WRITES_PER_MINUTE", 2)
    c, _, _ = _admin_client(app, db_path)
    for _ in range(2):
        assert _post(c, "/admin/api/reauth", {"password": "Admin-pass-2026"}).status_code == 200
    w = _post(c, "/admin/api/reauth", {"password": "Admin-pass-2026"})
    assert w.status_code == 429 and w.get_json()["rate_limited"] is True and w.headers.get("Retry-After")
    assert c.get("/admin/api/support-logins").status_code == 200           # reads still go
    for _ in range(3):
        c.get("/admin/api/support-logins")
    assert c.get("/admin/api/support-logins").status_code == 429
    # Another admin session is not held by this one's budget.
    other, _, _ = _admin_client(app, db_path, username="ops")
    assert other.get("/admin/api/lockouts").status_code == 200
