"""Team messages — manager DMs.

Built from Erik's own words: "a communication log where himself and the
other managers can all message each other in DMs." Scoped to whoever
actually has a login on the restaurant — this product never creates an
account for anyone but an owner or someone they invited (auth.
invite_team_member), so "every login" and "every manager" are the same set.
1:1 only, matching what was actually asked for.
"""
import os

import pytest
from flask import Flask

import auth
import client_api
import models
from auth import create_user, init_auth
from models import (
    TeamMessageError, count_unread_team_messages, create_restaurant,
    get_team_conversation, get_team_inbox, mark_team_messages_read,
    send_team_message,
)


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, client_api):
        monkeypatch.setattr(mod, "get_conn", redirect)
    init_auth(db_path=db_path)


def _restaurant(db_path, name="Simple EJ's"):
    from models import Restaurant
    return create_restaurant(Restaurant(name=name, owner_email="o@x.test"), db_path=db_path)


def _user(db_path, rid, username):
    return create_user(rid, username, f"{username}@x.test", "correct-horse", db_path=db_path)


# ── models layer ─────────────────────────────────────────────────────────

def test_a_message_can_be_sent_and_read_back(db_path):
    rid = _restaurant(db_path)
    erik = _user(db_path, rid, "erik")
    dana = _user(db_path, rid, "dana")
    msg = send_team_message(rid, erik, dana, "can you open tomorrow?", db_path=db_path)
    assert msg["body"] == "can you open tomorrow?"
    assert msg["sender_id"] == erik and msg["recipient_id"] == dana
    assert msg["read_at"] is None


def test_an_empty_message_is_refused(db_path):
    rid = _restaurant(db_path)
    erik = _user(db_path, rid, "erik")
    dana = _user(db_path, rid, "dana")
    with pytest.raises(TeamMessageError):
        send_team_message(rid, erik, dana, "   ", db_path=db_path)


def test_you_cannot_message_yourself(db_path):
    rid = _restaurant(db_path)
    erik = _user(db_path, rid, "erik")
    with pytest.raises(TeamMessageError):
        send_team_message(rid, erik, erik, "hi", db_path=db_path)


def test_a_recipient_from_another_restaurant_is_refused(db_path):
    """The cross-tenant guard: recipient_id is never trusted as this
    restaurant's teammate just because the caller says so."""
    rid = _restaurant(db_path, "Simple EJ's")
    other_rid = _restaurant(db_path, "Gia Mia")
    erik = _user(db_path, rid, "erik")
    stranger = _user(db_path, other_rid, "stranger")
    with pytest.raises(TeamMessageError):
        send_team_message(rid, erik, stranger, "hi", db_path=db_path)


def test_a_conversation_reads_oldest_first_from_both_sides(db_path):
    rid = _restaurant(db_path)
    erik = _user(db_path, rid, "erik")
    dana = _user(db_path, rid, "dana")
    send_team_message(rid, erik, dana, "first", db_path=db_path)
    send_team_message(rid, dana, erik, "second", db_path=db_path)
    send_team_message(rid, erik, dana, "third", db_path=db_path)
    thread = get_team_conversation(rid, erik, dana, db_path=db_path)
    assert [m["body"] for m in thread] == ["first", "second", "third"]


def test_reading_a_thread_marks_only_the_other_persons_messages_read(db_path):
    rid = _restaurant(db_path)
    erik = _user(db_path, rid, "erik")
    dana = _user(db_path, rid, "dana")
    send_team_message(rid, dana, erik, "hey erik", db_path=db_path)
    send_team_message(rid, erik, dana, "hey dana", db_path=db_path)
    mark_team_messages_read(rid, erik, dana, db_path=db_path)
    thread = get_team_conversation(rid, erik, dana, db_path=db_path)
    by_body = {m["body"]: m for m in thread}
    assert by_body["hey erik"]["read_at"] is not None
    # Erik's own sent message was never marked unread to begin with, and
    # reading his inbox must not touch it either way.
    assert by_body["hey dana"]["read_at"] is None


def test_unread_count_only_counts_messages_to_me(db_path):
    rid = _restaurant(db_path)
    erik = _user(db_path, rid, "erik")
    dana = _user(db_path, rid, "dana")
    send_team_message(rid, dana, erik, "one", db_path=db_path)
    send_team_message(rid, dana, erik, "two", db_path=db_path)
    send_team_message(rid, erik, dana, "reply", db_path=db_path)
    assert count_unread_team_messages(rid, erik, db_path=db_path) == 2
    # Dana's own sent message doesn't count against her, but Erik's reply —
    # which she hasn't opened — does.
    assert count_unread_team_messages(rid, dana, db_path=db_path) == 1
    mark_team_messages_read(rid, dana, erik, db_path=db_path)
    assert count_unread_team_messages(rid, dana, db_path=db_path) == 0


def test_inbox_lists_every_teammate_even_with_no_history(db_path):
    rid = _restaurant(db_path)
    erik = _user(db_path, rid, "erik")
    _user(db_path, rid, "dana")
    inbox = get_team_inbox(rid, erik, db_path=db_path)
    assert [t["username"] for t in inbox] == ["dana"]
    assert inbox[0]["last_message"] is None
    assert inbox[0]["unread"] == 0


def test_inbox_shows_the_last_message_and_who_sent_it(db_path):
    rid = _restaurant(db_path)
    erik = _user(db_path, rid, "erik")
    dana = _user(db_path, rid, "dana")
    send_team_message(rid, dana, erik, "running 10 late", db_path=db_path)
    inbox = get_team_inbox(rid, erik, db_path=db_path)
    assert inbox[0]["last_message"] == "running 10 late"
    assert inbox[0]["last_from_me"] is False
    assert inbox[0]["unread"] == 1


def test_inbox_sorts_by_most_recent_activity_never_messaged_last(db_path):
    rid = _restaurant(db_path)
    erik = _user(db_path, rid, "erik")
    dana = _user(db_path, rid, "dana")
    cole = _user(db_path, rid, "cole")
    send_team_message(rid, erik, cole, "older", db_path=db_path)
    send_team_message(rid, dana, erik, "newer", db_path=db_path)
    _user(db_path, rid, "avery")
    inbox = get_team_inbox(rid, erik, db_path=db_path)
    # datetime('now') is second-resolution, so cole/dana can tie within the
    # same test run — what actually matters is that avery (never messaged)
    # sorts after both teammates with real activity.
    assert set(t["username"] for t in inbox[:2]) == {"dana", "cole"}
    assert inbox[2]["username"] == "avery"


def test_inbox_excludes_teammates_from_other_restaurants(db_path):
    rid = _restaurant(db_path, "Simple EJ's")
    other_rid = _restaurant(db_path, "Gia Mia")
    erik = _user(db_path, rid, "erik")
    _user(db_path, other_rid, "someone_else")
    assert get_team_inbox(rid, erik, db_path=db_path) == []


# ── route layer ──────────────────────────────────────────────────────────

@pytest.fixture
def app(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    import mobile_api
    for mod in (models, auth, client_api, mobile_api):
        monkeypatch.setattr(mod, "get_conn", redirect)
    flask_app = Flask(__name__)
    flask_app.register_blueprint(client_api.client_bp)
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


def _login_session(client, db_path, rid, username):
    import auth as _auth
    uid = create_user(rid, username, f"{username}@x.test", "correct-horse", db_path=db_path)
    token = _auth.create_session(uid, db_path=db_path)
    client.set_cookie("session_token", token)
    return uid


def test_routes_need_a_login(client):
    for path, method in (("/api/team/inbox", "get"),
                         ("/api/team/messages/1", "get"),
                         ("/api/team/messages", "post")):
        resp = getattr(client, method)(path, json={})
        assert resp.status_code in (401, 403), path


def test_send_and_read_a_thread_end_to_end(client, db_path):
    rid = _restaurant(db_path)
    erik_id = _login_session(client, db_path, rid, "erik")
    dana_id = _user(db_path, rid, "dana")

    resp = client.post("/api/team/messages",
                       json={"recipient_id": dana_id, "body": "close early tonight?"})
    assert resp.get_json()["ok"] is True

    inbox = client.get("/api/team/inbox").get_json()
    assert inbox["ok"] is True
    assert inbox["teammates"][0]["username"] == "dana"
    assert inbox["teammates"][0]["last_from_me"] is True

    thread = client.get(f"/api/team/messages/{dana_id}").get_json()
    assert thread["ok"] is True
    assert thread["messages"][0]["body"] == "close early tonight?"


def test_cannot_message_someone_outside_the_restaurant(client, db_path):
    rid = _restaurant(db_path, "Simple EJ's")
    other_rid = _restaurant(db_path, "Gia Mia")
    _login_session(client, db_path, rid, "erik")
    stranger_id = _user(db_path, other_rid, "stranger")
    resp = client.post("/api/team/messages", json={"recipient_id": stranger_id, "body": "hi"})
    assert resp.status_code == 400


# ── the web panel ────────────────────────────────────────────────────────

def _dashboard_html():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return open(os.path.join(root, "templates", "dashboard.html"), encoding="utf-8").read()


def test_the_header_has_a_messages_icon_and_panel():
    html = _dashboard_html()
    assert 'id="team-msg-btn"' in html
    assert 'id="team-msg-panel"' in html
    assert 'id="team-msg-badge"' in html
    assert "toggleTeamMsgPanel()" in html


def test_the_panel_talks_to_the_real_endpoints():
    html = _dashboard_html()
    assert "fetch('/api/team/inbox')" in html
    assert "fetch('/api/team/messages/'" in html
    assert "fetch('/api/team/messages'," in html
