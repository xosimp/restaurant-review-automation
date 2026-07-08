"""is_demo — the flag that gates whether a restaurant's data can be
automatically wiped and reseeded. Before this existed, the boot-time Gia Mia
refresh ran unconditionally against any restaurant named "Gia Mia" with no
way to stop it once that restaurant became a real client."""
import sys
import types

from flask import Flask

import admin_routes
from models import create_restaurant, get_restaurant, update_restaurant, Restaurant


def test_create_restaurant_defaults_is_demo_false(db_path):
    rid = create_restaurant(Restaurant(name="Real Client", owner_email="r@x.com"), db_path=db_path)
    r = get_restaurant(rid, db_path=db_path)
    assert r.is_demo == 0


def test_create_restaurant_can_set_is_demo_true(db_path):
    rid = create_restaurant(Restaurant(name="Demo Co", owner_email="d@x.com", is_demo=1), db_path=db_path)
    r = get_restaurant(rid, db_path=db_path)
    assert r.is_demo == 1


def test_update_restaurant_can_flip_is_demo(db_path):
    rid = create_restaurant(Restaurant(name="Demo Co", owner_email="d@x.com", is_demo=1), db_path=db_path)
    update_restaurant(rid, {"is_demo": 0}, db_path=db_path)
    r = get_restaurant(rid, db_path=db_path)
    assert r.is_demo == 0


def test_reseed_demo_data_refuses_when_not_demo(db_path, monkeypatch):
    monkeypatch.setattr(admin_routes, "get_restaurant", lambda rid: get_restaurant(rid, db_path=db_path))
    rid = create_restaurant(Restaurant(name="Real Client", owner_email="r@x.com", is_demo=0), db_path=db_path)
    with Flask(__name__).app_context():
        resp = admin_routes.reseed_demo_data.__wrapped__(rid, current_user={"is_admin": 1})
        assert resp[1] == 400
        assert resp[0].get_json()["ok"] is False


def test_reseed_demo_data_runs_when_is_demo(db_path, monkeypatch):
    """The route's `from hosted_dashboard import _refresh_gia_mia_reviews` is a
    lazy, function-body import — importing the real hosted_dashboard module
    would execute its module-level side effects (real DB init, background
    scheduler/seed threads against the real reviews.db, not this test's
    fixture). Inject a fake module into sys.modules instead so the import
    resolves without ever touching the real module."""
    monkeypatch.setattr(admin_routes, "get_restaurant", lambda rid: get_restaurant(rid, db_path=db_path))
    rid = create_restaurant(Restaurant(name="Demo Co", owner_email="d@x.com", is_demo=1), db_path=db_path)

    called = {}

    def fake_refresh(restaurant_id):
        called["rid"] = restaurant_id

    fake_module = types.ModuleType("hosted_dashboard")
    fake_module._refresh_gia_mia_reviews = fake_refresh
    monkeypatch.setitem(sys.modules, "hosted_dashboard", fake_module)

    with Flask(__name__).app_context():
        resp = admin_routes.reseed_demo_data.__wrapped__(rid, current_user={"is_admin": 1})
        assert resp.get_json()["ok"] is True
    assert called["rid"] == rid
