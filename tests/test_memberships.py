"""memberships — Identity → Tenant → Role, and the migration onto it.

The risk this file exists to cover is not the new feature, it is the ~390
existing routes that read current_user["restaurant_id"] and the six live
accounts whose authorization must not shift by a single bit when this table
appears underneath them.
"""
import pytest

import auth
import models
from auth import (backfill_memberships, create_session, create_user,
                  get_membership, get_memberships_for_restaurant,
                  get_memberships_for_user, get_session_user, init_auth,
                  set_membership_active, set_user_role, upsert_membership)
from models import Restaurant, create_restaurant, get_conn


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(auth, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    init_auth(db_path=db_path)


def _restaurant(db_path, name="Simple EJ's"):
    return create_restaurant(Restaurant(name=name, owner_email="o@x.test"), db_path=db_path)


# ── migration ──────────────────────────────────────────────────────────────

def test_backfill_gives_every_existing_login_the_role_it_already_had(db_path):
    rid = _restaurant(db_path)
    uid = create_user(rid, "erik", "erik@x.test", "pw", db_path=db_path)
    conn = get_conn(db_path)
    conn.execute("DELETE FROM memberships")   # simulate pre-migration state
    conn.commit()
    conn.close()

    created = backfill_memberships(db_path=db_path)
    assert created == 1
    m = get_membership(uid, rid, db_path=db_path)
    assert m["role"] == "client"
    assert m["restaurant_id"] == rid
    assert m["pin_hash"] is None


def test_backfill_is_idempotent(db_path):
    rid = _restaurant(db_path)
    create_user(rid, "erik", "erik@x.test", "pw", db_path=db_path)
    backfill_memberships(db_path=db_path)
    assert backfill_memberships(db_path=db_path) == 0
    assert backfill_memberships(db_path=db_path) == 0


def test_backfill_preserves_a_non_default_role(db_path):
    rid = _restaurant(db_path)
    uid = create_user(rid, "will", "will@x.test", "pw", db_path=db_path)
    set_user_role(uid, "owner", db_path=db_path)
    conn = get_conn(db_path)
    conn.execute("DELETE FROM memberships")
    conn.commit()
    conn.close()
    backfill_memberships(db_path=db_path)
    assert get_membership(uid, rid, db_path=db_path)["role"] == "owner"


def test_a_session_with_no_membership_row_still_resolves(db_path):
    """The dual-read's whole point: the table's absence can never lock
    anybody out. A login racing the backfill keeps users.role."""
    rid = _restaurant(db_path)
    uid = create_user(rid, "erik", "erik@x.test", "pw", db_path=db_path)
    token = create_session(uid, db_path=db_path)
    conn = get_conn(db_path)
    conn.execute("DELETE FROM memberships")
    conn.commit()
    conn.close()

    user = get_session_user(token, db_path=db_path)
    assert user is not None
    assert user["role"] == "client"
    assert user["restaurant_id"] == rid
    assert user["membership_id"] is None


def test_the_membership_role_wins_over_the_legacy_column(db_path):
    """users.role cannot express "manager here, employee there". Once a
    membership exists for the restaurant the session is acting in, it is the
    authority."""
    rid = _restaurant(db_path)
    uid = create_user(rid, "dana", "dana@x.test", "pw", db_path=db_path)
    upsert_membership(uid, rid, "manager", employee_name="Dana K.", db_path=db_path)
    token = create_session(uid, db_path=db_path)

    user = get_session_user(token, db_path=db_path)
    assert user["role"] == "manager"          # not 'client' from users.role
    assert user["employee_name"] == "Dana K."
    assert user["membership_id"] is not None


# ── the multi-restaurant story ─────────────────────────────────────────────

def test_one_identity_can_hold_different_roles_at_different_restaurants(db_path):
    """The reason this is a join table and not a column: someone can be an
    employee at one location and a manager at another on ONE account."""
    rid_a = _restaurant(db_path, "EJ's Downtown")
    rid_b = _restaurant(db_path, "EJ's Uptown")
    uid = create_user(rid_a, "jordan", "jordan@x.test", "pw", db_path=db_path)
    upsert_membership(uid, rid_a, "employee", employee_name="Jordan P.", db_path=db_path)
    upsert_membership(uid, rid_b, "manager", employee_name="Jordan P.", db_path=db_path)

    mine = get_memberships_for_user(uid, db_path=db_path)
    assert {m["restaurant_id"]: m["role"] for m in mine} == {
        rid_a: "employee", rid_b: "manager"}


def test_the_session_resolves_the_role_for_the_restaurant_it_is_acting_in(db_path):
    rid_a = _restaurant(db_path, "EJ's Downtown")
    rid_b = _restaurant(db_path, "EJ's Uptown")
    uid = create_user(rid_a, "jordan", "jordan@x.test", "pw", db_path=db_path)
    upsert_membership(uid, rid_a, "employee", db_path=db_path)
    upsert_membership(uid, rid_b, "manager", db_path=db_path)
    token = create_session(uid, db_path=db_path)

    assert get_session_user(token, db_path=db_path)["role"] == "employee"

    # Point the session at the other restaurant: same identity, other role.
    conn = get_conn(db_path)
    conn.execute("UPDATE users SET role='owner' WHERE id=?", (uid,))
    conn.execute("UPDATE sessions SET active_restaurant_id=? WHERE user_id=?", (rid_b, uid))
    conn.commit()
    conn.close()
    user = get_session_user(token, db_path=db_path)
    assert user["restaurant_id"] == rid_b
    assert user["role"] == "manager"


def test_upsert_refuses_a_role_that_is_not_in_the_registry(db_path):
    rid = _restaurant(db_path)
    uid = create_user(rid, "erik", "erik@x.test", "pw", db_path=db_path)
    with pytest.raises(ValueError):
        upsert_membership(uid, rid, "supreme-leader", db_path=db_path)


def test_upsert_updates_rather_than_duplicating(db_path):
    rid = _restaurant(db_path)
    uid = create_user(rid, "erik", "erik@x.test", "pw", db_path=db_path)
    upsert_membership(uid, rid, "employee", db_path=db_path)
    upsert_membership(uid, rid, "manager", db_path=db_path)
    assert len(get_memberships_for_user(uid, db_path=db_path)) == 1
    assert get_membership(uid, rid, db_path=db_path)["role"] == "manager"


# ── tenant isolation ───────────────────────────────────────────────────────

def test_a_roster_only_contains_its_own_restaurants_memberships(db_path):
    rid_a = _restaurant(db_path, "Simple EJ's")
    rid_b = _restaurant(db_path, "Gia Mia")
    ua = create_user(rid_a, "erik", "erik@x.test", "pw", db_path=db_path)
    ub = create_user(rid_b, "stranger", "stranger@x.test", "pw", db_path=db_path)
    upsert_membership(ua, rid_a, "employee", employee_name="Erik", db_path=db_path)
    upsert_membership(ub, rid_b, "employee", employee_name="Stranger", db_path=db_path)

    names = [m["employee_name"] for m in get_memberships_for_restaurant(rid_a, db_path=db_path)]
    assert names == ["Erik"]


def test_deactivating_is_scoped_to_the_acting_restaurant(db_path):
    """An owner must not be able to deactivate a membership that belongs to
    somebody else's restaurant by guessing its id."""
    rid_a = _restaurant(db_path, "Simple EJ's")
    rid_b = _restaurant(db_path, "Gia Mia")
    ub = create_user(rid_b, "stranger", "stranger@x.test", "pw", db_path=db_path)
    m = upsert_membership(ub, rid_b, "employee", db_path=db_path)

    assert set_membership_active(m["id"], rid_a, False, db_path=db_path) is False
    assert get_membership(ub, rid_b, db_path=db_path) is not None
    assert set_membership_active(m["id"], rid_b, False, db_path=db_path) is True
    assert get_membership(ub, rid_b, db_path=db_path) is None


def test_an_inactive_membership_fails_closed_rather_than_falling_back(db_path):
    rid = _restaurant(db_path)
    uid = create_user(rid, "dana", "dana@x.test", "pw", db_path=db_path)
    m = upsert_membership(uid, rid, "employee", db_path=db_path)
    set_membership_active(m["id"], rid, False, db_path=db_path)
    token = create_session(uid, db_path=db_path)
    # An identity with memberships but none active here is not authorised
    # here. It used to fall back to users.role 'client' — the owner console
    # of the restaurant that had just removed them (SEC-1).
    assert get_session_user(token, db_path=db_path) is None
