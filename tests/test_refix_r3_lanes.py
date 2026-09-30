"""Memory re-audit 9/29/26, workstream R3 — lane_eviction (FORGET-7,
PEOPLE-12, PEOPLE-17).

One FIFO per kind, shared by every login: a manager's 30 notes pushed out the
owner's founding facts, private notes from one login evicted another's, and
Account's lane counts told a manager how many facts were kept from them. Now
each bucket has its own budget (the owner's shared facts; the team's, at
half; each login's own notes; the sales audit's), eviction takes the least
used first, a pinned fact never goes, an owner's evicted rule is kept until
dismissed, and the lanes count what the viewer may read.
"""
import pytest

import memory_context
import models
import ops
import owner_memory
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(monkeypatch, db_path):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


def _rid():
    return create_restaurant(Restaurant(name="Lane Co", owner_email="lane@x.test"))


def _owner(rid, uid=101):
    return {"id": uid, "restaurant_id": rid, "is_admin": 0, "role": "client", "username": "erik"}


def _manager(rid, uid=202, name="dana"):
    return {"id": uid, "restaurant_id": rid, "is_admin": 0, "role": "manager", "username": name}


def _live(rid):
    return {f["fact"] for f in models.get_ask_memory(rid)}


def test_a_managers_notes_never_push_out_the_owners_facts():
    """FORGET-7 / PEOPLE-12: 30 manager context notes evicted the owner's."""
    rid = _rid()
    owner_memory.remember(rid, "Football Sundays start 10/5", kind="context", user=_owner(rid))
    for i in range(models.ASK_MEMORY_CAPS["context"] + 5):
        owner_memory.remember(rid, f"Manager note number {i}", kind="context", user=_manager(rid))
    assert "Football Sundays start 10/5" in _live(rid)
    team = [f for f in models.get_ask_memory(rid) if f["user_id"] == 202]
    assert len(team) == int(models.ASK_MEMORY_CAPS["context"] * models.ASK_MEMORY_DELEGATE_SHARE)


def test_private_notes_have_their_own_budget_per_login():
    rid = _rid()
    owner_memory.remember(rid, "Remind me: call the linen company", kind="context", audience="author",
                          user=_owner(rid))
    for i in range(models.ASK_MEMORY_CAPS["context"] + 2):
        owner_memory.remember(rid, f"Dana's own reminder {i}", kind="context", audience="author",
                              user=_manager(rid))
    assert "Remind me: call the linen company" in _live(rid)
    assert "Dana's own reminder 0" not in _live(rid)
    assert "Dana's own reminder 2" in _live(rid)


def test_eviction_takes_the_least_used_fact_not_the_oldest_written():
    rid = _rid()
    for i in range(models.ASK_MEMORY_CAPS["goal"]):
        owner_memory.remember(rid, f"Aim {i}: a warm room", kind="goal", user=_owner(rid))
    first = next(f for f in models.get_ask_memory(rid) if f["fact"] == "Aim 0: a warm room")
    conn = models.get_conn()
    conn.execute("UPDATE ask_memory SET created_at=datetime('now', '-30 days')")
    conn.commit()
    conn.close()
    # a prompt carries the oldest one: memory_context stamps it used
    block = memory_context.memory_context(rid, "ask", subjects=())
    assert "Aim 0: a warm room" in block.text
    assert next(f for f in models.get_ask_memory(rid) if f["id"] == first["id"])["last_used_at"]
    conn = models.get_conn()
    conn.execute("UPDATE ask_memory SET last_used_at=NULL WHERE id!=?", (first["id"],))
    conn.commit()
    conn.close()
    owner_memory.remember(rid, "Aim new: the neighbourhood's living room", kind="goal", user=_owner(rid))
    assert "Aim 0: a warm room" in _live(rid), "the one in use stays"
    assert len([f for f in models.get_ask_memory(rid) if f["kind"] == "goal"]) == models.ASK_MEMORY_CAPS["goal"]


def test_a_pinned_fact_is_never_evicted():
    rid = _rid()
    owner_memory.remember(rid, "Aim zero: never close early", kind="goal", user=_owner(rid))
    fid = next(f["id"] for f in models.get_ask_memory(rid))
    assert owner_memory.pin(rid, fid, user=_owner(rid))["pinned"] is True
    assert owner_memory.pin(rid, fid, user=_manager(rid))["status"] == 403
    for i in range(models.ASK_MEMORY_CAPS["goal"] + 3):
        owner_memory.remember(rid, f"Aim {i}: a warm room", kind="goal", user=_owner(rid))
    assert "Aim zero: never close early" in _live(rid)


def test_lane_counts_are_what_the_viewer_may_read():
    """PEOPLE-17 (prove.py §6): the manager saw 1 fact, the lane said 2."""
    rid = _rid()
    owner_memory.remember(rid, "We're selling the restaurant next spring", audience="principals", user=_owner(rid))
    owner_memory.remember(rid, "Deliveries come at 9", user=_owner(rid))
    owner_memory.remember(rid, "My own reminder", audience="author", user=_manager(rid))
    lanes = {l["kind"]: l["count"] for l in owner_memory.account_view(rid, _manager(rid))["lanes"]}
    assert lanes["context"] == 2                       # the team fact and their own
    owner = owner_memory.account_view(rid, _owner(rid))
    assert {l["kind"]: l["count"] for l in owner["lanes"]}["context"] == 2
    assert owner["others_private"] == 1                # a count, never the text
    assert all("My own reminder" != f["fact"] for f in owner["facts"])
    assert owner_memory.account_view(rid, _manager(rid))["others_private"] == 0


def test_an_owners_evicted_rule_outlives_the_archive_window_until_dismissed(db_path):
    rid = _rid()
    for i in range(models.ASK_MEMORY_CAPS["constraint"] + 1):
        owner_memory.remember(rid, f"Rule {i}: never cut the host", kind="constraint", user=_owner(rid))
    owner_memory.remember(rid, "Manager rule", kind="constraint", user=_manager(rid))
    conn = models.get_conn()
    conn.execute("INSERT INTO ask_memory_archive (restaurant_id, fact, kind, reason, authority, audience, "
                 "archived_at) VALUES (?, 'A manager rule, evicted', 'constraint', 'evicted', 'delegate', 'team', "
                 "datetime('now', '-500 days'))", (rid,))
    conn.execute("UPDATE ask_memory_archive SET archived_at=datetime('now', '-500 days')")
    conn.commit()
    conn.close()
    ops.prune_ledgers(db_path)
    left = {a["fact"]: a for a in models.get_ask_memory_archive(rid)}
    assert "Rule 0: never cut the host" in left, "the owner's evicted rule is kept"
    assert "A manager rule, evicted" not in left
    out = owner_memory.dismiss(rid, left["Rule 0: never cut the host"]["id"], user=_owner(rid))
    # (R1 merge) the result also names its type and audience, for the
    # activity log, which never carries the fact's words (PEOPLE-1).
    assert out["dismissed"] == "Rule 0: never cut the host"
    assert models.get_ask_memory_archive(rid) == []
