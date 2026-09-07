"""Account -> Close my account (mobile_api.py's /account/request-deletion).

Apple App Store Review Guideline 5.1.1(v): an app that supports account
creation must let the user initiate deletion from inside the app — a
"please email us" flow doesn't satisfy it outside a handful of regulated
industries, which this isn't. Cavnar AI still can't self-serve deactivate an
account (clients are under a service contract), so this is a real request
that notifies Will to start the 30-day wind-down — not self-service
deletion, but a genuine in-app initiation with a real confirmation, replacing
what used to be an in-app button that just opened the user's own email
client and hoped they sent something.
"""
import pytest
from flask import Flask

import auth
import client_api
import guest_marketing
import mobile_api
import models
from auth import create_user, init_auth
from mobile_api import mobile_bp
from models import create_restaurant, Restaurant, get_conn, get_deletion_requested_at, request_account_deletion


def _redirect_db(monkeypatch, db_path):
    real_get_conn = models.get_conn
    redirect = lambda *a, **k: real_get_conn(db_path)
    for mod in (models, auth, client_api, mobile_api, guest_marketing):
        monkeypatch.setattr(mod, "get_conn", redirect)


@pytest.fixture(autouse=True)
def _init_auth_tables(db_path):
    init_auth(db_path=db_path)


@pytest.fixture
def app(db_path, monkeypatch):
    _redirect_db(monkeypatch, db_path)
    flask_app = Flask(__name__)
    flask_app.register_blueprint(mobile_bp)
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


def _restaurant(db_path, **kw):
    return create_restaurant(Restaurant(name=kw.pop("name", "Close Test Co"), owner_email="c@x.com",
                                        owner_name=kw.pop("owner_name", "Erik"), **kw), db_path=db_path)


def _login(client, db_path, rid, username="alice", password="correct-horse"):
    create_user(rid, username, f"{username}@x.com", password, db_path=db_path)
    resp = client.post("/mobile/api/login", json={"username": username, "password": password})
    return resp.get_json()["token"]


def _hdr(token):
    return {"Authorization": f"Bearer {token}"}


# ── models.py helpers ────────────────────────────────────────────────────────

def test_request_account_deletion_sets_and_is_idempotent(db_path):
    rid = _restaurant(db_path)
    assert get_deletion_requested_at(rid, db_path=db_path) is None
    first = request_account_deletion(rid, db_path=db_path)
    assert first and get_deletion_requested_at(rid, db_path=db_path) == first
    second = request_account_deletion(rid, db_path=db_path)
    assert second == first, "a second request must not reset the notice clock"


def test_request_account_deletion_scoped_to_its_own_restaurant(db_path):
    rid1 = _restaurant(db_path, name="Co One")
    rid2 = _restaurant(db_path, name="Co Two")
    request_account_deletion(rid1, db_path=db_path)
    assert get_deletion_requested_at(rid1, db_path=db_path) is not None
    assert get_deletion_requested_at(rid2, db_path=db_path) is None


# ── route ────────────────────────────────────────────────────────────────────

def test_route_requires_authentication(client):
    assert client.post("/mobile/api/account/request-deletion").status_code == 401


def test_first_request_notifies_will_logs_the_event_and_returns_a_timestamp(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    sent = {}
    monkeypatch.setattr("emails.send_account_deletion_request_email",
                        lambda name, owner, email, at: sent.update(name=name, owner=owner, email=email, at=at) or True)
    resp = client.post("/mobile/api/account/request-deletion", headers=_hdr(token))
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True and body["requested_at"]
    assert sent["name"] == "Close Test Co" and sent["owner"] == "Erik" and sent["email"] == "alice@x.com"
    assert sent["at"] == body["requested_at"]
    conn = get_conn(db_path)
    row = conn.execute("SELECT event_type FROM activity_log WHERE restaurant_id=? ORDER BY id DESC LIMIT 1", (rid,)).fetchone()
    conn.close()
    assert row and row["event_type"] == "deletion_requested"


def test_second_request_does_not_send_a_second_email_or_move_the_timestamp(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    calls = []
    monkeypatch.setattr("emails.send_account_deletion_request_email", lambda *a: calls.append(a) or True)
    first = client.post("/mobile/api/account/request-deletion", headers=_hdr(token)).get_json()
    second = client.post("/mobile/api/account/request-deletion", headers=_hdr(token)).get_json()
    assert len(calls) == 1, "re-tapping the button must not re-notify Will"
    assert second["requested_at"] == first["requested_at"]


def test_a_reader_failure_still_records_the_request(client, db_path, monkeypatch):
    """The notice email is best-effort — if Resend is down, the owner's
    request still has to be recorded and confirmed, not silently lost."""
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    def _boom(*a):
        raise RuntimeError("resend is down")
    monkeypatch.setattr("emails.send_account_deletion_request_email", _boom)
    resp = client.post("/mobile/api/account/request-deletion", headers=_hdr(token))
    assert resp.status_code == 200 and resp.get_json()["ok"] is True
    assert get_deletion_requested_at(rid, db_path=db_path) is not None


def test_account_summary_reflects_a_pending_request(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    monkeypatch.setattr("emails.send_account_deletion_request_email", lambda *a: True)
    before = client.get("/mobile/api/account", headers=_hdr(token)).get_json()
    assert before["profile"]["deletion_requested_at"] is None
    requested_at = client.post("/mobile/api/account/request-deletion", headers=_hdr(token)).get_json()["requested_at"]
    after = client.get("/mobile/api/account", headers=_hdr(token)).get_json()
    assert after["profile"]["deletion_requested_at"] == requested_at


def test_request_is_scoped_to_the_caller_own_restaurant_over_http(client, db_path, monkeypatch):
    rid1 = _restaurant(db_path, name="Co One")
    rid2 = _restaurant(db_path, name="Co Two")
    monkeypatch.setattr("emails.send_account_deletion_request_email", lambda *a: True)
    token1 = _login(client, db_path, rid1, username="one")
    client.post("/mobile/api/account/request-deletion", headers=_hdr(token1))
    token2 = _login(client, db_path, rid2, username="two")
    other = client.get("/mobile/api/account", headers=_hdr(token2)).get_json()
    assert other["profile"]["deletion_requested_at"] is None
