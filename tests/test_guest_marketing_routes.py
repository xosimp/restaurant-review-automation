"""client_api.py's guest text club routes — the one feature in this app
where a module gate is enforced server-side, not just hidden in the UI
(see the comment above _restaurant_has_marketing_module in client_api.py:
sending a guest SMS campaign has a real per-message Twilio cost, so a
client without the Marketing module hitting the API directly would be a
real billable abuse path, unlike every other module gate here)."""
import pytest
from flask import Flask

import auth
import client_api
import models
from client_api import (
    client_bp, guest_contacts_list, guest_contacts_add, guest_contacts_delete,
    guest_contacts_mark_visit, guest_campaign_draft, guest_campaign_send,
    guest_optin_page, guest_optin_submit, guest_qr_code,
)
from auth_routes import auth_bp
from models import create_restaurant, Restaurant


@pytest.fixture(autouse=True)
def _init_tables(db_path):
    from guest_marketing import init_guest_marketing
    init_guest_marketing(db_path=db_path)


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    real_get_conn = models.get_conn
    redirect = lambda *a, **k: real_get_conn(db_path)
    for mod in (models, auth, client_api):
        monkeypatch.setattr(mod, "get_conn", redirect)


def _restaurant(db_path, **kw):
    rid = create_restaurant(Restaurant(name=kw.pop("name", "Route Test Co"), owner_email="r@x.com", **kw), db_path=db_path)
    return rid


@pytest.fixture
def app():
    flask_app = Flask(__name__, template_folder="../templates")
    flask_app.register_blueprint(client_bp)
    flask_app.register_blueprint(auth_bp)
    return flask_app


def _login_as(monkeypatch, rid):
    monkeypatch.setattr(auth, "get_current_user", lambda: {"id": 1, "restaurant_id": rid, "is_admin": 0})


# ── authenticated routes reject restaurants without the Marketing module ────
# These routes return a bare jsonify(...) on success (200 implied) but a
# (response, status) tuple on the 403 rejection path — unpack accordingly,
# matching the pattern in test_admin_routes.py for calling route functions
# directly rather than through the full Flask test client.

def test_guest_contacts_list_rejects_without_marketing_module(monkeypatch, app, db_path):
    rid = _restaurant(db_path, module_marketing=0)
    _login_as(monkeypatch, rid)
    with app.test_request_context("/api/guest-contacts"):
        resp, status = guest_contacts_list()
    assert status == 403
    assert "Marketing module" in resp.get_json()["error"]


def test_guest_contacts_add_rejects_without_marketing_module(monkeypatch, app, db_path):
    rid = _restaurant(db_path, module_marketing=0)
    _login_as(monkeypatch, rid)
    with app.test_request_context("/api/guest-contacts", method="POST", json={"name": "Jane", "phone": "555-123-4567"}):
        resp, status = guest_contacts_add()
    assert status == 403


def test_guest_contacts_delete_rejects_without_marketing_module(monkeypatch, app, db_path):
    rid = _restaurant(db_path, module_marketing=0)
    _login_as(monkeypatch, rid)
    with app.test_request_context("/api/guest-contacts/1", method="DELETE"):
        resp, status = guest_contacts_delete(1)
    assert status == 403


def test_guest_contacts_mark_visit_rejects_without_marketing_module(monkeypatch, app, db_path):
    rid = _restaurant(db_path, module_marketing=0)
    _login_as(monkeypatch, rid)
    with app.test_request_context("/api/guest-contacts/1/mark-visit", method="POST"):
        resp, status = guest_contacts_mark_visit(1)
    assert status == 403


def test_guest_campaign_draft_rejects_without_marketing_module(monkeypatch, app, db_path):
    rid = _restaurant(db_path, module_marketing=0)
    _login_as(monkeypatch, rid)
    with app.test_request_context("/api/guest-campaign/draft", method="POST", json={"type": "general"}):
        resp, status = guest_campaign_draft()
    assert status == 403


def test_guest_campaign_send_rejects_without_marketing_module(monkeypatch, app, db_path):
    rid = _restaurant(db_path, module_marketing=0)
    _login_as(monkeypatch, rid)
    with app.test_request_context("/api/guest-campaign/send", method="POST", json={"message": "hi"}):
        resp, status = guest_campaign_send()
    assert status == 403


def test_guest_qr_rejects_without_marketing_module(monkeypatch, app, db_path):
    rid = _restaurant(db_path, module_marketing=0)
    _login_as(monkeypatch, rid)
    with app.test_request_context("/api/guest-qr"):
        resp, status = guest_qr_code()
    assert status == 403


def test_guest_qr_returns_a_real_png(monkeypatch, app, db_path):
    """send_file's streaming response needs a real WSGI request/response
    cycle to read back — a bare test_request_context() with a direct
    function call raises on .get_data() (passthrough mode), so this one
    goes through the actual test client instead."""
    rid = _restaurant(db_path, module_marketing=1)
    monkeypatch.setattr(auth, "get_current_user", lambda: {"id": 1, "restaurant_id": rid, "is_admin": 0})
    client = app.test_client()
    resp = client.get("/api/guest-qr")
    assert resp.mimetype == "image/png"
    assert resp.data[:8] == b"\x89PNG\r\n\x1a\n"  # real PNG magic bytes, not a placeholder


def test_guest_contacts_list_allows_with_marketing_module(monkeypatch, app, db_path):
    rid = _restaurant(db_path, module_marketing=1)
    _login_as(monkeypatch, rid)
    with app.test_request_context("/api/guest-contacts"):
        resp = guest_contacts_list()
    assert resp.get_json()["ok"] is True


# ── public join page / opt-in also respect the gate ─────────────────────────

def test_public_join_page_404s_without_marketing_module(db_path):
    rid = _restaurant(db_path, module_marketing=0)
    app_ = Flask(__name__, template_folder="../templates")
    app_.register_blueprint(client_bp)
    with app_.test_request_context(f"/join/{rid}"):
        body, status = guest_optin_page(rid)
    assert status == 404


def test_public_optin_submit_rejects_without_marketing_module(db_path):
    rid = _restaurant(db_path, module_marketing=0)
    app_ = Flask(__name__, template_folder="../templates")
    app_.register_blueprint(client_bp)
    with app_.test_request_context(f"/api/public/guest-optin/{rid}", method="POST",
                                    json={"name": "Jane", "phone": "555-123-4567", "consent": True}):
        resp, status = guest_optin_submit(rid)
    assert status == 404
    assert resp.get_json()["ok"] is False
