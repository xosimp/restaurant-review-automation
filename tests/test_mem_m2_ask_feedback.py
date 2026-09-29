"""Memory audit 9/29/26 (workstream M2, ask_feedback): a rating teaches
something specific. Each carries what the rated answer was (depth, tools,
modules, verdict, topic); it is read per login; its pattern becomes a
visible, forgettable preference and the depth Ask chooses; and the answer's
own accuracy is read from how answers on its topic have been rated.
"""
import types

import pytest

import ask_cavnar
import auth
import models
import owner_memory
from models import Restaurant, create_restaurant, get_restaurant

OWNER = {"id": 1, "role": "client", "is_admin": 0, "username": "erik"}
MANAGER = {"id": 2, "role": "manager", "is_admin": 0, "username": "dana"}


@pytest.fixture(autouse=True)
def _db(monkeypatch, db_path):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(auth, "get_conn", lambda *a, **k: real(db_path), raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


def _rid():
    return create_restaurant(Restaurant(name="Rate Co", owner_email="rate@x.test"))


def _answer(rid, user_id=1, depth="executive", topic="labor", text="Labor ran 31.4%."):
    cid = models.create_ask_conversation(rid, user_id=user_id)
    models.save_ask_message(rid, "user", "why is labor high", user_id=user_id, conversation_id=cid)
    models.save_ask_message(rid, "assistant", text, user_id=user_id, conversation_id=cid,
                            tools=[{"name": "read_labor_detail", "input": {}}],
                            meta={"depth": depth, "tools_used": ["read_labor_detail"], "modules_consulted": ["labor"],
                                  "verdict": "pass", "confidence_pct": 64, "topic": topic})
    return models.latest_ask_answer_id(rid, cid, user_id=user_id)


def test_a_rating_carries_what_the_answer_was():
    rid = _rid()
    mid = _answer(rid)
    row = models.record_ask_feedback(rid, mid, False, "too long", user_id=1)
    assert row["depth"] == "executive" and row["topic"] == "labor"
    stored = models.ask_feedback_rows(rid, user_id=1)[0]
    assert stored["tools"] == ["read_labor_detail"] and stored["modules"] == ["labor"]
    assert stored["verdict"] == "pass" and stored["confidence_pct"] == 64


def test_feedback_is_read_per_login():
    rid = _rid()
    models.record_ask_feedback(rid, _answer(rid, user_id=1), True, user_id=1)
    models.record_ask_feedback(rid, _answer(rid, user_id=2), False, "useless", user_id=2)
    mine = models.ask_feedback_summary(rid, user_id=1)
    assert (mine["rated"], mine["helpful"], mine["notes"]) == (1, 1, [])
    owner_view = ask_cavnar._feedback_context(rid, viewer=OWNER)
    assert "1 of 1 answers rated helpful" in owner_view and "useless" not in owner_view
    assert "useless" in ask_cavnar._feedback_context(rid, viewer=MANAGER)


def test_three_too_long_ratings_become_a_visible_preference_and_the_depth_follows():
    rid = _rid()
    for _ in range(3):
        models.record_ask_feedback(rid, _answer(rid), False, "Too long — just give me the number", user_id=1)
    pref = owner_memory.derive_rating_preferences(rid, OWNER)
    assert pref["preference"] == "short"
    fact = next(f for f in models.get_ask_memory(rid) if f.get("origin") == "ratings")
    assert fact["audience"] == "author" and fact["user_id"] == 1 and fact["author_label"] == "From their own ratings"
    assert owner_memory.rating_preference(rid, 1) == "short"
    assert owner_memory.rating_preference(rid, 2) is None, "the manager's answers are theirs"
    assert ask_cavnar._depth_for("why is labor high?", prefer="short") == "standard"
    assert ask_cavnar._depth_for("why is labor high?") == "executive"
    # the turn carries the note, outside the cached block
    captured = {}
    import ask_cavnar as ac
    orig = ac.create_with_retry
    ac.create_with_retry = lambda client, **kw: captured.update(kw) or types.SimpleNamespace(
        content=[types.SimpleNamespace(type="text", text="ok")], stop_reason="end_turn")
    try:
        ac.ask_with_tools(get_restaurant(rid), "why is labor high?", user=OWNER)
    finally:
        ac.create_with_retry = orig
    assert any("ANSWER LENGTH" in b["text"] for b in captured["system"][1:])
    assert "ANSWER LENGTH" not in captured["system"][0]["text"]


def test_a_forgotten_rating_preference_is_not_put_straight_back():
    rid = _rid()
    for _ in range(3):
        models.record_ask_feedback(rid, _answer(rid), False, "too long", user_id=1)
    fact = owner_memory.derive_rating_preferences(rid, OWNER)["fact"]
    assert owner_memory.forget(rid, fact, user=OWNER) == {"forgotten": fact}
    assert owner_memory.derive_rating_preferences(rid, OWNER) is None
    assert owner_memory.rating_preference(rid, 1) is None
    archived = models.get_ask_memory_archive(rid)
    assert archived and archived[0]["reason"] == "forgotten"


def test_the_answers_accuracy_is_read_from_how_answers_on_its_topic_were_rated():
    rid = _rid()
    for i in range(6):
        models.record_ask_feedback(rid, _answer(rid), i < 5, user_id=1)
    conf = ask_cavnar._answer_confidence("Labor ran 31.4%.", ["snapshot"], ["read_labor_detail"], ["labor"], [], rid,
                                         topic="labor", viewer_id=1)
    acc = conf["dimensions"]["accuracy"]
    assert acc["source"] == "ratings" and acc["n"] == 6 and acc["improved"] == 5
    assert acc["basis"] == "rated helpful 5 of 6 times on labor questions (your ratings)"
    assert acc["pct"] is not None
    # another topic has no record of its own yet
    other = ask_cavnar._answer_confidence("Food cost is 31%.", ["snapshot"], ["read_food_cost"], ["food cost"], [],
                                          rid, topic="food", viewer_id=1)
    assert other["dimensions"]["accuracy"].get("source") != "ratings"
