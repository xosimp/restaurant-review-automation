"""push.py — APNs delivery, mirroring test_webhook_delivery.py's structure:
retry-with-backoff, per-delivery logging, and auto-disable-by-deletion after
repeated failures, plus the one real deviation from the webhook pattern —
an APNs "this token is permanently dead" response deletes it immediately
instead of waiting out the failure counter."""
import pytest

import push
from push import (
    init_push, register_device_token, get_device_tokens,
    remove_device_token, _deliver, _AUTO_DISABLE_AFTER,
)
from auth import init_auth, create_user
from models import get_conn, create_restaurant, Restaurant


@pytest.fixture
def rid(db_path):
    init_auth(db_path=db_path)
    return create_restaurant(Restaurant(name="Push Co", owner_email="p@x.com"), db_path=db_path)


@pytest.fixture
def uid(db_path, rid):
    return create_user(rid, "owner1", "owner1@x.com", "correct-horse", db_path=db_path)


def _no_sleep(monkeypatch):
    monkeypatch.setattr(push.time, "sleep", lambda s: None)


def _no_real_jwt(monkeypatch):
    """_provider_jwt() signs with a real APNs .p8 key from env vars — tests
    have none, so stub it out; delivery tests care about the HTTP behavior,
    not the auth token itself."""
    monkeypatch.setattr(push, "_provider_jwt", lambda: "fake-jwt-token")


def _fake_httpx_client(status_code=200, reason=None, raises=None):
    calls = []

    class _Resp:
        def __init__(self):
            self.status_code = status_code

        def json(self):
            return {"reason": reason} if reason else {}

    class _Client:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, content=None, headers=None):
            calls.append((url, content, headers))
            if raises:
                raise raises
            return _Resp()

    return _Client, calls


def _register(db_path, uid, rid, token="a" * 64, environment="production"):
    register_device_token(uid, rid, token, environment, db_path=db_path)
    return get_device_tokens(rid, db_path=db_path)[0]


def test_successful_delivery_logs_one_row(db_path, rid, uid, monkeypatch):
    _no_sleep(monkeypatch)
    _no_real_jwt(monkeypatch)
    init_push(db_path=db_path)
    token_row = _register(db_path, uid, rid)

    fake_client, calls = _fake_httpx_client(status_code=200)
    monkeypatch.setattr("httpx.Client", fake_client)

    result = _deliver(token_row, "1star", "1-star review", "Ann left a 1-star review", None, db_path=db_path)

    assert len(calls) == 1  # succeeded first try, no retries
    assert result == {"ok": True, "status": 200, "attempts": 1, "error": None}

    conn = get_conn(db_path)
    row = conn.execute("SELECT ok, attempts, status FROM push_deliveries WHERE device_token_id=?", (token_row["id"],)).fetchone()
    conn.close()
    assert row["ok"] == 1
    assert row["attempts"] == 1
    assert row["status"] == 200


def test_failed_delivery_retries_three_times_with_backoff(db_path, rid, uid, monkeypatch):
    _no_sleep(monkeypatch)
    _no_real_jwt(monkeypatch)
    init_push(db_path=db_path)
    token_row = _register(db_path, uid, rid)

    fake_client, calls = _fake_httpx_client(status_code=500)
    monkeypatch.setattr("httpx.Client", fake_client)

    result = _deliver(token_row, "1star", "title", "body", None, db_path=db_path)

    assert len(calls) == 3  # exhausted all 3 attempts — 500 isn't a permanent-failure reason
    assert result["ok"] is False
    assert result["attempts"] == 3


def test_network_exception_is_recorded_as_error(db_path, rid, uid, monkeypatch):
    _no_sleep(monkeypatch)
    _no_real_jwt(monkeypatch)
    init_push(db_path=db_path)
    token_row = _register(db_path, uid, rid)

    fake_client, calls = _fake_httpx_client(raises=ConnectionError("refused"))
    monkeypatch.setattr("httpx.Client", fake_client)

    result = _deliver(token_row, "1star", "title", "body", None, db_path=db_path)
    assert result["ok"] is False
    assert result["status"] == 0
    assert "refused" in result["error"]


def test_consecutive_failures_accumulate_across_calls(db_path, rid, uid, monkeypatch):
    _no_sleep(monkeypatch)
    _no_real_jwt(monkeypatch)
    init_push(db_path=db_path)
    _register(db_path, uid, rid)

    fake_client, calls = _fake_httpx_client(status_code=500)
    monkeypatch.setattr("httpx.Client", fake_client)

    for _ in range(3):
        token_row = get_device_tokens(rid, db_path=db_path)[0]  # re-fetch — failures accumulate
        _deliver(token_row, "1star", "title", "body", None, db_path=db_path)

    conn = get_conn(db_path)
    row = conn.execute("SELECT consecutive_failures FROM device_tokens WHERE restaurant_id=?", (rid,)).fetchone()
    conn.close()
    assert row["consecutive_failures"] == 3


def test_token_deleted_after_auto_disable_threshold(db_path, rid, uid, monkeypatch):
    _no_sleep(monkeypatch)
    _no_real_jwt(monkeypatch)
    init_push(db_path=db_path)
    _register(db_path, uid, rid)

    fake_client, calls = _fake_httpx_client(status_code=500)
    monkeypatch.setattr("httpx.Client", fake_client)

    for _ in range(_AUTO_DISABLE_AFTER):
        rows = get_device_tokens(rid, db_path=db_path)
        if not rows:
            break
        _deliver(rows[0], "1star", "title", "body", None, db_path=db_path)

    assert get_device_tokens(rid, db_path=db_path) == []  # deleted, not just disabled


def test_permanent_failure_reason_deletes_token_on_first_failure(db_path, rid, uid, monkeypatch):
    """APNs' BadDeviceToken/Unregistered means the token will never work
    again — this should delete it on the very first such response, not wait
    out the full _AUTO_DISABLE_AFTER counter like a transient 500 would."""
    _no_sleep(monkeypatch)
    _no_real_jwt(monkeypatch)
    init_push(db_path=db_path)
    token_row = _register(db_path, uid, rid)

    fake_client, calls = _fake_httpx_client(status_code=410, reason="Unregistered")
    monkeypatch.setattr("httpx.Client", fake_client)

    result = _deliver(token_row, "1star", "title", "body", None, db_path=db_path)

    assert result["ok"] is False
    assert len(calls) == 1  # no point retrying a permanently dead token
    assert get_device_tokens(rid, db_path=db_path) == []


def test_successful_delivery_resets_consecutive_failures(db_path, rid, uid, monkeypatch):
    _no_sleep(monkeypatch)
    _no_real_jwt(monkeypatch)
    init_push(db_path=db_path)
    _register(db_path, uid, rid)
    conn = get_conn(db_path)
    conn.execute("UPDATE device_tokens SET consecutive_failures=5 WHERE restaurant_id=?", (rid,))
    conn.commit()
    conn.close()
    token_row = get_device_tokens(rid, db_path=db_path)[0]

    fake_client, calls = _fake_httpx_client(status_code=200)
    monkeypatch.setattr("httpx.Client", fake_client)

    _deliver(token_row, "1star", "title", "body", None, db_path=db_path)

    conn = get_conn(db_path)
    row = conn.execute("SELECT consecutive_failures, last_success_at FROM device_tokens WHERE restaurant_id=?", (rid,)).fetchone()
    conn.close()
    assert row["consecutive_failures"] == 0
    assert row["last_success_at"] is not None


def test_register_device_token_upserts_by_token(db_path, rid, uid):
    init_push(db_path=db_path)
    token = "b" * 64
    register_device_token(uid, rid, token, "sandbox", db_path=db_path)
    register_device_token(uid, rid, token, "production", db_path=db_path)  # re-register (e.g. reinstall)

    rows = get_device_tokens(rid, db_path=db_path)
    assert len(rows) == 1
    assert rows[0]["environment"] == "production"


def test_register_device_token_clears_prior_failures(db_path, rid, uid):
    init_push(db_path=db_path)
    token = "c" * 64
    register_device_token(uid, rid, token, "production", db_path=db_path)
    conn = get_conn(db_path)
    conn.execute("UPDATE device_tokens SET consecutive_failures=8 WHERE apns_token=?", (token,))
    conn.commit()
    conn.close()

    register_device_token(uid, rid, token, "production", db_path=db_path)

    row = get_device_tokens(rid, db_path=db_path)[0]
    assert row["consecutive_failures"] == 0


def test_remove_device_token_deletes_it(db_path, rid, uid):
    init_push(db_path=db_path)
    token = "d" * 64
    register_device_token(uid, rid, token, "production", db_path=db_path)
    remove_device_token(token, db_path=db_path)
    assert get_device_tokens(rid, db_path=db_path) == []


def test_fire_push_is_a_noop_with_no_registered_devices(db_path, rid):
    """No device registered — the alert-firing code in notify.py calls this
    unconditionally when al_*_push is on; it must not raise."""
    init_push(db_path=db_path)
    push.fire_push(rid, "1star", "title", "body", db_path=db_path)  # should not raise


# ── delivery concurrency is bounded ─────────────────────────────────────────
#
# fire_push used to start one thread per device. _deliver retries with
# backoff, so each thread lives for seconds, and the scheduler fires push for
# every restaurant in one pass — fifty restaurants with a couple of devices
# each meant a hundred-plus concurrent threads on a small Railway container,
# every one holding an HTTP/2 connection and writing to the same SQLite file.

def test_deliveries_run_on_a_bounded_pool_not_a_thread_per_device(db_path, rid, uid, monkeypatch):
    import threading as _t
    init_push(db_path=db_path)
    for i in range(12):
        register_device_token(uid, rid, f"tok{i:02d}" + "0" * 58, "production", db_path=db_path)

    peak = {"n": 0}
    live = {"n": 0}
    lock = _t.Lock()
    release = _t.Event()

    def slow_deliver(*a, **k):
        with lock:
            live["n"] += 1
            peak["n"] = max(peak["n"], live["n"])
        release.wait(2)
        with lock:
            live["n"] -= 1

    monkeypatch.setattr(push, "_deliver", slow_deliver)
    monkeypatch.setattr(push, "_MAX_PUSH_WORKERS", 4)
    monkeypatch.setattr(push, "_executor", None)  # rebuild the pool at the patched size

    push.fire_push(rid, "1star", "t", "b", db_path=db_path)
    _t.Event().wait(0.3)
    observed_peak = peak["n"]
    release.set()
    push._push_executor().shutdown(wait=True)
    monkeypatch.setattr(push, "_executor", None)

    assert observed_peak <= 4, f"{observed_peak} deliveries ran at once for 12 devices"
    assert observed_peak >= 2, "the pool should still deliver in parallel, not one at a time"


def test_every_device_is_still_delivered_to(db_path, rid, uid, monkeypatch):
    """Bounding concurrency must not drop anyone."""
    import threading as _t
    seen = []
    lock = _t.Lock()
    init_push(db_path=db_path)
    for i in range(7):
        register_device_token(uid, rid, f"dev{i:02d}" + "0" * 58, "production", db_path=db_path)

    def record(token_row, *a, **k):
        with lock:
            seen.append(token_row["apns_token"])

    monkeypatch.setattr(push, "_deliver", record)
    push.fire_push(rid, "1star", "t", "b", db_path=db_path)
    push._push_executor().shutdown(wait=True)
    monkeypatch.setattr(push, "_executor", None)

    assert len(seen) == 7
    assert len(set(seen)) == 7


def test_a_full_queue_drops_rather_than_exhausting_the_container(db_path, rid, uid, monkeypatch):
    init_push(db_path=db_path)
    register_device_token(uid, rid, "z" * 64, "production", db_path=db_path)
    calls = []
    monkeypatch.setattr(push, "_deliver", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(push, "_MAX_PUSH_QUEUED", 0)
    push.fire_push(rid, "1star", "t", "b", db_path=db_path)
    push._push_executor().shutdown(wait=True)
    monkeypatch.setattr(push, "_executor", None)
    assert calls == [], "past the ceiling, a delivery is dropped rather than queued forever"
