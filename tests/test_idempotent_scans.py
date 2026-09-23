"""A retried invoice or recipe scan (same Idempotency-Key) is answered from
the first result, never a second paid model call (CLIENT-21)."""
import pytest
from flask import Flask

import models
import strategy_routes
from models import Restaurant, create_restaurant


@pytest.fixture
def app(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    a = Flask(__name__)
    a.register_blueprint(strategy_routes.strategy_mobile_bp, url_prefix="/mobile/api")
    return a


def test_a_retried_scan_with_the_same_key_is_not_read_twice(app, db_path, monkeypatch):
    rid = create_restaurant(Restaurant(name="Scan Co", owner_email="s@x.test"), db_path=db_path)
    calls = []
    body = strategy_routes._idempotent(lambda u, **k: calls.append(1) or ({"ok": True, "lines": 3}, 200), "t")
    h = {"Idempotency-Key": "file-abc"}
    with app.test_request_context(headers=h):
        first = body({"restaurant_id": rid})
    with app.test_request_context(headers=h):
        second = body({"restaurant_id": rid})
    assert calls == [1] and first == second == ({"ok": True, "lines": 3}, 200)


def test_a_failed_scan_is_not_remembered(app, db_path):
    rid = create_restaurant(Restaurant(name="Scan Co", owner_email="s@x.test"), db_path=db_path)
    results = iter([({"ok": False}, 502), ({"ok": True}, 200)])
    body = strategy_routes._idempotent(lambda u, **k: next(results), "t")
    h = {"Idempotency-Key": "k2"}
    with app.test_request_context(headers=h):
        assert body({"restaurant_id": rid})[1] == 502
    with app.test_request_context(headers=h):
        assert body({"restaurant_id": rid}) == ({"ok": True}, 200)
