"""Memory re-audit 9/29/26, workstream R3 — rating_pref (QUALITY-7,
FORGET-4, LOOPS-8).

The answer-length preference read off a login's ratings could not reverse
("short" was tested first, over 180 days of ratings), and it deleted itself,
unarchived, once its "too long" notes aged out — usually because it worked.
Now a kept preference is judged only on the ratings since it was derived:
the opposite side outnumbering it replaces it, the answers it avoids rated
helpful retires it (archived, shown in Account), and nothing else removes it.
"""
import pytest

import models
import owner_memory
from models import Restaurant, create_restaurant

OWNER = {"id": 1, "role": "client", "is_admin": 0, "username": "erik"}


@pytest.fixture(autouse=True)
def _db(monkeypatch, db_path):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


def _rid():
    return create_restaurant(Restaurant(name="Pref Co", owner_email="pref@x.test"))


def _rate(rid, helpful, note=None, depth="executive", uid=1):
    cid = models.create_ask_conversation(rid, user_id=uid)
    models.save_ask_message(rid, "user", "why is labor high", user_id=uid, conversation_id=cid)
    models.save_ask_message(rid, "assistant", "Labor ran 31.4%.", user_id=uid, conversation_id=cid,
                            meta={"depth": depth, "topic": "labor"})
    mid = models.latest_ask_answer_id(rid, cid, user_id=uid)
    models.record_ask_feedback(rid, mid, helpful, note, user_id=uid)


def _age_everything(days):
    """Move every rating and the derived fact `days` into the past."""
    conn = models.get_conn()
    conn.execute("UPDATE ask_feedback SET updated_at=datetime('now', ?)", (f"-{days} days",))
    conn.execute("UPDATE ask_memory SET created_at=datetime('now', ?), confirmed_at=NULL", (f"-{days} days",))
    conn.commit()
    conn.close()


def test_newer_explain_more_ratings_reverse_a_short_preference():
    """QUALITY-7 / t1_rating_pref.py: "rid2 after 6 'more detail' ratings: short"."""
    rid = _rid()
    for _ in range(3):
        _rate(rid, False, "too long")
    assert owner_memory.derive_rating_preferences(rid, OWNER)["preference"] == "short"
    for _ in range(6):
        _rate(rid, False, "not enough, explain more", depth="standard")
    out = owner_memory.derive_rating_preferences(rid, OWNER)
    assert out["preference"] == "full"
    assert owner_memory.rating_preference(rid, 1) == "full"
    gone = models.get_ask_memory_archive(rid)
    assert gone[0]["reason"] == "replaced" and gone[0]["fact"].startswith("Prefers short")


def test_a_preference_is_not_deleted_when_its_evidence_ages_out():
    """FORGET-4 / LOOPS-8: a helpful rating of a short answer, 200 days on,
    deleted the preference (unarchived) and answers went long again."""
    rid = _rid()
    for _ in range(3):
        _rate(rid, False, "Too long — just give me the number")
    owner_memory.derive_rating_preferences(rid, OWNER)
    _age_everything(200)
    _rate(rid, True, depth="standard")                       # the preference working
    out = owner_memory.derive_rating_preferences(rid, OWNER)
    assert out["preference"] == "short"
    assert owner_memory.rating_preference(rid, 1) == "short"
    fact = next(f for f in models.get_ask_memory(rid) if f.get("origin") == "ratings")
    assert fact["confirmed_at"], "the helpful rating stamps it as still supported"


def test_helpful_long_answers_retire_it_into_the_archive_with_a_reason():
    rid = _rid()
    for _ in range(3):
        _rate(rid, False, "too long")
    owner_memory.derive_rating_preferences(rid, OWNER)
    _age_everything(30)
    for _ in range(4):
        _rate(rid, True, depth="executive")
    assert owner_memory.derive_rating_preferences(rid, OWNER) is None
    assert owner_memory.rating_preference(rid, 1) is None
    view = owner_memory.account_view(rid, OWNER)
    left = next(a for a in view["archived"] if a["fact"].startswith("Prefers short"))
    assert left["reason"] == "contradicted" and left["reason_label"] == "your newer ratings no longer asked for it"


def test_the_forget_marker_is_found_however_busy_the_archive_is():
    """QUALITY-7: the forgotten guard read only the newest 200 archive rows."""
    rid = _rid()
    for _ in range(3):
        _rate(rid, False, "too long")
    fact = owner_memory.derive_rating_preferences(rid, OWNER)["fact"]
    owner_memory.forget(rid, fact, user=OWNER)
    conn = models.get_conn()
    for i in range(260):
        conn.execute("INSERT INTO ask_memory_archive (restaurant_id, fact, reason, archived_at) "
                     "VALUES (?, ?, 'evicted', datetime('now', '+1 minute'))", (rid, f"old note {i}"))
    conn.commit()
    conn.close()
    assert owner_memory.derive_rating_preferences(rid, OWNER) is None
    assert owner_memory.rating_preference(rid, 1) is None
