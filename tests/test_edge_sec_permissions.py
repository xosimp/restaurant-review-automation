"""Edge cases on roles: what a manager, a teammate or a support login can
do that only the restaurant's principal (owner) should.

From the SEC edge audit (Permissions & Roles). permissions.py says a manager
"runs the floor without administering logins" and support "writes nothing";
test_permissions.py pins the role -> permission map itself. This file walks
the side doors around that map: the 2FA switches and backup codes, the
account email that doubles as the restaurant's owner_email, outbound
webhooks, POS credentials, review retention and auto-approve, a support
login writing through view-as, auth_bp's JSON posts that sit outside CSRF,
and routes that no module gate covers.

Tests pinning today's correct behaviour carry no marker. Tests asserting the
correct behaviour for a defect the audit confirmed are xfail(strict=True)
with the finding id, so a fix turns them into failures until the marker goes.
"""
import base64
import json
import os
import re
import subprocess
import sys
import tempfile

import pytest
from flask import Flask

import admin_routes
import auth
import auth_routes
import client_api
import mobile_api
import models
from auth import (create_session, create_user, get_session_user, init_auth,
                  upsert_membership)
from models import (Restaurant, create_restaurant, get_restaurant,
                    update_restaurant)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = os.path.join(ROOT, "templates")
CSRF = {"X-CSRF": "edge-csrf"}


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    import webhooks
    for mod in (models, auth, auth_routes, mobile_api, client_api, admin_routes, webhooks):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    init_auth(db_path=db_path)
    webhooks.init_webhooks(db_path=db_path)
    models.init_two_fa_backup_codes(db_path=db_path)
    import webhooks
    webhooks.init_webhooks(db_path=db_path)
    auth_routes._login_attempts.clear()
    yield
    auth_routes._login_attempts.clear()


def _app():
    import square_routes
    import toast_routes
    app = Flask(__name__, template_folder=TEMPLATES)
    app.secret_key = "test-secret"
    for bp in (auth_routes.auth_bp, client_api.client_bp, mobile_api.mobile_bp, admin_routes.admin_bp,
               toast_routes.toast_bp, square_routes.square_bp):
        app.register_blueprint(bp)
    return app


def _web(db_path, uid):
    """A browser client signed in as uid, carrying the CSRF double-submit
    pair (client_bp enforces it whenever hosted_dashboard has wired it)."""
    c = _app().test_client()
    c.set_cookie("session_token", create_session(uid, db_path=db_path))
    c.set_cookie("csrf_js", CSRF["X-CSRF"])
    return c


def _bearer(db_path, uid):
    return {"Authorization": f"Bearer {create_session(uid, device_type='ios', db_path=db_path)}"}


def _setup(db_path, two_fa=False, second_role="manager"):
    rid = create_restaurant(Restaurant(name="R", owner_email="owner@x.test"), db_path=db_path)
    owner = create_user(rid, "owner", "owner@x.test", "ownerpass1", db_path=db_path)
    upsert_membership(owner, rid, "client", db_path=db_path)
    other = create_user(rid, "mgr", "mgr@x.test", "mgrpass12", db_path=db_path)
    auth.set_user_role(other, second_role, db_path=db_path)
    upsert_membership(other, rid, second_role, db_path=db_path)
    if two_fa:
        update_restaurant(rid, {"two_fa_enabled": 1}, db_path=db_path)
    return rid, owner, other


def _events(db_path, rid):
    conn = models.get_conn(db_path)
    rows = [r["event_type"] for r in conn.execute(
        "SELECT event_type FROM activity_log WHERE restaurant_id=?", (rid,)).fetchall()]
    conn.close()
    return rows


# ── the restaurant's 2FA switches and backup codes ───────────────────────────

def test_the_owner_can_mint_backup_codes_and_turn_2fa_off(db_path):
    rid, owner, mgr = _setup(db_path, two_fa=True)
    r = _web(db_path, owner).post("/api/account/2fa/backup-codes", headers=CSRF).get_json()
    assert r["ok"] is True and len(r["backup_codes"]) == 10
    assert _web(db_path, owner).post("/api/account/2fa/disable", headers=CSRF).status_code == 200
    assert get_restaurant(rid, db_path=db_path).two_fa_enabled == 0


@pytest.mark.xfail(strict=True, reason="SEC-6: any console role can mint the restaurant's 2FA backup codes (mobile)")
def test_a_manager_is_refused_backup_code_generation_on_mobile(db_path):
    rid, owner, mgr = _setup(db_path, two_fa=True)
    c = _app().test_client()
    r = c.post("/mobile/api/account/2fa/backup-codes", headers=_bearer(db_path, mgr))
    assert r.status_code == 403
    assert not (r.get_json() or {}).get("backup_codes")


@pytest.mark.xfail(strict=True, reason="SEC-6: any console role can mint the restaurant's 2FA backup codes (web)")
def test_a_manager_is_refused_backup_code_generation_on_the_web(db_path):
    rid, owner, mgr = _setup(db_path, two_fa=True)
    r = _web(db_path, mgr).post("/api/account/2fa/backup-codes", headers=CSRF)
    assert r.status_code == 403


@pytest.mark.xfail(strict=True, reason="SEC-6: any console role can disable the restaurant's 2FA (mobile)")
def test_a_manager_is_refused_turning_2fa_off_on_mobile(db_path):
    rid, owner, mgr = _setup(db_path, two_fa=True)
    r = _app().test_client().post("/mobile/api/account/2fa/disable", headers=_bearer(db_path, mgr))
    assert r.status_code == 403
    assert get_restaurant(rid, db_path=db_path).two_fa_enabled == 1


@pytest.mark.xfail(strict=True, reason="SEC-6: any console role can disable the restaurant's 2FA (web account route)")
def test_a_manager_is_refused_turning_2fa_off_on_the_web(db_path):
    rid, owner, mgr = _setup(db_path, two_fa=True)
    r = _web(db_path, mgr).post("/api/account/2fa/disable", headers=CSRF)
    assert r.status_code == 403
    assert get_restaurant(rid, db_path=db_path).two_fa_enabled == 1


@pytest.mark.xfail(strict=True, reason="SEC-6/SEC-21: /api/toggle-2fa has no permission check — a manager turns the owner's 2FA off")
def test_a_manager_is_refused_the_toggle_2fa_switch(db_path):
    rid, owner, mgr = _setup(db_path, two_fa=True)
    r = _web(db_path, mgr).post("/api/toggle-2fa", json={"enabled": False})
    assert r.status_code == 403
    assert get_restaurant(rid, db_path=db_path).two_fa_enabled == 1


@pytest.mark.xfail(strict=True, reason="SEC-21: /api/toggle-login-notify has no permission check — a manager silences login alerts")
def test_a_manager_is_refused_the_login_alert_switch(db_path):
    rid, owner, mgr = _setup(db_path)
    update_restaurant(rid, {"login_notify": 1}, db_path=db_path)
    r = _web(db_path, mgr).post("/api/toggle-login-notify", json={"enabled": False})
    assert r.status_code == 403
    assert get_restaurant(rid, db_path=db_path).login_notify == 1


@pytest.mark.xfail(strict=True, reason="SEC-21: /api/toggle-staff-signin-notify has no permission check")
def test_a_manager_is_refused_the_staff_sign_in_alert_switch(db_path):
    rid, owner, mgr = _setup(db_path)
    update_restaurant(rid, {"staff_signin_notify": 1}, db_path=db_path)
    r = _web(db_path, mgr).post("/api/toggle-staff-signin-notify", json={"enabled": False})
    assert r.status_code == 403
    assert get_restaurant(rid, db_path=db_path).staff_signin_notify == 1


def test_turning_2fa_off_from_the_app_is_recorded_in_account_activity(db_path):
    rid, owner, mgr = _setup(db_path, two_fa=True)
    _app().test_client().post("/mobile/api/account/2fa/disable", headers=_bearer(db_path, owner))
    assert "two_fa_disabled" in _events(db_path, rid)


@pytest.mark.xfail(strict=True, reason="SEC-6/SEC-21: the web /api/toggle-2fa change leaves no account-activity entry")
def test_turning_2fa_off_from_the_web_toggle_is_recorded_in_account_activity(db_path):
    rid, owner, mgr = _setup(db_path, two_fa=True)
    assert _web(db_path, owner).post("/api/toggle-2fa", json={"enabled": False}).status_code == 200
    assert any("two_fa" in e for e in _events(db_path, rid))


@pytest.mark.xfail(strict=True, reason="SEC-5/SEC-6: a manager mints backup codes, swaps the pending uid and becomes the owner")
def test_a_manager_cannot_become_the_owner_with_a_self_minted_backup_code(db_path):
    rid, owner, mgr = _setup(db_path, two_fa=True)
    c = _app().test_client()
    codes = (c.post("/mobile/api/account/2fa/backup-codes", headers=_bearer(db_path, mgr)).get_json() or {}).get("backup_codes") or ["000000"]
    pending = c.post("/mobile/api/login", json={"username": "mgr", "password": "mgrpass12"}).get_json()["pending_token"]
    rid_s, _uid, secret = base64.urlsafe_b64decode(pending).decode().split(":", 2)
    forged = base64.urlsafe_b64encode(f"{rid_s}:{owner}:{secret}".encode()).decode()
    v = c.post("/mobile/api/verify-2fa", json={"pending_token": forged, "code": codes[0]}).get_json() or {}
    assert not (v.get("token") and get_session_user(v["token"], db_path=db_path)["id"] == owner)


# ── a login's own email is not the restaurant's owner_email ─────────────────

def test_the_owners_own_email_change_moves_the_restaurant_contact(db_path):
    rid, owner, mgr = _setup(db_path)
    r = _web(db_path, owner).post("/api/update-email", json={"new_email": "owner-new@x.test", "current_password": "ownerpass1"})
    assert r.get_json()["ok"] is True
    assert get_restaurant(rid, db_path=db_path).owner_email == "owner-new@x.test"


@pytest.mark.xfail(strict=True, reason="SEC-13: a manager's web email change rewrites restaurants.owner_email (the 2FA/alert channel)")
def test_a_managers_web_email_change_leaves_the_owner_email_alone(db_path):
    rid, owner, mgr = _setup(db_path)
    r = _web(db_path, mgr).post("/api/update-email", json={"new_email": "mgr-new@x.test", "current_password": "mgrpass12"})
    assert r.get_json()["ok"] is True
    assert get_restaurant(rid, db_path=db_path).owner_email == "owner@x.test"


@pytest.mark.xfail(strict=True, reason="SEC-13: a manager's in-app email change rewrites restaurants.owner_email")
def test_a_managers_mobile_email_change_leaves_the_owner_email_alone(db_path):
    rid, owner, mgr = _setup(db_path)
    r = _app().test_client().post("/mobile/api/account/update-email", headers=_bearer(db_path, mgr),
                                  json={"new_email": "mgr-new@x.test", "current_password": "mgrpass12"})
    assert r.get_json()["ok"] is True
    assert get_restaurant(rid, db_path=db_path).owner_email == "owner@x.test"


@pytest.mark.xfail(strict=True, reason="SEC-13: a manager's email change detaches their location from the owner's group")
def test_a_managers_email_change_leaves_the_location_in_the_owners_group(db_path):
    l1 = create_restaurant(Restaurant(name="L1", owner_email="boss@x.test", location_group="G"), db_path=db_path)
    l2 = create_restaurant(Restaurant(name="L2", owner_email="boss@x.test", location_group="G"), db_path=db_path)
    boss = create_user(l1, "boss", "boss@x.test", "bosspass1", db_path=db_path)
    auth.set_user_role(boss, "owner", db_path=db_path)
    upsert_membership(boss, l1, "owner", db_path=db_path)
    mgr2 = create_user(l2, "mgr2", "mgr2@x.test", "mgrpass12", db_path=db_path)
    auth.set_user_role(mgr2, "manager", db_path=db_path)
    upsert_membership(mgr2, l2, "manager", db_path=db_path)
    _web(db_path, mgr2).post("/api/update-email", json={"new_email": "mgr2-new@x.test", "current_password": "mgrpass12"})
    r = _web(db_path, boss).post("/api/switch-location", json={"restaurant_id": l2}, headers=CSRF)
    assert r.status_code == 200 and r.get_json()["ok"] is True


# ── administration a teammate should not reach ──────────────────────────────

@pytest.fixture
def no_ssrf_lookup(monkeypatch):
    import webhooks
    monkeypatch.setattr(webhooks, "_validate_webhook_url", lambda url: None)


def test_the_owner_can_register_an_outbound_webhook(db_path, no_ssrf_lookup):
    rid, owner, mgr = _setup(db_path)
    r = _web(db_path, owner).post("/api/webhook", json={"url": "https://hooks.example.com/x"}, headers=CSRF)
    assert r.get_json()["ok"] is True and r.get_json()["secret"]


@pytest.mark.xfail(strict=True, reason="SEC-24: any console role registers an outbound webhook and receives its HMAC secret")
@pytest.mark.parametrize("role", ["manager", "member"])
def test_a_teammate_cannot_register_an_outbound_webhook(db_path, no_ssrf_lookup, role):
    rid, owner, mate = _setup(db_path, second_role=role)
    r = _web(db_path, mate).post("/api/webhook", json={"url": "https://hooks.example.com/x"}, headers=CSRF)
    assert r.status_code == 403
    assert not (r.get_json() or {}).get("secret")


@pytest.mark.xfail(strict=True, reason="SEC-24: any console role overwrites the Toast credentials from the app")
def test_a_manager_cannot_overwrite_toast_credentials_from_the_app(db_path, monkeypatch):
    import toast
    monkeypatch.setattr(toast, "get_toast_token", lambda rid, *a, **k: "tok")
    rid, owner, mgr = _setup(db_path)
    update_restaurant(rid, {"toast_client_id": "good-id"}, db_path=db_path)
    r = _app().test_client().post("/mobile/api/connections/toast", headers=_bearer(db_path, mgr),
                                  json={"toast_client_id": "evil", "toast_client_secret": "s", "toast_restaurant_guid": "g"})
    assert r.status_code == 403
    assert get_restaurant(rid, db_path=db_path).toast_client_id == "good-id"


@pytest.mark.xfail(strict=True, reason="SEC-24: any console role overwrites the Toast credentials on the web")
def test_a_manager_cannot_overwrite_toast_credentials_on_the_web(db_path, monkeypatch):
    import toast
    monkeypatch.setattr(toast, "test_credentials", lambda *a, **k: {"ok": True})
    rid, owner, mgr = _setup(db_path)
    update_restaurant(rid, {"toast_client_id": "good-id"}, db_path=db_path)
    r = _web(db_path, mgr).post("/api/toast/save", headers=CSRF,
                                json={"client_id": "evil", "client_secret": "s", "restaurant_guid": "g"})
    assert r.status_code == 403
    assert get_restaurant(rid, db_path=db_path).toast_client_id == "good-id"


@pytest.mark.xfail(strict=True, reason="SEC-24: any console role overwrites the Square credentials from the app")
def test_a_manager_cannot_overwrite_square_credentials_from_the_app(db_path, monkeypatch):
    import square
    monkeypatch.setattr(square, "test_credentials", lambda *a, **k: {"ok": True, "location_name": "X"})
    rid, owner, mgr = _setup(db_path)
    update_restaurant(rid, {"square_access_token": "good-token"}, db_path=db_path)
    r = _app().test_client().post("/mobile/api/connections/square", headers=_bearer(db_path, mgr),
                                  json={"square_access_token": "evil", "square_location_id": "L"})
    assert r.status_code == 403
    assert get_restaurant(rid, db_path=db_path).square_access_token == "good-token"


def test_the_owner_can_set_review_retention(db_path):
    rid, owner, mgr = _setup(db_path)
    r = _web(db_path, owner).post("/api/account-settings/data-retention", json={"months": 12}, headers=CSRF)
    assert r.status_code == 200
    assert get_restaurant(rid, db_path=db_path).data_retention_months == 12


@pytest.mark.xfail(strict=True, reason="SEC-24: any console role can set review retention to 6 months (soft-delete)")
@pytest.mark.parametrize("surface", ["web", "mobile"])
def test_a_manager_cannot_shorten_review_retention(db_path, surface):
    rid, owner, mgr = _setup(db_path)
    if surface == "web":
        r = _web(db_path, mgr).post("/api/account-settings/data-retention", json={"months": 6}, headers=CSRF)
    else:
        r = _app().test_client().post("/mobile/api/account/data-retention", json={"months": 6},
                                      headers=_bearer(db_path, mgr))
    assert r.status_code == 403
    assert int(get_restaurant(rid, db_path=db_path).data_retention_months or 0) == 0


@pytest.mark.xfail(strict=True, reason="SEC-24: any console role can switch on auto-approve (public replies under the brand)")
@pytest.mark.parametrize("surface", ["web", "mobile"])
def test_a_manager_cannot_switch_on_auto_approve(db_path, surface):
    rid, owner, mgr = _setup(db_path)
    body = {"enabled": True, "daily_cap": 50}
    if surface == "web":
        r = _web(db_path, mgr).post("/api/account-settings/auto-approve", json=body, headers=CSRF)
    else:
        r = _app().test_client().post("/mobile/api/account/auto-approve", json=body, headers=_bearer(db_path, mgr))
    assert r.status_code == 403
    assert not get_restaurant(rid, db_path=db_path).auto_approve_5star


# ── support writing through view-as ──────────────────────────────────────────

def _support_via_view_as(db_path):
    rid, owner, mgr = _setup(db_path)
    hq = create_restaurant(Restaurant(name="Cavnar HQ", owner_email="hq@cavnar.test"), db_path=db_path)
    sup = create_user(hq, "support1", "sup@cavnar.test", "suppass12", db_path=db_path)
    auth.set_user_role(sup, "support", db_path=db_path)
    c = _app().test_client()
    c.set_cookie("session_token", create_session(sup, db_path=db_path))
    c.set_cookie("csrf_js", CSRF["X-CSRF"])
    r = c.get(f"/admin/view-as/{rid}")
    assert r.status_code == 302
    viewing = get_session_user(c.get_cookie("session_token").value, db_path=db_path)
    assert viewing["id"] == owner and viewing["device_type"] == "admin-view-as"
    return rid, c


def test_support_can_open_view_as_and_read_as_the_client(db_path):
    rid, c = _support_via_view_as(db_path)
    assert c.get("/api/account/team").status_code == 200


@pytest.mark.xfail(strict=True, reason="SEC-12: a view-as session opened by read-only support can perform client writes")
def test_a_view_as_session_opened_by_support_is_refused_every_write(db_path):
    rid, c = _support_via_view_as(db_path)
    update_restaurant(rid, {"two_fa_enabled": 1}, db_path=db_path)
    r1 = c.post("/api/toggle-2fa", json={"enabled": False}, headers=CSRF)
    r2 = c.post("/api/account-settings/data-retention", json={"months": 6}, headers=CSRF)
    assert (r1.status_code, r2.status_code) == (403, 403)
    assert get_restaurant(rid, db_path=db_path).two_fa_enabled == 1


# ── routes no module gate covers ─────────────────────────────────────────────

@pytest.mark.xfail(strict=True, reason="SEC-24: /api/inv-trend is not in _MODULE_PREFIXES, so a manager reads food-cost trends")
def test_the_inventory_trend_route_belongs_to_the_food_cost_module():
    assert auth._required_module("/api/inv-trend") == "inventory"


def _all_rules():
    import clover_routes
    import rpower_routes
    import social_routes
    import strategy_routes
    app = _app()
    for bp in (strategy_routes.strategy_bp, strategy_routes.strategy_mobile_bp, social_routes.social_bp,
               clover_routes.clover_bp, rpower_routes.rpower_bp):
        app.register_blueprint(bp)
    return sorted({r.rule for r in app.url_map.iter_rules()
                   if r.rule.startswith("/api/") or r.rule.startswith("/mobile/api/")})


@pytest.mark.xfail(strict=True, reason="SEC-24: no explicit allow-list of deliberately ungated /api and /mobile/api routes exists (180+ unmapped)")
def test_every_api_rule_is_module_mapped_or_listed_as_deliberately_ungated():
    # The audit's mitigation: a route is either under a module prefix or on
    # an explicit, reviewed list. The list's name here is this test's
    # proposal; the rule it enforces is the audit's.
    ungated = getattr(auth, "_UNGATED_PREFIXES", None)
    assert ungated is not None, "auth has no explicit list of deliberately ungated routes"
    unmapped = [rule for rule in _all_rules()
                if not auth._required_module(rule) and not any(rule.startswith(p) for p in ungated)]
    assert unmapped == []


# ── auth_bp's JSON posts and CSRF, against the real app wiring ───────────────

_CSRF_SCRIPT = r'''
import json, os, re, socket, sqlite3, sys
vol = sys.argv[1]
sqlite3.connect(os.path.join(vol, "reviews.db")).close()
os.environ.update(RAILWAY_VOLUME_MOUNT_PATH=vol, RUN_SCHEDULER_IN_WEB="0", ANTHROPIC_API_KEY="",
                  RESEND_API_KEY="", TWILIO_AUTH_TOKEN="", STRIPE_SECRET_KEY="", SECRET_KEY="test-secret",
                  CAVNAR_PIN_PEPPER="test-pepper", HIBP_DISABLED="1", ADMIN_REQUIRE_2FA="0",
                  GOOGLE_PLACES_API_KEY="", PERPLEXITY_API_KEY="", OPENAI_API_KEY="")
os.environ.pop("RAILWAY_ENVIRONMENT", None); os.environ.pop("RAILWAY_PROJECT_ID", None)
def _blocked(*a, **k):
    raise OSError("network blocked in test subprocess")
socket.create_connection = _blocked
socket.socket.connect = lambda self, *a, **k: _blocked()
import hosted_dashboard as h
import auth, models
from models import Restaurant
rid = models.create_restaurant(Restaurant(name="Csrf Co", owner_email="csrf-owner@x.test", module_reviews=1))
uid = auth.create_user(rid, "csrfowner", "csrf-owner@x.test", "correct-horse-battery", is_admin=False)
c = h.app.test_client()
page = c.get("/login").get_data(as_text=True)
token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
r = c.post("/login", data={"username": "csrfowner", "password": "correct-horse-battery", "csrf_token": token})
assert r.status_code in (302, 303), r.status_code
out = {}
for path, body in json.loads(sys.argv[2]):
    out[path] = c.post(path, json=body).status_code      # no X-CSRF header
print("RESULT " + json.dumps(out))
'''

_AUTH_JSON_POSTS = [
    ("/api/toggle-2fa", {"enabled": False}),
    ("/api/toggle-login-notify", {"enabled": False}),
    ("/api/toggle-staff-signin-notify", {"enabled": False}),
    ("/api/sessions/revoke-others", {}),
    ("/auth/google/disconnect", {}),
    ("/api/update-email", {"new_email": "x@y.test", "current_password": "wrong"}),
    ("/api/change-password", {"current": "wrong", "new_password": "whatever12"}),
]


@pytest.fixture(scope="module")
def csrf_statuses():
    vol = tempfile.mkdtemp(prefix="cavnar-sec-csrf-")
    out = subprocess.run([sys.executable, "-c", _CSRF_SCRIPT, vol, json.dumps(_AUTH_JSON_POSTS)],
                         cwd=ROOT, capture_output=True, text=True, timeout=180)
    line = [l for l in out.stdout.splitlines() if l.startswith("RESULT ")]
    assert out.returncode == 0 and line, out.stdout[-1500:] + "\n" + out.stderr[-2500:]
    return json.loads(line[-1][len("RESULT "):])


@pytest.mark.xfail(strict=True, reason="SEC-21: auth_bp's cookie-authenticated JSON posts are exempt from CSRF")
@pytest.mark.parametrize("path", [p for p, _ in _AUTH_JSON_POSTS])
def test_every_session_json_post_under_auth_requires_the_csrf_header(csrf_statuses, path):
    assert csrf_statuses[path] == 403
