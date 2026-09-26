"""Signing in with a backup code, resending a code, and saying where it went.

Backup codes are minted as "ABCD-1234" (models.generate_backup_codes) and
the server accepted them, but neither client could type one: the web page
had six digit boxes whose paste stripped letters, and the app's Verify
needed exactly six characters. What these protect:

  * the server takes a backup code however it is typed — lower case,
    spaces, no dash — and still only once;
  * the web page offers "Use a backup code" (one text field), and a backup
    code that fails brings the page back in that mode;
  * /mobile/api/resend-2fa shares the web's /resend-2fa body: same token,
    same throttle, same destination;
  * the web page and the phone's login response say whether the code was
    texted or emailed;
  * two_fa.html is on the sign-in page's brand fonts and tokens, and says
    its status inline, never in an alert().
"""
import os
import re

import pytest
from flask import Flask

import auth
import auth_routes
import client_api
import mobile_api
import models
from auth import create_user, init_auth, upsert_membership
from models import (Restaurant, create_restaurant, generate_backup_codes, normalize_backup_code,
                    update_restaurant, verify_and_consume_backup_code)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = os.path.join(ROOT, "templates")


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
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
    import emails
    import notify
    box = {"email": [], "sms": []}
    monkeypatch.setattr(emails, "send_2fa_code",
                        lambda to, name, code, owner=None: box["email"].append((to, code)))
    monkeypatch.setattr(notify, "send_2fa_sms", lambda to, name, code: box["sms"].append((to, code)) or True)
    return box


@pytest.fixture
def client():
    app = Flask(__name__, template_folder=TEMPLATES)
    app.secret_key = "test-secret"
    for bp in (auth_routes.auth_bp, mobile_api.mobile_bp, client_api.client_bp):
        app.register_blueprint(bp)
    return app.test_client()


def _setup(db_path):
    rid = create_restaurant(Restaurant(name="R", owner_email="owner@x.test"), db_path=db_path)
    owner = create_user(rid, "owner", "owner@x.test", "ownerpass1", db_path=db_path)
    upsert_membership(owner, rid, "client", db_path=db_path)
    update_restaurant(rid, {"two_fa_enabled": 1}, db_path=db_path)
    return rid, owner


def _login_form(client):
    client.get("/login")
    csrf = client.get_cookie("csrf_token").value
    return client.post("/login", data={"username": "owner", "password": "ownerpass1", "csrf_token": csrf})


def _pending(resp):
    html = resp.get_data(as_text=True)
    marker = 'name="pending_token" value="'
    start = html.index(marker) + len(marker)
    return html[start:html.index('"', start)]


# ── the server's backup-code normalisation ──────────────────────────────────

@pytest.mark.parametrize("typed", ["7F3A-92C1", "7f3a-92c1", "7F3A92C1", " 7f3a 92c1 ", "7F-3A92-C1", "7f3a–92c1"])
def test_a_backup_code_is_read_however_it_is_typed(typed):
    assert normalize_backup_code(typed) == "7F3A-92C1"


@pytest.mark.parametrize("typed", ["", None, "123456", "7F3A-92C", "7F3A-92C1X", "ZZZZ-ZZZZ"])
def test_what_cannot_be_a_backup_code_is_none(typed):
    assert normalize_backup_code(typed) is None


def test_a_lowercase_dashless_backup_code_signs_in_once(db_path):
    rid, _ = _setup(db_path)
    code = generate_backup_codes(rid, db_path=db_path)[0]
    sloppy = code.replace("-", " ").lower()
    assert verify_and_consume_backup_code(rid, sloppy, db_path=db_path) is True
    assert verify_and_consume_backup_code(rid, code, db_path=db_path) is False     # single use


def test_the_web_sign_in_takes_a_sloppy_backup_code(db_path, client, sent):
    rid, owner = _setup(db_path)
    code = generate_backup_codes(rid, db_path=db_path)[0]
    pending = _pending(_login_form(client))
    csrf = client.get_cookie("csrf_token").value
    resp = client.post("/verify-2fa", data={"pending_token": pending, "code": code.replace("-", "").lower(),
                                            "next_url": "/", "csrf_token": csrf})
    assert resp.status_code == 302
    tok = client.get_cookie("session_token")
    assert tok and auth.get_session_user(tok.value, db_path=db_path)["id"] == owner


def test_the_phone_sign_in_takes_a_sloppy_backup_code(db_path, client, sent):
    rid, owner = _setup(db_path)
    code = generate_backup_codes(rid, db_path=db_path)[0]
    r = client.post("/mobile/api/login", json={"username": "owner", "password": "ownerpass1"}).get_json()
    v = client.post("/mobile/api/verify-2fa",
                    json={"pending_token": r["pending_token"], "code": " " + code.lower() + " "}).get_json()
    assert v["ok"] is True and v["user"]["id"] == owner


def test_a_wrong_backup_code_brings_the_page_back_in_backup_mode(db_path, client, sent):
    rid, _ = _setup(db_path)
    generate_backup_codes(rid, db_path=db_path)
    pending = _pending(_login_form(client))
    csrf = client.get_cookie("csrf_token").value
    html = client.post("/verify-2fa", data={"pending_token": pending, "code": "0000-0000",
                                            "next_url": "/", "csrf_token": csrf}).get_data(as_text=True)
    assert "Incorrect code" in html
    assert re.search(r'<div class="backup" id="backup-box">', html)          # shown
    assert re.search(r'<div class="code-input" id="code-boxes"[^>]*hidden', html)


# ── where the code went ─────────────────────────────────────────────────────

def test_the_web_page_says_emailed_for_an_email_code(db_path, client, sent):
    _setup(db_path)
    html = _login_form(client).get_data(as_text=True)
    assert "Check your email" in html and "We emailed a 6-digit code to" in html
    assert "We texted" not in html


def test_the_web_page_says_texted_for_a_text_code(db_path, client, sent):
    rid, _ = _setup(db_path)
    update_restaurant(rid, {"two_fa_method": "sms", "owner_phone": "+15125550100"}, db_path=db_path)
    html = _login_form(client).get_data(as_text=True)
    assert sent["sms"] and "Check your texts" in html and "We texted a 6-digit code to" in html
    assert "Check your email" not in html


def test_the_phone_login_names_the_channel(db_path, client, sent):
    rid, _ = _setup(db_path)
    r = client.post("/mobile/api/login", json={"username": "owner", "password": "ownerpass1"}).get_json()
    assert r["channel"] == "email"
    update_restaurant(rid, {"two_fa_method": "sms", "owner_phone": "+15125550100"}, db_path=db_path)
    r = client.post("/mobile/api/login", json={"username": "owner", "password": "ownerpass1"}).get_json()
    assert r["channel"] == "sms"


# ── resend, web and phone ───────────────────────────────────────────────────

def test_the_phone_can_resend_a_code_to_the_same_place(db_path, client, sent):
    _setup(db_path)
    r = client.post("/mobile/api/login", json={"username": "owner", "password": "ownerpass1"}).get_json()
    resp = client.post("/mobile/api/resend-2fa", json={"pending_token": r["pending_token"]})
    body = resp.get_json()
    assert resp.status_code == 200 and body["ok"] is True and body["channel"] == "email"
    assert [to for to, _c in sent["email"]] == ["owner@x.test", "owner@x.test"]
    # The resent code is the one that now works.
    v = client.post("/mobile/api/verify-2fa",
                    json={"pending_token": r["pending_token"], "code": sent["email"][-1][1]}).get_json()
    assert v["ok"] is True


def test_the_phone_resend_refuses_a_bad_token(db_path, client, sent):
    resp = client.post("/mobile/api/resend-2fa", json={"pending_token": "not-a-token"})
    assert resp.status_code == 401 and resp.get_json()["ok"] is False
    assert sent["email"] == []


def test_web_and_phone_resend_share_one_throttle(db_path, client, sent, monkeypatch):
    calls = []
    monkeypatch.setattr(auth_routes, "_is_rate_limited", lambda key, *a: calls.append(key) or True)
    for path in ("/resend-2fa", "/mobile/api/resend-2fa"):
        resp = client.post(path, json={"pending_token": "x"})
        assert resp.status_code == 429 and resp.get_json()["ok"] is False
    assert calls[0] == calls[1] and calls[0].startswith("2fa-resend:")


def test_both_resend_routes_call_the_one_body():
    src_web = open(os.path.join(ROOT, "auth_routes.py"), encoding="utf-8").read()
    src_mob = open(os.path.join(ROOT, "mobile_api.py"), encoding="utf-8").read()
    assert "payload, status = resend_two_fa(" in src_web
    assert "from auth_routes import resend_two_fa" in src_mob and "payload, status = resend_two_fa(" in src_mob


# ── the page itself ─────────────────────────────────────────────────────────

def _page():
    with open(os.path.join(TEMPLATES, "two_fa.html"), encoding="utf-8") as f:
        return f.read()


def test_two_fa_page_is_on_brand():
    t = _page()
    assert "DM Sans" not in t and "DM Serif" not in t
    assert '<link rel="stylesheet" href="/static/fonts/cavnar-fonts.css">' in t
    assert "'Apfel Grotezk'" in t and "'Clash Display'" in t and "'Space Grotesk'" in t
    # Colours come from the :root tokens; the only literal hexes are the
    # token definitions and the browser's theme-color.
    css = re.search(r"<style>(.*?)</style>", t, re.S).group(1)
    root = re.search(r":root\{[^}]*\}", css).group(0)
    assert not re.findall(r"#[0-9a-fA-F]{3,6}\b", css.replace(root, ""))


def test_two_fa_page_never_alerts_and_offers_a_backup_code():
    t = _page()
    assert "alert(" not in t
    assert 'id="twofa-status" role="status" aria-live="polite"' in t
    assert 'id="backup-code"' in t and "Use a backup code instead" in t
    # The paste handler no longer throws letters away unconditionally:
    # a pasted backup code moves to the backup field.
    assert "setMode(true);" in t
