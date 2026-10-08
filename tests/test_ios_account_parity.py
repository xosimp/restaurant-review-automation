"""iOS parity audit 10/7/26 — Account, settings and auth (items 2, 7, 13,
27, 44, 57, 65, 70, 80, 86, 88, 89 and the Account matrix rows).

The backend halves: passkeys over bearer tokens, a teammate deleting their
own login, alert switches saved only when sent, the staff sign-in notice
twin, POS "Sync now" and RPOWER disconnect twins, the security checkup and
account health scored once on the server, and the fields the app's Account
screen now reads. Pinned against the source where a rule must hold for
every client (auto-approve's body keys, the morning brief's hours)."""
import os
import re

import pytest
from flask import Flask

import auth
import client_api
import mobile_api
import models
from auth import create_user, init_auth
from models import Restaurant, create_restaurant
from test_passkeys import Authenticator

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IOS = os.path.join(ROOT, "ios", "CavnarAI", "CavnarAI")
APP_RP = "dashboard.cavnar.ai"
APP_ORIGIN = "https://dashboard.cavnar.ai"


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    import sys
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        bound = getattr(mod, "get_conn", None) if mod is not None else None
        if bound is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    for mod in (models, auth, client_api, mobile_api):
        monkeypatch.setattr(mod, "get_conn", conn, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setenv("BASE_URL", "https://dashboard.cavnar.ai")
    init_auth(db_path=db_path)
    import push
    push.init_push(db_path=db_path)


@pytest.fixture
def app(monkeypatch):
    import auth_routes
    from client_api import client_bp
    from mobile_api import mobile_bp
    a = Flask(__name__, template_folder=os.path.join(ROOT, "templates"))
    a.register_blueprint(client_bp)
    a.register_blueprint(mobile_bp)
    a.register_blueprint(auth_routes.auth_bp)
    monkeypatch.setattr(auth_routes, "_send_restaurant_login_alert", lambda *a, **k: None)
    monkeypatch.setattr(mobile_api, "_send_login_notification", lambda *a, **k: None)
    return a


@pytest.fixture
def client(app):
    return app.test_client()


def _restaurant(db_path, name="Parity Co", **cols):
    rid = create_restaurant(Restaurant(name=name, owner_email=f"{name[:3].lower()}@x.test",
                                       owner_name="Erik"), db_path=db_path)
    if cols:
        c = models.get_conn(db_path)
        c.execute("UPDATE restaurants SET " + ", ".join(f"{k}=?" for k in cols) + " WHERE id=?",
                  (*cols.values(), rid))
        c.commit()
        c.close()
    return rid


def _login(client, db_path, rid, username="owner.ej", role=None, admin=False):
    uid = create_user(rid, username, f"{username}@x.test", "correct horse battery", db_path=db_path)
    if role or admin:
        c = models.get_conn(db_path)
        if role:
            c.execute("UPDATE users SET role=? WHERE id=?", (role, uid))
        if admin:
            c.execute("UPDATE users SET is_admin=1 WHERE id=?", (uid,))
        c.commit()
        c.close()
    r = client.post("/mobile/api/login", json={"username": username, "password": "correct horse battery"})
    assert r.status_code == 200, r.get_json()
    return uid, {"Authorization": f"Bearer {r.get_json()['token']}"}


def _row(db_path, sql, *args):
    c = models.get_conn(db_path)
    try:
        return c.execute(sql, args).fetchone()
    finally:
        c.close()


# ── #57 Passkeys over bearer tokens ─────────────────────────────────────────

def _app_device():
    return Authenticator(rp_id=APP_RP, origin=APP_ORIGIN)


def _register_in_app(client, headers, device, password="correct horse battery"):
    r = client.post("/mobile/api/passkeys/options", json={"password": password}, headers=headers)
    assert r.status_code == 200, r.get_json()
    opts = r.get_json()["options"]
    # The app's relying party is the site's own host, whatever host the
    # request reached (the entitlement names dashboard.cavnar.ai).
    assert opts["rp"]["id"] == APP_RP
    assert opts["authenticatorSelection"]["userVerification"] == "required"
    assert opts["authenticatorSelection"]["residentKey"] == "required"
    r = client.post("/mobile/api/passkeys", json={"credential": device.create(opts)}, headers=headers)
    assert r.status_code == 200, r.get_json()
    return r.get_json()["passkey"]


def _sign_in_in_app(client, device, **kw):
    r = client.post("/mobile/api/passkey/options", json={})
    assert r.status_code == 200, r.get_json()
    opts = r.get_json()["options"]
    assert opts["rpId"] == APP_RP and opts["userVerification"] == "required"
    return client.post("/mobile/api/passkey/verify",
                       json={"credential": device.get(opts, **kw), "device_id": "dev-1"})


def test_a_passkey_added_in_the_app_signs_in_with_the_login_token_and_counts_as_2fa(client, db_path):
    rid = _restaurant(db_path)
    uid, h = _login(client, db_path, rid)
    phone = _app_device()
    # The password step-up is kept: a wrong one is refused.
    assert client.post("/mobile/api/passkeys/options", json={"password": "nope"},
                       headers=h).status_code == 403
    saved = _register_in_app(client, h, phone)
    assert saved["name"] == "iPhone"
    listed = client.get("/mobile/api/passkeys", headers=h).get_json()["passkeys"]
    assert [p["id"] for p in listed] == [saved["id"]] and listed[0]["created"].count("/") == 2

    # 2FA on for the restaurant: the passkey is this sign-in's second factor.
    c = models.get_conn(db_path)
    c.execute("UPDATE restaurants SET two_fa_enabled=1 WHERE id=?", (rid,))
    c.commit()
    c.close()
    r = _sign_in_in_app(client, phone)
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["ok"] and body["token"] and body["user"]["id"] == uid and body["requires_2fa"] is False
    sess = _row(db_path, "SELECT device_type, two_factor_at IS NOT NULL FROM sessions "
                         "WHERE user_id=? AND device_id='dev-1'", uid)
    assert tuple(sess) == ("ios", 1)
    # The token is a real bearer session.
    me = client.get("/mobile/api/me", headers={"Authorization": f"Bearer {body['token']}"})
    assert me.status_code == 200 and me.get_json()["user"]["id"] == uid


def test_a_passkey_made_in_the_app_also_signs_in_on_the_website(app, client, db_path):
    rid = _restaurant(db_path)
    _uid, h = _login(client, db_path, rid)
    phone = _app_device()
    _register_in_app(client, h, phone)
    web = app.test_client()
    web.set_cookie("csrf_token", "f", domain=APP_RP)
    hh = {"Host": APP_RP}
    opts = web.post("/auth/passkey/options", json={"csrf_token": "f"}, headers=hh).get_json()["options"]
    r = web.post("/auth/passkey/verify", json={"csrf_token": "f", "credential": phone.get(opts)}, headers=hh)
    assert r.status_code == 200, r.get_json()


def test_what_the_app_passkey_sign_in_refuses(client, db_path):
    rid = _restaurant(db_path)
    uid, h = _login(client, db_path, rid)
    phone = _app_device()
    _register_in_app(client, h, phone)
    # A challenge is single use.
    opts = client.post("/mobile/api/passkey/options", json={}).get_json()["options"]
    cred = phone.get(opts, count=10)
    assert client.post("/mobile/api/passkey/verify", json={"credential": cred}).status_code == 200
    assert client.post("/mobile/api/passkey/verify", json={"credential": cred}).status_code == 401
    # "This wasn't me" keeps the login locked.
    c = models.get_conn(db_path)
    c.execute("UPDATE users SET must_reset_password=1 WHERE id=?", (uid,))
    c.commit()
    r = _sign_in_in_app(client, phone, count=20)
    assert r.status_code == 403 and r.get_json()["password_reset_required"] is True
    c.execute("UPDATE users SET must_reset_password=0, is_active=0 WHERE id=?", (uid,))
    c.commit()
    c.close()
    assert _sign_in_in_app(client, phone, count=30).status_code in (401, 403)
    # No credential at all.
    assert client.post("/mobile/api/passkey/verify", json={}).status_code == 400
    # A passkey made for another site.
    evil = Authenticator(rp_id="evil.test", origin="https://evil.test")
    assert _sign_in_in_app(client, evil).status_code == 401


def test_the_app_passkey_refuses_an_internal_login_and_needs_no_session_to_start(client, db_path):
    rid = _restaurant(db_path)
    _uid, h = _login(client, db_path, rid, "will.admin", admin=True)
    phone = _app_device()
    _register_in_app(client, h, phone)
    r = _sign_in_in_app(client, phone)
    assert r.status_code == 403 and "authenticator" in r.get_json()["error"]
    # The list and the add need a session; sign-in's options do not.
    assert client.get("/mobile/api/passkeys").status_code == 401
    assert client.post("/mobile/api/passkeys/options", json={}).status_code == 401


def test_view_as_cannot_plant_one_from_the_app_and_removal_is_per_login(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    uid, h = _login(client, db_path, rid)
    saved = _register_in_app(client, h, _app_device())
    other, h2 = _login(client, db_path, rid, "server.ana", role="member")
    assert client.post(f"/mobile/api/passkeys/{saved['id']}/remove", headers=h2).status_code == 404
    assert client.post(f"/mobile/api/passkeys/{saved['id']}/remove", headers=h).status_code == 200
    assert client.get("/mobile/api/passkeys", headers=h).get_json()["passkeys"] == []
    from auth_routes import _do_passkey_register_options, _do_passkey_register
    viewer = dict(auth.get_user_by_id(uid), acting_admin_id=99)
    assert _do_passkey_register_options(viewer, {"password": "correct horse battery"}, APP_RP)[1] == 403
    assert _do_passkey_register(viewer, {"credential": {}}, APP_RP)[1] == 403


def test_passkey_routes_are_one_body_on_both_clients():
    src = open(os.path.join(ROOT, "mobile_api.py")).read()
    for body in ("_do_passkey_login_options", "_passkey_signin_user", "_do_passkeys_list",
                 "_do_passkey_register_options", "_do_passkey_register", "_do_passkey_remove"):
        assert body in src, body
        assert f"def {body}" in open(os.path.join(ROOT, "auth_routes.py")).read()
    yml = open(os.path.join(ROOT, "ios", "CavnarAI", "project.yml")).read()
    assert "webcredentials:dashboard.cavnar.ai" in yml


# ── #13 Delete my login ─────────────────────────────────────────────────────

def test_a_teammate_deletes_their_own_login_and_everything_that_named_them(client, db_path):
    rid = _restaurant(db_path)
    _owner, _ho = _login(client, db_path, rid)
    uid, h = _login(client, db_path, rid, "server.ana", role="manager")
    _register_in_app(client, h, _app_device())
    c = models.get_conn(db_path)
    c.execute("UPDATE users SET recovery_email='ana@else.test', phone='+13125550100' WHERE id=?", (uid,))
    c.execute("INSERT INTO trusted_devices (restaurant_id, user_id, token_hash, expires_at) VALUES (?,?,?,?)",
              (rid, uid, "h1", "2099-01-01"))
    c.commit()
    c.close()
    r = client.post("/mobile/api/account/delete-login", headers=h)
    assert r.status_code == 200, r.get_json()
    row = _row(db_path, "SELECT is_active, email, username, recovery_email, phone FROM users WHERE id=?", uid)
    assert row[0] == 0 and row[1].endswith("@deleted.invalid") and row[2] == f"deleted-{uid}"
    assert row[3] is None and row[4] is None
    for table in ("sessions", "user_passkeys", "trusted_devices"):
        assert _row(db_path, f"SELECT COUNT(*) FROM {table} WHERE user_id=?", uid)[0] == 0, table
    # The token is gone with it, and the address can be invited again.
    assert client.get("/mobile/api/me", headers=h).status_code == 401
    create_user(rid, "ana2", "server.ana@x.test", "another password 1", db_path=db_path)


def test_an_owner_cannot_delete_a_login_this_way_and_view_as_never_can(client, db_path):
    rid = _restaurant(db_path)
    owner, h = _login(client, db_path, rid)
    _login(client, db_path, rid, "server.ana", role="manager")
    r = client.post("/mobile/api/account/delete-login", headers=h)
    assert r.status_code == 403 and "Close my account" in r.get_json()["error"]
    assert _row(db_path, "SELECT is_active FROM users WHERE id=?", owner)[0] == 1
    uid = _row(db_path, "SELECT id FROM users WHERE username='server.ana'")[0]
    viewer = dict(auth.get_user_by_id(uid), acting_admin_id=1)
    assert client_api._do_delete_own_login(viewer)[1] == 403
    assert _row(db_path, "SELECT is_active FROM users WHERE id=?", uid)[0] == 1


def test_the_web_route_is_the_same_body_and_billing_never_blocks_it():
    assert "/api/account/delete-login" in auth._BILLING_EXEMPT_PREFIXES
    src = open(os.path.join(ROOT, "client_api.py")).read()
    assert src.count("_do_delete_own_login(current_user)") >= 1
    assert "_do_delete_own_login(current_user)" in open(os.path.join(ROOT, "mobile_api.py")).read()


# ── #86 Alert switches: each client's missing toggles, saved only when sent ─

def _ios_alert_body(**over):
    body = {k: False for k in ("alert_1star", "alert_2star", "alert_health", "alert_neg_spike",
                               "alert_negative_trend", "alert_no_response", "alert_5star", "alert_labor_over",
                               "urgent_via_sms", "urgent_via_email", "digest_enabled",
                               "al_1star_push", "al_2star_push", "al_5star_push", "al_health_push",
                               "al_spike_push", "al_unres_push", "push_sound")}
    body.update(digest_day="monday", contacts=[])
    body.update(over)
    return body


def test_a_save_from_either_client_leaves_the_other_clients_switches_alone(client, db_path, monkeypatch):
    rid = _restaurant(db_path, alert_rating_threshold=1, alert_rating_floor=4.6, alert_any_review=1,
                      alert_resp_approved=1, alert_food_waste=1, alert_ai_visibility_drop=1,
                      alert_health_bypass_quiet=1, alert_extra_emails="gm@x.test")
    uid, h = _login(client, db_path, rid)
    # An app build that predates the web's four sends none of them.
    assert client.post("/mobile/api/account/alert-settings", json=_ios_alert_body(
        alert_food_waste=True, alert_ai_visibility_drop=True, alert_health_bypass_quiet=True,
        alert_extra_emails="gm@x.test"), headers=h).status_code == 200
    r = models.get_restaurant(rid)
    assert (r.alert_rating_threshold, r.alert_rating_floor, r.alert_any_review, r.alert_resp_approved) == (1, 4.6, 1, 1)
    # A web page that predates the app's four sends none of those.
    monkeypatch.setattr(auth, "get_current_user", lambda: dict(auth.get_user_by_id(uid)))
    assert client.post("/api/alert-settings", json={"alert_rating_threshold": 1, "alert_rating_floor": 4.6,
                                                    "alert_any_review": 1, "alert_resp_approved": 1,
                                                    "contacts": []}).status_code == 200
    r = models.get_restaurant(rid)
    assert (r.alert_food_waste, r.alert_ai_visibility_drop, r.alert_health_bypass_quiet) == (1, 1, 1)
    assert r.alert_extra_emails == "gm@x.test"


def test_each_client_now_has_the_others_switches(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    uid, h = _login(client, db_path, rid)
    assert client.post("/mobile/api/account/alert-settings", json=_ios_alert_body(
        alert_rating_threshold=True, alert_rating_floor=4.3, alert_any_review=True, alert_resp_approved=True),
        headers=h).status_code == 200
    s = client.get("/mobile/api/account", headers=h).get_json()["alerts"]["settings"]
    assert s["alert_rating_threshold"] is True and s["alert_rating_floor"] == 4.3
    assert s["alert_any_review"] is True and s["alert_resp_approved"] is True
    monkeypatch.setattr(auth, "get_current_user", lambda: dict(auth.get_user_by_id(uid)))
    assert client.post("/api/alert-settings", json={"alert_food_waste": 1, "alert_ai_visibility_drop": 1,
                                                    "alert_health_bypass_quiet": 1,
                                                    "alert_extra_emails": "chef@x.test, gm@x.test",
                                                    "contacts": []}).status_code == 200
    s = client.get("/api/alert-settings").get_json()["settings"]
    assert (s["alert_food_waste"], s["alert_ai_visibility_drop"], s["alert_health_bypass_quiet"]) == (1, 1, 1)
    assert s["alert_extra_emails"] == "chef@x.test,gm@x.test"
    # A floor that is not a star rating is named, never stored.
    r = client.post("/mobile/api/account/alert-settings", json=_ios_alert_body(alert_rating_floor=9),
                    headers=h)
    assert r.status_code == 400 and "between 1 and 5" in r.get_json()["error"]


def test_the_ios_alert_body_sends_the_web_switches():
    vm = open(os.path.join(IOS, "Features", "Account", "AccountViewModel.swift")).read()
    for key in ("alert_rating_threshold", "alert_rating_floor", "alert_any_review", "alert_resp_approved"):
        assert f'"{key}"' in vm, key
    web = open(os.path.join(ROOT, "templates", "dashboard.html")).read()
    for el in ("al-food-waste", "al-ai-visibility", "al-extra-emails", "al-health-bypass"):
        assert f'id="{el}"' in web, el


# ── #2 Auto-approve keeps the 4-star rule ───────────────────────────────────

def test_the_app_reads_and_sends_include_4star(client, db_path):
    rid = _restaurant(db_path, auto_approve_5star=1, auto_approve_4star=1)
    _uid, h = _login(client, db_path, rid)
    assert client.get("/mobile/api/account", headers=h).get_json()["reviews"]["auto_approve_4star"] is True
    assert client.post("/mobile/api/account/auto-approve", headers=h, json={
        "enabled": True, "paused": False, "daily_cap": 5, "earned": False, "include_4star": True}).status_code == 200
    assert models.get_restaurant(rid).auto_approve_4star == 1


def test_the_swift_auto_approve_body_carries_every_key_the_shared_body_reads():
    src = open(os.path.join(ROOT, "client_api.py")).read()
    body = src[src.index("def _do_auto_approve("):]
    body = body[:body.index("\ndef ", 10)]
    read = set(re.findall(r'\(data or \{\}\)\.get\("([a-z_0-9]+)"', body))
    assert read == {"daily_cap", "enabled", "paused", "include_4star", "earned"}, read
    vm = open(os.path.join(IOS, "Features", "Account", "AccountViewModel.swift")).read()
    decl = vm[vm.index("struct AutoApproveBody"):]
    decl = decl[:decl.index("}\n")]
    for key in read:
        assert f'"{key}"' in decl or re.search(rf"\b{key}\b", decl), key


# ── #70 Staff sign-in notice, nightly report, PIN events ────────────────────

def test_the_staff_sign_in_notice_has_a_mobile_twin_for_the_owner_only(client, db_path):
    rid = _restaurant(db_path)
    _uid, h = _login(client, db_path, rid)
    r = client.post("/mobile/api/account/staff-signin-notify", json={"enabled": True}, headers=h)
    assert r.status_code == 200 and r.get_json()["enabled"] is True
    assert models.get_restaurant(rid).staff_signin_notify == 1
    assert client.get("/mobile/api/account", headers=h).get_json()["account"]["staff_signin_notify"] is True
    _m, hm = _login(client, db_path, rid, "mgr.sam", role="manager")
    assert client.post("/mobile/api/account/staff-signin-notify", json={"enabled": False},
                       headers=hm).status_code == 403
    assert models.get_restaurant(rid).staff_signin_notify == 1


def test_the_nightly_report_switch_answers_with_its_state(client, db_path):
    rid = _restaurant(db_path)
    _uid, h = _login(client, db_path, rid)
    mid, _hm = _login(client, db_path, rid, "mgr.sam", role="manager")
    r = client.post(f"/mobile/api/account/team/{mid}/access", json={"nightly_report": False}, headers=h)
    assert r.status_code == 200 and r.get_json()["nightly_report"] is False
    team = client.get("/mobile/api/account/team", headers=h).get_json()["members"]
    assert [m["nightly_report"] for m in team if m["id"] == mid] == [False]


# ── #80 Sync now, RPOWER disconnect, every connection counted ───────────────

def test_sync_now_twins_use_the_web_bodies(client, db_path, monkeypatch):
    import scheduler
    calls = []
    monkeypatch.setattr(scheduler, "start_manual_pos_sync", lambda rid, who: calls.append(rid) or (7, False))
    rid = _restaurant(db_path)
    _uid, h = _login(client, db_path, rid)
    r = client.post("/mobile/api/connections/toast/sync", headers=h)
    assert r.status_code == 200 and r.get_json()["ok"] is False and calls == []
    assert client.post("/mobile/api/connections/rpower/sync", headers=h).status_code == 400
    assert client.post("/mobile/api/connections/nope/sync", headers=h).status_code == 404
    import toast
    monkeypatch.setattr(toast, "is_connected", lambda rid: True)
    r = client.post("/mobile/api/connections/toast/sync", headers=h)
    assert r.get_json()["ok"] is True and calls == [rid]


def test_rpower_disconnect_from_the_app_is_the_owners(client, db_path):
    rid = _restaurant(db_path, rpower_store_mid="mid-1", rpower_token="t", rpower_store_name="EJ's")
    _uid, h = _login(client, db_path, rid)
    _m, hm = _login(client, db_path, rid, "mgr.sam", role="manager")
    assert client.delete("/mobile/api/connections/rpower", headers=hm).status_code == 403
    assert models.get_restaurant(rid).rpower_store_mid == "mid-1"
    assert client.delete("/mobile/api/connections/rpower", headers=h).status_code == 200
    r = models.get_restaurant(rid)
    assert r.rpower_store_mid is None and r.rpower_token is None


def test_the_account_payload_lists_every_connection(client, db_path):
    rid = _restaurant(db_path, gmb_refresh_token="g", rpower_store_mid="m", ga4_property_id="123456789")
    _uid, h = _login(client, db_path, rid)
    conn = client.get("/mobile/api/account", headers=h).get_json()["connections"]
    assert conn["web_analytics"]["connected"] is True and conn["rpower"]["connected"] is True
    import account_health
    counted = account_health.connections(models.get_restaurant(rid))
    assert counted["connected"] == 3 and counted["total"] == 7


# ── #89 + security checkup: scored once, on the server ─────────────────────

def test_the_checkup_is_one_score_on_both_routes(client, db_path, monkeypatch):
    rid = _restaurant(db_path, two_fa_enabled=0, login_notify=1)
    uid, h = _login(client, db_path, rid)
    m = client.get("/mobile/api/account/security-summary", headers=h).get_json()
    monkeypatch.setattr(auth, "get_current_user", lambda: dict(auth.get_user_by_id(uid)))
    w = client.get("/api/account/security-summary").get_json()
    assert m["checkup"] == w["checkup"]
    ck = m["checkup"]
    assert [i["key"] for i in ck["items"]] == ["two_fa", "password", "backup_codes", "login_notify",
                                               "recovery_email", "devices"]
    assert ck["max"] == 100
    assert ck["score"] == sum(i["points"] for i in ck["items"] if i["earned"])
    by = {i["key"]: i for i in ck["items"]}
    assert by["login_notify"]["earned"] and not by["two_fa"]["earned"] and by["two_fa"]["fix_label"] == "Turn on"
    assert by["backup_codes"]["earned"] is False   # needs two-factor first (the web's rule)


def test_account_health_payload(client, db_path, monkeypatch):
    import value_delivered
    rid = _restaurant(db_path, module_reviews=1, module_labor=0, owner_phone="+13125550100", voice_notes="warm",
                      gmb_refresh_token="g", alert_1star=1, billing_status="past_due")
    uid, h = _login(client, db_path, rid)
    monkeypatch.setattr(value_delivered, "delivered", lambda rid, **k: {
        "monthly": 1200.0, "net_monthly": 950.0, "wins": 2, "worsened": {"count": 1}, "evaluated": 5,
        "in_flight": 1, "opportunity": 99999})
    d = client.get("/mobile/api/account/health", headers=h).get_json()
    assert d["ok"] and [i["key"] for i in d["items"]] == ["profile", "people", "integrations", "notifications",
                                                           "security", "subscription"]
    states = {i["key"]: i["state"] for i in d["items"]}
    assert states["subscription"] == "bad" and states["profile"] == "ok"
    assert d["fix"]["key"] in ("integrations", "notifications", "subscription")
    assert d["tone"] in ("good", "warn", "bad") and 0 <= d["score"] <= 100
    # The measured line is `delivered`, net — never the opportunity figure.
    assert d["measured"]["line"] == "Measured, net: $950/mo · 5 changes measured"
    assert "99,999" not in str(d) and "99999" not in d["measured"]["line"]
    feats = {f["key"]: f["on"] for f in d["features"]}
    assert feats["reviews"] is True and feats["intel"] is False
    # The web answers the same body.
    monkeypatch.setattr(auth, "get_current_user", lambda: dict(auth.get_user_by_id(uid)))
    w = client.get("/api/account/health").get_json()
    # The same score and items; only the app's billing words differ — it
    # never says "update your card" (re-audit 10/8/26, #15).
    sub = lambda items: [dict(i, sub=None) if i["key"] == "subscription" else i for i in items]  # noqa: E731
    assert w["score"] == d["score"] and sub(w["items"]) == sub(d["items"])


def test_a_teammates_health_leaves_billing_out_and_nothing_measured_is_not_zero(client, db_path, monkeypatch):
    import value_delivered
    rid = _restaurant(db_path)
    _login(client, db_path, rid)
    _m, hm = _login(client, db_path, rid, "mgr.sam", role="manager")
    monkeypatch.setattr(value_delivered, "delivered", lambda rid, **k: {
        "monthly": 0, "net_monthly": 0, "wins": 0, "worsened": {"count": 0}, "evaluated": 0, "in_flight": 0})
    d = client.get("/mobile/api/account/health", headers=hm).get_json()
    assert "subscription" not in [i["key"] for i in d["items"]]
    assert d["measured"]["line"] == "Nothing measured yet" and d["measured"]["net_monthly"] is None


# ── Matrix: the morning brief's hours, the referral row ────────────────────

def test_both_clients_offer_the_brief_hours_the_server_takes():
    import morning_brief
    assert morning_brief.LATEST_SEND_HOUR == 14
    web = open(os.path.join(ROOT, "templates", "dashboard.html")).read()
    assert "for (var i = 4; i <= 13; i++)" in web
    ios = open(os.path.join(IOS, "Features", "Account", "AccountAlertsDetailView.swift")).read()
    assert "ForEach(4...13" in ios


def test_the_referral_row_is_drawn_for_an_account_holder_only():
    web = open(os.path.join(ROOT, "templates", "dashboard.html")).read()
    i = web.index('aria-controls="ac-ref-fold"')
    assert "{% if _principal %}" in web[i - 300:i]
    ios = open(os.path.join(IOS, "Features", "Account", "AccountView.swift")).read()
    j = ios.index('"Refer a restaurant"')
    assert "isOwner" in ios[j - 400:j]
