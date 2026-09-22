"""Admin Console edge cases from the security audit (SEC.md, "Admin Console"
appendix items 6, 7, 8, 9, 11, 12 and findings SEC-15, SEC-23, SEC-28,
SEC-30, SEC-31).

What these protect:
- Support tooling acts on the right person. View-as and "reset password by
  restaurant" must pick the restaurant's principal (owner/client) login, never
  a staff PIN identity or whichever row SQLite happens to return first.
- An admin save changes only what it was sent: a partial client-settings
  payload must not reset billing, modules, alerts or the owner email.
- The public status page's admin writes sit behind the same controls as the
  rest of /admin (2FA gate, CSRF, JSON-only bodies, the admin_events audit).
- A GET never swaps an admin's session for a view-as session.
- An admin password reset ends the user's existing sessions and lifts the
  forced-reset flag, so a frozen account can sign in again.
- Referral mail from Cavnar's domain is rate-limited and never carries
  caller-authored HTML.
- A competitor refresh while one is already running joins that job rather
  than starting another paid background thread.

Admins are authenticated for real: an is_admin user plus an
auth.create_session cookie, so admin_required runs end to end. Every test
sends the CSRF double-submit pair so the file behaves the same whether or not
another test has imported hosted_dashboard (which wires csrf_protect onto
admin_bp for the rest of the process). No email, network or AI call is made:
the Resend SDK is stubbed to capture payloads, and threads are counted, never
started.

Tests that pass pin behaviour that works today. Tests marked xfail(strict)
assert the CORRECT behaviour for a confirmed defect and will flip to a failure
the day it is fixed, so the marker is removed with the fix.
"""
import pytest
from flask import Flask

import admin_routes
import auth
import auth_routes
import client_api
import models
import status_manager
import status_routes
from auth import create_session, create_user, init_auth, upsert_membership, get_session_user
from models import Restaurant, create_restaurant, update_restaurant, get_restaurant

CSRF = "edge-sec-admin-csrf"


# ── fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _redirect_db(db_path, monkeypatch):
    """Every module that bound get_conn at import time points at the test
    database; status_manager opens its own sqlite3 connection on its bound
    DB_PATH, so that is redirected too."""
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, auth_routes, admin_routes, client_api):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    monkeypatch.setattr(status_manager, "DB_PATH", db_path)
    init_auth(db_path=db_path)
    from models import init_email_log
    init_email_log(db_path=db_path)
    auth_routes._login_attempts.clear()


@pytest.fixture
def app():
    flask_app = Flask(__name__, template_folder="../templates")
    flask_app.secret_key = "edge-sec-admin"
    flask_app.register_blueprint(admin_routes.admin_bp)
    flask_app.register_blueprint(auth_routes.auth_bp)
    flask_app.register_blueprint(status_routes.status_bp)
    flask_app.register_blueprint(client_api.client_bp)
    return flask_app


def _client(app, token):
    c = app.test_client()
    c.set_cookie("session_token", token)
    c.set_cookie("csrf_js", CSRF)
    return c


def _post(c, url, **kw):
    headers = kw.pop("headers", {})
    headers.setdefault("X-CSRF", CSRF)
    return c.post(url, headers=headers, **kw)


def _admin(db_path):
    """Will: an is_admin login on Cavnar's own restaurant row."""
    home = create_restaurant(Restaurant(name="Cavnar HQ", owner_email="will@cavnar.test"), db_path=db_path)
    uid = create_user(home, "will", "will@cavnar.test", "Admin-pass-2026", is_admin=True, db_path=db_path)
    return home, uid


def _restaurant(db_path, name="Client Grill", owner_email="owner@client.test"):
    return create_restaurant(Restaurant(name=name, owner_email=owner_email), db_path=db_path)


def _owner(db_path, rid, username="owner", email="owner@client.test"):
    uid = create_user(rid, username, email, "Owner-pass-2026", db_path=db_path)
    upsert_membership(uid, rid, "client", db_path=db_path)
    return uid


def _staff_identity(db_path, rid, name="dana"):
    """A staff PIN identity, built the way auth.claim_staff_signup builds one:
    a users row with an @staff.invalid email and an employee membership."""
    username = "%s.%d" % (name, rid)
    uid = create_user(rid, username, "%s@staff.invalid" % username, "Unused-random-pw-123", db_path=db_path)
    upsert_membership(uid, rid, "employee", employee_name=name.title(), db_path=db_path)
    return uid


def _row(db_path, sql, args=()):
    conn = models.get_conn(db_path)
    try:
        return conn.execute(sql, args).fetchone()
    finally:
        conn.close()


def _rows(db_path, sql, args=()):
    conn = models.get_conn(db_path)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


# ── SEC-28 / items 6, 7: view-as and reset-by-restaurant pick the principal ──

def test_view_as_acts_as_the_owner_when_the_owner_is_the_only_login(app, db_path):
    _, admin_uid = _admin(db_path)
    rid = _restaurant(db_path)
    owner = _owner(db_path, rid)
    c = _client(app, create_session(admin_uid, db_path=db_path))
    r = c.get("/admin/view-as/%d" % rid)
    assert r.status_code == 302
    row = _row(db_path, "SELECT user_id FROM sessions WHERE device_type='admin-view-as'")
    assert row is not None and row["user_id"] == owner


@pytest.mark.xfail(strict=True, reason="SEC-28: view-as picks users LIMIT 1 unordered, "
                                       "so an older staff PIN identity is impersonated instead of the owner")
def test_view_as_targets_the_owner_even_when_a_staff_identity_was_created_first(app, db_path):
    _, admin_uid = _admin(db_path)
    rid = _restaurant(db_path)
    staff = _staff_identity(db_path, rid)            # lower id: first row LIMIT 1 sees
    owner = _owner(db_path, rid)
    assert staff < owner
    c = _client(app, create_session(admin_uid, db_path=db_path))
    c.get("/admin/view-as/%d" % rid)
    row = _row(db_path, "SELECT user_id FROM sessions WHERE device_type='admin-view-as'")
    assert row is not None and row["user_id"] == owner


@pytest.mark.xfail(strict=True, reason="SEC-28: view-as picks users LIMIT 1 unordered, "
                                       "so an older manager login is impersonated instead of the owner")
def test_view_as_targets_the_owner_even_when_a_manager_login_was_created_first(app, db_path):
    _, admin_uid = _admin(db_path)
    rid = _restaurant(db_path)
    mgr = create_user(rid, "mgr", "mgr@client.test", "Manager-pass-2026", db_path=db_path)
    conn = models.get_conn(db_path)
    conn.execute("UPDATE users SET role='manager' WHERE id=?", (mgr,))
    conn.commit(); conn.close()
    upsert_membership(mgr, rid, "manager", db_path=db_path)
    owner = _owner(db_path, rid)
    c = _client(app, create_session(admin_uid, db_path=db_path))
    c.get("/admin/view-as/%d" % rid)
    row = _row(db_path, "SELECT user_id FROM sessions WHERE device_type='admin-view-as'")
    assert row is not None and row["user_id"] == owner


@pytest.mark.xfail(strict=True, reason="SEC-28: reset-by-restaurant picks users LIMIT 1 unordered, "
                                       "so it targets an older staff PIN identity instead of the owner")
def test_reset_by_restaurant_targets_the_owner_and_not_a_staff_identity(app, db_path, monkeypatch):
    """Which login the route chooses, isolated from what the reset then does:
    reset_password_by_restaurant hands the chosen id to the module-level
    reset_password, which is replaced with a recorder here (see the next
    test for why the real hand-off cannot be exercised today)."""
    from flask import jsonify
    chosen = []
    monkeypatch.setattr(admin_routes, "reset_password",
                        lambda user_id, current_user=None: (chosen.append(user_id), jsonify(ok=True))[1])
    _, admin_uid = _admin(db_path)
    rid = _restaurant(db_path)
    staff = _staff_identity(db_path, rid)            # lower id: first row LIMIT 1 sees
    owner = _owner(db_path, rid)
    assert staff < owner
    c = _client(app, create_session(admin_uid, db_path=db_path))
    r = _post(c, "/admin/reset-password-by-restaurant/%d" % rid, json={"password": "Fresh-owner-pass-2026"})
    assert r.get_json()["ok"] is True
    assert chosen == [owner]


@pytest.mark.xfail(strict=True, reason="NEW (not in SEC.md): reset_password_by_restaurant calls the "
                                       "admin_required-wrapped reset_password with current_user=, so every call "
                                       "raises TypeError (multiple values for 'current_user') and 500s")
def test_reset_by_restaurant_resets_the_owners_password(app, db_path):
    _, admin_uid = _admin(db_path)
    rid = _restaurant(db_path)
    owner = _owner(db_path, rid)
    before = _row(db_path, "SELECT password_hash FROM users WHERE id=?", (owner,))["password_hash"]
    c = _client(app, create_session(admin_uid, db_path=db_path))
    r = _post(c, "/admin/reset-password-by-restaurant/%d" % rid, json={"password": "Fresh-owner-pass-2026"})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    from werkzeug.security import check_password_hash
    after = _row(db_path, "SELECT password_hash FROM users WHERE id=?", (owner,))["password_hash"]
    assert after != before and check_password_hash(after, "Fresh-owner-pass-2026")


# ── SEC-30 / item 8: partial client-settings payload ─────────────────────────

def _configured_restaurant(db_path):
    rid = _restaurant(db_path, owner_email="owner@client.test")
    update_restaurant(rid, {
        "billing_status": "active",
        "module_reviews": 1, "module_labor": 1, "module_inventory": 1, "module_marketing": 1,
        "alert_1star": 1, "alert_2star": 1, "urgent_via_email": 1,
        "digest_enabled": 1, "reviews_live": 1,
        "hourly_rate": 19.5,
    }, db_path=db_path)
    return rid


def test_a_full_settings_payload_still_saves_every_field_it_carries(app, db_path):
    _, admin_uid = _admin(db_path)
    rid = _configured_restaurant(db_path)
    c = _client(app, create_session(admin_uid, db_path=db_path))
    r = _post(c, "/admin/client-settings/%d" % rid, json={
        "name": "Renamed Grill", "owner_email": "owner@client.test", "billing_status": "active",
        "module_reviews": 1, "module_labor": 1, "module_inventory": 1, "module_marketing": 0,
        "alert_1star": True, "hourly_rate": 21,
    })
    assert r.get_json()["ok"] is True
    rest = get_restaurant(rid, db_path=db_path)
    assert rest.name == "Renamed Grill"
    assert rest.module_marketing == 0 and rest.module_labor == 1
    assert rest.billing_status == "active" and rest.hourly_rate == 21.0


@pytest.mark.xfail(strict=True, reason="SEC-30: save_client_settings writes every key from data.get(key, default), "
                                       "so a one-field payload resets billing, modules, alerts and owner_email")
def test_saving_client_settings_with_one_field_changes_only_that_field(app, db_path):
    _, admin_uid = _admin(db_path)
    rid = _configured_restaurant(db_path)
    c = _client(app, create_session(admin_uid, db_path=db_path))
    r = _post(c, "/admin/client-settings/%d" % rid, json={"name": "Renamed Grill"})
    assert r.get_json()["ok"] is True
    rest = get_restaurant(rid, db_path=db_path)
    assert rest.name == "Renamed Grill"
    assert rest.billing_status == "active"
    assert (rest.module_labor, rest.module_inventory, rest.module_marketing) == (1, 1, 1)
    assert (rest.alert_1star, rest.alert_2star, rest.urgent_via_email) == (1, 1, 1)
    assert rest.reviews_live == 1
    assert rest.hourly_rate == 19.5
    assert rest.owner_email == "owner@client.test"


# ── SEC-23 / item 9: status-page admin endpoints ────────────────────────────

def _incident_count(db_path):
    return _row(db_path, "SELECT COUNT(*) AS n FROM status_incidents")["n"]


def test_status_admin_write_is_refused_without_a_session_or_for_a_client_login(app, db_path):
    rid = _restaurant(db_path)
    owner = _owner(db_path, rid)
    anon = app.test_client()
    assert anon.post("/admin/status/incident", json={"title": "Outage"}).status_code == 403
    c = _client(app, create_session(owner, db_path=db_path))
    assert _post(c, "/admin/status/incident", json={"title": "Outage"}).status_code == 403
    assert _post(c, "/admin/status/update", json={"service_key": "dashboard", "status": "outage"}).status_code == 403
    assert _incident_count(db_path) == 0


def test_status_admin_write_works_for_a_real_admin_with_a_json_body(app, db_path):
    _, admin_uid = _admin(db_path)
    c = _client(app, create_session(admin_uid, db_path=db_path))
    r = _post(c, "/admin/status/incident", json={"title": "Slow drafts", "body": "Investigating"})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert _incident_count(db_path) == 1


def test_an_admin_blueprint_write_is_recorded_in_admin_events(app, db_path):
    """The control for the status-page gap below: /admin writes on admin_bp
    are audited before they run."""
    _, admin_uid = _admin(db_path)
    rid = _restaurant(db_path)
    c = _client(app, create_session(admin_uid, db_path=db_path))
    _post(c, "/admin/client-settings/%d" % rid, json={"name": "Audited Grill", "owner_email": "owner@client.test"})
    rows = _rows(db_path, "SELECT event_type FROM admin_events WHERE event_type LIKE 'admin_write:%'")
    assert any("save_client_settings" in r["event_type"] for r in rows)


@pytest.mark.xfail(strict=True, reason="SEC-23: status_routes._require_admin only checks is_admin; "
                                       "ADMIN_REQUIRE_2FA is never consulted")
def test_status_admin_write_is_refused_for_an_admin_without_two_factor_when_required(app, db_path, monkeypatch):
    home, admin_uid = _admin(db_path)
    monkeypatch.setenv("ADMIN_REQUIRE_2FA", "1")
    assert not get_restaurant(home, db_path=db_path).two_fa_enabled
    c = _client(app, create_session(admin_uid, db_path=db_path))
    # The same admin is held at the door of an admin_bp write...
    held = _post(c, "/admin/client-settings/%d" % home, json={"name": "x"})
    assert held.status_code == 403 and held.get_json().get("two_factor_required") is True
    # ...and must be held at the status page's door too.
    r = _post(c, "/admin/status/incident", json={"title": "Fake outage"})
    assert r.status_code == 403
    assert _incident_count(db_path) == 0


@pytest.mark.xfail(strict=True, reason="SEC-23: status admin endpoints use request.get_json(force=True), "
                                       "so a text/plain (cross-site form) body is parsed as JSON")
def test_status_admin_write_does_not_parse_a_text_plain_body_as_json(app, db_path):
    _, admin_uid = _admin(db_path)
    c = _client(app, create_session(admin_uid, db_path=db_path))
    r = _post(c, "/admin/status/incident", data='{"title": "Forged outage"}',
              content_type="text/plain")
    assert r.status_code >= 400
    assert _incident_count(db_path) == 0


@pytest.mark.xfail(strict=True, reason="SEC-23: status admin writes live on status_bp, "
                                       "so admin_bp's _audit_admin_write never records them")
def test_status_admin_write_is_recorded_in_admin_events(app, db_path):
    _, admin_uid = _admin(db_path)
    c = _client(app, create_session(admin_uid, db_path=db_path))
    r = _post(c, "/admin/status/incident", json={"title": "Slow drafts"})
    assert r.status_code == 200
    rows = _rows(db_path, "SELECT event_type, summary FROM admin_events")
    assert any("/admin/status/incident" in (r["summary"] or "") or "incident" in (r["event_type"] or "")
               for r in rows)


@pytest.mark.xfail(strict=True, reason="SEC-23: status_bp is exempt from csrf_protect in hosted_dashboard.py "
                                       "although it carries admin POST routes")
def test_the_status_blueprint_is_csrf_protected_where_the_app_wires_csrf():
    """Source-level: hosted_dashboard.py is unsafe to import here (real DB
    init, background threads), so the wiring is read from its source — every
    blueprint handed to csrf_protect at boot."""
    import ast
    import os
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hosted_dashboard.py")
    tree = ast.parse(open(path).read())
    protected = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.For) and isinstance(node.iter, ast.Tuple):
            body_calls = [n for n in ast.walk(node) if isinstance(n, ast.Call)
                          and getattr(n.func, "id", "") == "csrf_protect"]
            if body_calls:
                protected |= {e.id for e in node.iter.elts if isinstance(e, ast.Name)}
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "csrf_protect":
            protected |= {a.id for a in node.args if isinstance(a, ast.Name)}
    assert "admin_bp" in protected, "parser sanity: admin_bp is protected at boot"
    assert "status_bp" in protected


# ── SEC-34 / item 11: view-as over GET ──────────────────────────────────────

@pytest.mark.xfail(strict=True, reason="SEC-34: /admin/view-as/<rid> is a GET that mints an impersonation "
                                       "session and replaces the admin's cookie (cross-site top-level GET)")
def test_a_get_to_view_as_does_not_switch_the_admins_session(app, db_path):
    _, admin_uid = _admin(db_path)
    rid = _restaurant(db_path)
    _owner(db_path, rid)
    admin_token = create_session(admin_uid, db_path=db_path)
    c = _client(app, admin_token)
    c.get("/admin/view-as/%d" % rid)
    assert _row(db_path, "SELECT COUNT(*) AS n FROM sessions WHERE device_type='admin-view-as'")["n"] == 0
    assert c.get_cookie("session_token").value == admin_token


# ── SEC-7 / SEC-8 / item 12: admin reset ────────────────────────────────────

def test_admin_reset_sets_a_password_the_user_can_sign_in_with(app, db_path):
    _, admin_uid = _admin(db_path)
    rid = _restaurant(db_path)
    owner = _owner(db_path, rid)
    c = _client(app, create_session(admin_uid, db_path=db_path))
    r = _post(c, "/admin/reset-password/%d" % owner, json={"password": "Fresh-owner-pass-2026"})
    assert r.get_json()["ok"] is True
    from werkzeug.security import check_password_hash
    h = _row(db_path, "SELECT password_hash FROM users WHERE id=?", (owner,))["password_hash"]
    assert check_password_hash(h, "Fresh-owner-pass-2026")


@pytest.mark.xfail(strict=True, reason="SEC-8: admin reset_password never touches sessions, "
                                       "so an attacker's existing session survives the reset")
def test_admin_reset_ends_every_existing_session_of_that_user(app, db_path):
    _, admin_uid = _admin(db_path)
    rid = _restaurant(db_path)
    owner = _owner(db_path, rid)
    web = create_session(owner, db_path=db_path)
    ios = create_session(owner, device_type="ios", db_path=db_path)
    assert get_session_user(web, db_path=db_path) and get_session_user(ios, db_path=db_path)
    c = _client(app, create_session(admin_uid, db_path=db_path))
    assert _post(c, "/admin/reset-password/%d" % owner, json={"password": "Fresh-owner-pass-2026"}).get_json()["ok"]
    assert get_session_user(web, db_path=db_path) is None
    assert get_session_user(ios, db_path=db_path) is None


@pytest.mark.xfail(strict=True, reason="SEC-7: admin reset_password never clears must_reset_password, "
                                       "so a frozen login stays locked after the admin resets it")
def test_admin_reset_clears_the_forced_reset_flag(app, db_path):
    _, admin_uid = _admin(db_path)
    rid = _restaurant(db_path)
    owner = _owner(db_path, rid)
    conn = models.get_conn(db_path)
    conn.execute("UPDATE users SET must_reset_password=1 WHERE id=?", (owner,))
    conn.commit(); conn.close()
    c = _client(app, create_session(admin_uid, db_path=db_path))
    assert _post(c, "/admin/reset-password/%d" % owner, json={"password": "Fresh-owner-pass-2026"}).get_json()["ok"]
    assert _row(db_path, "SELECT must_reset_password FROM users WHERE id=?", (owner,))["must_reset_password"] == 0


# ── SEC-15: send-referral ───────────────────────────────────────────────────

@pytest.fixture
def captured_mail(monkeypatch):
    """send_referral calls resend.Emails.send directly; capture instead."""
    sent = []
    import resend
    monkeypatch.setattr(resend.Emails, "send", staticmethod(lambda payload: sent.append(payload) or {"id": "x"}),
                        raising=False)
    return sent


def test_a_referral_sends_the_invite_and_notifies_will(app, db_path, captured_mail):
    rid = _restaurant(db_path)
    owner = _owner(db_path, rid)
    c = _client(app, create_session(owner, db_path=db_path))
    r = _post(c, "/api/send-referral", json={"name": "Pat", "email": "pat@friend.test", "note": "Try it"})
    assert r.get_json()["ok"] is True
    assert [p["to"] for p in captured_mail][0] == ["pat@friend.test"]
    assert len(captured_mail) == 2


@pytest.mark.xfail(strict=True, reason="SEC-15: send-referral has no rate limit (the '10 per hour' comment "
                                       "is unimplemented) — an open relay from will@")
def test_the_eleventh_referral_in_an_hour_from_one_restaurant_is_refused(app, db_path, captured_mail):
    rid = _restaurant(db_path)
    owner = _owner(db_path, rid)
    c = _client(app, create_session(owner, db_path=db_path))
    for i in range(10):
        r = _post(c, "/api/send-referral", json={"name": "P%d" % i, "email": "p%d@friend.test" % i})
        assert r.get_json()["ok"] is True
    sent_before = len(captured_mail)
    r = _post(c, "/api/send-referral", json={"name": "P10", "email": "p10@friend.test"})
    assert r.status_code == 429
    assert len(captured_mail) == sent_before


@pytest.mark.xfail(strict=True, reason="SEC-15: the referral note is interpolated raw into the email HTML")
def test_the_referral_note_is_html_escaped_in_every_email(app, db_path, captured_mail):
    rid = _restaurant(db_path)
    owner = _owner(db_path, rid)
    c = _client(app, create_session(owner, db_path=db_path))
    note = '<a href="https://evil.test/login">Reset your password</a>'
    r = _post(c, "/api/send-referral", json={"name": "Pat", "email": "pat@friend.test", "note": note})
    assert r.get_json()["ok"] is True
    assert captured_mail
    for payload in captured_mail:
        assert '<a href="https://evil.test' not in payload["html"]
    assert "&lt;a href=" in captured_mail[0]["html"]


# ── SEC-31: competitor refresh ──────────────────────────────────────────────

@pytest.fixture
def counted_threads(monkeypatch):
    """refresh_competitor_intel does a function-local `import threading`, so
    the module attribute is replaced; the target is never run."""
    import threading
    started = []

    class _CountingThread(object):
        def __init__(self, target=None, args=(), kwargs=None, daemon=None, **_):
            self.target, self.args = target, args

        def start(self):
            started.append(self.args)

    monkeypatch.setattr(threading, "Thread", _CountingThread)
    return started


def _full_system_owner(db_path):
    rid = _restaurant(db_path)
    update_restaurant(rid, {"module_reviews": 1, "module_labor": 1, "module_inventory": 1,
                            "module_marketing": 1}, db_path=db_path)
    return rid, _owner(db_path, rid)


def test_a_competitor_refresh_starts_one_job_and_returns_its_id(app, db_path, counted_threads):
    rid, owner = _full_system_owner(db_path)
    c = _client(app, create_session(owner, db_path=db_path))
    r = _post(c, "/api/refresh-competitor-intel")
    body = r.get_json()
    assert r.status_code == 200 and body["ok"] is True and body["job_id"]
    assert len(counted_threads) == 1
    assert _row(db_path, "SELECT status FROM async_jobs WHERE job_id=?", (body["job_id"],))["status"] == "pending"


@pytest.mark.xfail(strict=True, reason="SEC-31: refresh_competitor_intel has no pending-job check, "
                                       "so every POST starts another Places+Claude thread")
def test_a_second_competitor_refresh_while_one_is_pending_joins_it(app, db_path, counted_threads):
    rid, owner = _full_system_owner(db_path)
    c = _client(app, create_session(owner, db_path=db_path))
    first = _post(c, "/api/refresh-competitor-intel").get_json()
    second = _post(c, "/api/refresh-competitor-intel").get_json()
    assert second["ok"] is True
    assert second["job_id"] == first["job_id"]
    assert len(counted_threads) == 1
