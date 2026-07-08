"""Webhook delivery hardening — before this, a broken endpoint (dead Zapier
hook, expired URL) fired forever with 2 back-to-back attempts and no history
visible to the client beyond "last status". Now: 3 attempts with backoff,
every attempt logged to webhook_deliveries, and consecutive failures across
calls auto-disable the webhook with a reason the client can see and clear."""
import types

import pytest

import webhooks
from webhooks import (
    init_webhooks, save_webhook, get_webhook,
    reactivate_webhook, get_webhook_deliveries, _deliver, _AUTO_DISABLE_AFTER,
)
from models import get_conn, create_restaurant, Restaurant


@pytest.fixture
def rid(db_path):
    """webhooks.restaurant_id is a real FK against restaurants(id) — a
    plain literal id with no matching row fails with IntegrityError."""
    return create_restaurant(Restaurant(name="Webhook Co", owner_email="w@x.com"), db_path=db_path)


def _no_sleep(monkeypatch):
    monkeypatch.setattr(webhooks.time, "sleep", lambda s: None)


def _fake_post(status_code=200, ok=True, raises=None):
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        if raises:
            raise raises
        return types.SimpleNamespace(status_code=status_code, ok=ok)

    return post, calls


def test_successful_delivery_logs_one_row(db_path, rid, monkeypatch):
    _no_sleep(monkeypatch)
    init_webhooks(db_path=db_path)
    save_webhook(rid, "https://example.com/hook", ["review.received"], db_path=db_path)
    wh = get_webhook(rid, db_path=db_path)

    post, calls = _fake_post(status_code=200, ok=True)
    monkeypatch.setattr("requests.post", post)

    _deliver(wh, "review.received", {"x": 1}, db_path=db_path)

    assert len(calls) == 1  # succeeded on first attempt, no retries needed
    deliveries = get_webhook_deliveries(rid, db_path=db_path)
    assert len(deliveries) == 1
    assert deliveries[0]["ok"] == 1
    assert deliveries[0]["attempts"] == 1
    assert deliveries[0]["status"] == 200


def test_failed_delivery_retries_three_times_with_backoff(db_path, rid, monkeypatch):
    _no_sleep(monkeypatch)
    init_webhooks(db_path=db_path)
    save_webhook(rid, "https://example.com/hook", ["review.received"], db_path=db_path)
    wh = get_webhook(rid, db_path=db_path)

    post, calls = _fake_post(status_code=500, ok=False)
    monkeypatch.setattr("requests.post", post)

    _deliver(wh, "review.received", {"x": 1}, db_path=db_path)

    assert len(calls) == 3  # exhausted all 3 attempts
    deliveries = get_webhook_deliveries(rid, db_path=db_path)
    assert deliveries[0]["ok"] == 0
    assert deliveries[0]["attempts"] == 3
    assert deliveries[0]["status"] == 500


def test_network_exception_is_recorded_as_error(db_path, rid, monkeypatch):
    _no_sleep(monkeypatch)
    init_webhooks(db_path=db_path)
    save_webhook(rid, "https://example.com/hook", ["review.received"], db_path=db_path)
    wh = get_webhook(rid, db_path=db_path)

    post, calls = _fake_post(raises=ConnectionError("refused"))
    monkeypatch.setattr("requests.post", post)

    _deliver(wh, "review.received", {"x": 1}, db_path=db_path)

    deliveries = get_webhook_deliveries(rid, db_path=db_path)
    assert deliveries[0]["ok"] == 0
    assert "refused" in deliveries[0]["error"]


def test_consecutive_failures_accumulate_across_calls(db_path, rid, monkeypatch):
    _no_sleep(monkeypatch)
    init_webhooks(db_path=db_path)
    save_webhook(rid, "https://example.com/hook", ["review.received"], db_path=db_path)

    post, calls = _fake_post(status_code=500, ok=False)
    monkeypatch.setattr("requests.post", post)

    for _ in range(3):
        wh = get_webhook(rid, db_path=db_path)  # re-fetch — consecutive_failures changed each time
        _deliver(wh, "review.received", {}, db_path=db_path)

    conn = get_conn(db_path)
    row = conn.execute("SELECT consecutive_failures, is_active FROM webhooks WHERE restaurant_id=?", (rid,)).fetchone()
    conn.close()
    assert row["consecutive_failures"] == 3
    assert row["is_active"] == 1  # not yet at the auto-disable threshold


def test_auto_disables_after_threshold_reached(db_path, rid, monkeypatch):
    _no_sleep(monkeypatch)
    init_webhooks(db_path=db_path)
    conn = get_conn(db_path)
    conn.execute(
        "INSERT INTO webhooks (restaurant_id, url, secret, events, consecutive_failures) VALUES (?,?,?,?,?)",
        (rid, "https://example.com/hook", "whsec_x", '["review.received"]', _AUTO_DISABLE_AFTER - 1)
    )
    conn.commit()
    conn.close()
    wh = get_webhook(rid, db_path=db_path)

    post, calls = _fake_post(status_code=500, ok=False)
    monkeypatch.setattr("requests.post", post)

    _deliver(wh, "review.received", {}, db_path=db_path)  # this failure crosses the threshold

    conn = get_conn(db_path)
    row = conn.execute(
        "SELECT consecutive_failures, is_active, disabled_reason FROM webhooks WHERE restaurant_id=?", (rid,)
    ).fetchone()
    conn.close()
    assert row["consecutive_failures"] == _AUTO_DISABLE_AFTER
    assert row["is_active"] == 0
    assert "auto-disabled" in row["disabled_reason"].lower()
    # An auto-disabled webhook no longer matches get_webhook()'s is_active=1 filter
    assert get_webhook(rid, db_path=db_path) is None


def test_successful_delivery_resets_consecutive_failures(db_path, rid, monkeypatch):
    _no_sleep(monkeypatch)
    init_webhooks(db_path=db_path)
    conn = get_conn(db_path)
    conn.execute(
        "INSERT INTO webhooks (restaurant_id, url, secret, events, consecutive_failures) VALUES (?,?,?,?,?)",
        (rid, "https://example.com/hook", "whsec_x", '["review.received"]', 5)
    )
    conn.commit()
    conn.close()
    wh = get_webhook(rid, db_path=db_path)

    post, calls = _fake_post(status_code=200, ok=True)
    monkeypatch.setattr("requests.post", post)

    _deliver(wh, "review.received", {}, db_path=db_path)

    conn = get_conn(db_path)
    row = conn.execute("SELECT consecutive_failures FROM webhooks WHERE restaurant_id=?", (rid,)).fetchone()
    conn.close()
    assert row["consecutive_failures"] == 0


def test_reactivate_webhook_clears_disabled_state(db_path, rid):
    init_webhooks(db_path=db_path)
    conn = get_conn(db_path)
    conn.execute(
        "INSERT INTO webhooks (restaurant_id, url, secret, events, is_active, consecutive_failures, disabled_reason) VALUES (?,?,?,?,0,?,?)",
        (rid, "https://example.com/hook", "whsec_x", '["review.received"]', 10, "Auto-disabled after 10 consecutive failed deliveries")
    )
    conn.commit()
    conn.close()

    reactivate_webhook(rid, db_path=db_path)

    wh = get_webhook(rid, db_path=db_path)
    assert wh is not None
    assert wh["consecutive_failures"] == 0
    assert wh["disabled_reason"] is None


def test_resaving_a_disabled_webhook_reactivates_it(db_path, rid):
    init_webhooks(db_path=db_path)
    conn = get_conn(db_path)
    conn.execute(
        "INSERT INTO webhooks (restaurant_id, url, secret, events, is_active, consecutive_failures, disabled_reason) VALUES (?,?,?,?,0,?,?)",
        (rid, "https://example.com/hook", "whsec_x", '["review.received"]', 10, "Auto-disabled after 10 consecutive failed deliveries")
    )
    conn.commit()
    conn.close()

    save_webhook(rid, "https://example.com/hook-v2", ["review.received", "alert.fired"], db_path=db_path)

    wh = get_webhook(rid, db_path=db_path)
    assert wh is not None
    assert wh["url"] == "https://example.com/hook-v2"
    assert wh["consecutive_failures"] == 0
    assert wh["disabled_reason"] is None


def test_get_webhook_deliveries_orders_most_recent_first_and_respects_limit(db_path, rid, monkeypatch):
    _no_sleep(monkeypatch)
    init_webhooks(db_path=db_path)
    save_webhook(rid, "https://example.com/hook", ["review.received"], db_path=db_path)

    post, calls = _fake_post(status_code=200, ok=True)
    monkeypatch.setattr("requests.post", post)

    for i in range(3):
        wh = get_webhook(rid, db_path=db_path)
        _deliver(wh, f"event.{i}", {}, db_path=db_path)

    deliveries = get_webhook_deliveries(rid, limit=2, db_path=db_path)
    assert len(deliveries) == 2
    assert deliveries[0]["event_type"] == "event.2"  # most recent first


def test_migration_backfills_new_columns_on_existing_db(db_path, rid):
    """init_webhooks() runs at every boot — a webhooks row created before
    consecutive_failures/disabled_reason existed must not break get_webhook()."""
    conn = get_conn(db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS webhooks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id INTEGER NOT NULL,
            url TEXT NOT NULL,
            secret TEXT NOT NULL,
            events TEXT NOT NULL DEFAULT '[]',
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            last_fired_at TEXT,
            last_status INTEGER
        )
    """)
    conn.execute(
        "INSERT INTO webhooks (restaurant_id, url, secret, events) VALUES (?, 'https://x.com', 'whsec_x', '[\"review.received\"]')",
        (rid,)
    )
    conn.commit()
    conn.close()

    init_webhooks(db_path=db_path)  # runs the ALTER TABLE migration

    wh = get_webhook(rid, db_path=db_path)
    assert wh is not None
    assert wh["consecutive_failures"] == 0
