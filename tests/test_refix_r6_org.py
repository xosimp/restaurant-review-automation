"""Person-level learning follows the person across an organisation's
locations, and an owner's fact can be every location's (memory re-audit
9/29/26, PEOPLE-13).

A three-location owner's "prefer short answers" (from their ratings at
location A) did not apply at B or C; their chats and "often asks" split
three ways; "we close every location on Thanksgiving" told at A never
reached B's schedule or C's nightly report.
"""
import pytest

import ask_conversations
import models
import owner_memory
import staff_settings
from models import Restaurant, create_restaurant, update_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    fake = lambda *a, **k: real(db_path)  # noqa: E731
    monkeypatch.setattr(models, "get_conn", fake)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(staff_settings, "get_conn", fake)
    yield


def _group(names=("Downtown", "North")):
    ids = []
    for name in names:
        rid = create_restaurant(Restaurant(name=f"EJ's {name}", owner_email="erik@x.test"))
        update_restaurant(rid, {"location_group": "Simple EJ's", "location_name": name})
        ids.append(rid)
    return ids


OWNER = {"id": 1, "role": "owner", "is_admin": 0, "username": "erik"}
MANAGER = {"id": 2, "role": "manager", "is_admin": 0, "username": "gm"}


def _req(rid, surface="schedule"):
    class R:
        pass
    r = R()
    r.restaurant_id, r.surface, r.db_path, r.subjects, r.viewer = rid, surface, None, [], None
    return r


def test_an_owners_fact_for_every_location_reaches_the_others_and_only_they_may_set_it():
    a, b = _group()
    lone = create_restaurant(Restaurant(name="Elsewhere", owner_email="z@x.test"))
    owner_memory.remember(a, "We close every location on Thanksgiving", kind="constraint", user=OWNER, scope="org")
    owner_memory.remember(a, "The Downtown patio closes at 9", kind="context", user=OWNER)
    # An owner's constraint is an owner rule since the R4 refix (PROMPTS-1):
    # rule_lines serves it, constraint_lines the rest — read both.
    def _lines(rid):
        return owner_memory.rule_lines(_req(rid)) + owner_memory.constraint_lines(_req(rid))
    at_b = [l["text"] for l in _lines(b)]
    assert "Constraint: We close every location on Thanksgiving" in at_b
    assert not any("patio" in t for t in at_b), "a location's own fact leaked to its sibling"
    assert not [l for l in _lines(lone) if "Thanksgiving" in l["text"]]
    line = next(l for l in _lines(b) if "Thanksgiving" in l["text"])
    assert line["who"].endswith("for every location")
    # A manager may neither keep one nor move one to every location.
    with pytest.raises(owner_memory.MemoryRefused):
        owner_memory.remember(a, "Staff meal is at 3", user=MANAGER, scope="org")
    patio = next(f for f in models.get_ask_memory(a) if "patio" in f["fact"])
    with pytest.raises(owner_memory.MemoryRefused):
        owner_memory.set_scope(a, patio["id"], "org", MANAGER)
    owner_memory.set_scope(a, patio["id"], "org", OWNER)
    assert any("patio" in l["text"] for l in owner_memory.constraint_lines(_req(b)))
    # Forgotten where it is kept, not from a sibling.
    out = owner_memory.forget(b, "We close every location on Thanksgiving", user=OWNER)
    assert "forget it there" in out.get("error", "")
    view = owner_memory.account_view(b, OWNER)
    th = next(f for f in view["facts"] if "Thanksgiving" in f["fact"])
    assert th["scope"] == "org" and th["from_location"] == a and th["can_forget"] is False


def test_a_persons_rating_preference_applies_at_every_location():
    a, b = _group()
    conn = models.get_conn()
    for i in range(3):
        conn.execute("INSERT INTO ask_feedback (restaurant_id, message_id, user_id, helpful, note, depth, updated_at) "
                     "VALUES (?,?,?,?,?,?,datetime('now'))", (a, 100 + i, OWNER["id"], 0, "too long, just the number",
                                                               "executive"))
    conn.commit()
    conn.close()
    assert owner_memory.rating_preference(b, OWNER["id"]) is None
    owner_memory.derive_rating_preferences(a, OWNER)
    assert owner_memory.rating_preference(a, OWNER["id"]) == "short"
    assert owner_memory.rating_preference(b, OWNER["id"]) == "short"
    assert owner_memory.rating_preference(b, MANAGER["id"]) is None


def test_the_questions_a_person_asks_are_theirs_across_locations():
    a, b = _group()
    conn = models.get_conn()
    for rid, n, uid in ((a, 2, OWNER["id"]), (b, 1, OWNER["id"]), (a, 3, MANAGER["id"])):
        for i in range(n):
            cid = conn.execute("INSERT INTO ask_cavnar_conversations (restaurant_id, user_id, title, topics, "
                               "updated_at) VALUES (?,?,?,?,datetime('now'))",
                               (rid, uid, f"Labor at {rid} #{i}", "labor")).lastrowid
            conn.execute("INSERT INTO ask_cavnar_messages (conversation_id, restaurant_id, role, content) "
                         "VALUES (?,?,?,?)", (cid, rid, "user", "labor?"))
    conn.commit()
    conn.close()
    oa = ask_conversations.often_asks(b, OWNER["id"])
    assert oa and oa["chats"] == 3, "the owner's chats at Downtown did not count at North"
    past = ask_conversations.past_conversations(b, OWNER["id"])
    other = [p for p in past if p.get("location")]
    assert other and all(p["still_open"] is False for p in other) and other[0]["location"] == "Downtown"
    # Another login's chats never cross.
    assert not [p for p in past if "#2" in p["title"] and p.get("location")]


def test_scope_survives_r3s_key_rebuild_and_the_archive():
    """R3 rebuilt ask_memory onto (restaurant_id, fact, author) at boot: the
    rebuild copies every column, so an organisation-wide fact stays one; an
    evicted or forgotten org fact that is put back comes back org-wide."""
    a, b = _group()
    conn = models.get_conn()
    conn.execute("DROP TABLE ask_memory")
    conn.execute("CREATE TABLE ask_memory (id INTEGER PRIMARY KEY AUTOINCREMENT, restaurant_id INTEGER NOT NULL, "
                 "fact TEXT NOT NULL, kind TEXT, source TEXT, user_id INTEGER, "
                 "created_at TEXT NOT NULL DEFAULT (datetime('now')), scope TEXT, UNIQUE(restaurant_id, fact))")
    conn.execute("INSERT INTO ask_memory (restaurant_id, fact, kind, user_id, scope) VALUES (?,?,?,?,?)",
                 (a, "We close every location on Thanksgiving", "constraint", OWNER["id"], "org"))
    conn.commit()
    conn.close()
    models.init_ask_memory()
    row = next(f for f in models.get_ask_memory(a) if "Thanksgiving" in f["fact"])
    assert row["scope"] == "org"
    assert any("Thanksgiving" in l["text"] for l in owner_memory.constraint_lines(_req(b)))
    models.archive_ask_facts(a, [row["id"]], "evicted")
    arch = models.get_ask_memory_archive(a)
    assert arch and arch[0].get("scope", "org") == "org"
    models.restore_ask_fact(a, arch[0]["id"])
    assert next(f for f in models.get_ask_memory(a) if "Thanksgiving" in f["fact"])["scope"] == "org"
