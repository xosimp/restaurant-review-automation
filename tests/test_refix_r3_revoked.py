"""Memory re-audit 9/29/26, workstream R3 — revoked_login (FORGET-6,
PEOPLE-12).

Revoking a teammate touched no memory: their "author" notes, which no one
else could read or forget, kept steering every internal prompt. Now a
departed login's private notes move to the archive as 'author_left', where
the owner sees them and may keep one as their own; their shared facts stay,
marked as from someone who left.
"""
import pytest

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


def _login(db_path, rid, name, role):
    uid = auth.create_user(rid, name, f"{name}@x.test", "pw", db_path=db_path)
    conn = models.get_conn()
    conn.execute("UPDATE users SET role=? WHERE id=?", (role, uid))
    conn.commit()
    conn.close()
    return {"id": uid, "restaurant_id": rid, "is_admin": 0, "role": role, "username": name}


class _Req:
    def __init__(self, rid, surface):
        self.restaurant_id, self.surface, self.db_path, self.subjects = rid, surface, None, ()


def test_a_revoked_logins_private_notes_stop_steering_and_reach_the_owner(db_path):
    rid = create_restaurant(Restaurant(name="Left Co", owner_email="left@x.test"))
    owner = _login(db_path, rid, "erik", "client")
    ana = _login(db_path, rid, "ana", "manager")
    owner_memory.remember(rid, "Never schedule the patio before May", kind="constraint", audience="author",
                          modules=["schedule"], user=ana)
    owner_memory.remember(rid, "Never schedule Marco on Fridays", kind="constraint", modules=["schedule"], user=ana)
    lines = [l["text"] for l in owner_memory.constraint_lines(_Req(rid, "schedule"))]
    assert "Constraint: Never schedule the patio before May" in lines
    # the owner can neither see nor forget it while she is here
    assert all(f["fact"] != "Never schedule the patio before May"
               for f in owner_memory.account_view(rid, owner)["facts"])

    assert auth.revoke_team_member(rid, ana["id"], owner["id"], db_path=db_path)["ok"]

    lines = [l["text"] for l in owner_memory.constraint_lines(_Req(rid, "schedule"))]
    assert "Constraint: Never schedule the patio before May" not in lines
    view = owner_memory.account_view(rid, owner)
    left = next(a for a in view["archived"] if a["fact"] == "Never schedule the patio before May")
    assert left["reason"] == "author_left" and left["can_restore"]
    assert left["reason_label"] == "the person who added it no longer has access"
    shared = next(f for f in view["facts"] if f["fact"] == "Never schedule Marco on Fridays")
    assert shared["author_left"] is True and shared["can_forget"] is True

    # kept: it comes back as the owner's own, owner-only
    assert owner_memory.restore(rid, left["id"], user=owner)["fact"] == "Never schedule the patio before May"
    back = next(f for f in models.get_ask_memory(rid) if f["fact"] == "Never schedule the patio before May")
    assert back["user_id"] == owner["id"] and back["audience"] == "principals"
    assert back["source"].startswith("Kept from")


def test_a_login_switched_off_any_other_way_is_retired_on_the_next_read(db_path):
    rid = create_restaurant(Restaurant(name="Off Co", owner_email="off@x.test"))
    _login(db_path, rid, "erik", "client")
    ana = _login(db_path, rid, "ana", "manager")
    owner_memory.remember(rid, "Remind me about the walk-in", audience="author", user=ana)
    conn = models.get_conn()
    conn.execute("UPDATE users SET is_active=0 WHERE id=?", (ana["id"],))
    conn.commit()
    conn.close()
    assert owner_memory.facts_for(rid) == []
    assert models.get_ask_memory_archive(rid)[0]["reason"] == "author_left"
