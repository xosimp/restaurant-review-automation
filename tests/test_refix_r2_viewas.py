"""Memory re-audit fix round 9/29/26, workstream R2 — what an admin does and
reads through view-as is support's, never the owner's own history.

  PEOPLE-7   Ask chats, notes, topics and ratings through view-as are filed
             under the admin behind it (a support thread marked 'view_as'):
             never the owner's history, "often asks" or past chats, and a
             support rating never replaces the owner's own.
  PEOPLE-15  a rating says whose judgement it is (login, authority, via); an
             admin's is kept and not counted in the Operational Score.
  PEOPLE-20  support through view-as does not read the owner's author-only
             memory or their chats, and reading Account memory or Ask history
             is recorded as "view_as_read".
"""
import json

import pytest
from flask import Flask, g

import admin_routes
import ask_conversations as conv
import auth
import auth_routes
import client_api
import memory_context
import models
import owner_memory
from auth import create_session, create_user, init_auth, upsert_membership
from models import Restaurant, create_restaurant

CSRF = "refix-r2-view-as"


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


def _rid(name="View Co"):
    return create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test"))


def _view_as(rid, uid=11, admin_id=99, role="support"):
    return {"id": uid, "restaurant_id": rid, "is_admin": 0, "role": "client", "username": "erik",
            "acting_admin_id": admin_id, "acting_admin": "sam", "acting_admin_role": role,
            "device_type": "admin-view-as"}


OWNER = {"id": 11, "role": "client", "is_admin": 0, "username": "erik"}


# ── PEOPLE-7: a view-as chat is support's own thread ────────────────────────

def test_the_ask_identity_is_the_admin_behind_a_view_as():
    from permissions import acting_login_id
    assert acting_login_id(_view_as(1)) == 99
    assert acting_login_id(OWNER) == 11
    assert acting_login_id({"id": 11, "device_type": "admin-view-as"}) == -1   # no admin named: nobody's
    assert client_api._ask_uid(_view_as(1)) == 99


def test_a_view_as_chat_never_reaches_the_owners_history_or_often_asks():
    rid = _rid()
    # The owner's own chat.
    own = models.create_ask_conversation(rid, user_id=11)
    models.save_ask_message(rid, "user", "How did labor do?", user_id=11, conversation_id=own)
    # Support's chats through view-as: filed under the admin, marked view_as.
    app = Flask(__name__)
    with app.test_request_context("/api/ask-cavnar"):
        g.view_as = {"acting_admin_id": 99, "acting_admin": "sam", "acting_admin_role": "support"}
        for i in range(4):
            cid = models.create_ask_conversation(rid, user_id=client_api._ask_uid(_view_as(rid)))
            models.save_ask_message(rid, "user", f"Labor question {i}", user_id=99, conversation_id=cid)
            models.save_ask_message(rid, "assistant", "Labor ran 30%.", user_id=99, conversation_id=cid,
                                    meta={"modules_consulted": ["labor"]})
    row = models.get_conn().execute("SELECT user_id, via FROM ask_cavnar_conversations WHERE id=?",
                                    (cid,)).fetchone()
    assert row["user_id"] == 99 and row["via"] == "view_as"
    owners = models.list_ask_conversations(rid, viewer_id=11)
    assert [c["id"] for c in owners] == [own]
    assert conv.often_asks(rid, 11) == {}, "support's labor chats are not what the owner often asks"
    assert all(p["conversation_id"] == own for p in conv.past_conversations(rid, 11))
    # ...and support reads its own thread, never the owner's chat.
    assert own not in [c["id"] for c in models.list_ask_conversations(rid, viewer_id=99)]


def test_a_support_rating_never_replaces_the_owners(db_path):
    import strategy_routes
    rid = _rid()
    cid = models.create_ask_conversation(rid, user_id=11)
    models.save_ask_message(rid, "user", "q", user_id=11, conversation_id=cid)
    models.save_ask_message(rid, "assistant", "a", user_id=11, conversation_id=cid)
    mid = models.latest_ask_answer_id(rid, cid, user_id=11)
    assert models.record_ask_feedback(rid, mid, True, user_id=11, authority="principal")
    # Support in view-as rates the same answer: filed under the admin, and
    # refused (it is not support's answer) — the owner's rating stands.
    app = Flask(__name__)
    with app.test_request_context("/mobile/api/ask-cavnar/feedback", method="POST",
                                  json={"message_id": mid, "helpful": False}):
        body, status = strategy_routes._do_ask_feedback(_view_as(rid))
    assert status == 404
    rows = models.get_conn().execute("SELECT user_id, helpful, authority FROM ask_feedback WHERE message_id=?",
                                     (mid,)).fetchall()
    assert [(r["user_id"], r["helpful"], r["authority"]) for r in rows] == [(11, 1, "principal")]


# ── PEOPLE-15: ratings say whose judgement they are ─────────────────────────

def test_a_view_as_rating_is_supports_and_not_in_the_operational_score():
    rid = _rid()
    models.set_capability(rid, "Maria", score=4, updated_by="erik", user=OWNER)
    models.set_capability(rid, "Jon", score=2, updated_by="support:sam", user=_view_as(rid))
    caps = models.get_capabilities(rid)
    assert caps["Maria"]["overall"]["authority"] == "principal"
    assert caps["Jon"]["overall"]["authority"] == "admin" and caps["Jon"]["overall"]["via"] == "view_as"
    row = models.get_conn().execute("SELECT updated_by_user_id FROM staff_capabilities WHERE employee_name='Jon'"
                                    ).fetchone()
    assert row["updated_by_user_id"] == 99
    assert models.get_operational_scores(rid) == {"Maria": 4}
    # The owner rating Jon makes it count.
    models.set_capability(rid, "Jon", score=3, updated_by="erik", user=OWNER)
    assert models.get_operational_scores(rid) == {"Maria": 4, "Jon": 3}


def test_a_capability_change_through_view_as_names_the_admin_as_data():
    rid = _rid()
    app = Flask(__name__)
    with app.test_request_context("/mobile/api/labor/team/rating", method="POST"):
        g.cavnar_current_user = _view_as(rid)
        g.view_as = {"acting_admin_id": 99, "acting_admin": "sam", "acting_admin_role": "support"}
        models.record_capability_change(rid, "rating", subject="Jon · overall", before=None, after={"score": 2},
                                        changed_by="erik")
    row = models.get_conn().execute("SELECT changed_by, changed_by_user_id, authority, via FROM capability_changes "
                                    "WHERE restaurant_id=?", (rid,)).fetchone()
    assert row["changed_by_user_id"] == 99 and row["authority"] == "admin" and row["via"] == "view_as"
    assert row["changed_by"].startswith("support:")


# ── PEOPLE-20: support does not read the owner's private memory ─────────────

def test_view_as_reads_no_author_only_line_of_the_owner():
    line = {"audience": "author", "author_id": 11, "text": "my private note"}
    assert memory_context.visible(line, OWNER)
    assert not memory_context.visible(line, _view_as(1))
    assert not memory_context.visible(line, _view_as(1, role="admin"))
    # Principals-level lines stay readable to support, as before.
    assert memory_context.visible({"audience": "principals", "author_id": 11}, _view_as(1))


def test_a_fact_support_adds_through_view_as_is_never_the_owners():
    a = owner_memory.author_of(_view_as(1))
    assert a["user_id"] == 99 and a["authority"] == "admin" and a["label"] == "Cavnar AI support"


def test_ask_conversation_memory_through_view_as_is_supports_own():
    rid = _rid()
    own = models.create_ask_conversation(rid, user_id=11)
    for i in range(3):
        c = models.create_ask_conversation(rid, user_id=11)
        models.save_ask_message(rid, "user", f"Labor {i}", user_id=11, conversation_id=c)
        models.save_ask_message(rid, "assistant", "ok", user_id=11, conversation_id=c,
                                meta={"modules_consulted": ["labor"]})
    req = type("R", (), {"viewer": OWNER, "restaurant_id": rid, "subjects": (), "db_path": None})()
    assert any("most often asks" in l["text"] for l in conv.memory_lines(req))
    req.viewer = _view_as(rid)
    assert not any("most often asks" in l["text"] for l in conv.memory_lines(req))
    # The owner's chat is not one support may open by id either.
    req.subjects = (f"conversation:{own}",)
    assert conv.memory_lines(req) == []


def _world(db_path):
    hq = create_restaurant(Restaurant(name="Cavnar AI Admin", owner_email="w@x.test"), db_path=db_path)
    actor = create_user(hq, "sup", "sup@x.test", "Support-pass-2026", role="support", db_path=db_path)
    rid = create_restaurant(Restaurant(name="Client Grill", owner_email="owner@client.test"), db_path=db_path)
    owner = create_user(rid, "owner", "owner@client.test", "Owner-pass-2026", db_path=db_path)
    upsert_membership(owner, rid, "client", db_path=db_path)
    return actor, rid, owner


def test_reading_ask_history_through_view_as_is_recorded(db_path):
    app = Flask(__name__, template_folder="../templates")
    app.secret_key = "refix-r2"
    for bp in (auth_routes.auth_bp, admin_routes.admin_bp, client_api.client_bp):
        app.register_blueprint(bp)
    actor, rid, owner = _world(db_path)
    own = models.create_ask_conversation(rid, user_id=owner)
    models.save_ask_message(rid, "user", "What did labor cost?", user_id=owner, conversation_id=own)
    c = app.test_client()
    c.set_cookie("session_token", create_session(actor, db_path=db_path))
    c.set_cookie("csrf_js", CSRF)
    assert c.post("/admin/view-as/%d" % rid, headers={"X-CSRF": CSRF}).status_code == 302
    r = c.get("/api/ask-cavnar/conversations")
    assert r.status_code == 200
    assert r.get_json()["conversations"] == [], "support does not page through the owner's chats"
    rows = [dict(x) for x in models.get_conn(db_path).execute(
        "SELECT * FROM admin_events WHERE event_type='view_as_read'").fetchall()]
    assert rows and rows[-1]["actor_id"] == actor and rows[-1]["restaurant_id"] == rid
    assert json.loads(rows[-1]["after_json"])["path"] == "/api/ask-cavnar/conversations"
    # An ordinary read through view-as is not recorded.
    n = len(rows)
    c.get("/api/ask-cavnar/opening")
    assert len(models.get_conn(db_path).execute(
        "SELECT id FROM admin_events WHERE event_type='view_as_read'").fetchall()) == n
