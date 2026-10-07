"""Memory audit 9/29/26 (workstream M2, conversations): Ask remembers a chat
beyond the 12 turns it replays, and the chats before it.

  * a rolling summary of the turns that scrolled out, validated line by
    line against those turns, replayed as the first context block;
  * the tool calls each answer made, stored with it and replayed;
  * the last answer uncut;
  * chats capped per login, an evicted chat kept in ask_topics;
  * read_past_conversations over this login's own chats and ask_topics,
    and a "questions you often ask" line.
"""
import json
import types

import pytest

import ask_cavnar
import ask_cavnar_tools as tools
import ask_conversations as conv
import auth
import models
from models import Restaurant, create_restaurant, get_restaurant


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


def _rid():
    return create_restaurant(Restaurant(name="Chat Co", owner_email="chat@x.test"))


OWNER = {"id": 1, "role": "client", "is_admin": 0, "username": "erik"}
MANAGER = {"id": 2, "role": "manager", "is_admin": 0, "username": "dana"}


def _chat(rid, turns, user_id=1, cid=None):
    """Turns in chat `cid`, or in a NEW chat of this login's."""
    if cid is None:
        cid = models.create_ask_conversation(rid, user_id=user_id)
    for role, text in turns:
        cid = models.save_ask_message(rid, role, text, user_id=user_id, conversation_id=cid)
    return cid


def _long_chat(rid, user_id=1):
    turns = [("user", "I want labor under 26% by December"),
             ("assistant", "Labor ran 31.4% last week. Option 1: trim Tuesday lunch. Option 2: move a server "
                           "from Monday to Friday. Option 3: cap overtime at 5 hours.")]
    for i in range(9):
        turns += [("user", f"question {i}"), ("assistant", f"answer {i}")]
    return _chat(rid, turns, user_id=user_id)


# ── the rolling summary ─────────────────────────────────────────────────────

def test_turns_that_scroll_out_are_summarised_validated_and_replayed_first(monkeypatch):
    rid = _rid()
    cid = _long_chat(rid)
    calls = []

    def fake_call(restaurant_id, previous, turns):
        calls.append([t["content"] for t in turns])
        return json.dumps({"aim": "Labor under 26% by December",
                           "figures": ["Labor ran 31.4% last week", "Food cost ran 44% in August"],
                           "proposals": ["Option 2: move a server from Monday to Friday"],
                           "decisions": [], "open_questions": ["Which option to take"]})
    monkeypatch.setattr(conv, "_summarize_call", fake_call)
    out = conv.maybe_summarize(rid, cid, user_id=1)
    assert calls and "I want labor under 26% by December" in calls[0][0]
    assert out["proposals"] == ["Option 2: move a server from Monday to Friday"]
    assert out["figures"] == ["Labor ran 31.4% last week"], "a figure the turns never held is dropped"
    # nothing new has scrolled out: no second call
    assert conv.maybe_summarize(rid, cid, user_id=1) is None and len(calls) == 1
    # ...and it is a context block of the next turn, fenced
    captured = {}
    monkeypatch.setattr(ask_cavnar, "create_with_retry", lambda client, **kw: captured.update(kw) or types.SimpleNamespace(
        content=[types.SimpleNamespace(type="text", text="ok")], stop_reason="end_turn"))
    ask_cavnar.ask_with_tools(get_restaurant(rid), "what was option 2?", history=[], user=OWNER, conversation_id=cid)
    blocks = captured["system"]
    assert "cache_control" in blocks[0]
    # Per turn, so after the cached snapshot and uncached (AI cost audit
    # 10/7/26 #30: it was the first block after the static rules, which put
    # a per-turn text in front of the snapshot's cache breakpoint).
    assert "Restaurant:" in blocks[1]["text"] and "cache_control" in blocks[1]
    chat = [b for b in blocks if "Option 2: move a server" in b["text"]]
    assert len(chat) == 1 and blocks.index(chat[0]) > 1 and "cache_control" not in chat[0]
    assert "Restaurant:" not in chat[0]["text"]
    import ai_guard
    assert ai_guard.UNTRUSTED_OPEN in chat[0]["text"]


def test_another_logins_chat_summary_never_reaches_this_one(monkeypatch):
    rid = _rid()
    cid = _long_chat(rid, user_id=1)
    monkeypatch.setattr(conv, "_summarize_call", lambda *a: json.dumps({"aim": "Sell the patio furniture"}))
    conv.maybe_summarize(rid, cid)
    import memory_context
    mine = memory_context.memory_context(rid, "ask_conversation", viewer=OWNER, subjects=[f"conversation:{cid}"])
    theirs = memory_context.memory_context(rid, "ask_conversation", viewer=MANAGER, subjects=[f"conversation:{cid}"])
    assert "Sell the patio furniture" in mine.text and "patio furniture" not in theirs.text


def test_a_failed_summary_call_never_breaks_the_answer(monkeypatch):
    rid = _rid()
    cid = _long_chat(rid)
    monkeypatch.setattr(conv, "_summarize_call", lambda *a: 1 / 0)
    assert conv.maybe_summarize(rid, cid) is None


# ── what the last answer read, and the last answer uncut ────────────────────

def test_an_answer_keeps_its_tool_calls_and_the_next_turn_is_told_them():
    rid = _rid()
    cid = _chat(rid, [("user", "how's labor")])
    models.save_ask_message(rid, "assistant", "Labor ran 31.4%.", user_id=1, conversation_id=cid,
                            tools=[{"name": "read_labor_detail", "input": {"weeks": 8}}],
                            meta={"depth": "standard", "tools_used": ["read_labor_detail"], "topic": "labor"})
    hist = models.get_ask_history(rid, conversation_id=cid, with_tools=True)
    assert hist[-1]["tools"] == [{"name": "read_labor_detail", "input": {"weeks": 8}}]
    assert "tools" not in models.get_ask_history(rid, conversation_id=cid)[-1], "never sent to a client"
    import memory_context
    text = memory_context.memory_context(rid, "ask_conversation", viewer=OWNER, subjects=[f"conversation:{cid}"]).text
    assert 'read_labor_detail {"weeks": 8}' in text


def test_the_last_answer_replays_in_full_and_older_ones_are_cut():
    long_answer = "x" * 9000
    history = [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "y" * 9000},
               {"role": "user", "content": "q2"}, {"role": "assistant", "content": long_answer}]
    out = ask_cavnar._sanitize_history(history)
    # An older answer is cut to _MAX_OLDER_ANSWER_LENGTH (800) since the AI
    # cost audit of 10/7/26 (#66) — it was _MAX_HISTORY_TURN_LENGTH (2,400),
    # resent on every call of every later question.
    assert len(out[1]["content"]) == ask_cavnar._MAX_OLDER_ANSWER_LENGTH == 800
    assert out[3]["content"] == long_answer


# ── per-login caps, and what an evicted chat leaves ─────────────────────────

def test_a_managers_chats_never_evict_the_owners_and_an_evicted_chat_is_kept_in_topics(monkeypatch):
    rid = _rid()
    monkeypatch.setattr(models, "_ASK_CONVERSATIONS_KEEP", 3)
    owner_first = models.create_ask_conversation(rid, user_id=1)
    _chat(rid, [("user", "why was labor high last week"), ("assistant", "Labor ran 31.4%.")], cid=owner_first)
    conn = models.get_conn()
    conn.execute("UPDATE ask_cavnar_conversations SET topics='labor' WHERE id=?", (owner_first,))
    conn.execute("UPDATE ask_cavnar_conversations SET updated_at=datetime('now', '-1 day') WHERE id=?",
                 (owner_first,))
    conn.commit()
    conn.close()
    for i in range(5):
        c = models.create_ask_conversation(rid, user_id=2)
        _chat(rid, [("user", f"manager q{i}")], user_id=2, cid=c)
    assert models.get_ask_conversation(rid, owner_first) is not None, "the manager's chats evict only theirs"
    for i in range(3):
        c = models.create_ask_conversation(rid, user_id=1)
        _chat(rid, [("user", f"owner q{i}")], cid=c)
    assert models.get_ask_conversation(rid, owner_first) is None
    conn = models.get_conn()
    row = conn.execute("SELECT * FROM ask_topics WHERE conversation_id=?", (owner_first,)).fetchone()
    conn.close()
    assert row["title"] == "why was labor high last week" and row["topics"] == "labor" and row["user_id"] == 1


# ── past chats, and what this person keeps asking ───────────────────────────

def test_read_past_conversations_finds_an_earlier_chat_by_its_words_for_its_own_login_only(monkeypatch):
    rid = _rid()
    old = _long_chat(rid)
    monkeypatch.setattr(conv, "_summarize_call", lambda *a: json.dumps({
        "proposals": ["Option 2: move a server from Monday to Friday"]}))
    conv.maybe_summarize(rid, old)
    other = _chat(rid, [("user", "what's the weather")])
    view = tools.viewer_restaurant(get_restaurant(rid), OWNER)
    view._ask_conversation_id = other
    out = json.loads(tools.run_read_tool("read_past_conversations", rid, {"query": "option server friday"},
                                         restaurant=view))
    assert out["count"] == 1 and out["conversations"][0]["conversation_id"] == old
    assert "Option 2" in out["conversations"][0]["notes"]
    mgr = tools.viewer_restaurant(get_restaurant(rid), MANAGER)
    assert json.loads(tools.run_read_tool("read_past_conversations", rid, {"query": "option"},
                                          restaurant=mgr))["count"] == 0


def test_a_short_chat_with_no_notes_offers_its_last_answer():
    rid = _rid()
    _chat(rid, [("user", "give me three ways to cut tuesday labor"),
                ("assistant", "## Option 1\nTrim lunch.\n\n## Option 2\nMove a server.")])
    rows = conv.past_conversations(rid, 1, query="tuesday labor")
    assert "Move a server" in rows[0]["excerpt"]


def test_the_questions_this_person_often_asks_reach_their_turn():
    rid = _rid()
    for i in range(4):
        c = _chat(rid, [("user", f"labor question {i}")])
        conn = models.get_conn()
        conn.execute("UPDATE ask_cavnar_conversations SET topics=? WHERE id=?",
                     ("labor,reviews" if i % 2 else "labor", c))
        conn.commit()
        conn.close()
    line = conv.often_asks_line(rid, 1)
    assert "labor (4 chats)" in line["text"] and "reviews (2 chats)" in line["text"]
    assert conv.often_asks_line(rid, 2) is None, "another login's chats are not theirs"


def test_answer_topics_come_from_what_it_read_then_the_words():
    assert conv.answer_topic("anything", ["labor"]) == "labor"
    assert conv.answer_topic("how many 1-star reviews", []) == "reviews"
    assert conv.answer_topic("hello there", None) == "general"
