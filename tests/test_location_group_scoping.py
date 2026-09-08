"""location_group is a tenancy boundary, not a label.

From the pre-launch audit: `location_group` is free text an admin types per
location, and three features key on it — switching the active location, the
group Home rollup, and (since billing reconciliation moved into the webhook)
which restaurants a Stripe cancellation churns. All three matched on the
string alone, so two unrelated clients typed into the same group became one
tenant: either owner could switch into the other's locations and read their
reviews, labor and sales, and cancelling one subscription churned the other.
"""
import pytest
from flask import Flask

import admin_routes
import auth
import client_api
import guest_marketing
import home_brief
import mobile_api
import models
import webhook_routes
from auth import create_user, init_auth, set_user_role
from models import (Restaurant, create_restaurant, get_location_group,
                    location_group_conflict, update_restaurant)


def _redirect_db(monkeypatch, db_path):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, admin_routes, client_api, mobile_api, guest_marketing,
                webhook_routes, home_brief):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)


@pytest.fixture(autouse=True)
def _init(db_path, monkeypatch):
    _redirect_db(monkeypatch, db_path)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    init_auth(db_path=db_path)


def _loc(db_path, name, owner_email, group=None, location_name=None):
    return create_restaurant(
        Restaurant(name=name, owner_email=owner_email, location_group=group,
                   location_name=location_name),
        db_path=db_path,
    )


# ── the data layer ──────────────────────────────────────────────────────────

def test_two_owners_who_typed_the_same_group_name_are_not_one_group(db_path):
    mine_a = _loc(db_path, "Syrup Wicker Park", "ann@a.test", "Syrup", "Wicker Park")
    mine_b = _loc(db_path, "Syrup Lakeview", "ann@a.test", "Syrup", "Lakeview")
    theirs = _loc(db_path, "Syrup Nashville", "bob@b.test", "Syrup", "Nashville")

    mine = [r["id"] for r in get_location_group("Syrup", db_path=db_path, owner_email="ann@a.test")]
    assert sorted(mine) == sorted([mine_a, mine_b])
    assert theirs not in mine

    other = [r["id"] for r in get_location_group("Syrup", db_path=db_path, owner_email="bob@b.test")]
    assert other == [theirs]


def test_owner_email_matching_ignores_case_and_whitespace(db_path):
    rid = _loc(db_path, "Uptown", "  Ann@A.test ", "Syrup", "Uptown")
    found = [r["id"] for r in get_location_group("Syrup", db_path=db_path, owner_email="ann@a.test")]
    assert found == [rid]


def test_omitting_the_owner_still_returns_the_raw_name_match(db_path):
    """Admin tooling reporting on a collision needs to see both sides."""
    a = _loc(db_path, "A", "ann@a.test", "Syrup")
    b = _loc(db_path, "B", "bob@b.test", "Syrup")
    assert sorted(r["id"] for r in get_location_group("Syrup", db_path=db_path)) == sorted([a, b])


# ── the collision guard at the write ────────────────────────────────────────

def test_a_group_name_owned_by_someone_else_is_a_conflict(db_path):
    _loc(db_path, "Syrup Nashville", "bob@b.test", "Syrup")
    assert location_group_conflict("Syrup", "ann@a.test", db_path=db_path) == "bob@b.test"


def test_your_own_group_name_is_not_a_conflict(db_path):
    _loc(db_path, "Syrup Lakeview", "ann@a.test", "Syrup")
    assert location_group_conflict("Syrup", "ann@a.test", db_path=db_path) is None
    assert location_group_conflict("Syrup", "ANN@A.test", db_path=db_path) is None


def test_an_unused_or_blank_group_name_is_not_a_conflict(db_path):
    assert location_group_conflict("Brand New", "ann@a.test", db_path=db_path) is None
    assert location_group_conflict("", "ann@a.test", db_path=db_path) is None
    assert location_group_conflict("   ", "ann@a.test", db_path=db_path) is None


def test_saving_a_location_does_not_conflict_with_itself(db_path):
    rid = _loc(db_path, "Syrup Lakeview", "ann@a.test", "Syrup")
    assert location_group_conflict("Syrup", "ann@a.test", exclude_id=rid, db_path=db_path) is None


# ── the route that hands out access ─────────────────────────────────────────

@pytest.fixture
def app(db_path, monkeypatch):
    from mobile_api import mobile_bp
    flask_app = Flask(__name__)
    flask_app.register_blueprint(mobile_bp)
    return flask_app


def test_an_owner_cannot_switch_into_another_client_identically_named_group(app, db_path):
    """The escalation this closes: one admin typo away from a full read of
    another restaurant's reviews, labor and sales."""
    mine = _loc(db_path, "Syrup Lakeview", "ann@a.test", "Syrup", "Lakeview")
    theirs = _loc(db_path, "Syrup Nashville", "bob@b.test", "Syrup", "Nashville")
    uid = create_user(mine, "ann", "ann@a.test", "correct-horse", db_path=db_path)
    set_user_role(uid, "owner", db_path=db_path)

    client = app.test_client()
    token = client.post("/mobile/api/login", json={"username": "ann", "password": "correct-horse"}).get_json()["token"]
    hdr = {"Authorization": f"Bearer {token}"}

    resp = client.post("/mobile/api/switch-location", headers=hdr, json={"restaurant_id": theirs})
    assert resp.status_code == 403
    assert resp.get_json()["ok"] is False

    listed = client.get("/mobile/api/group-locations", headers=hdr).get_json()
    assert [l["id"] for l in listed["locations"]] == [mine]


def test_an_owner_can_still_switch_within_their_own_group(app, db_path):
    a = _loc(db_path, "Syrup Lakeview", "ann@a.test", "Syrup", "Lakeview")
    b = _loc(db_path, "Syrup Wicker Park", "ann@a.test", "Syrup", "Wicker Park")
    uid = create_user(a, "ann", "ann@a.test", "correct-horse", db_path=db_path)
    set_user_role(uid, "owner", db_path=db_path)

    client = app.test_client()
    token = client.post("/mobile/api/login", json={"username": "ann", "password": "correct-horse"}).get_json()["token"]
    hdr = {"Authorization": f"Bearer {token}"}

    assert client.post("/mobile/api/switch-location", headers=hdr, json={"restaurant_id": b}).status_code == 200
    listed = client.get("/mobile/api/group-locations", headers=hdr).get_json()
    assert sorted(l["id"] for l in listed["locations"]) == sorted([a, b])


# ── billing propagation ─────────────────────────────────────────────────────

def test_a_cancellation_does_not_churn_a_different_client_same_named_group(db_path):
    mine_a = _loc(db_path, "Syrup Lakeview", "ann@a.test", "Syrup")
    mine_b = _loc(db_path, "Syrup Wicker Park", "ann@a.test", "Syrup")
    theirs = _loc(db_path, "Syrup Nashville", "bob@b.test", "Syrup")

    siblings = webhook_routes._sibling_restaurant_ids(mine_a)
    assert sorted(siblings) == sorted([mine_a, mine_b])
    assert theirs not in siblings


def test_a_location_with_no_group_is_its_own_sibling_set(db_path):
    solo = _loc(db_path, "Solo", "solo@x.test")
    assert webhook_routes._sibling_restaurant_ids(solo) == [solo]


# ── the admin write refuses the collision that creates all of this ──────────

def test_admin_cannot_assign_a_group_name_owned_by_another_client(db_path, monkeypatch):
    from flask import Flask
    from admin_routes import admin_bp, save_client_settings
    monkeypatch.setattr(auth, "get_current_user", lambda: {"id": 999, "is_admin": 1})
    _loc(db_path, "Syrup Nashville", "bob@b.test", "Syrup")
    mine = _loc(db_path, "Some Place", "ann@a.test")

    flask_app = Flask(__name__)
    flask_app.register_blueprint(admin_bp)
    with flask_app.test_request_context(
        f"/admin/client-settings/{mine}", method="POST",
        json={"name": "Some Place", "owner_email": "ann@a.test", "location_group": "Syrup"},
    ):
        resp = save_client_settings(mine)
    body = resp.get_json() if hasattr(resp, "get_json") else resp[0].get_json()
    assert body["ok"] is False
    assert "bob@b.test" in body["error"]

    from models import get_restaurant
    assert get_restaurant(mine, db_path=db_path).location_group is None, "the write must not land"


def test_admin_can_still_save_a_location_own_group_name(db_path, monkeypatch):
    from flask import Flask
    from admin_routes import admin_bp, save_client_settings
    monkeypatch.setattr(auth, "get_current_user", lambda: {"id": 999, "is_admin": 1})
    a = _loc(db_path, "Syrup Lakeview", "ann@a.test", "Syrup")

    flask_app = Flask(__name__)
    flask_app.register_blueprint(admin_bp)
    with flask_app.test_request_context(
        f"/admin/client-settings/{a}", method="POST",
        json={"name": "Syrup Lakeview", "owner_email": "ann@a.test", "location_group": "Syrup"},
    ):
        save_client_settings(a)
    from models import get_restaurant
    assert get_restaurant(a, db_path=db_path).location_group == "Syrup"
