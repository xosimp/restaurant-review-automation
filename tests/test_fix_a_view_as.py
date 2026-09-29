"""Fix round A: view-as (#13, #85, #154, #49 — owner decision 3).

An admin's view-as keeps write access, but:
  * the banner shows whenever the SESSION is a view-as (the old test was
    current_user.is_admin, which a view-as session — the client's own
    login — never has, so it never showed);
  * every write made through it is recorded as the acting admin's;
  * opening and stopping are both recorded;
  * it lasts two hours from when it was opened, absolute;
  * it opens only on the restaurant's owner login, never a stand-in;
  * stopping is a POST (a GET only asks) and ends only a view-as session;
  * the web tab ping does not count the admin's clicks as the owner's.
"""
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
import models
from auth import create_session, create_user, get_session_user, init_auth, upsert_membership
from models import Restaurant, create_restaurant

CSRF = "fix-a-view-as-csrf"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


@pytest.fixture
def app():
    a = Flask(__name__, template_folder="../templates")
    a.secret_key = "fix-a"
    for bp in (auth_routes.auth_bp, admin_routes.admin_bp, client_api.client_bp):
        a.register_blueprint(bp)
    return a


def _world(db_path, support=False):
    hq = create_restaurant(Restaurant(name="Cavnar AI Admin", owner_email="w@x.test"), db_path=db_path)
    if support:
        actor = create_user(hq, "sup", "sup@x.test", "Support-pass-2026", role="support", db_path=db_path)
    else:
        actor = create_user(hq, "will", "w@x.test", "Admin-pass-2026", is_admin=True, db_path=db_path)
    rid = create_restaurant(Restaurant(name="Client Grill", owner_email="owner@client.test"), db_path=db_path)
    owner = create_user(rid, "owner", "owner@client.test", "Owner-pass-2026", db_path=db_path)
    upsert_membership(owner, rid, "client", db_path=db_path)
    return actor, rid, owner


def _client(app, token):
    c = app.test_client()
    c.set_cookie("session_token", token)
    c.set_cookie("csrf_js", CSRF)
    return c


def _events(db_path, kind):
    conn = models.get_conn(db_path)
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM admin_events WHERE event_type=? ORDER BY id", (kind,))]
    finally:
        conn.close()


def _open(app, db_path, support=False):
    actor, rid, owner = _world(db_path, support=support)
    admin_tok = create_session(actor, db_path=db_path)
    c = _client(app, admin_tok)
    r = c.post("/admin/view-as/%d" % rid, headers={"X-CSRF": CSRF})
    assert r.status_code == 302
    return c, actor, rid, owner, admin_tok


def test_opening_records_who_for_how_long_and_puts_the_admin_on_the_session(app, db_path):
    c, actor, rid, owner, _ = _open(app, db_path)
    viewing = get_session_user(c.get_cookie("session_token").value, db_path=db_path)
    assert viewing["id"] == owner and viewing["device_type"] == "admin-view-as"
    assert viewing["acting_admin_id"] == actor and viewing["acting_admin"] == "will"
    assert viewing["view_as_read_only"] is False                    # an admin's view writes (owner decision 3)
    started = _events(db_path, "view_as_started")
    assert started and started[-1]["restaurant_id"] == rid
    # Typed columns (integration wave: the one audit call, record_admin_action).
    assert started[-1]["actor"] == "will" and started[-1]["actor_id"] == actor
    assert json.loads(started[-1]["after_json"])["hours"] == 2


def test_every_write_through_view_as_is_recorded_as_the_admins(app, db_path):
    """Owner decision 3: an admin's view keeps write access, and the change
    is the admin's on the record."""
    c, actor, rid, owner, _ = _open(app, db_path)
    r = c.post("/api/toggle-login-notify", json={"enabled": True}, headers={"X-CSRF": CSRF})
    assert r.status_code == 200 and models.get_restaurant(rid, db_path=db_path).login_notify
    rows = _events(db_path, "view_as_write")
    assert rows, "the write was not attributed"
    # Typed columns (integration wave: the one audit call, record_admin_action).
    row = rows[-1]
    p = json.loads(row["after_json"])
    assert row["actor"] == "will" and row["actor_id"] == actor and p["as_user_id"] == owner
    assert p["path"] == "/api/toggle-login-notify" and p["status"] == 200 and row["result"] == "ok"
    assert "will (viewing as owner)" in row["summary"]


def test_the_tab_ping_does_not_count_the_admin_as_the_owner(app, db_path):
    c, actor, rid, owner, _ = _open(app, db_path)
    r = c.post("/api/log-activity", json={"tab": "reviews"}, headers={"X-CSRF": CSRF})
    assert r.get_json().get("skipped") == "view_as"
    assert not models.get_restaurant(rid, db_path=db_path).last_activity
    assert _events(db_path, "view_as_write") == []            # a no-op ping is not a write on the record


def test_a_support_view_as_refuses_writes_and_records_the_refusal(app, db_path):
    c, actor, rid, owner, _ = _open(app, db_path, support=True)
    r = c.post("/api/toggle-login-notify", json={"enabled": True}, headers={"X-CSRF": CSRF})
    assert r.status_code == 403 and r.get_json()["read_only"] is True
    assert not models.get_restaurant(rid, db_path=db_path).login_notify
    row = _events(db_path, "view_as_write")[-1]
    assert row["result"] == "denied" and row["actor"] == "sup"


def test_view_as_refuses_a_restaurant_with_no_owner_login(app, db_path):
    """#85: it used to fall back to any console login — a manager's."""
    hq = create_restaurant(Restaurant(name="Cavnar AI Admin", owner_email="w@x.test"), db_path=db_path)
    admin = create_user(hq, "will", "w@x.test", "Admin-pass-2026", is_admin=True, db_path=db_path)
    rid = create_restaurant(Restaurant(name="No Owner", owner_email="o@x.test"), db_path=db_path)
    mgr = create_user(rid, "mgr", "m@x.test", "Manager-pass-2026", role="manager", db_path=db_path)
    upsert_membership(mgr, rid, "manager", db_path=db_path)
    c = _client(app, create_session(admin, db_path=db_path))
    r = c.post("/admin/view-as/%d" % rid, headers={"X-CSRF": CSRF})
    assert r.status_code == 404
    conn = models.get_conn(db_path)
    assert conn.execute("SELECT COUNT(*) FROM sessions WHERE device_type='admin-view-as'").fetchone()[0] == 0
    conn.close()


def test_stopping_is_a_post_that_records_it_and_brings_the_admins_session_back(app, db_path):
    c, actor, rid, owner, admin_tok = _open(app, db_path)
    view_tok = c.get_cookie("session_token").value
    asked = c.get("/admin/stop-viewing")
    assert asked.status_code == 200 and get_session_user(view_tok, db_path=db_path) is not None
    r = c.post("/admin/stop-viewing", headers={"X-CSRF": CSRF})
    assert r.status_code == 302 and r.headers["Location"].endswith("/admin")
    assert get_session_user(view_tok, db_path=db_path) is None
    assert c.get_cookie("session_token").value == admin_tok          # no second sign-in
    stopped = _events(db_path, "view_as_stopped")
    assert stopped and stopped[-1]["actor"] == "will" and stopped[-1]["actor_id"] == actor


def test_a_cross_site_stop_viewing_link_signs_nobody_out(app, db_path):
    actor, rid, owner = _world(db_path)
    owner_tok = create_session(owner, db_path=db_path)
    c = _client(app, owner_tok)
    c.get("/admin/stop-viewing")
    c.post("/admin/stop-viewing", headers={"X-CSRF": CSRF})
    assert get_session_user(owner_tok, db_path=db_path) is not None


def test_the_confirm_page_says_how_long_and_that_changes_are_recorded(app, db_path):
    actor, rid, owner = _world(db_path)
    c = _client(app, create_session(actor, db_path=db_path))
    body = c.get("/admin/view-as/%d" % rid).get_data(as_text=True)
    assert "for 2 hours" in body and "recorded under your name" in body


# ── the banner, rendered by the real app (hosted_dashboard) ────────────────

_BANNER = r'''
import json, os, re, sqlite3, sys
vol = sys.argv[1]
sqlite3.connect(os.path.join(vol, "reviews.db")).close()
os.environ.update(RAILWAY_VOLUME_MOUNT_PATH=vol, RUN_SCHEDULER_IN_WEB="0", ANTHROPIC_API_KEY="",
                  RESEND_API_KEY="", TWILIO_AUTH_TOKEN="", STRIPE_SECRET_KEY="", SECRET_KEY="test-secret",
                  CAVNAR_PIN_PEPPER="test-pepper", HIBP_DISABLED="1", ADMIN_PASSWORD="Admin-pass-2026!",
                  GOOGLE_PLACES_API_KEY="", PERPLEXITY_API_KEY="")
os.environ.pop("ADMIN_REQUIRE_2FA", None)
os.environ.pop("RAILWAY_ENVIRONMENT", None); os.environ.pop("RAILWAY_PROJECT_ID", None)
import socket
def _blocked(*a, **k):
    raise OSError("network blocked in test subprocess")
socket.create_connection = _blocked
import hosted_dashboard as h
import auth, models
from models import Restaurant
rid = models.create_restaurant(Restaurant(name="Banner Grill", owner_email="bo@x.test", module_reviews=1))
uid = auth.create_user(rid, "bannerowner", "bo@x.test", "correct-horse-battery")
auth.upsert_membership(uid, rid, "client")
c = h.app.test_client()
page = c.get("/login").get_data(as_text=True)
tok = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
r = c.post("/login", data={"username": "will", "password": "Admin-pass-2026!", "csrf_token": tok})
out = {"admin_login": r.status_code}
csrf = c.get_cookie("csrf_js").value if c.get_cookie("csrf_js") else ""
if not csrf:
    c.get("/admin/view-as/%d" % rid)
    csrf = c.get_cookie("csrf_js").value
r = c.post("/admin/view-as/%d" % rid, headers={"X-CSRF": csrf})
out["view_as"] = r.status_code
html = c.get("/").get_data(as_text=True)
out["banner"] = 'id="view-as-banner"' in html
out["names_admin"] = "— will." in html
out["post_form"] = 'action="/admin/stop-viewing"' in html and 'method="post"' in html
owner_c = h.app.test_client()
page = owner_c.get("/login").get_data(as_text=True)
tok = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
owner_c.post("/login", data={"username": "bannerowner", "password": "correct-horse-battery", "csrf_token": tok})
out["owner_banner"] = 'id="view-as-banner"' in owner_c.get("/").get_data(as_text=True)
print("RESULT " + json.dumps(out))
'''


def test_the_banner_shows_on_a_view_as_session_and_never_to_the_owner():
    vol = tempfile.mkdtemp(prefix="cavnar-fix-a-banner-")
    out = subprocess.run([sys.executable, "-c", _BANNER, vol], cwd=ROOT, capture_output=True, text=True, timeout=240)
    line = [l for l in out.stdout.splitlines() if l.startswith("RESULT ")]
    assert out.returncode == 0 and line, out.stdout[-1500:] + "\n" + out.stderr[-2500:]
    got = json.loads(line[-1][len("RESULT "):])
    assert got["admin_login"] == 302 and got["view_as"] == 302, got
    assert got["banner"] and got["names_admin"] and got["post_form"], got
    assert got["owner_banner"] is False, got
