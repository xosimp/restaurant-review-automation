"""The web halves of the iOS-first features (client_api's parity block):
every route answers under a web session and calls the same handler the
phone uses — plus the phone halves of the web-first ones."""
import pytest
from flask import Flask

import admin_routes
import auth
import auth_routes
import client_api
import mobile_api
import models
import notify
import value_delivered
from auth import create_user, init_auth
from client_api import client_bp
from mobile_api import mobile_bp
from models import create_restaurant, get_conn, get_restaurant, Restaurant


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    real_get_conn = models.get_conn
    redirect = lambda *a, **k: real_get_conn(db_path)
    for mod in (models, admin_routes, auth, auth_routes, client_api, mobile_api):
        monkeypatch.setattr(mod, "get_conn", redirect)
    init_auth(db_path=db_path)
    from models import init_two_fa_backup_codes
    init_two_fa_backup_codes(db_path=db_path)


@pytest.fixture
def client():
    flask_app = Flask(__name__, template_folder="../templates")
    flask_app.register_blueprint(client_bp)
    flask_app.register_blueprint(mobile_bp)
    return flask_app.test_client()


def _restaurant(db_path, **kw):
    return create_restaurant(Restaurant(name="Parity Co", owner_email="p@x.com", **kw), db_path=db_path)


def _web_user(monkeypatch, db_path, rid):
    uid = create_user(rid, "parity", "p@x.com", "parity-pass-1", db_path=db_path)
    user = {"id": uid, "restaurant_id": rid, "is_admin": 0, "username": "parity", "email": "p@x.com", "role": "client"}
    monkeypatch.setattr(auth, "get_current_user", lambda: user)
    return user


def _mobile_token(client, db_path, rid):
    create_user(rid, "mob", "mob@x.com", "mobile-pass-1", db_path=db_path)
    r = client.post("/mobile/api/login", json={"username": "mob", "password": "mobile-pass-1"})
    return {"Authorization": "Bearer " + r.get_json()["token"]}


def test_web_account_parity_routes_answer(client, db_path, monkeypatch):
    rid = _restaurant(db_path, module_reviews=1, module_labor=1, module_marketing=1)
    _web_user(monkeypatch, db_path, rid)
    for path in ["/api/home", "/api/account/team", "/api/account/login-history", "/api/account/activity",
                 "/api/account/2fa/backup-codes", "/api/account/2fa/trusted-devices",
                 "/api/account/security-summary", "/api/ai-visibility/history", "/api/labor/schedule-history"]:
        r = client.get(path)
        assert r.status_code == 200, (path, r.data[:200])
        assert r.get_json()["ok"] is True, path


def test_web_profile_edit_writes_the_same_fields_as_the_phone(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    _web_user(monkeypatch, db_path, rid)
    r = client.post("/api/account/profile", json={"owner_name": "Ada", "owner_phone": "555-0100", "tone_preset": "warm",
                                                  "timezone": "America/Denver", "sign_off_name": "Ada & team"})
    assert r.status_code == 200 and r.get_json()["ok"]
    rest = get_restaurant(rid)
    assert (rest.owner_name, rest.owner_phone, rest.tone_preset, rest.timezone) == ("Ada", "555-0100", "warm", "America/Denver")


def test_web_team_invite_and_revoke(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    _web_user(monkeypatch, db_path, rid)
    monkeypatch.setattr("emails.send_team_invite_email", lambda *a, **k: True, raising=False)
    r = client.post("/api/account/team/invite", json={"name": "Mate", "email": "mate@x.com"})
    assert r.get_json()["ok"], r.get_json()
    uid = r.get_json()["user_id"]
    members = client.get("/api/account/team").get_json()["members"]
    assert any(m["id"] == uid and not m["is_you"] for m in members)
    assert client.post(f"/api/account/team/{uid}/revoke", json={}).get_json()["ok"]
    assert all(m["id"] != uid or not m["is_active"] for m in client.get("/api/account/team").get_json()["members"])


def test_web_brand_voice_carries_tone_and_sign_off(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    _web_user(monkeypatch, db_path, rid)
    r = client.post("/api/brand-voice", json={"voice_notes": "v", "never_say": "n", "menu_notes": "m",
                                              "tone_preset": "playful", "sign_off_name": "Chef"})
    assert r.get_json()["ok"]
    d = client.get("/api/brand-voice").get_json()
    assert (d["tone_preset"], d["sign_off_name"]) == ("playful", "Chef")


def test_mobile_home_carries_the_setup_checklist(client, db_path):
    rid = _restaurant(db_path, module_reviews=1, module_marketing=1)
    headers = _mobile_token(client, db_path, rid)
    steps = client.get("/mobile/api/home", headers=headers).get_json()["setup_checklist"]
    # Onboarding checklists were retired on web and iOS (Sep 2026): the key
    # stays in the payload for older app builds, always empty.
    assert steps == []
    # the dismiss route still answers for older builds, and Home stays empty after it
    assert client.post("/mobile/api/account/dismiss-onboarding", headers=headers).get_json()["ok"]
    assert client.get("/mobile/api/home", headers=headers).get_json()["setup_checklist"] == []


def test_mobile_mark_posted_uses_the_web_handler(client, db_path):
    rid = _restaurant(db_path, module_reviews=1)
    headers = _mobile_token(client, db_path, rid)
    conn = get_conn(db_path)
    conn.execute("INSERT INTO reviews (restaurant_id, external_id, platform, author, rating, text, response_status, "
                 "draft_response, fetched_at, review_date) VALUES (?, 'ext-1', 'yelp', 'A', 5, 'great', 'approved', "
                 "'thanks', datetime('now'), '2026-09-01')", (rid,))
    review_id = conn.execute("SELECT id FROM reviews WHERE restaurant_id=?", (rid,)).fetchone()[0]
    conn.commit(); conn.close()
    r = client.post(f"/mobile/api/reviews/{review_id}/mark-posted", headers=headers)
    assert r.status_code == 200 and r.get_json()["ok"], r.get_json()
    conn = get_conn(db_path)
    assert conn.execute("SELECT response_status FROM reviews WHERE id=?", (review_id,)).fetchone()[0] == "posted"
    conn.close()
