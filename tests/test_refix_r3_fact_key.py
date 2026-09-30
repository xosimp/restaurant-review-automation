"""Memory re-audit 9/29/26, workstream R3 — fact_text_key (QUALITY-6,
INVENTORY-3, PEOPLE-5, PROMPTS-13).

ask_memory was UNIQUE(restaurant_id, fact): the same sentence from another
login took the fact over — its author, audience, modules and dates — and two
logins' rating-derived preferences flipped between them. Now a fact is keyed
by its text AND its author; a restatement never widens its audience; another
login saying words it can read is a confirmation stamped on the original.
"""
import sqlite3

import pytest

import models
import owner_memory
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(monkeypatch, db_path):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


def _rid():
    return create_restaurant(Restaurant(name="Key Co", owner_email="key@x.test"))


def _owner(rid, uid=101):
    return {"id": uid, "restaurant_id": rid, "is_admin": 0, "role": "client", "username": "erik"}


def _manager(rid, uid=202):
    return {"id": uid, "restaurant_id": rid, "is_admin": 0, "role": "manager", "username": "dana"}


def _rows(rid, text):
    return [f for f in models.get_ask_memory(rid) if f["fact"] == text]


def test_a_manager_repeating_the_owners_private_fact_never_takes_it_over():
    """QUALITY-6 (a) / t8_collide.py: it became the manager's, team-visible,
    for marketing, expiring on the manager's date."""
    rid = _rid()
    text = "Football Sundays start 10/5"
    owner_memory.remember(rid, text, kind="context", modules=["labor"], audience="principals", user=_owner(rid))
    owner_memory.remember(rid, text, kind="context", modules=["marketing"], valid_until="2099-10-06",
                          user=_manager(rid))
    rows = {f["user_id"]: f for f in _rows(rid, text)}
    own = rows[101]
    assert (own["audience"], own["author_label"], own["modules"], own["valid_until"]) == \
        ("principals", "Erik, owner", "labor", None)
    # the manager's own words are their own row — never wider than the owner's
    assert rows[202]["audience"] == "principals" and rows[202]["author_label"] == "Dana, manager"


def test_restating_a_fact_you_can_read_is_a_confirmation_not_a_takeover():
    """PROMPTS-13: the restater is recorded as confirmed_by."""
    rid = _rid()
    text = "Never cut the host on Fridays"
    owner_memory.remember(rid, text, kind="constraint", user=_owner(rid))
    out = owner_memory.remember(rid, text, kind="context", audience="team", user=_manager(rid))
    assert out["confirmed"] is True
    rows = _rows(rid, text)
    assert len(rows) == 1
    r = rows[0]
    assert r["user_id"] == 101 and r["kind"] == "constraint" and r["author_label"] == "Erik, owner"
    assert r["confirmed_by"] == "Dana, manager" and r["confirmed_at"]
    view = owner_memory.account_view(rid, _owner(rid))
    f = next(f for f in view["facts"] if f["fact"] == text)
    assert f["confirmed_by"] == "Dana, manager" and f["confirmed_on"]


def test_the_same_author_restating_never_widens_the_audience():
    rid = _rid()
    text = "We're selling the restaurant next spring"
    owner_memory.remember(rid, text, audience="principals", user=_owner(rid))
    owner_memory.remember(rid, text, audience="team", user=_owner(rid))
    assert [f["audience"] for f in _rows(rid, text)] == ["principals"]


def test_two_logins_keep_their_own_rating_preference():
    """QUALITY-6 (b), INVENTORY-3, PEOPLE-5: the owner's preference went to
    None when the manager derived the identical sentence."""
    rid = _rid()
    for uid in (101, 202):
        for _ in range(3):
            cid = models.create_ask_conversation(rid, user_id=uid)
            models.save_ask_message(rid, "user", "why", user_id=uid, conversation_id=cid)
            models.save_ask_message(rid, "assistant", "Labor ran 31%.", user_id=uid, conversation_id=cid,
                                    meta={"depth": "executive"})
            mid = models.latest_ask_answer_id(rid, cid, user_id=uid)
            models.record_ask_feedback(rid, mid, False, "too long", user_id=uid)
    owner_memory.derive_rating_preferences(rid, _owner(rid))
    owner_memory.derive_rating_preferences(rid, _manager(rid))
    assert owner_memory.rating_preference(rid, 101) == "short"
    assert owner_memory.rating_preference(rid, 202) == "short"
    owner_memory.derive_rating_preferences(rid, _owner(rid))
    assert owner_memory.rating_preference(rid, 202) == "short", "never flips back and forth"


def test_forgetting_drops_only_this_logins_row():
    rid = _rid()
    text = "Patio opens in April"
    owner_memory.remember(rid, text, audience="principals", user=_owner(rid))
    owner_memory.remember(rid, text, user=_manager(rid))                  # can't read the owner's: own row
    assert owner_memory.forget(rid, text, user=_manager(rid)) == {"forgotten": text}
    assert [f["user_id"] for f in _rows(rid, text)] == [101]


def test_an_old_text_keyed_table_is_rebuilt_at_boot_with_its_rows(tmp_path):
    """The migration: the table-level UNIQUE(restaurant_id, fact) cannot be
    dropped in SQLite, so init_ask_memory rebuilds the table once."""
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE ask_memory (
        id INTEGER PRIMARY KEY AUTOINCREMENT, restaurant_id INTEGER NOT NULL, fact TEXT NOT NULL,
        kind TEXT, source TEXT, user_id INTEGER, created_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(restaurant_id, fact))""")
    conn.execute("ALTER TABLE ask_memory ADD COLUMN audience TEXT")
    conn.execute("INSERT INTO ask_memory (restaurant_id, fact, kind, user_id, audience) "
                 "VALUES (1, 'Closed Mondays', 'context', 5, 'team')")
    conn.commit()
    conn.close()
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    models._migrate_ask_memory_key(c)
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_ask_memory_fact_author "
              "ON ask_memory(restaurant_id, fact, COALESCE(user_id, 0))")
    assert not models._ask_memory_has_text_key(c)
    assert [tuple(r) for r in c.execute("SELECT restaurant_id, fact, user_id, audience FROM ask_memory")] == \
        [(1, "Closed Mondays", 5, "team")]
    c.execute("INSERT INTO ask_memory (restaurant_id, fact, user_id) VALUES (1, 'Closed Mondays', 6)")
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO ask_memory (restaurant_id, fact, user_id) VALUES (1, 'Closed Mondays', 5)")
    c.close()
