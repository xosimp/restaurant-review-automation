"""Edge cases on the front door: login, 2FA, password reset and sessions.

These come from the SEC edge audit (Authentication & Sessions). The happy
paths are covered in test_auth_routes.py / test_security.py; this file is
the set of things an attacker or an unlucky owner does next:

  - edits the user id inside a 2FA pending token,
  - rotates X-Forwarded-For to reset every per-address throttle,
  - guesses the 6-digit in-app reset code from several addresses,
  - completes a password reset after a freeze and expects to get back in,
  - resets a password and expects the attacker's session to end,
  - follows a login link whose ?next= points off-site,
  - signs in as owner and manager the same morning with 2FA on,
  - keeps using a session switched into a location that was since sold.

A test that asserts the correct behaviour for a defect the audit confirmed
is marked xfail(strict=True) with the finding id, so it turns into a failure
the day the defect is fixed and the marker has to come off with the fix.
"""
import base64
import inspect
import os

import pytest
from flask import Flask

import auth
import auth_routes
import client_api
import mobile_api
import models
import security
from auth import (create_session, create_user, get_session_user, init_auth,
                  upsert_membership)
from models import (Restaurant, create_restaurant, get_restaurant,
                    update_restaurant)

TEMPLATES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    """Every module that bound get_conn at import (CLAUDE.md, bound
    imports) is pointed at this test's database."""
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, auth_routes, mobile_api, client_api):
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
    """Capture every 2FA / reset code the routes try to deliver."""
    import emails
    box = {"2fa": [], "reset_code": [], "reset_link": []}
    monkeypatch.setattr(emails, "send_2fa_code",
                        lambda to, name, code, owner=None: box["2fa"].append((to, code)))
    monkeypatch.setattr(emails, "send_password_reset_code_email",
                        lambda to, code: box["reset_code"].append((to, code)) or True)
    monkeypatch.setattr(emails, "send_password_reset_email",
                        lambda to, url: box["reset_link"].append((to, url)) or True)
    return box


def _app(proxy=False):
    app = Flask(__name__, template_folder=TEMPLATES)
    app.secret_key = "test-secret"
    for bp in (auth_routes.auth_bp, mobile_api.mobile_bp, client_api.client_bp):
        app.register_blueprint(bp)

    @app.route("/_whoami_ip")
    def _whoami_ip():
        return auth_routes._get_client_ip()

    if proxy:
        auth.install_proxy_fix(app)
    return app


@pytest.fixture
def client():
    return _app().test_client()


@pytest.fixture
def proxied():
    """The app as production runs it: behind ProxyFix trusting one hop."""
    return _app(proxy=True).test_client()


def _setup(db_path, two_fa=False):
    rid = create_restaurant(Restaurant(name="R", owner_email="owner@x.test"), db_path=db_path)
    owner = create_user(rid, "owner", "owner@x.test", "ownerpass1", db_path=db_path)
    upsert_membership(owner, rid, "client", db_path=db_path)
    mgr = create_user(rid, "mgr", "mgr@x.test", "mgrpass12", db_path=db_path)
    conn = models.get_conn(db_path)
    conn.execute("UPDATE users SET role='manager' WHERE id=?", (mgr,))
    conn.commit(); conn.close()
    upsert_membership(mgr, rid, "manager", db_path=db_path)
    if two_fa:
        update_restaurant(rid, {"two_fa_enabled": 1}, db_path=db_path)
    return rid, owner, mgr


def _swap_uid(pending_token, new_uid):
    rid_s, _uid, secret = base64.urlsafe_b64decode(pending_token).decode().split(":", 2)
    return base64.urlsafe_b64encode(f"{rid_s}:{new_uid}:{secret}".encode()).decode()


def _login_form(client, username, password, next_url=None):
    client.get("/login")
    csrf = client.get_cookie("csrf_token").value
    path = "/login" + (f"?next={next_url}" if next_url is not None else "")
    return client.post(path, data={"username": username, "password": password, "csrf_token": csrf})


def _pending_from_html(resp):
    html = resp.get_data(as_text=True)
    marker = 'name="pending_token" value="'
    start = html.index(marker) + len(marker)
    return html[start:html.index('"', start)]


def _web_reset(client, token, password):
    client.get(f"/reset-password/{token}")
    csrf = client.get_cookie("csrf_token").value
    return client.post(f"/reset-password/{token}",
                       data={"password": password, "confirm": password, "csrf_token": csrf})


def _must_reset(db_path, uid):
    conn = models.get_conn(db_path)
    v = conn.execute("SELECT must_reset_password FROM users WHERE id=?", (uid,)).fetchone()[0]
    conn.close()
    return v


# ── 2FA pending token: the user id inside it ─────────────────────────────────

def test_the_unedited_pending_token_signs_the_manager_in_as_the_manager(client, db_path):
    rid, owner, mgr = _setup(db_path, two_fa=True)
    r = client.post("/mobile/api/login", json={"username": "mgr", "password": "mgrpass12"}).get_json()
    code = get_restaurant(rid, db_path=db_path).two_fa_code
    v = client.post("/mobile/api/verify-2fa", json={"pending_token": r["pending_token"], "code": code}).get_json()
    assert v["ok"] is True
    assert get_session_user(v["token"], db_path=db_path)["id"] == mgr


def test_a_mobile_pending_token_edited_to_name_the_owner_is_refused(client, db_path):
    rid, owner, mgr = _setup(db_path, two_fa=True)
    r = client.post("/mobile/api/login", json={"username": "mgr", "password": "mgrpass12"}).get_json()
    code = get_restaurant(rid, db_path=db_path).two_fa_code
    v = client.post("/mobile/api/verify-2fa",
                    json={"pending_token": _swap_uid(r["pending_token"], owner), "code": code})
    assert v.status_code == 401
    assert not (v.get_json() or {}).get("token")


def test_a_web_pending_token_edited_to_name_the_owner_is_refused(client, db_path):
    rid, owner, mgr = _setup(db_path, two_fa=True)
    pending = _pending_from_html(_login_form(client, "mgr", "mgrpass12"))
    code = get_restaurant(rid, db_path=db_path).two_fa_code
    csrf = client.get_cookie("csrf_token").value
    resp = client.post("/verify-2fa", data={"pending_token": _swap_uid(pending, owner), "code": code,
                                            "next_url": "/", "csrf_token": csrf})
    tok = client.get_cookie("session_token")
    assert tok is None or get_session_user(tok.value, db_path=db_path)["id"] != owner


def test_a_swapped_pending_token_cannot_reach_a_login_locked_by_this_wasnt_me(client, db_path):
    rid, owner, mgr = _setup(db_path, two_fa=True)
    report = auth.create_login_report(owner, None, db_path=db_path)
    auth.consume_login_report(report, db_path=db_path)          # owner's password is now burned
    r = client.post("/mobile/api/login", json={"username": "mgr", "password": "mgrpass12"}).get_json()
    code = get_restaurant(rid, db_path=db_path).two_fa_code
    v = client.post("/mobile/api/verify-2fa",
                    json={"pending_token": _swap_uid(r["pending_token"], owner), "code": code}).get_json()
    assert not v.get("ok")


def test_resend_code_works_with_the_token_the_login_page_issued(client, db_path, sent):
    rid, owner, mgr = _setup(db_path, two_fa=True)
    pending = _pending_from_html(_login_form(client, "owner", "ownerpass1"))
    r = client.post("/resend-2fa", json={"pending_token": pending}).get_json()
    assert r["ok"] is True


# ── throttles and the address they key on ───────────────────────────────────

def test_the_client_address_is_the_one_the_proxy_vouches_for(proxied):
    r = proxied.get("/_whoami_ip", headers={"X-Forwarded-For": "6.6.6.6, 203.0.113.9"})
    assert r.get_data(as_text=True) == "203.0.113.9"


def test_the_mobile_reset_throttle_stops_one_address_guessing_codes(proxied, db_path, sent):
    _setup(db_path)
    proxied.post("/mobile/api/forgot-password", json={"email": "owner@x.test"},
                 headers={"X-Forwarded-For": "198.51.100.1"})
    statuses = [proxied.post("/mobile/api/reset-password",
                             json={"email": "owner@x.test", "code": f"{i:06d}", "new_password": "attackerpw1"},
                             headers={"X-Forwarded-For": "203.0.113.9"}).status_code
                for i in range(12)]
    assert 429 in statuses


def test_a_rotating_forwarded_for_does_not_reset_the_mobile_reset_throttle(proxied, db_path, sent):
    _setup(db_path)
    statuses = [proxied.post("/mobile/api/reset-password",
                             json={"email": "owner@x.test", "code": f"{i:06d}", "new_password": "attackerpw1"},
                             headers={"X-Forwarded-For": f"10.9.0.{i}, 203.0.113.9"}).status_code
                for i in range(12)]
    assert 429 in statuses


def test_a_rotating_forwarded_for_does_not_reset_the_2fa_verify_throttle(proxied, db_path):
    rid, owner, mgr = _setup(db_path, two_fa=True)
    r = proxied.post("/mobile/api/login", json={"username": "owner", "password": "ownerpass1"},
                     headers={"X-Forwarded-For": "203.0.113.9"}).get_json()
    statuses = [proxied.post("/mobile/api/verify-2fa", json={"pending_token": r["pending_token"], "code": "000000"},
                             headers={"X-Forwarded-For": f"10.8.0.{i}, 203.0.113.9"}).status_code
                for i in range(12)]
    assert 429 in statuses


def test_forgot_password_from_one_address_is_throttled(proxied, db_path, sent):
    _setup(db_path)
    statuses = [proxied.post("/mobile/api/forgot-password", json={"email": "owner@x.test"},
                             headers={"X-Forwarded-For": "203.0.113.9"}).status_code for _ in range(10)]
    assert 429 in statuses
    assert len(sent["reset_code"]) <= security.IP_MAX_ANON


def test_a_rotating_forwarded_for_does_not_reset_the_forgot_password_throttle(proxied, db_path, sent):
    _setup(db_path)
    for i in range(12):
        proxied.post("/mobile/api/forgot-password", json={"email": "owner@x.test"},
                     headers={"X-Forwarded-For": f"10.7.0.{i}, 203.0.113.9"})
    assert len(sent["reset_code"]) <= security.IP_MAX_ANON


def test_a_successful_login_does_not_wipe_the_address_budget(client, db_path, sent):
    _setup(db_path)
    env = {"REMOTE_ADDR": "203.0.113.50"}
    for i in range(security.IP_MAX_ANON - 1):
        client.post("/mobile/api/reset-password", environ_base=env,
                    json={"email": "owner@x.test", "code": f"{i:06d}", "new_password": "attackerpw1"})
    assert client.post("/mobile/api/login", environ_base=env,
                       json={"username": "mgr", "password": "mgrpass12"}).status_code == 200
    client.post("/mobile/api/reset-password", environ_base=env,
                json={"email": "owner@x.test", "code": "999999", "new_password": "attackerpw1"})
    r = client.post("/mobile/api/reset-password", environ_base=env,
                    json={"email": "owner@x.test", "code": "999998", "new_password": "attackerpw1"})
    assert r.status_code == 429


# ── the in-app 6-digit reset code ────────────────────────────────────────────

def test_five_wrong_codes_for_one_email_burn_the_code_even_from_five_addresses(client, db_path, sent):
    rid, owner, mgr = _setup(db_path)
    client.post("/mobile/api/forgot-password", json={"email": "owner@x.test"},
                environ_base={"REMOTE_ADDR": "198.51.100.1"})
    real = sent["reset_code"][-1][1]
    wrong = [c for c in (f"{i:06d}" for i in range(10)) if c != real][:5]
    for i, c in enumerate(wrong):
        client.post("/mobile/api/reset-password", environ_base={"REMOTE_ADDR": f"198.51.100.{10 + i}"},
                    json={"email": "owner@x.test", "code": c, "new_password": "attackerpw1"})
    r = client.post("/mobile/api/reset-password", environ_base={"REMOTE_ADDR": "198.51.100.99"},
                    json={"email": "owner@x.test", "code": real, "new_password": "attackerpw1"})
    assert r.status_code != 200
    assert auth.verify_password("owner", "ownerpass1", db_path=db_path)


def test_the_mobile_reset_code_is_not_stored_verbatim(client, db_path, sent):
    rid, owner, mgr = _setup(db_path)
    client.post("/mobile/api/forgot-password", json={"email": "owner@x.test"})
    code = sent["reset_code"][-1][1]
    conn = models.get_conn(db_path)
    stored = conn.execute("SELECT reset_token FROM users WHERE id=?", (owner,)).fetchone()[0]
    conn.close()
    assert stored and stored != code


def test_a_wrong_reset_code_is_refused_before_the_breach_lookup(client, db_path, monkeypatch, sent):
    _setup(db_path)
    calls = []
    monkeypatch.setattr(security, "password_pwned", lambda pw: calls.append(pw) or False)
    r = client.post("/mobile/api/reset-password",
                    json={"email": "owner@x.test", "code": "123456", "new_password": "attackerpw1"})
    assert r.status_code == 400
    assert calls == []


def test_the_correct_mobile_reset_code_sets_the_new_password(client, db_path, sent):
    rid, owner, mgr = _setup(db_path)
    client.post("/mobile/api/forgot-password", json={"email": "owner@x.test"})
    code = sent["reset_code"][-1][1]
    r = client.post("/mobile/api/reset-password",
                    json={"email": "owner@x.test", "code": code, "new_password": "brandnewpw9"})
    assert r.get_json()["ok"] is True
    assert auth.verify_password("owner", "brandnewpw9", db_path=db_path)


_CODE_SITES = [
    (auth_routes, "login"), (auth_routes, "resend_2fa"), (auth_routes, "send_2fa_test"),
    (mobile_api, "mobile_login"), (mobile_api, "mobile_forgot_password"),
    (auth, "start_recovery_email"),
]


def test_security_codes_are_drawn_from_secrets_not_random():
    offenders = [f"{m.__name__}.{fn}" for m, fn in _CODE_SITES
                 if "random.randint" in inspect.getsource(getattr(m, fn))
                 or "_rnd.randint" in inspect.getsource(getattr(m, fn))]
    src = open(os.path.join(os.path.dirname(TEMPLATES), "mobile_api.py")).read()
    # mobile_api's send-2fa-test twin draws from the module-level `random`.
    start = src.find("def mobile_send_2fa_test")
    if start != -1 and "random.randint" in src[start:src.find("\n@mobile_bp.route", start)]:
        offenders.append("mobile_api.mobile_send_2fa_test")
    assert offenders == []


# ── must_reset_password: freeze and "This wasn't me" must be recoverable ─────

def test_after_a_freeze_a_completed_web_reset_lets_the_owner_sign_in(client, db_path):
    rid, owner, mgr = _setup(db_path)
    security.freeze_restaurant(rid, db_path=db_path)
    token = models.create_reset_token("owner@x.test", db_path=db_path)
    assert _web_reset(client, token, "brandnewpw9").status_code == 302
    r = client.post("/mobile/api/login", json={"username": "owner", "password": "brandnewpw9"})
    assert r.status_code == 200 and r.get_json()["ok"] is True


def test_after_this_wasnt_me_a_completed_mobile_reset_lets_the_owner_sign_in(client, db_path, sent):
    rid, owner, mgr = _setup(db_path)
    auth.consume_login_report(auth.create_login_report(owner, None, db_path=db_path), db_path=db_path)
    client.post("/mobile/api/forgot-password", json={"email": "owner@x.test"})
    code = sent["reset_code"][-1][1]
    assert client.post("/mobile/api/reset-password",
                       json={"email": "owner@x.test", "code": code, "new_password": "brandnewpw9"}).get_json()["ok"]
    assert _must_reset(db_path, owner) == 0
    r = client.post("/mobile/api/login", json={"username": "owner", "password": "brandnewpw9"})
    assert r.status_code == 200


def test_a_frozen_login_is_refused_until_it_resets(client, db_path):
    rid, owner, mgr = _setup(db_path)
    security.freeze_restaurant(rid, db_path=db_path)
    r = client.post("/mobile/api/login", json={"username": "owner", "password": "ownerpass1"})
    assert r.status_code == 403 and r.get_json()["password_reset_required"] is True


def test_the_not_me_link_does_not_lock_the_account_on_a_plain_get(client, db_path):
    rid, owner, mgr = _setup(db_path)
    live = create_session(owner, db_path=db_path)
    report = auth.create_login_report(owner, live, db_path=db_path)
    client.get(f"/auth/not-me/{report}")
    assert get_session_user(live, db_path=db_path) is not None
    assert _must_reset(db_path, owner) == 0


# ── a reset or a password change ends the intruder's session ─────────────────

def test_a_web_reset_ends_every_existing_session(client, db_path):
    rid, owner, mgr = _setup(db_path)
    attacker = create_session(owner, device_type="ios", db_path=db_path)
    token = models.create_reset_token("owner@x.test", db_path=db_path)
    assert _web_reset(client, token, "brandnewpw9").status_code == 302
    assert get_session_user(attacker, db_path=db_path) is None


def test_a_mobile_reset_ends_every_existing_session(client, db_path, sent):
    rid, owner, mgr = _setup(db_path)
    attacker = create_session(owner, device_type="ios", db_path=db_path)
    client.post("/mobile/api/forgot-password", json={"email": "owner@x.test"})
    code = sent["reset_code"][-1][1]
    assert client.post("/mobile/api/reset-password",
                       json={"email": "owner@x.test", "code": code, "new_password": "brandnewpw9"}).get_json()["ok"]
    assert get_session_user(attacker, db_path=db_path) is None


def test_a_web_password_change_ends_every_other_session_but_this_one(client, db_path):
    rid, owner, mgr = _setup(db_path)
    attacker = create_session(owner, device_type="ios", db_path=db_path)
    mine = create_session(owner, db_path=db_path)
    client.set_cookie("session_token", mine)
    r = client.post("/api/change-password", json={"current": "ownerpass1", "new_password": "brandnewpw9"})
    assert r.get_json()["ok"] is True
    assert get_session_user(mine, db_path=db_path) is not None
    assert get_session_user(attacker, db_path=db_path) is None


def test_a_mobile_password_change_ends_every_other_session_but_this_one(client, db_path):
    rid, owner, mgr = _setup(db_path)
    attacker = create_session(owner, db_path=db_path)
    mine = create_session(owner, device_type="ios", db_path=db_path)
    r = client.post("/mobile/api/account/change-password", headers={"Authorization": f"Bearer {mine}"},
                    json={"current": "ownerpass1", "new_password": "brandnewpw9"})
    assert r.get_json()["ok"] is True
    assert get_session_user(mine, db_path=db_path) is not None
    assert get_session_user(attacker, db_path=db_path) is None


# ── ?next= after login and 2FA ───────────────────────────────────────────────

def test_a_relative_next_is_honoured_after_login(client, db_path):
    _setup(db_path)
    r = _login_form(client, "owner", "ownerpass1", next_url="/reviews")
    assert r.status_code == 302 and r.headers["Location"].endswith("/reviews")


@pytest.mark.xfail(strict=True, reason="SEC-22: an absolute ?next= after login is an open redirect")
def test_an_absolute_next_is_ignored_after_login(client, db_path):
    _setup(db_path)
    r = _login_form(client, "owner", "ownerpass1", next_url="https://evil.example/phish")
    assert r.status_code == 302
    assert "evil.example" not in r.headers["Location"]


@pytest.mark.xfail(strict=True, reason="SEC-22: a protocol-relative ?next= after login is an open redirect")
def test_a_protocol_relative_next_is_ignored_after_login(client, db_path):
    _setup(db_path)
    r = _login_form(client, "owner", "ownerpass1", next_url="//evil.example/phish")
    assert r.status_code == 302
    assert "evil.example" not in r.headers["Location"]


@pytest.mark.xfail(strict=True, reason="SEC-22: an absolute next_url after 2FA is an open redirect")
def test_an_absolute_next_url_is_ignored_after_2fa(client, db_path):
    rid, owner, mgr = _setup(db_path, two_fa=True)
    pending = _pending_from_html(_login_form(client, "owner", "ownerpass1"))
    csrf = client.get_cookie("csrf_token").value
    r = client.post("/verify-2fa", data={"pending_token": pending, "code": get_restaurant(rid, db_path=db_path).two_fa_code,
                                         "next_url": "https://evil.example/phish", "csrf_token": csrf})
    assert r.status_code == 302
    assert client.get_cookie("session_token") is not None      # the sign-in itself worked
    assert "evil.example" not in r.headers["Location"]


# ── one 2FA slot per restaurant ──────────────────────────────────────────────

@pytest.mark.xfail(strict=True, reason="SEC-20: 2FA is one slot per restaurant; a second login's challenge clobbers the first")
def test_two_logins_at_one_restaurant_can_complete_2fa_concurrently(client, db_path, sent):
    rid, owner, mgr = _setup(db_path, two_fa=True)
    first = client.post("/mobile/api/login", json={"username": "owner", "password": "ownerpass1"}).get_json()
    owner_code = sent["2fa"][-1][1]
    client.post("/mobile/api/login", json={"username": "mgr", "password": "mgrpass12"})
    v = client.post("/mobile/api/verify-2fa", json={"pending_token": first["pending_token"], "code": owner_code})
    assert v.status_code == 200 and v.get_json()["ok"] is True


@pytest.mark.xfail(strict=True, reason="SEC-20: pressing Send test code overwrites a sign-in's pending 2FA code")
def test_sending_a_2fa_test_code_does_not_break_a_sign_in_in_progress(client, db_path, sent):
    rid, owner, mgr = _setup(db_path, two_fa=True)
    first = client.post("/mobile/api/login", json={"username": "owner", "password": "ownerpass1"}).get_json()
    owner_code = sent["2fa"][-1][1]
    other = _app().test_client()
    other.set_cookie("session_token", create_session(mgr, db_path=db_path))
    other.post("/api/send-2fa-test", json={"method": "email"})
    v = client.post("/mobile/api/verify-2fa", json={"pending_token": first["pending_token"], "code": owner_code})
    assert v.status_code == 200


@pytest.mark.xfail(strict=True, reason="SEC-20/SEC-39: the emailed 2FA code is stored in plaintext on the restaurants row")
def test_the_2fa_code_is_not_stored_verbatim(client, db_path, sent):
    rid, owner, mgr = _setup(db_path, two_fa=True)
    client.post("/mobile/api/login", json={"username": "owner", "password": "ownerpass1"})
    code = sent["2fa"][-1][1]
    assert get_restaurant(rid, db_path=db_path).two_fa_code != code


# ── an owner session switched into a location ────────────────────────────────

def _group(db_path):
    l1 = create_restaurant(Restaurant(name="L1", owner_email="boss@x.test", location_group="G"), db_path=db_path)
    l2 = create_restaurant(Restaurant(name="L2", owner_email="boss@x.test", location_group="G"), db_path=db_path)
    boss = create_user(l1, "boss", "boss@x.test", "bosspass1", db_path=db_path)
    auth.set_user_role(boss, "owner", db_path=db_path)
    upsert_membership(boss, l1, "owner", db_path=db_path)
    mgr2 = create_user(l2, "mgr2", "mgr2@x.test", "mgrpass12", db_path=db_path)
    auth.set_user_role(mgr2, "manager", db_path=db_path)
    upsert_membership(mgr2, l2, "manager", db_path=db_path)
    return l1, l2, boss


def test_a_session_switched_into_a_group_location_acts_there(client, db_path):
    l1, l2, boss = _group(db_path)
    tok = create_session(boss, db_path=db_path)
    client.set_cookie("session_token", tok)
    client.set_cookie("csrf_js", "edge-csrf")        # client_bp's double-submit pair, when wired
    assert client.post("/api/switch-location", json={"restaurant_id": l2},
                       headers={"X-CSRF": "edge-csrf"}).get_json()["ok"] is True
    assert get_session_user(tok, db_path=db_path)["restaurant_id"] == l2


def test_a_switched_session_stops_acting_at_a_location_once_it_leaves_the_group(client, db_path):
    l1, l2, boss = _group(db_path)
    tok = create_session(boss, db_path=db_path)
    client.set_cookie("session_token", tok)
    client.set_cookie("csrf_js", "edge-csrf")        # client_bp's double-submit pair, when wired
    assert client.post("/api/switch-location", json={"restaurant_id": l2},
                       headers={"X-CSRF": "edge-csrf"}).get_json()["ok"] is True
    update_restaurant(l2, {"location_group": "SoldToSomeoneElse", "owner_email": "newowner@y.test"}, db_path=db_path)
    assert get_session_user(tok, db_path=db_path)["restaurant_id"] != l2
    team = client.get("/api/account/team").get_json() or {}
    assert "mgr2" not in [m.get("username") for m in team.get("members", [])]


# ── reflected parameters ─────────────────────────────────────────────────────

def test_the_google_callback_still_reports_a_failed_connection(client, db_path):
    rid, owner, mgr = _setup(db_path)
    client.set_cookie("session_token", create_session(owner, db_path=db_path))
    body = client.get("/auth/google/callback?error=access_denied").get_data(as_text=True)
    assert "Connection failed" in body and "gmb:'error'" in body


def test_the_google_callback_never_reflects_the_error_parameter_unescaped(client, db_path):
    rid, owner, mgr = _setup(db_path)
    client.set_cookie("session_token", create_session(owner, db_path=db_path))
    body = client.get("/auth/google/callback?error=x'});alert(document.domain);//").get_data(as_text=True)
    assert "'});alert(document.domain)" not in body
    body = client.get("/auth/google/callback?error=</script><script>alert(1)</script>").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in body


# ── malformed JSON on the unauthenticated mobile endpoints ───────────────────

@pytest.mark.xfail(strict=True, reason="SEC-32: /mobile/api/login 500s on a JSON body that is a string or an array")
@pytest.mark.parametrize("body", ['"x"', "[1]"])
def test_the_mobile_login_answers_400_to_a_json_body_that_is_not_an_object(client, db_path, body):
    _setup(db_path)
    r = client.post("/mobile/api/login", data=body, content_type="application/json")
    assert r.status_code == 400


@pytest.mark.xfail(strict=True, reason="SEC-32: /mobile/api/verify-2fa 500s on a JSON body that is a string or an array")
@pytest.mark.parametrize("body", ['"x"', "[1]"])
def test_the_mobile_2fa_verify_answers_400_to_a_json_body_that_is_not_an_object(client, db_path, body):
    _setup(db_path)
    r = client.post("/mobile/api/verify-2fa", data=body, content_type="application/json")
    assert r.status_code == 400


# ── side-effecting GETs, timing, stored tokens ───────────────────────────────

@pytest.mark.xfail(strict=True, reason="SEC-34: GET /logout deletes the session, so any page can log a user out")
def test_a_plain_get_to_logout_does_not_end_the_session(client, db_path):
    rid, owner, mgr = _setup(db_path)
    tok = create_session(owner, db_path=db_path)
    client.set_cookie("session_token", tok)
    client.get("/logout")
    assert get_session_user(tok, db_path=db_path) is not None


@pytest.mark.xfail(strict=True, reason="SEC-35: verify_password returns before hashing for an unknown username (timing enumeration)")
def test_verify_password_pays_for_a_hash_even_for_an_unknown_username(db_path, monkeypatch):
    _setup(db_path)
    calls = []
    real = auth.check_password_hash
    monkeypatch.setattr(auth, "check_password_hash", lambda h, p: calls.append(1) or real(h, p))
    assert auth.verify_password("no-such-user", "whatever", db_path=db_path) is None
    assert calls == [1]


def test_verify_password_hashes_for_a_known_username(db_path, monkeypatch):
    _setup(db_path)
    calls = []
    real = auth.check_password_hash
    monkeypatch.setattr(auth, "check_password_hash", lambda h, p: calls.append(1) or real(h, p))
    assert auth.verify_password("owner", "wrong", db_path=db_path) is None
    assert calls == [1]


def test_login_report_tokens_are_not_stored_verbatim(db_path):
    rid, owner, mgr = _setup(db_path)
    token = auth.create_login_report(owner, None, db_path=db_path)
    conn = models.get_conn(db_path)
    stored = [r[0] for r in conn.execute("SELECT token FROM login_reports").fetchall()]
    conn.close()
    assert token not in stored


def test_the_not_me_page_escapes_the_account_email(client, db_path, sent):
    rid = create_restaurant(Restaurant(name="R", owner_email="o@x.test"), db_path=db_path)
    uid = create_user(rid, "odd", "<img src=x onerror=alert(1)>@x.test", "pw123456", db_path=db_path)
    body = client.get(f"/auth/not-me/{auth.create_login_report(uid, None, db_path=db_path)}").get_data(as_text=True)
    assert "<img src=x" not in body


# ── SSO: an unverified provider email must not link an account ───────────────

class _Resp:
    def __init__(self, payload):
        self._p = payload
        self.ok = True
        self.status_code = 200

    def json(self):
        return self._p


def _google_sso(client, monkeypatch, verified):
    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp({"access_token": "at"}))
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(
        {"email": "owner@x.test", "id": "g-123", "verified_email": verified}))
    client.set_cookie("g_sso_state", "st8")
    return client.get("/auth/google-sso/callback?state=st8&code=c0de")


def test_google_sso_with_a_verified_email_signs_the_owner_in(client, db_path, monkeypatch):
    _setup(db_path)
    r = _google_sso(client, monkeypatch, verified=True)
    assert r.status_code == 302 and r.headers["Location"].endswith("/")
    assert client.get_cookie("session_token") is not None


@pytest.mark.xfail(strict=True, reason="SEC-38: Google SSO links and signs in on an email match without reading verified_email")
def test_google_sso_refuses_an_unverified_provider_email(client, db_path, monkeypatch):
    rid, owner, mgr = _setup(db_path)
    r = _google_sso(client, monkeypatch, verified=False)
    assert client.get_cookie("session_token") is None
    conn = models.get_conn(db_path)
    linked = conn.execute("SELECT google_id FROM users WHERE id=?", (owner,)).fetchone()[0]
    conn.close()
    assert not linked


@pytest.mark.xfail(strict=True, reason="SEC-38: Sign in with Apple links on an email match without reading email_verified")
def test_apple_signin_refuses_an_unverified_provider_email(client, db_path, monkeypatch):
    rid, owner, mgr = _setup(db_path)
    monkeypatch.setattr(mobile_api, "_verify_apple_identity_token",
                        lambda tok, bundle: {"sub": "apple-1", "email": "owner@x.test", "email_verified": "false"})
    r = client.post("/mobile/api/apple-signin", json={"identity_token": "t"})
    assert r.status_code in (401, 403)
    conn = models.get_conn(db_path)
    linked = conn.execute("SELECT apple_user_id FROM users WHERE id=?", (owner,)).fetchone()[0]
    conn.close()
    assert not linked
