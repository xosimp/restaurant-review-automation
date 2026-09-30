"""Memory re-audit 9/29/26, workstream R3 — archive_restore (FORGET-2,
FORGET-3, INVENTORY-3's related findings).

Account listed the 30 newest archived facts and filtered them for the viewer
AFTER the limit, and restore accepted only an id in that list — so the
"year to restore" failed past the 30th row, and a manager could see an empty
archive. Restoring an expired fact put its passed date back, and the next
read archived it again. Now the archive is filtered in SQL and paged, restore
reads the table by id with the same check, a passed date is cleared or
replaced, and the author's authority comes back with the fact.
"""
from datetime import date, timedelta

import pytest
from flask import Flask

import auth
import models
import owner_memory
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(monkeypatch, db_path):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    auth.init_auth(db_path=db_path)
    yield


def _rid():
    return create_restaurant(Restaurant(name="Arch Co", owner_email="arch@x.test"))


def _owner(rid, uid=101):
    return {"id": uid, "restaurant_id": rid, "is_admin": 0, "role": "client", "username": "erik"}


def _manager(rid, uid=202):
    return {"id": uid, "restaurant_id": rid, "is_admin": 0, "role": "manager", "username": "dana"}


def _archive(rid, n, **cols):
    conn = models.get_conn()
    for i in range(n):
        conn.execute("INSERT INTO ask_memory_archive (restaurant_id, fact, kind, reason, user_id, audience, "
                     "authority) VALUES (?, ?, 'context', 'evicted', ?, ?, ?)",
                     (rid, f"{cols.get('prefix', 'Old fact')} {i}", cols.get("user_id"),
                      cols.get("audience", "team"), cols.get("authority", "principal")))
    conn.commit()
    conn.close()


def test_every_archived_fact_is_reachable_and_restorable_not_just_the_newest_30():
    """FORGET-2 / proof_restore.py: 36 archived, account_view listed 30."""
    rid = _rid()
    _archive(rid, 36)
    first = owner_memory.account_view(rid, _owner(rid))
    assert len(first["archived"]) == owner_memory.ARCHIVE_PAGE and first["archive_more"] is True
    older = owner_memory.account_view(rid, _owner(rid), archive_before=first["archived"][-1]["id"])
    assert len(older["archived"]) == 6 and older["archive_more"] is False
    oldest = older["archived"][-1]
    assert owner_memory.restore(rid, oldest["id"], user=_owner(rid))["fact"] == "Old fact 0"
    assert "Old fact 0" in {f["fact"] for f in models.get_ask_memory(rid)}


def test_the_viewer_filter_runs_before_the_page_is_cut():
    rid = _rid()
    _archive(rid, 3, prefix="Dana's own", user_id=202, audience="author", authority="delegate")
    _archive(rid, 40, prefix="Owner only", user_id=101, audience="principals")
    view = owner_memory.account_view(rid, _manager(rid))
    assert {a["fact"] for a in view["archived"]} == {"Dana's own 0", "Dana's own 1", "Dana's own 2"}
    hidden = next(a for a in models.get_ask_memory_archive(rid) if a["fact"].startswith("Owner only"))
    assert owner_memory.restore(rid, hidden["id"], user=_manager(rid))["status"] == 404


def test_restoring_an_expired_fact_does_not_bounce_back_to_the_archive():
    """FORGET-3 / proof_restore.py: live after restore, gone after the next read."""
    rid = _rid()
    today = date.today()
    owner_memory.remember(rid, "Patio closed for repairs", valid_until=(today + timedelta(days=1)).isoformat(),
                          user=_owner(rid), today=today)
    later = today + timedelta(days=3)
    owner_memory.expire(rid, today=later)
    aid = models.get_ask_memory_archive(rid)[0]["id"]
    out = owner_memory.restore(rid, aid, user=_owner(rid), today=later)
    assert out["fact"] == "Patio closed for repairs" and out["valid_until"] is None
    assert [f["fact"] for f in owner_memory.facts_for(rid, today=later)] == ["Patio closed for repairs"]
    assert models.get_ask_memory_archive(rid) == []


def test_a_restore_can_carry_a_new_date_but_never_a_past_one():
    rid = _rid()
    owner_memory.remember(rid, "Check the comps with me", kind="followup", due_on=date.today().isoformat(),
                          user=_owner(rid))
    owner_memory.expire(rid, today=date.today() + timedelta(days=30))
    aid = models.get_ask_memory_archive(rid)[0]["id"]
    assert owner_memory.restore(rid, aid, user=_owner(rid), due_on="2020-01-01")["status"] == 400
    due = (date.today() + timedelta(days=5)).isoformat()
    assert owner_memory.restore(rid, aid, user=_owner(rid), due_on=due)["due_on"] == due


def test_restore_keeps_the_authors_authority():
    """PEOPLE-5: the archive had no authority column; a restore dropped it."""
    rid = _rid()
    for i in range(models.ASK_MEMORY_CAPS["goal"] + 1):
        owner_memory.remember(rid, f"Aim {i}", kind="goal", user=_owner(rid))
    gone = models.get_ask_memory_archive(rid)[0]
    assert gone["authority"] == "principal"
    owner_memory.restore(rid, gone["id"], user=_owner(rid))
    back = next(f for f in models.get_ask_memory(rid) if f["fact"] == gone["fact"])
    assert back["authority"] == "principal" and back["user_id"] == 101


def test_the_restore_route_reads_the_table_not_the_rendered_page(db_path):
    import strategy_routes
    app = Flask(__name__)
    app.register_blueprint(strategy_routes.strategy_mobile_bp)
    client = app.test_client()
    rid = _rid()
    uid = auth.create_user(rid, "own", "own@x.test", "pw", db_path=db_path)
    h = {"Authorization": f"Bearer {auth.create_session(uid, db_path=db_path)}"}
    _archive(rid, 45)
    oldest = min(a["id"] for a in models.get_ask_memory_archive(rid, limit=100))
    r = client.post("/mobile/api/account/memory/restore", headers=h, json={"id": oldest}).get_json()
    assert r["ok"] and r["fact"] == "Old fact 0"
    page = client.get("/mobile/api/account/memory?archive_before=999999", headers=h).get_json()
    assert page["ok"] and len(page["archived"]) == 30 and page["archive_more"] is True
