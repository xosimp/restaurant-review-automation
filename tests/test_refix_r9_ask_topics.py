"""Memory re-audit fix round (9/29/26), R9 — "ask_topics" (INVENTORY-7,
FORGET-15, INVENTORY-14).

  * Clear history and delete-chat also delete what the evicted chats were
    about (ask_topics); the "Recent questions" line and
    read_past_conversations stop quoting what the login cleared.
  * Ownerless legacy rows are read only by an account holder.
  * ask_topics and ask_feedback are on the retention registry (400 days).
  * A rolling chat summary folds the OLDEST scrolled turns first and
    continues from the last one folded.
"""
import json
import sqlite3

import pytest

import ask_conversations as conv
import auth
import models
import ops
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(monkeypatch, db_path):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    import ai_utils
    monkeypatch.setattr(ai_utils, "ai_rate_limited", lambda *a, **k: False)
    yield


def _setup(db_path):
    auth.init_auth(db_path=db_path)
    rid = create_restaurant(Restaurant(name="Topics Co", owner_email="t@x.test"))
    c = sqlite3.connect(db_path)
    c.execute("INSERT INTO users (id, restaurant_id, username, email, password_hash, role) "
              "VALUES (1, ?, 'erik', 'e@x.test', 'x', 'client')", (rid,))
    c.execute("INSERT INTO users (id, restaurant_id, username, email, password_hash, role) "
              "VALUES (2, ?, 'dana', 'd@x.test', 'x', 'manager')", (rid,))
    for uid, title in ((1, "should I let Dana go"), (2, "tuesday lunch labor"), (None, "legacy owner question")):
        for i in range(2):
            c.execute("INSERT INTO ask_topics (restaurant_id, user_id, conversation_id, title, topics, message_count, "
                      "ended_at) VALUES (?, ?, ?, ?, 'labor', 4, datetime('now', '-2 days'))",
                      (rid, uid, 100 + i + (uid or 9) * 10, f"{title} {i}"))
    c.commit()
    c.close()
    return rid


def _titles(db_path, rid, uid):
    live, kept = conv._topics_rows(rid, uid, 90, db_path=db_path)
    return sorted(r["title"] for r in kept)


def test_legacy_rows_are_read_only_by_an_account_holder(db_path):
    rid = _setup(db_path)
    assert any(t.startswith("legacy") for t in _titles(db_path, rid, 1))
    mgr = _titles(db_path, rid, 2)
    assert mgr == ["tuesday lunch labor 0", "tuesday lunch labor 1"], "a manager reads their own only"


def test_clear_history_deletes_the_logins_kept_topics(db_path):
    rid = _setup(db_path)
    models.clear_ask_history(rid, db_path=db_path, viewer_id=2)
    assert _titles(db_path, rid, 2) == []
    # the owner's own and the legacy rows are untouched by the manager's clear
    assert len(_titles(db_path, rid, 1)) == 4
    models.clear_ask_history(rid, db_path=db_path, viewer_id=1)
    assert _titles(db_path, rid, 1) == []
    assert conv.often_asks_line(rid, 1, db_path=db_path) is None
    assert conv.past_conversations(rid, 1, query="Dana", db_path=db_path) == []


def test_delete_chat_deletes_its_kept_topic(db_path):
    rid = _setup(db_path)
    cid = models.create_ask_conversation(rid, user_id=1)
    models.save_ask_message(rid, "user", "hi", user_id=1, conversation_id=cid)
    c = sqlite3.connect(db_path)
    c.execute("INSERT INTO ask_topics (restaurant_id, user_id, conversation_id, title) VALUES (?, 1, ?, 'x')",
              (rid, cid))
    c.commit()
    models.delete_ask_conversation(rid, cid, db_path=db_path, viewer_id=1)
    assert c.execute("SELECT COUNT(*) FROM ask_topics WHERE conversation_id=?", (cid,)).fetchone()[0] == 0


def test_ask_topics_and_feedback_are_on_the_registry(db_path, monkeypatch):
    assert ops.retention_state("ask_topics")["state"] == "ok" and ops._RETENTION_DAYS["ask_topics"] == 400
    assert ops.retention_state("ask_feedback")["state"] == "ok" and ops._RETENTION_DAYS["ask_feedback"] == 400
    rid = _setup(db_path)
    c = sqlite3.connect(db_path)
    c.execute("UPDATE ask_topics SET created_at=datetime('now', '-401 days') WHERE user_id=2")
    c.execute("INSERT INTO ask_feedback (restaurant_id, user_id, message_id, helpful, note, created_at) "
              "VALUES (?, 1, 5, 0, 'about Dana', datetime('now', '-401 days'))", (rid,))
    c.commit()
    out = ops.prune_ledgers(db_path)
    assert out.get("ask_topics") == 2 and out.get("ask_feedback") == 1


def test_a_backlog_is_folded_oldest_first_and_continues(db_path, monkeypatch):
    rid = create_restaurant(Restaurant(name="Backlog Co", owner_email="b@x.test"))
    cid = models.create_ask_conversation(rid, user_id=1)
    for i in range(30):
        models.save_ask_message(rid, "user", f"q{i}", user_id=1, conversation_id=cid)
        models.save_ask_message(rid, "assistant", f"a{i}", user_id=1, conversation_id=cid)
    seen = []
    monkeypatch.setattr(conv, "_summarize_call",
                        lambda rid_, prev, turns: seen.append([t["content"] for t in turns]) or
                        json.dumps({"aim": "x"}))
    conv.maybe_summarize(rid, cid, user_id=1)
    assert seen and seen[0][0] == "q0", "the oldest scrolled turn is folded first"
    assert len(seen[0]) == conv.SUMMARY_MAX_TURNS
    c = sqlite3.connect(db_path)
    through = c.execute("SELECT summary_through_id FROM ask_cavnar_conversations WHERE id=?", (cid,)).fetchone()[0]
    folded_last = c.execute("SELECT id FROM ask_cavnar_messages WHERE conversation_id=? ORDER BY id LIMIT 1 OFFSET ?",
                            (cid, conv.SUMMARY_MAX_TURNS - 1)).fetchone()[0]
    assert through == folded_last, "the notes run through the last turn folded, so the rest come next"
    conv.maybe_summarize(rid, cid, user_id=1)
    assert len(seen) == 2 and seen[1][0] == "q15", "the next pass starts where the last stopped"
